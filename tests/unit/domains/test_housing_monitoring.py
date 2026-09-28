"""Housing monitors on the subscription store (#2005, U09)."""

from __future__ import annotations

import pytest

from src.kb.housing import HousingError
from src.kb.housing_monitoring import HousingMonitor
from src.kb.subscriptions import SubscriptionStore
from tests.unit import housing_fixture_builder as fb
from tests.unit import housing_harness as h

NS = h.NS


def commit(env, watermark):
    SubscriptionStore(env.conn).commit_watermark(
        NS, watermark, kind="ingestion", detail={"run": watermark}
    )


def kinds(result):
    return sorted(
        (
            n["kind"],
            n["item"].get("zone_id")
            or n["item"].get("plan_id")
            or n["item"].get("edition_id"),
        )
        for n in result["notifications"]
    )


@pytest.fixture()
def env():
    env = h.Env().world()
    env.place = env.address()
    yield env
    env.conn.close()


def test_a_place_monitor_reports_new_publications_stage_changes_and_editions_once(env):
    monitor = HousingMonitor(env.conn)
    created = monitor.create(
        NS,
        "home",
        selector={"place_id": env.place["place_id"]},
        principal_id="alice",
        scopes=h.SCOPES,
    )
    assert (
        created["domain"] == "geospatial" and "no new scheduler" in created["refresh"]
    )
    commit(env, 1)
    first = monitor.run(
        created["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    assert kinds(first) == [
        ("new_land_value_publication", "1099001"),
        ("new_rent_index_edition", "berliner-mietspiegel-2099"),
        ("plan_stage_recorded", "1-99a"),
    ]
    assert (
        monitor.run(
            created["subscription_id"], 1, principal_id="alice", scopes=h.SCOPES
        )["status"]
        == "replayed"
    )
    env.run(list(h.WFS_SOURCES), "unchanged-again")
    commit(env, 2)
    unchanged = monitor.run(
        created["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    assert unchanged["status"] == "evaluated" and unchanged["notifications"] == []
    env.boris_2100()
    env.bplan_stage_change()
    commit(env, 3)
    later = monitor.run(
        created["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    assert kinds(later) == [
        ("new_land_value_publication", "1099001"),
        ("plan_stage_recorded", "1-99a"),
    ]
    land = next(
        n for n in later["notifications"] if n["kind"] == "new_land_value_publication"
    )
    assert land["item"]["valuation_date"] == "2100-01-01"
    assert (
        land["supersedes"]["valuation_date"] == "2099-01-01"
        and land["source_revision"]["feature_revision_id"]
    )
    stage = next(
        n for n in later["notifications"] if n["kind"] == "plan_stage_recorded"
    )
    assert (
        stage["item"]["stage"] == "festgesetzt"
        and stage["supersedes"]["stage"] == "aufstellungsbeschluss"
    )
    assert (
        "published by" in stage["message"] and "nothing is concluded" in stage["note"]
    )
    for note in first["notifications"] + later["notifications"]:
        assert not any(
            word in note["message"].lower()
            for word in ("increase", "decrease", "value of your", "should", "advice")
        )


def test_a_missed_run_is_recovered_from_the_watermark_without_duplicates(env):
    monitor = HousingMonitor(env.conn)
    created = monitor.create(
        NS,
        "district",
        selector={"district_code": "001"},
        principal_id="alice",
        scopes=h.SCOPES,
    )
    commit(env, 1)
    first = monitor.run(
        created["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    assert ("plan_stage_recorded", "1-98") in kinds(first) and (
        "new_land_value_publication",
        "1099003",
    ) in kinds(first)
    env.mietspiegel_2101()
    commit(env, 2)
    commit(env, 3)  # the run at 2 was missed
    recovered = monitor.run(
        created["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    assert recovered["watermark"] == 3 and kinds(recovered) == [
        ("new_rent_index_edition", "berliner-mietspiegel-2101")
    ]
    (edition,) = recovered["notifications"]
    assert edition["supersedes"]["edition_id"] == "berliner-mietspiegel-2099"
    fresh = HousingMonitor(env.conn, initialize=False)  # a restarted process
    assert (
        fresh.run(created["subscription_id"], 3, principal_id="alice", scopes=h.SCOPES)[
            "status"
        ]
        == "replayed"
    )
    polled = fresh.poll(
        created["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    assert len(polled["events"]) == len(first["notifications"]) + 1


def test_zone_and_plan_selectors_and_corrections(env):
    monitor = HousingMonitor(env.conn)
    zone = monitor.create(
        NS,
        "zone",
        selector={"zone_id": "1099002"},
        principal_id="alice",
        scopes=h.SCOPES,
    )
    plan = monitor.create(
        NS, "plan", selector={"plan_id": "1-98"}, principal_id="alice", scopes=h.SCOPES
    )
    commit(env, 1)
    assert kinds(
        monitor.run(zone["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    ) == [("new_land_value_publication", "1099002")]
    assert kinds(
        monitor.run(plan["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    ) == [("plan_stage_recorded", "1-98")]
    features = fb.boris_features("2099-01-01")
    features[1]["properties"]["brw"] = (
        "6150"  # the publisher corrects the same Stichtag
    )
    adapter = env.wfs_adapter(
        "berlin-boris-bodenrichtwerte", features, "2099-03-09T08:00:00Z"
    )
    env.run(
        ["berlin-boris-bodenrichtwerte"],
        "boris-corrected",
        adapters={"berlin-boris-bodenrichtwerte": adapter},
    )
    commit(env, 2)
    corrected = monitor.run(
        zone["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    (note,) = corrected["notifications"]
    assert (
        note["kind"] == "new_land_value_publication_corrected"
        and note["item"]["value_text"] == "6150"
    )
    assert (
        note["source_revision"]["change"] == "correction"
        and note["supersedes"]["change"] == "initial"
    )


def test_selectors_are_validated_and_scoped(env):
    monitor = HousingMonitor(env.conn)
    with pytest.raises(HousingError):
        monitor.create(
            NS,
            "bad",
            selector={"zone_id": "1", "plan_id": "2"},
            principal_id="alice",
            scopes=h.SCOPES,
        )
    with pytest.raises(HousingError):
        monitor.create(
            NS, "bad2", selector={"street": "x"}, principal_id="alice", scopes=h.SCOPES
        )
    with pytest.raises(HousingError) as denied:
        monitor.create(
            NS,
            "geo",
            selector={"place_id": env.place["place_id"]},
            principal_id="alice",
            scopes={h.READ, f"namespace:{NS}:read", "knowledge:subscriptions:write"},
        )
    assert denied.value.code == "unauthorized"
    with pytest.raises(HousingError) as unknown:
        monitor.create(
            NS,
            "nowhere",
            selector={"place_id": "place:none"},
            principal_id="alice",
            scopes=h.SCOPES,
        )
    assert unknown.value.code == "not_found"
    with pytest.raises(HousingError):
        monitor.create(
            NS,
            "district-13",
            selector={"district_code": "13"},
            principal_id="alice",
            scopes=h.SCOPES,
        )
    other = SubscriptionStore(env.conn).create(
        {"namespace": NS, "query": {"operation": "search", "kind": "other"}},
        "other",
        principal_id="alice",
        scopes=h.SCOPES,
    )
    with pytest.raises(HousingError):
        monitor.run(other["subscription_id"], principal_id="alice", scopes=h.SCOPES)
