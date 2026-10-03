"""The Climate and Environment bundle's optional waste features and the ``environment.waste`` provider (#2740, WC11
#2801)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import duckdb
import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import (
    validate_composition_manifest,
    validate_provider_descriptor,
)
from src.composition.readiness import CompositionView
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.waste import (
    FEATURES,
    WASTE_SCOPES,
    WASTE_TOOLS,
    WASTE_WRITES,
    readiness,
    required_scopes,
    selected_features,
)

ROOT = Path(__file__).resolve().parents[3]
PACK = {"pack_id": "climate-environment-waste", "version": "1.0.0", "range": "^1.0.0"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def plan_for(features=None):
    bundles = adapt_all()
    root = {"pack": "climate-environment", "version": bundles["climate-environment"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "climate-environment" in b["consumers"]}


def test_descriptor_declares_capabilities_constraints_scopes_stores_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "environment.waste")
    assert validate_provider_descriptor(descriptor) == []
    assert {c["id"] for c in descriptor["capabilities"]} == {
        "environment.waste-records", "environment.waste-identity", "environment.waste-monitoring"}
    records = descriptor["capabilities"][0]
    assert records["contract"] == {"name": "noesis-waste-record", "version": "2.0.0"}
    constraints = records["semantic_constraints"]
    assert {"series", "transfers", "vintages", "absence", "side_by_side", "links", "minimisation",
            "exclusions"} <= set(constraints)
    assert "no blending of Eurostat, OECD and EEA figures" in constraints["exclusions"]
    assert "no second facility register" in constraints["transfers"]
    assert "provider_absent" in constraints["links"]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "environment.waste" for s in d["stores"]}
    assert not owned & others
    assert all(t.startswith("waste_") for s in descriptor["stores"] for t in s["tables"])
    assert {o["tool"].split(".", 1)[1] for o in descriptor["operations"]} == WASTE_TOOLS
    for operation in descriptor["operations"]:
        name = operation["tool"].split(".", 1)[1]
        mutability = "write" if name in WASTE_WRITES else "read"
        assert operation["required_scopes"] == required_scopes(name, mutability) == WASTE_SCOPES[name], name
        assert (operation["side_effect"] == "read-only") == (name not in WASTE_WRITES)
    catalog = json.loads((ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    listed = {t["name"]: t for t in catalog["tools"] if t["name"] in WASTE_TOOLS}
    assert set(listed) == WASTE_TOOLS
    assert all(listed[n]["mutability"] == ("write" if n in WASTE_WRITES else "read") for n in listed)
    assert all(listed[n]["required_scopes"] == WASTE_SCOPES[n] for n in listed)


def test_sources_are_separate_optional_features_off_by_default_and_links_are_never_required():
    manifest = json.loads((ROOT / "packs/climate-environment/manifest.json").read_text())
    assert validate_composition_manifest(manifest) == []
    assert manifest["version"] == "0.1.1"  # additive bump; ^0.1.0 pins still resolve
    features = {f["id"]: f for f in manifest["optional_features"]}
    assert set(FEATURES.values()) <= set(features)
    for feature in FEATURES.values():
        assert features[feature]["default"] is False
        required = {r["capability"] for r in features[feature]["requires"]}
        assert not {c for c in required if c.startswith(("chemicals.", "products.", "substances."))}
        assert "provider_absent" in features[feature]["description"]
    assert "environment.records" in {r["capability"] for r in features["waste-eea-transfers"]["requires"]}
    assert not any(r["capability"].startswith("environment.waste") for r in manifest["requires"])
    profile = next(p for p in manifest["contributes"]["profiles"] if p["id"] == "environment.waste")
    assert {"biennial", "truncated", "removed_by_source", "transfer row"} <= set(profile["vocabulary"])
    off = plan_for()
    assert "environment.waste" not in bound(off) and PACK not in off["source_packs"]
    for feature in FEATURES.values():
        assert {"pack": "climate-environment", "feature": feature, "reason": "not selected"} in off["omissions"]
    for chosen in (["waste-eurostat"], ["waste-eurostat-circular-economy"], ["waste-eea-transfers"], ["waste-oecd"],
                   sorted(FEATURES.values()), ["waste-oecd", "water-usgs"]):
        on = plan_for(chosen)
        assert "environment.waste" in bound(on) and on["source_packs"] == off["source_packs"]
        assert bound(off) <= bound(on)
        assert sorted(on["features"]["climate-environment"]) == sorted(chosen)
    view = CompositionView(plan_for(["waste-eurostat"]), provider_descriptors(), adapt_all().values())
    assert view.tools["noesis-knowledge-engine.waste_indicator_for_place"].provider == "environment.waste"
    assert not list(ROOT.glob("packs/*waste*"))  # no new pack


def test_feature_selection_follows_the_active_composition_and_readiness_reports_degraded_links():
    empty = readiness(duckdb.connect(":memory:"))
    assert empty["features"] == {f: False for f in FEATURES.values()} and empty["stores_ready"] is False
    assert {link["status"] for link in empty["links"].values()} == {"provider_absent"}  # degrades, never fails
    assert {p["live_verification"] for p in empty["providers"].values()} == {"unverified-live"}
    conn, coordinator, bundles, _ = _migrated()
    assert selected_features(conn) == []
    coordinator.select("climate-environment", bundles["climate-environment"]["version"], features=["waste-oecd"])
    assert coordinator.activate("climate-environment-waste-oecd-on")["status"] == "published"
    assert selected_features(conn) == ["waste-oecd"]
    coordinator.select("climate-environment", bundles["climate-environment"]["version"], features=[])
    coordinator.activate("climate-environment-waste-off")
    assert selected_features(conn) == []
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "climate-environment (waste features)" in doc and "`noesis-waste-record-v2`" in doc


def test_taxonomy_classifies_the_provider_and_the_gap_row_is_gone():
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"]["environment.waste"] == {
        "subdomains": ["waste-circular-economy"], "shapes": ["statistical-series", "observations"]}
    program = (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
    assert "| `waste-circular-economy` |" not in program
    assert "WC01 is covered offline" in program and "(WC13)" in program
    covered, gaps = (int(n) for n in re.search(r"(\d+) are covered and (\d+) are gaps", program).groups())
    assert covered + gaps == 89 and covered >= 68
    readme = (ROOT / "README.md").read_text()
    assert f"Today {covered} are covered" in readme and f"The other {gaps} are" in readme
