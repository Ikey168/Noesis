"""The Science bundle's optional ``education-statistics`` feature and the ``science.education-statistics`` provider
(#2438)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import validate_composition_manifest, validate_provider_descriptor
from src.composition.readiness import CompositionView
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb.education_statistics import feature_enabled, readiness
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
PACK = {"pack_id": "primary-scientific-evidence", "version": "1.2.0", "range": "^1.2.0"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def science_plan(features=None, descriptors=None):
    bundles = adapt_all()
    root = {"pack": "science", "version": bundles["science"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()),
                     descriptors if descriptors is not None else provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "science" in b["consumers"]}


def test_descriptor_declares_read_only_lookups_stores_probe_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "science.education-statistics")
    assert validate_provider_descriptor(descriptor) == []
    raw = json.loads((ROOT / "packs/science/providers/science.education-statistics.json").read_text())
    assert validate_provider_descriptor(raw) == []
    (capability,) = descriptor["capabilities"]
    assert capability["contract"] == {"name": "noesis-education-statistic-record", "version": "1.0.0"}
    assert "rankings" in capability["semantic_constraints"]["exclusions"]
    assert {o["side_effect"] for o in descriptor["operations"]} == {"read-only"}
    assert {"institution-as-of", "country-as-of"} <= {o["id"] for o in descriptor["operations"]}
    assert all("knowledge:education:read" in o["required_scopes"] for o in descriptor["operations"])
    assert descriptor["readiness_probes"] == [{"id": "vintages", "kind": "table-exists", "target": "edu_vintages"}]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != descriptor["id"] for s in d["stores"]}
    assert not owned & others
    assert all(t.startswith("edu_") for s in descriptor["stores"] for t in s["tables"])


def test_the_feature_is_optional_off_by_default_and_binds_sdmx_handling_and_identity():
    composition = json.loads((ROOT / "packs/science/composition.json").read_text())
    feature = next(f for f in composition["optional_features"] if f["id"] == "education-statistics")
    assert feature["default"] is False
    assert {r["capability"] for r in feature["requires"]} == {
        "science.education-statistics", "economics.knowledge", "platform.entity-identity",
        "platform.subscriptions", "platform.source-acquisition"}
    assert validate_composition_manifest(adapt_all()["science"]) == []
    pack = json.loads((ROOT / "packs/science/pack.json").read_text())
    assert "university rankings, league tables, quality or composite scores" in pack["exclusions"]
    manifest = json.loads((ROOT / "config/source_packs/scientific.json").read_text())
    assert {s["source_id"] for s in manifest["sources"] if s.get("connector") == "education-statistics"} == {
        "ipeds-institution-statistics", "eter-institution-statistics", "unesco-uis-education-indicators",
        "oecd-eag-education-indicators", "eurostat-rd-statistics"}


@pytest.mark.parametrize("selection", [[], ["education-statistics"], ["cultural-collections", "education-statistics"]])
def test_each_selection_resolves_and_off_keeps_todays_bindings(selection):
    plan = science_plan(selection)
    assert sorted(plan["features"]["science"]) == sorted(selection)
    on = "education-statistics" in selection
    assert ("science.education-statistics" in bound(plan)) == on
    if on:
        assert {"economics.core", "platform.subscriptions", "platform.source-runtime"} <= bound(plan)
        assert PACK in plan["source_packs"]
    else:
        assert bound(plan) == bound(science_plan([]))
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.institution_statistics_as_of" in view.tools) == on


def test_readiness_and_enablement_follow_the_active_selection():
    report = readiness(duckdb.connect(":memory:"))
    assert report["selected"] is False and report["stores_ready"] is False
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live"}
    assert not (ROOT / "packs/education").exists()
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("science", bundles["science"]["version"], features=["education-statistics"])
    assert coordinator.activate("science-education-on")["status"] == "published"
    assert feature_enabled(conn) is True
    coordinator.select("science", bundles["science"]["version"], features=[])
    coordinator.activate("science-education-off")
    assert feature_enabled(conn) is False
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "optional `education-statistics` feature" in doc and "`noesis-education-statistic-record-v1`" in doc
