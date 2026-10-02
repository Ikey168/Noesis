"""Enforcement acquisition: SEC releases, FCA final notices, EPA ECHO cases, EDPB Art. 60 decisions (EN01, EN03-EN06)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.enforcement_sources import (
    DECLINED,
    FORMATS,
    LIVE_VERIFICATION,
    MINIMISATION,
    PROVIDER_CONTRACTS,
    SEC_USER_AGENT_ENV,
    EnforcementAdapter,
    fixture_transport,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.kb.enforcement import EnforcementStore
from tests.unit import enforcement_harness as h


def by_kind(items):
    out: dict[str, list] = {}
    for item in items:
        out.setdefault(item["kind"], []).append(item)
    return out


def test_audit_decisions_are_machine_readable_and_every_source_is_unverified_live():
    assert set(PROVIDER_CONTRACTS) == {spec["provider"] for spec in FORMATS.values()}
    for provider, contract in PROVIDER_CONTRACTS.items():
        assert {"access", "authentication", "licence", "rate_limits", "revisions", "personal_data"} <= set(contract)
        assert LIVE_VERIFICATION[provider] == {"status": "unverified-live", "intended": "verified-live",
                                               "note": LIVE_VERIFICATION[provider]["note"]}
    assert {"stored", "redacted", "excluded", "retention", "access", "matching"} <= set(MINIMISATION)
    assert {"fca-individual-notices", "fca-register-api"} <= set(DECLINED)
    sources = [s for s in h.manifest()["sources"] if s["connector"] == "enforcement"]
    assert {s["source_id"] for s in sources} == set(h.SOURCES)
    assert all(s["enforcement"]["live_verification"] == "unverified-live" for s in sources)
    assert all(s["auth"] == {"kind": "none"} for s in sources)
    assert h.manifest()["version"] == "1.5.0"


def test_pinned_fixtures_replay_deterministically():
    pack = json.loads(h.PACK.read_text())
    pack["sources"] = [s for s in pack["sources"] if s["connector"] == "enforcement"]  # stdlib-only replay
    result = SourcePackConformance(h.ROOT).offline(pack)
    enforcement = [s for s in result["sources"] if s["connector"] == "enforcement"]
    assert len(enforcement) == 4 and all(s["valid"] for s in enforcement)


def test_sec_releases_keyed_by_release_number_with_settlement_wording_and_court_cases():
    items = by_kind(h.records("sec-enforcement-releases"))
    actions = {a["record_key"]: a for a in items["enforcement_action"]}
    assert set(actions) == {h.SEC_LR, h.SEC_AP}
    lr = actions[h.SEC_LR]
    assert lr["action_type"] == "civil_action" and lr["published_on"] == "2025-06-10"
    assert lr["settlement"] == {"settled_as_published": True,
                                "admission_as_published": "Without admitting or denying the allegations in the "
                                                          "complaint"}
    assert "Rule 10b-5" in lr["legal_bases"] and "Section 17(a) of the Securities Act of 1933" in lr["legal_bases"]
    assert lr["court_cases"] == [{"docket_number": "1:25-cv-09901", "court": "S.D.N.Y.",
                                  "caption": "Exampla Holdings plc and [individual]"}]
    assert lr["related_references"] == ["M.99001"]
    ap = actions[h.SEC_AP]
    assert {"scheme": "sec-file-number", "value": "3-99902", "type_as_published": "File No."} in \
        ap["related_identifiers"]
    assert ap["settlement"]["admission_as_published"] == "without admitting or denying the findings"
    respondents = {r["name_as_published"]: r for r in items["respondent"]}
    assert respondents["Exampla Holdings plc"]["identifiers"] == [
        {"scheme": "sec-cik", "type_as_published": "CIK", "value": "0009999101"}]
    penalties = items["penalty"]
    assert [(p["penalty_type"], p["amount_as_published"], p["currency"]) for p in penalties] == [
        ("civil_penalty", "$2,500,000", "USD")]  # the individual's own penalty is not recorded
    assert all(d["content_sha256"] for d in items["enforcement_decision"])


def test_individuals_are_counted_never_named():
    for source_id in h.SOURCES:
        text = json.dumps(h.records(source_id))
        for name in ("Jordan Placeholder", "Placeholder", "Casey", "Alex"):
            assert name not in text, (source_id, name)
    sec = {a["record_key"]: a for a in h.records("sec-enforcement-releases") if a["kind"] == "enforcement_action"}
    assert sec[h.SEC_LR]["natural_person_respondents"] == 1
    assert sec[h.SEC_LR]["title"] == "Exampla Holdings plc and [individual]"
    epa = next(a for a in h.records("epa-echo-enforcement-cases") if a["record_key"] == h.EPA_EX)
    assert epa["natural_person_respondents"] == 1
    body = (h.FIXTURES / "fca_northwind-brokers-limited-2024.txt").read_text().replace("Northwind Brokers Limited",
                                                                                          "Jo Placeholder")
    with pytest.raises(SourcePackError) as exc:
        h.fetch("fca-final-notices",
                bodies={"/publication/final-notices/northwind-brokers-limited-2024.pdf": body})
    assert "addressed to individuals" in str(exc.value)


def test_fca_notices_keep_both_penalty_figures_frn_rules_and_upper_tribunal_references():
    items = by_kind(h.records("fca-final-notices"))
    penalties = {(p["action_key"], p["penalty_type"]): p for p in items["penalty"]}
    assert penalties[(h.FCA_EX, "financial_penalty")]["amount_as_published"] == "£1,400,000"
    before = penalties[(h.FCA_EX, "penalty_before_settlement_discount")]
    assert before["amount_as_published"] == "£2,000,000" and before["note"] == "30% (stage 1) discount"
    assert before["currency"] == "GBP"
    actions = {a["record_key"]: a for a in items["enforcement_action"]}
    assert {"Principle 3 of the Authority's Principles for Businesses", "SYSC 6.1.1R",
            "section 206 of the Act"} <= set(actions[h.FCA_EX]["legal_bases"])
    assert actions[h.FCA_EX]["related_identifiers"][0] == {"scheme": "gb-fca-frn", "value": "999001",
                                                           "type_as_published": "Firm Reference Number"}
    appeal = items["appeal"][0]
    assert appeal["action_key"] == h.FCA_NW and appeal["reference"] == "FS/2023/0017"
    assert appeal["forum_as_published"] == "Upper Tribunal" and appeal["decided_on"] == "2024-09-30"
    conn = h.connection()
    h.apply(conn, "fca-final-notices")
    h.apply(conn, "fca-final-notices", v2=True)
    store = EnforcementStore(conn)
    notices = store.action_children(h.NS, h.FCA_EX)["enforcement_decision"]
    chain = store.history(h.NS, notices[0]["record"]["record_key"])
    assert [v["record"]["source_status"] for v in chain] == ["published", "corrected"]
    assert chain[1]["record"]["amended_on"] == "2025-05-02"
    assert chain[0]["record"]["content_sha256"] != chain[1]["record"]["content_sha256"]
    assert [v["record"]["source_status"] for v in store.history(h.NS, h.FCA_EX)] == ["published", "corrected"]


def test_fca_pdf_without_the_optional_extractor_is_an_explicit_degraded_state(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_pdfminer(name, *args, **kwargs):
        if name.startswith("pdfminer"):
            raise ImportError("pdfminer.six is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pdfminer)
    pages = h.native_pages("fca-final-notices")
    for page in pages:
        page["headers"] = {"Content-Type": "application/pdf"}
    adapter = EnforcementAdapter(h.source("fca-final-notices"), transport=fixture_transport(pages))
    with pytest.raises(SourcePackError) as exc:
        adapter.fetch_page({"operation": "records", "parameters": {}}, cursor=None)
    assert exc.value.code == "source_unavailable" and "pdfminer" in str(exc.value)


def test_echo_cases_keep_penalty_fields_separate_facilities_by_frs_and_revise_on_settlement():
    items = by_kind(h.records("epa-echo-enforcement-cases"))
    actions = {a["record_key"]: a for a in items["enforcement_action"]}
    facility = actions[h.EPA_EX]["facilities"][0]
    assert facility["frs_id"] == "110099990001" and facility["latitude"] == 37.8001
    assert facility["coordinate_source"] == "ECHO facility coordinates (FRS)"
    lakeside = actions[h.EPA_NW]["facilities"][0]
    assert lakeside["latitude"] is None and lakeside["coordinate_source"] is None  # no coordinates published
    assert actions[h.EPA_EX]["legal_bases"] == ["Clean Air Act Section 112(r)"]
    assert actions[h.EPA_EX]["court_cases"][0]["docket_number"] == "3:25-cv-09903"
    nw = {p["penalty_type"]: p for p in items["penalty"] if p["action_key"] == h.EPA_NW}
    assert nw["federal_penalty"]["amount_as_published"] == "$42,000"
    assert nw["cost_recovery"]["amount_as_published"] == "$0"
    assert nw["state_local_penalty"]["amount_status"] == "not_published"
    assert set(nw) == {"federal_penalty", "state_local_penalty", "supplemental_environmental_project",
                       "cost_recovery", "compliance_action_cost"}
    conn = h.connection()
    h.apply(conn, "epa-echo-enforcement-cases")
    h.apply(conn, "epa-echo-enforcement-cases", v2=True)
    store = EnforcementStore(conn)
    chain = store.history(h.NS, h.EPA_EX)
    assert [v["record"]["status_as_published"] for v in chain] == ["Active", "Closed"]
    assert chain[1]["record"]["settlement"]["admission_as_published"] is None
    assert chain[1]["record"]["outcome_as_published"] == "Consent Decree entered; the defendant does not admit liability"
    sep = next(v for v in store.action_children(h.NS, h.EPA_EX)["penalty"]
               if v["record"]["penalty_type"] == "supplemental_environmental_project")
    assert [v["record"]["amount_status"] for v in store.history(h.NS, sep["record"]["record_key"])] == [
        "not_published", "stated"]


def test_edpb_entries_keep_lead_and_concerned_authorities_measures_and_withdrawals_are_revisions():
    items = by_kind(h.records("edpb-art60-final-decisions"))
    actions = {a["record_key"]: a for a in items["enforcement_action"]}
    entry = actions[h.EDPB_EX]
    assert entry["lead_authority"]["code"] == "eu-dpa-nl"
    assert [c["country"] for c in entry["concerned_authorities"]] == ["DE", "FR"]
    assert entry["corrective_measures_as_published"] == ["Administrative fine", "Reprimand"]
    assert entry["legal_bases"] == ["Article 5(1)(f)", "Article 32", "Article 33"]
    fine = next(p for p in items["penalty"] if p["action_key"] == h.EDPB_EX)
    assert (fine["amount_as_published"], fine["currency"]) == ("EUR 750,000", "EUR")
    assert [r["name_as_published"] for r in items["respondent"]] == ["Exampla Intermediate B.V."]
    anonymous = actions[h.EDPB_ANON]
    assert anonymous["natural_person_respondents"] == 0 and anonymous["lead_authority"]["country"] == "IE"
    conn = h.connection()
    h.apply(conn, "edpb-art60-final-decisions")
    result = h.apply(conn, "edpb-art60-final-decisions", v2=True)
    assert sum(r["counts"].get("revised", 0) for r in result) == 1
    chain = EnforcementStore(conn).history(h.NS, h.EDPB_ANON)
    assert [v["record"]["source_status"] for v in chain] == ["published", "removed_by_source"]


def test_declined_units_foreign_hosts_and_missing_user_agent_are_refused(monkeypatch):
    pages = h.native_pages("epa-echo-enforcement-cases")
    pages[0]["final_url"] = "https://elsewhere.example/echo/case_rest_services.get_case_info"
    adapter = EnforcementAdapter(h.source("epa-echo-enforcement-cases"), transport=fixture_transport(pages))
    with pytest.raises(SourcePackError) as exc:
        adapter.fetch_page({"operation": "records", "parameters": {}}, cursor=None)
    assert exc.value.code == "network_policy"
    item = h.source("sec-enforcement-releases")
    item["enforcement"]["selection"] = {"releases": [{"path": "trading-suspensions/34-1"}]}
    with pytest.raises(SourcePackError):
        EnforcementAdapter(item, transport=fixture_transport([]))
    monkeypatch.delenv(SEC_USER_AGENT_ENV, raising=False)
    live = EnforcementAdapter(h.source("sec-enforcement-releases"))
    with pytest.raises(SourcePackError) as exc:
        live.fetch_page({"operation": "records", "parameters": {}}, cursor=None)
    assert exc.value.code == "source_unavailable" and SEC_USER_AGENT_ENV in str(exc.value)


def test_receipts_name_every_request_and_the_runtime_projector_is_registered():
    from src.ingestion.source_pack_runtime import PROJECTORS

    assert "noesis-enforcement-record-v2" in PROJECTORS
    conn = h.connection()
    h.apply(conn, "sec-enforcement-releases", run_id="run-sec")
    receipts = EnforcementStore(conn).receipts(h.NS, "run-sec", scopes=h.SCOPES)
    assert len(receipts) == 2 and receipts[0]["requests"][0]["path"].startswith("/enforcement-litigation/")
    assert receipts[0]["evidence_origin"] == "fixture"
    assert h.apply(conn, "sec-enforcement-releases", run_id="again")[0]["counts"]["inserted"] == 0
