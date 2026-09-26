"""Composition contracts, v1 adapters and the fixture corpus (C02, #1804-#1809)."""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path

import duckdb
import pytest

from src.composition import contracts as c
from src.domains import pack_install
from src.domains.pack_format import (
    PACK_FORMAT_V2,
    PackManifest,
    validate_any_manifest,
    validate_composition_manifest,
    validate_manifest,
)
from src.kb.schema_registry import READ_SCOPE, REGISTER_SCOPE, SchemaRegistry

ROOT = Path(__file__).resolve().parents[3]
CORPUS = sorted((ROOT / "tests/fixtures/composition/contracts").glob("*.json"))
PACKS = sorted((ROOT / "packs").glob("*/pack.json"))


def _validate(contract: str, document, context: dict):
    if contract == "manifest":
        return c.validate_manifest(document, known_capabilities=context.get("known_capabilities"))
    if contract == "provider":
        return c.validate_provider(document)
    if contract == "providers":
        return c.validate_providers(document)
    if contract == "plan":
        return c.validate_plan(document)
    if contract == "readiness":
        return c.validate_readiness(document)
    if contract == "receipt":
        plans = context.get("generation_plans")
        return c.validate_receipt(
            document,
            generation_plans=None if plans is None else {int(k): v for k, v in plans.items()},
        )
    raise AssertionError(contract)


@pytest.mark.parametrize("path", CORPUS, ids=lambda path: path.stem)
def test_fixture_corpus_validates_or_rejects_with_expected_class(path: Path):
    case = json.loads(path.read_text())
    if case["valid"]:
        _validate(case["contract"], case["document"], case.get("context", {}))
        return
    with pytest.raises(c.CompositionError) as caught:
        _validate(case["contract"], case["document"], case.get("context", {}))
    assert caught.value.code == case["expect"], caught.value.as_dict()


def test_corpus_covers_each_required_category_with_valid_and_invalid_variants():
    coverage: dict[str, set[bool]] = {}
    for path in CORPUS:
        case = json.loads(path.read_text())
        for item in case["covers"]:
            coverage.setdefault(item, set()).add(case["valid"])
    for category in ("aliases", "ranges", "store-owners", "effects", "critical-unknown-fields"):
        assert coverage.get(category) == {True, False}, category


def test_manifest_rejections_named_by_the_issue():
    codes = {json.loads(p.read_text()).get("expect") for p in CORPUS}
    assert {"unknown_critical_extension", "unknown_capability", "invalid_range",
            "executable_reference"} <= codes


def test_v1_validation_is_unchanged_and_v2_is_dispatched_by_format():
    for path in PACKS:
        data = json.loads(path.read_text())
        assert validate_manifest(data) == []
        assert validate_any_manifest(data) == []
    adapted = c.load_pack(PACKS[0].parent)
    assert adapted["pack_format"] == PACK_FORMAT_V2
    assert validate_any_manifest(adapted) == []
    # v1 validation still rejects a v2 document; v2 validation reports errors as a list.
    assert validate_manifest(adapted)
    broken = dict(adapted, requires=[{"capability": "spatial.relation", "range": "latest"}])
    assert validate_composition_manifest(broken)


@pytest.mark.parametrize("path", PACKS, ids=lambda path: path.parent.name)
def test_v1_pack_round_trips_through_the_adapter(path: Path):
    v1 = json.loads(path.read_text())
    adapted = c.load_pack(path.parent)
    legacy = PackManifest.from_dict(v1)
    view = c.v1_view(adapted)
    assert view == legacy.to_dict()
    assert adapted["contributes"].get("capability_labels", []) == legacy.capabilities
    assert {r["name"]: r["range"] for r in adapted.get("references", {}).get("contracts", [])} == (
        legacy.schema_versions)
    assert adapted["contributes"].get("ontology", {}) == legacy.ontology_extensions
    if (path.parent / "composition.json").exists():
        assert "adapter" not in adapted  # composition fields are declared, not adapter-supplied
    else:
        assert set(adapted["adapter"]["supplied_defaults"]) >= {"requires", "store_ownership",
                                                                "contract_ranges"}
    try:
        before = pack_install.install_manifest(legacy)
        after = pack_install.install_manifest(PackManifest.from_dict(view))
        assert before == after
    finally:
        pack_install.uninstall(legacy.name)


def test_code_registered_domain_packs_adapt_with_identical_contract_facts():
    from src.composition.identifiers import code_registered_packs

    packs = code_registered_packs()
    assert packs
    for name, pack in packs.items():
        adapted = c.adapt_domain_pack(pack)
        assert adapted["adapter"]["source"] == "domain-pack"
        assert adapted["contributes"].get("capability_labels", []) == list(pack.capabilities)
        assert {r["name"]: r["range"] for r in adapted.get("references", {}).get("contracts", [])} == (
            dict(pack.schema_versions))
        assert adapted["contributes"].get("ontology", {}) == dict(pack.ontology_extensions)
        assert adapted["adapter"]["declared_routes"] == list(pack.route_modules)
        assert "requires" in adapted["adapter"]["supplied_defaults"], name


def test_adapter_never_writes_back_or_changes_enablement():
    from src.domains import registry

    before = set(registry._ENABLED)
    snapshots = {p: p.read_bytes() for p in PACKS}
    c.load_all_packs()
    assert set(registry._ENABLED) == before
    assert all(p.read_bytes() == data for p, data in snapshots.items())


def test_geospatial_descriptor_binds_the_three_named_tools():
    (provider,) = [p for p in c.load_providers() if p["provider_id"] == "noesis.geospatial"]
    tools = {b["id"] for cap in provider["capabilities"] for b in cap["bindings"]}
    assert {
        "noesis-knowledge-engine.calculate_spatial_relation",
        "noesis-knowledge-engine.store_geospatial_geometry",
        "noesis-knowledge-engine.record_geospatial_resolution",
    } <= tools
    for capability in provider["capabilities"]:
        assert capability["effect"] in c.EFFECTS
        assert capability["readiness"]["probe"]


def test_plan_digest_ignores_array_order_and_tracks_every_pin():
    plan = json.loads((ROOT / "tests/fixtures/composition/contracts/plan-valid.json").read_text())[
        "document"]
    shuffled = copy.deepcopy(plan)
    rng = random.Random(7)
    for key, value in shuffled.items():
        if isinstance(value, list):
            rng.shuffle(value)
    shuffled["manifests"].append({"name": "zeta", "version": "1.0.0", "manifest_hash": "sha256:" + "3" * 64})
    reference = copy.deepcopy(plan)
    reference["manifests"].insert(0, {"name": "zeta", "version": "1.0.0", "manifest_hash": "sha256:" + "3" * 64})
    assert c.plan_digest(shuffled) == c.plan_digest(reference)
    changed = copy.deepcopy(plan)
    changed["manifests"][0]["manifest_hash"] = "sha256:" + "4" * 64
    assert c.plan_digest(changed) != c.plan_digest(plan)


def test_contracts_resolve_through_the_schema_registry_with_declared_dependencies():
    conn = duckdb.connect(":memory:")
    registry = SchemaRegistry(conn)
    for contract, (name, version, depends) in c.CONTRACT_MODULES.items():
        module = registry.resolve("schema", name, version, scopes={READ_SCOPE})
        assert module["content"]["$id"].endswith(f"{contract}.json")
        assert {d["name"] for d in module["dependencies"]} == set(depends)
    declared = c.declare_contract_dependencies(
        registry, principal_id="operator", scopes={READ_SCOPE, REGISTER_SCOPE}
    )
    assert len(declared) == sum(len(d) for _, _, d in c.CONTRACT_MODULES.values())
    plan_module = registry.resolve("schema", "composition-plan", "1.0.0", scopes={READ_SCOPE})
    manifest_module = registry.resolve("schema", "pack-manifest", "2.0.0", scopes={READ_SCOPE})
    impact = registry.impact(manifest_module["module_id"], scopes={READ_SCOPE})
    assert plan_module["module_id"] in json.dumps(impact)
