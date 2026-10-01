"""UN Treaty Collection, CELLAR and Council of Europe acquisition (#2595, #2599, #2605) and the TR01 contracts (#2586)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.source_packs import (
    SUPPORTED_CONNECTORS,
    SourcePackConformance,
    SourcePackError,
)
from src.ingestion.treaties_sources import (
    BOUNDED_COVERAGE,
    FORMATS,
    LICENCE_DECISIONS,
    LIVE_VERIFICATION,
    MINIMISATION,
    PROVIDER_CONTRACTS,
    TreatiesAdapter,
    citations_in,
    fixture_transport,
    parse_date,
)
from tests.unit import treaties_harness as h


def by_key(records):
    return {r["record_key"]: r for r in records}


def test_contracts_licence_decisions_minimisation_and_bounded_coverage_are_declared():
    assert set(PROVIDER_CONTRACTS) == {"untc", "cellar", "coe-treaty-office"} == set(LICENCE_DECISIONS)
    assert LIVE_VERIFICATION["untc"]["status"] == "declined"
    assert {LIVE_VERIFICATION[p]["status"] for p in ("cellar", "coe-treaty-office")} == {"unverified-live"}
    for contract in PROVIDER_CONTRACTS.values():
        assert {"authentication", "rate_limits", "licence", "attribution", "revisions", "endpoints"} <= set(contract)
    assert "treaty full texts (linked, not reproduced)" in MINIMISATION["never_stored"]
    assert {"untc", "cellar", "coe-treaty-office", "periods"} == set(BOUNDED_COVERAGE)
    audit = (h.ROOT / "docs/development/treaties-evidence/source-audit.md").read_text()
    for needed in ("## Minimisation decision", "LIVE_VERIFICATION", "UN Treaty Collection licence decision",
                   "read 2026-09-30", "_verify_"):
        assert needed in audit
    value = h.manifest()
    ours = [s for s in value["sources"] if s["connector"] == "treaties"]
    assert {s["source_id"] for s in ours} == {h.UNTC, h.CELLAR, h.COE} and "treaties" in SUPPORTED_CONNECTORS
    assert {s["treaties"]["format"] for s in ours} == set(FORMATS)
    assert {s["source_id"]: s["treaties"]["live_verification"] for s in ours} == {
        h.UNTC: "declined", h.CELLAR: "unverified-live", h.COE: "unverified-live"}
    result = SourcePackConformance(h.ROOT).offline(value)
    assert result["valid"]
    counts = {s["source_id"]: s["records"] for s in result["sources"]}
    assert counts[h.UNTC] == 0 and counts[h.CELLAR] == 1 and counts[h.COE] == 12


def test_the_declined_untc_entry_makes_no_request():
    calls = []
    adapter = TreatiesAdapter(h.source(h.UNTC), transport=lambda **kw: calls.append(kw))
    with pytest.raises(SourcePackError) as error:
        adapter.fetch_page({"operation": "records", "parameters": {}}, cursor=None)
    assert error.value.code == "licence_declined" and calls == []


def test_untc_status_page_keeps_identifiers_actions_notes_and_statements_verbatim():
    records = by_key(h.records(h.UNTC))
    treaty = records[h.UNTC_TREATY]["fields"]
    assert {(i["scheme"], i["value"]) for i in treaty["identifiers"]} == {
        ("untc-mtdsg", "XXIX-99"), ("untc-chapter", "29"), ("unts-registration", "99001")}
    assert treaty["adoption"] == {"place": "Geneva", "date": "2098-03-01", "date_as_published": "1 March 2098"}
    assert treaty["entry_into_force"]["as_published"] == "1 January 2099, in accordance with article 20."
    assert treaty["text_policy"] == "linked, not stored" and records[h.UNTC_TREATY]["depositary_date"] == "2099-09-30"
    approval = records["treaties:action:untc:XXIX-99:european-union:approval:table"]["fields"]
    assert approval["action_date"] == "2098-11-15" and approval["action_type_as_published"] == "Approval (AA)"
    assert approval["participant"]["kind"] == "regional-economic-integration-organisation"
    assert approval["footnotes"][0]["text"].startswith("The instrument of approval of the European Union")
    accession = records["treaties:action:untc:XXIX-99:examplestan:accession:table"]["fields"]
    assert accession["action_type"] == "accession" and accession["date_as_published"] == "2 Jan 2099 a"
    reservation = records["treaties:action:untc:XXIX-99:examplestan:reservation:undated:1"]["fields"]
    assert reservation["text"] == ("Reservation: Examplestan does not consider itself bound by article 12, "
                                   "paragraph 2, of the Convention.")
    assert reservation["action_date"] is None and reservation["section_note"].startswith("(Unless otherwise")
    assert reservation["text_anchor"]["id"] == "res-examplestan-1"
    objection = records["treaties:action:untc:XXIX-99:germany:objection:2099-03-15:1"]["fields"]
    assert objection["objected"]["action_key"] == "treaties:action:untc:XXIX-99:examplestan:reservation:undated:1"
    assert "objects to it" in objection["text"]


def test_untc_revisions_between_runs_carry_the_depositary_stamp():
    later = by_key(h.records(h.UNTC, v2=True))
    assert later[h.UNTC_TREATY]["depositary_date"] == "2100-04-15"
    assert later["treaties:action:untc:XXIX-99:germany:ratification:table"]["fields"]["action_date"] == "2098-09-04"
    assert "treaties:action:untc:XXIX-99:examplonia:signature:table" not in later
    withdrawal = later["treaties:action:untc:XXIX-99:examplestan:withdrawal:2100-02-01:1"]["fields"]
    assert withdrawal["withdraws"].startswith("a statement published in this section")


def test_cellar_agreement_reuses_the_legal_adapter_and_groups_expressions():
    (record,) = h.records(h.CELLAR)
    fields = record["fields"]
    assert record["record_key"] == h.CELLAR_TREATY
    assert [e["language"] for e in fields["expressions"]] == ["ENG", "FRA"]
    assert {i["scheme"] for i in fields["identifiers"]} == {"celex", "cellar-work", "eli"}
    assert fields["signature"]["date"] == "2098-09-05" and fields["entry_into_force"]["dates"] == ["2099-06-01"]
    assert fields["conclusion"]["date"] is None and "no conclusion date property" in fields["conclusion"]["basis"]
    assert [a["celex"] for a in fields["eu_acts"]] == ["32098D0901", "32099D0042"]
    assert all("role is not inferred" in a["basis"] for a in fields["eu_acts"])
    assert fields["cross_references"] == [{"scheme": "cets", "value": "990", "as_written": "CETS No. 990",
                                           "basis": "citation in the published title (ENG expression)"}]
    receipt = h.fetch(h.CELLAR)[0].receipt
    assert [r["name"] for r in receipt["requests"]] == ["expressions:0", "agreement"]
    assert receipt["evidence_origin"] == "fixture"
    (later,) = h.records(h.CELLAR, v2=True)
    assert [a["celex"] for a in later["fields"]["eu_acts"]][-1] == "32100R0007"


def test_council_of_europe_chart_and_declarations():
    records = by_key(h.records(h.COE))
    treaty = records[h.COE_TREATY]["fields"]
    assert treaty["identifiers"] == [{"scheme": "cets", "value": "990"}]
    assert treaty["entry_into_force"]["conditions_as_published"] == "3 Ratifications including 2 member States."
    ratification = records["treaties:action:coe:990:france:ratification:chart"]["fields"]
    assert (ratification["action_date"], ratification["effective_date"]) == ("2098-11-15", "2099-03-01")
    accession = records["treaties:action:coe:990:examplestan:accession:chart"]["fields"]
    assert accession["action_type"] == "accession" and accession["participant"]["kind"] == "state"
    reservation = records["treaties:action:coe:990:france:reservation:2098-11-15:1"]["fields"]
    assert reservation["text"].startswith("In accordance with article 25 of the Convention, France reserves")
    assert reservation["articles_as_published"] == "12" and reservation["effective_date"] == "2099-03-01"
    later = by_key(h.records(h.COE, v2=True))
    denunciation = later["treaties:action:coe:990:germany:denunciation:2100-01-10:1"]["fields"]
    assert (denunciation["deposit_date"], denunciation["effective_date"]) == ("2100-01-10", "2100-05-01")
    withdrawal = later["treaties:action:coe:990:france:withdrawal:2100-02-01:1"]["fields"]
    assert withdrawal["withdraws"] == "reservation"


def test_bounds_host_policy_and_drift_fail_the_unit():
    item = h.source(h.COE)
    item["treaties"]["selection"] = {"treaties": [{"number": str(n)} for n in range(1, 22)]}
    with pytest.raises(SourcePackError):
        TreatiesAdapter(item)
    pages = h.native_pages(h.COE)
    pages[0]["final_url"] = "https://elsewhere.example/chart"
    with pytest.raises(SourcePackError) as error:
        TreatiesAdapter(h.source(h.COE), transport=fixture_transport(pages)).fetch_page(
            {"operation": "records", "parameters": {}}, cursor=None)
    assert error.value.code == "network_policy"
    pages = h.native_pages(h.COE)
    pages[0]["body"] = pages[0]["body"].replace("CETS No. 990", "CETS No. 991")
    with pytest.raises(SourcePackError) as error:
        TreatiesAdapter(h.source(h.COE), transport=fixture_transport(pages)).fetch_page(
            {"operation": "records", "parameters": {}}, cursor=None)
    assert error.value.code == "schema_drift"


def test_dates_and_citations_are_parsed_exactly():
    assert parse_date("1 Mar 2098") == "2098-03-01" and parse_date("05/09/2098") == "2098-09-05"
    assert parse_date("Status as at : 30-09-2099 05:00:44 EDT") == "2099-09-30" and parse_date("pending") is None
    assert citations_in("Convention (CETS No. 990) and UNTS No. 99001") == [
        {"scheme": "cets", "value": "990", "as_written": "CETS No. 990"},
        {"scheme": "unts-registration", "value": "99001", "as_written": "UNTS No. 99001"}]
    assert json.dumps(h.records(h.COE)).count("signatory") == 0
