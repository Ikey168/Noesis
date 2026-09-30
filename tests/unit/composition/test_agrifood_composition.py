"""The Agriculture and Food Systems bundle composes its provider with Economics, Products,
Geospatial and platform providers (#2365)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import validate_composition_manifest, validate_provider_descriptor
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.domains.pack_format import validate_manifest
from src.ingestion.source_packs import validate_source_pack
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.agrifood import AGRIFOOD_READS, AGRIFOOD_TOOLS, required_scopes

ROOT = Path(__file__).resolve().parents[3]
SHARED = {"economics.core", "products.safety", "geospatial.core", "platform.subscriptions",
          "platform.source-runtime"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def test_pack_files_validate_and_reference_the_source_pack():
    pack = json.loads((ROOT / "packs/agrifood/pack.json").read_text())
    assert validate_manifest(pack) == []
    source_pack = validate_source_pack(json.loads((ROOT / pack["source_pack"]).read_text()))
    assert source_pack["pack_id"] == "agrifood"
    assert {s["agrifood"]["provider"] for s in source_pack["sources"]} == {
        "faostat", "nass-quickstats", "fas-psd", "eurostat-agri", "agri-food-portal"}
    assert validate_composition_manifest(adapt_all()["agrifood"]) == []


def test_descriptor_declares_operations_with_the_catalog_scopes_stores_and_probe():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "agrifood.core")
    assert validate_provider_descriptor(descriptor) == []
    tools = {o["tool"].split(".", 1)[1]: o for o in descriptor["operations"]}
    assert set(tools) <= AGRIFOOD_TOOLS
    for name, operation in tools.items():
        mutability = "read" if name in AGRIFOOD_READS else "write"
        assert operation["required_scopes"] == required_scopes(name, mutability), name
        assert (operation["side_effect"] == "read-only") == (name in AGRIFOOD_READS), name
    catalog = json.loads((ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    listed = {t["name"]: t for t in catalog["tools"] if t["name"] in tools}
    assert all(listed[n]["required_scopes"] == tools[n]["required_scopes"] for n in tools)
    owned = {s["store"] for s in descriptor["stores"]}
    others = {s["store"] for d in provider_descriptors() if d["id"] != "agrifood.core" for s in d["stores"]}
    assert owned == {"src.kb.agrifood_store", "src.kb.agrifood_identity", "src.kb.agrifood_links"}
    assert not owned & others  # no parallel series, spatial, notice or subscription store
    assert descriptor["readiness_probes"] == [{"id": "values", "kind": "table-exists", "target": "agrifood_values"}]


def test_the_bundle_resolves_to_its_provider_plus_shared_providers():
    bundles = adapt_all()
    result = resolve([{"pack": "agrifood", "version": bundles["agrifood"]["version"]}], list(bundles.values()),
                     provider_descriptors())
    assert result.ok, result.failure
    bound = {b["provider"] for b in result.plan["bindings"] if "agrifood" in b["consumers"]}
    assert bound >= {"agrifood.core"} | SHARED
    # Climate & Environment is read by citation when present, never required: disabling it stays possible.
    assert "environment.core" not in bound
    assert {"pack_id": "agrifood", "version": "1.0.0", "range": "^1.0.0"} in result.plan["source_packs"]


def test_disabling_agrifood_is_a_selection_change_that_keeps_shared_packs():
    from src.kb import agrifood_bundle

    conn, coordinator, _, _ = _migrated()
    assert agrifood_bundle.is_enabled(conn, "global")
    result = agrifood_bundle.set_enabled(conn, "global", False, principal_id="operator", scopes={"operator"})
    assert result["authority"] == "composition-coordinator" and result["enabled"] is False
    assert result["receipt"]["status"] == "published"
    plan = coordinator.active()["plan"]
    assert "agrifood" not in {p["id"] for p in plan["packs"]}
    assert {"economics", "products", "geospatial"} <= {p["id"] for p in plan["packs"]}
    again = agrifood_bundle.set_enabled(conn, "global", True, principal_id="operator", scopes={"operator"})
    assert again["enabled"] is True and again["receipt"]["status"] == "published"
