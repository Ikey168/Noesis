"""Monitor new actions, decisions, penalties, appeals and corrections through subscriptions (#2651, EN11)."""

from __future__ import annotations

import pytest

from src.ingestion.enforcement_sources import fixture_transport
from src.kb.enforcement import EnforcementError
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


def test_new_revised_corrected_removed_and_unchanged_cases(conn):
    monitor = EnforcementMonitor(conn, now=h.Clock())
    subs = SubscriptionStore(conn)
    subs.commit_watermark(h.NS, 1)
    specs = {"entity": {"watch": "entity", "key": h.HOLD_ENTITY, "ownership_namespace": h.OWN_NS, "group": True},
             "action": {"watch": "action", "key": h.SEC_LR},
             "authority": {"watch": "authority", "key": "eu-sa-nl"},
             "basis": {"watch": "legal_basis", "key": "Principle 3"},
             "identifier": {"watch": "identifier", "key": "sec-cik:0009999101"}}
    ids = {name: monitor.create(h.NS, name, principal_id="alice", scopes=h.SCOPES, **spec)["subscription_id"]
           for name, spec in specs.items()}
    first = {name: monitor.run(i, 1, principal_id="alice", scopes=h.SCOPES) for name, i in ids.items()}
    assert {"new_action", "new_decision", "new_penalty", "new_notice_document"} <= set(kinds(first["entity"]))
    assert all(n["cites"]["revision_id"] for n in first["entity"]["notifications"])
    for source_id in ("sec-litigation-releases", "fca-final-notices", "edpb-art60-decisions"):
        h.apply(conn, source_id, v2=True)
    subs.commit_watermark(h.NS, 2)
    second = {name: monitor.run(i, 2, principal_id="alice", scopes=h.SCOPES) for name, i in ids.items()}
    action = second["action"]
    assert {"action_revised", "decision_revised", "new_notice_document", "notice_document_revised"} <= set(
        kinds(action))
    revised = next(n for n in action["notifications"] if n["kind"] == "action_revised")
    assert revised["cites"]["previous"]["revision"] == 1 and revised["cites"]["revision"] == 2
    assert "decided_on" in revised["changed_fields"]
    assert revised["changes"]["decided_on"] == {"before": None, "after": "2099-04-03"}
    assert revised["evidence_origin"] == "fixture" and "not an assessment" in revised["note"]
    corrected = next(n for n in second["basis"]["notifications"] if n["kind"] == "action_corrected")
    assert "legal_bases" in corrected["changed_fields"]
    removed = next(n for n in second["authority"]["notifications"] if n["action_key"] == h.EDPB_NL)
    assert removed["kind"] == "action_removed_by_source"
    assert "action_revised" in kinds(second["identifier"])
    subs.commit_watermark(h.NS, 3)
    assert monitor.run(ids["action"], 3, principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    assert monitor.poll(ids["action"], principal_id="alice", scopes=h.SCOPES)["events"]


def test_refresh_is_bounded_idempotent_with_receipts_and_live_unverified_revisions_are_withheld(conn):
    monitor = EnforcementMonitor(conn, now=h.Clock())
    pages = h.v2_pages("epa-echo-cases")

    def live(**kwargs):
        return {**fixture_transport(pages)(**kwargs), "origin": "live"}

    refreshed = monitor.refresh(h.source("epa-echo-cases"), run_id="refresh-1", scopes=h.SCOPES, transport=live)
    assert refreshed["complete"] and refreshed["counts"]["revised"] >= 1 and refreshed["units"] == 1
    assert refreshed["receipts"][0]["evidence_origin"] == "live"
    again = monitor.refresh(h.source("epa-echo-cases"), run_id="refresh-2", scopes=h.SCOPES, transport=live)
    assert again["counts"]["inserted"] == again["counts"]["revised"] == 0 and again["receipts"]
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    sub = monitor.create(h.NS, "epa", watch="action", key=h.EPA, principal_id="alice", scopes=h.SCOPES)
    result = monitor.run(sub["subscription_id"], 1, principal_id="alice", scopes=h.SCOPES)
    assert result["withheld_unverified_live_revisions"] >= 1
    action = next(n for n in result["notifications"] if n["kind"] == "new_action")
    assert action["cites"]["revision"] == 1 and action["evidence_origin"] == "fixture"


def test_invalid_targets_and_natural_persons_are_refused(conn):
    monitor = EnforcementMonitor(conn)
    with pytest.raises(EnforcementError):
        monitor.create(h.NS, "x", watch="action", key="LR-99901", principal_id="alice", scopes=h.SCOPES)
    with pytest.raises(EnforcementError):
        monitor.create(h.NS, "y", watch="entity", key=h.HOLD_ENTITY, principal_id="alice", scopes=h.SCOPES)
    with pytest.raises(EnforcementError):
        monitor.create(h.NS, "z", watch="authority", key="bafin", principal_id="alice", scopes=h.SCOPES)
    with pytest.raises(EnforcementError) as caught:
        monitor.create(h.NS, "p", watch="entity", key=h.PERSON_ENTITY, ownership_namespace=h.OWN_NS,
                       principal_id="alice", scopes=h.SCOPES)
    assert caught.value.code == "natural_person_not_a_query_key"
