"""Store: places not a new spatial store, linked-not-merged stations, kinds, units, failures, namespaces."""

import pytest

from src.kb.environment_store import (
    FACILITY_COLLECTION,
    STATION_COLLECTION,
    EnvironmentStore,
    EnvironmentStoreError,
    record_id,
)
from src.kb.geospatial import GeospatialStore
from tests.unit.environment import harness
from tests.unit.environment.harness import NS, SCOPES


@pytest.fixture(scope="module")
def acquired():
    env = harness.Env()
    results = env.acquire_all()
    assert all(r["ok"] for r in results)
    return env


def test_stations_become_geospatial_places_and_features_with_provider_identifiers(acquired):
    store = EnvironmentStore(acquired.conn)
    geo = GeospatialStore(acquired.conn)
    stations = store.records(NS, scopes=SCOPES, record_type="station")
    assert len(stations) == 4
    for station in stations:
        place = geo.place(NS, station["place_id"], scopes={"knowledge:geospatial:read"})
        assert place["place_type"] == "environment-station"
        assert place["source_ids"] == {station["provider"]: station["native_id"]}
        geometry = geo.geometry(NS, station["geometry_id"], scopes={"knowledge:geospatial:read"})
        assert geometry["geometry"]["type"] == "Point" and geometry["crs"] == "EPSG:4326"
    collections = dict(acquired.conn.execute(
        "SELECT collection, count(*) FROM geospatial_features WHERE namespace=? GROUP BY 1", [NS]).fetchall())
    assert collections == {STATION_COLLECTION: 4, FACILITY_COLLECTION: 2}
    tables = {r[0] for r in acquired.conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert not any("environment" in t and ("geometr" in t or "place" in t) for t in tables)  # no new spatial store


def test_overlapping_stations_are_linked_not_merged(acquired):
    store = EnvironmentStore(acquired.conn)
    links = [link for link in store.links(NS, scopes=SCOPES) if link["relation"] == "same-station"]
    assert len(links) == 1
    link = links[0]
    assert link["state"] == "linked" and link["merged"] is False
    assert link["evidence"]["eea_station_code"] == "DEBE068" and link["evidence"]["distance_m"] < 50
    openaq = record_id(NS, "station", "openaq", "2993")
    uba = record_id(NS, "station", "uba", "282")
    assert set(link["records"]) == {openaq, uba}
    assert store.record(NS, openaq, scopes=SCOPES)["content"]["title"] == "DEBE068"
    assert store.record(NS, uba, scopes=SCOPES)["content"]["title"] == "Berlin Mitte"
    installations = [link for link in store.links(NS, scopes=SCOPES) if link["relation"] == "same-installation"]
    assert len(installations) == 1 and installations[0]["basis"].startswith("ETS identifier published")


def test_reacquiring_unchanged_content_creates_no_revision_or_vintage(acquired):
    before = acquired.conn.execute("SELECT count(*), (SELECT count(*) FROM environment_vintages) FROM environment_record_revisions").fetchone()
    assert acquired.acquire("uba")["ok"]
    after = acquired.conn.execute("SELECT count(*), (SELECT count(*) FROM environment_vintages) FROM environment_record_revisions").fetchone()
    assert before == after


def test_kinds_stay_separate_and_values_are_normalised(acquired):
    store = EnvironmentStore(acquired.conn)
    kinds = dict(acquired.conn.execute("SELECT provider, string_agg(DISTINCT kind, ',') FROM environment_vintages GROUP BY 1").fetchall())
    assert kinds["open-meteo-archive"] == "model" and kinds["open-meteo-forecast"] == "forecast"
    assert "observation" not in kinds["open-meteo-archive"] + kinds["open-meteo-forecast"]
    observations = store.records(NS, scopes=SCOPES, record_type="observation_series", kind="observation")
    assert not any(r["provider"].startswith("open-meteo") for r in observations)
    series = store.series(NS, record_id(NS, "observation_series", "uba", "282:5:2"), scopes=SCOPES, unit="mg/m³")
    assert series["values"][2]["normalized_unit"] == "microgram / meter ** 3"
    assert series["values"][2]["converted"]["value"] == "0.045000"
    forecast = store.series(NS, store.records(NS, scopes=SCOPES, provider="open-meteo-forecast")[0]["record_id"], scopes=SCOPES)
    assert forecast["kind_notice"] == "forecast values; never an observation"


def test_a_series_never_changes_kind(acquired):
    store = EnvironmentStore(acquired.conn)
    record = store.record(NS, record_id(NS, "grid_event", "smard", "411:DE:quarterhour:1789941600000"), scopes=SCOPES)
    flipped = {**record["content"], "kind": "observation", "points": []}
    flipped["title"] = "flipped"
    flipped.pop("values_digest", None)
    with pytest.raises(EnvironmentStoreError) as error:
        store.apply(NS, [flipped], run_id="flip", principal_id="alice", scopes=SCOPES)
    assert error.value.code == "kind_conflict"


def test_provider_failure_is_recorded_without_touching_values(acquired):
    values = acquired.conn.execute("SELECT count(*) FROM environment_values").fetchone()[0]
    acquired.web.failures["smard"] = 503
    try:
        failed = acquired.acquire("smard")
    finally:
        acquired.web.failures.clear()
    assert failed["ok"] is False and failed["failure"]["failure_code"] == "http_503"
    state = EnvironmentStore(acquired.conn).provider_state(NS, "smard")
    assert state["stale"] and state["last_failure_code"] == "http_503"
    assert acquired.conn.execute("SELECT count(*) FROM environment_values").fetchone()[0] == values
    assert acquired.acquire("smard")["ok"]
    assert not EnvironmentStore(acquired.conn).provider_state(NS, "smard")["stale"]


def test_writes_are_namespace_scoped_and_refuse_global():
    env = harness.Env()
    with pytest.raises(EnvironmentStoreError) as error:
        env.acquire("dwd", namespace="global", scopes=SCOPES | {"namespace:global:write", "namespace:global:read"})
    assert error.value.code == "namespace_forbidden"
    with pytest.raises(EnvironmentStoreError):
        EnvironmentStore(env.conn).apply(NS, [], run_id="x", principal_id="p", scopes={"knowledge:environment:write"})
