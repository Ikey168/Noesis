"""Monitor new cases, stage changes, decisions and aid awards through subscriptions (#2217, CS11)."""

from __future__ import annotations

import pytest

from src.ingestion.competition_sources import fixture_transport
from src.kb.competition import CompetitionError
from src.kb.competition_monitoring import CompetitionMonitor
from src.kb.subscriptions import SubscriptionStore
from tests.unit import competition_harness as h


def kinds(result):
    return sorted({n["kind"] for n in result["notifications"]})


@pytest.fixture()
def conn():
    connection = h.connection()
    h.reviewed(connection)
    yield connection
    connection.close()


def test_every_event_type_cites_before_and_after_revisions(conn):
    monitor = CompetitionMonitor(conn, now=h.Clock())
    subs = SubscriptionStore(conn)
    subs.commit_watermark(h.NS, 1)
    specs = {"company": dict(watch="company", key=h.HOLD_ENTITY, ownership_namespace=h.OWN_NS, group=True),
             "case": dict(watch="case", key=h.MERGER),
             "beneficiary": dict(watch="beneficiary", key=h.INT_ENTITY, ownership_namespace=h.OWN_NS),
             "authority": dict(watch="authority", key="uk-cma", instrument="merger")}
    ids = {name: monitor.create(h.NS, name, principal_id="alice", scopes=h.SCOPES, **spec)["subscription_id"]
           for name, spec in specs.items()}
    first = {name: monitor.run(i, 1, principal_id="alice", scopes=h.SCOPES) for name, i in ids.items()}
    assert {"new_case", "stage_change", "new_decision_document", "new_aid_award"} <= set(kinds(first["company"]))
    assert kinds(first["beneficiary"]) == ["new_aid_award"]
    for name in ("ec-competition-cases", "eu-state-aid-tam", "uk-cma-cases"):
        h.apply(conn, name, v2=True)
    subs.commit_watermark(h.NS, 2)
    second = {name: monitor.run(i, 2, principal_id="alice", scopes=h.SCOPES) for name, i in ids.items()}
    case = second["case"]
    assert set(kinds(case)) == {"case_revised", "stage_change", "new_decision_document"}
    revised = next(n for n in case["notifications"] if n["kind"] == "case_revised")
    assert revised["cites"]["previous"]["revision"] == 1 and revised["cites"]["revision"] == 2
    assert revised["evidence_origin"] == "fixture"
    award = next(n for n in second["beneficiary"]["notifications"] if n["kind"] == "aid_award_changed")
    assert award["previous_summary"]["amount_as_published"] == "12500000.00"
    assert award["summary"]["status"] == "corrected" and award["cites"]["previous"]["revision_id"]
    assert set(kinds(second["authority"])) == {"case_revised", "stage_change", "new_decision_document"}
    # Unchanged payloads emit nothing.
    subs.commit_watermark(h.NS, 3)
    assert monitor.run(ids["case"], 3, principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    assert monitor.poll(ids["case"], principal_id="alice", scopes=h.SCOPES)["events"]


def test_refresh_is_bounded_idempotent_with_receipts_and_live_unverified_revisions_are_withheld(conn):
    monitor = CompetitionMonitor(conn, now=h.Clock())
    pages = h.native_pages("uk-cma-cases", h.V2["uk-cma-cases"])

    def live(**kwargs):
        return {**fixture_transport(pages)(**kwargs), "origin": "live"}

    refreshed = monitor.refresh(h.source("uk-cma-cases"), run_id="refresh-1", principal_id="op", scopes=h.SCOPES,
                                transport=live)
    assert refreshed["complete"] and refreshed["counts"]["revised"] >= 1
    assert refreshed["receipts"][0]["evidence_origin"] == "live"
    again = monitor.refresh(h.source("uk-cma-cases"), run_id="refresh-2", principal_id="op", scopes=h.SCOPES,
                            transport=live)
    assert again["counts"]["inserted"] == again["counts"]["revised"] == 0 and again["receipts"]
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    sub = monitor.create(h.NS, "cma", watch="case", key=h.CMA, principal_id="alice", scopes=h.SCOPES)
    result = monitor.run(sub["subscription_id"], 1, principal_id="alice", scopes=h.SCOPES)
    assert result["withheld_unverified_live_revisions"] >= 1
    case = next(n for n in result["notifications"] if n["kind"] == "new_case")
    assert case["cites"]["revision"] == 1 and case["evidence_origin"] == "fixture"


def test_invalid_targets_are_refused(conn):
    monitor = CompetitionMonitor(conn)
    with pytest.raises(CompetitionError):
        monitor.create(h.NS, "x", watch="case", key="M.99001", principal_id="alice", scopes=h.SCOPES)
    with pytest.raises(CompetitionError):
        monitor.create(h.NS, "y", watch="company", key=h.HOLD_ENTITY, principal_id="alice", scopes=h.SCOPES)
    with pytest.raises(CompetitionError):
        monitor.create(h.NS, "z", watch="authority", key="bundeskartellamt", principal_id="alice", scopes=h.SCOPES)
