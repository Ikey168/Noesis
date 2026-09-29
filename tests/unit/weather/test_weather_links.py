"""WX10 (#2173): citations between Weather and Climate & Environment records, never duplicated owners."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.kb.environment_places import EnvironmentDossiers
from src.kb.environment_store import EnvironmentStore
from src.kb.weather_links import WeatherClimateLinks
from src.kb.weather_queries import WeatherQueries
from src.kb.weather_store import WeatherError, WeatherStore
from tests.unit.weather import fixture_builder as fb
from tests.unit.weather import harness as h

ROOT = Path(__file__).resolve().parents[3]
WEATHER_MODULES = sorted(
    [
        *ROOT.glob("src/kb/weather_*.py"),
        ROOT / "src/ingestion/weather_sources.py",
        ROOT / "tools/knowledge_engine_mcp/weather.py",
    ]
)


@pytest.fixture(scope="module")
def world():
    conn = h.connection()
    climate = h.climate_pack_acquires_the_same_station(conn)
    assert (
        climate["revisions"] >= 3
    )  # the station and its two hourly series, owned by the Climate pack
    h.acquire_all(conn, until="2026-06-10T14:05:00Z")
    h.acquire(conn, "2026-06-10T14:10:00Z", h.dwd_hourly_tu)
    return conn


def test_a_station_in_both_packs_is_cited_and_its_series_are_not_recreated(world):
    env = EnvironmentStore(world, initialize=False)
    series = env.records(h.ENV_NS, scopes=h.SCOPES, record_type="observation_series")
    assert sorted(r["native_id"] for r in series) == [
        f"{fb.DWD}:RF_TU",
        f"{fb.DWD}:TT_TU",
    ]
    assert len(env.records(h.ENV_NS, scopes=h.SCOPES, record_type="station")) >= 1
    station = env.find(h.ENV_NS, "station", "dwd", fb.DWD)
    before = env.record(h.ENV_NS, station, scopes=h.SCOPES)["revision"]
    result = WeatherQueries(world).observations(
        h.NS,
        scopes=h.SCOPES,
        principal_id=h.PRINCIPAL,
        station=f"dwd:{fb.DWD}",
        window_from="2026-06-10T12:00:00Z",
        window_to="2026-06-10T13:00:00Z",
    )
    hourly = next(
        r
        for r in result["reports"]
        if r["report_type"] == "dwd-hourly" and r["parameters"][0]["parameter"] != "R1"
    )
    refs = hourly["environment"]
    assert refs["station_record_id"] == station
    assert set(refs["series"]) == {"TT_TU", "RF_TU"}
    assert refs["series"]["TT_TU"] == env.find(
        h.ENV_NS, "observation_series", "dwd", f"{fb.DWD}:TT_TU"
    )
    assert (
        env.record(h.ENV_NS, station, scopes=h.SCOPES)["revision"] == before
    )  # the owner's station untouched


def test_warnings_attach_to_an_environment_dossier_reviewably_and_revertibly(world):
    dossiers = EnvironmentDossiers(world)
    world.execute(
        "INSERT INTO environment_dossiers VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            "env-dossier:fixture",
            h.ENV_NS,
            "climate-user",
            "h",
            None,
            None,
            "{}",
            "{}",
            "c",
            1,
        ],
    )
    del dossiers
    warning = WeatherStore(world, initialize=False).currents(
        h.NS, record_type="warning", scopes=h.SCOPES, provider="dwd-cap"
    )[0]
    links = WeatherClimateLinks(world)
    link = links.attach(
        h.NS,
        revision_id=warning["revision_id"],
        target_kind="environment-dossier",
        target_id="env-dossier:fixture",
        note="frost warning beside the place dossier",
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
    )
    assert (
        link["state"] == "proposed"
        and link["citation"]["issuer"] == "Deutscher Wetterdienst"
    )
    assert (
        links.for_target(
            h.NS, "environment-dossier", "env-dossier:fixture", scopes=h.SCOPES
        )
        == []
    )
    with pytest.raises(WeatherError):
        links.review(
            h.NS,
            link["link_id"],
            "accept",
            "ok",
            principal_id=h.PRINCIPAL,
            scopes=h.REVIEWER_SCOPES,
        )
    links.review(
        h.NS,
        link["link_id"],
        "accept",
        "relevant",
        principal_id=h.REVIEWER,
        scopes=h.REVIEWER_SCOPES,
    )
    assert (
        len(
            links.for_target(
                h.NS, "environment-dossier", "env-dossier:fixture", scopes=h.SCOPES
            )
        )
        == 1
    )
    reverted = links.revert(
        h.NS,
        link["link_id"],
        "wrong place",
        principal_id=h.REVIEWER,
        scopes=h.REVIEWER_SCOPES,
    )
    assert reverted["state"] == "reverted"
    assert (
        links.for_target(
            h.NS, "environment-dossier", "env-dossier:fixture", scopes=h.SCOPES
        )
        == []
    )
    with pytest.raises(WeatherError) as err:
        links.attach(
            h.NS,
            revision_id=warning["revision_id"],
            target_kind="event-record",
            target_id="event:none",
            note="x",
            principal_id=h.PRINCIPAL,
            scopes=h.SCOPES,
        )
    assert err.value.code == "not_found"


def test_no_weather_module_writes_environment_records_other_than_registered_stations(
    world,
):
    written = re.compile(r"(INSERT|UPDATE|DELETE)[^\"']*\benvironment_", re.IGNORECASE)
    for path in WEATHER_MODULES:
        if path.exists():
            assert not written.search(path.read_text()), path
    apply_calls = [
        p for p in WEATHER_MODULES if p.exists() and "env.apply(" in p.read_text()
    ]
    assert [p.name for p in apply_calls] == ["weather_store.py"]
    kinds = {
        r[0]
        for r in world.execute(
            "SELECT DISTINCT record_type FROM environment_records "
            "WHERE record_id IN (SELECT record_id FROM environment_record_revisions "
            "WHERE principal_id=?)",
            [h.PRINCIPAL],
        ).fetchall()
    }
    assert kinds == {"station"}
    from src.kb import environment_records as er

    series = er.series(
        "dwd",
        f"{fb.DWD}:TT_TU",
        "copy attempt",
        source_url="https://opendata.dwd.de/x",
        location={"kind": "station", "ref": f"dwd:{fb.DWD}"},
        indicator={"code": "TT_TU", "name": "t"},
        unit="°C",
        interval="PT1H",
        aggregation="instant",
        kind="observation",
        values=[],
    )
    with pytest.raises(WeatherError) as err:
        WeatherStore(world, initialize=False).register_stations(
            h.ENV_NS, [series], run_id="x", principal_id=h.PRINCIPAL, scopes=h.SCOPES
        )
    assert err.value.code == "boundary"
