"""The Political bundle's optional campaign-finance features and the ``political.campaign-finance`` provider (#2523)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import validate_composition_manifest, validate_provider_descriptor
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb.campaign_finance_records import feature_enabled
from tests.unit.composition.test_migration import _migrated
from tests.unit.composition.test_political_lobbying_composition import bound, political_plan
from tools.knowledge_engine_mcp.campaign_finance import CAMPAIGN_FINANCE_SCOPES

ROOT = Path(__file__).resolve().parents[3]
FEATURE_PROVIDERS = {
    "political.core",
    "political.campaign-finance",
    "ownership.core",
    "platform.entity-identity",
    "platform.subscriptions",
    "platform.source-runtime",
}
SOURCE_PACK = {"pack_id": "official-political-records", "version": "1.4.0", "range": "^1.4.0"}


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def descriptor():
    return next(d for d in provider_descriptors() if d["id"] == "political.campaign-finance")


def test_descriptor_declares_operations_scopes_stores_exclusions_and_the_source_pack():
    found = descriptor()
    assert validate_provider_descriptor(found) == []
    capability = found["capabilities"][0]
    assert capability["contract"] == {"name": "noesis-campaign-finance-record", "version": "1.0.0"}
    constraints = capability["semantic_constraints"]
    assert "no influence scoring" in constraints["exclusions"] and "CF01" in constraints["minimisation"]
    tools = {op["tool"].rsplit(".", 1)[1]: op for op in found["operations"]}
    assert {"campaign_finance_totals_as_of", "campaign_finance_affiliate_donations",
            "campaign_finance_contest_filings", "review_campaign_finance_identity_match",
            "create_campaign_finance_monitor", "export_campaign_finance_evidence_bundle"} <= set(tools)
    for name, op in tools.items():
        assert op["required_scopes"] == CAMPAIGN_FINANCE_SCOPES[name]
    assert found["source_packs"] == [SOURCE_PACK]
    owned = {s["record_type"] for s in found["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "political.campaign-finance"
              for s in d["stores"]}
    assert not owned & others  # no second entity, subscription or ownership store
    # existing Political providers are unchanged
    for provider in ("political.core", "political.lobbying", "political.elections", "political.legislation"):
        assert json.loads((ROOT / f"packs/political/providers/{provider}.json").read_text())["id"] == provider


def test_us_and_uk_are_separate_optional_features_off_by_default():
    composition = json.loads((ROOT / "packs/political/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    for feature in ("campaign-finance-us", "campaign-finance-uk"):
        assert features[feature]["default"] is False
        required = {r["capability"] for r in features[feature]["requires"]}
        assert "political.campaign-finance" in required
        # elections, lobbying and ownership links degrade when absent, so the features never require them
        assert not required & {"political.lobbying", "political.election-records", "ownership.graph"}
    assert validate_composition_manifest(adapt_all()["political"]) == []
    assert "political.campaign-finance" not in bound(political_plan())


@pytest.mark.parametrize("selection", [["campaign-finance-us"], ["campaign-finance-uk"],
                                       ["campaign-finance-us", "campaign-finance-uk"]])
def test_selecting_a_feature_binds_the_provider_and_consumed_ones(selection):
    plan = political_plan(selection)
    assert sorted(plan["features"]["political"]) == sorted(selection)
    assert bound(plan) == FEATURE_PROVIDERS
    assert SOURCE_PACK in plan["source_packs"]


def test_features_coexist_with_lobbying_elections_and_legislation():
    bundles = adapt_all()
    for selection in (["lobbying", "campaign-finance-us"], ["elections", "campaign-finance-uk"],
                      ["legislation-us", "campaign-finance-us"]):
        plan = political_plan(selection, bundles=bundles)
        assert "political.campaign-finance" in bound(plan)
        assert ("political.lobbying" in bound(plan)) == ("lobbying" in selection)


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert not feature_enabled(conn, "campaign-finance-us") and not feature_enabled(conn, "campaign-finance-uk")
    coordinator.select("political", bundles["political"]["version"], features=["campaign-finance-uk"])
    assert coordinator.activate("political-campaign-finance-uk-on")["status"] == "published"
    assert feature_enabled(conn, "campaign-finance-uk") and not feature_enabled(conn, "campaign-finance-us")
    pack = json.loads((ROOT / "packs/political/pack.json").read_text())
    assert {"influence scoring of donors or recipients", "dark-money or undisclosed-funding inference",
            "profiling of individual donors beyond the published minimised record"} <= set(pack["exclusions"])
    assert not list(ROOT.glob("packs/*campaign*"))  # no new pack
