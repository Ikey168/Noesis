"""Campaign-finance monitors: new, amended and unchanged filings and independent expenditures (#2521)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.campaign_finance_sources import fixture_transport
from src.kb.campaign_finance_links import CampaignFinanceLinks
from src.kb.campaign_finance_monitoring import CampaignFinanceMonitor
from src.kb.campaign_finance_records import CampaignFinanceError
from src.kb.subscriptions import SubscriptionStore
from tests.unit import campaign_finance_harness as h


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def test_committee_monitor_reports_new_amended_and_unchanged_filings_with_citations():
    conn = h.connection()
    h.load_all(conn)
    monitor = CampaignFinanceMonitor(conn)
    watch = monitor.create(h.NS, "campaign", watch="committee", key=h.CAMPAIGN, principal_id="alice",
                           scopes=h.SCOPES)
    assert "no new scheduler" in watch["refresh"]
    with pytest.raises(CampaignFinanceError):
        monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)  # no committed watermark
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    first = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(first) == ["amendment_filed", "filing_published", "filing_published"]
    h.apply(conn, "us-fec-filings", version="v2", run_id="run:v2")
    SubscriptionStore(conn).commit_watermark(h.NS, 2)
    second = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(second) == ["amendment_filed", "most_recent_flag_changed", "most_recent_flag_changed"]
    amendment = next(n for n in second["notifications"] if n["kind"] == "amendment_filed")
    assert amendment["amendment_chain"] == [1500101, 1500150, 1500170]
    assert amendment["cites"]["revision_id"] and amendment["cites"]["previous_revision_id"] is None
    flag = next(n for n in second["notifications"] if n["record_key"].endswith(":1500150"))
    assert flag["before"] == [True, 1500150] and flag["after"] == [False, 1500170]
    assert flag["cites"]["previous_revision_id"] and flag["cites"]["previous_revision_id"] != flag["cites"]["revision_id"]
    SubscriptionStore(conn).commit_watermark(h.NS, 3)
    assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    polled = monitor.poll(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert polled["events"]


def test_contest_and_organisation_monitors_cite_expenditures_and_never_name_individuals():
    conn = h.accepted_world()
    CampaignFinanceLinks(conn).link_contests(h.NS, principal_id="alice", scopes=h.SCOPES)
    monitor = CampaignFinanceMonitor(conn)
    contest = monitor.create(h.NS, "county", watch="contest", key=h.contest(conn, "us-fips-county", "99001"),
                             principal_id="alice", scopes=h.SCOPES)
    organisation = monitor.create(h.NS, "industries", watch="organisation", key="lei:5299EXAMPLEINDUSTR01",
                                  ownership_namespace=h.OWN_NS, principal_id="alice", scopes=h.SCOPES)
    uk = monitor.create(h.NS, "party", watch="committee", key=h.UK_PARTY, principal_id="alice", scopes=h.SCOPES)
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    seen = monitor.run(contest["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    ies = [n for n in seen["notifications"] if n["kind"] == "independent_expenditure_reported"]
    assert sorted((n["support_oppose_indicator"], n["source_assertion"]) for n in ies) == [
        ("O", "periodic report"), ("S", "24/48-hour notice"), ("S", "periodic report")]
    donations = monitor.run(organisation["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(donations).count("contribution_reported") == 3
    assert monitor.run(uk["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    h.apply(conn, "uk-ec-donations", version="v2", run_id="run:ec2")
    SubscriptionStore(conn).commit_watermark(h.NS, 2)
    revised = monitor.run(uk["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert "filing_revised" in kinds(revised) and "filing_published" in kinds(revised)  # Q1 gained a late item; Q2
    text = json.dumps([seen, donations, revised])
    assert "PLACEHOLDER" not in text and "FICTIVE, JO" not in text and "Pat Placeholder" not in text
    with pytest.raises(CampaignFinanceError):
        monitor.create(h.NS, "bad", watch="contest", key="not-a-contest", principal_id="alice", scopes=h.SCOPES)


def test_refresh_is_bounded_idempotent_receipted_and_unverified_live_revisions_are_withheld():
    conn = h.connection()
    monitor = CampaignFinanceMonitor(conn)
    source = h.source("us-fec-filings")
    pages = h.native_pages("us-fec-filings")
    first = monitor.refresh(source, run_id="refresh-1", principal_id="op", scopes=h.SCOPES,
                            transport=fixture_transport(pages), secret="k")
    assert first["units"] == 3 and first["complete"] and first["counts"]["new"] == 7
    assert len(first["receipts"]) == 3
    again = monitor.refresh(source, run_id="refresh-2", principal_id="op", scopes=h.SCOPES,
                            transport=fixture_transport(pages), secret="k")
    assert again["counts"].get("new", 0) == 0 and again["counts"]["unchanged"] == 7

    def live(**kwargs):
        response = dict(fixture_transport(h.native_pages("us-fec-filings", "v2"))(**kwargs))
        response.pop("origin")
        return response

    watch = monitor.create(h.NS, "live", watch="committee", key=h.CAMPAIGN, principal_id="alice", scopes=h.SCOPES)
    monitor.refresh(source, run_id="refresh-live", principal_id="op", scopes=h.SCOPES, transport=live, secret="k")
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    result = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert result["withheld_unverified_live_revisions"] >= 1  # live, unverified-live provider: not notified
