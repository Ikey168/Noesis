"""Offline place-to-weather-record acceptance (WX13, #2176).

With sockets blocked: the first fictional stages run through the source-pack
runtime and its projector, and the rest go through the same adapter and
projector at their simulated acquisition times. The journey then covers unit
and QC normalisation, station identity review, place projection, as-of
observation, forecast-as-issued and warnings-in-force queries, verification with
hand-checked metrics, Climate & Environment cross-links and monitor events. It
asserts that nothing emits a Noesis-produced forecast or advice text and that no
environment record owner is duplicated. The production pack replays offline too.
"""

from __future__ import annotations

import json
import re
import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.ingestion.weather_sources import WeatherSourceAdapter, fixture_transport
from src.kb.environment_store import EnvironmentStore
from src.kb.subscriptions import SubscriptionStore
from src.kb.weather_identity import WeatherStationIdentity
from src.kb.weather_monitoring import WeatherMonitor
from src.kb.weather_queries import WeatherQueries
from src.kb.weather_store import WeatherStore
from src.kb.weather_verification import ForecastVerification
from tests.unit.weather import fixture_builder as fb
from tests.unit.weather import harness as h

PLACE = {"point": [13.53, 52.38], "radius_m": 5000}
RUNTIME_STAGES = {
    "dwd-mosmix-l": "2026-06-10T04:30:00Z",
    "dwd-cap-warnings": "2026-06-10T06:30:00Z",
}
ADVICE = re.compile(
    r"\b(you should|we recommend|stay indoors|avoid travel|do not fly|take shelter)\b",
    re.I,
)


def PUBLIC_DNS(
    _host,
):  # the runtime's resolver hook: a public address, so no private-network refusal
    return ["8.8.8.8"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError(
            "network access attempted during the offline acceptance run"
        )

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (
        dict(domain_registry._REGISTRY),
        set(domain_registry._ENABLED),
        domain_registry._AUTHORITY,
    )
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def runtime_first_stages(conn) -> dict:
    """The first MOSMIX run and CAP alert through SourcePackRuntime with fictional selections."""

    production = json.loads(h.PACK.read_text())
    fictional = {
        "dwd-mosmix-l": h.mosmix("2026-06-10T03:00:00.000Z"),
        "dwd-cap-warnings": h.dwd_cap("alert"),
    }
    sources = []
    for entry in production["sources"]:
        if entry["source_id"] in fictional:
            weather = fictional[entry["source_id"]][0]["weather"]
            sources.append(
                {
                    **entry,
                    "weather": {
                        **entry["weather"],
                        "namespace": h.NS,
                        "selection": weather["selection"],
                    },
                }
            )
    manifest = validate_source_pack({**production, "sources": sources})
    SourcePackStore(conn).install(
        manifest, principal_id="operator", enable=True, now_ms=10
    )
    start = h.ms("2026-06-10T06:30:00Z")
    clock = iter(range(start, start + 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    adapters = {}
    for source in manifest["sources"]:
        runtime.accept_license(
            manifest["pack_id"], source["source_id"], principal_id="operator"
        )
        adapters[source["source_id"]] = WeatherSourceAdapter(
            source, transport=fixture_transport(fictional[source["source_id"]][1])
        )
    return runtime.run(
        {
            "pack_id": manifest["pack_id"],
            "run_key": "weather-offline",
            "operation": "observe",
            "max_results": 5000,
            "max_bytes": 20_000_000,
            "timeout_ms": 60_000,
        },
        principal_id="operator",
        adapters=adapters,
        dns_resolver=PUBLIC_DNS,
    )


def strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, str):
        yield value


def test_place_to_cited_observations_forecasts_warnings_verification_links_and_monitors():
    conn = h.connection()
    h.climate_pack_acquires_the_same_station(
        conn
    )  # the Climate & Environment owner already holds station 99901
    receipt = runtime_first_stages(conn)
    assert {s["source_id"]: s["status"] for s in receipt["sources"]} == {
        "dwd-mosmix-l": "complete",
        "dwd-cap-warnings": "complete",
    }
    monitor = WeatherMonitor(conn)
    subscriptions = SubscriptionStore(conn)
    place_monitor = monitor.create(
        h.NS,
        "place",
        target={"kind": "place", "point": PLACE["point"]},
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
    )["subscription_id"]
    subscriptions.commit_watermark(h.NS, 1)
    assert (
        monitor.run(place_monitor, 1, principal_id=h.PRINCIPAL, scopes=h.SCOPES)[
            "baseline"
        ]
        is True
    )

    for at, stage in h.STAGES:
        if at in RUNTIME_STAGES.values():
            continue  # already acquired through the runtime
        h.acquire(conn, at, stage)
    h.acquire(conn, "2026-07-16T09:00:00Z", h.dwd_hourly_tu)
    before = conn.execute("SELECT count(*) FROM weather_revisions").fetchone()[0]
    for at, stage in h.STAGES:  # re-acquiring everything unchanged adds nothing
        h.acquire(conn, "2026-07-17T00:00:00Z", stage)
    assert (
        conn.execute("SELECT count(*) FROM weather_revisions").fetchone()[0] == before
    )

    # Identity: proximity only proposes; another principal accepts; source-stated ICAO links MOSMIX and METAR.
    identity = WeatherStationIdentity(conn)
    candidate = next(
        c
        for c in identity.propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES)[
            "candidates"
        ]
        if (c["left"], c["right"]) == (f"dwd-mosmix:{fb.MOSMIX}", f"dwd:{fb.DWD}")
    )
    assert candidate["state"] == "proposed"
    identity.review(
        h.NS,
        candidate["candidate_id"],
        "accept",
        "same airfield",
        principal_id=h.REVIEWER,
        scopes=h.REVIEWER_SCOPES,
    )
    assert identity.equivalent(h.NS, f"dwd:{fb.DWD}", scopes=h.SCOPES)["members"] == [
        f"aviationweather:{fb.ICAO}",
        f"dwd-mosmix:{fb.MOSMIX}",
        f"dwd:{fb.DWD}",
    ]

    queries = WeatherQueries(conn)
    observed = queries.observations(
        h.NS,
        scopes=h.SCOPES,
        principal_id=h.PRINCIPAL,
        place=PLACE,
        window_from="2026-05-31T00:00:00Z",
        window_to="2026-06-10T13:00:00Z",
    )
    dwd = [
        r
        for r in observed["reports"]
        if r["station"] == f"dwd:{fb.DWD}" and r["report_type"] == "dwd-10min"
    ]
    assert [r["location_vintage"]["valid_from"] for r in dwd] == [
        "1990-01-01",
        "2026-06-01",
        "2026-06-01",
    ]
    tt = next(p for p in dwd[1]["parameters"] if p["parameter"] == "TT_10")
    assert (tt["value"], tt["qc"]["native"], tt["qc"]["common"]) == (
        "18.6",
        "10",
        "passed",
    )
    # The later 'recent' copies restate the first revision below the historical one: nothing new is recorded.
    assert [x["change_kind"] for x in dwd[1]["history"]] == ["initial", "correction"]
    hourly = next(
        r
        for r in observed["reports"]
        if r["report_type"] == "dwd-hourly" and r["parameters"][0]["parameter"] != "R1"
    )
    assert hourly["environment"]["series"]["TT_TU"] == EnvironmentStore(
        conn, initialize=False
    ).find(h.ENV_NS, "observation_series", "dwd", f"{fb.DWD}:TT_TU")
    metar = next(
        r
        for r in observed["reports"]
        if r["station"] == f"aviationweather:{fb.ICAO}"
        and r["observed_at"] == "2026-06-10T11:50:00Z"
    )
    assert metar["correction"] == "COR"

    issued = queries.forecast_as_issued(
        h.NS,
        scopes=h.SCOPES,
        place=PLACE,
        valid_time="2026-06-10T13:00:00Z",
        issued_before="2026-06-10T11:00:00Z",
        knowledge_cutoff="2026-06-10T11:00:00Z",
    )
    assert {(f["provider"], f["issued_at"]) for f in issued["forecasts"]} == {
        ("dwd-mosmix", "2026-06-10T09:00:00Z"),
        ("open-meteo", "2026-06-10T03:00:00Z"),
    }
    evolution = queries.forecast_evolution(
        h.NS,
        scopes=h.SCOPES,
        station=f"dwd:{fb.DWD}",
        valid_time="2026-06-10T13:00:00Z",
        parameter="TTT",
    )
    assert [
        i["elements"][0]["lead_time_s"] // 3600 for i in evolution["issuances"]
    ] == [10, 4, 1]

    warnings = {
        t: queries.warnings_in_force(
            h.NS, scopes=h.SCOPES, principal_id=h.PRINCIPAL, place=PLACE, as_of=t
        )
        for t in ("2026-06-10T21:00:00Z", "2026-06-11T03:00:00Z")
    }
    assert (
        warnings["2026-06-10T21:00:00Z"]["in_force"][0]["message"]["msg_type"]
        == "Update"
    )
    assert warnings["2026-06-11T03:00:00Z"]["not_in_force"][0]["state"] == "cancelled"

    verification = ForecastVerification(conn).verify(
        h.NS,
        station=f"dwd:{fb.DWD}",
        parameter="air_temperature",
        providers=["dwd-mosmix"],
        period_from="2026-06-10T00:00:00Z",
        period_to="2026-06-10T23:00:00Z",
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
    )
    short = next(
        r for r in verification["by_provider_and_lead"] if r["lead_bucket"] == "0-6h"
    )
    assert (short["n"], short["bias"], short["mae"], short["rmse"]) == (
        2,
        "-0.1",
        "0.1",
        "0.141421",
    )
    assert (
        verification["excluded"]["captured_after_valid_time"] == 2
        and verification["ranking"] is None
    )

    subscriptions.commit_watermark(h.NS, 2)
    events = monitor.run(place_monitor, 2, principal_id=h.PRINCIPAL, scopes=h.SCOPES)[
        "notifications"
    ]
    assert [n["event"] for n in events] == ["warning_cancelled"] and events[0][
        "advice"
    ] is None

    # No Noesis-produced forecast and no advice text anywhere in the answers.
    answers = [
        observed,
        issued,
        evolution,
        *warnings.values(),
        verification,
        {"events": events},
    ]
    for text in (s for answer in answers for s in strings(answer)):
        assert not ADVICE.search(text), text
    issuers = {f["issuer"] for f in issued["forecasts"]} | {
        i["issuer"] for i in evolution["issuances"]
    }
    assert issuers <= {
        "Deutscher Wetterdienst",
        "Open-Meteo",
        "NOAA / National Weather Service",
    }
    assert not [
        row
        for row in conn.execute("SELECT content_json FROM weather_revisions").fetchall()
        if re.search(r'"issuer":\s*"[^"]*noesis', row[0], re.I)
    ]

    # No environment owner duplicated: Weather wrote stations only, through the owner, and no series.
    env = EnvironmentStore(conn, initialize=False)
    series = env.records(h.ENV_NS, scopes=h.SCOPES, record_type="observation_series")
    assert sorted(r["native_id"] for r in series) == [
        f"{fb.DWD}:RF_TU",
        f"{fb.DWD}:TT_TU",
    ]
    weather_written = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT e.record_type FROM environment_records e JOIN environment_record_revisions v "
            "USING(record_id) WHERE v.run_id LIKE '%environment-stations'"
        ).fetchall()
    }
    assert weather_written == {"station"}
    assert (
        WeatherStore(conn, initialize=False).provider_state(h.NS, "dwd-cap")["stale"]
        is False
    )


def test_production_sources_replay_offline_through_the_runtime():
    conn = h.connection()
    manifest = validate_source_pack(json.loads(h.PACK.read_text()))
    SourcePackStore(conn).install(
        manifest, principal_id="operator", enable=True, now_ms=10
    )
    clock = iter(range(1_000, 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    for source in manifest["sources"]:
        runtime.accept_license(
            manifest["pack_id"], source["source_id"], principal_id="operator"
        )
    receipt = runtime.run(
        {
            "pack_id": manifest["pack_id"],
            "run_key": "weather-production-offline",
            "operation": "observe",
            "max_results": 5000,
            "max_bytes": 50_000_000,
            "timeout_ms": 60_000,
        },
        principal_id="operator",
        adapters=runtime.fixture_adapters(manifest["pack_id"], h.ROOT),
        dns_resolver=PUBLIC_DNS,
    )
    assert {s["source_id"]: s["status"] for s in receipt["sources"]} == {
        s["source_id"]: "complete" for s in manifest["sources"]
    }
    # Authored bodies name fictional stations only, so the production selection stores nothing.
    assert conn.execute("SELECT count(*) FROM weather_revisions").fetchone()[0] == 0
