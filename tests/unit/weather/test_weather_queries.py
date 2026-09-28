"""WX08 (#2171): as-of observations across a relocation, forecast as issued and warnings in force."""

from __future__ import annotations

import pytest

from src.kb.weather_identity import WeatherStationIdentity
from src.kb.weather_queries import WeatherQueries
from src.kb.weather_store import WeatherError
from tests.unit.weather import fixture_builder as fb
from tests.unit.weather import harness as h

PLACE = {"point": [13.53, 52.38], "radius_m": 5000}
US_PLACE = {"point": [-70.0, 41.0], "radius_m": 5000}


@pytest.fixture(scope="module")
def world():
    conn = h.connection()
    h.acquire_all(conn)
    identity = WeatherStationIdentity(conn)
    candidate = next(
        c
        for c in identity.propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)[
            "candidates"
        ]
        if (c["left"], c["right"]) == (f"dwd-mosmix:{fb.MOSMIX}", f"dwd:{fb.DWD}")
    )
    identity.review(
        h.NS,
        candidate["candidate_id"],
        "accept",
        "same airfield",
        principal_id=h.REVIEWER,
        scopes=h.REVIEWER_SCOPES,
    )
    return conn, WeatherQueries(conn)


def test_observations_at_a_place_span_the_relocation_with_the_vintage_used(world):
    _, queries = world
    result = queries.observations(
        h.NS,
        scopes=h.SCOPES,
        principal_id=h.PRINCIPAL,
        place=PLACE,
        window_from="2026-05-31T00:00:00Z",
        window_to="2026-06-10T13:00:00Z",
        knowledge_cutoff="2026-06-20T00:00:00Z",
    )
    dwd = [
        r
        for r in result["reports"]
        if r["station"] == f"dwd:{fb.DWD}" and r["report_type"] == "dwd-10min"
    ]
    assert [(r["observed_at"], r["location_vintage"]["valid_from"]) for r in dwd] == [
        ("2026-05-31T12:00:00Z", "1990-01-01"),
        ("2026-06-10T12:00:00Z", "2026-06-01"),
        ("2026-06-10T13:00:00Z", "2026-06-01"),
    ]
    assert all(r["location_vintage"]["proximity_receipt_id"] for r in dwd)
    noon = dwd[1]["parameters"]
    tt = next(p for p in noon if p["parameter"] == "TT_10")
    assert (
        tt["value"] == "18.4"
        and tt["qc"]["native"] == "1"
        and tt["qc"]["common"] == "provisional"
    )
    assert tt["common_parameter"] == "air_temperature"
    metar = next(
        r
        for r in result["reports"]
        if r["station"] == f"aviationweather:{fb.ICAO}"
        and r["observed_at"] == "2026-06-10T11:50:00Z"
    )
    assert metar["correction"] == "COR" and metar["raw_text"].startswith("METAR COR")
    temp = next(p for p in metar["parameters"] if p["parameter"] == "temp")
    assert temp["normalised"]["value"] == "19" and temp["normalised"]["unit"] == "°C"
    assert result["knowledge_cutoff"] == "2026-06-20T00:00:00Z" and result["n"] == len(
        result["reports"]
    )


def test_as_of_observations_use_only_what_was_acquired_by_the_cutoff(world):
    _, queries = world
    before_cor = queries.observations(
        h.NS,
        scopes=h.SCOPES,
        principal_id=h.PRINCIPAL,
        station=f"aviationweather:{fb.ICAO}",
        window_from="2026-06-10T11:00:00Z",
        window_to="2026-06-10T12:00:00Z",
        knowledge_cutoff="2026-06-10T12:10:00Z",
    )
    assert [r["raw_text"] for r in before_cor["reports"]] == [
        "METAR EDXM 101150Z 24008KT 9999 FEW030 18/09 Q1015"
    ]
    later = queries.observations(
        h.NS,
        scopes=h.SCOPES,
        principal_id=h.PRINCIPAL,
        station=f"dwd:{fb.DWD}",
        window_from="2026-06-10T12:00:00Z",
        window_to="2026-06-10T12:00:00Z",
    )
    tt = next(
        p
        for r in later["reports"]
        if r["report_type"] == "dwd-10min"
        for p in r["parameters"]
        if p["parameter"] == "TT_10"
    )
    assert tt["value"] == "18.6" and tt["qc"]["common"] == "passed"
    nothing = queries.observations(
        h.NS,
        scopes=h.SCOPES,
        principal_id=h.PRINCIPAL,
        station=f"dwd:{fb.DWD}",
        window_from="2026-01-01T00:00:00Z",
        window_to="2026-01-02T00:00:00Z",
    )
    assert nothing["status"] == "no report on record" and nothing["reports"] == []


def test_forecast_as_issued_takes_the_latest_issuance_before_the_cutoff_per_provider(
    world,
):
    _, queries = world
    result = queries.forecast_as_issued(
        h.NS,
        scopes=h.SCOPES,
        station=f"dwd:{fb.DWD}",
        valid_time="2026-06-10T13:00:00Z",
        issued_before="2026-06-10T11:00:00Z",
        parameter=None,
    )
    by = {(f["provider"], f["product"]): f for f in result["forecasts"]}
    assert by[("dwd-mosmix", "MOSMIX_L")]["issued_at"] == "2026-06-10T09:00:00Z"
    ttt = next(
        e for e in by[("dwd-mosmix", "MOSMIX_L")]["elements"] if e["parameter"] == "TTT"
    )
    assert (ttt["value"], ttt["lead_time_s"], ttt["normalised"]["value"]) == (
        "292.45",
        4 * 3600,
        "19.3",
    )
    assert by[("dwd-mosmix", "MOSMIX_L")]["match"]["rule"] == "equivalent-station"
    assert (
        by[("open-meteo", "open-meteo-forecast")]["issued_at"] == "2026-06-10T03:00:00Z"
    )
    assert (
        by[("open-meteo", "open-meteo-forecast")]["match"]["rule"]
        == "declared-grid-point"
    )
    early = queries.forecast_as_issued(
        h.NS,
        scopes=h.SCOPES,
        station=f"dwd:{fb.DWD}",
        valid_time="2026-06-10T13:00:00Z",
        issued_before="2026-06-10T08:00:00Z",
        knowledge_cutoff="2026-06-10T08:00:00Z",
    )
    # Both Open-Meteo runs were acquired after 08:00, so neither is known at that cutoff.
    assert {(f["provider"], f["issued_at"]) for f in early["forecasts"]} == {
        ("dwd-mosmix", "2026-06-10T03:00:00Z")
    }
    none = queries.forecast_as_issued(
        h.NS,
        scopes=h.SCOPES,
        station=f"dwd:{fb.DWD}",
        valid_time="2026-06-10T13:00:00Z",
        issued_before="2026-06-10T02:00:00Z",
    )
    assert none["status"] == "no forecast on record"


def test_forecast_evolution_lists_every_issuance_for_a_valid_time(world):
    _, queries = world
    result = queries.forecast_evolution(
        h.NS,
        scopes=h.SCOPES,
        station=f"dwd:{fb.DWD}",
        valid_time="2026-06-10T12:00:00Z",
        parameter="TTT",
    )
    assert [
        (i["issued_at"], i["elements"][0]["lead_time_s"]) for i in result["issuances"]
    ] == [
        ("2026-06-10T03:00:00Z", 9 * 3600),
        ("2026-06-10T09:00:00Z", 3 * 3600),
        ("2026-06-10T12:00:00Z", 0),
    ]
    us = queries.forecast_evolution(
        h.NS,
        scopes=h.SCOPES,
        place=US_PLACE,
        valid_time="2026-06-10T13:00:00Z",
        parameter="temperature",
    )
    assert [i["issued_at"] for i in us["issuances"]] == [
        "2026-06-10T09:00:00Z",
        "2026-06-10T11:00:00Z",
    ]


def test_warnings_in_force_follow_alert_update_and_cancel(world):
    _, queries = world

    def at(time, knowledge=None):
        return queries.warnings_in_force(
            h.NS,
            scopes=h.SCOPES,
            principal_id=h.PRINCIPAL,
            place=PLACE,
            as_of=time,
            knowledge_cutoff=knowledge,
        )

    before_onset = at("2026-06-10T10:00:00Z")
    assert (
        before_onset["in_force"] == []
        and before_onset["not_in_force"][0]["state"] == "not_yet_in_force"
    )
    evening = at("2026-06-10T21:00:00Z")
    assert len(evening["in_force"]) == 1
    message = evening["in_force"][0]["message"]
    assert message["msg_type"] == "Update" and message["severity"] == "Moderate"
    assert (
        message["instruction"] == "Text des Herausgebers (fiktiv)."
        and evening["containment"]["receipt_id"]
    )
    assert [m["msg_type"] for m in evening["in_force"][0]["chain"]] == [
        "Alert",
        "Update",
    ]
    cancelled = at("2026-06-11T03:00:00Z")
    assert (
        cancelled["in_force"] == []
        and cancelled["not_in_force"][0]["state"] == "cancelled"
    )
    # Known only up to 01:00: the cancellation had not been acquired, so the update was still in force.
    known = at("2026-06-11T03:00:00Z", knowledge="2026-06-11T01:00:00Z")
    assert known["in_force"] and known["knowledge_cutoff"] == "2026-06-11T01:00:00Z"
    elsewhere = queries.warnings_in_force(
        h.NS,
        scopes=h.SCOPES,
        principal_id=h.PRINCIPAL,
        place={"point": [10.0, 50.0]},
        as_of="2026-06-10T21:00:00Z",
    )
    assert elsewhere["status"] == "no warning on record in force"


def test_nws_update_supersedes_its_alert_in_any_arrival_order(world):
    conn, queries = world
    result = queries.warnings_in_force(
        h.NS,
        scopes=h.SCOPES,
        principal_id=h.PRINCIPAL,
        place=US_PLACE,
        as_of="2026-06-11T07:00:00Z",
    )
    assert (
        len(result["in_force"]) == 1
        and result["in_force"][0]["message"]["msg_type"] == "Update"
    )
    reordered = h.connection()
    h.acquire(reordered, "2026-06-10T22:30:00Z", lambda: h.nws_alerts("second"))
    h.acquire(reordered, "2026-06-10T23:00:00Z", lambda: h.nws_alerts("first"))
    again = WeatherQueries(reordered).warnings_in_force(
        h.NS,
        scopes=h.SCOPES,
        principal_id=h.PRINCIPAL,
        place=US_PLACE,
        as_of="2026-06-11T07:00:00Z",
    )
    assert [c["msg_type"] for c in again["in_force"][0]["chain"]] == ["Alert", "Update"]


def test_not_ready_before_any_source_ran_and_scope_is_required():
    queries = WeatherQueries(h.connection())
    with pytest.raises(WeatherError) as err:
        queries.observations(
            h.NS,
            scopes=h.SCOPES,
            principal_id=h.PRINCIPAL,
            station="dwd:99901",
            window_from="2026-01-01T00:00:00Z",
            window_to="2026-01-02T00:00:00Z",
        )
    assert err.value.code == "not_ready"
    with pytest.raises(WeatherError) as err:
        queries.observations(
            h.NS,
            scopes={"knowledge:geospatial:read"},
            principal_id=h.PRINCIPAL,
            station="dwd:99901",
            window_from="2026-01-01T00:00:00Z",
            window_to="2026-01-02T00:00:00Z",
        )
    assert err.value.code == "unauthorized"
