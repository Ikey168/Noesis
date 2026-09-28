"""The Materials bundle composes like the other packs (MT12, #2090)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import (
    validate_composition_manifest,
    validate_provider_descriptor,
)
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.domains.pack_format import validate_manifest
from src.ingestion.source_pack_runtime import PROJECTORS
from src.kb import materials_bundle
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.materials import MATERIALS_SCOPES, MATERIALS_TOOLS

ROOT = Path(__file__).resolve().parents[3]
OWN = {"materials.computed", "materials.experimental", "materials.structures"}
SHARED = {
    "science.literature",
    "technology.standards",
    "platform.source-runtime",
    "platform.subscriptions",
    "platform.entity-identity",
    "ownership.core",
}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (
        dict(domain_registry._REGISTRY),
        set(domain_registry._ENABLED),
        domain_registry._AUTHORITY,
    )
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def test_v1_manifest_declares_capabilities_examples_exclusions_and_source_pack():
    data = json.loads((ROOT / "packs/materials/pack.json").read_text())
    assert validate_manifest(data) == []
    assert data["source_pack"] == "config/source_packs/materials.json"
    assert set(data["exclusions"]) == set(materials_bundle.EXCLUSIONS)
    assert {e["tool"] for e in data["query_examples"]} <= MATERIALS_TOOLS
    assert all(e["semantics"] for e in data["query_examples"])


def test_descriptors_are_valid_and_every_tool_is_bound_with_its_declared_scopes():
    descriptors = {d["id"]: d for d in provider_descriptors() if d["id"] in OWN}
    assert set(descriptors) == OWN
    bound = {}
    for descriptor in descriptors.values():
        assert validate_provider_descriptor(descriptor) == [], descriptor["id"]
        for operation in descriptor["operations"]:
            bound[operation["tool"].split(".", 1)[1]] = operation["required_scopes"]
    assert bound == MATERIALS_SCOPES
    stores = {s["store"] for d in descriptors.values() for s in d["stores"]}
    others = {
        s["store"]
        for d in provider_descriptors()
        if d["id"] not in OWN
        for s in d["stores"]
    }
    assert (
        stores == {"src.kb.materials_store", "src.kb.materials_releases"}
        and not stores & others
    )
    assert PROJECTORS["noesis-material-record-v1"].__name__ == "_materials_projector"


def test_bundle_resolves_to_its_providers_plus_shared_ones_and_disables_as_a_selection(
    isolated_registry,
):
    manifest = adapt_all()["materials"]
    assert validate_composition_manifest(manifest) == []
    conn, coordinator, _, _ = _migrated()
    plan = coordinator.active()["plan"]
    bound = {b["provider"] for b in plan["bindings"] if "materials" in b["consumers"]}
    assert bound == OWN | SHARED
    assert ("materials", "^1.0.0") in {
        (p["pack_id"], p.get("range")) for p in plan["source_packs"]
    }
    assert materials_bundle.is_enabled(conn)
    receipt = coordinator.disable("materials", "materials:disable:test")
    assert receipt["status"] == "published" and not materials_bundle.is_enabled(conn)
    plan = coordinator.active()["plan"]
    standards = next(
        b for b in plan["bindings"] if b["capability"] == "technology.standards"
    )
    assert (
        "materials" not in standards["consumers"]
        and standards["provider"] == "technology.standards"
    )
    with pytest.raises(materials_bundle.BundleError):
        materials_bundle.require_enabled(conn)
