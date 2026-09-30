"""The new Society bundle, its native manifest and the ``society.income`` provider (#2637, IP11)."""

from __future__ import annotations

import json
from pathlib import Path

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
from src.kb.income_distribution_links import IncomeLinks
from src.kb.income_distribution_monitoring import IncomeMonitor
from src.kb.income_distribution_records import feature_enabled, feature_state
from tests.unit import income_distribution_harness as h
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = json.loads((ROOT / "packs/society/manifest.json").read_text())
COMPOSITION = json.loads((ROOT / "packs/society/composition.json").read_text())
BASE = {"society.income", "economics.core", "geospatial.core", "platform.subscriptions", "platform.source-runtime"}
PACK = {"pack_id": "society-statistics", "version": "1.0.0", "range": "^1.0.0"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def plan(features=None):
    bundles = adapt_all()
    root = {"pack": "society", "version": bundles["society"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(result):
    return {b["provider"] for b in result["bindings"] if "society" in b["consumers"]}


def test_manifest_is_native_valid_and_the_composition_view_matches_it():
    assert validate_composition_manifest(MANIFEST) == []
    adapted = adapt_all()["society"]
    assert "adapter" not in adapted and adapted["content_hash"] == MANIFEST["content_hash"]
    assert COMPOSITION["requires"] == MANIFEST["requires"]
    assert COMPOSITION["optional_features"] == MANIFEST["optional_features"]
    assert COMPOSITION["exclusions"] == MANIFEST["advisory"]["exclusions"]
    assert [(f["id"], f["default"]) for f in MANIFEST["optional_features"]] == [
        ("pip", True), ("eu-silc", True), ("oecd-idd", True), ("demographics-links", False), ("labour-links", False)]
    assert MANIFEST["compatibility_aliases"] == {"society-population": "society"}
    for exclusion in ("nowcasting poverty or inequality", "filling years a source did not publish",
                      "setting or applying poverty lines no source published",
                      "blending PIP, EU-SILC and OECD figures into one series",
                      "re-harmonising welfare concepts, equivalence scales or PPP rounds"):
        assert exclusion in MANIFEST["advisory"]["exclusions"]
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["packs"]["society"] == {"domain": "society-population"}
    assert taxonomy["providers"]["society.income"] == {"subdomains": ["income-poverty-inequality"],
                                                       "shapes": ["statistical-series"]}


def test_descriptor_declares_read_only_operations_its_own_stores_and_the_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "society.income")
    assert validate_provider_descriptor(descriptor) == []
    (capability,) = descriptor["capabilities"]
    assert capability["contract"] == {"name": "noesis-income-distribution-record", "version": "1.0.0"}
    assert {"series", "vintages", "comparability", "minimisation", "exclusions"} <= set(
        capability["semantic_constraints"])
    assert {o["side_effect"] for o in descriptor["operations"]} == {"read-only"}
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "society.income" for s in d["stores"]}
    assert not owned & others
    assert all(t.startswith("income_") for s in descriptor["stores"] for t in s["tables"])
    pack = validate_source_pack(json.loads((ROOT / "config/source_packs/society.json").read_text()))
    assert descriptor["source_packs"] == [PACK] and pack["version"] == PACK["version"]
    # Existing Society-and-population providers keep their bundles and ids.
    ids = {d["id"] for d in provider_descriptors()}
    assert {"economics.demographics", "economics.labour", "science.education-statistics"} <= ids
    assert [p.name for p in (ROOT / "packs/society/providers").iterdir()] == ["society.income.json"]


@pytest.mark.parametrize("features", [None, ["pip"], ["pip", "eu-silc", "oecd-idd", "demographics-links"],
                                      ["eu-silc", "labour-links"]])
def test_each_selection_resolves_and_link_features_bind_only_their_provider(features):
    result = plan(features)
    providers = bound(result)
    assert BASE <= providers
    assert ("economics.demographics" in providers) == bool(features and "demographics-links" in features)
    assert ("economics.labour" in providers) == bool(features and "labour-links" in features)
    assert PACK in result["source_packs"]
    view = CompositionView(result, provider_descriptors(), adapt_all().values())
    assert "noesis-knowledge-engine.income_indicator_for_place" in view.tools


def test_features_follow_the_composition_selection_and_links_degrade_gracefully():
    conn, coordinator, bundles, _ = _migrated(h.connection())
    assert feature_state(conn, "pip") == "selected" and not feature_enabled(conn, "labour-links")
    h.load_all(conn)
    links = IncomeLinks(conn)
    assert links.link_labour(h.NS, principal_id="svc", scopes=h.SCOPES)["status"] == "feature_not_selected"
    assert links.link_demographics(h.NS, principal_id="svc", scopes=h.SCOPES)["status"] == "feature_not_selected"
    coordinator.select("society", bundles["society"]["version"], features=["eu-silc", "labour-links"])
    assert coordinator.activate("society-links-on")["status"] == "published"
    assert feature_enabled(conn, "labour-links") and feature_state(conn, "pip") == "not_selected"
    labour = links.link_labour(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert labour["status"] == "evaluated" and labour["unresolved"]  # labour store absent: provider_absent
    refused = IncomeMonitor(conn).refresh(h.NS, h.source("pip"), principal_id="op", scopes=h.SCOPES,
                                          transport=lambda **_: (_ for _ in ()).throw(AssertionError("no request")))
    assert refused["status"] == "feature_not_selected" and refused["releases"] == []
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "`society.income`" in doc and "`noesis-income-distribution-record-v1`" in doc
