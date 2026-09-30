"""The Economics bundle's optional ``public-finance`` feature and the ``economics.public-finance`` provider (#1996)."""

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
from src.kb.public_finance import feature_enabled
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
FEATURE_PROVIDERS = {
    "economics.core",
    "economics.public-finance",
    "legal.core",
    "political.core",
    "procurement.core",
    "funding.core",
    "ownership.core",
    "platform.entity-identity",
    "platform.subscriptions",
    "platform.source-runtime",
}
# 1.3.0 adds the demographics feature's sources (#1914); the public-finance sources are unchanged.
PACK = {
    "pack_id": "economic-statistics-and-filings",
    "version": "1.3.0",
    "range": "^1.3.0",
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


def economics_plan(features=None, descriptors=None, bundles=None):
    bundles = bundles or adapt_all()
    root = {"pack": "economics", "version": bundles["economics"]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve(
        [root],
        list(bundles.values()),
        descriptors if descriptors is not None else provider_descriptors(),
    )
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "economics" in b["consumers"]}


def with_demographics(bundles):
    """The shipped optional ``demographics`` feature (#2007); the bundle is used as composed."""
    assert any(
        f["id"] == "demographics" for f in bundles["economics"]["optional_features"]
    )
    return bundles


def test_descriptor_declares_the_capability_read_only_operations_stores_probe_and_source_pack():
    descriptor = next(
        d for d in provider_descriptors() if d["id"] == "economics.public-finance"
    )
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["implementation"] == {
        "name": "src.kb.public_finance",
        "version": "1.0.0",
        "server": "noesis-knowledge-engine",
    }
    (capability,) = descriptor["capabilities"]
    assert capability["id"] == "economics.public-finance"
    assert capability["contract"] == {
        "name": "noesis-public-finance-record",
        "version": "1.0.0",
    }
    assert {o["side_effect"] for o in descriptor["operations"]} == {"read-only"}
    tools = {o["tool"].rsplit(".", 1)[1] for o in descriptor["operations"]}
    assert {
        "compare_budget_line",
        "inspect_budget_line",
        "budget_line_dossier",
        "list_beneficiary_payments",
    } <= tools
    assert all(
        s["revision_addressable"] and s["namespace_scoped"]
        for s in descriptor["stores"]
    )
    assert all(
        t.startswith("public_finance_")
        for s in descriptor["stores"]
        for t in s["tables"]
    )
    assert descriptor["readiness_probes"] == [
        {"id": "figures", "kind": "table-exists", "target": "public_finance_figures"}
    ]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {
        s["record_type"]
        for d in provider_descriptors()
        if d["id"] != "economics.public-finance"
        for s in d["stores"]
    }
    assert (
        not owned & others
    )  # no second scheduler, permission ledger, project, entity or spatial store


def test_the_bundle_validates_and_behaves_unchanged_with_the_feature_off_by_default():
    composition = json.loads((ROOT / "packs/economics/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    # public-finance, the demographics feature (#1914) and the trade features (#2210) are optional and off by default.
    # The labour-statistics feature (#2219) is optional and off by default too.
    assert set(features) == {"public-finance", "demographics", "trade-comtrade", "trade-comext", "labour-statistics"}
    assert features["demographics"]["default"] is False
    feature = features["public-finance"]
    assert feature["default"] is False
    assert {r["capability"] for r in feature["requires"]} >= {
        "economics.public-finance",
        "economics.knowledge",
        "legal.works",
        "political.knowledge",
        "procurement.award-history",
        "funding.opportunities",
        "platform.entity-identity",
        "platform.subscriptions",
        "platform.source-acquisition",
    }
    assert validate_composition_manifest(adapt_all()["economics"]) == []
    plan = economics_plan()
    assert plan["features"]["economics"] == [] and bound(plan) == {"economics.core"}
    assert {
        "pack": "economics",
        "feature": "public-finance",
        "reason": "not selected",
    } in plan["omissions"]
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert "noesis-knowledge-engine.compare_budget_line" not in view.tools
    assert "noesis-kb.kb_economic" in view.tools


def test_enabling_it_binds_the_legal_political_procurement_funding_identity_and_runtime_providers():
    plan = economics_plan(["public-finance"])
    assert (
        plan["features"]["economics"] == ["public-finance"]
        and bound(plan) == FEATURE_PROVIDERS
    )
    # The bundle now pins 1.6.0 (the labour sources, #2219); this provider still declares ^1.3.0, which it meets.
    assert {**PACK, "version": "1.6.0", "range": "^1.6.0"} in plan["source_packs"]
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    tool = view.tools["noesis-knowledge-engine.compare_budget_line"]
    assert tool.pack == "economics" and tool.provider == "economics.public-finance"
    assert [p["target"] for p in tool.probes] == ["public_finance_figures"]
    profiles = {p["id"]: p for p in adapt_all()["economics"]["contributes"]["profiles"]}
    defaults = profiles["economics.budget-review"]["workflow_defaults"]
    assert defaults["district_collection"] == "alkis_bezirke:bezirksgrenzen"
    assert "cited reconciliation method" in defaults["differences"]


def test_the_feature_coexists_with_the_planned_demographics_feature():
    bundles = with_demographics(adapt_all())
    assert validate_composition_manifest(bundles["economics"]) == []
    for selection in (
        [],
        ["public-finance"],
        ["demographics"],
        ["demographics", "public-finance"],
    ):
        plan = economics_plan(selection, bundles=bundles)
        assert sorted(plan["features"]["economics"]) == sorted(selection)
        assert ("economics.public-finance" in bound(plan)) == (
            "public-finance" in selection
        )


def test_a_missing_consumed_provider_is_a_visible_omission():
    descriptors = [d for d in provider_descriptors() if d["id"] != "procurement.core"]
    plan = economics_plan(["public-finance"], descriptors)
    assert plan["features"][
        "economics"
    ] == [] and "economics.public-finance" not in bound(plan)
    omission = next(o for o in plan["omissions"] if o["feature"] == "public-finance")
    assert (
        omission["capability"].startswith("procurement.")
        and "missing_contract" in omission["reason"]
    )


def test_no_new_pack_source_pack_manifest_or_enablement_flag():
    pack = json.loads((ROOT / "packs/economics/pack.json").read_text())
    assert {
        "public-finance-figure-vintages",
        "public-finance-basis-aware-comparisons",
    } <= set(pack["capabilities"])
    assert {"budget_line", "beneficiary_payment", "audit_finding"} <= set(
        pack["ontology_extensions"]["object_types"]
    )
    assert {"fiscal forecasts", "waste or fraud determinations"} <= set(
        pack["exclusions"]
    )
    assert not list(ROOT.glob("packs/*public*finance*")) and not list(
        ROOT.glob("config/source_packs/*finance*")
    )
    from tools.knowledge_engine_mcp import public_finance

    assert not [
        t
        for t in public_finance.PUBLIC_FINANCE_TOOLS
        if t.startswith("set_") and t.endswith("_enabled")
    ]
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert (
        "optional `public-finance` feature" in doc
        and "`noesis-public-finance-record-v1`" in doc
    )


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select(
        "economics", bundles["economics"]["version"], features=["public-finance"]
    )
    assert coordinator.activate("economics-public-finance-on")["status"] == "published"
    assert feature_enabled(conn) is True
    coordinator.select("economics", bundles["economics"]["version"], features=[])
    coordinator.activate("economics-public-finance-off")
    assert feature_enabled(conn) is False
