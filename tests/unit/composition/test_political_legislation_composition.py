"""The Political bundle's optional legislation features and the ``political.legislation`` provider (#2447)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import validate_composition_manifest, validate_provider_descriptor
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb.legislation import feature_enabled
from tests.unit.composition.test_migration import _migrated
from tests.unit.composition.test_political_lobbying_composition import bound, political_plan
from tools.knowledge_engine_mcp.legislation import LEGISLATION_SCOPES

ROOT = Path(__file__).resolve().parents[3]
FEATURE_PROVIDERS = {
    "political.core",
    "political.legislation",
    "ownership.core",
    "platform.entity-identity",
    "platform.subscriptions",
    "platform.source-runtime",
}


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
    return next(d for d in provider_descriptors() if d["id"] == "political.legislation")


def test_descriptor_declares_operations_scopes_stores_and_the_source_pack():
    found = descriptor()
    assert validate_provider_descriptor(found) == []
    capability = found["capabilities"][0]
    assert capability["contract"] == {"name": "noesis-legislation-record", "version": "1.0.0"}
    assert "no passage prediction" in capability["semantic_constraints"]["exclusions"]
    tools = {op["tool"].rsplit(".", 1)[1]: op for op in found["operations"]}
    assert {"bill_dossier_as_of", "list_bill_votes", "lookup_sponsor_bills", "review_legislation_identity_match",
            "create_legislation_monitor", "export_bill_evidence_bundle"} <= set(tools)
    for name, op in tools.items():
        assert op["required_scopes"] == LEGISLATION_SCOPES[name]
    assert found["source_packs"] == [{"pack_id": "official-political-records", "version": "1.3.0",
                                      "range": "^1.3.0"}]
    owned = {s["record_type"] for s in found["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "political.legislation"
              for s in d["stores"]}
    assert not owned & others  # no second dossier, entity or subscription store
    assert all("legislative_dossier" not in t for s in found["stores"] for t in s["tables"])


def test_us_and_uk_are_separate_optional_features_off_by_default():
    composition = json.loads((ROOT / "packs/political/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    for feature in ("legislation-us", "legislation-uk"):
        assert features[feature]["default"] is False
        required = {r["capability"] for r in features[feature]["requires"]}
        assert "political.legislation" in required
        # lobbying and Legal links degrade when absent, so the features never require them
        assert not required & {"political.lobbying", "legal.works", "legal.core"}
    assert validate_composition_manifest(adapt_all()["political"]) == []
    plan = political_plan()
    assert "political.legislation" not in bound(plan)


@pytest.mark.parametrize("selection", [["legislation-us"], ["legislation-uk"], ["legislation-us", "legislation-uk"]])
def test_selecting_a_feature_binds_the_provider_and_consumed_ones(selection):
    plan = political_plan(selection)
    assert sorted(plan["features"]["political"]) == sorted(selection)
    assert bound(plan) == FEATURE_PROVIDERS
    # the bundle ships 1.4.0 (campaign-finance sources, #2209); the descriptor's ^1.3.0 range still resolves
    assert {"pack_id": "official-political-records", "version": "1.4.0", "range": "^1.4.0"} in plan["source_packs"]


def test_features_coexist_with_lobbying_and_elections():
    bundles = adapt_all()
    for selection in (["lobbying", "legislation-us"], ["elections", "legislation-uk"]):
        plan = political_plan(selection, bundles=bundles)
        assert "political.legislation" in bound(plan)
        assert ("political.lobbying" in bound(plan)) == ("lobbying" in selection)


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert not feature_enabled(conn, "legislation-us") and not feature_enabled(conn, "legislation-uk")
    coordinator.select("political", bundles["political"]["version"], features=["legislation-us"])
    assert coordinator.activate("political-legislation-us-on")["status"] == "published"
    assert feature_enabled(conn, "legislation-us") and not feature_enabled(conn, "legislation-uk")
    pack = json.loads((ROOT / "packs/political/pack.json").read_text())
    assert {"bill passage prediction", "member scoring or ideology ratings",
            "summaries presented as a bill's legal effect"} <= set(pack["exclusions"])
    assert not list(ROOT.glob("packs/*legislation*"))  # no new pack
