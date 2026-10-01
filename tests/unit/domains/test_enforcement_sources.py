"""SEC, FCA, EPA ECHO and EDPB enforcement acquisition through the real adapter (#2651, EN01, EN03-EN06)."""

from __future__ import annotations

import pytest

from src.ingestion.enforcement_sources import (
    BOUNDED_COVERAGE,
    DECLINED,
    LIVE_VERIFICATION,
    MINIMISATION,
    PROVIDER_CONTRACTS,
    EnforcementAdapter,
    EnforcementFormatError,
    fixture_transport,
    money,
    pdf_text,
    redact,
    sentences,
)
from src.ingestion.source_packs import SourcePackError
from src.kb.enforcement import EnforcementStore
from tests.unit import enforcement_harness as h


def records(source_id, **kwargs):
    pages = h.fetch(source_id, **kwargs)
    return [r["enforcement_record"] for page in pages for r in page.records], [page.receipt for page in pages]


def by_kind(items, kind):
    return [r for r in items if r.get("kind") == kind]


def test_audit_is_machine_readable_and_every_source_is_unverified_live():
    assert set(PROVIDER_CONTRACTS) == set(LIVE_VERIFICATION) == {"us-sec", "uk-fca", "us-epa-echo", "edpb"}
    assert all(v["status"] == "unverified-live" for v in LIVE_VERIFICATION.values())
    for contract in PROVIDER_CONTRACTS.values():
        assert {"endpoints", "authentication", "rate_limits", "revisions", "licence"} <= set(contract)
    assert "fca-individual-notices" in DECLINED and "per_run" in BOUNDED_COVERAGE
    assert {"natural_persons", "retention", "who_may_query"} <= set(MINIMISATION)
    for source_id in h.SOURCES:
        assert h.source(source_id)["enforcement"]["live_verification"] == "unverified-live"


def test_sec_litigation_release_keeps_admission_wording_sanctions_and_the_court_case():
    items, receipts = records("sec-litigation-releases")
    (action,) = by_kind(items, "enforcement_action")
    assert action["record_key"] == h.SEC_LR and action["native_id"] == "LR-99901"
    assert action["admission_wording"] == "Without admitting or denying the allegations in the complaint"
    assert action["settled"] is True and action["initiated_on"] == "2099-02-27"
    assert action["court_cases"][0]["docket_number"] == "1:99-cv-00901"
    assert "Section 17(a) of the Securities Act of 1933" in action["legal_bases"]
    organisation, person = by_kind(items, "respondent")
    assert organisation["identifiers"] == [{"scheme": "sec-cik", "value": "0009999101",
                                            "type_as_published": "CIK No."}]
    assert person == {**person, "party_type": "natural_person", "pseudonym": "natural person 2"}
    assert "name_as_published" not in person
    text = str(items)
    assert "Jordan" not in text and "[natural person 2]" in action["outcome_as_published"]
    penalties = {p["penalty_type"]: p for p in by_kind(items, "penalty")}
    assert {k: (p["amount"], p["currency"]) for k, p in penalties.items()} == {
        "civil_penalty": ("2500000", "USD"), "disgorgement": ("1200000", "USD"),
        "prejudgment_interest": ("85000", "USD")}
    assert receipts[0]["withheld"]["natural_person_penalties"] == 1
    documents = by_kind(items, "notice_document")
    assert any(d["content_sha256"] for d in documents) and any(d["content_sha256"] is None for d in documents)
    assert receipts[0]["requests"][0]["path"] == "/enforcement-litigation/litigation-releases/lr-99901"


def test_sec_administrative_proceeding_is_keyed_by_release_and_file_number():
    items, _ = records("sec-administrative-proceedings")
    (action,) = by_kind(items, "enforcement_action")
    assert {(i["scheme"], i["value"]) for i in action["identifiers"]} == {("sec-release", "34-99902"),
                                                                         ("sec-file-number", "3-99902")}
    assert action["admission_wording"] == "without admitting or denying the findings herein"
    (decision,) = by_kind(items, "decision")
    assert decision["decision_type_as_published"] == "Order Instituting Cease-and-Desist Proceedings"
    assert by_kind(items, "penalty")[0]["amount_as_published"] == "$750,000"


def test_fca_final_notices_keep_frn_discount_stages_appeals_and_withhold_individuals():
    items, receipts = records("fca-final-notices")
    actions = {a["native_id"]: a for a in by_kind(items, "enforcement_action")}
    assert set(actions) == {"exampla-uk-limited-2099", "northwind-payments-limited-2099"}
    assert receipts[2]["withheld"]["individual_only_actions"] == 1 and receipts[2]["records"] == 0
    assert "Jordan" not in str(items) and "JXE01001" not in str(items)
    respondent = next(r for r in by_kind(items, "respondent") if r["action_key"] == h.FCA_EXAMPLA)
    assert respondent["identifiers"][0] == {"scheme": "fca-frn", "value": "999002",
                                            "type_as_published": "Firm Reference Number"}
    stages = {p["stage"]: p for p in by_kind(items, "penalty") if p["action_key"] == h.FCA_EXAMPLA}
    assert stages["after_settlement_discount"]["amount"] == "1400000"
    assert stages["before_settlement_discount"]["amount"] == "2000000"
    assert stages["after_settlement_discount"]["discount_as_published"] == "30% (stage 1) discount"
    assert {"Principle 3", "SYSC 6.1.1R", "Financial Services and Markets Act 2000"} <= set(
        actions["exampla-uk-limited-2099"]["legal_bases"])
    (appeal,) = by_kind(items, "appeal")
    assert appeal["reference"] == "FS/2099/0007" and appeal["stated_on"] == "2099-06-02"
    assert "Upper Tribunal dismissed the reference" in appeal["status_as_published"]
    assert actions["northwind-payments-limited-2099"]["settled"] is None


def test_epa_echo_case_keeps_facilities_and_separate_published_penalty_fields():
    items, _ = records("epa-echo-cases")
    (action,) = by_kind(items, "enforcement_action")
    assert action["native_id"] == "04-2099-0101" and action["authority"] == "us-epa"
    assert action["facilities"][0]["frs_registry_id"] == "110099990001"
    assert action["facilities"][0]["latitude_as_published"] == "33.7490"
    assert action["legal_bases"] == ["CAA 112(r)(1)", "EPCRA 312"]
    penalties = {p["penalty_type"]: p for p in by_kind(items, "penalty")}
    assert set(penalties) == {"federal_penalty", "state_local_penalty", "sep_cost", "cost_recovery",
                              "compliance_action_cost"}
    assert penalties["cost_recovery"]["status"] == "not_published" and penalties["cost_recovery"]["amount"] is None
    assert penalties["state_local_penalty"]["amount"] == "0"


def test_edpb_register_entries_keep_authorities_measures_and_unpublished_fines():
    items, _ = records("edpb-art60-decisions")
    actions = {a["native_id"]: a for a in by_kind(items, "enforcement_action")}
    assert actions["99901"]["authority"] == "eu-sa-ie"
    assert actions["99901"]["concerned_authorities"] == ["eu-sa-de", "eu-sa-fr"]
    decision = next(d for d in by_kind(items, "decision") if d["action_key"] == h.EDPB_IE)
    assert decision["corrective_measures"] == ["Administrative fine", "Compliance order"]
    fines = {p["action_key"]: p for p in by_kind(items, "penalty")}
    assert (fines[h.EDPB_IE]["amount"], fines[h.EDPB_IE]["currency"]) == ("250000", "EUR")
    assert fines[h.EDPB_NL]["status"] == "not_published"
    assert not [r for r in by_kind(items, "respondent") if r["action_key"] == h.EDPB_NL]


def test_a_withdrawn_notice_becomes_a_removal_revision_never_a_deletion():
    conn = h.connection()
    h.apply(conn, "edpb-art60-decisions")
    results = h.apply(conn, "edpb-art60-decisions", v2=True)
    assert sum(r["counts"]["removed_by_source"] for r in results) == 1
    store = EnforcementStore(conn)
    history = store.history(h.NS, h.EDPB_NL)
    assert [v["record"]["publication_status"] for v in history] == ["published", "removed_by_source"]
    assert store.action_children(h.NS, h.EDPB_NL)["decision"]


def test_replays_are_idempotent_and_corrections_are_revisions():
    conn = h.connection()
    h.load_all(conn)
    again = h.apply(conn, "fca-final-notices", run_id="again")
    assert sum(r["counts"]["inserted"] + r["counts"]["revised"] for r in again) == 0
    corrected = h.apply(conn, "fca-final-notices", v2=True)
    assert sum(r["counts"]["revised"] for r in corrected) >= 2
    history = EnforcementStore(conn).history(h.NS, h.FCA_EXAMPLA)
    assert history[-1]["record"]["publication_status"] == "corrected"
    assert "SYSC 4.1.1R" in history[-1]["record"]["legal_bases"]
    assert "SYSC 4.1.1R" not in history[0]["record"]["legal_bases"]


def test_failures_and_host_policy_are_classified():
    source = h.source("sec-litigation-releases")
    pages = h.native_pages("sec-litigation-releases")
    for status, code in ((503, "source_unavailable"), (403, "authentication_failed"), (429, "rate_limited"),
                         (400, "schema_drift")):
        broken = [{**pages[0], "status": status}]
        with pytest.raises(SourcePackError) as caught:
            h.fetch("sec-litigation-releases", pages=broken, item=source)
        assert caught.value.code == code
    moved = [{**pages[0], "final_url": "https://evil.example/x"}]
    with pytest.raises(SourcePackError) as caught:
        h.fetch("sec-litigation-releases", pages=moved)
    assert caught.value.code == "network_policy"
    drift = [{**pages[0], "body": "<html><body>nothing</body></html>"}]
    with pytest.raises(SourcePackError) as caught:
        h.fetch("sec-litigation-releases", pages=drift)
    assert caught.value.code == "schema_drift"


def test_declarations_are_bounded_and_the_sec_user_agent_is_required():
    source = h.source("sec-litigation-releases")
    source["enforcement"].pop("user_agent")
    with pytest.raises(SourcePackError):
        EnforcementAdapter(source, transport=fixture_transport([]))
    bad = h.source("fca-final-notices")
    bad["enforcement"]["selection"] = {"notices": [{"slug": "../etc"}]}
    with pytest.raises(SourcePackError):
        EnforcementAdapter(bad, transport=fixture_transport([]))
    adapter = EnforcementAdapter(h.source("sec-litigation-releases"),
                                 transport=fixture_transport(h.native_pages("sec-litigation-releases")))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "records", "parameters": {"q": "x"}}, cursor=None)
    assert caught.value.code == "parameter_forbidden"


def test_text_helpers_quote_exactly():
    assert money("a civil penalty of $1.2 million") == ("1200000", "USD")
    assert money("EUR 250 000") == ("250000", "EUR")
    assert money("no figure") == (None, None)
    assert sentences("SEC v. Fixture Co., Civil Action No. 1:99 (S.D.N.Y.). Next sentence.")[1] == "Next sentence."
    assert redact("Jordan Example consented; Example paid.", {"Jordan Example": "natural person 2"}) == \
        "[natural person 2] consented; [natural person 2] paid."
    assert redact("Example Data Co. paid", {"Jordan Example": "natural person 1"},
                  ["Example Data Co."]) == "Example Data Co. paid"
    with pytest.raises(EnforcementFormatError):
        pdf_text(b"not a pdf")
    with pytest.raises(EnforcementFormatError):
        pdf_text(b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\n%%EOF")
