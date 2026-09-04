from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb.subscriptions import SubscriptionError, SubscriptionStore

ROOT = Path(__file__).resolve().parents[3]
WRITE = {"knowledge:subscriptions:read", "knowledge:subscriptions:write", "knowledge:subscriptions:deliver", "namespace:research:read"}


@pytest.fixture()
def store(tmp_path: Path):
    conn=duckdb.connect(str(tmp_path/"subscriptions.duckdb")); value=SubscriptionStore(conn)
    yield value
    conn.close()


def definition(**overrides):
    value={"namespace":"research","domain":"scientific","query":{"operation":"search","text":"replication"},"filters":{"language":"en"},"cadence":{"trigger":"watermark"},"delivery":{"kind":"poll"}}
    value.update(overrides); return value


def create(store, key="create-1", **overrides): return store.create(definition(**overrides),key,principal_id="alice",scopes=WRITE)


def test_schema_fixture_and_migration(store: SubscriptionStore) -> None:
    schema=json.loads((ROOT/"contracts/schemas/jsonschema/noesis-knowledge-subscription-v1.json").read_text()); fixture=json.loads((ROOT/"contracts/examples/knowledge-subscriptions/research-subscription.json").read_text())
    Draft7Validator.check_schema(schema); assert not list(Draft7Validator(schema).iter_errors(fixture))
    assert store.conn.execute("SELECT version FROM noesis_schema_migrations WHERE component='knowledge-subscriptions'").fetchone()==(1,)


def test_create_is_idempotent_versioned_and_owner_isolated(store: SubscriptionStore) -> None:
    made=create(store); assert create(store)==made
    with pytest.raises(SubscriptionError,match="reused"): store.create(definition(filters={}),"create-1",principal_id="alice",scopes=WRITE)
    assert store.list(principal_id="alice",scopes=WRITE)==[made]
    with pytest.raises(SubscriptionError,match="does not exist"): store.inspect(made["subscription_id"],principal_id="bob",scopes=WRITE)
    updated=store.update(made["subscription_id"],{"filters":{"language":"de"}},principal_id="alice",scopes=WRITE)
    assert updated["version"]==2 and updated["filters"]=={"language":"de"}
    assert store.set_status(made["subscription_id"],"paused",principal_id="alice",scopes=WRITE)["status"]=="paused"
    assert store.set_status(made["subscription_id"],"active",principal_id="alice",scopes=WRITE)["status"]=="active"
    assert store.delete(made["subscription_id"],principal_id="alice",scopes=WRITE)["status"]=="deleted"


def test_determinism_namespace_and_quota_guards(store: SubscriptionStore) -> None:
    for query in ({"operation":"random"},{"operation":"search","now":True}):
        with pytest.raises(SubscriptionError,match="deterministic"): store.create(definition(query=query),str(query),principal_id="alice",scopes=WRITE)
    with pytest.raises(SubscriptionError,match="namespace"): store.create(definition(),"no-scope",principal_id="alice",scopes={"knowledge:subscriptions:write"})
    store.max_active_per_principal=1; create(store)
    with pytest.raises(SubscriptionError,match="quota"): store.create(definition(filters={"other":1}),"second",principal_id="alice",scopes=WRITE)


def test_committed_watermark_diff_event_taxonomy_replay_and_cursor(store: SubscriptionStore) -> None:
    made=create(store); sid=made["subscription_id"]
    with pytest.raises(SubscriptionError,match="committed"): store.evaluate(sid,1,{"items":[]},principal_id="alice",scopes=WRITE)
    store.commit_watermark("research",1,detail={"batch":"one"},committed_at_ms=10)
    first=store.evaluate(sid,1,{"items":[{"id":"a","value":1},{"id":"b","value":2}],"coverage":{"complete":True}},principal_id="alice",scopes=WRITE,observed_at_ms=20)
    assert first["events"]==2
    assert store.evaluate(sid,1,{"items":[{"id":"a","value":1},{"id":"b","value":2}],"coverage":{"complete":True}},principal_id="alice",scopes=WRITE)["status"]=="replayed"
    store.commit_watermark("research",2,kind="ingestion")
    second=store.evaluate(sid,2,{"items":[{"id":"a","value":3,"corrected":True},{"id":"c","value":4}],"coverage":{"complete":False}},principal_id="alice",scopes=WRITE)
    assert second["events"]==4
    page=store.poll(sid,principal_id="alice",scopes=WRITE,limit=2); assert len(page["events"])==2 and page["has_more"]
    rest=store.poll(sid,principal_id="alice",scopes=WRITE,cursor=page["cursor"],limit=20)
    assert {event["event_type"] for event in rest["events"]}>={"removed","corrected","coverage-degraded"}
    other=store.create(definition(filters={"language":"de"}),"other",principal_id="alice",scopes=WRITE)
    with pytest.raises(SubscriptionError,match="another"): store.poll(other["subscription_id"],principal_id="alice",scopes=WRITE,cursor=page["cursor"])
    assert store.evaluate(sid,1,{"items":[]},principal_id="alice",scopes=WRITE)["status"]=="ignored"


def test_delivery_hooks_expiration_transfer_and_late_runs(store: SubscriptionStore) -> None:
    made=create(store,delivery={"kind":"queue","destination_ref":"configured:research-events"}); sid=made["subscription_id"]
    store.commit_watermark("research",3); store.evaluate(sid,3,{"items":[{"id":"x"}]},principal_id="alice",scopes=WRITE)
    delivery=store.pending_deliveries(principal_id="alice",scopes=WRITE)[0]
    assert delivery["delivery_kind"]=="queue" and "configured:research-events"==delivery["destination_ref"]
    with pytest.raises(SubscriptionError,match="operator"): store.transfer(sid,"bob",principal_id="alice",scopes=WRITE)
    transferred=store.transfer(sid,"bob",principal_id="alice",scopes=WRITE|{"operator"}); assert transferred["owner_principal"]=="bob"


def test_rate_limit_and_snapshot_conflict(store: SubscriptionStore) -> None:
    store.rate_limit=1; create(store)
    with pytest.raises(SubscriptionError,match="rate"): store.list(principal_id="alice",scopes=WRITE)
    store.rate_limit=120; made=store.list(principal_id="alice",scopes=WRITE)[0]; store.commit_watermark("research",1)
    store.evaluate(made["subscription_id"],1,{"items":[{"id":"a"}]},principal_id="alice",scopes=WRITE)
    with pytest.raises(SubscriptionError,match="different"): store.evaluate(made["subscription_id"],1,{"items":[{"id":"b"}]},principal_id="alice",scopes=WRITE)
