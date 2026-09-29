"""WX07 (#2170): units and QC normalisation, station identity by statements or review, places per vintage."""

from __future__ import annotations

import pytest

from src.kb import weather_records as wr
from src.kb.weather_identity import WeatherStationIdentity
from src.kb.weather_normalise import normalise, qc_common
from src.kb.weather_store import LOCATION_COLLECTION, WeatherError, WeatherStore
from tests.unit.weather import fixture_builder as fb
from tests.unit.weather import harness as h

LOC = {"url": "https://opendata.dwd.de/fixture"}


@pytest.fixture(scope="module")
def world():
    conn = h.connection()
    h.acquire_all(conn)
    return conn


def test_units_are_exact_without_pint_and_keep_native_values():
    for value, unit, expected, target in [
        ("68", "°F", "20", "°C"),
        ("293.15", "K", "20", "°C"),
        ("125", "1/10 °C", "12.5", "°C"),
        ("10", "kt", "5.144444", "m/s"),
        ("29.92", "inHg", "1013.207589", "hPa"),
        ("101325", "Pa", "1013.25", "hPa"),
        ("10", "SM", "16093.44", "m"),
        ("0.4", "kg/m2", "0.4", "mm"),
    ]:
        result = normalise(value, unit)
        assert (
            result["value"],
            result["unit"],
            result["native_value"],
            result["native_unit"],
        ) == (expected, target, value, unit)
        assert len(result["receipt_sha256"]) == 64
    assert normalise(None, "°C") is None and normalise("1", "furlong") is None


def test_qc_flags_map_to_the_common_vocabulary_and_keep_the_native_flag():
    assert qc_common("dwd-qn", "10")["common"] == "passed"
    assert qc_common("dwd-qn", "1")["common"] == "provisional"
    assert qc_common("dwd-qn", "3") == {
        "scheme": "dwd-qn",
        "common": "checked_partial",
        "native": "3",
        "meaning": "automatic control and correction (ROUTINE)",
    }
    assert qc_common("dwd-qn", "4")["common"] == "unknown"
    assert (
        qc_common("awc-qcfield", "0", raw_text="METAR X 101220Z Q1015 $")["common"]
        == "suspect"
    )
    assert (
        qc_common("awc-qcfield", "0", raw_text="METAR X 101220Z Q1015")["common"]
        == "unknown"
    )


def test_relocated_station_keeps_one_identity_with_a_place_per_vintage(world):
    identity = WeatherStationIdentity(world, initialize=False)
    sites = identity.sites(h.NS, f"dwd:{fb.DWD}", scopes=h.SCOPES)
    assert (
        len(sites) == 1
        and len(sites[0]["vintages"]) == 2
        and sites[0]["relocations"][0]["to"] == "2026-06-01"
    )
    natives = {
        r[0]
        for r in world.execute(
            "SELECT native_id FROM geospatial_features WHERE collection=?",
            [LOCATION_COLLECTION],
        ).fetchall()
    }
    assert natives == {f"dwd:{fb.DWD}@1990-01-01", f"dwd:{fb.DWD}@2026-06-01"}


def test_source_stated_identifiers_link_and_proximity_only_proposes(world):
    identity = WeatherStationIdentity(world, initialize=False)
    stated = identity.stated_links(h.NS)
    assert [(link["left"], link["right"], link["scheme"]) for link in stated] == [
        (f"aviationweather:{fb.ICAO}", f"dwd-mosmix:{fb.MOSMIX}", "icao")
    ]
    before = identity.equivalent(h.NS, f"dwd:{fb.DWD}", scopes=h.SCOPES)
    assert before["members"] == [f"dwd:{fb.DWD}"]
    proposed = identity.propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    pairs = {(c["left"], c["right"]): c for c in proposed["candidates"]}
    assert (f"dwd-mosmix:{fb.MOSMIX}", f"dwd:{fb.DWD}") in pairs
    assert all(c["state"] == "proposed" for c in pairs.values())
    assert {tuple(s["pair"]) for s in proposed["skipped"]} == {
        (f"aviationweather:{fb.ICAO}", f"dwd-mosmix:{fb.MOSMIX}")
    }
    candidate = pairs[(f"dwd-mosmix:{fb.MOSMIX}", f"dwd:{fb.DWD}")]
    assert (
        candidate["inbox_target"]["kind"] == "entity"
        and candidate["inbox_target"]["id"]
    )
    with pytest.raises(WeatherError) as err:
        identity.review(
            h.NS,
            candidate["candidate_id"],
            "accept",
            "same site",
            principal_id=h.PRINCIPAL,
            scopes=h.REVIEWER_SCOPES,
        )
    assert err.value.code == "self_review"
    accepted = identity.review(
        h.NS,
        candidate["candidate_id"],
        "accept",
        "same airfield per coordinates",
        principal_id=h.REVIEWER,
        scopes=h.REVIEWER_SCOPES,
    )
    assert accepted["state"] == "accepted"
    members = identity.equivalent(h.NS, f"dwd:{fb.DWD}", scopes=h.SCOPES)["members"]
    assert members == [
        f"aviationweather:{fb.ICAO}",
        f"dwd-mosmix:{fb.MOSMIX}",
        f"dwd:{fb.DWD}",
    ]
    again = identity.propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    assert again["created"] == []  # re-proposing never touches a reviewed candidate


def test_revert_never_reactivates_and_the_lookup_follows_it():
    conn = h.connection()
    h.acquire_all(conn, until="2026-06-10T14:05:00Z")
    identity = WeatherStationIdentity(conn, initialize=False)
    cid = next(
        c["candidate_id"]
        for c in identity.propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)[
            "candidates"
        ]
        if c["left"] == f"dwd-mosmix:{fb.MOSMIX}" and c["right"] == f"dwd:{fb.DWD}"
    )
    identity.review(
        h.NS,
        cid,
        "accept",
        "same site",
        principal_id=h.REVIEWER,
        scopes=h.REVIEWER_SCOPES,
    )
    reverted = identity.revert(
        h.NS,
        cid,
        "coordinates were a catalogue error",
        principal_id=h.REVIEWER,
        scopes=h.REVIEWER_SCOPES,
    )
    assert reverted["state"] == "reverted"
    assert identity.equivalent(h.NS, f"dwd:{fb.DWD}", scopes=h.SCOPES)["members"] == [
        f"dwd:{fb.DWD}"
    ]
    with pytest.raises(WeatherError):
        identity.review(
            h.NS,
            cid,
            "accept",
            "again",
            principal_id=h.REVIEWER,
            scopes=h.REVIEWER_SCOPES,
        )


def _stations(conn, records):
    store = WeatherStore(conn)
    store.apply(
        h.NS,
        records,
        run_id="direct",
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
        retrieved_at_ms=1_000,
    )


def test_two_nearby_distinct_stations_are_never_merged_and_a_reused_id_is_split():
    conn = h.connection()
    records = []
    for native, lat, lon in (
        ("99901", "52.3810", "13.5290"),
        ("99902", "52.3818", "13.5296"),
    ):
        records.append(
            wr.location_vintage(
                "dwd-cdc",
                {"provider": "dwd", "native_id": native},
                latitude=lat,
                longitude=lon,
                valid_from="2000-01-01",
                locator=LOC,
            )
        )
    records.append(
        wr.location_vintage(
            "dwd-cdc",
            {"provider": "dwd", "native_id": "99903"},
            latitude="51.0000",
            longitude="10.0000",
            valid_from="1950-01-01",
            valid_to="2001-12-31",
            name="Altdorf",
            locator=LOC,
        )
    )
    records.append(
        wr.location_vintage(
            "dwd-cdc",
            {"provider": "dwd", "native_id": "99903"},
            latitude="53.5000",
            longitude="9.0000",
            valid_from="2005-01-01",
            name="Neudorf",
            locator=LOC,
        )
    )
    records.append(
        wr.environment_station(
            "dwd-mosmix",
            "10999",
            "MUSTERSTADT",
            source_url=LOC["url"],
            longitude=13.5288,
            latitude=52.3812,
            identifiers={"mosmix_id": "10999"},
        )
    )
    records.append(
        wr.location_vintage(
            "dwd-cdc",
            {"provider": "dwd", "native_id": "99904"},
            latitude="53.5001",
            longitude="9.0001",
            valid_from="2005-01-01",
            locator=LOC,
        )
    )
    _stations(conn, records)
    identity = WeatherStationIdentity(conn)
    proposed = identity.propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    pairs = {(c["left"], c["right"]) for c in proposed["candidates"]}
    assert ("dwd:99901", "dwd:99902") not in pairs and (
        "dwd:99903",
        "dwd:99904",
    ) not in pairs
    assert {
        ("dwd-mosmix:10999", "dwd:99901"),
        ("dwd-mosmix:10999", "dwd:99902"),
    } <= pairs
    by_pair = {
        (c["left"], c["right"]): c["candidate_id"] for c in proposed["candidates"]
    }
    identity.review(
        h.NS,
        by_pair[("dwd-mosmix:10999", "dwd:99901")],
        "accept",
        "same site",
        principal_id=h.REVIEWER,
        scopes=h.REVIEWER_SCOPES,
    )
    identity.review(
        h.NS,
        by_pair[("dwd-mosmix:10999", "dwd:99902")],
        "reject",
        "different mast",
        principal_id=h.REVIEWER,
        scopes=h.REVIEWER_SCOPES,
    )
    assert identity.equivalent(h.NS, "dwd:99902", scopes=h.SCOPES)["members"] == [
        "dwd:99902"
    ]
    assert (
        "dwd:99902"
        not in identity.equivalent(h.NS, "dwd:99901", scopes=h.SCOPES)["members"]
    )
    sites = identity.sites(h.NS, "dwd:99903", scopes=h.SCOPES)
    assert len(sites) == 2 and "reused id" in sites[1]["basis"]


def test_stronger_evidence_upgrades_a_proposed_candidate():
    conn = h.connection()
    records = [
        wr.environment_station(
            "dwd-mosmix",
            "10999",
            "M",
            source_url=LOC["url"],
            longitude=13.5288,
            latitude=52.3812,
            identifiers={"mosmix_id": "10999"},
        ),
        wr.environment_station(
            "aviationweather",
            "EDXM",
            "M",
            source_url=LOC["url"],
            longitude=13.53,
            latitude=52.3805,
            identifiers={"icao": "EDXM"},
        ),
        wr.location_vintage(
            "dwd-cdc",
            {"provider": "dwd", "native_id": "99901"},
            latitude="52.3810",
            longitude="13.5290",
            valid_from="2000-01-01",
            locator=LOC,
        ),
    ]
    _stations(conn, records)
    store = WeatherStore(conn)
    store.apply(
        h.NS,
        [
            {
                "contract": "noesis-weather-station-identifier-v1",
                "station": {"provider": "aviationweather", "native_id": "EDXM"},
                "scheme": "any",
                "value": "X",
                "stated_by": "t",
                "locator": LOC,
            }
        ],
        run_id="i",
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
        retrieved_at_ms=2_000,
    )
    identity = WeatherStationIdentity(conn)
    proposed = identity.propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    assert ("aviationweather:EDXM", "dwd-mosmix:10999") in {
        (c["left"], c["right"]) for c in proposed["candidates"]
    }
    store.apply(
        h.NS,
        [
            {
                "contract": "noesis-weather-station-identifier-v1",
                "station": {"provider": "dwd-mosmix", "native_id": "10999"},
                "scheme": "icao",
                "value": "EDXM",
                "stated_by": "dwd-mosmix-catalogue",
                "locator": LOC,
            }
        ],
        run_id="c",
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
        retrieved_at_ms=3_000,
    )
    upgraded = next(
        c
        for c in identity.candidates(h.NS, scopes=h.SCOPES)
        if (c["left"], c["right"]) == ("aviationweather:EDXM", "dwd-mosmix:10999")
    )
    assert upgraded["state"] == "superseded-by-source-statement"
    assert upgraded["basis_upgraded_to"] == "source-stated-identifier"
