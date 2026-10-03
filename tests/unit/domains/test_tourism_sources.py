"""TO03, TO04 (#2739): tourism source contracts and acquisition (Eurostat tourism occupancy and capacity)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.ingestion.tourism_sources import (
    BOUNDED_COVERAGE,
    CAPS,
    EXCLUSIONS,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    TourismStatisticsAdapter,
    fixture_transport,
)
from src.kb.tourism_store import TourismStore
from tests.unit import tourism_harness as h


def test_every_source_has_the_audited_contract_caps_and_live_verification_entry():
    assert set(PROVIDER_CONTRACTS) == set(LIVE_VERIFICATION) == set(h.SOURCES.values()) | {"un-tourism"}
    assert set(CAPS) == set(h.SOURCES.values())
    for provider in h.SOURCES.values():
        contract = PROVIDER_CONTRACTS[provider]
        for key in ("publisher", "access", "authentication", "licence", "redistribution", "rate_limits",
                    "revision_model", "definitions", "unavailable_fallback"):
            assert contract[key], (provider, key)
        assert contract["status"] == LIVE_VERIFICATION[provider]["status"] == "unverified-live"
        assert "stale" in contract["unavailable_fallback"]
    assert PROVIDER_CONTRACTS["un-tourism"]["status"] == LIVE_VERIFICATION["un-tourism"]["status"] == "not-implemented"
    occupancy, capacity = CAPS["eurostat-tourism-occupancy"], CAPS["eurostat-tourism-capacity"]
    assert occupancy["monthly-national"] == {"documents": 2, "series_per_response": 30, "months": 36}
    assert occupancy["annual-nuts2"] == {"documents": 1, "series_per_response": 10}
    assert capacity == {"annual-nuts2": {"documents": 1, "series_per_response": 10}}
    assert BOUNDED_COVERAGE["eurostat-tourism-occupancy"]["monthly-national"]["places"] == {"Germany": "geo DE"}
    assert BOUNDED_COVERAGE["eurostat-tourism-occupancy"]["annual-nuts2"]["places"] == {"Berlin": "geo DE30"}
    assert BOUNDED_COVERAGE["eurostat-tourism-capacity"]["annual-nuts2"]["places"] == {"Berlin": "geo DE30"}
    assert BOUNDED_COVERAGE["un-tourism"]["status"] == "not-implemented"
    assert {"blending Eurostat and UN Tourism figures", "annual totals computed from months",
            "occupancy rates, averages, per-capita or per-bed figures of our own"} <= set(EXCLUSIONS)
    assert any("tour_dem_" in e and "tour_ce_oa" in e for e in EXCLUSIONS)
    audit = (h.ROOT / "docs/development/tourism-evidence/source-audit.md").read_text()
    for name in ("tour_occ_nim", "tour_occ_arm", "tour_occ_nin2", "tour_cap_nuts2", "LIVE_VERIFICATION", "#2739",
                 "Deviation", "not-implemented"):
        assert name in audit


def test_the_pinned_fixtures_replay_offline_through_the_real_adapter():
    manifest = h.manifest()
    assert manifest["version"] == "1.8.0"  # the tourism-statistics sources (#2739)
    tourism = [s for s in manifest["sources"] if s.get("connector") == "tourism-statistics"]
    assert {s["source_id"] for s in tourism} == set(h.SOURCES.values())
    assert {s["mapping"]["target_schema"] for s in tourism} == {"noesis-tourism-statistics-record-v2"}
    assert {s["tourism_statistics"]["live_verification"] for s in tourism} == {"unverified-live"}
    assert {s["auth"]["kind"] for s in tourism} == {"none"}
    replay = SourcePackConformance(h.ROOT).offline({**manifest, "sources": tourism})
    assert replay["valid"] and replay["coverage"]["verified"] == 2
    for name in h.SOURCES:
        fixture = json.loads((h.ROOT / h.source(name)["fixture"]["path"]).read_text())
        assert fixture["captured"] is None and "fictional" in fixture["note"]
        for page in fixture["native_pages"]:
            assert all(line.split(",")[7][:4] in {"2094", "2095", "2096", "2097"}
                       for line in page["body"].splitlines()[1:])


def test_occupancy_keeps_residence_accommodation_frequency_and_nuts_version_with_flags_verbatim():
    pages = h.fetch("occupancy")
    assert len(pages) == 3  # one request per declared document
    items = [r["tourism_item"] for page in pages for r in page]
    monthly = [i for i in items if i["frequency"] == "monthly"]
    annual = [i for i in items if i["frequency"] == "annual"]
    assert {(i["dataset"], i["residence"]["code"]) for i in monthly} == {
        (d, r) for d in ("tour_occ_nim", "tour_occ_arm") for r in ("TOTAL", "DOM", "FOR")}
    assert {i["area"]["code"] for i in monthly} == {"DE"} and {i["area"]["code"] for i in annual} == {"DE30"}
    assert all(i["area"]["nuts_version"] == "2021" for i in items)
    assert {i["accommodation"]["code"] for i in items} == {"I551-I553"}
    nights_for = next(i for i in monthly if i["dataset"] == "tour_occ_nim" and i["residence"]["code"] == "FOR")
    cells = {o["period"]: o for o in nights_for["observations"]}
    assert cells["2096-01"]["flags"] == {"OBS_FLAG": "b"} and cells["2096-04"]["flags"] == {"OBS_FLAG": "p"}
    assert any(n["kind"] == "break" and n["periods"] == ["2096-01"] for n in nights_for["source_notes"])
    (berlin,) = annual
    regional = {o["period"]: o for o in berlin["observations"]}
    assert regional["2095"]["status"] == "confidential" and regional["2095"]["value"] is None
    assert regional["2095"]["value_text"] == ":" and regional["2095"]["flag_meanings"] == ["confidential"]
    assert regional["2094"]["flags"] == {"OBS_FLAG": "d"}
    assert any(n["kind"] == "definition_differs" for n in berlin["source_notes"])
    assert berlin["definition"]["coverage_threshold"].startswith("establishments with 10 or more bed places")
    header = pages[0][0]["tourism_release"]
    assert header["release_basis"] == "provider_last_update" and header["published_on"] == "2024-02-12"
    assert header["live_verification"] == "unverified-live" and header["evidence_origin"] == "fixture"
    assert header["document_key"] == "eurostat-tourism-occupancy:tour_occ_nim:M.TOTAL+DOM+FOR.NR.I551-I553.DE"


def test_capacity_states_the_reference_date_per_value():
    items = {r["tourism_item"]["indicator"]["code"]: r["tourism_item"] for page in h.fetch("capacity") for r in page}
    assert {i["indicator"]["concept"] for i in items.values()} == {"establishments", "bed_places"}
    for item in items.values():
        assert item["residence"]["code"] == "not_applicable" and item["area"] == {
            "scheme": "eurostat-geo", "code": "DE30", "nuts_version": "2021", "level": 2, "label": "Berlin"}
        for obs in item["observations"]:
            reference = obs["attributes"]["capacity_reference"]
            assert reference["period"] == obs["period"] and "31 July" in reference["stated"]
    assert {o["period"]: o["flags"] for o in items["ESTBL"]["observations"]}["2095"] == {"OBS_FLAG": "e"}


def test_declarations_are_bounded_and_exclusions_refused():
    item = h.source("occupancy")
    too_long = json.loads(json.dumps(item))
    too_long["tourism_statistics"]["documents"][0]["params"]["endPeriod"] = "2099-12"
    excluded = json.loads(json.dumps(item))
    excluded["tourism_statistics"]["documents"][0]["flow"] = "tour_dem_tnw"
    platform = json.loads(json.dumps(item))
    platform["tourism_statistics"]["documents"][0]["flow"] = "tour_ce_oan"
    third_monthly = json.loads(json.dumps(item))
    third_monthly["tourism_statistics"]["documents"][2] = {
        **third_monthly["tourism_statistics"]["documents"][0], "key": "M.TOTAL.NR.I551.DE"}
    wrong_level = json.loads(json.dumps(item))
    wrong_level["tourism_statistics"]["documents"][2]["area"]["labels"] = {"DE": "Germany"}
    no_threshold = json.loads(json.dumps(item))
    no_threshold["tourism_statistics"]["documents"][0]["definition"] = {
        k: v for k, v in item["tourism_statistics"]["documents"][0]["definition"].items()
        if k != "coverage_thresholds"}
    no_version = json.loads(json.dumps(item))
    no_version["tourism_statistics"]["documents"][0]["area"]["nuts_version"] = "1999"
    keyed = json.loads(json.dumps(item))
    keyed["auth"] = {"kind": "optional-secret", "secret_ref": "X"}
    moved = json.loads(json.dumps(item))
    moved["endpoint"] = "https://example.org/data"
    no_reference = json.loads(json.dumps(h.source("capacity")))
    no_reference["tourism_statistics"]["documents"][0].pop("capacity_reference")
    for broken in (too_long, excluded, platform, third_monthly, wrong_level, no_threshold, no_version, keyed, moved,
                   no_reference):
        with pytest.raises(SourcePackError):
            TourismStatisticsAdapter(broken)
    adapter = TourismStatisticsAdapter(item, transport=fixture_transport(h.pages("occupancy")))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "release", "parameters": {"geo": "FR"}}, cursor=None)
    assert caught.value.code == "parameter_forbidden"


@pytest.mark.parametrize("failure", ["http_error", "redirect", "schema_drift", "over_budget", "undeclared_place"])
def test_a_failed_document_fails_the_run_with_a_receipt_and_leaves_vintages_current(failure):
    conn = h.connection()
    h.apply(conn, "occupancy", retrieved_at_ms=h.FIRST_RETRIEVAL)
    store = TourismStore(conn)
    before = conn.execute("SELECT vintage_id, status FROM tourism_vintages ORDER BY vintage_id").fetchall()
    revised = h.pages("occupancy", revision=True)
    item = h.revision_source("occupancy")
    if failure == "http_error":
        transport = fixture_transport([dict(p, status=503) for p in revised])
    elif failure == "redirect":
        def transport(**kwargs):
            return {**fixture_transport(revised)(**kwargs), "final_url": "https://example.org/x"}
    elif failure == "schema_drift":
        transport = fixture_transport([dict(p, body=p["body"].replace("c_resid", "residence")) for p in revised])
    elif failure == "over_budget":
        item["budgets"]["max_bytes"] = 100
        transport = fixture_transport(revised)
    else:
        transport = fixture_transport([dict(p, body=p["body"].replace(",DE,", ",FR,")) for p in revised])
    with pytest.raises(SourcePackError) as caught:
        h.fetch("occupancy", item=item, transport=transport)
    code = caught.value.code
    assert code in {"source_unavailable", "network_policy", "schema_drift", "response_too_large"}
    store.record_failure(h.NS, "eurostat-tourism-occupancy", code=code, run_id="run:failed", source_id=item["source_id"],
                         scopes=h.SCOPES)
    assert conn.execute("SELECT vintage_id, status FROM tourism_vintages ORDER BY vintage_id").fetchall() == before
    state = store.provider_state(h.NS, "eurostat-tourism-occupancy")
    assert state["stale"] is True and state["reason"] == code
    receipts = [r for r in store.receipts(h.NS, scopes=h.READ_ONLY) if r["outcome"] == "failed"]
    assert receipts and receipts[-1]["detail"]["failure_code"] == code


def test_the_runtime_failure_path_records_a_receipt_and_reports_stale():
    from src.kb.tourism_records import readiness
    from src.kb.tourism_store import TourismProjector

    conn = h.connection()
    h.apply(conn, "capacity", retrieved_at_ms=h.FIRST_RETRIEVAL)
    finished = TourismProjector(conn).finish_source(run_id="run:x", manifest=None, source=h.source("capacity"),
                                                    status="failed", principal_id="svc")
    assert finished["provider_state"]["stale"] is True
    status = readiness(conn)["providers"]
    assert status["eurostat-tourism-capacity"]["stale"] is True
    assert status["un-tourism"]["access_decision"] == "not-implemented"
