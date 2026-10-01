"""Medicines source contracts (MR01) and EMA, Drugs@FDA, DailyMed and DSC acquisition (MR03-MR06), offline."""

from __future__ import annotations

import json

import pytest

from src.ingestion import clinical_providers as cp
from src.ingestion import medicines_sources as ms
from src.ingestion.source_packs import SourcePackConformance, SourcePackError, load_source_packs
from src.kb.clinical_records import ClinicalRecordStore
from tests.unit import medicines_fixture_builder as fb
from tests.unit.clinical.harness import NS, ROOT
from tests.unit.clinical.medicines_harness import MEDICINES_SCHEMA, SCOPES, Env


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import urllib.request

    def refuse(*args, **kwargs):
        raise AssertionError("offline tests must not open network connections")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse)


def medicines(env, kind=None, provider=None):
    rows = ClinicalRecordStore(env.conn, initialize=False).find(NS, scopes=SCOPES, kinds={
        "medicinal-product", "marketing-authorisation", "label-revision", "safety-communication"}, provider=provider)
    return [r for r in rows if r["record"].get("contract") == MEDICINES_SCHEMA and (kind is None
                                                                                   or r["record_kind"] == kind)]


# ---------------------------------------------------------------------- MR01


def test_every_source_has_an_access_decision_merged_into_the_clinical_contracts():
    required = {"documentation", "access", "authentication", "rate_limits", "pagination", "cadence", "terms",
                "retained_evidence", "identifiers", "cross_references", "status", "unavailable_fallback"}
    for provider in ("ema-epar", "drugs-at-fda", "dailymed", "fda-dsc", "rxnorm"):
        contract = cp.PROVIDER_CONTRACTS[provider]
        assert required <= set(contract), provider
        assert contract["record_owner"] == "src.kb.clinical_medicines"
        assert cp.LIVE_VERIFICATION[provider]["status"] == "unverified-live"
        assert cp.PROVIDER_HOSTS[provider]
    drugs = cp.PROVIDER_CONTRACTS["drugs-at-fda"]
    assert drugs["reuses"] == "openfda" and "no second openFDA client" in drugs["access"]
    for provider in ("ema-epar", "drugs-at-fda", "dailymed", "fda-dsc"):
        assert cp.PROVIDER_CONTRACTS[provider]["revision_behaviour"]
    assert "EMEA/H/C/009002" in ms.BOUNDED_SET["eu_products"] and "withdrawn" in ms.BOUNDED_SET["eu_products"][
        "EMEA/H/C/009002"]
    assert ms.BOUNDED_SET["safety_communications"]
    audit = (ROOT / "docs/roadmaps/clinical-medicines-source-audit.md").read_text()
    for needle in ("EMA legal notice", "openFDA Terms of Service", "NLM", "no second openFDA client", "_verify_",
                   "withdrawn", "Drug Safety Communication"):
        assert needle in audit


def test_sources_are_entries_of_the_existing_pack_with_pinned_fixtures_that_replay():
    pack = next(p for p in load_source_packs(ROOT / "config/source_packs") if p["pack_id"] == "clinical-evidence")
    assert pack["version"] == "0.1.4"  # 0.1.3 health capacity (#2215), 0.1.4 medical devices (#2654)
    added = [s for s in pack["sources"] if s["mapping"]["target_schema"] == MEDICINES_SCHEMA]
    assert {s["source_id"] for s in added} == {"medicines-ema-epar", "medicines-drugsfda-submissions",
                                               "medicines-dailymed-spl", "medicines-fda-dsc"}
    drugs = next(s for s in added if s["source_id"] == "medicines-drugsfda-submissions")
    assert drugs["connector"] == "openfda" and drugs["auth"]["secret_ref"] == "NOESIS_OPENFDA_API_KEY"
    result = SourcePackConformance(ROOT).offline(pack)
    assert result["valid"], result["sources"]
    for source in added:
        fixture = json.loads((ROOT / source["fixture"]["path"]).read_text())
        assert fixture["authored"] is True and "Not live evidence" in fixture["note"]


def test_medicines_fixtures_and_manifest_are_pinned_and_in_sync():
    from tests.unit import surveillance_fixture_builder as sfb

    manifest = json.loads((ROOT / "config/source_packs/clinical-evidence.json").read_text())
    assert fb.build(write=False) == manifest
    assert sfb.build(write=False) == manifest


# ---------------------------------------------------------------------- MR03 EMA


def test_ema_authorisations_withdrawal_reason_and_smpc_sections():
    env = Env()
    receipt = env.acquire_medicines("m1", ["medicines-ema-epar"])
    assert receipt["status"] == "complete"
    events = {(r["native_id"], r["record"]["event"]["kind"]): r["record"] for r in
              medicines(env, "marketing-authorisation", "ema")}
    assert events[("EMEA/H/C/009001", "grant")]["event"]["effective_date"] == "2020-02-10"
    withdrawal = events[("EMEA/H/C/009002", "withdrawal")]
    assert withdrawal["event"]["status"] == "withdrawn" and withdrawal["event"]["effective_date"] == "2024-11-15"
    assert "commercial reasons" in withdrawal["event"]["reason"]["text"]
    assert withdrawal["event"]["reason"]["locator"]["url"].endswith("_en.pdf")
    assert events[("EMEA/H/C/009001", "variation")]["procedure"]["number"] == "EMEA/H/C/009001/II/0012"
    (label,) = [r["record"] for r in medicines(env, "label-revision", "ema")]
    assert label["document"]["version"] == "4" and label["document"]["effective_date"] == "2026-06-01"
    codes = [s["code"] for s in label["sections"]]
    assert codes == ["1", "2", "4.1", "4.3", "4.4", "4.8", "5.1"]
    assert {o["code"] for o in label["omitted_sections"]} == {"4.2", "4.9"}
    assert "never retained" not in json.dumps(label["sections"])
    section = next(s for s in label["sections"] if s["code"] == "4.4")
    assert section["text"].startswith("Acute pancreatitis") and section["locator"]["lines"]
    assert {(r["kind"], r["value"]) for r in label["cited_references"]} == {
        ("nct", "NCT09000001"), ("eudract", "2015-900001-10"), ("pmid", "99000001")}
    assert all(r["attribution"] == ms.EMA_ATTRIBUTION for r in (label, withdrawal))


def test_ema_revisions_are_kept_side_by_side_keyed_by_product_number_and_revision():
    env = Env()
    env.serve_earlier()
    env.acquire_medicines("m1", ["medicines-ema-epar"])
    env.serve_pinned()
    env.acquire_medicines("m2", ["medicines-ema-epar"])
    labels = sorted((r["record"]["document"]["version"], r["record"]["document"]["effective_date"])
                    for r in medicines(env, "label-revision", "ema"))
    assert labels == [("3", "2025-03-10"), ("4", "2026-06-01")]
    variations = sorted(r["record"]["procedure"]["number"] for r in medicines(env, "marketing-authorisation", "ema")
                        if r["record"]["event"]["kind"] == "variation")
    assert variations == ["EMEA/H/C/009001/II/0009", "EMEA/H/C/009001/II/0012"]


def test_ema_refuses_unpinned_paths_and_reports_missing_product_information():
    source = next(s for s in json.loads((ROOT / "config/source_packs/clinical-evidence.json").read_text())["sources"]
                  if s["source_id"] == "medicines-ema-epar")
    bad = json.loads(json.dumps(source))
    bad["medicines"]["products"][0]["product_information_path"] = "/../etc/passwd"
    with pytest.raises(SourcePackError):
        ms.MedicinesAdapter({**bad, "source_hash": "x"}, transport=lambda **_: {"status": 404})
    with pytest.raises(SourcePackError):
        ms.parse_smpc_text("no sections here", url="https://www.ema.europa.eu/x", product_number="EMEA/H/C/009001")


# ---------------------------------------------------------------------- MR04 Drugs@FDA


def test_drugsfda_submissions_through_the_existing_openfda_adapter_with_disclaimer():
    env = Env()
    receipt = env.acquire_medicines("m1", ["medicines-drugsfda-submissions"])
    assert receipt["status"] == "complete"
    assert all(call["url"].startswith("https://api.fda.gov/drug/drugsfda.json") for call in env.web.calls)
    rows = medicines(env, provider="openfda")
    assert all(r["record"]["disclaimer"]["text"].startswith("Do not rely on openFDA") for r in rows)
    submissions = {r["record"]["submission"]["type"] + "-" + r["record"]["submission"]["number"]: r["record"]
                   for r in rows if r["record_kind"] == "marketing-authorisation" and r["record"].get("submission")}
    assert submissions["ORIG-1"]["event"] == {"kind": "grant", "native_status": "AP", "status": "approved",
                                              "effective_date": "2019-09-05",
                                              "locator": {"json_pointer": "/results/0/submissions/0"}}
    assert submissions["SUPPL-7"]["event"]["kind"] == "labeling-revision"
    assert isinstance(cp.ADAPTERS["openfda"], type) and "medicines" not in cp.ADAPTERS


def test_marketing_status_changes_are_kept_as_history():
    env = Env()
    env.acquire_medicines("m1", ["medicines-drugsfda-submissions"])
    env.serve_discontinued()
    env.acquire_medicines("m2", ["medicines-drugsfda-submissions"])
    statuses = sorted((r["record"]["event"]["kind"], r["record"]["event"]["native_status"])
                      for r in medicines(env, "marketing-authorisation", "openfda")
                      if not r["record"].get("submission"))
    assert statuses == [("discontinuation", "product 001: Discontinued"),
                        ("marketing-status", "product 001: Prescription")]
    (product,) = medicines(env, "medicinal-product", "openfda")
    history = ClinicalRecordStore(env.conn, initialize=False).history(NS, product["record_id"], scopes=SCOPES)
    assert [h["record"]["status"]["normalized"] for h in history["revisions"]] == ["marketed", "discontinued"]


def test_drugsfda_submissions_cannot_be_mixed_with_clinical_endpoints():
    source = next(s for s in json.loads((ROOT / "config/source_packs/clinical-evidence.json").read_text())["sources"]
                  if s["source_id"] == "medicines-drugsfda-submissions")
    mixed = json.loads(json.dumps(source))
    mixed["clinical"]["endpoints"] = ["label", "drugsfda-submissions"]
    with pytest.raises(SourcePackError):
        cp.OpenfdaAdapter({**mixed, "source_hash": "x"}, transport=lambda **_: {"status": 404})


# ---------------------------------------------------------------------- MR05 DailyMed


def test_spl_versions_are_label_revisions_with_loinc_sections_and_prior_versions_retained():
    env = Env()
    env.serve_earlier()
    env.acquire_medicines("m1", ["medicines-dailymed-spl"])
    env.serve_pinned()
    receipt = env.acquire_medicines("m2", ["medicines-dailymed-spl"])
    assert receipt["status"] == "complete"
    labels = {r["record"]["document"]["version"]: r["record"] for r in medicines(env, "label-revision", "dailymed")}
    assert set(labels) == {"7", "8"}
    latest = labels["8"]
    assert latest["document"]["id"] == fb.SET_ID and latest["document"]["effective_date"] == "2025-04-15"
    assert latest["document"]["revision_date"] == "2025-04-15"
    boxed = next(s for s in latest["sections"] if s["code"] == "34066-1")
    assert boxed["code_system"] == "loinc" and boxed["title"] == "WARNING: RISK OF PANCREATITIS"
    assert boxed["locator"]["xpath"].startswith("/document/component/structuredBody/")
    assert {o["code"] for o in latest["omitted_sections"]} == {"34068-7"}
    assert "never retained" not in json.dumps(latest["sections"])
    assert "34066-1" not in {s["code"] for s in labels["7"]["sections"]}
    assert [s["name"] for s in latest["active_substances"]] == ["NOETIGLUTIDE"]


def test_spl_rejects_another_set_id():
    with pytest.raises(SourcePackError):
        ms.parse_spl_xml(fb.spl_xml(8).encode(), set_id="00000000-0000-4000-8000-000000000000",
                         url="https://dailymed.nlm.nih.gov/x")


# ---------------------------------------------------------------------- MR06 DSC


def test_dsc_quotes_issue_date_named_substances_and_keeps_updates_as_revisions():
    env = Env()
    env.acquire_medicines("m1", ["medicines-fda-dsc"])
    (row,) = medicines(env, "safety-communication")
    item = row["record"]
    assert item["issued"] == "2026-04-15" and item["updates"] == []
    assert item["named_substances"] == [{"name": "noetiglutide", "role": "generic name",
                                         "locator": {"url": item["source_url"], "table_row": 1}}]
    assert item["named_products"][0]["name"] == "Noetiglu (fixture)"
    assert any("acute pancreatitis" in q["text"] for q in item["quotes"])
    env.serve_dsc_update()
    env.acquire_medicines("m2", ["medicines-fda-dsc"])
    history = ClinicalRecordStore(env.conn, initialize=False).history(NS, row["record_id"], scopes=SCOPES)
    first, second = history["revisions"]
    assert first["record"]["updates"] == [] and first["record"]["issued"] == "2026-04-15"
    assert second["record"]["updates"][0]["date"] == "2026-06-02"
    assert second["record"]["updates"][0]["text"].startswith("[6-2-2026] UPDATE")
    assert second["record"]["native_version"]["date"] == "2026-06-02"


def test_dsc_page_without_a_title_is_schema_drift():
    with pytest.raises(SourcePackError) as exc:
        ms.parse_dsc_html(b"<html><body><p>no title</p></body></html>", url="https://www.fda.gov/x")
    assert exc.value.code == "schema_drift"


def test_receipts_record_hashes_and_injected_execution():
    env = Env()
    receipt = env.acquire_medicines("m1")
    assert receipt["status"] == "complete"
    rows = env.conn.execute("SELECT receipt_json FROM source_pack_page_receipts").fetchall() if env.conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='source_pack_page_receipts'").fetchone() else []
    state = ClinicalRecordStore(env.conn, initialize=False)
    for provider in ("ema-epar", "drugs-at-fda", "dailymed", "fda-dsc"):
        assert state.provider_state(NS, provider)["last_execution"] == "injected"
    del rows
