"""Extractives monitors through platform.subscriptions: new, revised and unchanged cases (#2702)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.extractives_sources import fixture_transport
from src.kb.extractives_monitoring import ExtractivesMonitor
from src.kb.extractives_records import ExtractivesError
from tests.unit import extractives_harness as h


def _kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def test_country_and_commodity_monitors_hear_new_reports_revisions_and_releases_once():
    conn = h.connection()
    h.load_first(conn)
    h.import_concordances(conn)
    from src.kb.extractives_identity import ExtractivesIdentity

    identity = ExtractivesIdentity(conn)
    h.review_all(identity, identity.propose_commodities(h.NS, principal_id="a", scopes=h.SCOPES)["assertions"])
    h.review_all(identity, identity.propose_countries(h.NS, principal_id="a", scopes=h.SCOPES)["assertions"])
    monitor = ExtractivesMonitor(conn, now=lambda: h.FIRST_RETRIEVAL + 1)
    country = monitor.create(h.NS, "peru", target={"country": "PER"}, principal_id="alice", scopes=h.SCOPES)
    copper = monitor.create(h.NS, "copper-peru", target={"commodity": {"hs_code": "2603"}, "country": "PER",
                                                         "statistic": "production"},
                            principal_id="alice", scopes=h.SCOPES)
    assert "no new scheduler" in country["refresh"]
    first = monitor.run(country["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert _kinds(first) == ["new_report", "new_report"]
    assert all(n["source_revision"]["release_id"] and n["record_id"] for n in first["notifications"])
    assert _kinds(monitor.run(copper["subscription_id"], principal_id="alice", scopes=h.SCOPES)) == [
        "new_release", "new_release"]
    # Unchanged: re-acquiring the same files adds nothing and a re-run emits nothing.
    h.load_first(conn)
    assert monitor.run(country["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    # New and revised: the revised EITI report and the next USGS release and BGS edition.
    h.load_later(conn)
    later = ExtractivesMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 1)
    revised = later.run(country["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert _kinds(revised) == ["report_revision"]
    notice = revised["notifications"][0]
    assert notice["previous_record_id"] and notice["record_id"].endswith("@2")
    assert notice["detail"]["changes"]["lines_changed"][0]["after"]["amount_text"] == "800000.00"
    releases = _kinds(later.run(copper["subscription_id"], principal_id="alice", scopes=h.SCOPES))
    assert releases.count("new_release") == 2 and "revised_values" in releases and "removed_periods" in releases
    # A restart replays nothing.
    again = ExtractivesMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 2)
    assert again.run(copper["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    polled = again.poll(country["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert polled


def test_company_monitors_follow_accepted_matches():
    conn = h.connection()
    h.reviewed(conn)
    monitor = ExtractivesMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 1)
    watch = monitor.create(h.NS, "exampla", target={"company": h.HOLD_ENTITY, "ownership_namespace": h.OWN_NS,
                                                    "group": True}, principal_id="alice", scopes=h.SCOPES)
    kinds = _kinds(monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES))
    assert kinds == ["new_report", "new_report", "report_revision"]
    with pytest.raises(ExtractivesError):
        monitor.create(h.NS, "bad", target={"statistic": "production"}, principal_id="alice", scopes=h.SCOPES)


def test_refresh_is_bounded_idempotent_and_receipted():
    conn = h.connection()
    monitor = ExtractivesMonitor(conn, now=lambda: h.FIRST_RETRIEVAL)
    item = h.source("eiti")
    first = monitor.refresh(h.NS, item, principal_id="op", scopes=h.SCOPES,
                            transport=fixture_transport(h.pages("eiti")), max_documents=1)
    assert first["status"] == "bounded" and first["new_releases"] == 1
    full = monitor.refresh(h.NS, item, principal_id="op", scopes=h.SCOPES,
                           transport=fixture_transport(h.pages("eiti")))
    assert full["status"] == "complete" and full["unchanged_releases"] == 1 and full["new_releases"] == 1
    again = monitor.refresh(h.NS, item, principal_id="op", scopes=h.SCOPES,
                            transport=fixture_transport(h.pages("eiti")))
    assert again["new_releases"] == 0 and again["unchanged_releases"] == 2
    limited = [{**p, "status": 429, "headers": {"Retry-After": "3600"}, "body": ""} for p in h.pages("eiti")]
    stopped = monitor.refresh(h.NS, item, principal_id="op", scopes=h.SCOPES, transport=fixture_transport(limited))
    assert stopped["status"] == "stopped" and stopped["stopped"]["code"] == "rate_limited"
    wait = monitor.refresh(h.NS, item, principal_id="op", scopes=h.SCOPES, transport=fixture_transport(limited))
    assert wait["status"] == "rate_limited_wait"
    receipts = monitor.receipts(h.NS, scopes=h.READ_ONLY)
    assert len(receipts) == 5 and all("contact@example.invalid" not in json.dumps(r) for r in receipts)
