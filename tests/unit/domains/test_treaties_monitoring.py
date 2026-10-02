"""Treaties monitors: new actions, depositary corrections, entry into force and unchanged pages (#2631, TR10)."""

from __future__ import annotations

import pytest

from src.ingestion.treaties_sources import fixture_transport
from src.kb.subscriptions import SubscriptionStore
from src.kb.treaties_monitoring import TreatiesMonitor
from src.kb.treaties_records import TreatiesError, forbidden_keys
from tests.unit import treaties_harness as h


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def test_treaty_monitor_reports_new_revised_removed_and_unchanged_records_with_citations():
    conn = h.connection()
    h.apply(conn, "untc-treaty-status")
    monitor = TreatiesMonitor(conn)
    watch = monitor.create(h.NS, "wetlands", watch="treaty", key="XXVII-99", principal_id="alice", scopes=h.SCOPES)
    assert "no new scheduler" in watch["refresh"]
    with pytest.raises(TreatiesError):
        monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)  # no committed watermark
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    first = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(first).count("action_recorded") == 7 and kinds(first).count("statement_recorded") == 4
    assert "entry_into_force_recorded" in kinds(first) and "treaty_recorded" in kinds(first)
    h.apply(conn, "untc-treaty-status", v2=True)
    SubscriptionStore(conn).commit_watermark(h.NS, 2)
    second = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    # Southland's ratification; the corrected accession date; Oldland's succession no longer shown; Southland's
    # declaration and the depositary's correction note.
    assert kinds(second) == ["action_recorded", "depositary_correction", "removed_by_source", "statement_recorded",
                             "statement_recorded"]
    correction = next(n for n in second["notifications"] if n["kind"] == "depositary_correction"
                      and n["record_key"].endswith("northwind-republic:accession:1"))
    assert correction["changed"]["deposit_date"] == {"before": "2092-05-10", "after": "2092-05-11"}
    assert correction["cites"]["previous_revision_id"] and correction["cites"]["revision_id"] != \
        correction["cites"]["previous_revision_id"]
    assert correction["cites"]["depositary_revision"] == "2099-06-20T10:00:00"
    removed = next(n for n in second["notifications"] if n["kind"] == "removed_by_source")
    assert removed["record_key"].endswith("oldland:succession:1") and "kept, not deleted" in removed["message"]
    SubscriptionStore(conn).commit_watermark(h.NS, 3)
    assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    assert monitor.poll(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["events"]
    assert not forbidden_keys([first, second])


def test_participant_monitor_reports_entry_into_force_and_ratification():
    conn = h.connection()
    h.apply(conn, "coe-treaty-office")
    monitor = TreatiesMonitor(conn)
    watch = monitor.create(h.NS, "southland", watch="participant", key="treaties:coe:participant:southland",
                           principal_id="alice", scopes=h.SCOPES)
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    assert kinds(monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)) == ["action_recorded"]
    h.apply(conn, "coe-treaty-office", v2=True)
    SubscriptionStore(conn).commit_watermark(h.NS, 2)
    later = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(later) == ["action_recorded", "entry_into_force_recorded"]
    assert next(n for n in later["notifications"] if n["kind"] == "entry_into_force_recorded")["effective_date"] == \
        "2099-09-01"
    with pytest.raises(TreatiesError):
        monitor.create(h.NS, "bad", watch="treaty", key="treaties:coe:participant:southland", principal_id="alice",
                       scopes=h.SCOPES)


def test_refresh_is_bounded_idempotent_receipted_and_unverified_live_revisions_are_withheld():
    conn = h.connection()
    monitor = TreatiesMonitor(conn)
    source = h.source("coe-treaty-office")
    pages = h.native_pages("coe-treaty-office")
    first = monitor.refresh(source, run_id="refresh-1", principal_id="op", scopes=h.SCOPES,
                            transport=fixture_transport(pages))
    assert first["units"] == 1 and first["complete"] and first["counts"]["new"] == 17
    assert len(first["receipts"]) == 1 and first["receipts"][0]["receipt"]["requests"]
    again = monitor.refresh(source, run_id="refresh-2", principal_id="op", scopes=h.SCOPES,
                            transport=fixture_transport(pages))
    assert again["counts"]["new"] == 0 and again["counts"]["unchanged"] == 17

    def live(**kwargs):
        response = dict(fixture_transport(h.native_pages("coe-treaty-office", v2=True))(**kwargs))
        response["origin"] = "live"
        return response

    monitor.refresh(source, run_id="refresh-live", principal_id="op", scopes=h.SCOPES, transport=live)
    watch = monitor.create(h.NS, "coe", watch="treaty", key="CETS 999", principal_id="alice", scopes=h.SCOPES)
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    result = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert result["withheld_unverified_live_revisions"] >= 1
    assert not [n for n in result["notifications"] if n["evidence_origin"] == "live"]
