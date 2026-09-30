"""The Geospatial bundle's optional ``real-estate`` feature and the ``geospatial.real-estate`` provider (#2512)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import satisfies, validate_composition_manifest, validate_provider_descriptor
from src.composition.readiness import CompositionView
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb.real_estate import feature_enabled, readiness
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def plan_for(features=None, descriptors=None):
    bundles = adapt_all()
    root = {"pack": "geospatial", "version": bundles["geospatial"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()),
                     descriptors if descriptors is not None else provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "geospatial" in b["consumers"]}


def test_descriptor_validates_offline_with_read_only_record_operations_probe_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "geospatial.real-estate")
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["implementation"]["name"] == "src.kb.real_estate"
    ops = {o["id"]: o for o in descriptor["operations"]}
    for name in ("list-records", "inspect-record", "parcel-revisions", "list-vintages"):
        assert ops[name]["side_effect"] == "read-only"
    assert descriptor["readiness_probes"] == [{"id": "records", "kind": "table-exists",
                                               "target": "real_estate_revisions"}]
    manifest = json.loads((ROOT / "packs/geospatial/source_packs/geospatial-real-estate.json").read_text())
    assert satisfies(manifest["version"], descriptor["source_packs"][0]["range"])
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != descriptor["id"] for s in d["stores"]}
    assert not owned & others
    from tools.knowledge_engine_mcp.real_estate import REAL_ESTATE_SCOPES, REAL_ESTATE_TOOLS

    assert set(REAL_ESTATE_SCOPES) == REAL_ESTATE_TOOLS
    for op in descriptor["operations"]:
        assert op["required_scopes"] == REAL_ESTATE_SCOPES[op["tool"].split(".", 1)[1]]


def test_feature_is_off_by_default_beside_housing_and_leaves_the_bundle_pin_alone():
    composition = json.loads((ROOT / "packs/geospatial/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert features["real-estate"]["default"] is False and "housing" in features
    assert composition["contributes"]["source_packs"] == [
        {"pack_id": "geospatial-berlin", "version": "1.1.0", "range": "^1.1.0"}]
    assert validate_composition_manifest(adapt_all()["geospatial"]) == []


@pytest.mark.parametrize("selection", [[], ["real-estate"], ["housing", "real-estate"]])
def test_each_selection_binds_the_provider_only_when_selected(selection):
    plan = plan_for(selection)
    assert ("geospatial.real-estate" in bound(plan)) == ("real-estate" in selection)
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.real_estate_place_as_of" in view.tools) == ("real-estate" in selection)


def test_a_missing_legal_provider_is_a_visible_omission():
    descriptors = [d for d in provider_descriptors() if d["id"] != "legal.core"]
    plan = plan_for(["real-estate"], descriptors)
    assert "geospatial.real-estate" not in bound(plan)
    omission = next(o for o in plan["omissions"] if o["feature"] == "real-estate")
    assert omission["capability"] == "legal.works"


def test_readiness_and_feature_enablement_follow_the_active_selection():
    assert readiness(duckdb.connect(":memory:"))["selected"] is False
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("geospatial", bundles["geospatial"]["version"], features=["real-estate"])
    assert coordinator.activate("geospatial-real-estate-on")["status"] == "published"
    assert feature_enabled(conn) is True


def test_tools_are_in_the_catalog_with_their_mutability_and_scopes():
    from tools.knowledge_engine_mcp.real_estate import REAL_ESTATE_SCOPES, REAL_ESTATE_TOOLS, REAL_ESTATE_WRITES

    catalog = json.loads((ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    tools = {t["name"]: t for t in catalog["tools"] if t["server"] == "noesis-knowledge-engine"}
    assert REAL_ESTATE_TOOLS <= set(tools)
    for name in REAL_ESTATE_TOOLS:
        assert tools[name]["mutability"] == ("write" if name in REAL_ESTATE_WRITES else "read"), name
        assert tools[name]["required_scopes"] == REAL_ESTATE_SCOPES[name], name
