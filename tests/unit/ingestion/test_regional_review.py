import hashlib
import json

import duckdb
import pytest

from src.ingestion.document_store import DocumentStore
from src.ingestion.provider_execution import ProviderError
from src.ingestion.regional_review import queue_candidate
from src.ingestion.regional_workflow import RegionalAcquisition
from src.kb.entity_history import EntityHistoryStore
from src.kb.review_inbox import ReviewInboxStore


def import_trial(conn, status="recruiting", observation="first"):
    raw = json.dumps(
        [
            {
                "id": "DRKS00000001",
                "title": "Berliner Studie",
                "status": status,
                "ids": ["2024-123456-12-00"],
            }
        ]
    ).encode()
    return RegionalAcquisition(
        conn, namespace="science", principal_id="a", reuse_notice="authored fixture"
    ).import_export(
        "drks",
        raw,
        {
            "source_url": "https://drks.de/search/de/trial/DRKS00000001",
            "format": "json",
            "export_schema_version": "fixture-v1",
            "field_map": {
                "id": "/id",
                "title": "/title",
                "registry_status": "/status",
                "identifiers": "/ids",
            },
        },
        observation,
        scopes={"operator"},
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )


def queue(conn, result, **overrides):
    return queue_candidate(
        conn,
        **{
            "namespace": "science",
            "principal_id": "a",
            "scopes": {"operator"},
            "observation_id": result["receipt"]["observation_id"],
            "record_index": 0,
            "relationship_index": 0,
            "domain": "clinical-trials",
            **overrides,
        },
    )


def test_registry_link_enters_existing_review_and_replays_without_merging():
    conn = duckdb.connect()
    result = import_trial(conn)
    task = queue(conn, result)
    assert task["status"] == "unassigned"
    assert task["sources"] == result["receipt"]["source_refs"]
    assert queue(conn, result)["task_id"] == task["task_id"]
    assert (
        conn.execute("SELECT count(*) FROM entity_identity_decisions").fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT count(*) FROM entity_history_redirects").fetchone()[0] == 0
    )
    inbox = ReviewInboxStore(conn)
    inbox.assign(
        "science",
        task["task_id"],
        ["reviewer-1", "reviewer-2"],
        principal_id="a",
        scopes={"operator"},
    )
    for reviewer in ("reviewer-1", "reviewer-2"):
        inbox.submit(
            "science",
            task["task_id"],
            task["target_revision_hash"],
            {"decision": "non-match"},
            "Distinct registrations in this authored test",
            100,
            "human",
            principal_id=reviewer,
            scopes={"operator"},
        )
    inbox.resolve(
        "science",
        task["task_id"],
        "Fixture review routing",
        principal_id="a",
        scopes={"operator"},
    )
    assert (
        conn.execute(
            "SELECT count(*) FROM entity_identity_decisions WHERE decision_type='non-match'"
        ).fetchone()[0]
        == 1
    )
    assert (
        conn.execute("SELECT count(*) FROM entity_history_redirects").fetchone()[0] == 0
    )
    conn.close()


def test_old_import_replay_does_not_restore_old_revision_and_review_rejects_it():
    conn = duckdb.connect()
    first = import_trial(conn)
    newer = import_trial(conn, "withdrawn", "second")
    # New workflow ID with the same old capture must also remain an offline replay.
    replay = import_trial(conn, observation="old-retry")
    assert replay["receipt"]["source_refs"] == first["receipt"]["source_refs"]
    doc_id = newer["receipt"]["document_ids"][0]
    assert (
        DocumentStore(conn).revisions.revision(doc_id)["revision_id"]
        == newer["receipt"]["source_refs"][0]["revision_id"]
    )
    with pytest.raises(ProviderError, match="current active"):
        queue(conn, first)
    assert queue(conn, newer)["status"] == "unassigned"
    conn.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"scopes": set()},
        {"record_index": True},
        {"relationship_index": 99},
        {"impact": float("nan")},
    ],
)
def test_invalid_or_unauthorized_candidate_does_not_create_decisions(overrides):
    conn = duckdb.connect()
    first = import_trial(conn)
    EntityHistoryStore(conn)
    with pytest.raises((ValueError, ProviderError)):
        queue(conn, first, **overrides)
    assert (
        conn.execute("SELECT count(*) FROM entity_identity_decisions").fetchone()[0]
        == 0
    )
    conn.close()


def test_sanctions_query_is_not_automatically_a_canonical_identity():
    from src.ingestion.regional_providers import RegionalEvidenceStore
    from tests.unit.ingestion.test_regional_providers import native_client

    client, _, conn = native_client(
        "opensanctions",
        {
            "responses": {
                "query-1": {
                    "results": [
                        {
                            "id": "candidate-1",
                            "caption": "Müller",
                            "schema": "Person",
                            "score": 0.8,
                            "datasets": ["authored-fixture"],
                            "properties": {"name": ["Müller"]},
                        }
                    ]
                }
            }
        },
        key="fixture-secret",
    )
    records, captured = client.sanctions_match(
        {"query-1": {"schema": "Person", "properties": {"name": ["Müller"]}}}, "matches"
    )
    receipt = RegionalEvidenceStore(conn).ingest(
        records["records"],
        captured,
        namespace="science",
        principal_id="a",
        scopes={"operator"},
        reuse_notice="authored fixture",
    )
    result = {"receipt": receipt}
    with pytest.raises(ValueError, match="existing local entity"):
        queue(conn, result, relationship_index=None)
    documents = DocumentStore(conn)
    documents.upsert(
        [
            {
                "document_id": "local-source",
                "source_type": "note",
                "language": "de",
                "ingested_at": 1,
                "content": "Müller",
            }
        ]
    )
    ref = {
        "document_id": "local-source",
        "revision_id": documents.revisions.revision("local-source")["revision_id"],
    }
    history = EntityHistoryStore(conn)
    history.register_entity(
        "science", "local-person", principal_id="a", scopes={"operator"}
    )
    task = queue(
        conn,
        result,
        relationship_index=None,
        entity_id="local-person",
        entity_sources=[ref],
    )
    assert len(task["sources"]) == 2
    assert (
        conn.execute("SELECT count(*) FROM entity_history_redirects").fetchone()[0] == 0
    )
    payload = json.loads(
        conn.execute("SELECT payload_json FROM entity_identity_decisions").fetchone()[0]
    )
    assert (
        payload["payload"]["candidate"]["local_entity_assignment"]
        == "explicit-coordinator-review-hypothesis"
    )
    assert payload["payload"]["candidate"]["provider_score"] == 0.8
    conn.close()
