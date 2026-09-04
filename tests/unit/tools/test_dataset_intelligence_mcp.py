from __future__ import annotations

import asyncio
import inspect
import json

import duckdb

from tools.knowledge_engine_mcp import server


def _call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def _register(tools, namespace, native):
    return _call(
        tools["register_dataset_catalog"],
        namespace=namespace,
        publisher_id=f"publisher:{namespace}",
        native_id=native,
        semantic_version="1",
        title=f"{namespace} fixture {native}",
        description="Offline conformance dataset",
        license={"id": "CC-BY-4.0"},
        tables=[
            {
                "name": "observations",
                "identity": "observations",
                "primary_key": ["period"],
                "columns": [
                    {
                        "name": "period",
                        "type": "string",
                        "nullable": False,
                        "semantic_role": "time",
                    },
                    {
                        "name": "value",
                        "type": "number",
                        "unit": "count",
                        "semantic_role": "measure",
                    },
                ],
            }
        ],
        code_lists=[],
        partitions=[{"dimension": "year"}],
        observed_at_ms=10,
    )


def test_dataset_mcp_catalog_ingestion_queries_join_lineage_and_auth(
    tmp_path, monkeypatch
):
    database = tmp_path / "datasets.duckdb"
    scopes = {"knowledge:dataset:read"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(database))
    )
    tools = asyncio.run(server.mcp.get_tools())
    expected = {
        "register_dataset_catalog",
        "get_dataset_catalog",
        "search_datasets",
        "register_dataset_release",
        "get_dataset_release",
        "ingest_tabular_dataset",
        "replay_tabular_ingestion",
        "slice_dataset_table",
        "compare_dataset_releases",
        "suggest_dataset_joins",
        "preview_dataset_join",
        "accept_dataset_join",
        "get_dataset_lineage",
    }
    assert expected <= tools.keys()
    denied = _register(tools, "economic", "gdp")
    assert denied["error"]["code"] == "unauthorized"
    scopes.add("knowledge:dataset:write")
    left = _register(tools, "economic", "gdp")
    right = _register(tools, "economic", "population")
    found = _call(
        tools["search_datasets"], namespace="economic", query="fixture", limit=1
    )
    assert len(found["items"]) == 1 and found["next_offset"] == 1
    releases = []
    for dataset, native in ((left, "gdp-v1"), (right, "population-v1")):
        releases.append(
            _call(
                tools["register_dataset_release"],
                namespace="economic",
                dataset_id=dataset["dataset_id"],
                native_release_id=native,
                vintage_id="2026-09",
                retrieved_at_ms=20,
            )
        )
    denied_ingest = _call(
        tools["ingest_tabular_dataset"],
        namespace="economic",
        release_id=releases[0]["release_id"],
        table_id=left["tables"][0]["table_id"],
        format="json",
        content='[{"period":"2026","value":1}]',
    )
    assert denied_ingest["error"]["code"] == "unauthorized"
    scopes.add("knowledge:dataset:ingest")
    receipts = []
    for dataset, release in zip((left, right), releases):
        receipts.append(
            _call(
                tools["ingest_tabular_dataset"],
                namespace="economic",
                release_id=release["release_id"],
                table_id=dataset["tables"][0]["table_id"],
                format="json",
                content=json.dumps([{"period": "2026", "value": 1}]),
                partition_key={"year": 2026},
            )
        )
    sliced = _call(
        tools["slice_dataset_table"],
        namespace="economic",
        release_id=releases[0]["release_id"],
        table_id=left["tables"][0]["table_id"],
        limit=1,
    )
    assert sliced["items"][0]["values"]["value"] == 1
    assert _call(
        tools["replay_tabular_ingestion"],
        namespace="economic",
        receipt_id=receipts[0]["receipt_id"],
    )["deterministic"]
    suggestions = _call(
        tools["suggest_dataset_joins"],
        namespace="economic",
        left_dataset_id=left["dataset_id"],
        right_dataset_id=right["dataset_id"],
    )
    assert suggestions["items"]
    denied_preview = _call(
        tools["preview_dataset_join"],
        namespace="economic",
        left_release_id=releases[0]["release_id"],
        right_release_id=releases[1]["release_id"],
        left_table_id=left["tables"][0]["table_id"],
        right_table_id=right["tables"][0]["table_id"],
        keys=[{"left": "period", "right": "period"}],
    )
    assert denied_preview["error"]["code"] == "unauthorized"
    scopes.add("knowledge:dataset:calculate")
    preview = _call(
        tools["preview_dataset_join"],
        namespace="economic",
        left_release_id=releases[0]["release_id"],
        right_release_id=releases[1]["release_id"],
        left_table_id=left["tables"][0]["table_id"],
        right_table_id=right["tables"][0]["table_id"],
        keys=[{"left": "period", "right": "period"}],
    )
    accepted = _call(
        tools["accept_dataset_join"], namespace="economic", preview=preview
    )
    lineage = _call(
        tools["get_dataset_lineage"],
        namespace="economic",
        transformation_id=accepted["transformation_id"],
    )
    assert lineage["lineage"]["inputs"] == [item["release_id"] for item in releases]


def test_dataset_mcp_economic_and_scientific_fixtures(tmp_path, monkeypatch):
    database = tmp_path / "datasets-domains.duckdb"
    scopes = {"knowledge:dataset:read", "knowledge:dataset:write"}
    monkeypatch.setattr(server, "_context", lambda: ("curator", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(database))
    )
    tools = asyncio.run(server.mcp.get_tools())
    for namespace in ("economic", "scientific"):
        dataset = _register(tools, namespace, "observations")
        result = _call(
            tools["search_datasets"], namespace=namespace, query="observations"
        )
        assert result["items"][0]["dataset_id"] == dataset["dataset_id"]


def test_dataset_capabilities_advertise_contracts_and_features():
    capabilities = server.knowledge_engine_capabilities.fn()
    assert {
        "noesis-dataset-catalog-v1",
        "noesis-dataset-release-v1",
        "noesis-tabular-ingestion-receipt-v1",
        "noesis-dataset-slice-v1",
        "noesis-dataset-join-v1",
    } <= set(capabilities["contracts"])
    assert {
        "versioned-dataset-table-column-identities",
        "vintage-and-partition-aware-tabular-observations",
        "bounded-multiformat-tabular-ingestion",
        "dataset-join-discovery-and-lineage",
    } <= set(capabilities["features"])
