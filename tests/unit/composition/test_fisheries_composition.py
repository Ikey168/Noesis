"""The Fisheries bundle composes its provider with platform providers and optional sanctions/geospatial (#2339)."""

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
from tools.knowledge_engine_mcp.fisheries import FISHERIES_READS, FISHERIES_TOOLS, required_scopes

ROOT = Path(__file__).resolve().parents[3]
BOUND = {"fisheries.core", "platform.entity-identity", "platform.subscriptions", "platform.source-runtime"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def _bound(features=None):
    bundles = adapt_all()
    root = {"pack": "fisheries", "version": bundles["fisheries"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    return {b["provider"] for b in result.plan["bindings"] if "fisheries" in b["consumers"]}, result.plan


def test_pack_files_validate_and_reference_the_source_pack():
    pack = json.loads((ROOT / "packs/fisheries/pack.json").read_text())
    assert validate_manifest(pack) == []
    source_pack = validate_source_pack(json.loads((ROOT / pack["source_pack"]).read_text()))
    assert source_pack["pack_id"] == "fisheries-maritime"
    assert validate_composition_manifest(adapt_all()["fisheries"]) == []
    features = {f["id"]: f for f in adapt_all()["fisheries"]["optional_features"]}
    assert set(features) == {"sanctions", "geospatial"} and not any(f["default"] for f in features.values())


def test_descriptor_declares_operations_with_the_catalog_scopes_stores_and_probe():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "fisheries.core")
    assert validate_provider_descriptor(descriptor) == []
    tools = {o["tool"].split(".", 1)[1]: o for o in descriptor["operations"]}
    assert set(tools) == FISHERIES_TOOLS - {"set_fisheries_bundle_enabled"}
    for name, operation in tools.items():
        mutability = "read" if name in FISHERIES_READS else "write"
        assert operation["required_scopes"] == required_scopes(name, mutability), name
        assert (operation["side_effect"] == "read-only") == (name in FISHERIES_READS), name
    catalog = json.loads((ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    listed = {t["name"]: t for t in catalog["tools"] if t["name"] in tools}
    assert all(listed[n]["required_scopes"] == tools[n]["required_scopes"] for n in tools)
    owned = {s["store"] for s in descriptor["stores"]}
    others = {s["store"] for d in provider_descriptors() if d["id"] != "fisheries.core" for s in d["stores"]}
    assert owned == {"src.kb.fisheries_store", "src.kb.fisheries_identity"}
    assert not owned & others  # no parallel entity, spatial, sanctions or subscription store
    assert descriptor["readiness_probes"] == [{"id": "revisions", "kind": "table-exists",
                                               "target": "fisheries_revisions"}]


def test_features_off_bind_only_platform_providers_and_on_bind_sanctions_and_geospatial():
    bound, plan = _bound()
    assert bound == BOUND
    assert {"pack_id": "fisheries-maritime", "version": "1.0.0", "range": "^1.0.0"} in plan["source_packs"]
    with_features, _ = _bound(["sanctions", "geospatial"])
    assert with_features == BOUND | {"legal.sanctions", "geospatial.core"}


def test_disabling_fisheries_is_a_selection_change_that_keeps_shared_providers():
    from src.kb import fisheries_bundle

    conn, coordinator, _, _ = _migrated()
    assert fisheries_bundle.is_enabled(conn, "global")
    result = fisheries_bundle.set_enabled(conn, "global", False, principal_id="operator", scopes={"operator"})
    assert result["authority"] == "composition-coordinator" and result["enabled"] is False
    assert result["receipt"]["status"] == "published"
    plan = coordinator.active()["plan"]
    assert "fisheries" not in {p["id"] for p in plan["packs"]}
    assert {"legal", "geospatial"} <= {p["id"] for p in plan["packs"]}
    again = fisheries_bundle.set_enabled(conn, "global", True, principal_id="operator", scopes={"operator"})
    assert again["enabled"] is True and again["receipt"]["status"] == "published"
