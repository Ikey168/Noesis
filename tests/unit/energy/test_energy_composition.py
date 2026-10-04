"""EN12 (#2265): the Energy Systems bundle, its provider descriptor, source pack and MCP tools."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import validate_composition_manifest, validate_provider_descriptor
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.ingestion.source_packs import validate_source_pack
from src.kb import energy_bundle
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.energy.harness import NS, SCOPES, acquire_all
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.energy import ENERGY_SCOPES, ENERGY_TOOLS, ENERGY_WRITES
from src.mcp_host.introspection import tool_map

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = json.loads((ROOT / "packs/energy/manifest.json").read_text())
COMPOSITION = json.loads((ROOT / "packs/energy/composition.json").read_text())
SHARED = {"market.core", "geospatial.core", "platform.entity-identity", "platform.subscriptions",
          "platform.source-runtime"}


def test_manifest_is_valid_native_and_the_composition_view_matches_it():
    assert validate_composition_manifest(MANIFEST) == []
    adapted = adapt_all()
    assert "adapter" not in adapted["energy"] and adapted["energy"]["content_hash"] == MANIFEST["content_hash"]
    assert COMPOSITION["requires"] == MANIFEST["requires"]
    assert COMPOSITION["exclusions"] == MANIFEST["advisory"]["exclusions"]
    assert COMPOSITION["optional_features"] == MANIFEST["optional_features"]
    assert [(f["id"], f["default"]) for f in MANIFEST["optional_features"]] == [("environment-citations", False)]
    for exclusion in ("price forecasting", "dispatch modelling", "emissions estimation beyond quoting the publisher",
                      "trading advice"):
        assert exclusion in MANIFEST["advisory"]["exclusions"]
    assert {c["id"] for c in MANIFEST["contributes"]["capabilities"]} == {
        "energy.records", "energy.queries", "energy.identity", "energy.links", "energy.monitoring"}


def test_descriptor_declares_operations_scopes_stores_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "energy.core")
    assert validate_provider_descriptor(descriptor) == []
    tools = {op["tool"].split(".", 1)[1]: op for op in descriptor["operations"]}
    assert set(tools) == ENERGY_TOOLS - {"set_energy_bundle_enabled"}
    for name, operation in tools.items():
        assert operation["required_scopes"] == ENERGY_SCOPES[name]
        assert (operation["side_effect"] == "read-only") == (name not in ENERGY_WRITES)
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "energy.core" for s in d["stores"]}
    assert not owned & others
    assert all(s["store"].startswith("src.kb.energy_") for s in descriptor["stores"])
    pack = validate_source_pack(json.loads((ROOT / "config/source_packs/energy.json").read_text()))
    assert descriptor["source_packs"] == [{"pack_id": pack["pack_id"], "version": pack["version"], "range": "^1.0.0"}]


def test_plan_binds_the_bundle_provider_and_shared_providers_without_duplicates():
    bundles = adapt_all()
    result = resolve([{"pack": "energy", "version": bundles["energy"]["version"]}], list(bundles.values()),
                     provider_descriptors())
    assert result.ok, result.failure
    bound = {b["provider"] for b in result.plan["bindings"] if "energy" in b["consumers"]}
    assert bound == {"energy.core"} | SHARED
    with_links = resolve([{"pack": "energy", "version": bundles["energy"]["version"],
                           "features": ["environment-citations"]}], list(bundles.values()), provider_descriptors())
    assert with_links.ok, with_links.failure
    assert "environment.core" in {b["provider"] for b in with_links.plan["bindings"] if "energy" in b["consumers"]}
    assert ("energy-systems", "^1.0.0") in {(p["pack_id"], p.get("range")) for p in result.plan["source_packs"]}


def test_disabling_energy_blocks_only_energy_entry_points():
    conn = duckdb.connect()
    acquire_all(conn, ["energy-entsoe"])
    status = energy_bundle.readiness(conn, NS, scopes=SCOPES, now=lambda: 1_790_400_000_000)
    assert status["providers"]["entsoe"]["status"] == "fixture-only"
    assert status["providers"]["eia"]["status"] == "unavailable"
    assert status["providers"]["eia"]["live_verification"]["status"] == "unverified-live"
    with pytest.raises(energy_bundle.BundleError):
        energy_bundle.set_enabled(conn, NS, False, principal_id="analyst", scopes=SCOPES)
    energy_bundle.set_enabled(conn, NS, False, principal_id="operator", scopes={"operator"})
    with pytest.raises(energy_bundle.BundleError) as disabled:
        energy_bundle.require_enabled(conn, NS)
    assert disabled.value.code == "bundle_disabled" and "Climate and Environment" in str(disabled.value)
    assert energy_bundle.is_enabled(conn, "other-namespace")


# ------------------------------------------------------------------ MCP tools


@pytest.fixture()
def mcp(tmp_path, monkeypatch):
    path = str(tmp_path / "energy.duckdb")
    conn = duckdb.connect(path)
    acquire_all(conn, ["energy-entsoe", "energy-ember-generation", "energy-charts-public-power"])
    conn.close()
    state = {"principal": "analyst", "scopes": set()}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return tool_map(server.mcp), state


def call(tools, state, name, scopes, principal="analyst", **kwargs):
    state["scopes"], state["principal"] = set(scopes), principal
    return tools[name].fn(**kwargs)


def test_tools_are_registered_in_the_catalog_with_their_scopes(mcp):
    tools, _ = mcp
    assert ENERGY_TOOLS <= set(tools) and set(ENERGY_SCOPES) == ENERGY_TOOLS
    catalog = json.loads((ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in ENERGY_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in ENERGY_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == ENERGY_SCOPES[name]
        assert by_name[name]["required_scopes"] == ENERGY_SCOPES[name]


def test_queries_identity_and_monitors_through_the_tools(mcp):
    tools, state = mcp
    read = {"knowledge:energy:read", f"namespace:{NS}:read"}
    contracts = call(tools, state, "energy_source_contracts", set())
    assert set(contracts["contracts"]) == {"entsoe", "eia", "ember", "eurostat", "energy-charts"}
    denied = call(tools, state, "energy_generation_mix", set(), namespace=NS, subject="DEU")
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    mix = call(tools, state, "energy_generation_mix", read, namespace=NS, subject="10Y1001A1001A82H")
    assert mix["status"] == "answered" and mix["sources"][0]["source"] == "entsoe:entsoe:A75:A16"
    write = SCOPES | {"knowledge:energy:review"}
    proposed = call(tools, state, "propose_energy_identity_matches", write, namespace=NS)
    deu = next(m for m in proposed["proposed"] if m["subject"]["code"] == "DEU")
    self_review = call(tools, state, "review_energy_identity_match", write, namespace=NS, match_id=deu["match_id"],
                       decision="accept", reason="checked")
    assert self_review["error"]["code"] == "self_review"
    accepted = call(tools, state, "review_energy_identity_match", write, principal="reviewer", namespace=NS,
                    match_id=deu["match_id"], decision="accept", reason="checked")
    assert accepted["state"] == "accepted"
    history_series = call(tools, state, "list_energy_series", read, namespace=NS, record_type="price")["series"][0]
    history = call(tools, state, "energy_revision_history", read, namespace=NS, series_id=history_series["series_id"])
    assert len(history["vintages"]) == 1
    capacity = call(tools, state, "energy_capacity_as_of", read, namespace=NS, subject="11WD2FIXTURE0001",
                    date="2026-06-01")
    assert capacity["capacity"][0]["capacity_on_date"] == "1400"
    monitor = call(tools, state, "create_energy_monitor", write, namespace=NS, request_key="zone",
                   subject="10Y1001A1001A82H", record_types=["price"])
    assert monitor["subscription_id"].startswith("subscription:")
    off = call(tools, state, "set_energy_bundle_enabled", {"operator"}, principal="operator", namespace=NS,
               enabled=False)
    assert off["enabled"] is False
    blocked = call(tools, state, "energy_generation_mix", read, namespace=NS, subject="DEU")
    assert blocked["error"]["code"] == "bundle_disabled"
