"""Revision rules, record-time reads, namespace isolation and lookups for the ownership store (#1851)."""

import pytest

from src.kb.ownership_records import record
from src.kb.ownership_store import OwnershipError, OwnershipStore, canonical_entity_id, record_id
from tests.unit.ownership import harness

SOURCE = {"provider": "companies-house", "provider_record_id": "09990009:profile"}


def entity(name, status="active"):
    return record("legal_entity", "companies-house:gb-coh:09990009", SOURCE, name=name, jurisdiction="GB",
                  identifiers=[{"scheme": "gb-coh", "value": "09990009"}], status=status)


def test_revisions_are_append_only_and_replays_are_idempotent():
    env = harness.Env()
    store = OwnershipStore(env.conn, now=env.now)
    first = store.put(harness.NS, [entity("Revision Test Ltd")], run_id="r1", principal_id="p", scopes=harness.SCOPES)
    assert first == {"inserted": 1, "revised": 0, "unchanged": 0}
    assert store.put(harness.NS, [entity("Revision Test Ltd")], run_id="r2", principal_id="p", scopes=harness.SCOPES)[
        "unchanged"] == 1
    cutoff = env.now()
    assert store.put(harness.NS, [entity("Revision Test Ltd", "dissolved")], run_id="r3", principal_id="p",
                     scopes=harness.SCOPES)["revised"] == 1
    rid = record_id(harness.NS, "companies-house:gb-coh:09990009")
    history = store.history(harness.NS, rid, principal_id="p", scopes=harness.SCOPES)
    assert [h["revision"] for h in history] == [1, 2] and [h["run_id"] for h in history] == ["r1", "r3"]
    assert store.get(harness.NS, rid, principal_id="p", scopes=harness.SCOPES)["record"]["status"] == "dissolved"
    assert store.get(harness.NS, rid, principal_id="p", scopes=harness.SCOPES, revision=1)["record"]["status"] == "active"
    earlier = store.records(harness.NS, principal_id="p", scopes=harness.SCOPES, known_at_ms=cutoff)
    assert earlier[0]["record"]["status"] == "active"  # as known at a record time
    pinned = store.records(harness.NS, principal_id="p", scopes=harness.SCOPES, pins={rid: 1})
    assert pinned[0]["revision"] == 1
    assert history[0]["record"]["canonical_entity_id"] == canonical_entity_id("companies-house:gb-coh:09990009")


def test_namespaces_isolate_records_and_scopes_are_enforced():
    env = harness.Env().ready()
    store = OwnershipStore(env.conn)
    other = {"knowledge:ownership:read", "namespace:other:read"}
    assert store.records("other", principal_id="p", scopes=other) == []
    with pytest.raises(OwnershipError) as caught:
        store.records(harness.NS, principal_id="p", scopes=other)
    assert caught.value.code == "unauthorized"
    with pytest.raises(OwnershipError):
        store.put(harness.NS, [entity("x")], run_id="r", principal_id="p", scopes=harness.READ_ONLY)
    with pytest.raises(OwnershipError):
        store.get("other", record_id(harness.NS, harness.UK_KEYS["gleif"]), principal_id="p", scopes=other)


def test_lookup_by_identifier_and_name_never_picks():
    env = harness.Env().ready()
    store = OwnershipStore(env.conn)
    by_lei = store.lookup(harness.NS, "lei", harness.UK, principal_id="p", scopes=harness.SCOPES)
    assert by_lei["status"] == "found"
    assert {m["record"]["record_key"] for m in by_lei["matches"]} == {harness.UK_KEYS["gleif"], harness.UK_KEYS["bods"]}
    by_number = store.lookup(harness.NS, "company_number", "09990002", principal_id="p", scopes=harness.SCOPES)
    assert {m["record"]["source"]["provider"] for m in by_number["matches"]} == {"gleif", "companies-house", "open-ownership"}
    assert store.lookup(harness.NS, "cik", "9999101", principal_id="p", scopes=harness.SCOPES)["status"] == "found"
    by_name = store.lookup(harness.NS, "name", "Exampla Holdings|GB", principal_id="p", scopes=harness.SCOPES)
    assert by_name["status"] == "candidates" and len(by_name["matches"]) == 4 and "never" in by_name["note"]
    assert store.lookup(harness.NS, "lei", "529900NOTHERE0000000", principal_id="p", scopes=harness.SCOPES)["status"] == "not_found"
