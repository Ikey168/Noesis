"""Knowledge transaction safety, parity, recovery, and MCP security tests."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb.transactions import (
    COMMIT_SCOPE,
    PREVIEW_SCOPE,
    READ_SCOPE,
    ROLLBACK_SCOPE,
    KnowledgeTransactionStore,
    TransactionError,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA = REPO_ROOT / "contracts/schemas/jsonschema/noesis-knowledge-mutation-v1.json"
VALID = REPO_ROOT / "contracts/examples/knowledge-mutation/valid-assert-and-link.json"
INVALID = (
    REPO_ROOT / "contracts/examples/knowledge-mutation/invalid-missing-evidence.json"
)
ALL_SCOPES = {PREVIEW_SCOPE, COMMIT_SCOPE, ROLLBACK_SCOPE, READ_SCOPE}


def _envelope(
    *,
    batch: str = "batch:test-001",
    key: str = "test-key-001",
    object_id: str = "claim:test",
    value: str = "Initial assertion",
    namespace: str = "corpus",
) -> dict:
    return {
        "contract": "noesis-knowledge-mutation-v1",
        "batch_id": batch,
        "namespace": namespace,
        "actor": {"principal_id": "analyst", "kind": "user"},
        "reason": "Reviewed assertion",
        "provenance": {"kind": "user-assertion", "method": "human-review"},
        "evidence": [{"document_id": "doc:1"}],
        "idempotency_key": key,
        "partial_batch": "atomic",
        "mutations": [
            {
                "mutation_id": "m1",
                "type": "assert",
                "target": {"kind": "object", "id": object_id, "expected_revision": 0},
                "object_type": "claim",
                "value": {"text": value},
                "metadata": {"reviewed": True},
            }
        ],
    }


def _correction(base: dict, *, batch: str, key: str, value: str, revision: int) -> dict:
    envelope = copy.deepcopy(base)
    envelope["batch_id"] = batch
    envelope["idempotency_key"] = key
    envelope["reason"] = "Correct after new evidence"
    envelope["mutations"] = [
        {
            "mutation_id": "correct-1",
            "type": "correct",
            "target": {
                "kind": "object",
                "id": base["mutations"][0]["target"]["id"],
                "expected_revision": revision,
            },
            "value": {"text": value},
        }
    ]
    return envelope


def _commit(store: KnowledgeTransactionStore, envelope: dict, scopes=ALL_SCOPES):
    principal = envelope["actor"]["principal_id"]
    preview = store.preview(envelope, principal_id=principal, scopes=scopes)
    assert preview["valid"] is True
    return store.commit(
        envelope,
        preview["approval_hash"],
        principal_id=principal,
        scopes=scopes,
    )


def test_schema_and_representative_fixtures():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = Draft7Validator(schema)
    assert not list(validator.iter_errors(json.loads(VALID.read_text())))
    assert list(validator.iter_errors(json.loads(INVALID.read_text())))


@pytest.mark.parametrize("namespace", ["corpus", "research_kg"])
def test_preview_is_deterministic_and_side_effect_free(namespace):
    conn = duckdb.connect(":memory:")
    if namespace != "corpus":
        conn.execute("CREATE TABLE provisioned_kgs(name TEXT, status TEXT)")
        conn.execute("INSERT INTO provisioned_kgs VALUES (?, 'deployed')", [namespace])
    store = KnowledgeTransactionStore(conn)
    before = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "knowledge_objects",
            "knowledge_relations",
            "knowledge_transaction_batches",
            "knowledge_transaction_audit",
            "knowledge_derivation_invalidations",
        )
    }
    scopes = {PREVIEW_SCOPE}
    if namespace != "corpus":
        scopes.add(f"knowledge:namespace:{namespace}:read")
    envelope = _envelope(namespace=namespace)
    first = store.preview(envelope, principal_id="analyst", scopes=scopes)
    second = store.preview(envelope, principal_id="analyst", scopes=scopes)
    after = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    }
    assert first == second
    assert first["valid"] is True
    assert before == after
    assert store.watermark(namespace) == 0


def test_atomic_commit_updates_graph_provenance_indexes_watermark_and_audit():
    conn = duckdb.connect(":memory:")
    store = KnowledgeTransactionStore(conn, clock=lambda: 1234)
    envelope = json.loads(VALID.read_text())
    result = _commit(store, envelope)

    assert result["watermark"] == 1
    assert len(result["affected"]) == 3
    assert conn.execute("SELECT COUNT(*) FROM knowledge_objects").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM knowledge_relations").fetchone()[0] == 1
    origins = conn.execute(
        "SELECT DISTINCT provenance_kind FROM knowledge_objects"
    ).fetchall()
    assert origins == [("user-assertion",)]
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM knowledge_derivation_invalidations"
        ).fetchone()[0]
        == 6
    )
    audit = store.audit(principal_id="auditor", scopes={READ_SCOPE})
    assert audit["events"][0]["approved_preview_hash"] == result["approval_hash"]
    assert audit["events"][0]["affected"] == result["affected"]


def test_idempotent_retry_returns_original_result_and_key_reuse_is_typed():
    conn = duckdb.connect(":memory:")
    store = KnowledgeTransactionStore(conn, clock=lambda: 100)
    envelope = _envelope()
    committed = _commit(store, envelope)
    replay = store.commit(
        envelope,
        committed["approval_hash"],
        principal_id="analyst",
        scopes=ALL_SCOPES,
    )
    assert replay["idempotent_replay"] is True
    assert (
        conn.execute("SELECT COUNT(*) FROM knowledge_transaction_audit").fetchone()[0]
        == 1
    )

    changed = copy.deepcopy(envelope)
    changed["batch_id"] = "batch:test-002"
    changed["mutations"][0]["value"]["text"] = "Different content"
    with pytest.raises(TransactionError, match="different content") as caught:
        store.commit(
            changed,
            "unused",
            principal_id="analyst",
            scopes=ALL_SCOPES,
        )
    assert caught.value.code == "idempotency_key_reused"


def test_expected_revision_and_stale_approval_prevent_partial_state():
    conn = duckdb.connect(":memory:")
    store = KnowledgeTransactionStore(conn)
    base = _envelope()
    _commit(store, base)

    stale = _correction(
        base, batch="batch:stale-001", key="stale-key-001", value="stale", revision=0
    )
    preview = store.preview(stale, principal_id="analyst", scopes={PREVIEW_SCOPE})
    assert preview["valid"] is False
    assert preview["conflicts"][0]["code"] == "revision_conflict"
    with pytest.raises(TransactionError) as conflict:
        store.commit(
            stale,
            preview["approval_hash"],
            principal_id="analyst",
            scopes={COMMIT_SCOPE},
        )
    assert conflict.value.code == "conflict"
    assert conn.execute("SELECT COUNT(*) FROM knowledge_objects").fetchone()[0] == 1

    current = _correction(
        base,
        batch="batch:current-001",
        key="current-key-001",
        value="current",
        revision=1,
    )
    approved = store.preview(current, principal_id="analyst", scopes={PREVIEW_SCOPE})
    current["mutations"][0]["value"]["text"] = "changed after approval"
    with pytest.raises(TransactionError) as changed:
        store.commit(
            current,
            approved["approval_hash"],
            principal_id="analyst",
            scopes={COMMIT_SCOPE},
        )
    assert changed.value.code == "stale_approval"


def test_atomic_batch_and_crash_recovery_roll_back_all_writes():
    conn = duckdb.connect(":memory:")
    envelope = json.loads(VALID.read_text())
    preview_store = KnowledgeTransactionStore(conn)
    preview = preview_store.preview(
        envelope, principal_id="analyst@example", scopes={PREVIEW_SCOPE}
    )

    def fail_after_first(index, _change):
        if index == 1:
            raise RuntimeError("simulated crash")

    crashing = KnowledgeTransactionStore(conn, failure_hook=fail_after_first)
    with pytest.raises(RuntimeError, match="simulated crash"):
        crashing.commit(
            envelope,
            preview["approval_hash"],
            principal_id="analyst@example",
            scopes={COMMIT_SCOPE},
        )
    for table in (
        "knowledge_objects",
        "knowledge_relations",
        "knowledge_transaction_batches",
        "knowledge_transaction_audit",
        "knowledge_derivation_invalidations",
        "knowledge_consolidation_watermarks",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_concurrent_corrections_have_one_winner(tmp_path):
    db_path = tmp_path / "transactions.duckdb"
    seed_conn = duckdb.connect(str(db_path))
    seed = KnowledgeTransactionStore(seed_conn)
    base = _envelope()
    _commit(seed, base)
    seed_conn.close()

    first = _correction(
        base, batch="batch:concurrent-a", key="concurrent-key-a", value="A", revision=1
    )
    second = _correction(
        base, batch="batch:concurrent-b", key="concurrent-key-b", value="B", revision=1
    )
    conn_a, conn_b = duckdb.connect(str(db_path)), duckdb.connect(str(db_path))
    store_a, store_b = (
        KnowledgeTransactionStore(conn_a),
        KnowledgeTransactionStore(conn_b),
    )
    preview_a = store_a.preview(first, principal_id="analyst", scopes={PREVIEW_SCOPE})
    preview_b = store_b.preview(second, principal_id="analyst", scopes={PREVIEW_SCOPE})

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = {
            pool.submit(
                store_a.commit,
                first,
                preview_a["approval_hash"],
                principal_id="analyst",
                scopes={COMMIT_SCOPE},
            ),
            pool.submit(
                store_b.commit,
                second,
                preview_b["approval_hash"],
                principal_id="analyst",
                scopes={COMMIT_SCOPE},
            ),
        }
        values = []
        for future in outcomes:
            try:
                values.append(future.result()["ok"])
            except TransactionError as exc:
                values.append(exc.code)
    assert values.count(True) == 1
    assert (
        len([value for value in values if value in {"stale_approval", "conflict"}]) == 1
    )
    check = duckdb.connect(str(db_path))
    assert (
        check.execute(
            "SELECT revision FROM knowledge_objects WHERE object_id='claim:test'"
        ).fetchone()[0]
        == 2
    )
    check.close()
    conn_a.close()
    conn_b.close()


def test_compensating_rollback_is_audited_and_never_erases_history():
    conn = duckdb.connect(":memory:")
    ticks = iter((100, 200))
    store = KnowledgeTransactionStore(conn, clock=lambda: next(ticks))
    envelope = _envelope()
    committed = _commit(store, envelope)
    rolled = store.rollback(
        envelope["batch_id"],
        "Reviewer withdrew approval",
        principal_id="supervisor",
        scopes={ROLLBACK_SCOPE},
    )
    assert rolled["watermark"] == 2
    row = conn.execute(
        "SELECT revision, retracted FROM knowledge_objects WHERE object_id='claim:test'"
    ).fetchone()
    assert row == (2, True)
    events = store.audit(principal_id="auditor", scopes={READ_SCOPE})["events"]
    assert [event["action"] for event in events] == ["commit", "rollback"]
    assert events[0]["request"] == envelope
    rollback_replay = store.rollback(
        envelope["batch_id"],
        "Reviewer withdrew approval",
        principal_id="supervisor",
        scopes={ROLLBACK_SCOPE},
    )
    assert rollback_replay["action"] == "rollback"
    assert rollback_replay["idempotent_replay"] is True
    commit_replay = store.commit(
        envelope,
        committed["approval_hash"],
        principal_id="analyst",
        scopes={COMMIT_SCOPE},
    )
    assert commit_replay["committed_at_ms"] == committed["committed_at_ms"]
    assert "action" not in commit_replay
    assert commit_replay["idempotent_replay"] is True
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM knowledge_derivation_invalidations"
        ).fetchone()[0]
        == 4
    )


def test_rollback_refuses_to_overwrite_a_later_revision():
    conn = duckdb.connect(":memory:")
    store = KnowledgeTransactionStore(conn)
    base = _envelope()
    _commit(store, base)
    later = _correction(
        base, batch="batch:later-001", key="later-key-001", value="later", revision=1
    )
    _commit(store, later)
    with pytest.raises(TransactionError) as caught:
        store.rollback(
            base["batch_id"], "too late", principal_id="admin", scopes={ROLLBACK_SCOPE}
        )
    assert caught.value.code == "rollback_conflict"
    assert (
        conn.execute("SELECT COUNT(*) FROM knowledge_transaction_audit").fetchone()[0]
        == 2
    )


@pytest.mark.parametrize("namespace", ["corpus", "research_kg"])
def test_preview_and_commit_have_corpus_namespace_backing_parity(namespace):
    conn = duckdb.connect(":memory:")
    if namespace != "corpus":
        conn.execute("CREATE TABLE provisioned_kgs(name TEXT, status TEXT)")
        conn.execute("INSERT INTO provisioned_kgs VALUES (?, 'deployed')", [namespace])
    store = KnowledgeTransactionStore(conn)
    envelope = _envelope(namespace=namespace)
    scopes = set(ALL_SCOPES)
    if namespace != "corpus":
        scopes |= {
            f"knowledge:namespace:{namespace}:read",
            f"knowledge:namespace:{namespace}:write",
        }
    preview = store.preview(envelope, principal_id="analyst", scopes=scopes)
    result = store.commit(
        envelope,
        preview["approval_hash"],
        principal_id="analyst",
        scopes=scopes,
    )
    assert result["namespace"] == namespace
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM knowledge_objects WHERE namespace=?", [namespace]
        ).fetchone()[0]
        == 1
    )


def test_preview_enforces_provisioned_namespace_ontology_without_writes():
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE provisioned_kgs(name TEXT, status TEXT, ontology TEXT)")
    conn.execute(
        "INSERT INTO provisioned_kgs VALUES (?, 'deployed', ?)",
        [
            "research_kg",
            json.dumps(
                {
                    "object_types": ["claim", "document"],
                    "relation_types": ["SUPPORTS"],
                }
            ),
        ],
    )
    store = KnowledgeTransactionStore(conn)
    envelope = _envelope(namespace="research_kg")
    envelope["mutations"][0]["object_type"] = "unregistered-type"
    scopes = {PREVIEW_SCOPE, "knowledge:namespace:research_kg:read"}

    preview = store.preview(envelope, principal_id="analyst", scopes=scopes)

    assert preview["valid"] is False
    assert preview["conflicts"][0] == {
        "mutation_id": "m1",
        "kind": "object",
        "id": "claim:test",
        "code": "ontology_violation",
        "field": "object_type",
        "value": "unregistered-type",
        "allowed": ["claim", "document"],
    }
    assert conn.execute("SELECT COUNT(*) FROM knowledge_objects").fetchone()[0] == 0


def test_permissions_actor_and_provenance_are_enforced_separately():
    conn = duckdb.connect(":memory:")
    store = KnowledgeTransactionStore(conn)
    envelope = _envelope()
    with pytest.raises(TransactionError) as unauthorized:
        store.preview(envelope, principal_id="analyst", scopes=set())
    assert unauthorized.value.code == "unauthorized"
    with pytest.raises(TransactionError) as actor:
        store.preview(envelope, principal_id="another", scopes={PREVIEW_SCOPE})
    assert actor.value.code == "actor_mismatch"

    source = copy.deepcopy(envelope)
    source["provenance"]["kind"] = "source-derived"
    with pytest.raises(TransactionError) as provenance:
        store.preview(source, principal_id="analyst", scopes={PREVIEW_SCOPE})
    assert provenance.value.code == "invalid_provenance"

    preview = store.preview(envelope, principal_id="analyst", scopes={PREVIEW_SCOPE})
    with pytest.raises(TransactionError) as no_commit:
        store.commit(
            envelope,
            preview["approval_hash"],
            principal_id="analyst",
            scopes={PREVIEW_SCOPE},
        )
    assert no_commit.value.code == "unauthorized"
    with pytest.raises(TransactionError):
        store.audit(principal_id="analyst", scopes={COMMIT_SCOPE})


def test_mcp_uses_operator_identity_and_never_accepts_caller_supplied_scopes(
    tmp_path, monkeypatch
):
    path = tmp_path / "mcp-transactions.duckdb"
    conn = duckdb.connect(str(path))
    KnowledgeTransactionStore(conn)
    conn.close()
    monkeypatch.setenv("NOESIS_DB_PATH", str(path))
    monkeypatch.setenv("NOESIS_MCP_PRINCIPAL", "analyst")
    monkeypatch.setenv(
        "NOESIS_MCP_SCOPES", f"{PREVIEW_SCOPE},{COMMIT_SCOPE},{READ_SCOPE}"
    )

    server_path = REPO_ROOT / "tools/transactions_mcp/server.py"
    spec = importlib.util.spec_from_file_location("transaction_mcp_test", server_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    tools = asyncio.run(module.mcp.get_tools())
    envelope = _envelope()
    preview = tools["preview_mutation_batch"].fn(envelope)
    committed = tools["commit_mutation_batch"].fn(envelope, preview["approval_hash"])
    assert committed["ok"] is True
    denied = tools["rollback_mutation_batch"].fn(envelope["batch_id"], "test")
    assert denied["error"]["code"] == "unauthorized"
    replay = tools["replay_mutation_audit"].fn()
    assert [event["action"] for event in replay["events"]] == ["commit"]
