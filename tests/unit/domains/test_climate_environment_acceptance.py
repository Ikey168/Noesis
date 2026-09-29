"""Offline place-to-environmental-dossier acceptance journey (#1904, E13) — one test per acceptance row.

Real adapters, the source-pack runtime, DurableHTTP, the document store, the
geospatial owners (places, features, WFS upgrade, relations, receipts), LEI,
policy-monitor assertions and subscriptions run against *authored* provider
fixtures (tests/fixtures/environment/README.md). Nothing opens a network
connection. This is offline evidence only; it is not live provider coverage.
"""

import pytest

from src.kb.environment_identity import EnvironmentIdentity
from src.kb.environment_monitoring import EnvironmentMonitor
from src.kb.environment_places import EnvironmentDossiers
from src.kb.environment_store import EnvironmentStore, EnvironmentStoreError, record_id
from src.kb.environment_vintages import compare
from src.kb.geospatial import GeospatialStore
from src.kb.subscriptions import SubscriptionStore
from tests.unit.environment import harness
from tests.unit.environment.harness import NS, REVIEWER_SCOPES, SCOPES


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    import httpx

    def refuse(*args, **kwargs):
        raise AssertionError("offline acceptance must not open network connections")

    monkeypatch.setattr(httpx, "Client", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture(scope="module")
def journey():
    import socket

    import httpx

    saved = httpx.Client, socket.getaddrinfo

    def refuse(*args, **kwargs):
        raise AssertionError("offline acceptance must not open network connections")

    httpx.Client, socket.getaddrinfo = refuse, refuse
    try:
        env = harness.world()
        dossiers = EnvironmentDossiers(env.conn, now=env.now)
        dossier = dossiers.build(NS, "journey", principal_id="alice", scopes=SCOPES, place_id=env.alexanderplatz["place_id"])
    finally:
        httpx.Client, socket.getaddrinfo = saved
    return env, dossiers, dossier


def test_row_place_resolution(journey):
    env, dossiers, dossier = journey
    geo = GeospatialStore(env.conn)
    resolution = geo.resolve(NS, "Alexanderplatz", scopes={"knowledge:geospatial:read"})
    assert resolution["status"] == "ambiguous"  # the DWD station "Berlin-Alexanderplatz" is also a place
    assert dossiers.build(NS, "by-mention", principal_id="alice", scopes=SCOPES, mention="Alexanderplatz")["status"] == "place_unresolved"
    exact = next(c for c in resolution["candidates"] if "exact-name" in c["reasons"])
    assert exact["place_id"] == dossier["place"]["place_id"] == env.alexanderplatz["place_id"]
    assert dossier["place"]["coordinates"] == harness.ALEXANDERPLATZ


def test_row_acquisition_from_fixtures():
    env = harness.Env()
    env.install_berlin(run=False)
    env.install_environment_pack()
    runtime_receipt = env.run_sources("climate-environment", [s["source_id"] for s in harness.pack_manifest()["sources"]],
                                      "acceptance", operation="observe")
    assert runtime_receipt["status"] == "complete" and len(runtime_receipt["sources"]) == 9
    durable = harness.Env()
    results = durable.acquire_all()
    assert all(r["ok"] for r in results) and {r["execution"] for r in results} == {"injected"}
    counts = {r["provider"]: r["records"] for r in results}
    assert counts == {"openaq": 3, "uba": 4, "entsoe": 6, "smard": 2, "eea-industry": 2, "eu-ets": 6,
                      "open-meteo-archive": 2, "open-meteo-forecast": 2, "dwd": 3}


def test_row_place_projection(journey):
    env, _, dossier = journey
    store = EnvironmentStore(env.conn)
    for station in store.records(NS, scopes=SCOPES, record_type="station"):
        place = GeospatialStore(env.conn).place(NS, station["place_id"], scopes={"knowledge:geospatial:read"})
        assert place["place_type"] == "environment-station" and station["feature_id"]
    within = next(r for r in dossier["receipts"] if r["operation"] == "points_within")
    assert within["boundary"]["title"] == "Mitte"
    assert len(dossier["sections"]["observations"]) == 3 and len(dossier["sections"]["layers"]) == 3


def test_row_spatial_as_of_queries(journey):
    env, dossiers, dossier = journey
    replay = dossiers.replay(NS, dossier["dossier_id"], scopes=SCOPES, principal_id="alice")
    assert replay["deterministic"]
    before_any = dossiers.build(NS, "before-acquisition", principal_id="alice", scopes=SCOPES,
                                place_id=env.alexanderplatz["place_id"], as_of_ms=harness.ms("2026-09-01T00:00:00+00:00"))
    assert not before_any["sections"]["facilities"] and not before_any["sections"]["grid"]
    assert {g["section"] for g in before_any["coverage_gaps"]} >= {"facilities", "grid", "model_output", "forecasts"}
    assert all(item["as_of"] for item in dossier["sections"]["grid"])


def test_row_vintage_comparison():
    env = harness.Env()
    env.acquire("eu-ets")
    env.tick(86400)
    env.web.overrides["eu_ets_verified_2025.csv"] = "eu_ets_verified_2025_corrected.csv"
    env.acquire("eu-ets", {**harness.selections()["eu-ets"], "released_at": "2026-06-15"})
    comparison = compare(env.conn, NS, record_id(NS, "observation_series", "eu-ets", "DE_900201:verified_emissions"),
                         scopes=SCOPES)
    change = comparison["changes"][0]
    assert (change["left"]["value"], change["right"]["value"], change["delta"]) == ("598412", "601930", "3518")
    assert comparison["left"]["vintage_id"] != comparison["right"]["vintage_id"]
    assert comparison["left"]["release_at_ms"] < comparison["right"]["release_at_ms"]


def test_row_facility_operator_linking(journey):
    env, _, _ = journey
    identity = EnvironmentIdentity(env.conn)
    proposed = identity.propose_operator_links(NS, principal_id="alice", scopes=SCOPES)
    assert not proposed["candidates"]  # no LEI or entity records in this world: operators stay source strings
    assert {u["reason"] for u in proposed["unmatched"]} == {"no candidate; operator stays a source string"}
    from src.kb.entities import add_manual_alias

    add_manual_alias(env.conn, "Beispiel Wärme Berlin", "Beispiel Wärme Berlin GmbH", "organization")
    candidate = identity.propose_operator_links(NS, principal_id="alice", scopes=SCOPES)["candidates"][0]
    accepted = identity.review(NS, candidate["link_id"], "accepted", "operator name matches the reviewed alias",
                               principal_id="bob", scopes=REVIEWER_SCOPES)
    assert accepted["state"] == "accepted" and accepted["target_kind"] == "canonical-entity"


def test_row_subscriptions():
    env = harness.Env()
    env.install_berlin(run=False)
    env.acquire_all()
    place = env.place()
    monitor = EnvironmentMonitor(env.conn, now=env.now)
    created = monitor.create(NS, "acceptance", place_id=place["place_id"], principal_id="alice", scopes=SCOPES,
                             thresholds=[{"indicator": "no2", "op": "gte", "value": "50", "unit": "µg/m³"}])
    SubscriptionStore(env.conn).commit_watermark(NS, 1, kind="ingestion", detail={"run": "fixtures"})
    first = monitor.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert sum(n["kind"] == "threshold_crossed" for n in first["notifications"]) == 2  # 53 (UBA) and 52.1 (OpenAQ)
    env.web.overrides["entsoe_a80_unplanned.xml"] = "entsoe_a80_unplanned_rev2.xml"
    env.acquire("entsoe")
    SubscriptionStore(env.conn).commit_watermark(NS, 2, kind="ingestion", detail={"run": "revision"})
    second = monitor.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert [n["kind"] for n in second["notifications"]] == ["updated_unavailability"]
    assert monitor.run(created["subscription_id"], 2, principal_id="alice", scopes=SCOPES)["status"] == "replayed"


def test_row_dossier_export(journey):
    env, dossiers, dossier = journey
    exported = dossiers.export(NS, dossier["dossier_id"], scopes=SCOPES, principal_id="alice")
    assert exported["contract"] == "noesis-environment-dossier-export-v1" and exported["sha256"]
    for text in ("## Observations", "## Model output (reanalysis)", "## Forecasts", "## Grid (bidding zone DE-LU)",
                 "## Facilities", "## Layers", "## Coverage gaps"):
        assert text in exported["markdown"]
    assert exported["dossier"]["evidence_kind"].startswith("as acquired")


def test_row_forecast_values_are_never_shown_as_observations(journey):
    _, _, dossier = journey
    observations = [s for entry in dossier["sections"]["observations"] for s in entry["series"]]
    assert observations and all(s["kind"] == "observation" for s in observations)
    assert not any(s["provider"].startswith("open-meteo") for s in observations)
    assert all(s["kind"] == "forecast" and s["kind_notice"] for s in dossier["sections"]["forecasts"])
    assert all(s["kind"] == "model" and "reanalysis" in s["title"] for s in dossier["sections"]["model_output"])
    forecasts = [e for e in dossier["sections"]["grid"] if e["kind"] == "forecast"]
    assert forecasts and all(e["kind_notice"] == "forecast values; never an observation" for e in forecasts)


def test_row_provisional_versus_validated_vintages():
    env = harness.Env()
    env.acquire("uba")
    env.acquire("dwd")
    env.tick(86400)
    env.web.overrides["uba_measures_282_5.json"] = "uba_measures_282_5_validated.json"
    env.acquire("uba", {**harness.selections()["uba"], "data_status": "validated"})
    store = EnvironmentStore(env.conn)
    uba = record_id(NS, "observation_series", "uba", "282:5:2")
    assert [v["status"] for v in store.vintages(NS, uba, scopes=SCOPES)] == ["provisional", "validated"]
    assert {v["status"] for v in store.series(NS, uba, scopes=SCOPES)["values"]} == {"validated"}
    dwd = store.series(NS, record_id(NS, "observation_series", "dwd", "00399:TT_TU"), scopes=SCOPES)
    assert {v["status"] for v in dwd["values"]} == {"provisional"} and "quality controlled" in dwd["status_basis"]


def test_row_places_without_coverage(journey):
    env, dossiers, _ = journey
    remote = env.place("Remote fixture point", [2.3522, 48.8566])
    gap = dossiers.build(NS, "remote", principal_id="alice", scopes=SCOPES, place_id=remote["place_id"])
    assert gap["status"] == "coverage_gap" and len(gap["coverage_gaps"]) == 6


def test_row_unit_conversion_through_pint(journey):
    pytest.importorskip("pint")  # optional unit-evaluation dependency, as in the other pint tests
    env, _, _ = journey
    series = EnvironmentStore(env.conn).series(NS, record_id(NS, "observation_series", "openaq", "sensor:7771"),
                                               scopes=SCOPES, unit="mg/m³")
    last = series["values"][-1]
    assert (last["value"], last["unit"], last["converted"]["value"], last["converted"]["unit"]) == (
        "52.1", "µg/m³", "0.052100", "milligram / meter ** 3")
    assert last["converted"]["receipt_sha256"] and "pint" in last["converted"]["method"]


def test_row_namespace_isolation(journey):
    env, dossiers, dossier = journey
    outsider = {"knowledge:environment:read", "knowledge:environment:write", "namespace:other:read", "namespace:other:write"}
    with pytest.raises(EnvironmentStoreError):
        EnvironmentStore(env.conn).records(NS, scopes=outsider)
    assert EnvironmentStore(env.conn).records("other", scopes=outsider) == []
    with pytest.raises(EnvironmentStoreError):
        dossiers.inspect("other", dossier["dossier_id"], scopes=outsider, principal_id="alice")
    with pytest.raises(EnvironmentStoreError):
        dossiers.inspect(NS, dossier["dossier_id"], scopes=SCOPES, principal_id="mallory")
