"""Composition contracts (C02): schemas, validators, v1 adapter, registry identities."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb
import pytest

from src.composition import adapter
from src.composition.contracts import (
    CONTRACTS,
    VALIDATORS,
    CompositionContractError,
    declare_contract_dependencies,
    plan_digest,
    require_valid,
    resolve_contract,
    validate_composition_manifest,
    validate_provider_descriptor,
    validate_provider_set,
)
from src.domains.pack_format import (
    PackManifest,
    validate_manifest,
    validate_pack_document,
)
from src.kb.schema_registry import READ_SCOPE, REGISTER_SCOPE, SchemaRegistry
from tests.unit import composition_corpus as corpus

ROOT = Path(__file__).resolve().parents[3]
CASES = json.loads(corpus.CORPUS.read_text())["cases"]


def test_committed_corpus_matches_its_builder():
    assert corpus.CORPUS.read_text() == corpus.build()


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_fixture_corpus(case):
    if case["contract"] == "provider-set":
        issues = validate_provider_set(case["document"])
    else:
        issues = VALIDATORS[case["contract"]](case["document"], **case.get("options", {}))
    if case["valid"]:
        assert issues == []
    else:
        assert case["expect"] in {issue.code for issue in issues}


def test_corpus_covers_every_required_category():
    names = {c["name"] for c in CASES}
    for category in ("alias", "range", "store", "side-effect", "unknown"):
        assert any(category in n and c["valid"] for n in names for c in CASES if c["name"] == n), category
        assert any(category in n and not c["valid"] for n in names for c in CASES if c["name"] == n), category


def test_plan_digest_ignores_array_order_and_tracks_pins():
    plan = corpus.plan(graph=[
        {"from": "osint", "to": "geospatial.spatial-relation", "kind": "requires"},
        {"from": "science", "to": "geospatial.spatial-relation", "kind": "requires"}])
    reordered = copy.deepcopy(plan)
    reordered["graph"].reverse()
    reordered["bindings"][0]["consumers"] = list(reversed(reordered["bindings"][0]["consumers"]))
    assert plan_digest(reordered) == plan["digest"]
    changed = copy.deepcopy(plan)
    changed["providers"][0]["content_hash"] = "sha256:" + "2" * 64
    assert plan_digest(changed) != plan["digest"]


def test_v1_validation_is_unchanged_and_dispatch_routes_by_format():
    for path in sorted((ROOT / "packs").glob("*/pack.json")):
        data = json.loads(path.read_text())
        assert validate_manifest(data) == validate_pack_document(data) == []
    assert validate_pack_document(corpus.manifest()) == []
    broken = {**corpus.manifest(), "installer": {}}
    assert validate_pack_document(broken) and validate_manifest(broken)  # v1 rejects the new format outright


def test_every_bundle_adapts_and_round_trips_to_its_v1_registration():
    adapted = adapter.adapt_all()
    assert set(adapted) >= {"economics", "geospatial", "legal", "market", "news", "osint", "political",
                            "products", "science", "technology", "energy"}
    for bundle, manifest in adapted.items():
        assert validate_composition_manifest(manifest) == [], bundle
        if "adapter" not in manifest:  # authored natively as a composition manifest
            continue
        defaults = set(manifest["adapter"]["supplied_defaults"])
        assert defaults >= {"capability-contracts", "store-ownership"}
        overlay = ROOT / "packs" / bundle / "composition.json"
        declares_requires = overlay.exists() and "requires" in json.loads(overlay.read_text())
        assert ("requires" in defaults) != declares_requires, bundle
    for path in sorted((ROOT / "packs").glob("*/pack.json")):
        data = json.loads(path.read_text())
        v1 = PackManifest.from_dict(data)
        view = adapter.v1_view(adapter.from_pack_manifest(data))
        assert view == {"capabilities": v1.capabilities, "schema_versions": v1.schema_versions,
                        "ontology_extensions": v1.ontology_extensions}
    for pack in adapter.code_packs():
        view = adapter.v1_view(adapter.from_domain_pack(pack))
        assert view == {"capabilities": pack.capabilities, "schema_versions": pack.schema_versions,
                        "ontology_extensions": pack.ontology_extensions}


def test_merged_bundles_keep_both_sources_and_legacy_names():
    legal = adapter.adapt_all()["legal"]
    assert legal["adapter"]["source"] == "noesis-pack-v1+domain-pack+composition-overlay"
    manifest_caps = json.loads((ROOT / "packs/legal/pack.json").read_text())["capabilities"]
    assert adapter.v1_view(legal)["capabilities"][: len(manifest_caps)] == manifest_caps
    science = adapter.adapt_all()["science"]
    assert "research" in science["adapter"]["legacy_names"]
    assert science["compatibility_aliases"] == {"research": "science"}
    assert science["advisory"]["legacy_code_enrichers"] == ["venue", "citation", "concept"]
    assert adapter.adapt_all()["news"]["advisory"]["legacy_route_modules"][0] == "src.api.routes.news_routes"
    assert adapter.adapt_all()["technology"]["compatibility_aliases"] == {"technical": "technology"}


def test_geospatial_descriptor_binds_registered_catalog_tools():
    assert validate_provider_descriptor(corpus.provider()) == []  # default: the generated catalog


def test_contracts_resolve_through_the_schema_registry_with_dependencies():
    conn = duckdb.connect(":memory:")
    registry = SchemaRegistry(conn)
    modules = {contract: resolve_contract(registry, contract) for contract in CONTRACTS}
    assert all(m["provenance"]["kind"] == "builtin" for m in modules.values())
    declared = declare_contract_dependencies(registry, principal_id="operator",
                                             scopes={READ_SCOPE, REGISTER_SCOPE})
    assert len(declared) == 4
    impact = registry.impact(modules["noesis-composition-plan-v1"]["module_id"], scopes={READ_SCOPE})
    consumers = {item["id"] for item in impact["affected"]["module"]}
    assert {modules["noesis-composition-readiness-v1"]["module_id"],
            modules["noesis-composition-activation-receipt-v1"]["module_id"]} <= consumers


def test_require_valid_raises_with_every_issue():
    with pytest.raises(CompositionContractError) as caught:
        require_valid("noesis-composition-readiness-v1", {"contract": "noesis-composition-readiness-v1"})
    assert caught.value.code == "schema" and len(caught.value.issues) >= 3
