from __future__ import annotations

import json
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.kb.derived_revisions import (
    DerivedRevisionError,
    DerivedRevisionStore,
    logical_identity,
    maintenance_observations,
)

ROOT = Path(__file__).resolve().parents[3]
NAMESPACE = "maintenance:test-pack"
PRODUCER = {"name": "fixture", "version": "1.0.0"}


def observation(
    document_id: str,
    revision_id: str,
    statement: str = "The facility remained open",
) -> dict:
    return {
        "object_type": "claim",
        "content": {"statement": statement, "stance": "support"},
        "document_id": document_id,
        "source_revision_id": revision_id,
        "producer": PRODUCER,
        "configuration": {"rules": "v1"},
        "observed_at_ms": 100,
    }


def change(document_id: str, revision_id: str, kind: str = "added") -> dict:
    return {
        "document_id": document_id,
        "revision_id": revision_id,
        "change_kind": kind,
    }


def publish(store: DerivedRevisionStore, receipt: dict) -> dict:
    store.publish_generation(receipt["namespace"], receipt["generation"])
    return receipt


def test_support_aware_revisions_retain_then_retract_shared_claim():
    conn = duckdb.connect(":memory:")
    store = DerivedRevisionStore(conn)
    logical_id = logical_identity("claim", observation("a", "r1")["content"])

    first = publish(
        store,
        store.apply_generation(
            NAMESPACE,
            1,
            [observation("a", "r1"), observation("b", "r2")],
            [change("a", "r1"), change("b", "r2")],
            now_ms=100,
        ),
    )
    assert first["counts"] == {"added": 1}
    assert len(store.revision(NAMESPACE, logical_id)["support"]) == 2

    second = publish(
        store,
        store.apply_generation(
            NAMESPACE,
            2,
            [observation("a", "r3", "The facility closed")],
            [change("a", "r3", "updated")],
            now_ms=200,
        ),
    )
    assert second["counts"] == {"added": 1, "support_updated": 1}
    still_active = store.revision(NAMESPACE, logical_id)
    assert still_active and [
        item["document_id"] for item in still_active["support"]
    ] == ["b"]

    third = publish(
        store,
        store.apply_generation(
            NAMESPACE,
            3,
            [],
            [change("b", "r4", "retracted")],
            now_ms=300,
        ),
    )
    assert third["counts"] == {"retracted": 1}
    assert store.revision(NAMESPACE, logical_id) is None
    retracted = store.revision(NAMESPACE, logical_id, include_retracted=True)
    assert retracted and retracted["revision"] == 3
    assert retracted["lifecycle"] == "retracted"
    assert not any(
        item["logical_id"] == logical_id
        for item in store.projection(NAMESPACE, "lexical")
    )
    conn.close()


def test_as_of_history_delta_pagination_lineage_and_replay():
    conn = duckdb.connect(":memory:")
    store = DerivedRevisionStore(conn)
    item = observation("a", "r1")
    logical_id = logical_identity("claim", item["content"])
    first = publish(
        store,
        store.apply_generation(NAMESPACE, 1, [item], [change("a", "r1")], now_ms=10),
    )
    second = publish(
        store,
        store.apply_generation(
            NAMESPACE,
            2,
            [observation("a", "r2", "The facility closed")],
            [change("a", "r2", "corrected")],
            now_ms=20,
        ),
    )

    assert store.revision(NAMESPACE, logical_id, generation=1)["lifecycle"] == "active"
    assert (
        store.revision(NAMESPACE, logical_id, generation=2, include_retracted=True)[
            "lifecycle"
        ]
        == "retracted"
    )
    assert len(store.history(NAMESPACE, logical_id, include_retracted=False)) == 1
    assert len(store.history(NAMESPACE, logical_id, include_retracted=True)) == 2
    page = store.delta(NAMESPACE, from_generation=1, to_generation=2, limit=1)
    assert page["page_count"] == 1 and page["next_cursor"]
    tail = store.delta(
        NAMESPACE,
        from_generation=1,
        to_generation=2,
        cursor=page["next_cursor"],
        limit=10,
    )
    assert page["item_count"] == first["item_count"] + second["item_count"]
    assert tail["changes"]
    assert store.replay(NAMESPACE, 1, 2)["verified"]
    lineage = store.lineage(first["changed"][0]["revision_id"])
    assert lineage["complete"] and lineage["sources"][0]["source_revision_id"] == "r1"
    explanation = store.explain_invalidation(NAMESPACE, logical_id)
    assert [item["change_kind"] for item in explanation["changes"]] == [
        "retracted",
        "added",
    ]
    conn.close()


def test_generation_is_idempotent_conflict_safe_and_atomic():
    conn = duckdb.connect(":memory:")
    store = DerivedRevisionStore(conn)
    inputs = [observation("a", "r1")]
    changes = [change("a", "r1")]
    committed = store.apply_generation(NAMESPACE, 1, inputs, changes, now_ms=10)
    assert store.revision(NAMESPACE, committed["changed"][0]["logical_id"]) is None
    assert store.projection(NAMESPACE, "lexical") == []
    store.publish_generation(NAMESPACE, 1)
    repeated = store.apply_generation(NAMESPACE, 1, inputs, changes, now_ms=20)
    assert (
        repeated["idempotent"] and repeated["change_hash"] == committed["change_hash"]
    )
    with pytest.raises(DerivedRevisionError, match="different inputs"):
        store.apply_generation(
            NAMESPACE, 1, [observation("b", "r2")], [change("b", "r2")]
        )
    with pytest.raises(DerivedRevisionError, match="conflicting object content"):
        left = observation("a", "same", "One")
        right = observation("a", "same", "Two")
        left["logical_id"] = right["logical_id"] = "forced-conflict"
        store.apply_generation(
            NAMESPACE,
            2,
            [left, right],
            [change("a", "same")],
        )
    assert store.health()["generations"] == 1
    conn.close()


def test_incremental_materializers_and_workflow_observation_adapter():
    documents = [
        {
            "document_id": "d1",
            "_revision_id": "document-revision:1",
            "title": "Study",
            "content": "A result was replicated.",
            "metadata": {"domain": "research"},
        }
    ]
    extraction = {
        "outputs": [
            {
                "input_id": "d1",
                "output": {
                    "output_type": "entity",
                    "value": {"name": "Result", "kind": "study"},
                },
                "provenance": {"extractor_name": "fixture", "extractor_version": "1"},
            },
            {
                "input_id": "d1",
                "output": {
                    "output_type": "relation",
                    "value": {
                        "subject": "study",
                        "predicate": "replicates",
                        "object": "result",
                    },
                },
                "provenance": {"extractor_name": "fixture", "extractor_version": "1"},
            },
        ]
    }
    observations = maintenance_observations(documents, extraction)
    assert {item["object_type"] for item in observations} == {
        "embedding",
        "entity",
        "index",
        "relation",
        "summary",
    }
    conn = duckdb.connect(":memory:")
    store = DerivedRevisionStore(conn)
    result = publish(
        store,
        store.apply_generation(
            NAMESPACE,
            1,
            observations,
            [change("d1", "document-revision:1")],
            now_ms=50,
        ),
    )
    assert result["item_count"] == 5
    assert {
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT projection_kind FROM derived_projection_items"
        ).fetchall()
    } == {"graph", "lexical", "summary", "vector"}
    assert len(store.projection(NAMESPACE, "graph")) == 2
    assert store.health()["objects"] == 5
    conn.close()


def test_public_contract_schemas_validate_store_receipts():
    conn = duckdb.connect(":memory:")
    store = DerivedRevisionStore(conn)
    receipt = publish(
        store,
        store.apply_generation(
            NAMESPACE,
            1,
            [observation("a", "r1")],
            [change("a", "r1")],
            now_ms=10,
        ),
    )
    revision = store.revision(
        NAMESPACE, receipt["changed"][0]["logical_id"], include_retracted=True
    )
    delta = store.delta(NAMESPACE, from_generation=1, to_generation=1)
    replay = store.replay(NAMESPACE, 1, 1)
    lineage = store.lineage(revision["revision_id"])
    values = {
        "noesis-derived-object-generation-v1.json": receipt,
        "noesis-derived-object-revision-v1.json": revision,
        "noesis-derived-object-generation-delta-v1.json": delta,
        "noesis-derived-object-replay-v1.json": replay,
        "noesis-derived-object-lineage-v1.json": lineage,
    }
    for name, value in values.items():
        schema = json.loads((ROOT / "contracts/schemas/jsonschema" / name).read_text())
        jsonschema.validate(value, schema)
    conn.close()
