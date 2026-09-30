"""The Funding & Grants bundle's optional ``development-finance`` feature and its provider (#2037)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb
import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import (
    satisfies,
    seal_manifest,
    validate_composition_manifest,
    validate_provider_descriptor,
)
from src.composition.readiness import CompositionView
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb import funding_bundle
from src.kb.development_finance import NEVER_SENTENCE, feature_enabled, readiness
from tests.unit.composition.test_migration import _migrated
from tools.knowledge_engine_mcp.development_finance import (
    DEVELOPMENT_FINANCE_SCOPES,
    DEVELOPMENT_FINANCE_TOOLS,
    DEVELOPMENT_FINANCE_WRITES,
)

ROOT = Path(__file__).resolve().parents[3]
PACK = {
    "pack_id": "economic-statistics-and-filings",
    "version": "1.4.0",
    "range": "^1.4.0",
}
CAPABILITIES = {
    "funding.development-finance-activities",
    "funding.development-finance-identity",
    "funding.development-finance-monitoring",
}
SHARED = {
    "economics.knowledge",
    "geospatial.place-resolution",
    "ownership.identity",
    "platform.entity-identity",
    "platform.source-acquisition",
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


def plan_for(features=None, bundles=None, descriptors=None, extra_roots=()):
    bundles = bundles or adapt_all()
    roots = [
        {"pack": "funding-grants", "version": bundles["funding-grants"]["version"]}
    ]
    if features is not None:
        roots[0]["features"] = features
    for name in extra_roots:
        roots.append({"pack": name, "version": bundles[name]["version"]})
    result = resolve(
        roots,
        list(bundles.values()),
        descriptors if descriptors is not None else provider_descriptors(),
    )
    assert result.ok, result.failure
    return result.plan


def without_feature(bundles):
    """Today's Funding & Grants manifest: the same bundle without the development-finance contributions."""
    manifest = copy.deepcopy(bundles["funding-grants"])
    body = {
        k: v
        for k, v in manifest.items()
        if k not in {"content_hash", "optional_features"}
    }
    contributes = body["contributes"]
    contributes["capabilities"] = [
        c for c in contributes["capabilities"] if c["id"] not in CAPABILITIES
    ]
    contributes["providers"] = [
        p for p in contributes["providers"] if p["id"] != "funding.development-finance"
    ]
    contributes["profiles"] = [
        p for p in contributes["profiles"] if p["id"] != "funding.development-finance"
    ]
    return {**bundles, "funding-grants": seal_manifest(body)}


def test_descriptor_declares_capabilities_operations_scopes_stores_probes_and_source_pack():
    descriptor = next(
        d for d in provider_descriptors() if d["id"] == "funding.development-finance"
    )
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["implementation"] == {
        "name": "src.kb.development_finance",
        "version": "1.0.0",
        "server": "noesis-knowledge-engine",
    }
    assert {c["id"] for c in descriptor["capabilities"]} == CAPABILITIES
    tools = {o["tool"].split(".", 1)[1]: o for o in descriptor["operations"]}
    assert set(tools) == DEVELOPMENT_FINANCE_TOOLS
    for name, operation in tools.items():
        assert operation["required_scopes"] == DEVELOPMENT_FINANCE_SCOPES[name], name
        expected = (
            "read-only"
            if name not in DEVELOPMENT_FINANCE_WRITES
            else ("acquisition" if name.startswith("acquire_") else "local-mutation")
        )
        assert operation["side_effect"] == expected, name
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {
        s["record_type"]
        for d in provider_descriptors()
        if d["id"] != "funding.development-finance"
        for s in d["stores"]
    }
    assert not owned & others
    assert all(
        t.startswith("devfin_") for s in descriptor["stores"] for t in s["tables"]
    )
    assert descriptor["source_packs"] == [PACK]
    manifest = json.loads((ROOT / "config/source_packs/economic.json").read_text())
    # 1.5.0 adds the Economics trade sources (#2210) and 1.6.0 the labour sources (#2219); the CRS source and the
    # ^1.4.0 pin are unchanged.
    assert manifest["version"] == "1.7.0"  # 1.7.0 adds the extractives sources (#2653)


def test_the_feature_and_profile_are_declared_off_by_default():
    manifest = json.loads((ROOT / "packs/funding-grants/manifest.json").read_text())
    assert validate_composition_manifest(manifest) == []
    (feature,) = manifest["optional_features"]
    assert feature["id"] == "development-finance" and feature["default"] is False
    assert {r["capability"] for r in feature["requires"]} == CAPABILITIES | SHARED
    profile = next(
        p
        for p in manifest["contributes"]["profiles"]
        if p["id"] == "funding.development-finance"
    )
    assert {"publisher", "activity", "revision", "coverage", "vintage"} <= set(
        profile["vocabulary"]
    )
    assert profile["workflow_defaults"]["never"] == NEVER_SENTENCE
    # The bundle's own requirements are unchanged by the feature.
    assert not {r["capability"] for r in manifest["requires"]} & (CAPABILITIES | SHARED)


def test_with_the_feature_off_the_bindings_equal_todays():
    bundles = adapt_all()
    today = plan_for(
        bundles=without_feature(bundles),
        descriptors=[
            d
            for d in provider_descriptors()
            if d["id"] != "funding.development-finance"
        ],
    )
    off = plan_for(bundles=bundles)
    assert json.dumps(off["bindings"], sort_keys=True) == json.dumps(
        today["bindings"], sort_keys=True
    )
    assert off["source_packs"] == today["source_packs"]
    assert "funding.development-finance" not in {b["provider"] for b in off["bindings"]}


def test_with_the_feature_on_the_provider_binds_with_one_authority_per_store():
    plan = plan_for(["development-finance"])
    bound = {
        b["provider"] for b in plan["bindings"] if "funding-grants" in b["consumers"]
    }
    assert {
        "funding.development-finance",
        "funding.core",
        "economics.core",
        "geospatial.core",
        "ownership.core",
        "platform.entity-identity",
        "platform.source-runtime",
    } <= bound
    assert {
        b["capability"]
        for b in plan["bindings"]
        if b["provider"] == "funding.development-finance"
    } == CAPABILITIES
    installed = json.loads((ROOT / "config/source_packs/economic.json").read_text())[
        "version"
    ]
    pins = [
        ref
        for ref in plan["source_packs"]
        if ref["pack_id"] == "economic-statistics-and-filings"
    ]
    assert pins and all(satisfies(installed, pin["range"]) for pin in pins)
    stores = {}
    for descriptor in provider_descriptors():
        for store in descriptor["stores"]:
            stores.setdefault(store["record_type"], set()).add(descriptor["id"])
    assert all(len(owners) == 1 for owners in stores.values())
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert {f"noesis-knowledge-engine.{t}" for t in DEVELOPMENT_FINANCE_TOOLS} <= set(
        view.tools
    )
    off = CompositionView(plan_for([]), provider_descriptors(), adapt_all().values())
    assert not {
        f"noesis-knowledge-engine.{t}" for t in DEVELOPMENT_FINANCE_TOOLS
    } & set(off.tools)


def test_procurement_still_reuses_funding_core_unchanged():
    bundles = adapt_all()
    roots = [{"pack": "procurement", "version": bundles["procurement"]["version"]}]
    result = resolve(roots, list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    funding = {
        b["capability"]
        for b in result.plan["bindings"]
        if b["provider"] == "funding.core"
    }
    assert {
        "funding.profiles",
        "funding.eligibility",
        "funding.shortlists",
        "funding.workspaces",
    } <= funding
    assert "funding.development-finance" not in {
        b["provider"] for b in result.plan["bindings"]
    }


def test_enabling_is_a_selection_change_and_disabling_funding_keeps_shared_providers():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select(
        "funding-grants",
        bundles["funding-grants"]["version"],
        features=["development-finance"],
    )
    receipt = coordinator.activate("funding-development-finance-on")
    assert receipt["status"] == "published" and feature_enabled(conn) is True
    plan = coordinator.active()["plan"]
    assert "funding.development-finance" in {b["provider"] for b in plan["bindings"]}
    status = funding_bundle.readiness(
        conn, "funding", scopes={"operator", "knowledge:funding:read"}
    )
    providers = {o["provider"] for o in status["composition"]["operations"]}
    assert "funding.development-finance" in providers
    result = funding_bundle.set_enabled(
        conn, "funding", False, principal_id="operator", scopes={"operator"}
    )
    assert result["enabled"] is False and feature_enabled(conn) is False
    assert {"funding.development-finance", "geospatial.core", "ownership.core"} <= {
        d["id"] for d in coordinator.installed("provider")
    }


def test_readiness_reports_the_stores_and_each_decision_without_a_new_pack_or_flag():
    report = readiness(duckdb.connect(":memory:"))
    assert report["selected"] is False and report["stores_ready"] is False
    assert set(report["stores"]) >= {"devfin_activity_revisions", "devfin_crs_vintages"}
    decisions = {p: r["access_decision"] for p, r in report["providers"].items()}
    assert (
        decisions["transparenzportal-bund"]
        == decisions["eu-aid-explorer"]
        == "not-implemented"
    )
    assert not list(ROOT.glob("packs/*development*")) and not list(
        ROOT.glob("config/source_packs/*development*")
    )
    assert not [
        t
        for t in DEVELOPMENT_FINANCE_TOOLS
        if t.startswith("set_") and t.endswith("_enabled")
    ]
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert (
        "optional `development-finance` feature" in doc
        and "`noesis-development-finance-record-v1`" in doc
    )
