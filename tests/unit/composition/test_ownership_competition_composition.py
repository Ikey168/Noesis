"""The Corporate Ownership bundle's optional ``competition`` feature and its provider (#2361)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import validate_composition_manifest, validate_provider_descriptor
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb.competition import feature_enabled
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


def plan(features=None, descriptors=None):
    bundles = adapt_all()
    root = {"pack": "corporate-ownership", "version": bundles["corporate-ownership"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()),
                     descriptors if descriptors is not None else provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(result):
    return {b["provider"] for b in result["bindings"] if "corporate-ownership" in b["consumers"]}


def test_descriptor_declares_constraints_stores_and_the_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "ownership.competition")
    assert validate_provider_descriptor(descriptor) == []
    constraints = descriptor["capabilities"][0]["semantic_constraints"]
    assert {"identity", "citations", "authorities", "exclusions"} <= set(constraints)
    assert "outcome" in constraints["exclusions"] and "side by side" in constraints["authorities"]
    assert descriptor["source_packs"] == [{"pack_id": "corporate-ownership", "version": "1.1.0", "range": "^1.1.0"}]
    tools = {op["tool"].split(".", 1)[1] for op in descriptor["operations"]}
    assert {"lookup_competition_cases", "competition_case_history", "state_aid_awards_for_beneficiary"} <= tools


def test_the_feature_is_off_by_default_and_selecting_it_binds_its_provider():
    manifest = json.loads((ROOT / "packs/corporate-ownership/manifest.json").read_text())
    feature = next(f for f in manifest["optional_features"] if f["id"] == "competition")
    assert feature["default"] is False
    assert {r["capability"] for r in feature["requires"]} >= {"ownership.competition", "ownership.identity",
                                                               "ownership.graph", "platform.subscriptions"}
    assert not any(r["capability"].startswith("legal.") for r in feature["requires"])  # no bundle cycle
    assert validate_composition_manifest(adapt_all()["corporate-ownership"]) == []
    default = plan()
    assert "ownership.competition" not in bound(default)
    assert {"pack": "corporate-ownership", "feature": "competition", "reason": "not selected"} in default["omissions"]
    selected = plan(["competition"])
    assert selected["features"]["corporate-ownership"] == ["competition"]
    assert {"ownership.competition", "ownership.core", "platform.subscriptions"} <= bound(selected)
    assert not list(ROOT.glob("packs/*competition*"))  # no new pack


def test_a_missing_provider_is_a_visible_omission():
    descriptors = [d for d in provider_descriptors() if d["id"] != "platform.subscriptions"]
    result = plan(["competition"], descriptors)
    assert result["features"]["corporate-ownership"] == []
    omission = next(o for o in result["omissions"] if o["feature"] == "competition")
    assert "missing_contract" in omission["reason"]


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("corporate-ownership", bundles["corporate-ownership"]["version"], features=["competition"])
    assert coordinator.activate("competition-on")["status"] == "published"
    assert feature_enabled(conn) is True
