"""Medical-devices source audit, declared sources and acquisition through the real adapter (#2658, #2669, #2674,
#2680, #2684)."""

from __future__ import annotations

import json

import pytest

from src.ingestion import medical_devices_sources as md
from src.ingestion.source_packs import SourcePackError, _digest, replay_native_fixture
from tests.unit import medical_devices_fixture_builder as fb
from tests.unit import medical_devices_harness as h


def by_key(records):
    return {r["record_key"]: r for r in records}


def test_the_audit_records_contracts_minimisation_modules_and_coverage():
    audit = (h.ROOT / "docs/development/medical-devices-evidence/source-audit.md").read_text()
    for source_id in h.SOURCES:
        assert f"`{source_id}`" in audit
    for needed in ("api_key", "NOESIS_OPENFDA_API_KEY", "meta.disclaimer", "MAUDE", "patient", "narratives:read",
                   "Retention", "Bounded first coverage", "documented-not-acquired", "Vigilance", "EGRESS",
                   "2026-09-30", "unverified-live"):
        assert needed in audit, needed
    assert {"openfda-device", "accessgudid", "eudamed"} <= set(md.PROVIDER_CONTRACTS)
    for contract in md.PROVIDER_CONTRACTS.values():
        assert {"endpoints", "authentication", "rate_limits", "revisions", "licence", "access_decision"} <= set(contract)
    assert md.PROVIDER_CONTRACTS["openfda-device-registrationlisting"]["access_decision"] == "documented-not-acquired"
    assert all(v["status"] != "verified-live" for v in md.LIVE_VERIFICATION.values())
    assert {m for m, spec in md.EUDAMED_MODULES.items() if not spec["acquired"]} == {
        "market-surveillance", "vigilance-post-market-surveillance", "clinical-investigations-performance-studies"}
    assert "patient" in " ".join(md.MINIMISATION["never_stored"])


def test_the_pack_declares_every_source_bounded_minimised_and_replaying_its_pinned_output():
    manifest = h.manifest()
    assert manifest["version"] == "0.1.4"
    ours = {s["source_id"]: s for s in manifest["sources"] if s["connector"] == "medical-devices"}
    assert set(ours) == set(h.SOURCES)
    for source_id, item in ours.items():
        declared = item["medical_devices"]
        assert declared["live_verification"] == "unverified-live"
        assert declared["minimisation"] == md.MINIMISATION_POLICY and declared["format"] == h.FORMATS[source_id]
        openfda = declared["provider"] == "openfda-device"
        assert (item["auth"] == {"kind": "optional-secret", "secret_ref": "NOESIS_OPENFDA_API_KEY"}) is openfda
        fixture = json.loads((h.ROOT / item["fixture"]["path"]).read_text())
        assert fixture == json.loads(json.dumps(h.source_pack_fixture(source_id)))  # generated from the harness
        assert _digest(list(replay_native_fixture(item, fixture))) == item["fixture"]["expected_output_hash"]
    assert fb.build(write=False) == {name: (fb.FIXTURES / name).read_text() for name in fb.build(write=False)}


def test_clearances_approvals_supplements_and_classifications_keep_published_decisions():
    records, receipts = h.fetch_all("devices-fda-510k")
    found = by_key(records)
    k1 = found["medical-devices:fda:510k:K999901"]
    assert k1["fields"]["decision_code"] == "SESE" and k1["effective_on"] == "2098-03-10"
    assert k1["disclaimer"]["text"].startswith("Do not rely on openFDA")
    assert {"contact", "address_1", "zip_code"} <= set(k1["minimisation"]["withheld"])
    assert [r["outcome"] for r in receipts] == ["returned", "not_published"]  # ZZC: openFDA NOT_FOUND
    pma = by_key(h.fetch_all("devices-fda-pma")[0])
    approval = pma["medical-devices:fda:pma:P999901"]
    assert approval["fields"]["supplements_as_published"] == ["S001", "S002"]
    supplement = pma["medical-devices:fda:pma:P999901:S002"]
    assert supplement["parent_key"] == approval["record_key"] and supplement["effective_on"] == "2099-04-10"
    later = by_key(h.fetch_all("devices-fda-pma", "v2")[0])
    assert later["medical-devices:fda:pma:P999901"]["fields"]["supplements_as_published"] == ["S001", "S002", "S003"]
    classes = by_key(h.fetch_all("devices-fda-classification")[0])
    assert classes["medical-devices:fda:product-code:ZZB"]["fields"]["device_class"] == "3"


def test_recalls_enforcement_and_maude_reports_are_minimised_and_carry_caveats():
    recall = h.fetch_all("devices-fda-recalls")[0][0]
    assert recall["fields"]["recall_status"] == "Open, Classified" and recall["premarket_numbers"] == ["K999901"]
    assert "additional_info_contact" in recall["minimisation"]["withheld"]
    terminated = h.fetch_all("devices-fda-recalls", "v2")[0][0]
    assert terminated["fields"]["recall_status"] == "Terminated" and terminated["effective_on"] == "2099-09-15"
    enforcement = h.fetch_all("devices-fda-enforcement")[0][0]
    assert enforcement["fields"]["recall_class"] == "Class II" and enforcement["record_key"] == recall["record_key"]
    reports, receipts = h.fetch_all("devices-fda-maude-reports")
    assert len(reports) == 3 and all(r["caveats"] == list(md.MAUDE_CAVEATS) for r in reports)
    first = by_key(reports)["medical-devices:fda:maude:9999901-2099-00001"]
    assert "patient" in first["minimisation"]["withheld"] and "patient" not in first["fields"]
    assert first["fields"]["narratives"][0]["as_published"] is True and first["udi_dis"] == [h.EXAMPLE_DI]
    counts = h.fetch_all("devices-fda-maude-counts")[0]
    assert counts[0]["fields"]["unit"] == "reports"
    assert counts[0]["fields"]["counts_as_published"] == [{"event_type_as_published": "Malfunction", "reports": 14},
                                                          {"event_type_as_published": "Injury", "reports": 3}]
    text = json.dumps([h.fetch_all(s) for s in h.SOURCES])
    assert not [p for p in fb.PERSONAL if p in text]
    assert all(r["requests"][0]["path"].startswith("/device/") for r in receipts)


def test_gudid_and_eudamed_records_keep_identifiers_versions_and_status_as_published():
    devices = by_key(h.fetch_all("devices-gudid-identifiers")[0])
    pump = devices[f"medical-devices:gudid:di:{h.EXAMPLE_DI}"]
    assert pump["fields"]["package_dis"] == [fb.GUDID_A_PACKAGE] and pump["native_revision"] == "version:3"
    assert [v["public_version_number"] for v in pump["fields"]["version_history_as_published"]] == ["1", "2", "3"]
    assert pump["premarket_numbers"] == ["K999901"] and pump["minimisation"]["withheld"] == ["contacts"]
    later = by_key(h.fetch_all("devices-gudid-identifiers", "v2")[0])[pump["record_key"]]
    assert later["revision_order"] > pump["revision_order"]
    actor = h.fetch_all("devices-eudamed-actors")[0][0]
    assert actor["fields"]["srn"] == fb.SRN and {"prrc", "contactDetails"} <= set(actor["minimisation"]["withheld"])
    device = h.fetch_all("devices-eudamed-devices")[0][0]
    assert device["udi_dis"] == [h.FIXTURE_DI] and device["manufacturer_srn"] == fb.SRN
    certificate = h.fetch_all("devices-eudamed-certificates", "v2")[0][0]
    assert certificate["fields"]["status"] == "Suspended" and certificate["effective_on"] == "2099-08-01"


def test_the_openfda_key_never_reaches_a_receipt_or_record_and_units_are_bounded():
    seen = {}

    def transport(*, url, params, headers, timeout):
        seen.update(params)
        return md.fixture_transport(h.native_pages("devices-fda-recalls"))(url=url, params=params, headers=headers,
                                                                            timeout=timeout)

    adapter = md.MedicalDevicesAdapter(h.source("devices-fda-recalls"), transport=transport,
                                       secret="secret-key-value-123")
    page = adapter.fetch_page({"operation": "records", "parameters": {}}, cursor=None)
    assert seen["api_key"] == "secret-key-value-123"
    assert "secret-key-value-123" not in json.dumps([page.receipt, [dict(r) for r in page.records]])
    item = h.source("devices-fda-maude-reports")
    item["medical_devices"]["selection"] = {"event_windows": [{"product_code": "ZZA", "from": "2098-01-01",
                                                               "to": "2099-06-30"}]}
    with pytest.raises(SourcePackError) as refused:
        md.MedicalDevicesAdapter(item)
    assert refused.value.code == "invalid_manifest"
    item = h.source("devices-eudamed-actors")
    item["medical_devices"]["selection"] = {"documents": [{"key": fb.SRN, "path": "/other/../x.json"}]}
    with pytest.raises(SourcePackError):
        md.MedicalDevicesAdapter(item)


def test_a_redirect_to_another_host_and_an_oversized_unit_are_refused_not_truncated():
    pages = h.native_pages("devices-fda-510k")
    pages[0] = {**pages[0], "final_url": "https://example.org/device/510k.json"}
    adapter = md.MedicalDevicesAdapter(h.source("devices-fda-510k"), transport=md.fixture_transport(pages))
    with pytest.raises(SourcePackError) as refused:
        adapter.fetch_page({"operation": "records", "parameters": {}}, cursor=None)
    assert refused.value.code == "network_policy"
    body = json.loads(h.native_pages("devices-fda-510k")[0]["body"])
    body["meta"]["results"]["total"] = 900
    pages = h.native_pages("devices-fda-510k")
    pages[0] = {**pages[0], "body": json.dumps(body)}
    pages += [{"request": pages[0]["request"] + f"&skip={n}", "status": 200, "body": json.dumps(body)}
              for n in range(2, 12, 2)]
    adapter = md.MedicalDevicesAdapter(h.source("devices-fda-510k"), transport=md.fixture_transport(pages))
    with pytest.raises(SourcePackError) as exhausted:
        adapter.fetch_page({"operation": "records", "parameters": {}}, cursor=None)
    assert exhausted.value.code == "budget_exhausted"
