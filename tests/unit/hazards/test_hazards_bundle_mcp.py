"""NH13 (#2366): the Natural Hazards bundle composes, resolves, exports cited evidence and exposes its MCP tools."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.composition.contracts import validate_composition_manifest, validate_provider_descriptor
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.ingestion.source_pack_runtime import PROJECTORS
from src.kb.hazards_bundle import BUNDLE, export_bundle, readiness, set_enabled
from src.kb.hazards_queries import events_affecting
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.composition.test_migration import _migrated
from tests.unit.hazards import harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.hazards import HAZARDS_TOOLS, HAZARDS_WRITES

ROOT = h.ROOT


@pytest.fixture()
def isolated_registry():
    """``_migrated`` cuts every bundle over; restore the legacy registry for later tests."""
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])
SHARED = {"geospatial.core", "osint.core", "platform.entity-identity", "platform.subscriptions",
          "platform.source-runtime"}


def test_manifest_descriptor_and_composition_view_validate_and_declare_dependencies():
    manifest = json.loads((ROOT / "packs/natural-hazards/manifest.json").read_text())
    descriptor = json.loads((ROOT / "packs/natural-hazards/providers/hazards.core.json").read_text())
    view = json.loads((ROOT / "packs/natural-hazards/composition.json").read_text())
    assert validate_composition_manifest(manifest) == [] and validate_provider_descriptor(descriptor) == []
    assert view["requires"] == manifest["requires"] and view["optional_features"] == manifest["optional_features"]
    assert view["exclusions"] == manifest["advisory"]["exclusions"]
    required = {r["capability"] for r in manifest["requires"]}
    assert {"geospatial.place-resolution", "osint.corroboration", "platform.subscriptions",
            "platform.source-acquisition"} <= required
    features = {f["id"]: f for f in manifest["optional_features"]}
    assert features["environment-links"]["requires"][0]["capability"] == "environment.records"
    assert features["weather-warnings"]["requires"][0]["capability"] == "weather.warnings"
    assert features["glofas"]["default"] is False  # behind the NH01 access decision
    assert PROJECTORS["noesis-hazard-record-v1"].__name__ == "_hazard_projector"
    assert all(s["store"].startswith("src.kb.hazards_") for s in descriptor["stores"])
    declared = {t for w in BUNDLE["contributions"]["workflows"].values() for t in w["tools"]}
    assert declared <= HAZARDS_TOOLS
    assert "risk scores" in BUNDLE["never"] and any("advice" in item for item in BUNDLE["never"])


def test_bundle_resolves_to_its_provider_plus_shared_providers_and_disables_as_a_selection_change(isolated_registry):
    conn, coordinator, _, _ = _migrated()
    plan = coordinator.active()["plan"]
    bound = {b["provider"] for b in plan["bindings"] if "natural-hazards" in b["consumers"]}
    assert bound == {"hazards.core"} | SHARED
    omitted = {o["feature"]: o for o in plan.get("omissions") or [] if o.get("pack") == "natural-hazards"}
    assert "weather-warnings" in omitted and "unavailable" in omitted["weather-warnings"]["reason"]
    owned = {s["store"] for d in provider_descriptors() if d["id"] == "hazards.core" for s in d["stores"]}
    others = {s["store"] for d in provider_descriptors() if d["id"] != "hazards.core" for s in d["stores"]}
    assert not owned & others  # no parallel spatial, entity, subscription or source store
    result = set_enabled(conn, "hazards", False, principal_id="operator", scopes={"operator"})
    assert result["authority"] == "composition-coordinator" and result["enabled"] is False
    plan = coordinator.active()["plan"]
    assert "natural-hazards" not in {p["id"] for p in plan["packs"]} and "geospatial" in {p["id"] for p in plan["packs"]}
    again = set_enabled(conn, "hazards", True, principal_id="operator", scopes={"operator"})
    assert again["enabled"] is True


def test_evidence_bundle_cites_every_record_with_source_revision_and_as_of():
    conn = h.connection()
    h.load_all(conn)
    answer = events_affecting(conn, h.NS, point=[26.70, 37.79], radius_m=50_000, start="2099-08-01", end="2099-08-31",
                              as_of="2099-08-12T00:00:00Z", scopes=h.SCOPES, principal_id="analyst")
    exported = export_bundle(answer, created_at_ms=h.ms("2099-09-03T00:00:00Z"))
    bundle = exported["bundle"]
    assert bundle["contract"] == "noesis-evidence-bundle-v1" and bundle["operation"]["as_of_ms"] == h.ms("2099-08-12")
    cited = {(c["record_id"], c["revision_id"]) for c in exported["citations"]}
    assert cited == {(e["record_id"], e["revision_used"]["revision_id"]) for e in answer["events"]}
    assert all(c["source_url"].startswith("https://") and c["published_at"] and c["as_of"] for c in exported["citations"])
    root = next(o for o in bundle["objects"] if o["id"] == "natural-hazards-answer")
    assert root["payload"]["exclusions"] == list(BUNDLE["never"])
    status = readiness(conn, h.NS, scopes=h.SCOPES)
    assert status["providers"]["usgs"]["status"] == "fixture-only"
    assert status["linked_providers"]["weather"]["status"] == "unavailable"


@pytest.fixture
def tools(tmp_path, monkeypatch):
    path = str(tmp_path / "hazards.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.REVIEW_SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_mcp_tools_have_declared_scopes_mutability_and_exclusions(tools):
    registered, state = tools
    assert HAZARDS_TOOLS <= set(registered)
    for name in HAZARDS_TOOLS:
        assert _mutability(name) == ("write" if name in HAZARDS_WRITES else "read"), name
    assert _required_scopes("knowledge_engine_mcp", "read", "hazard_provider_contracts") == []
    assert _required_scopes("knowledge_engine_mcp", "write", "review_hazard_correspondence") == ["knowledge:hazards:review"]
    contracts = registered["hazard_provider_contracts"].fn()
    assert contracts["live_verification"]["glofas"]["status"] == "key-gated" and "risk scores" in contracts["exclusions"]
    events = registered["hazard_events_for_place"].fn(namespace=h.NS, start="2099-08-01", end="2099-08-31",
                                                      point=[26.70, 37.79], radius_m=50_000)
    assert {e["native_id"] for e in events["events"]} >= {"us7000zz01", "20990810_0000031"}
    record_id = next(e["record_id"] for e in events["events"] if e["native_id"] == "us7000zz01")
    history = registered["hazard_event_revisions"].fn(namespace=h.NS, record_id=record_id)
    assert len(history["revisions"]) == 2
    proposed = registered["propose_hazard_correspondences"].fn(namespace=h.NS)
    candidate = proposed["correspondences"][0]["correspondence_id"]
    refused = registered["review_hazard_correspondence"].fn(namespace=h.NS, correspondence_id=candidate,
                                                            decision="accept", reason="same event")
    assert refused["ok"] is False and refused["error"]["code"] == "self_review"
    state["principal"] = "bob"
    accepted = registered["review_hazard_correspondence"].fn(namespace=h.NS, correspondence_id=candidate,
                                                             decision="accept", reason="same event")
    assert accepted["state"] == "accepted"
    alerts = registered["hazard_alerts_in_force"].fn(namespace=h.NS, at="2099-08-22T00:00:00Z", point=[26.3, 41.6],
                                                     radius_m=10_000, country="GRC")
    assert any(a["provider"] == "glofas" for a in alerts["alerts"])
    state["scopes"] = {"knowledge:read"}
    denied = registered["hazard_events_for_place"].fn(namespace=h.NS, start="2099-08-01", end="2099-08-31",
                                                      point=[26.70, 37.79], radius_m=50_000)
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
