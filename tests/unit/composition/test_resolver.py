"""Composition resolver (C03): closure, version locking, binding, plan assembly, resume."""

from __future__ import annotations

import ast
import builtins
import copy
import json
import random
import socket
from pathlib import Path

import pytest

from src.composition import resolver as resolver_module
from src.composition.contracts import validate_composition_manifest, validate_plan, validate_provider_descriptor
from src.composition.resolver import resolve, resume
from tests.unit import composition_corpus as corpus

CASES = json.loads(corpus.RESOLVER_CORPUS.read_text())["cases"]
OSINT = [{"pack": "osint", "range": "^1.0.0"}]


def _run(case, **overrides):
    options = {"selections": case.get("selections"), **overrides}
    return resolve(case["roots"], case["packs"], case["providers"], **options)


def test_committed_resolver_corpus_matches_its_builder():
    assert corpus.RESOLVER_CORPUS.read_text() == corpus.build_resolver()


def test_corpus_documents_are_valid_contracts():
    for case in CASES:
        for manifest in case["packs"]:
            assert validate_composition_manifest(manifest) == [], (case["name"], manifest["id"])
        for descriptor in case["providers"]:
            assert validate_provider_descriptor(descriptor) == [], (case["name"], descriptor["id"])


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_resolver_corpus(case):
    result = _run(case)
    if case["expect"] != "ok":
        assert not result.ok and result.failure.code == case["expect"], result
        return
    assert result.ok, result.failure
    assert validate_plan(result.plan) == []
    bindings = {b["capability"]: [b["provider"], b["provider_version"], b["reason"]] for b in result.plan["bindings"]}
    for capability, expected in case.get("bindings", {}).items():
        assert bindings[capability] == expected
    omitted = {f"{o['pack']}.{o['feature']}" for o in result.plan["omissions"]}
    assert set(case.get("omitted", [])) <= omitted


# ------------------------------------------------------------ C03.1 closure


def test_closure_joins_the_contributing_pack_and_records_edges():
    world = corpus.resolver_world()
    plan = resolve(OSINT, world["packs"], world["providers"]).plan
    assert [(p["id"], p["version"]) for p in plan["packs"]] == [("geospatial", "1.1.0"), ("osint", "1.0.0")]
    kinds = {(e["from"], e["kind"], e["to"]) for e in plan["graph"]}
    assert ("osint", "requires", "geospatial.spatial-relation") in kinds
    assert ("geospatial", "contributes", "geospatial.spatial-relation") in kinds
    assert ("neuronews-osint", "alias", "osint") in kinds
    assert plan["features"] == {"geospatial": [], "osint": []}
    assert {"pack": "osint", "feature": "imagery", "reason": "not selected"} in plan["omissions"]


def test_selected_but_unavailable_optional_branch_is_a_visible_omission():
    world = corpus.resolver_world()
    plan = resolve([{**OSINT[0], "features": ["imagery"]}], world["packs"], world["providers"]).plan
    omission = next(o for o in plan["omissions"] if o["feature"] == "imagery")
    assert omission["capability"] == "osint.media-provenance"
    assert omission["reason"].startswith("unavailable (missing_contract)")
    assert plan["features"]["osint"] == []
    assert "osint.media-provenance" not in {b["capability"] for b in plan["bindings"]}


def test_cycle_reports_the_full_path_by_pack_and_version():
    case = next(c for c in CASES if c["name"] == "fail-cycle")
    failure = _run(case).failure
    assert failure.details["path"] == ["cyc-a@1.0.0", "cyc-b@1.0.0", "cyc-a@1.0.0"]
    assert "cyc-a@1.0.0 -> cyc-b@1.0.0 -> cyc-a@1.0.0" in failure.message


def test_resolver_imports_nothing_that_performs_io():
    tree = ast.parse(Path(resolver_module.__file__).read_text())
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert imported <= {"__future__", "re", "collections.abc", "dataclasses", "typing",
                        "src.composition.contracts"}


def test_resolution_performs_no_io(monkeypatch):
    world = corpus.resolver_world()
    resolve(OSINT, world["packs"], world["providers"])  # warm the lazy range-grammar import

    def refuse(*_args, **_kwargs):
        raise AssertionError("resolver performed I/O")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    assert resolve(OSINT, world["packs"], world["providers"]).ok


# ------------------------------------------------------------ C03.2 versions and pins


def test_highest_satisfying_version_and_retained_compatible_pin():
    world = corpus.resolver_world()
    fresh = resolve(OSINT, world["packs"], world["providers"]).plan
    assert {p["id"]: p["version"] for p in fresh["providers"]} == {"geospatial.core": "1.1.0"}
    older = resolve(OSINT, world["packs"], [world["providers"][0]]).plan
    kept = resolve(OSINT, world["packs"], world["providers"], retained=older).plan
    assert {p["id"]: p["version"] for p in kept["providers"]} == {"geospatial.core": "1.0.0"}
    assert kept["packs"] == older["packs"]
    upgraded = resolve(OSINT, world["packs"], world["providers"], retained=older,
                       upgrade={"geospatial", "geospatial.core"}).plan
    assert upgraded["digest"] == fresh["digest"]


def test_incompatible_retained_pin_fails_instead_of_upgrading():
    world = corpus.resolver_world()
    older = resolve(OSINT, world["packs"], [world["providers"][0]]).plan
    stricter = copy.deepcopy(world["packs"])
    stricter[0] = corpus.pack("osint", requires=[corpus.req("geospatial.spatial-relation", "noesis-spatial-relation",
                                                            "^1.1.0")], aliases={"neuronews-osint": "osint"})
    failure = resolve(OSINT, stricter, world["providers"], retained=older, upgrade={"osint"}).failure
    assert failure.code == "incompatible_retained_pin"
    assert "osint" in failure.message and "geospatial" in failure.message and "1.0.0" in failure.message


def test_failure_messages_name_pack_capability_and_offending_version():
    for name in ("fail-incompatible-range", "fail-conflicting-major"):
        failure = _run(next(c for c in CASES if c["name"] == name)).failure
        assert "geospatial" in failure.message and "1." in failure.message
    major = _run(next(c for c in CASES if c["name"] == "fail-conflicting-major")).failure
    assert major.details["majors"] == [1, 2] and "mapper" in major.message


# ------------------------------------------------------------ C03.3 binding


def test_ambiguity_names_both_providers():
    failure = _run(next(c for c in CASES if c["name"] == "fail-ambiguous")).failure
    assert failure.details["candidates"] == ["geospatial.core", "osm.spatial"]


def test_semantic_mismatch_names_the_constraint():
    failure = _run(next(c for c in CASES if c["name"] == "fail-semantic-mismatch")).failure
    assert failure.details["constraint"] == "crs" and "EPSG:3857" in failure.message


def test_binding_records_consumers_contract_and_output_contracts():
    world = corpus.resolver_world()
    plan = resolve(OSINT + [{"pack": "science", "range": "^1.0.0"}], world["packs"], world["providers"]).plan
    (binding,) = plan["bindings"]
    assert binding["consumers"] == ["osint", "science"]
    assert binding["contract"] == {"name": "noesis-spatial-relation", "version": "1.1.0"}
    assert binding["operations"] == ["calculate-spatial-relation", "record-resolution", "store-geometry"]
    assert plan["output_contracts"] == [{"capability": "geospatial.spatial-relation",
                                         "name": "noesis-spatial-relation", "version": "1.1.0"}]
    assert plan["source_packs"] == [{"pack_id": "openalex", "version": "1.0.0"}]


# ------------------------------------------------------------ C03.4 plan and resume


def test_determinism_harness_shuffled_inputs_give_byte_identical_plans():
    world = corpus.resolver_world()
    extra = world["providers"] + [corpus.media_provider()]
    roots = [{**OSINT[0], "features": ["imagery"]}, {"pack": "research", "range": "^1.0.0"}]
    baseline = resolve(roots, world["packs"], extra).plan
    rng = random.Random(1813)
    for _ in range(25):
        packs, providers, shuffled_roots = list(world["packs"]), list(extra), list(roots)
        for items in (packs, providers, shuffled_roots):
            rng.shuffle(items)
        plan = resolve(shuffled_roots, packs, providers).plan
        assert json.dumps(plan, sort_keys=True) == json.dumps(baseline, sort_keys=True)


def test_resume_current_changed_and_unavailable():
    world = corpus.resolver_world()
    plan = resolve(OSINT, world["packs"], world["providers"]).plan
    assert resume(plan, world["packs"], world["providers"]).status == "current"

    changed = copy.deepcopy(world["providers"])
    changed[1]["capabilities"][0]["semantic_constraints"]["distance_units"] = "kilometers"
    result = resume(plan, world["packs"], changed)
    assert result.status == "new_plan_required"
    assert result.diff[0]["kind"] == "provider" and result.diff[0]["id"] == "geospatial.core"
    assert result.replan.ok and result.replan.plan["digest"] != plan["digest"]

    gone = resume(plan, world["packs"], [])
    assert gone.status == "unavailable_for_replay"
    assert gone.unavailable == [{"kind": "provider", "id": "geospatial.core",
                                 "pinned": {"version": "1.1.0", "content_hash": plan["providers"][0]["content_hash"]}}]

    tampered = {**plan, "resolver_version": "9.9.9"}
    assert resume(tampered, world["packs"], world["providers"]).status == "invalid_plan"
