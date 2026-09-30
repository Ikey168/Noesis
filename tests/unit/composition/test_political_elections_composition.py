"""The Political bundle's optional ``elections`` feature and the ``political.elections`` provider (#2000)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import (
    validate_composition_manifest,
    validate_provider_descriptor,
)
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb.elections import feature_enabled
from tests.unit.composition.test_migration import _migrated
from tests.unit.composition.test_political_lobbying_composition import (
    bound,
    political_plan,
)

ROOT = Path(__file__).resolve().parents[3]
FEATURE_PROVIDERS = {
    "political.core",
    "political.elections",
    "geospatial.core",
    "news.core",
    "ownership.core",
    "platform.entity-identity",
    "platform.subscriptions",
    "platform.source-runtime",
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


def test_descriptor_declares_the_capability_read_only_operations_stores_probe_and_source_pack():
    descriptor = next(
        d for d in provider_descriptors() if d["id"] == "political.elections"
    )
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["implementation"] == {
        "name": "src.kb.elections",
        "version": "1.0.0",
        "server": "noesis-knowledge-engine",
    }
    (capability,) = descriptor["capabilities"]
    assert capability["id"] == "political.election-records"
    assert capability["contract"] == {
        "name": "noesis-election-record",
        "version": "1.0.0",
    }
    assert {o["side_effect"] for o in descriptor["operations"]} == {"read-only"}
    tools = {o["tool"].rsplit(".", 1)[1] for o in descriptor["operations"]}
    assert {
        "election_contest_results",
        "election_contest_dossier",
        "list_election_poll_series",
    } <= tools
    stores = {s["store"]: s for s in descriptor["stores"]}
    assert all(
        s["revision_addressable"] and s["namespace_scoped"] for s in stores.values()
    )
    assert all(t.startswith("election_") for s in stores.values() for t in s["tables"])
    assert descriptor["readiness_probes"] == [
        {"id": "vintages", "kind": "table-exists", "target": "election_result_vintages"}
    ]
    assert descriptor["source_packs"] == [
        {"pack_id": "official-political-records", "version": "1.2.0", "range": "^1.2.0"}
    ]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {
        s["record_type"]
        for d in provider_descriptors()
        if d["id"] != "political.elections"
        for s in d["stores"]
    }
    assert (
        not owned & others
    )  # no second forecast, subscription, entity or spatial store


def test_the_bundle_validates_and_works_with_the_feature_off_by_default():
    composition = json.loads((ROOT / "packs/political/composition.json").read_text())
    feature = next(
        f for f in composition["optional_features"] if f["id"] == "elections"
    )
    assert feature["default"] is False
    assert {r["capability"] for r in feature["requires"]} >= {
        "political.election-records",
        "political.knowledge",
        "geospatial.feature-query",
        "geospatial.place-resolution",
        "platform.entity-identity",
        "platform.subscriptions",
        "platform.source-acquisition",
    }
    assert validate_composition_manifest(adapt_all()["political"]) == []
    plan = political_plan()
    assert plan["features"]["political"] == [] and bound(plan) == {"political.core"}
    assert {
        "pack": "political",
        "feature": "elections",
        "reason": "not selected",
    } in plan["omissions"]


def test_enabling_it_binds_the_geospatial_news_identity_subscription_and_runtime_providers():
    plan = political_plan(["elections"])
    assert (
        plan["features"]["political"] == ["elections"]
        and bound(plan) == FEATURE_PROVIDERS
    )
    assert {
        "pack_id": "official-political-records",
        "version": "1.4.0",  # 1.3.0 legislation (#2208), 1.4.0 campaign finance (#2209); result sources unchanged
        "range": "^1.4.0",
    } in plan["source_packs"]
    profiles = {p["id"]: p for p in adapt_all()["political"]["contributes"]["profiles"]}
    defaults = profiles["political.elections-review"]["workflow_defaults"]
    assert (
        defaults["boundary_collection"]["de-be-bezirk"]
        == "alkis_bezirke:bezirksgrenzen"
    )
    assert defaults["forecast_resolution"] == "certified vintage only"


def test_a_missing_geospatial_provider_is_a_visible_omission():
    descriptors = [d for d in provider_descriptors() if d["id"] != "geospatial.core"]
    plan = political_plan(["elections"], descriptors)
    assert plan["features"]["political"] == [] and "political.elections" not in bound(
        plan
    )
    omission = next(o for o in plan["omissions"] if o["feature"] == "elections")
    assert (
        omission["capability"].startswith("geospatial.")
        and "missing_contract" in omission["reason"]
    )


def test_no_new_pack_source_pack_manifest_or_enablement_flag():
    pack = json.loads((ROOT / "packs/political/pack.json").read_text())
    assert {"election-result-vintages", "election-poll-series"} <= set(
        pack["capabilities"]
    )
    assert {"election", "contest", "result_vintage", "poll_series"} <= set(
        pack["ontology_extensions"]["object_types"]
    )
    assert "contests" in pack["ontology_extensions"]["relation_types"]
    assert {
        "seat or outcome predictions",
        "poll aggregation into a single true number",
    } <= set(pack["exclusions"])
    assert not list(ROOT.glob("packs/*election*")) and not list(
        ROOT.glob("config/source_packs/*election*")
    )
    from tools.knowledge_engine_mcp import elections

    assert not [
        t
        for t in elections.ELECTION_TOOLS
        if t.startswith("set_") and t.endswith("_enabled")
    ]


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select(
        "political", bundles["political"]["version"], features=["elections"]
    )
    assert (
        coordinator.activate("political-elections-on")["status"] == "published"
        and feature_enabled(conn) is True
    )
    coordinator.select(
        "political", bundles["political"]["version"], features=["lobbying"]
    )
    coordinator.activate("political-elections-off")
    assert feature_enabled(conn) is False
