"""Monitor new actions, decisions, appeals and corrections through subscriptions (#2651, EN11)."""

from __future__ import annotations

import pytest

from src.ingestion.enforcement_sources import fixture_transport
from src.kb.enforcement import EnforcementError, EnforcementStore
from src.kb.enforcement_monitoring import EnforcementMonitor
from src.kb.subscriptions import SubscriptionStore
from tests.unit import enforcement_harness as h


def kinds(result):
    return sorted({n["kind"] for n in result["notifications"]})


@pytest.fixture()
def conn():
    connection = h.connection()
    h.reviewed(connection)
    yield connection
    connection.close()


def test_new_revised_and_unchanged_cases_cite_before_and_after_revisions(conn):
    monitor = EnforcementMonitor(conn, now=h.Clock())
    subs = SubscriptionStore(conn)
    subs.commit_watermark(h.NS, 1)
    specs = {"entity": {"watch": "entity", "key": h.HOLD_ENTITY, "ownership_namespace": h.OWN_NS, "group": True},
             "authority": {"watch": "authority", "key": "us-epa"},
             "basis": {"watch": "legal_basis", "key": "Regulation (EU) 2016/679"},
             "action": {"watch": "action", "key": h.FCA_EX}}
    ids = {name: monitor.create(h.NS, name, principal_id="alice", scopes=h.SCOPES, **spec)["subscription_id"]
           for name, spec in specs.items()}
    first = {name: monitor.run(i, 1, principal_id="alice", scopes=h.SCOPES) for name, i in ids.items()}
    assert {"new_action", "new_decision", "new_penalty"} <= set(kinds(first["entity"]))
    assert set(kinds(first["authority"])) == {"new_action", "new_penalty"}
    assert kinds(first["basis"]) == ["new_action", "new_decision", "new_penalty"]
    new = next(n for n in first["action"]["notifications"] if n["kind"] == "new_action")
    assert new["cites"]["revision"] == 1 and new["cites"]["previous"] is None
    assert new["evidence_origin"] == "fixture" and "not an assessment" in new["notice"]
    for source_id in h.SOURCES:
        h.apply(conn, source_id, v2=True)
    subs.commit_watermark(h.NS, 2)
    second = {name: monitor.run(i, 2, principal_id="alice", scopes=h.SCOPES) for name, i in ids.items()}
    action = second["action"]
    assert set(kinds(action)) == {"action_corrected", "decision_corrected"}
    corrected = next(n for n in action["notifications"] if n["kind"] == "decision_corrected")
    assert corrected["cites"]["previous"]["revision"] == 1 and corrected["cites"]["revision"] == 2
    assert {"amended_on", "content_sha256", "correction_as_published", "source_status"} <= set(
        corrected["changed_fields"])
    assert corrected["changes"]["amended_on"] == {"before": None, "after": "2025-05-02"}
    epa = second["authority"]
    assert set(kinds(epa)) == {"action_revised", "penalty_revised"}
    revised = next(n for n in epa["notifications"] if n["kind"] == "action_revised")
    assert revised["changes"]["status_as_published"] == {"before": "Active", "after": "Closed"}
    assert "action_removed_by_source" in kinds(second["basis"])
    # Unchanged payloads emit nothing.
    subs.commit_watermark(h.NS, 3)
    assert monitor.run(ids["action"], 3, principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    assert monitor.poll(ids["action"], principal_id="alice", scopes=h.SCOPES)["events"]


def test_refresh_is_bounded_idempotent_with_receipts_and_live_unverified_revisions_are_withheld(conn):
    monitor = EnforcementMonitor(conn, now=h.Clock())
    source = h.source("epa-echo-enforcement-cases")
    pages = h.native_pages("epa-echo-enforcement-cases", h.V2["epa-echo-enforcement-cases"])
    first = monitor.refresh(source, run_id="refresh-1", scopes=h.SCOPES, transport=fixture_transport(pages))
    assert first["complete"] and first["units"] == 2 and first["counts"]["revised"] > 0
    assert len(first["receipts"]) == 2 and all(r["requests"] for r in first["receipts"])
    again = monitor.refresh(source, run_id="refresh-2", scopes=h.SCOPES, transport=fixture_transport(pages))
    assert again["counts"].get("inserted", 0) == 0 and again["counts"].get("revised", 0) == 0
    with pytest.raises(EnforcementError):
        monitor.refresh(source, run_id="refresh-3", scopes=h.READ_ONLY, transport=fixture_transport(pages))
    # A live revision from an unverified-live provider is withheld from notifications.
    store = EnforcementStore(conn)
    live = dict(store.by_key(h.NS, h.EPA_NW)["record"])
    live["source"] = {**live["source"], "evidence_origin": "live", "revision": "2099-01-01"}
    live["status_as_published"] = "Reopened"
    live.pop("unknowns")
    store.apply(h.NS, [live], run_id="live-run")
    subs = SubscriptionStore(conn)
    subs.commit_watermark(h.NS, 5)
    sub = monitor.create(h.NS, "epa-live", watch="action", key=h.EPA_NW, principal_id="alice", scopes=h.SCOPES)
    result = monitor.run(sub["subscription_id"], 5, principal_id="alice", scopes=h.SCOPES)
    assert result["withheld_unverified_live_revisions"] == 1
    shown = next(n for n in result["notifications"] if n["kind"] == "new_action")
    assert shown["summary"]["status_as_published"] == "Closed"


def test_invalid_watches_are_refused(conn):
    monitor = EnforcementMonitor(conn)
    with pytest.raises(EnforcementError):
        monitor.create(h.NS, "x", watch="person", key="Jordan Placeholder", principal_id="alice", scopes=h.SCOPES)
    with pytest.raises(EnforcementError):
        monitor.create(h.NS, "y", watch="entity", key=h.HOLD_ENTITY, principal_id="alice", scopes=h.SCOPES)
    with pytest.raises(EnforcementError):
        monitor.create(h.NS, "z", watch="action", key="LR-99901", principal_id="alice", scopes=h.SCOPES)
