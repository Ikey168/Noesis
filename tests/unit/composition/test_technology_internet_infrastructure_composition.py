"""The Technology bundle's optional internet-infrastructure features and the ``technology.internet-infrastructure``
provider (II11, #2802 under #2743)."""

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
from src.kb.internet_infrastructure_records import FEATURES, feature_enabled, readiness
from tests.unit.composition.test_migration import _migrated

ROOT = Path(__file__).resolve().parents[3]
BASE = {"technology.core", "technology.patents", "technology.standards"}
FEATURE_PROVIDERS = BASE | {"technology.internet-infrastructure", "platform.subscriptions", "platform.source-runtime"}
PACK = {"pack_id": "technology-internet-infrastructure", "version": "1.0.0", "range": "^1.0.0"}
TOOL = "noesis-knowledge-engine.internet_infrastructure_records_as_of"


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
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
    result = resolve([root], list(bundles.values()), descriptors if descriptors is not None else provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def bound(plan):
    return {b["provider"] for b in plan["bindings"] if "technology" in b["consumers"]}


def test_descriptor_declares_capability_read_only_operations_stores_probe_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "technology.internet-infrastructure")
    assert validate_provider_descriptor(descriptor) == []
    (capability,) = descriptor["capabilities"]
    assert capability["contract"] == {"name": "noesis-internet-infrastructure-record", "version": "2.0.0"}
    constraints = capability["semantic_constraints"]
    assert {"records", "observations", "no_merging", "identity", "links", "lookups", "minimisation",
            "exclusions"} <= set(constraints)
    assert "no IP-keyed pivots" in constraints["exclusions"] and "not a gated tool" in constraints["lookups"]
    assert {o["side_effect"] for o in descriptor["operations"]} == {"read-only"}
    tools = {o["tool"].split(".", 1)[1] for o in descriptor["operations"]}
    assert {"internet_infrastructure_records_as_of", "internet_infrastructure_history",
            "export_internet_infrastructure_bundle"} <= tools
    assert descriptor["readiness_probes"] == [{"id": "revisions", "kind": "table-exists", "target": "ii_revisions"}]
    assert descriptor["source_packs"] == [PACK]
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {s["record_type"] for d in provider_descriptors() if d["id"] != "technology.internet-infrastructure"
              for s in d["stores"]}
    assert not owned & others
    assert all(t.startswith("ii_") for s in descriptor["stores"] for t in s["tables"])
    installed = validate_source_pack(json.loads(
        (ROOT / "config/source_packs/technology-internet-infrastructure.json").read_text()))
    assert installed["version"] == PACK["version"]


def test_features_are_optional_default_off_per_source_in_the_existing_technology_pack():
    composition = json.loads((ROOT / "packs/technology/composition.json").read_text())
    features = {f["id"]: f for f in composition["optional_features"]}
    assert set(FEATURES) <= set(features)
    for feature_id in FEATURES:
        feature = features[feature_id]
        assert feature["default"] is False
        assert {r["capability"] for r in feature["requires"]} == {
            "technology.internet-infrastructure", "platform.subscriptions", "platform.source-acquisition"}
        # OSINT and Vulnerabilities links degrade gracefully: neither provider is required.
        assert "provider_absent" in feature["description"] and "no IP-keyed" in feature["description"]
    assert features["vulnerabilities"]["default"] is False  # the existing feature is unchanged
    assert {"technology.internet-infrastructure"} <= {p["id"] for p in composition["contributes"]["providers"]}
    assert validate_composition_manifest(adapt_all()["technology"]) == []
    pack = json.loads((ROOT / "packs/technology/pack.json").read_text())
    assert {"exposed-service, port or banner data", "IP-keyed or person-keyed infrastructure lookups",
            "reputation, risk, hijack or misconfiguration verdicts", "ranking of networks"} <= set(pack["exclusions"])
    assert pack["schema_versions"]["internet-infrastructure-record"] == "2.0.0"
    assert {"asn", "rpki", "certificate transparency"} <= set(pack["planner_keywords"]["claims"])
    assert "network-registry-revisions" in pack["capabilities"]
    assert "autonomous_system" in pack["ontology_extensions"]["object_types"]
    assert not list(ROOT.glob("packs/*internet*"))  # no new pack


@pytest.mark.parametrize("selection", [[], ["internet-infrastructure-ripestat"], list(FEATURES),
                                       ["internet-infrastructure-rdap", "vulnerabilities"]])
def test_each_selection_resolves_independently_and_together(selection):
    plan = technology_plan(selection)
    selected = bool(set(selection) & set(FEATURES))
    assert ("technology.internet-infrastructure" in bound(plan)) == selected
    if selection == ["internet-infrastructure-ripestat"]:
        # The bound provider pins its own separate source pack (asserted on the descriptor); the bundle's pins stay.
        assert bound(plan) == FEATURE_PROVIDERS
    if not selected:
        assert PACK not in plan["source_packs"]
    view = CompositionView(plan, provider_descriptors(), adapt_all().values())
    assert (TOOL in view.tools) == selected


def test_readiness_and_feature_enablement_follow_the_composition_selection():
    report = readiness(duckdb.connect(":memory:"))
    assert report["selected"] == [] and report["stores_ready"] is False
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live"}
    conn, coordinator, bundles, _ = _migrated()
    assert feature_enabled(conn) is False
    coordinator.select("technology", bundles["technology"]["version"], features=["internet-infrastructure-ct"])
    assert coordinator.activate("technology-ii-on")["status"] == "published"
    assert feature_enabled(conn) is True and feature_enabled(conn, "internet-infrastructure-ct") is True
    assert feature_enabled(conn, "internet-infrastructure-ripestat") is False
    coordinator.select("technology", bundles["technology"]["version"], features=[])
    coordinator.activate("technology-ii-off")
    assert feature_enabled(conn) is False
    doc = (ROOT / "docs/architecture/composition-migration.md").read_text()
    assert "`internet-infrastructure-ripestat`" in doc and "`noesis-internet-infrastructure-record-v2`" in doc


def test_taxonomy_classifies_the_provider_and_the_gap_row_is_removed():
    taxonomy = json.loads((ROOT / "packs/taxonomy.json").read_text())
    assert taxonomy["providers"]["technology.internet-infrastructure"] == {
        "subdomains": ["internet-infrastructure"], "shapes": ["registry-records", "observations"]}
    program = (ROOT / "docs/roadmaps/domain-coverage-program.md").read_text()
    assert "| `internet-infrastructure` | Technology |" not in program
    assert "68 are covered and 21 are gaps" in program
    assert "Today 68 are covered" in (ROOT / "README.md").read_text()
