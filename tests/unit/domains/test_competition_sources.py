"""Competition acquisition: EC case search, TAM, GOV.UK CMA, FTC and DOJ (#2217, CS01, CS03-CS06)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.competition_sources import (
    DECLINED,
    FORMATS,
    INSTRUMENTS,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    CompetitionAdapter,
    fixture_transport,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.kb.competition import CompetitionStore
from tests.unit import competition_harness as h


def records(source_id: str, **kwargs) -> list[dict]:
    return [item["competition_record"] for page in h.fetch(source_id, **kwargs) for item in page.records]


def test_audit_decisions_are_machine_readable_and_every_source_is_unverified_live():
    assert set(PROVIDER_CONTRACTS) == {spec["provider"] for spec in FORMATS.values()}
    for provider, contract in PROVIDER_CONTRACTS.items():
        assert {"access", "licence", "rate_limits", "revisions", "attribution"} <= set(contract)
        assert LIVE_VERIFICATION[provider]["status"] == "unverified-live"
        assert LIVE_VERIFICATION[provider]["intended"] == "verified-live"
    assert {"hsr-early-termination", "doj-criminal", "court-dockets"} <= set(DECLINED)
    assert "Criminal" in INSTRUMENTS["us-doj"]["excluded"]
    sources = [s for s in h.manifest()["sources"] if s["connector"] == "competition"]
    assert {s["source_id"] for s in sources} == set(h.SOURCES)
    assert all(s["competition"]["live_verification"] == "unverified-live" for s in sources)
    assert h.manifest()["version"] == "1.1.0"


def test_pinned_fixtures_replay_deterministically():
    result = SourcePackConformance(h.ROOT).offline(json.loads(h.PACK.read_text()))
    competition = [s for s in result["sources"] if s["connector"] == "competition"]
    assert len(competition) == 5 and all(s["valid"] for s in competition)


def test_ec_cases_keyed_by_case_number_with_stages_parties_and_linked_documents():
    items = records("ec-competition-cases")
    by_kind: dict[str, list] = {}
    for item in items:
        by_kind.setdefault(item["kind"], []).append(item)
    cases = {c["record_key"]: c for c in by_kind["competition_case"]}
    assert set(cases) == {h.MERGER, h.AID_CASE}
    assert cases[h.MERGER]["instrument"] == "merger" and cases[h.AID_CASE]["instrument"] == "state_aid"
    stages = [s for s in by_kind["case_stage"] if s["case_key"] == h.MERGER]
    assert [s["stage_as_published"] for s in stages][0] == "Notification"
    assert "Statement of Objections" in {s["stage_as_published"] for s in stages}
    parties = {p["name_as_published"]: p for p in by_kind["case_party"]}
    assert parties["Exampla Holdings plc"]["role"] == "notifying_party"
    assert parties["Exampla Holdings plc"]["role_as_published"] == "Notifying party"
    assert parties["Northwind Widgets GmbH"]["country"] == "DE"
    document = next(d for d in by_kind["decision_document"] if d["case_key"] == h.AID_CASE)
    assert document["citation"]["oj_reference"] == "OJ C 150, 25.4.2025, p. 3"
    assert "Commission Regulation (EU) No 651/2014" in document["legal_references"]
    assert all(r["source"]["evidence_origin"] == "fixture" for r in items)


def test_changed_case_page_is_a_new_revision_and_stages_are_append_only():
    conn = h.connection()
    h.apply(conn, "ec-competition-cases")
    store = CompetitionStore(conn)
    before = {v["record"]["record_key"] for v in store.views(h.NS, ("case_stage",))}
    h.apply(conn, "ec-competition-cases", v2=True)
    history = store.history(h.NS, h.MERGER)
    assert [v["record"]["state_as_published"] for v in history] == ["Open", "Closed"]
    assert history[1]["record"]["court_dockets"][0]["docket_number"] == "T-999/25"
    after = {v["record"]["record_key"] for v in store.views(h.NS, ("case_stage",))}
    assert before < after  # the final decision is added; nothing earlier is removed
    final = store.case_children(h.NS, h.MERGER)["decision_document"]
    assert any(d["record"]["citation"].get("celex") == "32025M99001" for d in final)
    # A replay of an unchanged page adds nothing.
    assert h.apply(conn, "ec-competition-cases", v2=True, run_id="again")[0]["counts"]["inserted"] == 0


def test_tam_awards_keep_beneficiary_amounts_and_measure_as_published_and_revise_on_correction():
    conn = h.connection()
    h.apply(conn, "eu-state-aid-tam")
    store = CompetitionStore(conn)
    awards = {v["record"]["record_key"]: v["record"] for v in store.views(h.NS, ("state_aid_award",))}
    assert awards[h.AWARD_INT]["amount_as_published"] == "12500000.00"
    assert awards[h.AWARD_INT]["beneficiary_identifiers"] == [
        {"scheme": "nl-kvk", "type_as_published": "KVK", "value": "99990003"}]
    assert awards[h.AWARD_NW]["amount_as_published"] is None
    assert awards[h.AWARD_NW]["amount_range_as_published"] == "0.5-1 million"
    assert awards[h.AWARD_OTHER]["sa_number"] == "SA.99003"
    h.apply(conn, "eu-state-aid-tam", v2=True)
    corrected = store.history(h.NS, h.AWARD_INT)
    assert [v["record"]["amount_as_published"] for v in corrected] == ["12500000.00", "12000000.00"]
    assert store.history(h.NS, h.AWARD_NW)[-1]["record"]["status"] == "withdrawn"
    assert store.history(h.NS, h.AWARD_NW)[0]["record"]["status"] == "published"


def test_tam_result_longer_than_one_page_is_never_truncated():
    body = json.dumps({"totalElements": 250, "last": False, "content": []})
    request = "/competition/transparency/public/api/awards?countryCode=NL&page=0&saNumber=SA.99002&size=100"
    with pytest.raises(SourcePackError) as exc:
        h.fetch("eu-state-aid-tam", bodies={request: body})
    assert exc.value.code == "budget_exhausted"


def test_cma_case_stages_cite_the_page_revision_and_updates_add_revisions():
    items = records("uk-cma-cases")
    case = next(i for i in items if i["kind"] == "competition_case")
    assert case["instrument"] == "merger" and case["state_as_published"] == "open"
    assert case["related_case_numbers"] == ["M.99001", "ME/9999/25"]
    assert case["legal_references"] == ["Enterprise Act 2002"]
    stages = [i for i in items if i["kind"] == "case_stage"]
    assert all(s["page_revision"] for s in stages)
    assert {p["role_as_published"] for p in items if p["kind"] == "case_party"} == {"named in case title"}
    conn = h.connection()
    h.apply(conn, "uk-cma-cases")
    h.apply(conn, "uk-cma-cases", v2=True)
    history = CompetitionStore(conn).history(h.NS, h.CMA)
    assert [v["record"]["page_revision"] for v in history] == ["2025-04-17T09:00:00Z", "2025-06-30T09:00:00Z"]


def test_ftc_and_doj_actions_keep_organisations_and_dockets_as_citations_only():
    ftc = records("us-ftc-cases")
    case = next(i for i in ftc if i["kind"] == "competition_case")
    assert case["record_key"] == h.FTC and case["instrument"] == "merger"
    assert case["court_dockets"][0]["docket_number"] == "C-4999"
    assert "Section 7 of the Clayton Act" in case["legal_references"]
    names = {i["name_as_published"] for i in ftc if i["kind"] == "case_party"}
    assert names == {"Exampla Holdings plc", "Northwind Widgets, Inc."}  # the natural person is not recorded
    assert [s["stage_as_published"] for s in ftc if s["kind"] == "case_stage"] == [
        "Complaint", "Agreement Containing Consent Orders", "Decision and Order"]
    doj = records("us-doj-atr-cases")
    case = next(i for i in doj if i["kind"] == "competition_case")
    assert case["instrument"] == "antitrust" and case["opened_on"] == "2025-03-03"
    assert case["court_dockets"][0]["docket_number"] == "1:25-cv-09999"
    assert all(d["url"].startswith("https://www.justice.gov/") for d in doj if d["kind"] == "decision_document")


def test_declined_instruments_and_foreign_hosts_are_refused():
    page = "/atr/case/us-v-northwind-widgets"
    body = (h.FIXTURES / "doj_us-v-northwind-widgets.html").read_text().replace("Civil Non-Merger", "Criminal")
    with pytest.raises(SourcePackError) as exc:
        h.fetch("us-doj-atr-cases", bodies={page: body})
    assert exc.value.code == "schema_drift" and "declined" in str(exc.value)
    pages = h.native_pages("us-doj-atr-cases")
    pages[0]["final_url"] = "https://elsewhere.example/atr/case/us-v-northwind-widgets"
    adapter = CompetitionAdapter(h.source("us-doj-atr-cases"), transport=fixture_transport(pages))
    with pytest.raises(SourcePackError) as exc:
        adapter.fetch_page({"operation": "competition", "parameters": {}}, cursor=None)
    assert exc.value.code == "network_policy"
    item = h.source("ec-competition-cases")
    item["competition"]["selection"] = {"cases": [{"case_number": "FS.100"}]}
    with pytest.raises(SourcePackError):
        CompetitionAdapter(item, transport=fixture_transport([]))


def test_receipts_name_every_request_and_the_runtime_projector_is_registered():
    from src.ingestion.source_pack_runtime import PROJECTORS

    assert "noesis-competition-record-v1" in PROJECTORS
    conn = h.connection()
    h.apply(conn, "us-ftc-cases", run_id="run-ftc")
    receipts = CompetitionStore(conn).receipts(h.NS, "run-ftc", scopes=h.SCOPES)
    assert receipts and receipts[0]["requests"][0]["path"].startswith("/legal-library/browse/cases-proceedings/")
    assert receipts[0]["evidence_origin"] == "fixture"
