"""NH01, NH03-NH07 (#2304, #2315, #2320, #2324, #2328, #2334): bounded, receipted acquisition as published."""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft7Validator

from src.ingestion import hazard_sources as hs
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.kb.hazards_store import HazardStore
from tests.unit.hazards import harness as h


def _records(provider, files=None):
    _, pages = h.records(provider, files or h.latest(provider))
    return [r["hazard_record"] for page in pages for r in page.records], pages


def test_source_pack_declares_every_provider_with_live_verification_and_pinned_fixtures():
    manifest = h.manifest()
    schema = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-source-pack-v1.json").read_text())
    assert not list(Draft7Validator(schema).iter_errors(manifest))
    states = {s["natural_hazards"]["provider"]: s["natural_hazards"]["live_verification"] for s in manifest["sources"]}
    assert states == {"usgs": "unverified-live", "emsc": "unverified-live", "gdacs": "unverified-live",
                      "nhc": "unverified-live", "effis": "unverified-live", "glofas": "key-gated"}
    assert {p: v["status"] for p, v in hs.LIVE_VERIFICATION.items()} == states
    glofas = next(s for s in manifest["sources"] if s["source_id"] == "glofas-notifications")
    assert glofas["auth"] == {"kind": "required-secret", "secret_ref": "NOESIS_GLOFAS_TOKEN"}
    assert h.build(write=False) == {s["source_id"]: (s["fixture"]["sha256"], s["fixture"]["expected_output_hash"])
                                    for s in manifest["sources"]}
    report = SourcePackConformance(h.ROOT).offline(json.loads(h.PACK.read_text()))
    assert report["valid"] and report["coverage"] == {"configured": 6, "verified": 6}


def test_usgs_revisions_review_status_deleted_merged_and_pager_versions():
    records, pages = _records("usgs")
    assert all(page.receipt["evidence_origin"] == "fixture" and page.receipt["response_sha256"] for page in pages)
    by_id = {(r["record_type"], r["native_id"]): r for r in records}
    main = by_id[("hazard_event", "us7000zz01")]
    assert main["status"] == "reviewed" and main["identifiers"]["ids"] == ["us7000zz01", "at00zz01", "us7000zz03"]
    assert {(p["name"], p["value"], p["unit"], p["qualifier"]) for p in main["parameters"]} == {
        ("magnitude", "6.1", "magnitude", "mww"), ("depth", "12.4", "km", None)}
    assert main["revision_key"] == "4090035600000" and main["published_at"] == "2099-08-10T09:00:00.000Z"
    assert by_id[("hazard_event", "us7000zz02")]["status"] == "deleted"  # recorded, not removed
    merged = by_id[("hazard_event", "us7000zz03")]
    assert merged["status"] == "merged" and merged["merged_into"] == "us7000zz01" and merged["parameters"] == []
    pager = by_id[("impact_estimate", "us7000zz01:losspager:usus7000zz01")]
    assert pager["estimate"] == {"alert_level": "orange", "max_mmi": "7.1"}
    assert pager["notice"].startswith("the publisher's own estimate") and pager["product_version"] == "4090037400000"

    conn = h.connection()
    h.apply(conn, "usgs", h.EARLIER["usgs"], at="2099-08-10T03:50:00Z")
    second = h.apply(conn, "usgs", h.latest("usgs"), at="2099-08-11T00:00:00Z")
    assert second["revisions"] == 4 and second["unchanged"] == 3  # zz01, zz02, PAGER v2, zz03; re-read in details
    store = HazardStore(conn, initialize=False)
    rid = store.find(h.NS, "usgs", "hazard_event", "us7000zz01")
    history = store.revisions(h.NS, rid, scopes=h.SCOPES)["revisions"]
    assert [r["content"]["status"] for r in history] == ["automatic", "reviewed"]
    pager_id = store.find(h.NS, "usgs", "impact_estimate", "us7000zz01:losspager:usus7000zz01")
    assert [r["content"]["estimate"]["alert_level"] for r in store.revisions(h.NS, pager_id, scopes=h.SCOPES)["revisions"]] \
        == ["yellow", "orange"]
    deleted = store.find(h.NS, "usgs", "hazard_event", "us7000zz02")
    assert [r["content"]["status"] for r in store.revisions(h.NS, deleted, scopes=h.SCOPES)["revisions"]] == [
        "automatic", "deleted"]
    assert store.provider_state(h.NS, "usgs")["acquired"] is True


def test_emsc_keeps_its_own_parameters_and_author_never_merged_with_usgs():
    records, _ = _records("emsc")
    (event,) = records
    params = {p["name"]: p for p in event["parameters"]}
    assert params["author"]["value"] == "NOA" and params["magnitude"]["qualifier"] == "mw"
    assert params["depth"]["value"] == "12.0"  # properties.depth, not the negative GeoJSON z
    conn = h.connection()
    h.apply(conn, "usgs", h.latest("usgs"), at="2099-08-11T00:00:00Z")
    h.apply(conn, "emsc", h.EARLIER["emsc"], at="2099-08-10T03:50:00Z")
    h.apply(conn, "emsc", h.latest("emsc"), at="2099-08-11T00:00:00Z")
    store = HazardStore(conn, initialize=False)
    emsc = store.find(h.NS, "emsc", "hazard_event", "20990810_0000031")
    usgs = store.find(h.NS, "usgs", "hazard_event", "us7000zz01")
    assert emsc != usgs
    history = store.revisions(h.NS, emsc, scopes=h.SCOPES)["revisions"]
    assert [p["value"] for r in history for p in r["content"]["parameters"] if p["name"] == "author"] == ["EMSC", "NOA"]
    assert store.record(h.NS, usgs, scopes=h.SCOPES)["content"]["parameters"][1]["value"] == "6.1"


def test_gdacs_episodes_are_revisions_alerts_and_quoted_scores_with_glide():
    records, _ = _records("gdacs")
    by = {(r["record_type"], r["native_id"]): r for r in records}
    event = by[("hazard_event", "EQ1400001")]
    assert event["identifiers"]["glide"] == "EQ-2099-000101-GRC" and event["countries"] == ["GRC", "TUR"]
    assert event["episode_id"] == "1500002" and event["revision_key"] == "episode:1500002"
    alert = by[("alert", "EQ1400001:1500002")]
    assert alert["level"] == "Orange" and alert["wording"] == "Magnitude 6.1M, Depth:12.4km"
    assert alert["valid_to"] is None and "no expiry" in alert["validity_basis"]
    score = by[("impact_estimate", "EQ1400001:alertscore")]
    assert score["estimate"]["episode_alert_score"] == "2.5" and score["product_version"] == "episode 1500002"
    assert {r["hazard_type"] for r in records} == {"earthquake", "tropical_cyclone", "wildfire"}
    assert by[("hazard_event", "WF1020001")]["identifiers"]["glide"] is None


def test_nhc_advisories_as_issued_with_intermediate_numbers_forecast_track_and_cone_locator():
    records, _ = _records("nhc")
    advisories = [r for r in records if r["record_type"] == "advisory"]
    assert [a["advisory_number"] for a in advisories] == ["11", "12", "12A", "13"]
    assert [a["issued_at"] for a in advisories] == ["2099-09-01T03:00:00Z", "2099-09-01T09:00:00Z",
                                                    "2099-09-01T12:00:00Z", "2099-09-01T15:00:00Z"]
    twelve = advisories[1]
    assert twelve["storm_id"] == "AL052099" and twelve["cone"] == {
        "locator": "https://www.nhc.noaa.gov/gis/forecast/archive/al052099_5day_012.zip", "mirrored": False}
    assert [p["valid_at"] for p in twelve["forecast_track"]] == ["2099-09-01T18:00:00Z", "2099-09-02T06:00:00Z",
                                                                 "2099-09-03T06:00:00Z"]
    assert twelve["forecast_basis"].startswith("the issuing body's forecast as issued")
    assert twelve["watches_warnings"][0] == {"type": "Hurricane Warning", "area_text": "THE NORTHWESTERN BAHAMAS",
                                             "action": "in effect", "geometry": None}
    intermediate = advisories[2]
    assert intermediate["advisory_kind"].startswith("intermediate") and intermediate["forecast_track"] == []
    wind = {p["name"]: p for p in intermediate["parameters"]}["max_sustained_wind"]
    assert (wind["value"], wind["unit"], wind["as_published"]) == ("100", "mph", "100 MPH...155 KM/H")
    storm = [r for r in records if r["record_type"] == "hazard_event"]
    assert {r["native_id"] for r in storm} == {"AL052099"} and len(storm) == 4


def test_effis_burnt_area_revisions_and_glofas_modelled_alerts_behind_the_key():
    records, _ = _records("effis")
    (fire,) = records
    assert fire["geometry"]["type"] == "Polygon" and fire["published_at"] == "2099-08-15"
    area = {p["name"]: p for p in fire["parameters"]}["burnt_area"]
    assert (area["value"], area["unit"], area["qualifier"]) == ("342", "ha", "EFFIS mapped estimate")
    (notice,), _ = _records("glofas")
    assert notice["modelled"] is True and notice["model"]["version"] == "4.0"
    assert notice["model"]["notice"].startswith("the publisher's modelled output")
    assert notice["thresholds"][0]["return_period_years"] == "5"
    item = h.source("glofas")
    adapter = hs.HazardAdapter(item, transport=hs.fixture_transport([]), secret=None)
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "observe", "parameters": {}, "limit": 10}, cursor=None)
    assert caught.value.code == "authentication_failed"


def test_bounded_fail_closed_acquisition():
    item = h.source("emsc")
    url = item["natural_hazards"]["documents"][0]["url"]
    bad = [{"request": hs.fixture_request(url), "status": 200, "body": '{"type": "FeatureCollection", "features": '
            '[{"type": "Feature", "properties": {"mag": 5}}]}'}]
    with pytest.raises(SourcePackError) as caught:
        hs.HazardAdapter(item, transport=hs.fixture_transport(bad)).fetch_page(
            {"operation": "observe", "parameters": {}, "limit": 10}, cursor=None)
    assert caught.value.code == "schema_drift"
    rogue = copy.deepcopy(item)
    rogue["natural_hazards"]["documents"][0]["url"] = "https://example.org/x"
    with pytest.raises(SourcePackError) as caught:
        hs.HazardAdapter(rogue)
    assert caught.value.code == "network_policy"
    good = [{"request": hs.fixture_request(url), "status": 200, "body": h.body("emsc_query_2099-08-11.json")}]
    adapter = hs.HazardAdapter(item, transport=hs.fixture_transport(good))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "observe", "parameters": {"minmag": "1"}, "limit": 10}, cursor=None)
    assert caught.value.code == "parameter_forbidden"
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "observe", "parameters": {}, "limit": 0 or 1}, cursor="7")
    assert caught.value.code == "cursor_drift"
    limited = [{"request": hs.fixture_request(url), "status": 429, "headers": {"Retry-After": "30"}, "body": ""}]
    with pytest.raises(SourcePackError) as caught:
        hs.HazardAdapter(item, transport=hs.fixture_transport(limited)).fetch_page(
            {"operation": "observe", "parameters": {}, "limit": 10}, cursor=None)
    assert caught.value.code == "rate_limited"
