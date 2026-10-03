"""The Technology bundle's optional ``vulnerabilities`` feature and the ``technology.vulnerabilities`` provider (#2012)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import (
    validate_composition_manifest,
    validate_provider_descriptor,
)
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.kb.vulnerabilities import feature_enabled
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
BASE = {"technology.core", "technology.patents", "technology.standards"}
FEATURE_PROVIDERS = BASE | {
    "technology.vulnerabilities",
    "products.core",
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


def technology_plan(features=None, descriptors=None):
    bundles = adapt_all()
    root = {"pack": "technology", "version": bundles["technology"]["version"]}
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
    return {b["provider"] for b in plan["bindings"] if "technology" in b["consumers"]}


def test_descriptor_declares_the_capability_stores_probe_and_source_pack():
    descriptor = next(
        d for d in provider_descriptors() if d["id"] == "technology.vulnerabilities"
    )
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["implementation"] == {
        "name": "src.kb.vulnerabilities",
        "version": "1.0.0",
        "server": "noesis-knowledge-engine",
    }
    capability = descriptor["capabilities"][0]
    assert capability["contract"] == {
        "name": "noesis-vulnerability-record",
        "version": "1.0.0",
    }
    assert {o["tool"].rsplit(".", 1)[1] for o in descriptor["operations"]} >= {
        "inspect_vulnerability",
        "search_component_advisories",
        "compare_advisory_revisions",
    }
    assert all(o["side_effect"] == "read-only" for o in descriptor["operations"])
    store = descriptor["stores"][0]
    assert (
        store["store"] == "src.kb.vulnerabilities"
        and store["revision_addressable"]
        and store["namespace_scoped"]
    )
    assert "vuln_revisions" in store["tables"]
    assert descriptor["readiness_probes"] == [
        {"id": "revisions", "kind": "table-exists", "target": "vuln_revisions"}
    ]
    assert descriptor["source_packs"] == [
        {
            "pack_id": "technical-software-knowledge",
            "version": "1.2.0",
            "range": "^1.2.0",
        }
    ]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {
        s["record_type"]
        for d in provider_descriptors()
        if d["id"] != "technology.vulnerabilities"
        for s in d["stores"]
    }
    assert not owned & others


def test_the_bundle_resolves_with_the_feature_off_by_default():
    composition = json.loads((ROOT / "packs/technology/composition.json").read_text())
    feature = composition["optional_features"][0]
    assert feature["id"] == "vulnerabilities" and feature["default"] is False
    assert {r["capability"] for r in feature["requires"]} == {
        "technology.vulnerabilities",
        "technology.knowledge",
        "products.identities",
        "platform.entity-identity",
        "platform.subscriptions",
        "platform.source-acquisition",
    }
    assert validate_composition_manifest(adapt_all()["technology"]) == []
    plan = technology_plan()
    assert plan["features"]["technology"] == [] and bound(plan) == BASE
    assert {
        "pack": "technology",
        "feature": "vulnerabilities",
        "reason": "not selected",
    } in plan["omissions"]
    # The existing kb_technical, patent and standards entry points keep their providers.
    assert {"technology.core", "technology.patents", "technology.standards"} <= bound(
        plan
    )


def test_selecting_the_feature_binds_its_provider_and_the_consumed_ones():
    plan = technology_plan(["vulnerabilities"])
    assert (
        plan["features"]["technology"] == ["vulnerabilities"]
        and bound(plan) == FEATURE_PROVIDERS
    )
    # The consumed Products bundle is pulled in with its own optional safety (#1916), appliances and components
    # (#2061) features left unselected;
    # nothing of the vulnerabilities feature is omitted.
    # The Technology bundle's own AI models features (#2742) stay unselected too.
    assert plan["omissions"] == [
        {"pack": "technology", "feature": "ai-models-epoch", "reason": "not selected"},
        {"pack": "technology", "feature": "ai-models-hub", "reason": "not selected"},
        {"pack": "technology", "feature": "ai-models-openml", "reason": "not selected"},
        {"pack": "products", "feature": "appliances", "reason": "not selected"},
        {"pack": "products", "feature": "components", "reason": "not selected"},
        {"pack": "products", "feature": "food", "reason": "not selected"},  # food composition (#2216)
        # The Technology internet-infrastructure features (#2743) stay unselected.
        {"pack": "technology", "feature": "internet-infrastructure-ct", "reason": "not selected"},
        {"pack": "technology", "feature": "internet-infrastructure-peeringdb", "reason": "not selected"},
        {"pack": "technology", "feature": "internet-infrastructure-rdap", "reason": "not selected"},
        {"pack": "technology", "feature": "internet-infrastructure-ripestat", "reason": "not selected"},
        {"pack": "products", "feature": "safety", "reason": "not selected"},
    ]
    assert {
        "pack_id": "technical-software-knowledge",
        "version": "1.2.0",
        "range": "^1.2.0",
    } in plan["source_packs"]


def test_a_missing_consumed_provider_is_a_visible_omission_not_a_failure():
    descriptors = [d for d in provider_descriptors() if d["id"] != "products.core"]
    plan = technology_plan(["vulnerabilities"], descriptors)
    assert plan["features"]["technology"] == [] and bound(plan) == BASE
    omission = next(o for o in plan["omissions"] if o["feature"] == "vulnerabilities")
    assert (
        omission["capability"] == "products.identities"
        and "missing_contract" in omission["reason"]
    )


def test_no_new_pack_directory_or_enablement_flag_is_added():
    pack = json.loads((ROOT / "packs/technology/pack.json").read_text())
    assert {"vulnerability-advisory-revisions", "reviewable-component-identity"} <= set(
        pack["capabilities"]
    )
    assert pack["schema_versions"]["vulnerability-record"] == "1.0.0"
    assert {"exploitability or risk verdicts", "patch or remediation advice"} <= set(
        pack["exclusions"]
    )
    assert not list(ROOT.glob("packs/*vulnerab*"))


def test_feature_enablement_follows_the_active_composition_selection():
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select(
        "technology", bundles["technology"]["version"], features=["vulnerabilities"]
    )
    assert (
        coordinator.activate("technology-vulnerabilities-on")["status"] == "published"
    )
    assert feature_enabled(conn) is True
    coordinator.select("technology", bundles["technology"]["version"], features=[])
    coordinator.activate("technology-vulnerabilities-off")
    assert feature_enabled(conn) is False
