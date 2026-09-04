from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema.validators import validator_for

from src.ingestion.document_store import DocumentStore
from src.ingestion.revisions import DocumentRevisionStore, RevisionError

ROOT = Path(__file__).resolve().parents[3]


def payload(
    run: str,
    *,
    title: str = "Initial",
    content: str = "A sufficiently long original record body.",
    lifecycle: str = "active",
):
    return {
        "document_id": "record:1",
        "source_type": "news",
        "language": "en",
        "ingested_at": int(run[-1]) * 100,
        "created_at": 50,
        "source_id": "fixture-source",
        "url": "https://example.test/record/1",
        "title": title,
        "content": content,
        "authors": [],
        "metadata": {
            "source_pack_id": "fixture-pack",
            "source_pack_run_id": run,
            "lifecycle": lifecycle,
        },
    }


def commit(store: DocumentStore, run: str, watermark: int):
    return store.revisions.commit_change_set(
        "fixture-pack", watermark, run, committed_at_ms=watermark * 1000
    )


def test_immutable_revisions_unchanged_observations_and_metadata_edits():
    conn = duckdb.connect(":memory:")
    store = DocumentStore(conn)
    first = store.upsert([payload("run-1")])
    assert first.inserted == 1
    assert commit(store, "run-1", 1)["counts"] == {"added": 1}

    unchanged = store.upsert([payload("run-2")])
    assert (
        unchanged.duplicate == 1 and unchanged.changes[0]["change_kind"] == "unchanged"
    )
    assert commit(store, "run-2", 2)["counts"] == {"unchanged": 1}
    assert conn.execute(
        "SELECT COUNT(*) FROM document_revision_records"
    ).fetchone() == (1,)

    edited = store.upsert([payload("run-3", title="Metadata corrected")])
    assert edited.updated == 1
    assert edited.changes[0]["change_kind"] == "metadata_updated"
    commit(store, "run-3", 3)
    history = store.revisions.history("record:1")
    assert [item["revision"] for item in history] == [0, 1]
    assert history[1]["predecessor_revision_id"] == history[0]["revision_id"]


def test_delta_paging_replay_and_range_validation():
    store = DocumentStore(duckdb.connect(":memory:"))
    store.upsert([payload("run-1")])
    commit(store, "run-1", 1)
    store.upsert(
        [
            payload(
                "run-2",
                content="A substantively changed record body with revised facts.",
            )
        ]
    )
    commit(store, "run-2", 2)

    first = store.revisions.delta(
        "fixture-pack", from_watermark=1, to_watermark=2, limit=1
    )
    second = store.revisions.delta(
        "fixture-pack",
        from_watermark=1,
        to_watermark=2,
        cursor=first["next_cursor"],
        limit=1,
    )
    assert [first["changes"][0]["watermark"], second["changes"][0]["watermark"]] == [
        1,
        2,
    ]
    assert first["delta_hash"] == second["delta_hash"]
    assert store.revisions.replay("fixture-pack", 1, 2)["verified"]
    with pytest.raises(RevisionError, match="must not exceed"):
        store.revisions.delta("fixture-pack", from_watermark=2, to_watermark=1)
    with pytest.raises(RevisionError) as caught:
        commit(store, "run-2", 3)
    assert caught.value.code == "mixed_generation"


def test_retraction_hidden_by_default_but_available_to_explicit_history():
    store = DocumentStore(duckdb.connect(":memory:"))
    store.upsert([payload("run-1")])
    commit(store, "run-1", 1)
    store.upsert(
        [
            payload(
                "run-2",
                content="This article has been retracted by the publisher.",
                lifecycle="retracted",
            )
        ]
    )
    commit(store, "run-2", 2)

    assert store.revisions.revision("record:1") is None
    retracted = store.revisions.revision("record:1", include_retracted=True)
    assert retracted["revision"] == 1 and retracted["lifecycle"] == "retracted"
    before = store.revisions.revision("record:1", generation=1)
    assert before["revision"] == 0 and before["generation"] == 1
    assert len(store.revisions.history("record:1")) == 2


def test_existing_documents_migrate_to_deterministic_revision_zero():
    conn = duckdb.connect(":memory:")
    store = DocumentStore(conn)
    store.upsert(
        [
            {
                "document_id": "legacy",
                "source_type": "note",
                "language": "en",
                "ingested_at": 10,
                "content": "Legacy knowledge record.",
                "authors": [],
                "metadata": {},
            }
        ]
    )
    conn.execute("DELETE FROM document_current_revisions")
    conn.execute("DELETE FROM document_revision_records")
    migrated = DocumentRevisionStore(conn)
    revision_id = migrated.revision("legacy")["revision_id"]
    conn.execute("DELETE FROM document_current_revisions")
    conn.execute("DELETE FROM document_revision_records")
    assert DocumentRevisionStore(conn).revision("legacy")["revision_id"] == revision_id
    assert DocumentRevisionStore(conn).migrate_current_documents() == 0


def test_uncommitted_source_revision_is_not_queryable():
    store = DocumentStore(duckdb.connect(":memory:"))
    store.upsert([payload("run-1")])
    assert store.revisions.revision("record:1", include_retracted=True) is None
    commit(store, "run-1", 1)
    assert store.revisions.revision("record:1")["generation"] == 1
    with pytest.raises(RevisionError) as caught:
        store.revisions.revision("record:1", generation=2)
    assert caught.value.code == "generation_unavailable"


def test_source_change_set_accounts_for_distinct_identical_records():
    store = DocumentStore(duckdb.connect(":memory:"))
    first = payload("run-1")
    second = {**first, "document_id": "record:2"}
    summary = store.upsert([first, second])
    assert summary.inserted == 2
    change_set = commit(store, "run-1", 1)
    assert change_set["item_count"] == 2
    assert change_set["counts"] == {"added": 2}


def test_public_revision_contract_schemas_validate_runtime_values():
    store = DocumentStore(duckdb.connect(":memory:"))
    store.upsert([payload("run-1")])
    change_set = commit(store, "run-1", 1)
    values = {
        "noesis-document-revision-v1.json": store.revisions.revision("record:1"),
        "noesis-document-change-set-v1.json": change_set,
        "noesis-document-generation-delta-v1.json": store.revisions.delta(
            "fixture-pack", from_watermark=1, to_watermark=1
        ),
        "noesis-document-delta-replay-v1.json": store.revisions.replay(
            "fixture-pack", 1, 1
        ),
    }
    for name, value in values.items():
        schema = json.loads(
            (ROOT / "contracts" / "schemas" / "jsonschema" / name).read_text()
        )
        validator = validator_for(schema)
        validator.check_schema(schema)
        assert not list(validator(schema).iter_errors(value))
