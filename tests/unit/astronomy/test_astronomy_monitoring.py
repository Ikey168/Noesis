"""Astronomy monitors through subscriptions with two fixture polls (#2149, AS10)."""

from __future__ import annotations

import pytest

from src.kb.astronomy_monitoring import AstronomyMonitor
from src.kb.astronomy_records import AstronomyError
from src.kb.subscriptions import SubscriptionStore
from tests.unit.astronomy import harness as h

WATCHES = {
    "small": ("small_body", "K99A12B", None),
    "planet": ("exoplanet", "TOI-99902.01", None),
    "host": ("host", "Fict-303", None),
    "object": ("orbital_object", "99901", None),
    "provider": ("launch_provider", "FICTSPACE", None),
    "weather": ("space_weather", None, "G1"),
}


def poll(conn, monitor, subs, watermark):
    SubscriptionStore(conn).commit_watermark(h.NS, watermark, kind="ingestion")
    return {
        name: monitor.run(sid, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
        for name, sid in subs.items()
    }


def test_two_polls_notify_what_publishers_changed_citing_old_and_new_revisions():
    conn = h.connection()
    h.acquire_all(conn, until="2099-02-06")
    h.acquire(conn, "swpc", "2099-09-01T13")
    monitor = AstronomyMonitor(conn)
    subs = {}
    for name, (watch, target, scale) in WATCHES.items():
        created = monitor.create(
            h.NS,
            name,
            watch=watch,
            target=target,
            scale=scale,
            principal_id=h.PRINCIPAL,
            scopes=h.SCOPES,
        )
        subs[name] = created["subscription_id"]
    first = poll(conn, monitor, subs, 1)
    assert all(r["baseline"] and r["notifications"] == [] for r in first.values())
    h.acquire_all(conn)
    h.acquire(
        conn,
        "swpc",
        "2099-09-02T07",
        run_id="late-swpc",
        observed_at_ms=h.ms("2099-09-03"),
    )
    second = poll(conn, monitor, subs, 2)
    events = {
        name: sorted(n["event"] for n in r["notifications"])
        for name, r in second.items()
    }
    # The baseline already held MPC E2099-B17 and JPL orbit 3; MPO999123 and JPL orbit 12 are new.
    assert events["small"] == [
        "designation_identified",
        "orbit_solution_published",
        "orbit_solution_published",
        "risk_listing_changed",
    ]
    assert events["planet"] == ["disposition_changed"]
    assert events["host"] == ["disposition_changed"]  # the retraction listing
    assert "object_decayed" in events["object"]
    assert events["provider"] == ["launch_outcome_published"] * 3
    assert events["weather"] == ["space_weather_cancelled", "space_weather_issued"]
    changed = next(n for n in second["planet"]["notifications"])
    assert (
        changed["old_revision"]["revision_id"] != changed["new_revision"]["revision_id"]
    )
    assert changed["source"] == "nasa-exoplanet-archive" and isinstance(
        second["planet"]["n"], int
    )
    risk = next(
        n
        for n in second["small"]["notifications"]
        if n["event"] == "risk_listing_changed"
    )
    assert (
        "no verdict" in risk["message"]
        and risk["old_revision"]["record_id"] == risk["new_revision"]["record_id"]
    )
    # Replaying a watermark produces nothing new.
    replay = monitor.run(subs["planet"], 2, principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    assert replay["status"] == "replayed" and replay["notifications"] == []
    polled = monitor.poll(subs["planet"], principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    assert polled["events"]


def test_monitor_validation_and_readiness():
    conn = h.connection()
    monitor = AstronomyMonitor(conn)
    with pytest.raises(AstronomyError):
        monitor.create(
            h.NS,
            "x",
            watch="conjunctions",
            target="99901",
            principal_id=h.PRINCIPAL,
            scopes=h.SCOPES,
        )
    with pytest.raises(AstronomyError):
        monitor.create(
            h.NS,
            "x",
            watch="space_weather",
            scale="G9",
            principal_id=h.PRINCIPAL,
            scopes=h.SCOPES,
        )
    created = monitor.create(
        h.NS,
        "ok",
        watch="small_body",
        target="2099 AB12",
        principal_id=h.PRINCIPAL,
        scopes=h.SCOPES,
    )
    SubscriptionStore(conn).commit_watermark(h.NS, 1, kind="ingestion")
    with pytest.raises(AstronomyError) as error:
        monitor.run(
            created["subscription_id"], principal_id=h.PRINCIPAL, scopes=h.SCOPES
        )
    assert error.value.code == "not_ready"
