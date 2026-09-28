"""WX02 (#2165): record constructors, validation, no Noesis-made forecasts, explicit missing values, time handling."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.kb import weather_records as wr
from src.kb import weather_store as ws

ROOT = Path(__file__).resolve().parents[3]
NS = "weather-test"
SCOPES = {
    wr.READ_SCOPE,
    wr.WRITE_SCOPE,
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
    "knowledge:environment:read",
    "knowledge:environment:write",
    "namespace:environment:read",
    "namespace:environment:write",
}
LOC = {"url": "https://opendata.dwd.de/fixture/file.zip", "pointer": "line 2"}
STATION = {"provider": "dwd", "native_id": "99901"}


def report(value="12.3", qn="3", observed="2026-05-01T10:00:00Z", **kwargs):
    params = [
        {
            "parameter": "TT_10",
            "unit": "°C",
            "value": value,
            "qc": {"scheme": "dwd-qn", "native": qn},
        }
    ]
    return wr.observation_report(
        "dwd-cdc",
        STATION,
        observed,
        "dwd-10min",
        parameters=params,
        locator=LOC,
        **kwargs,
    )


def test_missing_values_are_absent_never_none_and_unknowns_are_listed():
    item = wr.parameter(
        "TT_10", unit="°C", qc={"scheme": "dwd-qn", "native": "3"}, missing="-999"
    )
    assert "value" not in item and item["missing"] == "-999"
    assert "None" not in json.dumps(item)
    vintage = wr.location_vintage(
        "dwd-cdc",
        STATION,
        latitude="52.1",
        longitude="13.2",
        valid_from="2001-01-01",
        locator=LOC,
    )
    assert (
        vintage["open_ended"] is True
        and "valid_to" not in vintage
        and "elevation_m" in vintage["unknowns"]
    )
    with pytest.raises(wr.WeatherRecordError):
        wr.parameter("TT_10", unit="°C", qc={"scheme": "dwd-qn"}, value="None")
    with pytest.raises(wr.WeatherRecordError):
        wr.parameter(
            "TT_10", unit="°C", qc={"scheme": "dwd-qn"}, value=12.3
        )  # floats are rejected


def test_times_are_utc_iso_and_offsetless_times_are_rejected():
    assert wr.utc("2026-05-01T12:00:00+02:00") == "2026-05-01T10:00:00Z"
    assert wr.utc(1777629600) == "2026-05-01T10:00:00Z"
    assert wr.ms("2026-05-01T10:00:00Z") - wr.ms("2026-05-01") == 10 * 3600 * 1000
    with pytest.raises(wr.WeatherRecordError):
        wr.utc("2026-05-01T12:00:00")
    with pytest.raises(wr.WeatherRecordError):
        report(observed="2026-05-01 10:00")


def test_no_record_can_hold_a_noesis_made_or_post_processed_forecast():
    element = {
        "parameter": "TTT",
        "valid_time": "2026-05-01T12:00:00Z",
        "unit": "K",
        "value": "285.1",
    }
    location = {
        "kind": "station",
        "ref": "dwd-mosmix:P9901",
        "station": {"provider": "dwd-mosmix", "native_id": "P9901"},
    }
    issuance = wr.forecast_issuance(
        "dwd-mosmix",
        "MOSMIX_L",
        location,
        "2026-05-01T03:00:00Z",
        run_id="r1",
        elements=[element],
        locator=LOC,
    )
    assert (
        issuance["issuer"] == "Deutscher Wetterdienst"
        and issuance["elements"][0]["lead_time_s"] == 9 * 3600
    )
    for field in ("bias_corrected", "blended", "producer", "reforecast"):
        with pytest.raises(wr.WeatherRecordError) as err:
            wr.forecast_issuance(
                "dwd-mosmix",
                "MOSMIX_L",
                location,
                "2026-05-01T03:00:00Z",
                run_id="r1",
                elements=[element],
                locator=LOC,
                **{field: True},
            )
        assert err.value.code == "not_published_data"
    forged = {**issuance, "issuer": "Noesis"}
    with pytest.raises(wr.WeatherRecordError):
        wr.validate(forged)
    with pytest.raises(wr.WeatherRecordError):
        wr.forecast_issuance(
            "noesis",
            "MOSMIX_L",
            location,
            "2026-05-01T03:00:00Z",
            run_id="r1",
            elements=[element],
            locator=LOC,
        )


def test_cap_references_and_update_without_reference_is_rejected():
    refs = wr.parse_cap_references(
        "opendata@dwd.de,2.49.0.0.276.0.DWD.PVW.1,2026-05-01T10:00:00+02:00"
    )
    assert refs == [
        {
            "sender": "opendata@dwd.de",
            "identifier": "2.49.0.0.276.0.DWD.PVW.1",
            "sent": "2026-05-01T08:00:00Z",
        }
    ]
    area = [
        {
            "description": "Musterkreis",
            "codes": [{"scheme": "WARNCELLID", "value": "199901000"}],
        }
    ]
    with pytest.raises(wr.WeatherRecordError):
        wr.warning(
            "dwd-cap",
            "id-2",
            "opendata@dwd.de",
            "2026-05-01T09:00:00Z",
            "Update",
            locator=LOC,
            event="FROST",
            severity="Minor",
            urgency="Immediate",
            certainty="Likely",
            areas=area,
        )
    alert = wr.warning(
        "dwd-cap",
        "id-1",
        "opendata@dwd.de",
        "2026-05-01T08:00:00Z",
        "Alert",
        locator=LOC,
        event="FROST",
        severity="Minor",
        urgency="Immediate",
        certainty="Likely",
        areas=area,
        instruction="Issuer text",
    )
    assert (
        alert["quoted_as_issued"] is True
        and "onset" in alert["unknowns"]
        and alert["instruction"] == "Issuer text"
    )


def test_every_record_type_round_trips_through_validation_and_the_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / wr.SCHEMA_PATH).read_text())
    area = [
        {
            "codes": [{"scheme": "UGC", "value": "XXZ001"}],
            "polygons": [[[1.0, 1.0], [1.0, 2.0], [2.0, 2.0], [1.0, 1.0]]],
        }
    ]
    records = [
        wr.location_vintage(
            "dwd-cdc",
            STATION,
            latitude="52.1",
            longitude="13.2",
            valid_from="2001-01-01",
            valid_to="2010-05-31",
            elevation_m="48",
            name="Musterstadt",
            locator=LOC,
        ),
        report(correction="COR"),
        wr.forecast_issuance(
            "nws",
            "nws-gridpoint-hourly",
            {
                "kind": "grid-cell",
                "ref": "nws:XXX/1,2",
                "geometry": {"type": "Polygon", "coordinates": area[0]["polygons"]},
                "declared_station": {
                    "provider": "aviationweather",
                    "native_id": "KXYZ",
                },
            },
            "2026-05-01T03:00:00Z",
            run_id="u1",
            locator=LOC,
            elements=[
                {
                    "parameter": "temperature",
                    "valid_time": "2026-05-01T04:00:00Z",
                    "unit": "F",
                    "value": "50",
                }
            ],
        ),
        wr.warning(
            "nws",
            "urn:x:1",
            "w-nws.webmaster@noaa.gov",
            "2026-05-01T08:00:00Z",
            "Alert",
            locator=LOC,
            event="Frost Advisory",
            severity="Minor",
            urgency="Expected",
            certainty="Likely",
            areas=area,
            onset="2026-05-02T04:00:00Z",
            expires="2026-05-02T12:00:00Z",
        ),
    ]
    for record in records:
        assert wr.validate(json.loads(json.dumps(record))) == record
        jsonschema.validate(record, schema)


def test_store_round_trip_revision_append_and_idempotency():
    conn = duckdb.connect()
    store = ws.WeatherStore(conn, now=lambda: 1_000)
    first = store.apply(
        NS,
        [report()],
        run_id="r1",
        principal_id="p",
        scopes=SCOPES,
        retrieved_at_ms=1_000,
    )
    again = store.apply(
        NS,
        [report()],
        run_id="r2",
        principal_id="p",
        scopes=SCOPES,
        retrieved_at_ms=2_000,
    )
    assert (
        first["revisions"] == 1 and again["unchanged"] == 1 and again["revisions"] == 0
    )
    changed = store.apply(
        NS,
        [report(value="12.4")],
        run_id="r3",
        principal_id="p",
        scopes=SCOPES,
        retrieved_at_ms=3_000,
    )
    assert changed["corrections"] == 1
    rid = ws.record_id(NS, "observation_report", report()["record_key"])
    current = store.current(NS, rid, scopes=SCOPES)
    assert (
        current["content"]["parameters"][0]["value"] == "12.4"
        and current["revision_count"] == 2
    )
    as_of = store.current(NS, rid, scopes=SCOPES, cutoff_ms=2_500)
    assert as_of["content"]["parameters"][0]["value"] == "12.3"
    assert [h["change_kind"] for h in current["history"]] == ["initial", "correction"]


def test_reversion_is_a_new_correction_and_qc_change_is_a_correction_not_a_conflict():
    conn = duckdb.connect()
    store = ws.WeatherStore(conn)
    rid = ws.record_id(NS, "observation_report", report()["record_key"])
    for n, (value, qn) in enumerate(
        [("1.0", "3"), ("2.0", "3"), ("1.0", "3"), ("1.0", "10")], start=1
    ):
        store.apply(
            NS,
            [report(value=value, qn=qn)],
            run_id=f"r{n}",
            principal_id="p",
            scopes=SCOPES,
            retrieved_at_ms=n * 1000,
        )
    kinds = [h["change_kind"] for h in store.current(NS, rid, scopes=SCOPES)["history"]]
    assert kinds == ["initial", "correction", "correction", "qc_change"]


def test_late_older_data_is_history_and_never_current():
    conn = duckdb.connect()
    store = ws.WeatherStore(conn)
    historical = report(
        value="5.1", qn="10", precedence=1, release={"period": "historical"}
    )
    recent = report(value="5.0", qn="1", precedence=0, release={"period": "recent"})
    store.apply(
        NS,
        [historical],
        run_id="h",
        principal_id="p",
        scopes=SCOPES,
        retrieved_at_ms=1000,
    )
    outcome = store.apply(
        NS,
        [recent],
        run_id="late",
        principal_id="p",
        scopes=SCOPES,
        retrieved_at_ms=2000,
    )
    assert outcome["late_history"] == 1 and outcome["corrections"] == 0
    rid = ws.record_id(NS, "observation_report", report()["record_key"])
    current = store.current(NS, rid, scopes=SCOPES)
    assert current["content"]["parameters"][0]["value"] == "5.1"
    assert [h["change_kind"] for h in current["history"]] == ["initial", "late_history"]


def test_unauthorized_and_not_ready_are_explicit():
    conn = duckdb.connect()
    assert not ws.ready(conn, NS)
    with pytest.raises(ws.WeatherError) as err:
        ws.require_ready(conn, NS)
    assert err.value.code == "not_ready"
    store = ws.WeatherStore(conn)
    with pytest.raises(ws.WeatherError) as err:
        store.apply(
            NS, [report()], run_id="r", principal_id="p", scopes={wr.READ_SCOPE}
        )
    assert err.value.code == "unauthorized"


def test_schema_registers_in_the_shared_registry():
    conn = duckdb.connect()
    registered = wr.register_schemas(
        conn, principal_id="svc", scopes={"knowledge:schema:register"}
    )
    assert registered and registered[0]["name"] == "weather-record"
