"""WX03-WX06 (#2166-#2169): DWD, MOSMIX, CAP, aviationweather.gov, NWS and Open-Meteo acquisition offline."""

from __future__ import annotations

import json

import pytest

from src.ingestion import weather_sources as ws_src
from src.ingestion.provider_execution import ProviderError
from src.ingestion.source_packs import SourcePackConformance, validate_source_pack
from src.kb import weather_store as ws
from tests.unit.weather import fixture_builder as fb
from tests.unit.weather import harness as h


@pytest.fixture(scope="module")
def world():
    conn = h.connection()
    h.acquire_all(conn)
    return conn, ws.WeatherStore(conn, initialize=False)


def currents(store, record_type, **filters):
    return store.currents(h.NS, record_type=record_type, scopes=h.SCOPES, **filters)


def test_dwd_relocation_is_two_location_vintages_and_never_relocates_observations(
    world,
):
    conn, store = world
    vintages = store.location_vintages(h.NS, f"dwd:{fb.DWD}", scopes=h.SCOPES)
    assert [
        (
            v["content"]["valid_from"],
            v["content"].get("valid_to"),
            v["content"]["latitude"],
        )
        for v in vintages
    ] == [("1990-01-01", "2026-05-31", "52.3700"), ("2026-06-01", None, "52.3810")]
    assert vintages[1]["content"]["open_ended"] is True
    places = conn.execute(
        "SELECT count(*) FROM geospatial_places WHERE place_key LIKE 'weather:location:%'"
    ).fetchone()
    assert places[0] == 2
    old = next(
        r
        for r in currents(store, "observation_report", subject_keys=[f"dwd:{fb.DWD}"])
        if r["content"]["observed_at"] == "2026-05-31T12:00:00Z"
    )
    assert (
        "latitude" not in old["content"]
    )  # a report never carries a location; it resolves by its own time


def test_dwd_quality_levels_verbatim_missing_values_absent_and_revisions_appended(
    world,
):
    conn, store = world
    reports = {
        r["content"]["observed_at"]: r
        for r in currents(store, "observation_report", subject_keys=[f"dwd:{fb.DWD}"])
        if r["content"]["report_type"] == "dwd-10min"
    }
    noon = reports["2026-06-10T12:00:00Z"]
    tt = next(p for p in noon["content"]["parameters"] if p["parameter"] == "TT_10")
    assert tt == {
        "parameter": "TT_10",
        "unit": "°C",
        "value": "18.6",
        "qc": {"scheme": "dwd-qn", "native": "10"},
    }
    # The late 'recent' copy restates the first revision below the historical one: nothing new is recorded.
    assert [x["change_kind"] for x in noon["history"]] == ["initial", "correction"]
    assert noon["content"]["release"]["period"] == "historical"
    missing = next(
        p
        for p in reports["2026-06-10T13:10:00Z"]["content"]["parameters"]
        if p["parameter"] == "TT_10"
    )
    assert "value" not in missing and missing["missing"].startswith(
        "published missing marker -999"
    )
    # As of before the historical release, the provisional value was current.
    before = store.current(
        h.NS, noon["record_id"], scopes=h.SCOPES, cutoff_ms=h.ms("2026-06-20T00:00:00Z")
    )
    assert (
        next(p for p in before["content"]["parameters"] if p["parameter"] == "TT_10")[
            "value"
        ]
        == "18.4"
    )
    assert all(
        r["content"]["attribution"].startswith("Quelle: Deutscher Wetterdienst")
        for r in reports.values()
    )


def test_stations_are_registered_through_the_environment_owner_only(world):
    conn, _ = world
    from src.kb.environment_store import EnvironmentStore

    env = EnvironmentStore(conn, initialize=False)
    stations = {
        (r["provider"], r["native_id"])
        for r in env.records(h.ENV_NS, scopes=h.SCOPES, record_type="station")
    }
    assert stations == {
        ("dwd", fb.DWD),
        ("dwd-mosmix", fb.MOSMIX),
        ("aviationweather", fb.ICAO),
        ("aviationweather", fb.US_ICAO),
    }
    kinds = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT record_type FROM environment_records"
        ).fetchall()
    }
    assert kinds == {"station"}
    assert not [
        t
        for (t,) in conn.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
        if t.startswith("weather_")
        and "station" in t
        and t != "weather_station_identifiers"
    ]


def test_mosmix_runs_are_separate_issuances_and_the_same_run_is_deduplicated(world):
    conn, store = world
    runs = currents(
        store, "forecast_issuance", subject_keys=[f"dwd-mosmix:{fb.MOSMIX}"]
    )
    assert [r["content"]["issued_at"] for r in runs] == [
        "2026-06-10T03:00:00Z",
        "2026-06-10T09:00:00Z",
        "2026-06-10T12:00:00Z",
    ]
    first = store.elements(runs[0]["revision_id"], parameter="TTT")
    assert [
        (e["valid_time"], e["lead_time_s"], e["value"], e["unit"]) for e in first
    ] == [
        ("2026-06-10T12:00:00Z", 9 * 3600, "291.35", "K"),
        ("2026-06-10T13:00:00Z", 10 * 3600, "292.15", "K"),
    ]
    late = store.elements(runs[2]["revision_id"], parameter="RR1c")
    assert "value" not in late[0] and late[0]["missing"] == "not published"
    again = h.acquire(
        conn, "2026-06-10T10:45:00Z", lambda: h.mosmix("2026-06-10T09:00:00.000Z")
    )
    assert (
        again[1]["outcome"]["revisions"] == 0 and again[1]["outcome"]["unchanged"] == 1
    )


def test_cap_messages_keep_references_and_areas_project_by_warncell(world):
    conn, store = world
    warnings = {
        r["content"]["msg_type"]: r["content"]
        for r in currents(store, "warning", provider="dwd-cap")
    }
    assert set(warnings) == {"Alert", "Update", "Cancel"}
    assert warnings["Update"]["references"][0]["identifier"] == fb.A1
    assert {r["identifier"] for r in warnings["Cancel"]["references"]} == {fb.A1, fb.U1}
    assert (
        warnings["Alert"]["sent"] == "2026-06-10T06:00:00Z"
        and warnings["Alert"]["event_code"] == "II:22"
    )
    assert warnings["Alert"]["instruction"] == "Text des Herausgebers (fiktiv)."
    natives = {
        r[0]
        for r in conn.execute(
            "SELECT native_id FROM geospatial_features WHERE collection=?",
            [ws.WARNING_AREA_COLLECTION],
        ).fetchall()
    }
    assert f"warncell:{fb.WARNCELL}" in natives and not any(
        "Musterkreis" in n for n in natives
    )


def test_metar_raw_text_verbatim_cor_is_a_revision_and_a_late_original_never_displaces_it(
    world,
):
    conn, store = world
    report = next(
        r
        for r in currents(
            store, "observation_report", subject_keys=[f"aviationweather:{fb.ICAO}"]
        )
        if r["content"]["observed_at"] == "2026-06-10T11:50:00Z"
    )
    assert (
        report["content"]["raw_text"]
        == "METAR COR EDXM 101150Z 24008KT 9999 FEW030 19/09 Q1015"
    )
    assert report["content"]["correction"] == "COR" and [
        x["change_kind"] for x in report["history"]
    ] == ["initial", "correction"]
    other = h.connection()
    h.acquire(other, "2026-06-10T12:40:00Z", lambda: h.awc("second"))
    late = h.acquire(other, "2026-06-10T13:00:00Z", lambda: h.awc("first"))
    assert (
        late[1]["outcome"]["late_history"] == 1
        and late[1]["outcome"]["corrections"] == 0
    )
    current = ws.WeatherStore(other, initialize=False).current(
        h.NS,
        ws.record_id(h.NS, "observation_report", report["record_key"]),
        scopes=h.SCOPES,
    )
    assert current["content"]["correction"] == "COR"
    suspect = next(
        r
        for r in currents(
            store, "observation_report", subject_keys=[f"aviationweather:{fb.ICAO}"]
        )
        if r["content"]["observed_at"] == "2026-06-10T12:20:00Z"
    )
    wdir = next(p for p in suspect["content"]["parameters"] if p["parameter"] == "wdir")
    assert wdir["missing"].startswith("published as VRB")


def test_taf_is_an_issuance_with_validity_and_raw_text_only(world):
    _, store = world
    taf = currents(
        store, "forecast_issuance", subject_keys=[f"aviationweather:{fb.ICAO}"]
    )[0]["content"]
    assert (
        taf["product"] == "TAF"
        and taf["elements"] == []
        and taf["raw_text"].startswith("TAF EDXM")
    )
    assert (taf["valid_from"], taf["valid_to"]) == (
        "2026-06-10T13:00:00Z",
        "2026-06-11T18:00:00Z",
    )


def test_nws_issuances_by_update_time_alert_update_and_user_agent(world):
    _, store = world
    runs = currents(store, "forecast_issuance", provider="nws")
    assert [r["content"]["issued_at"] for r in runs] == [
        "2026-06-10T09:00:00Z",
        "2026-06-10T11:00:00Z",
    ]
    assert runs[0]["content"]["location"]["declared_station"] == h.DECLARED_US
    wind = [e for e in store.elements(runs[0]["revision_id"], parameter="windSpeed")]
    assert (
        wind[0]["value"] == "10" and "value" not in wind[1]
    )  # a range stays unpublished as a number
    alerts = {
        r["content"]["msg_type"]: r["content"]
        for r in currents(store, "warning", provider="nws")
    }
    assert alerts["Update"]["references"][0]["identifier"] == fb.N1
    source, pages = h.nws_alerts("first")
    adapter = ws_src.WeatherSourceAdapter(
        {**source, "weather": {**source["weather"], "user_agent": "ua/1"}},
        transport=ws_src.fixture_transport(pages),
    )
    page = adapter.fetch_page({"operation": "observe", "parameters": {}}, cursor=None)
    assert page.receipt["request"]["user_agent"] == "ua/1"


def test_open_meteo_runs_are_issuance_vintages_labelled_as_model_grid_values(world):
    conn, store = world
    runs = currents(store, "forecast_issuance", provider="open-meteo")
    assert [r["content"]["issued_at"] for r in runs] == [
        "2026-06-10T00:00:00Z",
        "2026-06-10T03:00:00Z",
    ]
    content = runs[0]["content"]
    assert (
        content["location"]["kind"] == "grid-point"
        and "DWD ICON-D2" in content["originator"]
    )
    assert (
        content["attribution"] == "Open-Meteo (CC BY 4.0)"
        and content["kind"] == "forecast"
    )
    again = h.acquire(conn, "2026-06-10T11:00:00Z", lambda: h.open_meteo(1781060400))
    assert again[1]["outcome"]["unchanged"] == 1


def test_unbounded_selections_and_foreign_hosts_are_refused():
    with pytest.raises(ProviderError):
        ws_src.plan(
            "dwd-cdc", {"stations": ["433"], "resolution": "hourly", "period": "recent"}
        )
    with pytest.raises(ProviderError):
        ws_src.plan("dwd-mosmix", {"stations": ["10999"], "elements": ["XYZ"]})
    with pytest.raises(ProviderError):
        ws_src.plan("nws", {"points": [{"latitude": "1", "longitude": "2"}]})
    with pytest.raises(ProviderError):
        ws_src.plan(
            "dwd-cdc",
            {
                "stations": [fb.DWD],
                "resolution": "10_minutes",
                "period": "recent",
                "parameter": "precipitation",
            },
        )


def test_items_never_inherit_values_from_the_previous_item():
    body = json.loads(fb.metar("first"))
    body[2] = {k: v for k, v in body[2].items() if k not in {"temp", "dewp"}}
    records, _ = ws_src.parse_awc_metar(
        json.dumps(body).encode(),
        {"stations": [fb.ICAO, fb.US_ICAO]},
        url="https://aviationweather.gov/api/data/metar",
    )
    us = next(r for r in records if r["station"]["native_id"] == fb.US_ICAO)
    temp = next(p for p in us["parameters"] if p["parameter"] == "temp")
    assert "value" not in temp and temp["missing"] == "not reported"


def test_nws_grid_mapping_change_fails_closed():
    context = {"grid": {"wfo": "ZZX", "x": "12", "y": "34"}, "point": ["1", "2"]}
    body = json.dumps(
        {"properties": {"gridId": "ZZX", "gridX": 12, "gridY": 35}}
    ).encode()
    with pytest.raises(ProviderError):
        ws_src.parse_nws_points(body, context, url="https://api.weather.gov/points/1,2")


def test_production_pack_validates_and_replays_offline():
    manifest = validate_source_pack(json.loads(h.PACK.read_text()))
    assert manifest["domains"] == ["weather"] and len(manifest["sources"]) == 9
    report = SourcePackConformance(h.ROOT).offline(json.loads(h.PACK.read_text()))
    assert all(r["valid"] for r in report["sources"]), report
