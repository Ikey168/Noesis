"""BD10 (#2528): taxon, place and dataset monitors through subscriptions, one test per event type."""

import pytest

from src.kb.biodiversity_monitoring import BiodiversityMonitor
from src.kb.biodiversity_records import BiodiversityError
from tests.unit.biodiversity import harness
from tests.unit.biodiversity.fixture_builder import DS_SURVEY
from tests.unit.biodiversity.harness import ALL, NS


@pytest.fixture()
def env():
    item = harness.Env().loaded()
    item.mitte = item.place("Mitte (fixture)", harness.MITTE)["place_id"]
    item.monitor = BiodiversityMonitor(item.conn, now=item.tick)
    item.subscription = item.monitor.create(
        NS, "watch-1", principal_id="analyst", scopes=ALL, taxa=["Corvus cornix", "iucn:12419"],
        places=[item.mitte], datasets=[DS_SURVEY])["subscription_id"]
    return item


def run(env):
    return env.monitor.run(env.subscription, principal_id="analyst", scopes=ALL)


def test_monitor_is_a_subscription_with_a_baseline_and_no_replay_events(env):
    subscription = env.monitor.subscriptions.inspect(env.subscription, principal_id="analyst", scopes=ALL)
    assert subscription["query"]["kind"] == "biodiversity-monitor"
    assert subscription["cadence"] == {"trigger": "watermark", "source_pack": "climate-environment-biodiversity"}
    first = run(env)
    assert first["baseline"]
    kinds = {n["kind"] for n in first["notifications"]}
    assert {"occurrence_new", "taxonomic_status_change", "assessment_new"} <= kinds
    change = next(n for n in first["notifications"] if n["kind"] == "taxonomic_status_change")
    assert change["prior"]["release"] == "310001@2026-07-10" and change["new"]["release"] == "310002@2026-08-12"
    assert run(env)["notifications"] == []
    rerun = env.run("biodiversity-unchanged")
    assert rerun["status"] == "complete" and run(env)["notifications"] == []  # unchanged data emits nothing


def test_later_acquisition_emits_revision_removal_new_occurrence_and_assessment_events(env):
    run(env)
    env.advance(7)
    assert env.run("biodiversity-later", later=True)["status"] == "complete"
    later = run(env)
    assert not later["baseline"]
    by_kind = {}
    for note in later["notifications"]:
        by_kind.setdefault(note["kind"], []).append(note)
    assert {n["record_key"] for n in by_kind["occurrence_removed"]} == {"4011003"}
    revised = {n["record_key"] for n in by_kind["occurrence_revised"]}
    assert "4011002" in revised
    assert {n["record_key"] for n in by_kind["occurrence_new"]} == {"4011004"}
    removal = by_kind["occurrence_removed"][0]
    assert removal["prior"]["event"] == "published" and removal["new"]["event"] == "removed"
    assert {n["record_key"] for n in by_kind["assessment_new"]} == {"900004"}
    assert {n["record_key"] for n in by_kind["assessment_revised"]} == {"900003"}  # latest moved on
    assert "taxonomic_status_change" not in by_kind


def test_monitors_need_a_known_target_and_a_complete_run():
    fresh = harness.Env()
    with pytest.raises(BiodiversityError):
        BiodiversityMonitor(fresh.conn).create(NS, "x", principal_id="a", scopes=ALL, taxa=["Nothing"])
    env = harness.Env().loaded()
    with pytest.raises(BiodiversityError):
        BiodiversityMonitor(env.conn).create(NS, "x", principal_id="a", scopes=ALL, taxa=["Nomen nudum"])
    with pytest.raises(BiodiversityError):
        BiodiversityMonitor(env.conn).create(NS, "x", principal_id="a", scopes=ALL)
