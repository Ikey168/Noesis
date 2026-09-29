"""E09: re-published values become vintages; comparisons cite both; views pin and go stale."""

import pytest

from src.kb.environment_store import EnvironmentStore, record_id
from src.kb.environment_vintages import compare, pin_status
from tests.unit.environment import harness
from tests.unit.environment.harness import NS, SCOPES


@pytest.fixture
def env():
    env = harness.Env()
    for provider in ("uba", "eu-ets", "smard"):
        assert env.acquire(provider)["ok"]
    return env


def test_validated_air_quality_replaces_provisional_as_a_new_vintage(env):
    rid = record_id(NS, "observation_series", "uba", "282:5:2")
    env.tick(86400)
    env.web.overrides["uba_measures_282_5.json"] = "uba_measures_282_5_validated.json"
    assert env.acquire("uba", {**harness.selections()["uba"], "data_status": "validated"})["ok"]
    vintages = EnvironmentStore(env.conn).vintages(NS, rid, scopes=SCOPES)
    assert [v["status"] for v in vintages] == ["provisional", "validated"]
    assert vintages[1]["revision_of"] == vintages[0]["vintage_id"]
    assert vintages[1]["release_at_basis"] == "provider_vintage_fallback"
    assert vintages[1]["retrieved_at_basis"] == "connector_acquisition" and vintages[1]["retrieved_at_ms"] > vintages[0]["retrieved_at_ms"]
    comparison = compare(env.conn, NS, rid, scopes=SCOPES)
    assert comparison["left"]["vintage_id"] == vintages[0]["vintage_id"]
    assert comparison["right"]["vintage_id"] == vintages[1]["vintage_id"]
    changes = {c["period_start"]: c for c in comparison["changes"]}
    assert changes["2026-09-24T02:00:00Z"]["change"] == "value_revised" and changes["2026-09-24T02:00:00Z"]["delta"] == "-1"
    assert changes["2026-09-24T00:00:00Z"]["change"] == "status_changed"  # same value, provisional -> validated
    assert all(c["left"]["status"] == "provisional" and c["right"]["status"] == "validated" for c in changes.values())
    assert comparison["notice"].startswith("differences between published vintages")


def test_corrected_ets_emissions_and_revised_generation_totals_become_vintages(env):
    env.tick(86400)
    env.web.overrides["eu_ets_verified_2025.csv"] = "eu_ets_verified_2025_corrected.csv"
    assert env.acquire("eu-ets", {**harness.selections()["eu-ets"], "released_at": "2026-06-15"})["ok"]
    ets = record_id(NS, "observation_series", "eu-ets", "DE_900201:verified_emissions")
    comparison = compare(env.conn, NS, ets, scopes=SCOPES)
    assert [(c["left"]["value"], c["right"]["value"]) for c in comparison["changes"]] == [("598412", "601930")]
    assert comparison["right"]["release_at_basis"] == "caller_supplied"
    untouched = record_id(NS, "observation_series", "eu-ets", "DE_900202:verified_emissions")
    assert len(EnvironmentStore(env.conn).vintages(NS, untouched, scopes=SCOPES)) == 1
    env.web.overrides["smard_410_DE_quarterhour_1789941600000.json"] = "smard_410_DE_quarterhour_1789941600000_revised.json"
    assert env.acquire("smard")["ok"]
    smard = record_id(NS, "grid_event", "smard", "410:DE:quarterhour:1789941600000")
    revision = compare(env.conn, NS, smard, scopes=SCOPES)
    assert {c["change"] for c in revision["changes"]} == {"value_revised", "value_filled"}
    assert revision["right"]["release_at_basis"] == "provider_reported"  # SMARD meta_data.created


def test_downstream_views_pin_vintages_and_show_stale(env):
    rid = record_id(NS, "observation_series", "uba", "282:5:2")
    pinned = EnvironmentStore(env.conn).vintages(NS, rid, scopes=SCOPES)[0]["vintage_id"]
    assert pin_status(env.conn, NS, [pinned], scopes=SCOPES)["stale"] is False
    env.tick(3600)
    env.web.overrides["uba_measures_282_5.json"] = "uba_measures_282_5_validated.json"
    env.acquire("uba", {**harness.selections()["uba"], "data_status": "validated"})
    status = pin_status(env.conn, NS, [pinned], scopes=SCOPES)
    assert status["stale"] and status["pins"][0]["state"] == "stale" and len(status["pins"][0]["newer_vintages"]) == 1
    series = EnvironmentStore(env.conn).series(NS, rid, scopes=SCOPES, vintage_id=pinned)
    assert series["stale"] and [v["value"] for v in series["values"]] == ["30", "29", "45", "53"]
