"""Regression scenarios from issues #1413, #1417, #1428 and #1434."""

import json

import duckdb
import pytest

from src.kb.knowledge_retention import EXECUTE_SCOPE, KnowledgeRetentionError, KnowledgeRetentionStore
from src.kb.subscriptions import SubscriptionError, SubscriptionStore
from src.kb.workflows import STAGE_ORDER, WorkflowStore, reference_manifest


@pytest.mark.parametrize("boundary", ["_commit_watermark", "_publish_subscription_watermark"])
def test_resume_repairs_publication_without_repeating_index(monkeypatch, boundary):
    conn = duckdb.connect(":memory:")
    store = WorkflowStore(conn)
    calls = []

    def handler(context, state):
        calls.append(context.stage)
        if context.stage == "query":
            assert context.watermark == 1
        return {**state, context.stage: True}

    original = getattr(store, boundary)
    failed = False

    def interrupt(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("publication interrupted")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, boundary, interrupt)
    manifest = reference_manifest("recovery")
    handlers = dict.fromkeys(STAGE_ORDER, handler)
    with pytest.raises(RuntimeError, match="interrupted"):
        store.execute(manifest, handlers, {}, run_key="one")
    result = store.execute(manifest, handlers, {}, run_key="one")
    assert result["status"] == "completed"
    assert calls.count("index") == 1
    assert conn.execute("SELECT count(*) FROM knowledge_workflow_watermarks").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM knowledge_subscription_watermarks").fetchone()[0] == 1
    replay = store.execute(manifest, handlers, {}, run_key="one")
    assert replay["watermark"] == result["watermark"]
    assert calls.count("query") == 1
    conn.close()


@pytest.mark.parametrize("overflow", ["records", "tombstones"])
def test_checkpoint_overflow_is_bounded_and_never_commits(overflow):
    conn = duckdb.connect(":memory:")
    store = KnowledgeRetentionStore(conn)
    consumed = []

    def records():
        for i in range(100):
            consumed.append(i)
            yield {"id": i} if overflow == "records" else str(i)

    args = {"records": [], "tombstones": []}
    args[overflow] = records()
    with pytest.raises(KnowledgeRetentionError) as caught:
        store.checkpoint("test", 1, 100, **args, schema_version="1", limit=2,
                         principal_id="p", scopes={EXECUTE_SCOPE})
    assert caught.value.code == "checkpoint_too_large"
    assert consumed == [0, 1, 2]
    assert conn.execute("SELECT count(*) FROM retention_checkpoints").fetchone()[0] == 0
    result = store.checkpoint("test", 1, 2, [{"id": 0}, {"id": 1}], schema_version="1",
                              limit=2, principal_id="p", scopes={EXECUTE_SCOPE})
    assert result["record_count"] == 2 and result["status"] == "complete"
    conn.close()


@pytest.mark.parametrize("legacy_hash", [False, True])
def test_subscription_replay_checks_coverage_including_legacy_snapshots(legacy_hash):
    conn = duckdb.connect(":memory:")
    store = SubscriptionStore(conn)
    scopes = {"knowledge:subscriptions:read", "knowledge:subscriptions:write", "namespace:test:read"}
    subscription = store.create(
        {"namespace": "test", "domain": "scientific", "query": {"operation": "search", "text": "test"},
         "filters": {}, "cadence": {"trigger": "watermark"}, "delivery": {"kind": "poll"}},
        "one", principal_id="p", scopes=scopes,
    )
    store.commit_watermark("test", 1)
    result = {"items": [{"id": "a"}], "coverage": {"complete": True}}
    store.evaluate(subscription["subscription_id"], 1, result, principal_id="p", scopes=scopes)
    if legacy_hash:
        from src.kb.subscriptions import _digest
        conn.execute("UPDATE knowledge_subscription_snapshots SET result_hash=?", [_digest({"a": {"id": "a"}})])
    assert store.evaluate(subscription["subscription_id"], 1, result, principal_id="p", scopes=scopes)["status"] == "replayed"
    with pytest.raises(SubscriptionError, match="coverage"):
        store.evaluate(subscription["subscription_id"], 1, {**result, "coverage": {"complete": False}}, principal_id="p", scopes=scopes)
    assert json.loads(conn.execute("SELECT coverage_json FROM knowledge_subscription_snapshots").fetchone()[0])["complete"]
    conn.close()
