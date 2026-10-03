"""OSINT movements offline acceptance (#2221, MV14): aircraft and vessel to cited registry records and movements.

Authored fixtures for every acquired source (FAA registry, UK CAA G-INFO,
OpenSky, GFW port visits, Kystdatahuset open AIS, UNCTAD port-call aggregates)
replay through the real adapter and projector with the Fisheries pack's GFW
identity records, fictional airport and port polygons in the geospatial store
and authored sanctions snapshots. Derivation, identity review, citation links,
bounded answers, evidence export, monitors and the gated MCP surface follow.
Sockets are blocked for the whole journey. This is **offline evidence only**;
live coverage is MV15 (#2291) and is recorded separately, never here.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.ingestion.osint_movement_sources import LIVE_VERIFICATION
from src.mcp_host.introspection import tool_map
from src.osint.investigations import MOVEMENT_TOOLS
from src.osint.movement_monitoring import MovementMonitor
from src.osint.movements import (
    NO_COVERAGE,
    MovementError,
    MovementIdentity,
    MovementLinks,
    MovementQueries,
    MovementStore,
    derive_calls,
    forbidden_keys,
)
from tests.unit.osint.movement_harness import (
    ALL,
    IMO,
    MMSI_NEW,
    NS,
    VESSEL,
    Env,
    facilities,
    load_fisheries_all,
    seed_sanctions,
)

ROOT = Path(__file__).resolve().parents[3]
FAC = "movements-facilities"
EVIDENCE = "offline-fixture"  # never reported as live evidence


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the offline journey opened a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def _accept(identity, candidates, a, b):
    candidate = next(c for c in candidates if {c["left_key"], c["right_key"]} == {f"movements:{a}", f"movements:{b}"})
    return identity.review(NS, candidate["candidate_id"], "accept", "identifiers stated together by the cited source",
                           principal_id="reviewer", scopes=ALL)


def test_aircraft_and_vessel_to_cited_registry_records_sampled_movements_and_calls():
    bundles = adapt_all()
    plan = resolve([{"pack": "osint", "version": bundles["osint"]["version"], "features": ["movements"]}],
                   list(bundles.values()), provider_descriptors())
    assert plan.ok and "osint.movements" in {b["capability"] for b in plan.plan["bindings"]}

    env = Env().loaded()
    facilities(env.conn)
    load_fisheries_all(env.conn)
    seed_sanctions(env.conn)
    runs = MovementStore(env.conn, initialize=False).runs(NS)
    assert {r["evidence_origin"] for r in runs} == {"fixture"}  # offline evidence only
    assert all(v["status"] == "unverified-live" for v in LIVE_VERIFICATION.values())

    derived = derive_calls(env.conn, NS, facilities_namespace=FAC, principal_id="analyst", scopes=ALL)
    assert derived["derived"] == 4
    identity = MovementIdentity(env.conn)
    candidates = identity.propose(NS, principal_id="analyst", scopes=ALL, fisheries_namespace="global")["candidates"]
    _accept(identity, candidates, "aircraft:icao24:a0f1b2", "aircraft:registration:N901EX")
    _accept(identity, candidates, f"vessel:imo:{IMO}", f"vessel:mmsi:{MMSI_NEW}")
    _accept(identity, candidates, f"vessel:gfw:{VESSEL}", f"vessel:imo:{IMO}")
    links = MovementLinks(env.conn).link(NS, principal_id="analyst", scopes=ALL, sanctions_namespace="legal",
                                         fisheries_namespace="global")
    assert any(c["basis"] == "similar-name" for c in links["name_candidates"])
    monitor = MovementMonitor(env.conn)
    watch = monitor.create(NS, "acceptance", principal_id="analyst", scopes=ALL, identifiers=["N901EX", f"IMO {IMO}"],
                           sanctions_namespace="legal")
    assert monitor.run(watch["subscription_id"], principal_id="analyst", scopes=ALL)["baseline"]
    queries = MovementQueries(env.conn)

    # Aircraft: registry revision, one bounded window with a declared gap, published and derived calls, a listing.
    aircraft = queries.window(NS, "a0f1b2", "2099-05-01", "2099-05-02", scopes=ALL, facilities_namespace=FAC,
                              sanctions_namespace="legal", as_of="2025-03-01")
    (registry,) = aircraft["registry"]
    assert registry["record_key"] == "faa:N901EX" and registry["citation"]["evidence_origin"] == "fixture"
    (window,) = aircraft["sample_windows"]
    assert window["coverage"]["gaps"] and window["bound"]["max_window_hours"] == 48 and len(window["samples"]) == 10
    assert {c["status"] for c in aircraft["calls"]["source_published"]} == {"source-published"}
    assert {c["status"] for c in aircraft["calls"]["derived"]} == {"derived"}
    assert [m["to"] for m in aircraft["identity_matches"]["accepted"]] == ["aircraft:registration:N901EX"]
    assert any(s["status"] == "listed" and s["link"]["scheme"] == "registration" for s in aircraft["sanctions"])

    # Vessel: Fisheries identity by citation, AIS window with gaps, GFW visit with confidence, derived calls.
    vessel = queries.window(NS, f"IMO {IMO}", "2025-03-01", "2025-03-31", scopes=ALL, facilities_namespace=FAC,
                            fisheries_namespace="global", sanctions_namespace="legal", as_of="2025-03-01")
    assert {f"vessel:mmsi:{MMSI_NEW}", f"vessel:gfw:{VESSEL}"} <= set(vessel["subjects"])
    assert vessel["vessel_identity"][0]["stated_by"][0]["shared_with"].startswith("Fisheries")
    assert {w["provider"] for w in vessel["sample_windows"]} == {"kystdatahuset-ais", "gfw-port-visits"}
    assert vessel["calls"]["source_published"][0]["confidence"] == "4"
    assert {c["uncertain"] for c in vessel["calls"]["derived"]} == {True, False}
    assert any(s["status"] == "listed" for s in vessel["sanctions"] if s["link"]["target"] == "sanctions")
    assert any(s["link"]["target"] == "fisheries" for s in vessel["sanctions"])
    assert not forbidden_keys(aircraft) and not forbidden_keys(vessel)
    bundle = queries.export_bundle(vessel)["bundle"]
    assert all(o["payload"].get("as_of") == "2025-03-01" for o in bundle["objects"] if o["type"] == "evidence")

    # Negative cases: no coverage, person key, opted-out aircraft, over-bound window, natural-person aircraft.
    empty = queries.window(NS, "257000009", "2025-03-10", "2025-03-11", scopes=ALL)
    assert empty["coverage"]["statement"] == NO_COVERAGE and "did not move" not in json.dumps(
        empty["sample_windows"][0]["coverage"]["statement"])
    for identifier, start, end, code in (("Jane Q Example", "2099-05-01", "2099-05-02", "person_identifier_refused"),
                                         ("N907EX", "2099-05-01", "2099-05-02", "privacy_opt_out"),
                                         ("a0f1b2", "2099-01-01", "2099-06-01", "over_bound"),
                                         ("N902EX", "2099-05-01", "2099-05-02", "private_aircraft_refused")):
        with pytest.raises(MovementError) as refused:
            queries.window(NS, identifier, start, end, scopes=ALL)
        assert refused.value.code == code
    with pytest.raises(MovementError) as subscription:
        monitor.create(NS, "positions", principal_id="analyst", scopes=ALL, identifiers=["N901EX"], watch=["positions"])
    assert subscription.value.code == "movement_subscription_refused"


def test_gate_flag_off_serves_no_movement_tool(monkeypatch):
    for prefix in ("NOESIS", "NEURONEWS"):
        monkeypatch.delenv(f"{prefix}_OSINT_MOVEMENTS", raising=False)
        monkeypatch.delenv(f"{prefix}_OSINT_GATED_TOOLS", raising=False)
    spec = importlib.util.spec_from_file_location("osint_movements_acceptance", ROOT / "tools/osint_mcp/server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    served = tool_map(module.mcp)
    assert not set(MOVEMENT_TOOLS) & set(served) and "movement_source_contracts" in served
