"""Campaign-finance source audit, declared sources and acquisition through the real adapter (#2473, #2486-#2499)."""

from __future__ import annotations

import json

import pytest

from src.ingestion import campaign_finance_sources as cf
from src.ingestion.source_packs import SourcePackError, _digest, replay_native_fixture
from tests.unit import campaign_finance_harness as h

PERSONAL = ("PLACEHOLDER, PAT", "Pat Placeholder", "MOCK, JORDAN", "JORDAN", "SAMPLE, JO", "FICTIVE, JO",
            "99002", "99003", "99004", "99005", "EX2 2BB", "77002", "FICTIONAL EMPLOYER", "FICTIONAL OCCUPATION",
            "SELF-EMPLOYED", "SPOUSE", "PAYROLL", "2 SAMPLE ROAD", "3 MOCK LANE", "SAMPLE, TREASURER",
            "PLACEHOLDER, CHRIS", "MOCK, DANA", "1 EXAMPLE WAY")


def fetch_all(source_id: str, version: str = "v1") -> tuple[list[dict], list[dict]]:
    adapter = h.adapter(source_id, version)
    records, receipts, cursor = [], [], None
    for _ in adapter.units:
        page = adapter.fetch_page({"operation": "selection", "parameters": {}, "limit": 500}, cursor=cursor)
        records += [r["campaign_finance_record"] for r in page.records]
        receipts.append(page.receipt)
        cursor = page.next_cursor
    return records, receipts


def by_key(records):
    return {r["record_key"]: r for r in records}


def test_the_audit_records_contracts_the_reuse_restriction_and_the_minimisation_decision():
    audit = (h.ROOT / "docs/development/campaign-finance-evidence/source-audit.md").read_text()
    for source_id in h.SOURCES:
        assert f"`{source_id}`" in audit
    for needed in ("30111(a)(4)", "X-Api-Key", "amendment_chain", "most_recent", "ECRef", "Individual",
                   "individual-items:read", "Retention", "Bounded first coverage", "documented-not-acquired"):
        assert needed in audit, needed
    assert set(cf.PROVIDER_CONTRACTS) == {"openfec", "fec-bulk", "uk-electoral-commission"}
    for contract in cf.PROVIDER_CONTRACTS.values():
        assert {"endpoints", "authentication", "rate_limits", "revisions", "licence", "access_decision"} <= set(contract)
    assert cf.PROVIDER_CONTRACTS["fec-bulk"]["access_decision"] == "documented-not-acquired"
    assert all(v["status"] != "verified-live" for v in cf.LIVE_VERIFICATION.values())
    assert "name" in cf.MINIMISATION["never_stored_for_individuals"]
    assert "never matched" in cf.MINIMISATION["matching"] or "never" in cf.MINIMISATION["matching"]


def test_the_pack_declares_every_source_bounded_minimised_and_replaying_its_pinned_output():
    manifest = h.manifest()
    assert manifest["version"] == "1.4.0"
    ours = {s["source_id"]: s for s in manifest["sources"] if s["connector"] == "campaign-finance"}
    assert set(ours) == set(h.SOURCES)
    for source_id, item in ours.items():
        declared = item["campaign_finance"]
        assert declared["live_verification"] == "unverified-live"
        assert declared["minimisation"] == cf.MINIMISATION_POLICY
        assert declared["format"] == h.FORMATS[source_id]
        keyed = cf.FORMATS[declared["format"]]["keyed"]
        assert (item["auth"] == {"kind": "required-secret", "secret_ref": "NOESIS_OPENFEC_API_KEY"}) is keyed
        fixture = json.loads((h.ROOT / item["fixture"]["path"]).read_text())
        assert fixture == json.loads(json.dumps(h.source_pack_fixture(source_id)))  # generated from the harness
        assert _digest(list(replay_native_fixture(item, fixture))) == item["fixture"]["expected_output_hash"]


def test_committee_registrations_are_dated_revisions_without_personal_fields():
    records, receipts = fetch_all("us-fec-committees")
    campaign = [r for r in records if r["record_key"] == "campaign-finance:fec:committee:C00999901"]
    assert [(r["fields"]["cycle"], r["fields"]["designation"]) for r in campaign] == [(2100, "P"), (2098, "A")]
    assert campaign[0]["effective_on"] == "2099-01-10" and campaign[0]["revision_order"] > campaign[1]["revision_order"]
    pac = by_key(records)["campaign-finance:fec:committee:C00999902"]
    assert pac["fields"]["affiliated_committee_name"] == "EXAMPLE INDUSTRIES INC"
    assert pac["fields"]["withheld_as_personal"] == ["treasurer_name"]
    assert all(r["requests"][0]["path"].startswith("/committee/") for r in receipts)
    text = json.dumps([records, receipts])
    assert not [p for p in PERSONAL if p in text]


def test_filing_versions_keep_their_amendment_chain_and_published_flags():
    records, _ = fetch_all("us-fec-filings")
    found = by_key(records)
    original = found["campaign-finance:fec:filing:1500101"]
    amended = found["campaign-finance:fec:filing:1500150"]
    assert original["fields"]["amendment_indicator"] == "N" and original["fields"]["most_recent"] is False
    assert amended["fields"]["amendment_chain"] == [1500101, 1500150] and amended["fields"]["most_recent"] is True
    assert original["filing_group"] == amended["filing_group"] == "campaign-finance:fec:filing-group:1500101"
    assert amended["fields"]["totals_as_reported"]["total_receipts"] == 107400.0
    assert original["fields"]["totals_as_reported"]["total_receipts"] == 107500.0  # never overwritten
    termination = found["campaign-finance:fec:filing:1500390"]
    assert termination["fields"]["amendment_indicator"] == "T" and termination["fields"]["report_type"] == "TER"
    v2, _ = fetch_all("us-fec-filings", "v2")
    later = by_key(v2)
    assert later["campaign-finance:fec:filing:1500150"]["fields"]["most_recent"] is False  # as published
    assert later["campaign-finance:fec:filing:1500170"]["fields"]["amendment_chain"] == [1500101, 1500150, 1500170]


def test_itemised_receipts_minimise_individuals_and_keep_memo_and_amended_items_distinct():
    records, receipts = fetch_all("us-fec-schedule-a")
    found = by_key(records)
    pac = found["campaign-finance:fec:sa:1500101:4000001"]
    assert pac["fields"]["counterparty"] == {"kind": "organisation", "entity_type": "PAC",
                                             "name": "EXAMPLE INDUSTRIES INC POLITICAL ACTION COMMITTEE",
                                             "city": "EXAMPLETON", "state": "EX", "committee_id": "C00999902"}
    assert pac["filing_key"] == "campaign-finance:fec:filing:1500101"
    amended = found["campaign-finance:fec:sa:1500150:4000011"]
    assert amended["fields"]["amendment_indicator"] == "A" and amended["filing_key"].endswith(":1500150")
    person = found["campaign-finance:fec:sa:1500101:4000002"]
    assert person["fields"]["counterparty"] == {"kind": "natural-person", "entity_type": "IND", "name": None}
    assert person["minimisation"]["counterparty"] == "natural-person"
    assert {"contributor_name", "contributor_zip", "contributor_employer", "contributor_occupation",
            "contributor_aggregate_ytd", "memo_text"} <= set(person["minimisation"]["withheld"])
    assert person["fields"]["amount_as_reported"] == 2500.0 and person["fields"]["date_as_reported"] == "2099-03-05"
    memo = found["campaign-finance:fec:sa:1500150:4000013"]
    assert memo["fields"]["memo"] is True and memo["fields"]["memo_code"] == "X"
    assert sum(r["individual_items_minimised"] for r in receipts) == 3
    assert not [p for p in PERSONAL if p in json.dumps([records, receipts])]


def test_disbursements_and_independent_expenditures_as_reported():
    records, _ = fetch_all("us-fec-schedule-b")
    staff = by_key(records)["campaign-finance:fec:sb:1500101:4100002"]
    assert staff["fields"]["counterparty"]["kind"] == "natural-person"
    assert staff["fields"]["purpose_as_reported"] == "SALARY"
    records, _ = fetch_all("us-fec-schedule-e")
    found = by_key(records)
    notice = found["campaign-finance:fec:se:1500301:5000001"]
    periodic = found["campaign-finance:fec:se:1500310:5000011"]
    assert notice["fields"]["source_assertion"] == "24/48-hour notice"
    assert periodic["fields"]["source_assertion"] == "periodic report"
    assert notice["fields"]["transaction_id"] == periodic["fields"]["transaction_id"] == "SE.1"  # not deduplicated
    assert notice["fields"]["support_oppose_indicator"] == "S" and notice["fields"]["candidate_id"] == "P99000001"
    oppose = found["campaign-finance:fec:se:1500310:5000012"]
    assert oppose["fields"]["support_oppose_indicator"] == "O" and oppose["fields"]["candidate_name"] == "DEMO, SAM"
    assert oppose["fields"]["counterparty"]["name"] is None
    assert not [p for p in PERSONAL if p in json.dumps(records)]


def test_commission_donations_keep_status_and_company_numbers_and_minimise_individuals():
    records, receipts = fetch_all("uk-ec-donations")
    found = by_key(records)
    assert "campaign-finance:ukec:donation:C0990009" not in found  # another regulated entity: out of selection
    company = found["campaign-finance:ukec:donation:C0990001"]
    assert company["fields"]["counterparty"]["company_registration_number"] == "09990002"
    assert company["fields"]["amount_as_reported"] == "25000.00" and company["fields"]["accepted_date"] == "2099-02-15"
    assert company["filing_key"] == "campaign-finance:ukec:filing:donations:9901:q1-2099"
    person = found["campaign-finance:ukec:donation:C0990002"]
    assert person["fields"]["counterparty"] == {"kind": "natural-person", "status": "Individual", "name": None}
    assert set(person["minimisation"]["withheld"]) == {"DonorName", "Postcode", "DonorId"}
    union = found["campaign-finance:ukec:donation:C0990003"]
    assert union["fields"]["counterparty"]["status"] == "Trade Union" and union["fields"]["counterparty"]["name"]
    filing = found["campaign-finance:ukec:filing:donations:9901:q1-2099"]
    assert filing["fields"]["totals_as_reported"] is None and "no total is reported" in filing["fields"]["totals_note"]
    assert receipts[0]["requests"][0]["path"].startswith("/api/csv/Donations?")
    assert not [p for p in PERSONAL if p in json.dumps([records, receipts])]
    spending, _ = fetch_all("uk-ec-spending")
    item = by_key(spending)["campaign-finance:ukec:spending:RS0990001"]
    assert item["fields"]["election_name"] == "UK Parliamentary General Election 2099"
    assert item["fields"]["counterparty"]["name"] == "Example Print Ltd"


def test_keyset_pages_are_followed_and_a_longer_unit_is_never_truncated(monkeypatch):
    monkeypatch.setattr(cf, "PER_PAGE", 2)
    body = json.loads(h.fixture_body("fec_schedule_a_C00999901_2100.json"))
    rows = body["results"]
    first = {**body, "results": rows[:2], "pagination": {"count": 5, "per_page": 2, "last_indexes": {
        "last_index": "4000002", "last_contribution_receipt_date": "2099-03-05"}}}
    second = {**body, "results": rows[2:4], "pagination": {"count": 5, "per_page": 2, "last_indexes": {
        "last_index": "4000012", "last_contribution_receipt_date": "2099-03-05"}}}
    third = {**body, "results": rows[4:], "pagination": {"count": 5, "per_page": 2, "last_indexes": None}}
    base = "/v1/schedules/schedule_a/?committee_id=C00999901"

    def page(payload, extra=""):
        return {"request": f"{base}{extra}&per_page=2&sort=contribution_receipt_date&two_year_transaction_period=2100",
                "status": 200, "body": json.dumps(payload)}

    pages = [page(first), page(second, "&last_contribution_receipt_date=2099-03-05&last_index=4000002"),
             page(third, "&last_contribution_receipt_date=2099-03-05&last_index=4000012")]
    item = h.source("us-fec-schedule-a")
    item["campaign_finance"]["selection"] = {"committee_cycles": [{"committee_id": "C00999901", "cycle": 2100}]}
    adapter = cf.CampaignFinanceAdapter(item, transport=cf.fixture_transport(pages), secret=cf.FIXTURE_SECRET)
    fetched = adapter.fetch_page({"operation": "selection", "parameters": {}, "limit": 500}, cursor=None)
    assert len(fetched.records) == 5 and len(fetched.receipt["requests"]) == 3
    monkeypatch.setattr(cf, "MAX_PAGES_PER_UNIT", 2)
    with pytest.raises(SourcePackError) as refused:
        cf.CampaignFinanceAdapter(item, transport=cf.fixture_transport(pages), secret=cf.FIXTURE_SECRET).fetch_page(
            {"operation": "selection", "parameters": {}, "limit": 500}, cursor=None)
    assert refused.value.code == "budget_exhausted"


def test_the_api_key_travels_in_a_header_and_hosts_and_declarations_are_enforced():
    seen = {}
    pages = h.native_pages("us-fec-committees")

    def transport(**kwargs):
        seen.update(kwargs)
        return cf.fixture_transport(pages)(**kwargs)

    adapter = cf.CampaignFinanceAdapter(h.source("us-fec-committees"), transport=transport, secret="k-123")
    page = adapter.fetch_page({"operation": "selection", "parameters": {}, "limit": 500}, cursor=None)
    assert seen["headers"]["X-Api-Key"] == "k-123" and "api_key" not in seen["params"]
    assert "k-123" not in json.dumps(page.receipt) and "k-123" not in json.dumps([dict(r) for r in page.records])
    with pytest.raises(SourcePackError) as missing:
        cf.CampaignFinanceAdapter(h.source("us-fec-committees"), transport=transport).fetch_page(
            {"operation": "selection", "parameters": {}, "limit": 500}, cursor=None)
    assert missing.value.code == "authentication_failed"
    moved = [{**p, "final_url": "https://elsewhere.example/v1/x"} for p in pages]
    with pytest.raises(SourcePackError) as redirected:
        cf.CampaignFinanceAdapter(h.source("us-fec-committees"), transport=cf.fixture_transport(moved),
                                  secret="k").fetch_page({"operation": "selection", "parameters": {}, "limit": 500},
                                                         cursor=None)
    assert redirected.value.code == "network_policy"
    undeclared = h.source("uk-ec-donations")
    undeclared["campaign_finance"].pop("minimisation")
    with pytest.raises(SourcePackError):
        cf.CampaignFinanceAdapter(undeclared)
    wide = h.source("uk-ec-donations")
    wide["campaign_finance"]["selection"]["regulated_entities"][0]["to"] = "2101-01-01"
    with pytest.raises(SourcePackError):
        cf.CampaignFinanceAdapter(wide)


def test_a_commission_export_at_the_row_cap_is_refused_and_personal_fields_can_never_leave_the_parser():
    header = h.fixture_body("ukec_donations_9901_2099H1.csv").splitlines()[0]
    row = h.fixture_body("ukec_donations_9901_2099H1.csv").splitlines()[1]
    with pytest.raises(cf.CampaignFinanceFormatError) as capped:
        cf.parse_ec_donations("\n".join([header] + [row] * cf.UK_ROW_CAP).encode(), {"id": "9901"})
    assert capped.value.code == "input_limit"
    leaky = {"record_key": "x", "minimisation": {"counterparty": "natural-person"},
             "fields": {"counterparty": {"kind": "natural-person", "name": "Someone"}, "contributor_zip": "1"}}
    assert cf.minimisation_violations(leaky) == ["$.fields.contributor_zip", "$.fields.counterparty.name"]
    disguised = {"record_key": "y", "minimisation": {"counterparty": "organisation"},
                 "fields": {"counterparty": {"kind": "natural-person", "name": None}}}
    assert cf.minimisation_violations(disguised) == ["$.fields.counterparty.kind"]
