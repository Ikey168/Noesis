"""Legislation source contracts and the congress.gov, GovInfo, UK Bills, Votes and Hansard adapters (#2388, #2402,
#2407, #2412, #2419)."""

from __future__ import annotations

import hashlib
import json

import pytest

from src.ingestion.legislation_sources import (
    BOUNDED_COVERAGE,
    FIXTURE_SECRET,
    FORMATS,
    LIVE_VERIFICATION,
    OGL_ATTRIBUTION,
    PROVIDER_CONTRACTS,
)
from src.ingestion.lobbying_sources import parse_export
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.kb.legislation import LegislationStore
from tests.unit import legislation_harness as h


@pytest.fixture()
def conn():
    connection = h.connection()
    yield connection
    connection.close()


def records(source_id, **kw):
    return [r["legislation_record"] for page in h.fetch(source_id, **kw) for r in page.records]


def test_every_provider_has_a_contract_licence_and_live_verification_status():
    assert set(PROVIDER_CONTRACTS) == {"congress-gov", "senate-lis", "govinfo", "uk-bills", "uk-commons-votes",
                                       "uk-lords-votes", "uk-hansard"}
    assert {f for c in PROVIDER_CONTRACTS.values() for f in c["formats"]} == set(FORMATS)
    for provider, contract in PROVIDER_CONTRACTS.items():
        for key in ("endpoints", "authentication", "rate_limits", "identifiers", "revisions", "licence",
                    "attribution", "reason"):
            assert contract[key], (provider, key)
        assert contract["access_decision"] == "unverified-live"
        assert LIVE_VERIFICATION[provider]["status"] == "unverified-live"
    for provider in ("uk-bills", "uk-commons-votes", "uk-lords-votes", "uk-hansard"):
        assert PROVIDER_CONTRACTS[provider]["attribution"] == OGL_ATTRIBUTION
        assert "Open Parliament Licence" in PROVIDER_CONTRACTS[provider]["licence"]
    assert "public domain" in PROVIDER_CONTRACTS["congress-gov"]["licence"]
    assert set(BOUNDED_COVERAGE) == {"us", "gb"}
    audit = (h.ROOT / "docs/development/legislation-evidence/source-audit.md").read_text()
    for needle in ("_verify_", "Bounded first coverage", "Mapping gap", "Open Parliament Licence", "X-Api-Key"):
        assert needle in audit


def test_the_source_pack_declares_every_source_with_live_verification_and_replays_offline():
    manifest = h.manifest()
    assert manifest["version"] == "1.3.0"
    ours = [s for s in manifest["sources"] if s["connector"] == "legislation"]
    assert {s["source_id"] for s in ours} == set(h.SOURCES)
    for item in ours:
        assert item["legislation"]["live_verification"] == "unverified-live"
        assert item["mapping"]["target_schema"] == "noesis-legislation-record-v1"
        assert item["endpoint"].startswith("https://") and item["license"]["terms_url"].startswith("https://")
        keyed = FORMATS[item["legislation"]["format"]]["keyed"]
        assert (item["auth"]["kind"] == "required-secret") == keyed
    report = SourcePackConformance(h.ROOT).offline(manifest)
    assert report["valid"], [s for s in report["sources"] if not s["valid"]]


def test_congress_bill_keeps_actions_verbatim_sponsors_and_labelled_crs_summaries():
    (bill,) = records("us-congress-gov-bills")
    assert bill["record_key"] == bill["bill_key"] == h.US_BILL
    assert bill["bill_link"] == {"basis": "record-identity"} and bill["evidence_origin"] == "fixture"
    fields = bill["fields"]
    assert (fields["congress"], fields["bill_type"], fields["bill_number"]) == (156, "hr", 9901)
    assert [a["action_date"] for a in fields["actions"]] == sorted(a["action_date"] for a in fields["actions"])
    passage = next(a for a in fields["actions"] if a["action_code"] == "H38310")
    assert passage["text"] == "On passage Passed by the Yeas and Nays: 2 - 1 (Roll no. 101)."
    assert passage["recorded_votes"][0]["roll_number"] == 101
    assert fields["sponsors"][0]["member_key"] == "legislation:member:us-bioguide:S009901"
    withdrawn = next(c for c in fields["cosponsors"] if c["member_id"] == "P009903")
    assert (withdrawn["sponsorship_date"], withdrawn["withdrawn_date"]) == ("2099-02-20", "2099-03-01")
    (summary,) = fields["crs_summaries"]
    assert summary["label"] == "CRS summary" and "not the bill's text or its legal effect" in summary["notice"]
    assert fields["laws"] == []


def test_receipts_name_every_request_and_never_the_api_key():
    (page,) = h.fetch("us-congress-gov-bills")
    receipt = page.receipt
    assert [r["name"] for r in receipt["requests"]] == ["bill", "actions", "cosponsors", "summaries"]
    assert all(len(r["sha256"]) == 64 and r["status"] == 200 for r in receipt["requests"])
    assert receipt["live_verification"] == "unverified-live" and receipt["evidence_origin"] == "fixture"
    assert FIXTURE_SECRET not in json.dumps([receipt, [dict(r) for r in page.records]])
    with pytest.raises(SourcePackError) as missing:
        h.fetch("us-congress-gov-bills", secret=None)
    assert missing.value.code == "authentication_failed"


def test_update_date_change_is_a_new_revision_and_a_replay_adds_nothing(conn):
    (first,) = h.apply(conn, "us-congress-gov-bills")
    assert first["counts"]["new"] == 1
    (replay,) = h.apply(conn, "us-congress-gov-bills", run_id="run:replay")
    assert replay["counts"] == {"new": 0, "revised": 0, "unchanged": 1, "older-observation": 0}
    (second,) = h.apply(conn, "us-congress-gov-bills", v2=True)
    assert second["counts"]["revised"] == 1
    store = LegislationStore(conn)
    current = store.record(h.NS, h.US_BILL, scopes=h.SCOPES)
    assert current["revision_no"] == 2 and current["native_revision"] == "2099-05-02T08:00:00Z"
    assert current["record"]["fields"]["laws"][0]["citation"] == "Pub. L. 156-12"
    # A late older observation is logged but never replaces the current revision.
    (late,) = h.apply(conn, "us-congress-gov-bills", run_id="run:late")
    assert late["counts"]["older-observation"] == 1
    assert store.record(h.NS, h.US_BILL, scopes=h.SCOPES)["revision_id"] == current["revision_id"]
    history = store.history(h.NS, h.US_BILL, "us-congress-gov-bills", scopes=h.SCOPES)
    assert [e["change"] for e in history] == ["new", "revised", "older-observation"]
    assert store.receipts(h.NS, "run:replay", scopes=h.SCOPES)[0]["outcome"]["counts"]["unchanged"] == 1


def test_roll_calls_keep_member_positions_by_published_identifier():
    (house,) = records("us-congress-gov-house-votes")
    assert house["record_key"] == "us-roll-call:156-house-1-101" and house["bill_key"] == h.US_BILL
    assert house["bill_link"]["basis"] == "source-reference"
    positions = {p["member_id"]: p["position"] for p in house["fields"]["positions"]}
    assert positions == {"S009901": "Yea", "E009902": "Yea", "P009903": "Nay", "A009904": "Not Voting"}
    (senate,) = records("us-senate-roll-calls")
    assert senate["record_key"] == "us-roll-call:156-senate-1-55" and senate["bill_key"] == h.US_BILL
    assert {p["scheme"] for p in senate["fields"]["positions"]} == {"us-lis"}
    assert senate["fields"]["date"] == "2099-04-20"


def test_govinfo_text_versions_are_hashed_references_and_billstatus_is_a_separate_assertion():
    versions = records("us-govinfo-bills")
    assert [v["fields"]["version_code"] for v in versions] == ["ih", "rh", "eh"]
    ih = versions[0]
    raw = (h.FIXTURES / "govinfo_BILLS-156hr9901ih.xml").read_bytes()
    assert ih["fields"]["content_sha256"] == hashlib.sha256(raw).hexdigest()
    assert ih["fields"]["date_issued"] == "2099-02-03" and ih["fields"]["version_label"] == "Introduced in House"
    assert "Synthetic fixture text" not in json.dumps(versions)
    (status,) = records("us-govinfo-billstatus")
    assert status["record_kind"] == "us-bill-status" and status["record_key"] != h.US_BILL
    assert status["bill_key"] == h.US_BILL and status["provider"] == "govinfo"
    placeholder = next(c for c in status["fields"]["cosponsors"] if c["member_id"] == "P009903")
    assert placeholder["withdrawn_date"] is None  # BILLSTATUS disagrees with congress.gov; kept as published


def test_uk_bill_stages_publications_and_royal_assent_only_when_published(conn):
    first = records("uk-parliament-bills")
    kinds = [r["record_kind"] for r in first]
    assert kinds.count("uk-stage") == 5 and kinds.count("uk-publication") == 2
    bill = first[0]
    assert bill["record_key"] == h.UK_BILL and bill["fields"]["introduced_session_id"] == 39
    assert bill["fields"]["royal_assent"] == "not published"
    assert bill["fields"]["sponsors"][0]["organisation"] == "Department for Example"
    committee = next(r for r in first if r["record_key"] == "uk-stage:3901-20003")
    assert [s["date"] for s in committee["fields"]["sittings"]] == ["2099-02-15", "2099-02-17"]
    publication = next(r for r in first if r["record_kind"] == "uk-publication")
    assert publication["fields"]["publication_type"] == "Bill" and publication["locator"].startswith("https://")
    h.apply(conn, "uk-parliament-bills")
    (result,) = h.apply(conn, "uk-parliament-bills", v2=True)
    assert result["counts"] == {"new": 2, "revised": 1, "unchanged": 7, "older-observation": 0}
    store = LegislationStore(conn)
    assert store.record(h.NS, h.UK_BILL, scopes=h.SCOPES)["record"]["fields"]["royal_assent"] == "published"
    act = store.record(h.NS, "uk-publication:3901-40003", scopes=h.SCOPES)["record"]
    assert act["fields"]["publication_type"] == "Act of Parliament"


def test_divisions_and_debates_are_candidates_with_positions_and_references_only(conn):
    (division,) = records("uk-commons-divisions")
    assert division["bill_key"] is None and division["bill_link"]["candidate_bill_key"] == h.UK_BILL
    positions = {(p["member_id"], p["position"]) for p in division["fields"]["positions"]}
    assert ("4004", "aye_teller") in positions and ("4003", "no") in positions
    (lords,) = records("uk-lords-divisions")
    assert {p["position"] for p in lords["fields"]["positions"]} == {"content", "not_content"}
    (debate,) = records("uk-hansard-debates")
    assert debate["bill_key"] is None and len(debate["fields"]["contributions"]) == 2
    assert "never retained" not in json.dumps(debate)  # contribution text is not mirrored
    h.apply(conn, "uk-commons-divisions")
    (corrected,) = h.apply(conn, "uk-commons-divisions", v2=True)
    assert corrected["counts"]["revised"] == 1


def test_adapter_refuses_redirects_truncation_and_ad_hoc_queries():
    item = h.source("uk-commons-divisions")
    page = h.native_pages("uk-commons-divisions")[0]
    redirected = h.LegislationAdapter(
        item, transport=h.fixture_transport([{**page, "final_url": "https://elsewhere.example/x"}]))
    with pytest.raises(SourcePackError) as redirect:
        redirected.fetch_page({"operation": "selection", "parameters": {}, "limit": 5}, cursor=None)
    assert redirect.value.code == "network_policy"
    actions = json.loads((h.FIXTURES / "congress_bill_hr9901_actions.json").read_text())
    actions["pagination"]["next"] = "https://api.congress.gov/v3/bill/156/hr/9901/actions?offset=250"
    with pytest.raises(SourcePackError) as truncated:
        h.fetch("us-congress-gov-bills",
                bodies={"/v3/bill/156/hr/9901/actions?format=json&limit=250": json.dumps(actions)})
    assert truncated.value.code == "budget_exhausted"
    adapter = h.LegislationAdapter(h.source("uk-lords-divisions"),
                                   transport=h.fixture_transport(h.native_pages("uk-lords-divisions")))
    with pytest.raises(SourcePackError) as adhoc:
        adapter.fetch_page({"operation": "selection", "parameters": {"q": "x"}}, cursor=None)
    assert adhoc.value.code == "parameter_forbidden"


def test_lda_filings_name_bills_in_the_congress_of_the_filing_year():
    export = parse_export("us-lda-json", (h.FIXTURES / "us_lda_filings_2099q1.json").read_bytes())
    assert export["register"] == "us-lda" and export["publication_date"] == "2099-04-18"
    council = next(e for e in export["entries"] if e["native_id"] == "9001-8001")
    keys = [r["key"] for i in council["interests"] for r in i["references"]]
    assert keys == ["us-bill:156-hr-9901", "us-bill:156-s-9950"]
    assert council["interests"][0]["references"][0]["congress_basis"] == "filing year"
    association = next(e for e in export["entries"] if e["native_id"] == "9002-8002")
    assert association["interests"][0]["references"] == []  # a bill named without a number is no reference
