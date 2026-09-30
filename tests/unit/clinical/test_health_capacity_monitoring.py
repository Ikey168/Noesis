"""New releases, revisions and definition changes of a place's capacity indicators through subscriptions (HS09)."""

from __future__ import annotations

import pytest

from src.kb.health_capacity import NEVER_SENTENCE, HealthCapacityError
from src.kb.health_capacity_comparability import HealthCapacityComparability
from src.kb.health_capacity_monitoring import CONTRACT, HealthCapacityMonitor
from tests.unit.clinical import health_capacity_harness as h


@pytest.fixture
def env():
    environment = h.Env()
    environment.acquire("r1", ["gho", "eurostat"])
    environment.places = h.register_places(environment.conn)
    HealthCapacityComparability(environment.conn, now=environment.clock).resolve_places(
        h.NS, principal_id="analyst", scopes=h.SCOPES)
    return environment


def monitor(env):
    return HealthCapacityMonitor(env.conn, now=env.clock)


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def watch_beds(env, key="m-beds", **extra):
    return monitor(env).create(h.NS, key, watch={"place_id": env.places["DEU"], "domains": ["beds"], **extra},
                               principal_id="alice", scopes=h.SCOPES)


def test_a_release_is_delivered_once_and_replays_are_deduplicated(env):
    created = watch_beds(env)
    assert created["watch"]["domains"] == ["beds"] and "never derives" in created["threshold_policy"]
    first = monitor(env).run(created["subscription_id"], 1, principal_id="alice", scopes=h.SCOPES)
    assert kinds(first) == ["new-release", "new-release"]  # GHO WHS6_102 DEU and Eurostat HBEDT DE
    assert all(n["contract"] == CONTRACT and n["item"]["source_revision"]["release_id"] for n in
               first["notifications"])
    assert first["boundary"] == NEVER_SENTENCE
    assert monitor(env).run(created["subscription_id"], 1, principal_id="alice",
                            scopes=h.SCOPES)["notifications"] == []
    assert monitor(env).run(created["subscription_id"], 2, principal_id="alice",
                            scopes=h.SCOPES)["notifications"] == []


def test_a_revision_shows_old_and_new_values_with_both_vintages_cited(env):
    created = watch_beds(env, indicators=["hlth_rs_bds1:HBEDT"])
    monitor(env).run(created["subscription_id"], 1, principal_id="alice", scopes=h.SCOPES)
    env.eurostat_update("hlth_rs_bds1")
    env.acquire("r2", ["eurostat"])
    result = monitor(env).run(created["subscription_id"], 2, principal_id="alice", scopes=h.SCOPES)
    (revision,) = result["notifications"]
    assert revision["kind"] == "revision" and "both vintages cited" in revision["message"]
    (change,) = revision["item"]["changes"]
    assert change["reference_period"] == "2097"
    assert (change["left"]["value"], change["right"]["value"]) == ("782", "783.4")  # compared as numbers
    vintages = revision["item"]["vintages"]
    assert vintages["previous"]["vintage_id"] == change["left"]["vintage_id"]
    assert vintages["current"]["vintage_id"] == change["right"]["vintage_id"]
    assert vintages["previous"]["source_revision"]["published_on"] == "2098-03-15"
    assert vintages["current"]["source_revision"]["published_on"] == "2098-09-20"
    assert monitor(env).run(created["subscription_id"], 3, principal_id="alice",
                            scopes=h.SCOPES)["notifications"] == []


def test_a_definition_change_is_reported_as_a_break(env):
    created = watch_beds(env, indicators=["WHS6_102"])
    monitor(env).run(created["subscription_id"], 1, principal_id="alice", scopes=h.SCOPES)
    env.gho_definition_edition("WHS6_102", valid_from="2097-01-01")
    env.acquire("r2", ["gho"])
    result = monitor(env).run(created["subscription_id"], 2, principal_id="alice", scopes=h.SCOPES)
    assert kinds(result) == ["definition-change", "new-release"]
    change = next(n for n in result["notifications"] if n["kind"] == "definition-change")
    assert "break" in change["message"]
    assert (change["item"]["from"], change["item"]["to"], change["item"]["period"]) == ("GHO IMR 2090",
                                                                                          "GHO IMR 2098", "2097")


def test_thresholds_are_user_configured_only_and_unit_checked(env):
    created = monitor(env).create(
        h.NS, "m-t", watch={"place_code": "DEU", "indicators": ["WHS6_102"]}, principal_id="alice", scopes=h.SCOPES,
        thresholds=[{"value": "80", "unit": "per 10 000 population", "comparison": "above"},
                    {"value": "3", "unit": "beds"}])
    result = monitor(env).run(created["subscription_id"], 1, principal_id="alice", scopes=h.SCOPES)
    exceeded = [n for n in result["notifications"] if n["kind"] == "threshold-exceeded"]
    assert [n["item"]["value"] for n in exceeded] == ["80.1"]
    assert [n["reason"] for n in result["not_applicable"]] == [
        "a count threshold is never compared with a rate series"]
    with pytest.raises(HealthCapacityError):
        monitor(env).create(h.NS, "m-bad", watch={"place_id": "x", "place_code": "DE"}, principal_id="alice",
                            scopes=h.SCOPES)
    with pytest.raises(HealthCapacityError):
        monitor(env).create(h.NS, "m-bad2", watch={"place_code": "DE", "domains": ["quality"]},
                            principal_id="alice", scopes=h.SCOPES)
