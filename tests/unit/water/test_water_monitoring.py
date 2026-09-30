"""WA10 (#2632): station, river and water-body monitors through subscriptions: new, revised and unchanged cases."""

import pytest

from src.kb.water_identity import WaterIdentity
from src.kb.water_monitoring import WaterMonitor
from src.kb.water_records import WaterError
from tests.unit.water import fixture_builder, harness
from tests.unit.water.harness import ALL, NS


@pytest.fixture()
def env():
    item = harness.Env().loaded()
    item.ids = item.places()
    identity = WaterIdentity(item.conn, now=item.tick)
    proposed = identity.propose(NS, principal_id="alice", scopes=ALL)
    for match in proposed["matches"]:
        if match["method"] == "published-river-identifier":
            identity.review(NS, match["match_id"], "accept", "published water identifier", principal_id="bob",
                            scopes=ALL)
    item.monitor = WaterMonitor(item.conn, now=item.tick)
    item.subscription = item.monitor.create(
        NS, "watch-1", principal_id="analyst", scopes=ALL, stations=["USGS-99990001"],
        rivers=[item.ids["nordfluss"]], water_bodies=["DEFX_NORTHWIND_03", "DEFX_EXAMPLA_01"])["subscription_id"]
    return item


def run(env):
    return env.monitor.run(env.subscription, principal_id="analyst", scopes=ALL)


def test_monitor_is_a_subscription_and_unchanged_data_emits_nothing(env):
    subscription = env.monitor.subscriptions.inspect(env.subscription, principal_id="analyst", scopes=ALL)
    assert subscription["query"]["kind"] == "water-monitor"
    assert subscription["cadence"] == {"trigger": "watermark", "source_pack": "climate-environment-water"}
    first = run(env)
    assert first["baseline"]
    kinds = {n["kind"] for n in first["notifications"]}
    assert kinds == {"observation_above_threshold", "assessment_new_cycle"}
    above = [n for n in first["notifications"] if n["kind"] == "observation_above_threshold"]
    assert {n["value"] for n in above} == {524.0, 530.0, 541.0}  # above the published MHW of 520 cm
    assert above[0]["thresholds"][0]["name"] == "MHW" and above[0]["new"]["revision_id"]
    assert all(n["watched"] == f"river:{env.ids['nordfluss']}" for n in above)
    assert run(env)["notifications"] == []
    rerun = env.run("water-unchanged")
    assert rerun["status"] == "complete" and run(env)["notifications"] == []  # replay adds nothing


def test_later_acquisition_notifies_revisions_withdrawals_station_changes_and_new_cycles(env):
    run(env)
    env.advance(7)
    assert env.run("water-later", later=True)["status"] == "complete"
    later = run(env)
    assert not later["baseline"]
    by_kind = {}
    for note in later["notifications"]:
        by_kind.setdefault(note["kind"], []).append(note)
    revised = {n["record_key"]: n for n in by_kind["observation_revised"]}
    approved = revised["USGS-99990001|00060|00003|2026-09-04"]
    assert (approved["prior_quality"], approved["quality"]) == ("provisional", "approved")
    assert {"quality", "value", "last_modified", "qualifiers"} <= set(approved["what_changed"])
    assert approved["prior"]["revision_id"] != approved["new"]["revision_id"]
    corrected = revised[f"{fixture_builder.EXAMPLA}|W|instantaneous|2026-09-20T01:30:00+02:00"]
    assert (corrected["prior_value"], corrected["value"]) == (541.0, 540.0)
    assert {n["record_key"] for n in by_kind["observation_removed"]} == {"USGS-99990001|00060|00003|2026-09-06"}
    assert [n["value"] for n in by_kind["observation_above_threshold"]] == [548.0]  # the newly published value
    stations = {n["record_key"]: n["what_changed"] for n in by_kind["station_revised"]}
    assert "datum" in stations[fixture_builder.EXAMPLA] and "location" in stations[fixture_builder.NORTHWIND]
    assert [n["record_key"] for n in by_kind["assessment_new_cycle"]] == ["DEFX_NORTHWIND_03|2022"]
    assert "assessment_revised" not in by_kind


def test_monitors_need_a_known_target_and_a_complete_run():
    fresh = harness.Env()
    with pytest.raises(WaterError):
        WaterMonitor(fresh.conn).create(NS, "x", principal_id="a", scopes=ALL, stations=["EXAMPLA"])
    env = harness.Env().loaded()
    with pytest.raises(WaterError):
        WaterMonitor(env.conn).create(NS, "x", principal_id="a", scopes=ALL, stations=["No such gauge"])
    with pytest.raises(WaterError):
        WaterMonitor(env.conn).create(NS, "x", principal_id="a", scopes=ALL)
