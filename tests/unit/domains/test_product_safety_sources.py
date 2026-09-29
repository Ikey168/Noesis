"""Products safety notices: source contracts (R01, #1935) and the Safety Gate, CPSC, NHTSA and RASFF adapters (R03-R05).

Authored fixtures in the providers' documented shapes (fictional products and
notice numbers) replay through the real adapters; no test touches the network.
"""

from __future__ import annotations

import copy
import json
from functools import partial

import jsonschema
import pytest

from src.ingestion.product_sources import (
    ADAPTERS,
    SAFETY_CONNECTORS,
    SAFETY_PROVIDER_CONTRACTS,
    SafetyNoticeAdapter,
    fixture_transport,
    safety_date,
    safety_declaration,
)
from src.ingestion.source_pack_runtime import HTTPSPageAdapter
from src.ingestion.source_packs import (
    NATIVE_CONNECTOR_MODULES,
    SUPPORTED_CONNECTORS,
    SourcePackConformance,
    SourcePackError,
    replay_native_fixture,
)
from tests.unit import product_safety_harness as h

REQUEST = {"operation": "notices", "parameters": {}, "limit": 50}


def adapter(source_id: str, native_pages=None, **changes) -> SafetyNoticeAdapter:
    declared = h.source(source_id)
    declared.update(changes)
    return SafetyNoticeAdapter(
        declared, transport=fixture_transport(native_pages or h.pages(source_id))
    )


def statements(source_id: str, native_pages=None) -> list[dict]:
    fixture = {"native_pages": native_pages or h.pages(source_id)}
    return [
        r["product_safety_notice"]
        for r in replay_native_fixture(h.source(source_id), fixture)
    ]


# ------------------------------------------------------------------ R01 contracts


def test_every_source_has_a_documented_access_decision_and_nothing_is_live_yet():
    assert set(SAFETY_PROVIDER_CONTRACTS) == {
        "safety-gate",
        "cpsc",
        "nhtsa",
        "rasff",
        "baua",
        "gpsr",
    }
    for provider in SAFETY_CONNECTORS:
        contract = SAFETY_PROVIDER_CONTRACTS[provider]
        assert contract["status"] == "unverified-live"
        for field in (
            "documentation",
            "access",
            "authentication",
            "rate_limits",
            "pagination",
            "identifiers",
            "revision_semantics",
            "cadence",
            "publishes",
            "terms_url",
            "verify",
        ):
            assert contract[field], (provider, field)
    for provider in ("baua", "gpsr"):
        assert SAFETY_PROVIDER_CONTRACTS[provider]["status"] == "not-implemented"
        assert SAFETY_PROVIDER_CONTRACTS[provider]["reason"]
    assert "never scraped" in SAFETY_PROVIDER_CONTRACTS["baua"]["reason"]
    audit = (h.ROOT / "docs/roadmaps/products-safety-source-audit.md").read_text()
    for number in ("SR/00417/26", "26117", "26V104000", "2026.0457", "32023R0988"):
        assert number in audit


def test_the_pack_declares_bounded_notice_sources_with_terms_and_live_state():
    value = h.manifest()
    assert value["version"] == "1.1.0"
    notice_sources = [
        s for s in value["sources"] if s["connector"] in SAFETY_CONNECTORS
    ]
    assert sorted(s["source_id"] for s in notice_sources) == sorted(h.NOTICE_SOURCES)
    for item in notice_sources:
        assert item["mapping"]["target_schema"] == "noesis-product-safety-notice-v1"
        assert item["product_safety"]["live_verification"] == "unverified-live"
        assert item["license"]["terms_url"].startswith("https://") and item["auth"] == {
            "kind": "none"
        }
        assert 1 <= len(item["product_safety"]["selection"]) <= 50
        assert (
            NATIVE_CONNECTOR_MODULES[item["connector"]]
            == "src.ingestion.product_sources"
        )
    assert set(SAFETY_CONNECTORS) <= SUPPORTED_CONNECTORS and all(
        ADAPTERS[c] is SafetyNoticeAdapter for c in SAFETY_CONNECTORS
    )
    raw = json.loads(h.PACK.read_text())
    schema = json.loads(
        (h.ROOT / "contracts/schemas/jsonschema/noesis-source-pack-v1.json").read_text()
    )
    jsonschema.validate({**value, "sources": value["sources"]}, schema)
    assert raw["version"] == value["version"]


def test_offline_conformance_matches_the_pinned_output_hashes():
    result = SourcePackConformance(h.ROOT).offline(h.manifest())
    assert result["valid"]
    by_source = {s["source_id"]: s for s in result["sources"]}
    assert {sid: by_source[sid]["records"] for sid in h.NOTICE_SOURCES} == {
        "safety-gate-alerts": 5,
        "cpsc-recalls": 2,
        "nhtsa-recalls": 1,
        "rasff-notifications": 1,
    }


def test_fixtures_are_marked_authored_not_captured():
    for path in h.FIXTURES.values():
        fixture = json.loads(path.read_text())
        assert (
            fixture["authored"] is True
            and fixture["captured"] is None
            and "fictional" in fixture["note"]
        )


# ------------------------------------------------------------------ selection bounds


@pytest.mark.parametrize(
    "selection,code",
    [
        ([], "unbounded_source"),
        ([{"alert_number": f"SR/{i:05d}/26"} for i in range(51)], "unbounded_source"),
        ([{"alert_number": "SR-00417-26"}], "invalid_mapping"),
        ([{"query": "kettle"}], "invalid_mapping"),
        ([{"week": "2026-W11"}], "invalid_mapping"),
        ([{"week": "2026-W60", "category": "Toys"}], "invalid_mapping"),
    ],
)
def test_safety_gate_selection_is_explicit_and_bounded(selection, code):
    declared = h.source("safety-gate-alerts")
    declared["product_safety"]["selection"] = selection
    with pytest.raises(SourcePackError) as caught:
        safety_declaration(declared)
    assert caught.value.code == code


def test_cpsc_manufacturer_windows_are_bounded_and_nhtsa_uses_campaign_numbers():
    declared = h.source("cpsc-recalls")
    declared["product_safety"]["selection"] = [
        {
            "manufacturer": "Exampla Displays Inc.",
            "recall_date_start": "2024-01-01",
            "recall_date_end": "2026-01-01",
        }
    ]
    with pytest.raises(SourcePackError) as caught:
        safety_declaration(declared)
    assert caught.value.code == "unbounded_source"
    declared["product_safety"]["selection"] = [
        {
            "manufacturer": "X",
            "recall_date_start": "2026-03-01",
            "recall_date_end": "2026-02-01",
        }
    ]
    with pytest.raises(SourcePackError):
        safety_declaration(declared)
    nhtsa = h.source("nhtsa-recalls")
    nhtsa["product_safety"]["selection"] = [
        {"make": "VELOMARK", "model": "CITYRUNNER", "model_year": "2026"}
    ]
    with pytest.raises(SourcePackError) as caught:
        safety_declaration(nhtsa)
    assert (
        caught.value.code == "invalid_mapping"
    )  # recallsByVehicle is a filtered view, never acquired


def test_runs_use_the_pinned_selection_and_checkpoint_cursors():
    item = adapter("rasff-notifications")
    with pytest.raises(SourcePackError) as caught:
        item.fetch_page(
            {**REQUEST, "parameters": {"reference": "2026.9999"}}, cursor=None
        )
    assert caught.value.code == "parameter_forbidden"
    with pytest.raises(SourcePackError) as caught:
        item.fetch_page({**REQUEST, "operation": "models"}, cursor=None)
    assert caught.value.code == "operation_forbidden"
    with pytest.raises(SourcePackError) as caught:
        item.fetch_page(REQUEST, cursor=json.dumps({"index": 0, "scope": "other"}))
    assert caught.value.code == "cursor_drift"
    first = adapter("safety-gate-alerts").fetch_page(REQUEST, cursor=None)
    assert (
        json.loads(first.next_cursor)["index"] == 1
        and first.receipt["selector_outcome"] == "returned"
    )


def test_live_adapters_use_the_runtime_default_transport():
    item = SafetyNoticeAdapter(h.source("cpsc-recalls"))
    assert (
        isinstance(item.transport, partial)
        and item.transport.func is HTTPSPageAdapter._request
    )
    assert (
        item.transport.keywords["max_bytes"]
        == h.source("cpsc-recalls")["budgets"]["max_bytes"]
    )


# ------------------------------------------------------------------ failures


@pytest.mark.parametrize(
    "status,body,headers,code",
    [
        (401, None, {}, "authentication_failed"),
        (403, None, {}, "authentication_failed"),
        (429, None, {"Retry-After": "7"}, "rate_limited"),
        (503, None, {}, "source_unavailable"),
        (400, None, {}, "schema_drift"),
        (200, "<html>maintenance</html>", {}, "schema_drift"),
        (200, {"unexpected": True}, {}, "schema_drift"),
    ],
)
def test_failures_are_classified_not_swallowed(status, body, headers, code):
    native = h.pages("rasff-notifications")
    native[0].update({"status": status, "body": body, "headers": headers})
    with pytest.raises(SourcePackError) as caught:
        adapter("rasff-notifications", native).fetch_page(REQUEST, cursor=None)
    assert caught.value.code == code
    if code == "rate_limited":
        assert caught.value.details["retry_after_ms"] == 7000


def test_oversized_responses_and_pages_are_refused_never_truncated():
    native = h.pages("safety-gate-alerts")
    small = adapter(
        "safety-gate-alerts",
        native,
        budgets={**h.source("safety-gate-alerts")["budgets"], "max_bytes": 200},
    )
    with pytest.raises(SourcePackError) as caught:
        small.fetch_page(REQUEST, cursor=None)
    assert caught.value.code == "response_too_large"
    report = next(p for p in native if "weekly-reports" in p["request"])
    report["body"]["alerts"] = [
        copy.deepcopy(report["body"]["alerts"][0]) for _ in range(3)
    ]
    item = adapter("safety-gate-alerts", native)
    scope = json.loads(item.fetch_page(REQUEST, cursor=None).next_cursor)["scope"]
    with pytest.raises(SourcePackError) as caught:
        item.fetch_page(
            {**REQUEST, "limit": 2}, cursor=json.dumps({"index": 5, "scope": scope})
        )
    assert caught.value.code == "response_too_large"


def test_cross_host_redirects_are_refused():
    native = h.pages("nhtsa-recalls")
    native[0]["final_url"] = "https://mirror.example.org/recalls/campaignNumber"
    with pytest.raises(SourcePackError) as caught:
        adapter("nhtsa-recalls", native).fetch_page(REQUEST, cursor=None)
    assert caught.value.code == "network_policy"


def test_an_unknown_notice_number_is_a_selection_outcome_not_a_failure():
    item = adapter("safety-gate-alerts")
    cursor = None
    outcomes = []
    while True:
        page = item.fetch_page(REQUEST, cursor=cursor)
        outcomes.append(
            (
                page.receipt["selector"].get("alert_number")
                or page.receipt["selector"]["week"],
                page.receipt["selector_outcome"],
                len(page.records),
                page.receipt["filtered_out_by_category"],
            )
        )
        cursor = page.next_cursor
        if cursor is None:
            break
    assert ("SR/09999/26", "not_found", 0, 0) in outcomes
    # The weekly report keeps only the pinned category; the toy alert is filtered, not recorded or withdrawn.
    assert ("2026-W11", "returned", 1, 1) in outcomes


# ------------------------------------------------------------------ field mapping


def test_safety_gate_fields_are_verbatim_with_locators():
    alert = next(
        s
        for s in statements("safety-gate-alerts")
        if s["notice_number"] == "SR/00417/26"
    )
    raw = h.alert_page(h.pages("safety-gate-alerts"), "SR/00417/26")["body"]["alert"]
    identifications = {(i["kind"], i["value"]): i for i in alert["identifications"]}
    assert identifications[("brand", "Exampla")]["locator"] == {
        "json_pointer": "/product/brand"
    }
    assert identifications[("model", "EX-32U8")]["locator"] == {
        "json_pointer": "/product/typeNumberOfModel"
    }
    assert identifications[("batch", "B2025-11")]["group"] == 0
    # The barcode is kept verbatim with its GTIN state, even though its check digit is wrong.
    assert (
        identifications[("gtin", "4012345000029")]["gtin_state"] == "invalid_checksum"
    )
    assert alert["hazards"][0]["description"] == raw["risk"]["description"]
    assert alert["hazards"][0]["risk_level"] == "Serious risk"
    assert alert["corrective_actions"][0]["text"] == raw["measures"][0]["measureType"]
    assert alert["corrective_actions"][0]["taken_by"] == "Economic operator"
    assert alert["notifying_country"] == {"declared": "Germany", "code": "DE"}
    assert alert["notice_type"] == {"declared": "Alert notification", "value": "alert"}
    assert alert["issuing_authority"]["value"] == "eu-safety-gate"
    assert alert["published"] == "2026-03-13" and alert["revision_date"] == "2026-03-13"
    assert alert["compliance"][0]["locator"] == {"json_pointer": "/risk/compliance"}
    jsonschema.validate(
        alert,
        json.loads(
            (
                h.ROOT
                / "contracts/schemas/jsonschema/noesis-product-safety-notice-v1.json"
            ).read_text()
        ),
    )


def test_a_multi_code_barcode_field_keeps_every_code_and_the_raw_field():
    native = h.pages("safety-gate-alerts")
    page = h.alert_page(native, "SR/00431/26")
    page["body"]["alert"]["product"]["barcode"] = "4012345000016, 4012345000054"
    alert = next(
        s
        for s in statements("safety-gate-alerts", native)
        if s["notice_number"] == "SR/00431/26"
    )
    gtins = [
        (i["kind"], i["value"], i.get("part"))
        for i in alert["identifications"]
        if i["kind"].startswith("gtin")
    ]
    assert gtins == [
        ("gtin_field", "4012345000016, 4012345000054", None),
        ("gtin", "4012345000016", 0),
        ("gtin", "4012345000054", 1),
    ]


def test_unknown_enumerations_stay_unknown():
    native = h.pages("rasff-notifications")
    native[0]["body"]["notification"]["classification"] = "special notification"
    statement = statements("rasff-notifications", native)[0]
    assert statement["notice_type"] == {
        "declared": "special notification",
        "value": "unknown",
    }


def test_cpsc_upcs_models_parties_and_remedies_are_native():
    recall = next(
        s for s in statements("cpsc-recalls") if s["notice_number"] == "26117"
    )
    upc = next(i for i in recall["identifications"] if i["kind"] == "gtin")
    assert upc == {
        "kind": "gtin",
        "value": "012345678905",
        "group": -1,
        "gtin_state": "valid",
        "locator": {"json_pointer": "/ProductUPCs/0/UPC"},
    }
    assert (
        recall["jurisdiction"] == "US"
        and recall["issuing_authority"]["declared"]
        == "U.S. Consumer Product Safety Commission"
    )
    assert {(p["role"], p["name"]) for p in recall["parties"]} == {
        ("manufacturer", "Brightway Home Ltd."),
        ("importer", "Brightway Home USA Inc."),
        ("retailer", "Examplemart stores nationwide"),
    }
    assert recall["corrective_actions"][0]["remedy_type"] == ["Refund"]
    assert recall["corrective_actions"][0]["text"].startswith(
        "Consumers should immediately stop using"
    )
    assert {h_["hazard_type"] for h_ in recall["hazards"]} == {"Burn", "injury-report"}
    assert recall["published"] == "2026-02-12" and recall["native_record_id"] == "99117"
    brandless = next(
        s for s in statements("cpsc-recalls") if s["notice_number"] == "26140"
    )
    assert not [i for i in brandless["identifications"] if i["kind"] == "brand"]


def test_nhtsa_keeps_make_model_year_and_reads_day_first_dates():
    campaign = statements("nhtsa-recalls")[0]
    rows = sorted(
        (i["group"], i["kind"], i["value"])
        for i in campaign["identifications"]
        if i["kind"] in {"brand", "model", "model_year"}
    )
    assert rows == [
        (0, "brand", "VELOMARK"),
        (0, "model", "CITYRUNNER"),
        (0, "model_year", "2025"),
        (1, "brand", "VELOMARK"),
        (1, "model", "CITYRUNNER"),
        (1, "model_year", "2026"),
    ]
    assert (
        campaign["published"] == "2026-02-18"
        and campaign["published_declared"] == "18/02/2026"
    )
    assert campaign["order_basis"] == "observation"
    assert campaign["corrective_actions"][0]["remedy_type"] == ["overTheAirUpdate"]
    assert not any(k in campaign for k in ("diagonal", "attributes"))


def test_rasff_keeps_lots_best_before_hazard_and_distribution_verbatim():
    notification = statements("rasff-notifications")[0]
    values = {(i["kind"], i["value"]) for i in notification["identifications"]}
    assert {("batch", "L2601/B"), ("best_before", "30/05/2026")} <= values
    assert (
        notification["hazards"][0]["analytical_result"]
        == "presence /25g (ISO 6579-1:2017)"
    )
    assert [c["code"] for c in notification["distribution"]] == ["DE", "NL"]
    assert notification["followups"][0]["date"] == "2026-03-04"
    assert notification["revision_date"] == "2026-03-04"
    assert notification["notice_type"]["value"] == "alert"


def test_dates_are_normalised_to_iso_and_bad_dates_are_absent():
    assert safety_date("2026-02-12T00:00:00") == "2026-02-12"
    assert safety_date("18/02/2026", day_first=True) == "2026-02-18"
    assert safety_date("02/18/2026") == "2026-02-18"
    assert safety_date("31/02/2026", day_first=True) is None
    assert safety_date("") is None and safety_date("soon") is None


def test_unchanged_reacquisition_yields_identical_statements():
    first = replay_native_fixture(
        h.source("safety-gate-alerts"), {"native_pages": h.pages("safety-gate-alerts")}
    )
    second = replay_native_fixture(
        h.source("safety-gate-alerts"), {"native_pages": h.pages("safety-gate-alerts")}
    )
    assert first == second
    weekly = [
        r for r in first if r["product_safety_notice"]["payload_pointer"] == "/alerts/0"
    ]
    single = [
        r
        for r in first
        if r["id"] == "safety-gate:SR/00417/26"
        and r["product_safety_notice"]["payload_pointer"] == "/alert"
    ]
    assert weekly and single and weekly[0]["content"] == single[0]["content"]
