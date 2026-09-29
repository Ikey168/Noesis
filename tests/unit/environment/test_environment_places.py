"""E08: place-centred answers through the geospatial owners, as-of pins, replay and coverage gaps."""

import pytest

from src.kb.environment_places import EnvironmentDossiers
from src.kb.environment_store import EnvironmentStoreError
from tests.unit.environment import harness
from tests.unit.environment.harness import NS, SCOPES


@pytest.fixture(scope="module")
def berlin():
    env = harness.world()
    dossiers = EnvironmentDossiers(env.conn, now=env.now)
    dossier = dossiers.build(NS, "alexanderplatz", principal_id="alice", scopes=SCOPES,
                             place_id=env.alexanderplatz["place_id"])
    return env, dossiers, dossier


def test_observations_within_the_containing_district_use_the_within_query(berlin):
    _, _, dossier = berlin
    assert dossier["boundary"]["title"] == "Mitte"
    within = next(r for r in dossier["receipts"] if r["operation"] == "points_within")
    assert within["coverage"] == "partial"  # a bounded selection is never presented as complete coverage
    stations = {(e["station"]["provider"], e["station"]["title"]) for e in dossier["sections"]["observations"]}
    assert stations == {("uba", "Berlin Mitte"), ("openaq", "DEBE068"), ("dwd", "Berlin-Alexanderplatz")}
    uba = next(e for e in dossier["sections"]["observations"] if e["station"]["provider"] == "uba")
    assert uba["links"][0]["state"] == "linked"
    series = uba["series"][0]
    assert series["kind"] == "observation" and series["source_url"].startswith("https://")
    assert series["as_of"]["vintage_id"] and series["as_of"]["status"] == "provisional"


def test_kinds_are_listed_separately_with_sources_and_as_of(berlin):
    _, _, dossier = berlin
    sections = dossier["sections"]
    assert {s["kind"] for s in sections["model_output"]} == {"model"}
    assert {s["kind"] for s in sections["forecasts"]} == {"forecast"}
    assert all(s["model"]["issue_time"] == "2026-09-26T03:00:00Z" for s in sections["forecasts"])
    assert all(s["kind"] == "observation" for e in sections["observations"] for s in e["series"])
    for event in sections["grid"]:
        assert event["kind"] in {"observation", "forecast"} and event["source_url"] and event["as_of"]["revision_id"]
    assert {e["event_type"] for e in sections["grid"]} == {"generation", "load", "unavailability"}
    assert sum(e["kind"] == "forecast" for e in sections["grid"]) == 2  # ENTSO-E day-ahead and SMARD forecast


def test_facilities_near_the_place_use_proximity_relations_with_linked_ets_records(berlin):
    _, _, dossier = berlin
    facilities = dossier["sections"]["facilities"]
    assert [f["title"] for f in facilities] == ["HKW Mitte (authored fixture)"]  # Klingenberg is ~6 km away
    mitte = facilities[0]
    assert 1500 < mitte["distance_m"] < 1600
    assert {r["indicator"]["code"] for r in mitte["releases"]} == {"CO2", "NOX"}
    ets = mitte["linked_records"][0]
    assert ets["provider"] == "eu-ets" and {s["indicator"]["code"] for s in ets["series"]} == {
        "verified_emissions", "allocated_allowances"}
    assert "no compliance determination" in mitte["notice"]


def test_grid_events_belong_to_the_zone_containing_the_place(berlin):
    _, _, dossier = berlin
    assert dossier["bidding_zone"]["name"] == "DE-LU"
    zone = next(r for r in dossier["receipts"] if r["operation"] == "zone-contains")
    assert "authored coarse outline" in zone["basis"]
    smard = [e for e in dossier["sections"]["grid"] if e["provider"] == "smard"]
    assert {e["zone_match"] for e in smard} == {"member area of zone"}


def test_umweltatlas_layers_containing_the_place(berlin):
    _, _, dossier = berlin
    layers = {layer["collection"]: layer for layer in dossier["sections"]["layers"]}
    assert set(layers) == {"ua_umweltzone:umweltzone", "ua_stratlaerm_2022:laerm_lden", "ua_klimaanalyse_2022:klimafunktion"}
    assert layers["ua_stratlaerm_2022:laerm_lden"]["properties"]["lden_klasse"] == "> 65 - 70"


def test_replay_reproduces_every_spatial_answer(berlin):
    env, dossiers, dossier = berlin
    replay = dossiers.replay(NS, dossier["dossier_id"], scopes=SCOPES, principal_id="alice")
    assert replay["deterministic"] and replay["pins_retained"] and replay["content_hash_verified"]
    assert {c["operation"] for c in replay["receipts"]} >= {"points_within", "zone-contains", "facility-proximity",
                                                           "grid-cell-proximity"}
    again = dossiers.build(NS, "alexanderplatz", principal_id="alice", scopes=SCOPES, place_id=env.alexanderplatz["place_id"])
    assert again["content_hash"] == dossier["content_hash"]


def test_as_of_selection_uses_the_values_valid_at_that_time():
    env = harness.Env()
    env.install_berlin()
    env.acquire("uba")
    first_seen = env.now()
    env.tick(3600)
    env.web.overrides["uba_measures_282_5.json"] = "uba_measures_282_5_validated.json"
    env.acquire("uba", {**harness.selections()["uba"], "data_status": "validated"})
    place = env.place()
    dossiers = EnvironmentDossiers(env.conn, now=env.now)
    earlier = dossiers.build(NS, "then", principal_id="alice", scopes=SCOPES, place_id=place["place_id"], as_of_ms=first_seen)
    later = dossiers.build(NS, "now", principal_id="alice", scopes=SCOPES, place_id=place["place_id"])
    old = next(s for e in earlier["sections"]["observations"] for s in e["series"])
    new = next(s for e in later["sections"]["observations"] for s in e["series"])
    assert [v["value"] for v in old["values"]] == ["30", "29", "45", "53"] and old["as_of"]["status"] == "provisional"
    assert [v["value"] for v in new["values"]] == ["30", "29", "44", "51"] and new["as_of"]["status"] == "validated"
    assert dossiers.inspect(NS, earlier["dossier_id"], scopes=SCOPES, principal_id="alice")["stale"]
    assert not dossiers.inspect(NS, later["dossier_id"], scopes=SCOPES, principal_id="alice")["stale"]


def test_places_without_coverage_return_an_explicit_gap():
    env = harness.Env()
    env.install_berlin()
    env.acquire_all()
    paris = env.place("Paris fixture point", [2.3522, 48.8566])
    dossier = EnvironmentDossiers(env.conn, now=env.now).build(NS, "paris", principal_id="alice", scopes=SCOPES,
                                                               place_id=paris["place_id"])
    assert dossier["status"] == "coverage_gap"
    assert {g["section"] for g in dossier["coverage_gaps"]} == {"observations", "model_output", "forecasts", "grid",
                                                                "facilities", "layers"}
    assert all(not items for items in dossier["sections"].values())
    assert dossier["not_implemented"]["copernicus-cams"].startswith("not implemented")


def test_ambiguous_mentions_are_not_guessed_and_requests_are_idempotent():
    env = harness.Env()
    env.acquire("dwd")  # registers the station place "Berlin-Alexanderplatz"
    env.place()
    dossiers = EnvironmentDossiers(env.conn, now=env.now)
    result = dossiers.build(NS, "mention", principal_id="alice", scopes=SCOPES, mention="Alexanderplatz")
    assert result["status"] == "place_unresolved" and result["resolution"]["status"] == "ambiguous"
    with pytest.raises(EnvironmentStoreError):
        dossiers.build(NS, "bad", principal_id="alice", scopes=SCOPES)
    with pytest.raises(EnvironmentStoreError) as denied:
        dossiers.build(NS, "x", principal_id="mallory", scopes={"knowledge:environment:write", "namespace:other:write"},
                       mention="Alexanderplatz")
    assert denied.value.code == "unauthorized"


def test_export_shows_kind_source_and_as_of_for_every_item(berlin):
    _, dossiers, dossier = berlin
    exported = dossiers.export(NS, dossier["dossier_id"], scopes=SCOPES, principal_id="alice")
    markdown = exported["markdown"]
    assert "[forecast]" in markdown and "[model]" in markdown and "[observation]" in markdown
    assert "No attribution, projection or compliance determination" in markdown
    assert "copernicus-cams: not implemented" in markdown
    assert exported["citations"] and all(url.startswith("https://") for url in exported["citations"])
    with pytest.raises(EnvironmentStoreError):
        dossiers.export(NS, dossier["dossier_id"], scopes=SCOPES, principal_id="mallory")
