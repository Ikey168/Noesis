"""Native entity scoring must consume captured fields, not asserted provenance."""

import copy
import json

import duckdb
import pytest

from src.evaluation.runtime_errors import BackendError
from src.evaluation.runtime_jobs import dispatch
from src.evaluation.runtime_outcomes import backend_outcome
from src.ingestion.revisions import DocumentRevisionStore
from src.kb.entity_history import EntityHistoryStore
from src.kb.optional_runtime import OptionalAnalysisStore


@pytest.fixture
def state():
    conn = duckdb.connect()
    revisions = DocumentRevisionStore(conn)
    refs, records = [], []
    for key, name in [("a", "Müller GmbH"), ("b", "Müller")]:
        row = revisions.observe(
            {
                "document_id": key,
                "content": "Name: " + name,
                "language": "de",
                "metadata": {
                    "address": "Musterstraße 1",
                    "registry_json": json.dumps({"id": "DE123"}),
                },
            }
        )
        ref = {"document_id": key, "revision_id": row["revision_id"]}
        refs.append(ref)
        records.append(
            {
                "id": "entity:" + key,
                "type": "Organization",
                "field_sources": {
                    "name": {
                        **ref,
                        "pointer": "/content",
                        "start": 6,
                        "end": 6 + len(name),
                    },
                    "address": {**ref, "pointer": "/metadata/address"},
                    "identifiers": {
                        "registry": {
                            **ref,
                            "pointer": "/metadata/registry_json",
                            "json_pointer": "/id",
                        }
                    },
                },
            }
        )
    history = EntityHistoryStore(conn)
    for record in records:
        history.register_entity(
            "r", record["id"], principal_id="alice", scopes={"operator"}
        )
    try:
        yield (
            conn,
            revisions,
            refs,
            {"source": records[0], "candidates": records[1:], "threshold": 0.8},
        )
    finally:
        conn.close()


def native(op, payload, **kwargs):
    return backend_outcome(op, dispatch(op, payload))


def run(store, refs, payload, **kwargs):
    return store.run(
        "r",
        "bound",
        "rapidfuzz",
        payload,
        source_refs=refs,
        principal_id="alice",
        scopes={"operator"},
        **kwargs,
    )


def test_native_captured_fields_artifact_and_review_replay(state):
    pytest.importorskip("rapidfuzz")
    conn, _, refs, payload = state
    calls = []

    def executor(op, prepared, **kwargs):
        calls.append(copy.deepcopy(prepared))
        return native(op, prepared)

    store = OptionalAnalysisStore(conn, executor=executor)
    result = run(store, refs, payload)
    source = calls[0]["source"]
    assert source["name"] == "Müller GmbH" and source["identifiers"] == {
        "registry": "DE123"
    }
    assert source["revision"].startswith("entity-input:")
    assert source["provenance"]["field_sources"] == payload["source"]["field_sources"]
    assert (
        result["outcome"]["result"]["source_binding"]["contract"]
        == "noesis-entity-source-binding-v1"
    )
    artifact = store.graph.inspect(result["artifact"]["artifact_id"])
    assert len(artifact["dependencies"]) == 2
    assert (
        artifact["content"]["source_binding"]["identity_assignment"]
        == "operator-selected-review-hypothesis"
    )
    assert run(store, refs, payload)["replayed"] and len(calls) == 1
    task = store.queue_entity_candidate(
        "r", "bound", 0, principal_id="alice", scopes={"operator"}, domain="research"
    )
    assert task["status"] == "unassigned"
    assert (
        conn.execute("SELECT count(*) FROM entity_history_redirects").fetchone()[0] == 0
    )


@pytest.mark.parametrize(
    "change",
    [
        "raw_name",
        "unbound_record",
        "wrong_revision",
        "undeclared_source",
        "out_of_range",
        "boolean_offset",
        "unknown_entity",
    ],
)
def test_unbound_or_invalid_fields_fail_before_execution(state, change):
    conn, _, refs, payload = state
    name = payload["source"]["field_sources"]["name"]
    if change == "raw_name":
        payload["source"]["name"] = "Invented Corp"
    elif change == "unbound_record":
        payload["source"] = {
            "id": "entity:a",
            "name": "Invented",
            "revision": "r1",
            "type": "Organization",
        }
    elif change == "wrong_revision":
        name["revision_id"] = "invented"
    elif change == "undeclared_source":
        refs = refs[1:]
    elif change == "out_of_range":
        name["end"] = 100000
    elif change == "boolean_offset":
        name["start"] = True
    else:
        payload["source"]["id"] = "entity:unknown"
    store = OptionalAnalysisStore(
        conn, executor=lambda *a, **k: pytest.fail("unbound fields reached model")
    )
    with pytest.raises((BackendError, ValueError)):
        run(store, refs, payload)
    assert conn.execute("SELECT count(*) FROM knowledge_artifacts").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["id", "revision", "guard", "hash", "duplicate"])
def test_model_cannot_substitute_candidate_identity_or_guards(state, change):
    pytest.importorskip("rapidfuzz")
    conn, _, refs, payload = state

    def executor(op, prepared, **kwargs):
        result = native(op, prepared)
        row = result["result"]["candidates"][0]
        if change == "id":
            row["id"] = "entity:elsewhere"
        elif change == "revision":
            row["revision"] = "wrong"
        elif change == "guard":
            row["explicit_identifier_matches"] = []
        elif change == "hash":
            result["result"]["input_sha256"] = "wrong"
        else:
            result["result"]["candidates"].append(dict(row))
        return result

    with pytest.raises(BackendError, match="candidate"):
        run(OptionalAnalysisStore(conn, executor=executor), refs, payload)
    assert conn.execute("SELECT count(*) FROM knowledge_artifacts").fetchone()[0] == 0


@pytest.mark.parametrize("moment", ["inference", "review"])
def test_changed_canonical_state_cannot_publish_or_review(state, moment):
    pytest.importorskip("rapidfuzz")
    conn, _, refs, payload = state

    def executor(op, prepared, **kwargs):
        output = native(op, prepared)
        if moment == "inference":
            conn.execute(
                "UPDATE entity_history_identities SET status='superseded' WHERE entity_id='entity:b'"
            )
        return output

    store = OptionalAnalysisStore(conn, executor=executor)
    if moment == "inference":
        with pytest.raises(BackendError, match="identity changed"):
            run(store, refs, payload)
        assert (
            conn.execute("SELECT count(*) FROM knowledge_artifacts").fetchone()[0] == 0
        )
    else:
        run(store, refs, payload)
        conn.execute(
            "UPDATE entity_history_identities SET aliases_json='[\"changed\"]' WHERE entity_id='entity:b'"
        )
        with pytest.raises(BackendError, match="identity changed"):
            store.queue_entity_candidate(
                "r",
                "bound",
                0,
                principal_id="alice",
                scopes={"operator"},
                domain="research",
            )
        assert (
            conn.execute("SELECT count(*) FROM entity_identity_decisions").fetchone()[0]
            == 0
        )


def test_native_splink_uses_the_same_captured_field_binding(state):
    pytest.importorskip("splink")
    from src.evaluation.entity_backends import train_splink_policy

    conn, _, refs, payload = state
    records = [payload["source"], *payload["candidates"]]

    def training_record(key, name):
        return {
            "id": key,
            "name": name,
            "revision": "training-r1",
            "type": "Organization",
        }

    policy = train_splink_policy(
        [
            {
                "id": "yes",
                "group_id": "train-yes",
                "split": "train",
                "label_origin": "fixture",
                "match": True,
                "left": training_record("x", "Sample"),
                "right": training_record("y", "Sample"),
            },
            {
                "id": "no",
                "group_id": "train-no",
                "split": "train",
                "label_origin": "fixture",
                "match": False,
                "left": training_record("z", "Other"),
                "right": training_record("w", "Different"),
            },
        ]
    )
    store = OptionalAnalysisStore(conn, executor=native)
    result = store.run(
        "r",
        "splink",
        "splink",
        {"records": records, "policy": policy},
        source_refs=refs,
        principal_id="alice",
        scopes={"operator"},
    )
    assert len(result["outcome"]["result"]["source_binding"]["records"]) == 2
    assert result["artifact"] is not None
