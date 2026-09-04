from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from jsonschema import Draft202012Validator

from src.kb.dataset_intelligence import (
    CALCULATE_SCOPE,
    INGEST_SCOPE,
    READ_SCOPE,
    WRITE_SCOPE,
    DatasetIntelligenceError,
    DatasetIntelligenceStore,
)

READ = {READ_SCOPE}
WRITE = {WRITE_SCOPE}
INGEST = {INGEST_SCOPE}
CALCULATE = {CALCULATE_SCOPE}
SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def _validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def _schema(value_name="value", *, unit="count", frequency="annual"):
    return [
        {
            "name": "observations",
            "identity": "observations",
            "frequency": frequency,
            "primary_key": ["geo", "period"],
            "columns": [
                {
                    "name": "geo",
                    "type": "string",
                    "nullable": False,
                    "semantic_role": "geography",
                    "code_list_id": "iso-2",
                },
                {
                    "name": "period",
                    "type": "string",
                    "nullable": False,
                    "semantic_role": "time",
                },
                {
                    "name": value_name,
                    "type": "number",
                    "nullable": True,
                    "unit": unit,
                    "semantic_role": "measure",
                    "renamed_from": "value" if value_name != "value" else None,
                },
            ],
        }
    ]


def _dataset(store, native="population", version="1", predecessor=None, **updates):
    values = {
        "namespace": "economic",
        "publisher_id": "publisher:statistics",
        "native_id": native,
        "semantic_version": version,
        "title": f"Dataset {native}",
        "description": "Official observations",
        "license": {"id": "CC-BY-4.0"},
        "tables": _schema(),
        "code_lists": [
            {"native_id": "geo", "name": "Geography", "codes": {"DE": "Germany"}}
        ],
        "partitions": [{"dimension": "year"}],
        "predecessor_revision_id": predecessor,
        "principal_id": "curator",
        "scopes": WRITE,
        "observed_at_ms": int(version) * 10,
        "provenance": {"catalog": f"catalog:{native}"},
    }
    values.update(updates)
    return store.register_dataset(**values)


def _release(store, dataset, native="2026-01", **updates):
    values = {
        "namespace": dataset["namespace"],
        "dataset_id": dataset["dataset_id"],
        "native_release_id": native,
        "vintage_id": native,
        "retrieved_at_ms": 100,
        "published_at_ms": 90,
        "principal_id": "curator",
        "scopes": WRITE,
        "provenance": {"url": f"https://example.test/{native}"},
    }
    values.update(updates)
    return store.register_release(**values)


def test_catalog_schema_evolution_renames_partitions_and_duplicate_releases():
    conn = duckdb.connect(":memory:")
    store = DatasetIntelligenceStore(conn, now=lambda: 1000)
    first = _dataset(store)
    first_table = first["tables"][0]
    first_value = next(
        item for item in first_table["columns"] if item["name"] == "value"
    )
    second = _dataset(
        store,
        version="2",
        predecessor=first["dataset_revision_id"],
        tables=_schema("observation_value"),
    )
    renamed = next(
        item
        for item in second["tables"][0]["columns"]
        if item["name"] == "observation_value"
    )
    assert renamed["column_id"] == first_value["column_id"]
    assert second["tables"][0]["table_id"] == first_table["table_id"]
    assert second["partitions"] == [{"dimension": "year"}]
    release = _release(store, second)
    assert _release(store, second)["idempotent"]
    with pytest.raises(DatasetIntelligenceError, match="different content"):
        _release(store, second, vintage_id="changed")
    _validate("noesis-dataset-catalog-v1.json", second)
    _validate("noesis-dataset-release-v1.json", release)
    conn.close()


def test_releases_corrections_suppression_null_semantics_and_large_partition():
    conn = duckdb.connect(":memory:")
    store = DatasetIntelligenceStore(conn, now=lambda: 1000)
    dataset = _dataset(store)
    table_id = dataset["tables"][0]["table_id"]
    first = _release(store, dataset, "initial")
    rows = [
        {"geo": "DE", "period": str(2000 + index), "value": index}
        for index in range(1002)
    ]
    rows[1]["value"] = {"value": None, "null_semantic": "suppressed"}
    rows[2]["value"] = {"value": None, "null_semantic": "not-applicable"}
    receipt = store.ingest(
        "economic",
        first["release_id"],
        table_id,
        "json",
        json.dumps(rows),
        {"year": "all"},
        row_limit=1000,
        principal_id="operator",
        scopes=INGEST,
    )
    assert receipt["truncated"] and receipt["counts"]["inserted"] == 1000
    page = store.slice("economic", first["release_id"], table_id, scopes=READ, limit=3)
    assert page["items"][1]["null_semantics"]["value"] == "suppressed"
    assert page["items"][2]["null_semantics"]["value"] == "not-applicable"
    correction = _release(
        store,
        dataset,
        "correction",
        revision_of=first["release_id"],
        published_at_ms=200,
    )
    store.ingest(
        "economic",
        correction["release_id"],
        table_id,
        "json",
        json.dumps([{"geo": "DE", "period": "2000", "value": 99}]),
        {"year": "all"},
        principal_id="operator",
        scopes=INGEST,
    )
    comparison = store.compare_releases(
        "economic", first["release_id"], correction["release_id"], table_id, scopes=READ
    )
    assert comparison["changes"][0]["after"]["values"]["value"] == 99
    _validate("noesis-tabular-ingestion-receipt-v1.json", receipt)
    _validate("noesis-dataset-slice-v1.json", page)
    conn.close()


def test_csv_jsonl_parquet_api_malformed_encoding_drift_cancellation_and_replay():
    conn = duckdb.connect(":memory:")
    store = DatasetIntelligenceStore(conn, now=lambda: 1000)
    dataset = _dataset(store)
    table_id = dataset["tables"][0]["table_id"]
    formats = {
        "csv": "geo,period,value\nDE,2024,1\n",
        "jsonl": '{"geo":"DE","period":"2024","value":1}\n',
        "tabular-api": json.dumps(
            {"items": [{"geo": "DE", "period": "2024", "value": 1}]}
        ),
    }
    for index, (format, content) in enumerate(formats.items()):
        release = _release(store, dataset, f"release-{index}")
        receipt = store.ingest(
            "economic",
            release["release_id"],
            table_id,
            format,
            content,
            {"format": format},
            principal_id="operator",
            scopes=INGEST,
        )
        assert receipt["status"] == "completed"
        assert store.replay_ingestion("economic", receipt["receipt_id"], scopes=READ)[
            "deterministic"
        ]
    sink = io.BytesIO()
    pq.write_table(pa.table({"geo": ["DE"], "period": ["2024"], "value": [1.0]}), sink)
    release = _release(store, dataset, "parquet")
    parquet = store.ingest(
        "economic",
        release["release_id"],
        table_id,
        "parquet",
        base64.b64encode(sink.getvalue()).decode(),
        {},
        principal_id="operator",
        scopes=INGEST,
    )
    assert parquet["counts"]["inserted"] == 1
    bad = _release(store, dataset, "bad")
    malformed = store.ingest(
        "economic",
        bad["release_id"],
        table_id,
        "csv",
        "geo,period,value\nDE,2024,nope\n",
        {},
        principal_id="operator",
        scopes=INGEST,
    )
    assert malformed["status"] == "partial" and malformed["counts"]["quarantined"] == 1
    drift = _release(store, dataset, "drift")
    drifted = store.ingest(
        "economic",
        drift["release_id"],
        table_id,
        "json",
        '[{"geo":"DE","period":"2024","value":1,"extra":2}]',
        {},
        principal_id="operator",
        scopes=INGEST,
    )
    assert drifted["schema_drift"] == ["extra"]
    cancelled = store.ingest(
        "economic",
        drift["release_id"],
        table_id,
        "json",
        "[]",
        {"cancelled": True},
        cancel_requested=True,
        principal_id="operator",
        scopes=INGEST,
    )
    assert cancelled["status"] == "cancelled"
    with pytest.raises(DatasetIntelligenceError, match="cannot be decoded"):
        store.ingest(
            "economic",
            drift["release_id"],
            table_id,
            "csv",
            "é",
            {"encoding": "bad"},
            encoding="ascii",
            principal_id="operator",
            scopes=INGEST,
        )
    conn.close()


def test_join_discovery_many_to_many_mismatches_lineage_and_namespace_leakage():
    conn = duckdb.connect(":memory:")
    store = DatasetIntelligenceStore(conn, now=lambda: 1000)
    left = _dataset(
        store, native="left", tables=_schema(unit="usd", frequency="annual")
    )
    right = _dataset(
        store, native="right", tables=_schema(unit="eur", frequency="monthly")
    )
    left_table, right_table = left["tables"][0], right["tables"][0]
    suggestions = store.suggest_joins(
        "economic", left["dataset_id"], right["dataset_id"], scopes=READ
    )
    assert {item["basis"] for item in suggestions["items"]} & {
        "code-list",
        "semantic-role",
    }
    assert any("unit-mismatch" in item["warnings"] for item in suggestions["items"])
    left_release, right_release = (
        _release(store, left, "left-v1"),
        _release(store, right, "right-v1"),
    )
    duplicate_rows = [
        {"geo": "DE", "period": "2024", "value": 1},
        {"geo": "DE", "period": "2024", "value": 2},
    ]
    for release, table in ((left_release, left_table), (right_release, right_table)):
        store.ingest(
            "economic",
            release["release_id"],
            table["table_id"],
            "json",
            json.dumps(duplicate_rows),
            {},
            principal_id="operator",
            scopes=INGEST,
        )
    preview = store.preview_join(
        "economic",
        left_release["release_id"],
        right_release["release_id"],
        left_table["table_id"],
        right_table["table_id"],
        [{"left": "geo", "right": "geo"}, {"left": "period", "right": "period"}],
        scopes=CALCULATE,
    )
    assert preview["cardinality"] == "many-to-many"
    assert {"many-to-many", "temporal-mismatch"} <= set(preview["warnings"])
    accepted = store.accept_join(
        "economic", preview, principal_id="analyst", scopes=WRITE
    )
    assert accepted["lineage"]["inputs"] == [
        left_release["release_id"],
        right_release["release_id"],
    ]
    assert (
        store.lineage("economic", accepted["transformation_id"], scopes=READ)[
            "derived_table_id"
        ]
        == accepted["derived_table_id"]
    )
    _validate("noesis-dataset-join-v1.json", accepted)
    with pytest.raises(DatasetIntelligenceError, match="both datasets"):
        store.suggest_joins(
            "scientific", left["dataset_id"], right["dataset_id"], scopes=READ
        )
    conn.close()


def test_search_slice_pagination_budgets_economic_scientific_and_authorization():
    conn = duckdb.connect(":memory:")
    store = DatasetIntelligenceStore(conn, now=lambda: 1000)
    for namespace in ("economic", "scientific"):
        dataset = _dataset(store, native=f"{namespace}-fixture", namespace=namespace)
        release = _release(store, dataset)
        table_id = dataset["tables"][0]["table_id"]
        store.ingest(
            namespace,
            release["release_id"],
            table_id,
            "json",
            json.dumps(
                [
                    {"geo": "DE", "period": str(2020 + index), "value": index}
                    for index in range(5)
                ]
            ),
            {},
            principal_id="operator",
            scopes=INGEST,
        )
        found = store.search(namespace, "fixture", scopes=READ, limit=1)
        assert found["items"][0]["namespace"] == namespace
        first = store.slice(
            namespace, release["release_id"], table_id, scopes=READ, limit=2
        )
        second = store.slice(
            namespace,
            release["release_id"],
            table_id,
            scopes=READ,
            limit=2,
            offset=first["next_offset"],
        )
        assert len(first["items"] + second["items"]) == 4
    with pytest.raises(DatasetIntelligenceError, match="missing required scope"):
        store.search("economic", "fixture", scopes={"knowledge:read"})
    conn.close()
