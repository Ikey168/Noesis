"""The Legal bundle's optional ``courts`` and ``justice-statistics`` features and their providers (#2427)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import validate_composition_manifest, validate_provider_descriptor
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb.courts_justice import feature_enabled
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


def legal_plan(features=None, descriptors=None):
    bundles = adapt_all()
    root = {"pack": "legal", "version": bundles["legal"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()),
                     descriptors if descriptors is not None else provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "legal" in b["consumers"]}


def test_descriptors_declare_capabilities_constraints_stores_and_the_source_pack():
    descriptors = {d["id"]: d for d in provider_descriptors()}
    for provider, contract, probe in (("legal.courts", "noesis-court-docket", "legal_docket_revisions"),
                                      ("legal.justice-statistics", "noesis-justice-statistic", "justice_vintages")):
        descriptor = descriptors[provider]
        assert validate_provider_descriptor(descriptor) == []
        assert descriptor["capabilities"][0]["contract"] == {"name": contract, "version": "1.0.0"}
        assert descriptor["readiness_probes"][0]["target"] == probe
        assert descriptor["source_packs"] == [{"pack_id": "legal-research", "version": "1.4.0", "range": "^1.4.0"}]
        text = json.dumps(descriptor["capabilities"][0]["semantic_constraints"])
        assert "exclusions" in descriptor["capabilities"][0]["semantic_constraints"]
        assert ("minimisation" in text) and ("rank" in text or "profile" in text)
    constraints = descriptors["legal.courts"]["capabilities"][0]["semantic_constraints"]
    assert {"minimisation", "outcomes", "links"} <= set(constraints)
    assert "comparability" in descriptors["legal.justice-statistics"]["capabilities"][0]["semantic_constraints"]


def test_the_bundle_resolves_with_both_features_off_by_default():
    composition = json.loads((ROOT / "packs/legal/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert set(features) == {"sanctions", "federal-statutes", "courts", "justice-statistics",
                             "enforcement-sec", "enforcement-fca", "enforcement-epa", "enforcement-edpb"}  # #2651
    assert features["courts"]["default"] is False and features["justice-statistics"]["default"] is False
    assert validate_composition_manifest(adapt_all()["legal"]) == []
    plan = legal_plan()
    assert plan["features"]["legal"] == [] and bound(plan) == {"legal.core"}
    omitted = {o["feature"] for o in plan["omissions"] if o["pack"] == "legal"}
    assert {"courts", "justice-statistics"} <= omitted
    pack = json.loads((ROOT / "packs/legal/pack.json").read_text())
    assert {"court-dockets-and-opinions", "justice-statistics-as-of"} <= set(pack["capabilities"])
    assert "neighbourhood safety ratings" in pack["exclusions"]
    assert not list(ROOT.glob("packs/*court*")) and not list(ROOT.glob("packs/*justice*"))


def test_selecting_the_features_binds_their_providers_and_consumed_ones():
    courts = legal_plan(["courts"])
    assert courts["features"]["legal"] == ["courts"]
    assert {"legal.core", "legal.courts", "ownership.core", "platform.subscriptions"} <= bound(courts)
    assert "legal.justice-statistics" not in bound(courts)
    stats = legal_plan(["justice-statistics"])
    assert {"legal.justice-statistics", "geospatial.core"} <= bound(stats)
    assert {"pack_id": "legal-research", "version": "1.5.0", "range": "^1.1.0"} in stats["source_packs"]
    both = legal_plan(["courts", "justice-statistics", "sanctions", "federal-statutes"])
    assert sorted(both["features"]["legal"]) == ["courts", "federal-statutes", "justice-statistics", "sanctions"]


def test_a_missing_consumed_provider_is_a_visible_omission():
    descriptors = [d for d in provider_descriptors() if d["id"] != "geospatial.core"]
    plan = legal_plan(["justice-statistics"], descriptors)
    assert plan["features"]["legal"] == []
    omission = next(o for o in plan["omissions"] if o["feature"] == "justice-statistics")
    assert "missing_contract" in omission["reason"]


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn, "courts") is False
    coordinator.select("legal", bundles["legal"]["version"], features=["courts"])
    assert coordinator.activate("legal-courts-on")["status"] == "published"
    assert feature_enabled(conn, "courts") is True and feature_enabled(conn, "justice-statistics") is False
