"""Subscriptions to registration, routing and certificate changes (II10, #2797 under #2743). Offline only."""

from __future__ import annotations

import pytest

from src.ingestion import internet_infrastructure_sources as ii
from src.kb.internet_infrastructure_monitoring import InfrastructureMonitor
from src.kb.internet_infrastructure_records import (
    InfrastructureRecordError,
    forbidden_paths,
)
from tests.unit import internet_infrastructure_harness as h


@pytest.fixture()
def conn():
    value = h.connection()
    h.load_all(value)
    yield value
    value.close()


def _monitor(conn, now=h.SECOND_RETRIEVAL):
    return InfrastructureMonitor(conn, now=lambda: now)


def _refresh(conn, name, revision=None, at=h.SECOND_RETRIEVAL, transport=None):
    monitor = _monitor(conn, at)
    return monitor.refresh(h.NS, h.source(name), principal_id="svc", scopes=h.SCOPES,
                           transport=transport or ii.fixture_transport(h.pages(name, revision)),
                           secret=ii.FIXTURE_SECRET, retrieved_at_ms=at, sleep=lambda _s: None,
                           now_ms=lambda: at, bootstrap_cache={})


def _kinds(result):
    return sorted({n["kind"] for n in result["notifications"]})


def test_new_changed_removed_and_unchanged_cases(conn):
    monitor = _monitor(conn, h.FIRST_RETRIEVAL)
    asn = monitor.create(h.NS, "asn", target={"resource": "AS64500"}, principal_id="alice", scopes=h.SCOPES)
    ct = monitor.create(h.NS, "ct", target={"provider": "ct-log-list"}, principal_id="alice", scopes=h.SCOPES)
    domain = monitor.create(h.NS, "domain", target={"resource": {"domain": "example.org"}, "provider": "crtsh"},
                            principal_id="alice", scopes=h.SCOPES)
    first = monitor.run(asn["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    # New: the first acquisition is reported as new records (registration, self-declaration); routing baseline only.
    assert _kinds(first) == ["peeringdb_update", "registration_change"]
    assert _kinds(monitor.run(domain["subscription_id"], principal_id="alice", scopes=h.SCOPES)) == ["new_certificate"]
    monitor.run(ct["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    # Unchanged: re-acquiring the same answers adds nothing and notifies nothing.
    receipt = _refresh(conn, "peeringdb", at=h.FIRST_RETRIEVAL + 60_000)
    assert receipt["status"] == "complete" and receipt["unchanged_units"] == 1
    later = _monitor(conn)
    assert later.run(asn["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    # Changed and removed.
    for name in ("peeringdb", "rdap", "ripestat", "ct"):
        assert _refresh(conn, name, revision=name)["status"] == "complete"
    changed = later.run(asn["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert _kinds(changed) == ["peeringdb_update", "registration_change", "removed_by_source",
                               "routing_observation_changed"]
    for notice in changed["notifications"]:
        assert notice["citation"]["record_id"] == notice["record_id"] and notice["citation"]["as_of"]
        assert notice["what_changed"]
    removed = [n for n in changed["notifications"] if n["kind"] == "removed_by_source"]
    assert {n["record"]["object_kind"] for n in removed} == {"org", "netixlan", "fac"}
    net = next(n for n in changed["notifications"] if n["kind"] == "peeringdb_update")
    assert net["previous_record_id"] and net["what_changed"]["changes"]["fields"]["policy_general"]["after"] == \
        "Selective"
    routing = [n for n in changed["notifications"] if n["kind"] == "routing_observation_changed"]
    assert {n["what_changed"]["data_call"] for n in routing} == {"announced-prefixes", "routing-status"}
    assert forbidden_paths(changed) == [] and "verdict" in changed["note"]
    ct_changes = later.run(ct["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert _kinds(ct_changes) == ["ct_log_state_change"] and len(ct_changes["notifications"]) == 1
    # Replays emit nothing.
    assert later.run(asn["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []


def test_a_failed_refresh_never_produces_a_removal_and_waits_after_a_rate_limit(conn):
    monitor = _monitor(conn, h.FIRST_RETRIEVAL)
    asn = monitor.create(h.NS, "asn", target={"resource": "AS64500"}, principal_id="alice", scopes=h.SCOPES)
    monitor.run(asn["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    before = conn.execute("SELECT count(*) FROM ii_revisions").fetchone()[0]

    def rate_limited(**_kwargs):
        return {"status": 429, "headers": {"Retry-After": "120"}, "content": b""}

    receipt = _refresh(conn, "peeringdb", transport=rate_limited)
    assert receipt["status"] == "stopped" and receipt["stopped"]["code"] == "rate_limited"
    assert conn.execute("SELECT count(*) FROM ii_revisions").fetchone()[0] == before
    waited = _refresh(conn, "peeringdb", revision="peeringdb")
    assert waited["status"] == "rate_limited_wait" and waited["units"] == []

    def broken(**_kwargs):
        return {"status": 503, "headers": {}, "content": b""}

    failed = _refresh(conn, "rdap", transport=broken)
    assert failed["status"] == "stopped" and "nothing is marked removed" in failed["note"]
    assert conn.execute("SELECT count(*) FROM ii_revisions WHERE state='removed_by_source'").fetchone()[0] == 0
    assert _monitor(conn).run(asn["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []


@pytest.mark.parametrize(("target", "code"), [
    ({"resource": "192.0.2.1"}, "ip_lookup_refused"),
    ({"resource": "hostmaster@example.org"}, "person_identifier_refused"),
    ({"resource": "*.example.org"}, "wildcard_refused"),
    ({"resource": "AS64501"}, "undeclared_resource"),
    ({"provider": "caida"}, "invalid_watch"),
    ({}, "invalid_watch"),
])
def test_only_declared_resources_and_providers_can_be_watched(conn, target, code):
    with pytest.raises(InfrastructureRecordError) as caught:
        _monitor(conn).create(h.NS, "bad", target=target, principal_id="alice", scopes=h.SCOPES)
    assert caught.value.code == code
