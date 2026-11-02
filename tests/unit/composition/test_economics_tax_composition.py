"""The Economics bundle's optional per-source tax features and the ``economics.tax`` provider (#2889)."""

from __future__ import annotations

import json
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
from src.ingestion.source_packs import validate_source_pack
from src.kb.tax_records import FEATURES, enabled_providers, feature_enabled, readiness
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
PACK = {"pack_id": "economic-tax-transparency", "version": "1.0.0", "range": "^1.0.0"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def economics_plan(features=None):
    bundles = adapt_all()
    root = {"pack": "economics", "version": bundles["economics"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "economics" in b["consumers"]}


def test_descriptor_declares_capability_read_only_operations_store_probe_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "economics.tax")
    assert validate_provider_descriptor(descriptor) == []
    (capability,) = descriptor["capabilities"]
    assert capability["contract"] == {"name": "noesis-tax-transparency-record", "version": "2.0.0"}
    constraints = capability["semantic_constraints"]
    assert {"series", "editions", "estimates", "status", "identity", "sources", "minimisation",
            "exclusions"} <= set(constraints)
    assert "never presented as observations" in constraints["estimates"]
    assert "not-implemented" in constraints["sources"] and "reference-only" in constraints["sources"]
    assert {o["side_effect"] for o in descriptor["operations"]} == {"read-only"}
    tools = {o["tool"].split(".", 1)[1] for o in descriptor["operations"]}
    assert {"tax_indicator_for_jurisdiction", "tax_series_history", "export_tax_evidence_bundle",
            "tax_company_reports_for_entity"} <= tools
    assert descriptor["readiness_probes"] == [{"id": "vintages", "kind": "table-exists", "target": "tax_vintages"}]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "economics.tax" for s in d["stores"]}
    assert not owned & others
    assert all(t.startswith("tax_") for s in descriptor["stores"] for t in s["tables"])


def test_tax_features_are_optional_per_source_features_of_the_existing_economics_pack():
    composition = json.loads((ROOT / "packs/economics/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert set(FEATURES) == {"tax-oecd-cbcr", "tax-oecd-rates", "tax-eutax-offshore-wealth",
                             "tax-eutax-billionaire-etr", "tax-public-cbcr"}
    for name, source in FEATURES.items():
        feature = features[name]
        assert feature["default"] is False and f"(source {source})" in feature["description"]
        assert "never presented as observations" in feature["description"]
        assert "economics.tax" in {r["capability"] for r in feature["requires"]}
    assert "platform.entity-identity" in {r["capability"] for r in features["tax-public-cbcr"]["requires"]}
    assert "platform.source-acquisition" not in {r["capability"] for r in features["tax-public-cbcr"]["requires"]}
    profile = next(p for p in composition["contributes"]["profiles"] if p["id"] == "economics.tax")
    assert {"model estimate", "edition", "public CbCR reference", "CbCR aggregates"} <= set(profile["vocabulary"])
    assert PACK in composition["contributes"]["source_packs"]
    installed = validate_source_pack(json.loads(
        (ROOT / "config/source_packs/economic-tax-transparency.json").read_text()))
    assert installed["version"] == PACK["version"]
    assert validate_composition_manifest(adapt_all()["economics"]) == []
    pack = json.loads((ROOT / "packs/economics/pack.json").read_text())
    assert {"model estimates presented as observations", "company-level figures from public CbCR reports"} <= set(
        pack["exclusions"])
    assert pack["schema_versions"]["tax-transparency-record"] == "2.0.0"
    assert not [p for p in ROOT.glob("packs/*tax*") if p.is_dir()]  # no new pack


@pytest.mark.parametrize("selection", [[], ["tax-oecd-cbcr"], ["tax-eutax-offshore-wealth"], ["tax-public-cbcr"],
                                       list(FEATURES), ["tax-oecd-rates", "tourism-capacity"]])
def test_each_selection_resolves_independently_and_together(selection):
    plan = economics_plan(selection)
    tax = bool(set(selection) & set(FEATURES))
    assert ("economics.tax" in bound(plan)) == tax
    if selection == ["tax-oecd-cbcr"]:
        assert bound(plan) == {"economics.core", "economics.tax", "platform.source-runtime"}
        assert PACK in plan["source_packs"]
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert ("noesis-knowledge-engine.tax_indicator_for_jurisdiction" in view.tools) == tax


def test_readiness_and_feature_enablement_follow_the_composition_selection():
    report = readiness(duckdb.connect(":memory:"))
    assert report["selected"] is False and report["stores_ready"] is False
    assert {p["live_verification"] for p in report["providers"].values()} == {
        "unverified-live", "not-implemented", "reference-only"}
    assert enabled_providers(duckdb.connect(":memory:")) == set(FEATURES.values())
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("economics", bundles["economics"]["version"], features=["tax-oecd-rates"])
    assert coordinator.activate("economics-tax-on")["status"] == "published"
    assert feature_enabled(conn) is True and feature_enabled(conn, "tax-oecd-cbcr") is False
    assert enabled_providers(conn) == {"oecd-cts-rates"}
    status = readiness(conn)
    assert status["features_selected"] == ["tax-oecd-rates"]
    assert status["providers"]["oecd-cts-rates"]["feature_selected"] is True
    coordinator.select("economics", bundles["economics"]["version"], features=[])
    coordinator.activate("economics-tax-off")
    assert feature_enabled(conn) is False and enabled_providers(conn) == set()
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "`tax-public-cbcr` features" in doc and "`noesis-tax-transparency-record-v2`" in doc


def test_taxonomy_classifies_the_provider_under_public_finance_and_depth_names_its_sources():
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"]["economics.tax"] == {"subdomains": ["public-finance"],
                                                      "shapes": ["statistical-series", "versioned-documents"]}
    depth = json.loads((ROOT / "packs/depth.json").read_text())
    assert depth["providers"]["economics.tax"]["sources"] == ["oecd-cts-cbcr", "oecd-cts-rates",
                                                              "eutax-offshore-wealth", "eutax-billionaire-etr"]
    assert "eu-tax-observatory" in depth["publishers"]
