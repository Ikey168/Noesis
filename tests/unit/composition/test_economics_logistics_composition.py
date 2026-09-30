"""The Economics bundle's optional ``logistics`` feature and the ``economics.logistics`` provider (#2549)."""

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
from src.kb.logistics_records import feature_enabled, readiness
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
PACK = {"pack_id": "economic-shipping-and-logistics", "version": "1.0.0", "range": "^1.0.0"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def economics_plan(features=None, descriptors=None):
    bundles = adapt_all()
    root = {"pack": "economics", "version": bundles["economics"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), descriptors if descriptors is not None else provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "economics" in b["consumers"]}


def test_descriptor_declares_read_only_lookup_series_and_as_of_operations_with_probes_and_its_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "economics.logistics")
    assert validate_provider_descriptor(descriptor) == []
    (capability,) = descriptor["capabilities"]
    assert capability["contract"] == {"name": "noesis-logistics-record", "version": "1.0.0"}
    assert "forecasting" in capability["semantic_constraints"]["exclusions"]
    assert {o["side_effect"] for o in descriptor["operations"]} == {"read-only"}
    assert {"port-lookup", "series-values", "port-as-of", "country-as-of", "route-as-of"} <= {
        o["id"] for o in descriptor["operations"]}
    assert all("knowledge:logistics:read" in o["required_scopes"] for o in descriptor["operations"])
    assert descriptor["readiness_probes"] == [{"id": "vintages", "kind": "table-exists",
                                               "target": "logistics_vintages"}]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "economics.logistics"
              for s in d["stores"]}
    assert not owned & others
    assert all(t.startswith("logistics_") for s in descriptor["stores"] for t in s["tables"])


def test_the_feature_is_optional_off_by_default_and_binds_geospatial_feature_query():
    composition = json.loads((ROOT / "packs/economics/composition.json").read_text())
    feature = next(f for f in composition["optional_features"] if f["id"] == "logistics")
    assert feature["default"] is False
    required = {r["capability"] for r in feature["requires"]}
    assert {"economics.logistics", "geospatial.feature-query", "platform.subscriptions"} <= required
    assert PACK in composition["contributes"]["source_packs"]
    # The Economics bundle's pin on economic-statistics-and-filings is unchanged by this feature.
    assert [p["pack_id"] for p in composition["contributes"]["source_packs"]].count(
        "economic-statistics-and-filings") == 1
    assert validate_composition_manifest(adapt_all()["economics"]) == []
    pack = json.loads((ROOT / "packs/economics/pack.json").read_text())
    assert "freight-rate forecasts" in pack["exclusions"]


@pytest.mark.parametrize("selection", [[], ["logistics"], ["logistics", "trade-comext"]])
def test_selection_binds_the_provider_only_when_selected(selection):
    plan = economics_plan(selection)
    assert ("economics.logistics" in bound(plan)) == ("logistics" in selection)
    if selection == ["logistics"]:
        assert "geospatial.core" in bound(plan)
        assert PACK in plan["source_packs"]
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.query_port_logistics" in view.tools) == ("logistics" in selection)


def test_a_missing_geospatial_provider_is_a_visible_omission():
    descriptors = [d for d in provider_descriptors() if d["id"] != "geospatial.core"]
    plan = economics_plan(["logistics"], descriptors)
    assert "economics.logistics" not in bound(plan)
    omission = next(o for o in plan["omissions"] if o["feature"] == "logistics")
    assert omission["capability"].startswith("geospatial.")


def test_readiness_and_feature_enablement_follow_the_active_selection():
    report = readiness(duckdb.connect(":memory:"))
    assert report["selected"] is False and report["stores_ready"] is False
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live"}
    assert any(i["decision"] == "excluded" for i in report["freight_indices"])
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("economics", bundles["economics"]["version"], features=["logistics"])
    assert coordinator.activate("economics-logistics-on")["status"] == "published"
    assert feature_enabled(conn) is True
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "optional `logistics` feature" in doc and "`noesis-logistics-record-v1`" in doc
