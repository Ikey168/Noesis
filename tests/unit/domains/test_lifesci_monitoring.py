"""Life-science monitors through subscriptions: new, revised, obsoleted and unchanged cases (#2652, LS11 #2706)."""

from __future__ import annotations

import pytest

from src.ingestion.lifesci_sources import fixture_transport
from src.kb.lifesci_monitoring import LifeSciMonitor
from src.kb.lifesci_records import LifeSciError
from tests.unit import lifesci_harness as h


@pytest.fixture()
def env():
    conn = h.connection()
    h.load_all(conn)
    clock = {"now": h.FIRST + 5}
    monitor = LifeSciMonitor(conn, now=lambda: clock["now"])
    subs = {kind: monitor.create(h.NS, kind, watch={kind: value}, principal_id="alice",
                                 scopes=h.SCOPES)["subscription_id"]
            for kind, value in {"accession": "X9EXA2", "target": "X9EXA1", "taxon": "99000001"}.items()}
    return conn, monitor, subs, clock


def run(monitor, subscription_id):
    return monitor.run(subscription_id, principal_id="alice", scopes=h.SCOPES)


def test_new_records_notify_once_and_unchanged_reruns_emit_nothing(env):
    _, monitor, subs, _ = env
    first = run(monitor, subs["target"])
    assert sorted((n["kind"], n["record"]["native_id"]) for n in first["notifications"]) == [
        ("new_activity", "990000301"), ("new_activity", "990000302"), ("new_activity", "990000303"),
        ("new_entry", "CHEMBL9900001")]
    assert all(n["source_revision"]["revision_id"] == n["revision_id"] for n in first["notifications"])
    taxon = run(monitor, subs["taxon"])
    assert ("new_entry", "99000001") in {(n["kind"], n["record"]["native_id"]) for n in taxon["notifications"]}
    assert run(monitor, subs["target"])["notifications"] == []
    assert run(monitor, subs["taxon"])["notifications"] == []


def test_revised_obsoleted_and_new_activity_cite_both_revisions_and_state_what_changed(env):
    conn, monitor, subs, clock = env
    for sub in subs.values():
        run(monitor, sub)
    for provider in h.REVISIONS:
        h.apply(conn, provider, revision=True, at_ms=h.SECOND)
    clock["now"] = h.SECOND + 5
    merged = run(monitor, subs["accession"])["notifications"]
    assert [(n["kind"], n["successors"]) for n in merged] == [("entry_obsoleted", ["X9EXA1"])]
    assert merged[0]["previous_revision_id"] and merged[0]["changes"]["status"] == {"before": "active",
                                                                                   "after": "merged"}
    target = {n["record"]["native_id"]: n for n in run(monitor, subs["target"])["notifications"]}
    assert set(target) == {"990000302", "990000304"}
    assert target["990000302"]["kind"] == "activity_revised"
    assert target["990000302"]["changes"]["attributes"] == ["data_validity_comment"]
    assert target["990000304"]["kind"] == "new_activity"
    assert "CHEMBL_100" in target["990000304"]["message"]
    # The target itself did not change in CHEMBL_100: no notice for it.
    assert run(monitor, subs["taxon"])["notifications"] == []
    assert run(monitor, subs["target"])["notifications"] == []


def test_refresh_is_bounded_idempotent_and_receipted(env):
    _, monitor, _, clock = env
    source = h.source("chembl", revision=True)
    first = monitor.refresh(h.NS, source, principal_id="svc", scopes=h.SCOPES,
                            transport=fixture_transport(h.pages("chembl", revision=True)), max_documents=1)
    assert first["status"] == "bounded" and len(first["pages"]) == 1
    clock["now"] += 1
    full = monitor.refresh(h.NS, source, principal_id="svc", scopes=h.SCOPES,
                           transport=fixture_transport(h.pages("chembl", revision=True)))
    assert full["status"] == "complete" and full["new_revisions"] == 2
    clock["now"] += 1
    again = monitor.refresh(h.NS, source, principal_id="svc", scopes=h.SCOPES,
                            transport=fixture_transport(h.pages("chembl", revision=True)))
    assert again["new_revisions"] == 0 and again["unchanged"] == 5

    def limited(**_):
        return {"status": 429, "headers": {"Retry-After": "60"}, "content": b"", "origin": "fixture"}

    clock["now"] += 1
    stopped = monitor.refresh(h.NS, source, principal_id="svc", scopes=h.SCOPES, transport=limited)
    assert stopped["status"] == "stopped" and stopped["stopped"]["code"] == "rate_limited"
    waiting = monitor.refresh(h.NS, source, principal_id="svc", scopes=h.SCOPES, transport=limited)
    assert waiting["status"] == "rate_limited_wait"


def test_watch_validation(env):
    _, monitor, _, _ = env
    with pytest.raises(LifeSciError):
        monitor.create(h.NS, "bad", watch={"accession": "X9EXA1", "taxon": "1"}, principal_id="a", scopes=h.SCOPES)
    with pytest.raises(LifeSciError):
        monitor.create(h.NS, "bad2", watch={"taxon": "Exampla"}, principal_id="a", scopes=h.SCOPES)
