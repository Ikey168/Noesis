"""Water monitors on platform.subscriptions: new, revised and unchanged cases (#2582, WA10)."""

from __future__ import annotations

import pytest

from src.kb.water_monitoring import DEFAULT_THRESHOLDS, WaterMonitor
from src.kb.water_records import WaterError
from tests.unit.water import harness as h

NS = h.NS


def test_monitor_hears_thresholds_revisions_withdrawals_and_new_cycles_and_replays_add_nothing():
    env = h.Env().loaded()
    places = env.places()
    monitor = WaterMonitor(env.conn, now=env.tick)
    created = monitor.create(NS, "elbe-and-potomac", principal_id="alice", scopes=h.ALL, rivers=[places["elbe"]],
                             stations=["USGS-01646500"], water_bodies=["DERW_DESN_FIX-0002"])
    assert created["watch"]["thresholds"] == sorted(DEFAULT_THRESHOLDS) and "no separate scheduler" in \
        created["refresh"]
    baseline = monitor.run(created["subscription_id"], principal_id="alice", scopes=h.ALL)
    assert baseline["baseline"] and [n["kind"] for n in baseline["notifications"]] == ["assessment_new_cycle"]
    unchanged = monitor.run(created["subscription_id"], principal_id="alice", scopes=h.ALL)
    assert unchanged["notifications"] == []

    env.advance(1)
    assert env.run("water-replay")["status"] == "complete"
    assert monitor.run(created["subscription_id"], principal_id="alice", scopes=h.ALL)["notifications"] == []

    env.advance(7)
    assert env.run("water-later", later=True)["status"] == "complete"
    heard = monitor.run(created["subscription_id"], principal_id="alice", scopes=h.ALL)
    kinds = sorted(n["kind"] for n in heard["notifications"])
    assert kinds == ["assessment_new_cycle", "observation_above_threshold", "observation_revised",
                     "observation_revised", "observation_revised", "observation_revised", "station_removed",
                     "station_revised"]
    high = next(n for n in heard["notifications"] if n["kind"] == "observation_above_threshold")
    assert high["what_changed"]["threshold"]["shortname"] == "MHW" and high["what_changed"]["value"] == "495.0"
    assert "not a warning" in high["what_changed"]["statement"] and high["prior"] is None
    approved = [n for n in heard["notifications"] if n["kind"] == "observation_revised"
                and n["what_changed"]["quality"] == "approved"]
    assert len(approved) == 3 and all(n["prior"]["revision_id"] != n["new"]["revision_id"] for n in approved)
    datum = next(n for n in heard["notifications"] if n["kind"] == "station_revised")
    assert datum["what_changed"]["changes"] == ["datum"] and datum["watched"] == f"river:{places['elbe']}"
    cycle = next(n for n in heard["notifications"] if n["kind"] == "assessment_new_cycle")
    assert cycle["record_key"] == "DERW_DESN_FIX-0002|2022" and cycle["new"]["url"].startswith("https://")
    assert monitor.run(created["subscription_id"], principal_id="alice", scopes=h.ALL)["notifications"] == []
    polled = monitor.poll(created["subscription_id"], principal_id="alice", scopes=h.ALL)
    assert polled["events"]


def test_monitors_need_a_watch_a_known_subject_and_a_complete_run():
    env = h.Env()
    env.store.observe(NS, [])
    monitor = WaterMonitor(env.conn, now=env.tick)
    with pytest.raises(WaterError):
        monitor.create(NS, "empty", principal_id="alice", scopes=h.ALL)
    with pytest.raises(WaterError):
        monitor.create(NS, "unknown", principal_id="alice", scopes=h.ALL, stations=["nope"])
    with pytest.raises(WaterError) as error:
        monitor.complete_watermark(NS)
    assert error.value.code == "watermark_uncommitted"
