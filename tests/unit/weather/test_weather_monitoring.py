"""WX11 (#2174): monitors through subscriptions over fixture polls, quoting issuers and citing revisions."""

from __future__ import annotations

import pytest

from src.kb import weather_records as wr
from src.kb.subscriptions import SubscriptionStore
from src.kb.weather_monitoring import WeatherMonitor
from src.kb.weather_store import WeatherError, WeatherStore
from tests.unit.weather import fixture_builder as fb
from tests.unit.weather import harness as h

TARGETS = {
    "place": {"kind": "place", "point": [13.53, 52.38]},
    "metar": {"kind": "station", "station": f"aviationweather:{fb.ICAO}"},
    "dwd": {"kind": "station", "station": f"dwd:{fb.DWD}"},
    "mosmix": {
        "kind": "provider-parameter",
        "provider": "dwd-mosmix",
        "parameter": "TTT",
    },
    "frost": {"kind": "warning-event", "event": "FROST", "min_severity": "Moderate"},
}


def events(result):
    return sorted(n["event"] for n in result["notifications"])


def test_polls_raise_issued_updated_cancelled_forecast_and_correction_events():
    conn = h.connection()
    for at, stage in h.STAGES:
        if h.ms(at) <= h.ms("2026-06-10T12:05:00Z"):
            h.acquire(conn, at, stage)
    monitor = WeatherMonitor(conn)
    subscriptions = SubscriptionStore(conn)
    ids = {
        name: monitor.create(
            h.NS, name, target=target, principal_id=h.PRINCIPAL, scopes=h.SCOPES
        )["subscription_id"]
        for name, target in TARGETS.items()
    }
    subscriptions.commit_watermark(h.NS, 1)
    first = {
        name: monitor.run(sid, 1, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
        for name, sid in ids.items()
    }
    assert all(r["baseline"] and r["notifications"] == [] for r in first.values())

    for at, stage in h.STAGES:
        if h.ms("2026-06-10T12:05:00Z") < h.ms(at) <= h.ms("2026-06-10T14:05:00Z"):
            h.acquire(conn, at, stage)
    subscriptions.commit_watermark(h.NS, 2)
    second = {
        name: monitor.run(sid, 2, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
        for name, sid in ids.items()
    }
    assert events(second["place"]) == ["warning_updated"]
    update = second["place"]["notifications"][0]
    assert (
        update["quote"]["instruction"] == "Text des Herausgebers (fiktiv)."
        and update["advice"] is None
    )
    assert update["cites"]["identifier"] == fb.U1
    assert events(second["frost"]) == [
        "warning_updated"
    ]  # the update raised the severity to Moderate
    assert events(second["mosmix"]) == ["forecast_issued"]
    assert events(second["metar"]) == ["observation_corrected"]
    assert second["metar"]["notifications"][0]["quote"]["raw_text"].startswith(
        "METAR COR"
    )

    for at, stage in h.STAGES:
        if h.ms(at) > h.ms("2026-06-10T14:05:00Z"):
            h.acquire(conn, at, stage)
    subscriptions.commit_watermark(h.NS, 3)
    third = {
        name: monitor.run(sid, 3, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
        for name, sid in ids.items()
    }
    assert events(third["place"]) == ["warning_cancelled"]
    # The historical release corrected the 12:00 value and set QN 10 elsewhere; the late 'recent' copy raised nothing.
    assert (
        set(events(third["dwd"])) == {"observation_corrected"}
        and len(third["dwd"]["notifications"]) == 4
    )
    assert sorted(
        n["message"].rsplit("(", 1)[1] for n in third["dwd"]["notifications"]
    ) == ["correction)", "qc_change)", "qc_change)", "qc_change)"]
    replay = monitor.run(ids["place"], 3, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    assert replay["status"] == "replayed" and replay["notifications"] == []
    polled = monitor.poll(ids["place"], principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    assert polled["events"]


def test_a_new_location_vintage_raises_station_relocated():
    conn = h.connection()
    store = WeatherStore(conn)
    loc = {"url": "https://opendata.dwd.de/fixture"}
    old = wr.location_vintage(
        "dwd-cdc",
        {"provider": "dwd", "native_id": "99905"},
        latitude="52.1",
        longitude="13.1",
        valid_from="2000-01-01",
        locator=loc,
    )
    store.apply(
        h.NS,
        [old],
        run_id="a",
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
        retrieved_at_ms=1_000,
    )
    monitor = WeatherMonitor(conn)
    sid = monitor.create(
        h.NS,
        "relocation",
        target={"kind": "station", "station": "dwd:99905"},
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
    )["subscription_id"]
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    monitor.run(sid, 1, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    closed = wr.location_vintage(
        "dwd-cdc",
        {"provider": "dwd", "native_id": "99905"},
        latitude="52.1",
        longitude="13.1",
        valid_from="2000-01-01",
        valid_to="2026-05-31",
        locator=loc,
    )
    new = wr.location_vintage(
        "dwd-cdc",
        {"provider": "dwd", "native_id": "99905"},
        latitude="52.11",
        longitude="13.12",
        valid_from="2026-06-01",
        locator=loc,
    )
    store.apply(
        h.NS,
        [closed, new],
        run_id="b",
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
        retrieved_at_ms=2_000,
    )
    SubscriptionStore(conn).commit_watermark(h.NS, 2)
    result = monitor.run(sid, 2, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    assert events(result) == ["station_relocated"]


def test_monitors_need_a_source_run_and_their_owner():
    conn = h.connection()
    with pytest.raises(WeatherError) as err:
        WeatherMonitor(conn).create(
            h.NS,
            "x",
            target=TARGETS["place"],
            principal_id=h.PRINCIPAL,
            scopes=h.SCOPES,
        )
    assert err.value.code == "not_ready"
    with pytest.raises(WeatherError):
        WeatherMonitor(conn).poll(
            "subscription:none", principal_id=h.PRINCIPAL, scopes=h.SCOPES
        )
