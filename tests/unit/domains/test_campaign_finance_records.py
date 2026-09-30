"""Campaign-finance record store: amendment chains, revisions, filing references, minimisation at write time (#2479)."""

from __future__ import annotations

import copy
import json

import pytest

from src.kb.campaign_finance_records import (
    CampaignFinanceError,
    CampaignFinanceStore,
    forbidden_keys,
    readiness,
)
from tests.unit import campaign_finance_harness as h

GROUP = "campaign-finance:fec:filing-group:1500101"


def store(conn):
    return CampaignFinanceStore(conn, initialize=False)


def records(source_id, version="v1"):
    adapter = h.adapter(source_id, version)
    out, cursor = [], None
    for _ in adapter.units:
        page = adapter.fetch_page({"operation": "selection", "parameters": {}, "limit": 500}, cursor=cursor)
        out += [r["campaign_finance_record"] for r in page.records]
        cursor = page.next_cursor
    return out


def test_amended_filings_are_new_records_and_flags_change_as_revisions_never_overwrites():
    conn = h.connection()
    h.load_all(conn)
    first = store(conn).filing_chain(h.NS, GROUP, scopes=h.SCOPES)
    assert [v["file_number"] for v in first["versions"]] == [1500101, 1500150]
    assert first["selected"]["file_number"] == 1500150
    h.apply(conn, "us-fec-filings", version="v2", run_id="run:v2")
    chain = store(conn).filing_chain(h.NS, GROUP, scopes=h.SCOPES)
    assert [v["file_number"] for v in chain["versions"]] == [1500101, 1500150, 1500170]
    assert [v["most_recent_as_published"] for v in chain["versions"]] == [False, False, True]
    assert [v["totals_as_reported"]["total_receipts"] for v in chain["versions"]] == [107500.0, 107400.0, 107400.0]
    history = store(conn).history(h.NS, "campaign-finance:fec:filing:1500150", scopes=h.SCOPES)
    assert [e["change"] for e in history] == ["new", "revised"]
    assert history[0]["record"]["fields"]["most_recent"] is True  # the earlier observation is kept
    assert history[1]["previous_revision_id"] == history[0]["revision_id"]
    # as-of lookup: the version available on a date, the later versions named
    as_of = store(conn).filing_chain(h.NS, GROUP, scopes=h.SCOPES, as_of="2099-05-01")
    assert as_of["selected"]["file_number"] == 1500101
    assert as_of["later_versions"] == ["campaign-finance:fec:filing:1500150", "campaign-finance:fec:filing:1500170"]
    assert store(conn).filing_chain(h.NS, GROUP, scopes=h.SCOPES, as_of="2099-01-01")["selected"] is None


def test_line_items_reference_the_filing_revision_they_were_reported_against():
    conn = h.connection()
    h.load_all(conn)
    items = store(conn).records(h.NS, scopes=h.SCOPES, kinds=["contribution"], filing_key=(
        "campaign-finance:fec:filing:1500150"))
    assert {i["record_key"] for i in items} == {"campaign-finance:fec:sa:1500150:4000011",
                                                "campaign-finance:fec:sa:1500150:4000012",
                                                "campaign-finance:fec:sa:1500150:4000013"}
    filing = store(conn).records(h.NS, scopes=h.SCOPES, record_keys=["campaign-finance:fec:filing:1500150"])[0]
    assert {i["filing_revision_id"] for i in items} == {filing["revision_id"]}
    assert all(i["citation"]["filing_revision_id"] == filing["revision_id"] for i in items)
    assert sum(i["individual"] for i in items) == 1


def test_registrations_are_dated_revisions_and_an_older_one_observed_later_never_becomes_current():
    conn = h.connection()
    committee = [r for r in records("us-fec-committees") if r["record_key"].endswith("C00999901")]
    newer, older = committee
    cf = CampaignFinanceStore(conn)
    assert cf.project(h.NS, [newer], run_id="r1", source_id="us-fec-committees")["counts"]["new"] == 1
    late = cf.project(h.NS, [older], run_id="r2", source_id="us-fec-committees")
    assert late["counts"]["older-observation"] == 1
    (current,) = cf.records(h.NS, scopes=h.SCOPES, kinds=["committee"])
    assert current["record"]["fields"]["cycle"] == 2100 and current["revision_count"] == 2
    again = cf.project(h.NS, [older, newer], run_id="r3", source_id="us-fec-committees")
    assert again["counts"]["unchanged"] == 2  # replays add nothing


def test_commission_corrections_and_late_reports_are_revisions_linked_to_the_original():
    conn = h.connection()
    h.apply(conn, "uk-ec-donations", run_id="run:ec1")
    h.apply(conn, "uk-ec-donations", version="v2", run_id="run:ec2")
    cf = store(conn)
    corrected = cf.history(h.NS, "campaign-finance:ukec:donation:C0990001", scopes=h.SCOPES)
    assert [e["change"] for e in corrected] == ["new", "revised"]
    assert [e["record"]["fields"]["amount_as_reported"] for e in corrected] == ["25000.00", "25500.00"]
    (late,) = cf.history(h.NS, "campaign-finance:ukec:donation:C0990004", scopes=h.SCOPES)
    q1 = cf.history(h.NS, "campaign-finance:ukec:filing:donations:9901:q1-2099", scopes=h.SCOPES)
    assert late["late_observation"] is True and late["filing_revision_id"] == q1[-1]["revision_id"]
    assert [e["change"] for e in q1] == ["new", "revised"]  # the return gained the late item
    (new_period,) = cf.history(h.NS, "campaign-finance:ukec:donation:C0990005", scopes=h.SCOPES)
    assert new_period["late_observation"] is False  # its return was first seen in the same run


def test_minimisation_is_enforced_at_write_time_and_nothing_is_written():
    conn = h.connection()
    cf = CampaignFinanceStore(conn)
    person = next(r for r in records("us-fec-schedule-a") if r["minimisation"]["counterparty"] == "natural-person")
    leaky = copy.deepcopy(person)
    leaky["fields"]["counterparty"]["name"] = "PLACEHOLDER, PAT"
    with pytest.raises(CampaignFinanceError) as refused:
        cf.project(h.NS, [person, leaky], run_id="r", source_id="us-fec-schedule-a")
    assert refused.value.code == "minimisation_violation"
    assert "$.fields.counterparty.name" in refused.value.details["paths"]
    extra = copy.deepcopy(person)
    extra["fields"]["contributor_employer"] = "FICTIONAL EMPLOYER"
    with pytest.raises(CampaignFinanceError):
        cf.project(h.NS, [extra], run_id="r", source_id="us-fec-schedule-a")
    assert cf.records(h.NS, scopes=h.SCOPES) == []
    orphan = copy.deepcopy(person)
    orphan["filing_key"] = None
    with pytest.raises(CampaignFinanceError) as unfiled:
        cf.project(h.NS, [orphan], run_id="r", source_id="us-fec-schedule-a")
    assert unfiled.value.code == "invalid_record"


def test_reads_are_scoped_idempotent_and_carry_no_forbidden_keys():
    conn = h.connection()
    h.load_all(conn)
    before = conn.execute("SELECT count(*) FROM campaign_finance_revisions").fetchone()[0]
    h.load_all(conn, run_id="replay")
    assert conn.execute("SELECT count(*) FROM campaign_finance_revisions").fetchone()[0] == before
    with pytest.raises(CampaignFinanceError) as refused:
        store(conn).records(h.NS, scopes={"knowledge:political:campaign-finance:read"})
    assert refused.value.code == "unauthorized"
    everything = store(conn).records(h.NS, scopes=h.SCOPES)
    assert forbidden_keys(everything) == []
    text = json.dumps(everything)
    assert "PLACEHOLDER, PAT" not in text and "Pat Placeholder" not in text
    state = readiness(conn)
    assert state["store_ready"] and state["providers"]["openfec"]["live"] == "unverified-live"
    assert state["minimisation"]["minimised_individual_item_revisions"] == 6
    assert state["enabled"] == {"US": False, "GB": False}
