"""Reported totals with the amendment version stated, affiliate donations and contest filings (#2514, #2517)."""

from __future__ import annotations

import json

from src.kb.campaign_finance_links import CampaignFinanceLinks
from src.kb.campaign_finance_queries import CampaignFinanceQueries
from src.kb.campaign_finance_records import forbidden_keys
from tests.unit import campaign_finance_harness as h


def ask(conn):
    return CampaignFinanceQueries(conn)


def test_totals_as_of_a_date_name_the_version_used_and_show_the_amendment_differences():
    conn = h.connection()
    h.load_all(conn)
    early = ask(conn).reported_totals(h.NS, h.CAMPAIGN, scopes=h.SCOPES, as_of="2099-05-01")
    (q1,) = early["reports"]
    assert q1["version_used"]["file_number"] == 1500101 and q1["totals_as_reported"]["total_receipts"] == 107500.0
    assert [v["available_as_of"] for v in q1["amendment_chain"]] == [True, False]
    assert early["not_yet_filed_as_of"][0]["filing_group"] == "campaign-finance:fec:filing-group:1500201"
    later = ask(conn).reported_totals(h.NS, h.CAMPAIGN, scopes=h.SCOPES, as_of="2099-07-31")
    q1, q2 = later["reports"]
    assert q1["version_used"]["file_number"] == 1500150 and q1["version_used"]["amendment_indicator"] == "A"
    assert {"field": "total_receipts", "from_file": 1500101, "to_file": 1500150, "before": 107500.0,
            "after": 107400.0} in q1["differences_between_versions"]
    assert q2["version_used"]["file_number"] == 1500201 and "derived" not in later
    assert q1["citation"]["revision_id"] and q1["citation"]["observed_at"]
    h.apply(conn, "us-fec-filings", version="v2", run_id="run:v2")
    amended = ask(conn).reported_totals(h.NS, h.CAMPAIGN, scopes=h.SCOPES, as_of="2099-09-01")
    q1 = amended["reports"][0]
    assert q1["version_used"]["file_number"] == 1500170
    assert [v["most_recent_as_published"] for v in q1["amendment_chain"]] == [False, False, True]
    before_second = ask(conn).reported_totals(h.NS, h.CAMPAIGN, scopes=h.SCOPES, as_of="2099-07-31")
    assert before_second["reports"][0]["version_used"]["file_number"] == 1500150  # as-of stays reproducible
    assert forbidden_keys(amended) == []


def test_a_cross_period_sum_is_labelled_derived_and_lists_the_versions_used():
    conn = h.connection()
    h.load_all(conn)
    answer = ask(conn).reported_totals(h.NS, h.CAMPAIGN, scopes=h.SCOPES, as_of="2099-12-31", derive=True)
    derived = answer["derived"]
    assert derived["label"] == "derived" and derived["sums"]["total_receipts"] == 171400.0
    assert [v["filing_key"] for v in derived["filing_versions_used"]] == [
        "campaign-finance:fec:filing:1500150", "campaign-finance:fec:filing:1500201"]
    assert "cash_on_hand_end_period" in derived["not_summed"]
    period = ask(conn).reported_totals(h.NS, h.CAMPAIGN, scopes=h.SCOPES, as_of="2099-12-31",
                                       period_start="2099-04-01", period_end="2099-06-30")
    assert [r["report_type"] for r in period["reports"]] == ["Q2"]
    fund = ask(conn).reported_totals(h.NS, h.SUPER_PAC, scopes=h.SCOPES, as_of="2099-12-31")
    assert [(r["form_type"], r["version_used"]["amendment_indicator"]) for r in fund["reports"]] == [
        ("F24", "N"), ("F3X", "N"), ("F3X", "T")]


def test_none_on_record_and_commission_returns_without_reported_totals():
    conn = h.connection()
    h.load_all(conn)
    none = ask(conn).reported_totals(h.NS, h.NOT_ACQUIRED, scopes=h.SCOPES)
    assert none["status"] == "none_on_record" and none["reports"] == []
    uk = ask(conn).reported_totals(h.NS, h.UK_PARTY, scopes=h.SCOPES, derive=True)
    donations = next(r for r in uk["reports"] if "donations" in r["filing_group"])
    assert donations["totals_as_reported"] is None and "none reported" in donations["totals_note"]
    assert donations["derived"]["label"] == "derived" and donations["derived"]["sum"] == 77500.0
    bundle = ask(conn).evidence_bundle(ask(conn).reported_totals(h.NS, h.CAMPAIGN, scopes=h.SCOPES))
    assert bundle["bibliography"] and all(a["citations"] for a in bundle["sections"][0]["assertions"])


def test_individual_items_are_counted_unless_the_individual_scope_is_held():
    conn = h.connection()
    h.load_all(conn)
    public = ask(conn).filing_items(h.NS, "1500101", scopes=h.SCOPES)
    assert public["individual_items_withheld"] == 1  # the individual's contribution
    assert all(i["counterparty"]["kind"] == "organisation" for i in public["items"] if i["record_kind"] ==
               "contribution")
    (salary,) = [i for i in public["items"] if i["counterparty"]["kind"] == "natural-person"]
    assert salary["record_kind"] == "expenditure" and salary["counterparty"]["name"] is None  # payee reduced
    permitted = ask(conn).filing_items(h.NS, "1500101", scopes=h.SCOPES | {h.INDIVIDUAL})
    person = next(i for i in permitted["items"] if i["counterparty"]["kind"] == "natural-person")
    assert person["counterparty"] == {"kind": "natural-person", "entity_type": "IND", "name": None}
    assert person["amount_as_reported"] == 2500.0 and person["citation"]["filing_revision_id"]
    assert "PLACEHOLDER" not in json.dumps(permitted)
    assert ask(conn).filing_items(h.NS, "1599999", scopes=h.SCOPES)["status"] == "none_on_record"


def test_affiliate_donations_follow_accepted_matches_and_cited_ownership_relations():
    conn = h.accepted_world()
    answer = ask(conn).affiliate_donations(h.NS, "lei:5299EXAMPLEINDUSTR01", principal_id="alice", scopes=h.SCOPES,
                                           ownership_namespace=h.OWN_NS, as_of="2099-12-31")
    assert answer["status"] == "answered" and answer["unavailable"] == []
    found = {c["record_key"]: c for c in answer["contributions"]}
    assert set(found) == {"campaign-finance:fec:sa:1500101:4000001", "campaign-finance:fec:sa:1500150:4000011",
                          "campaign-finance:fec:sa:1500310:4000101"}
    energy = found["campaign-finance:fec:sa:1500310:4000101"]
    assert [s["step"] for s in energy["path"]] == ["organisation", "ownership", "identity"]
    assert energy["path"][1]["relation"] == "direct_parent" and energy["path"][1]["assertion_record_id"]
    pac = found["campaign-finance:fec:sa:1500101:4000001"]
    assert [s["step"] for s in pac["path"]] == ["organisation", "identity", "connected-organisation", "identity"]
    assert pac["reported_in"]["superseded_by"] == "campaign-finance:fec:filing:1500150"
    assert found["campaign-finance:fec:sa:1500150:4000011"]["reported_in"]["selected_as_of"] is True
    assert all(c["citation"]["source_id"] and c["citation"]["observed_at"] for c in answer["contributions"])
    early = ask(conn).affiliate_donations(h.NS, "lei:5299EXAMPLEINDUSTR01", principal_id="alice", scopes=h.SCOPES,
                                          ownership_namespace=h.OWN_NS, as_of="2099-05-01")
    assert {c["record_key"] for c in early["contributions"]} == {"campaign-finance:fec:sa:1500101:4000001"}
    direct = ask(conn).affiliate_donations(h.NS, "lei:5299EXAMPLEINDUSTR01", principal_id="alice", scopes=h.SCOPES)
    assert "campaign-finance:fec:sa:1500310:4000101" not in {c["record_key"] for c in direct["contributions"]}
    assert direct["unavailable"][0]["provider"] == "ownership.core"
    assert forbidden_keys(answer) == [] and "never expanded" in answer["notice"]


def test_contest_filings_list_committee_filings_party_returns_and_expenditures_as_reported():
    conn = h.accepted_world()
    CampaignFinanceLinks(conn).link_contests(h.NS, principal_id="alice", scopes=h.SCOPES)
    county = h.contest(conn, "us-fips-county", "99001")
    answer = ask(conn).contest_filings(h.NS, county, scopes=h.SCOPES)
    assert answer["status"] == "answered" and answer["contest"]["unit_native_id"] == "99001"
    assert {f["file_number"] for f in answer["candidate_committee_filings"]} == {1500101, 1500150, 1500201}
    ies = answer["independent_expenditures"]
    assert sorted((i["support_oppose_indicator"], i["source_assertion"]) for i in ies) == [
        ("O", "periodic report"), ("S", "24/48-hour notice"), ("S", "periodic report")]
    assert all(i["citation"]["filing_revision_id"] for i in ies)
    assert ask(conn).contest_filings(h.NS, h.contest(conn, "us-fips-county", "99003"),
                                     scopes=h.SCOPES)["status"] == "none_on_record"
    uk = ask(conn).contest_filings(h.NS, h.contest(conn, "gb-ons-pcon", "E14099901"), scopes=h.SCOPES)
    (spending,) = uk["party_spending_returns"]
    assert spending["link_basis"]["election_name_as_published"] == "UK Parliamentary General Election 2099"
    bundle = ask(conn).evidence_bundle(answer)
    assert len(bundle["sections"][0]["assertions"]) == 6
