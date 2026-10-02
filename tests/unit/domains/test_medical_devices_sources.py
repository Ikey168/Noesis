"""openFDA, AccessGUDID and EUDAMED acquisition through the medical-devices connector (MD01, MD03-MD06)."""

from __future__ import annotations

import copy
import json

import pytest

from src.ingestion.medical_devices_sources import (
    EUDAMED_MODULES,
    LIVE_VERIFICATION,
    MINIMISATION,
    PROVIDER_CONTRACTS,
    MedicalDevicesAdapter,
    fixture_transport,
)
from src.ingestion.source_packs import (
    SourcePackConformance,
    SourcePackError,
    validate_source_pack,
)
from src.kb.medical_devices_records import MedicalDeviceStore, personal_fields
from tests.unit import medical_devices_fixture_builder as builder
from tests.unit import medical_devices_harness as h


def records(source_id, **kwargs):
    return [r["medical_device_record"] for page in h.fetch(source_id, **kwargs) for r in page.records]


def test_sources_are_declared_unverified_live_with_the_minimisation_policy_and_pinned_fixtures():
    manifest = h.manifest()
    assert manifest["version"] == "0.1.4"
    ours = [s for s in manifest["sources"] if s["connector"] == "medical-devices"]
    assert {s["source_id"] for s in ours} == set(h.SOURCES)
    for source in ours:
        declared = source["medical_devices"]
        assert declared["live_verification"] == "unverified-live" == LIVE_VERIFICATION[declared["provider"]]["status"]
        assert declared["minimisation"] == MINIMISATION["id"] and source["auth"] == {"kind": "none"}
        # The pinned fixture is exactly what the builder composes from the authored responses.
        assert (h.ROOT / source["fixture"]["path"]).read_bytes() == builder.fixture_bytes(source["source_id"])
    report = SourcePackConformance(h.ROOT).offline(json.loads(h.PACK.read_text()))
    assert report["valid"], [s for s in report["sources"] if not s["valid"]]


def test_audit_records_endpoints_terms_rates_revisions_and_module_gaps():
    for provider, contract in PROVIDER_CONTRACTS.items():
        for field in ("endpoints", "authentication", "rate_limits", "revisions", "licence", "access_decision"):
            assert contract[field], (provider, field)
    assert {m for m, v in EUDAMED_MODULES.items() if v["status"] == "unavailable"} == {
        "vigilance-post-market-surveillance", "clinical-investigations-performance-studies", "market-surveillance"}
    audit = (h.ROOT / "docs/development/medical-devices-evidence/source-audit.md").read_text()
    for needle in ("not re-verified live", "LIVE_VERIFICATION", "minimisation", "EUDAMED", "AccessGUDID", "MAUDE"):
        assert needle in audit


def test_510k_pma_and_classification_are_keyed_with_decisions_and_supplements():
    clearances = records("clinical-devices-openfda-510k")
    assert [(r["record_key"], r["publication_state"]) for r in clearances] == [
        ("medical-devices:fda:510k:K999001", "published"), ("medical-devices:fda:510k:K999002", "published"),
        ("medical-devices:fda:510k:K999404", "not-published")]
    assert clearances[0]["disclaimer"]["text"] and clearances[0]["as_of"] == "2025-06-30"
    approvals = records("clinical-devices-openfda-pma", v2=True)
    assert [r["record_key"].rsplit(":", 1)[-1] for r in approvals] == ["P999001", "S001", "S002", "S003"]
    assert {r["record_kind"] for r in approvals[1:]} == {"approval-supplement"}
    codes = records("clinical-devices-openfda-classification")
    assert [(r["fields"]["product_code"], r["fields"]["device_class"]) for r in codes] == [("ZXA", "2"),
                                                                                         ("ZXB", "3")]


def test_recalls_and_maude_reports_keep_class_status_report_numbers_and_caveats():
    (recall,) = records("clinical-devices-openfda-recalls")
    assert (recall["fields"]["recall_class"], recall["fields"]["status_as_published"]) == ("Class II",
                                                                                           "Open, Classified")
    assert recall["fields"]["k_numbers"] == ["K999001"]
    maude = records("clinical-devices-openfda-maude")
    reports = [r for r in maude if r["record_kind"] == "adverse-event-report"]
    assert [r["fields"]["report_number"] for r in reports] == [f"9999001-2025-0000{i}" for i in (1, 2, 3)]
    counts = [r for r in maude if r["record_kind"] == "report-count"]
    assert counts[0]["fields"]["counts_as_published"] == [{"term": "Malfunction", "count": 2},
                                                          {"term": "Injury", "count": 1}]
    assert "never incidence" in counts[0]["fields"]["count_semantics"]
    assert counts[1]["publication_state"] == "not-published"  # ZXB: nothing published in the window
    for item in reports + counts[:1]:
        assert len(item["caveats"]) == 4
        assert personal_fields(item) == []
        assert "patient" not in json.dumps(item["fields"]).replace("patient_sections_excluded", "")
    assert reports[0]["fields"]["patient_sections_excluded"] == 1


def test_gudid_keeps_primary_and_package_dis_versions_and_premarket_links():
    v1, lead = records("clinical-devices-accessgudid")
    v2 = records("clinical-devices-accessgudid", v2=True)[0]
    assert v1["native_revision"] == "1" and v2["native_revision"] == "2"
    assert v2["fields"]["package_dis"][0]["di"] == "10899999000018"
    assert {"scheme": "fda-510k", "value": "K999001"} in v1["links_as_published"]
    assert {"scheme": "fda-application", "value": "NDA999001"} in lead["links_as_published"]
    assert v1["fields"]["customer_contacts_excluded"] == 1 and personal_fields(v1) == []


def test_eudamed_actors_devices_and_certificates_are_keyed_by_srn_basic_udi_and_certificate():
    actors = records("clinical-devices-eudamed-actors")
    assert [a["fields"]["srn"] for a in actors] == ["US-MF-000099902", "DE-MF-000099901"]
    assert all(a["fields"]["contact_persons_excluded"] == 2 and personal_fields(a) == [] for a in actors)
    (device,) = records("clinical-devices-eudamed-devices")
    assert device["fields"]["udi_dis"][0]["udi_di"] == h.PUMP_DI
    (v1,), (v2,) = records("clinical-devices-eudamed-certificates"), records("clinical-devices-eudamed-certificates",
                                                                               v2=True)
    assert (v1["fields"]["status_as_published"], v2["fields"]["status_as_published"]) == ("Valid", "Suspended")


def test_acquisition_is_bounded_receipted_and_all_or_nothing():
    pages = h.fetch("clinical-devices-openfda-510k")
    assert len(pages) == 3 and pages[-1].next_cursor is None
    receipt = pages[0].receipt
    assert receipt["contract"] == "noesis-medical-device-acquisition-receipt-v1"
    assert receipt["requests"][0]["path"].startswith("/device/510k.json?") and receipt["requests"][0]["sha256"]
    assert receipt["evidence_origin"] == "fixture" and receipt["minimisation"] == MINIMISATION["id"]
    # A result larger than one page is budget_exhausted, never truncated.
    native = h.native_pages("clinical-devices-openfda-pma")
    body = json.loads(native[0]["body"])
    body["meta"]["results"]["total"] = 250
    native[0]["body"] = json.dumps(body)
    with pytest.raises(SourcePackError) as caught:
        h.fetch("clinical-devices-openfda-pma", pages=native)
    assert caught.value.code == "budget_exhausted"
    # A response from another host is a network-policy failure.
    native = h.native_pages("clinical-devices-openfda-classification")
    native[0]["final_url"] = "https://elsewhere.example/device/classification.json"
    with pytest.raises(SourcePackError) as caught:
        h.fetch("clinical-devices-openfda-classification", pages=native)
    assert caught.value.code == "network_policy"
    # A missing disclaimer is schema drift: openFDA data is never stored without it.
    native = h.native_pages("clinical-devices-openfda-classification")
    body = json.loads(native[0]["body"])
    del body["meta"]["disclaimer"]
    native[0]["body"] = json.dumps(body)
    with pytest.raises(SourcePackError) as caught:
        h.fetch("clinical-devices-openfda-classification", pages=native)
    assert caught.value.code == "schema_drift"
    with pytest.raises(SourcePackError) as caught:
        MedicalDevicesAdapter(h.source("clinical-devices-openfda-510k"), transport=fixture_transport([])).fetch_page(
            {"operation": "records", "parameters": {"search": "k_number:*"}}, cursor=None)
    assert caught.value.code == "parameter_forbidden"


def test_declarations_are_validated():
    item = h.source("clinical-devices-openfda-maude")
    bad = copy.deepcopy(item)
    bad["medical_devices"]["selection"]["event_windows"][0]["received_to"] = "20270101"  # more than a year
    with pytest.raises(SourcePackError):
        MedicalDevicesAdapter(bad, transport=fixture_transport([]))
    bad = copy.deepcopy(item)
    bad["medical_devices"]["minimisation"] = "none"
    with pytest.raises(SourcePackError):
        MedicalDevicesAdapter(bad, transport=fixture_transport([]))
    manifest = json.loads(h.PACK.read_text())
    source = next(s for s in manifest["sources"] if s["source_id"] == "clinical-devices-accessgudid")
    source["medical_devices"]["selection"] = {"device_identifiers": [{"di": "x"}]}
    with pytest.raises(SourcePackError):
        validated = validate_source_pack(manifest)
        MedicalDevicesAdapter(next(s for s in validated["sources"] if s["source_id"] == "clinical-devices-accessgudid"),
                              transport=fixture_transport([]))


def test_the_runtime_projector_stores_pages_with_receipts():
    conn = h.connection()
    results = h.apply(conn, "clinical-devices-openfda-recalls")
    assert results[0]["counts"]["new"] == 1
    receipts = MedicalDeviceStore(conn).receipts(h.NS, scopes=h.SCOPES)
    assert receipts[0]["source_id"] == "clinical-devices-openfda-recalls" and receipts[0]["counts"]["new"] == 1
    again = h.apply(conn, "clinical-devices-openfda-recalls")
    assert again[0]["counts"]["unchanged"] == 1
