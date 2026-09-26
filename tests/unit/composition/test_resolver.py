"""Pure composition resolution (C03, #1810-#1813)."""

from __future__ import annotations

import ast
import builtins
import json
import random
import socket
from pathlib import Path

import pytest

from src.composition import contracts as c
from src.composition import resolver as r
from tests.unit.composition.helpers import CONTRACTS, capability, manifest, provider, req

ROOT = Path(__file__).resolve().parents[3]


def _resolve(roots, manifests, providers, **kwargs):
    return r.resolve(
        roots=[{"name": name, "range": spec} for name, spec in roots],
        manifests=manifests, providers=providers, contracts=kwargs.pop("contracts", CONTRACTS),
        **kwargs,
    )


def _shared_world():
    geo = provider("noesis.geo", stores=["geometry"],
                   capabilities=[capability("spatial.relation"), capability("spatial.store", effect="local-mutation", record_kinds=["geometry"])])
    base = provider("noesis.search", capabilities=[capability("scholarly.lookup")])
    geospatial = manifest("geospatial", providers=[("noesis.geo", "1.0.0")])
    osint = manifest("osint", requires=[req("spatial.relation")])
    research = manifest("research", requires=[req("spatial.relation"), req("scholarly.lookup"),
                                              req("spatial.store", feature="capture")],
                        features=["capture"])
    return [geospatial, osint, research], [geo, base]


# --------------------------------------------------------------------------- #
# C03.1 closure, optional features, cycles, purity
# --------------------------------------------------------------------------- #

def test_transitive_closure_pulls_in_the_contributing_pack():
    manifests, providers = _shared_world()
    plan = _resolve([("osint", "^1.0.0")], manifests, providers)["plan"]
    assert {m["name"] for m in plan["manifests"]} == {"osint", "geospatial"}
    kinds = {(e["from"], e["to"], e["kind"]) for e in plan["edges"]}
    assert ("osint@1.0.0", "noesis.geo@1.0.0", "binds") in kinds
    assert ("geospatial@1.0.0", "noesis.geo@1.0.0", "contributes") in kinds


def test_graph_and_digest_are_identical_for_any_input_ordering():
    manifests, providers = _shared_world()
    roots = [("research", "^1.0.0"), ("osint", "^1.0.0")]
    reference = _resolve(roots, manifests, providers, features={"research": ["capture"]})["plan"]
    rng = random.Random(1788)
    for _ in range(25):
        m, p, ro = list(manifests), list(providers), list(roots)
        rng.shuffle(m), rng.shuffle(p), rng.shuffle(ro)
        plan = _resolve(ro, m, p, features={"research": ["capture"]})["plan"]
        assert c.canonical(plan) == c.canonical(reference)
    assert c.validate_plan(reference)["digest"] == reference["digest"]


def test_cycle_names_every_pack_in_the_cycle():
    pa = provider("example.a", capabilities=[capability("a.cap")])
    pb = provider("example.b", capabilities=[capability("b.cap")])
    pc = provider("example.c", capabilities=[capability("c.cap")])
    a = manifest("alpha", requires=[req("b.cap")], providers=[("example.a", "1.0.0")])
    b = manifest("beta", requires=[req("c.cap")], providers=[("example.b", "1.0.0")])
    g = manifest("gamma", requires=[req("a.cap")], providers=[("example.c", "1.0.0")])
    with pytest.raises(c.CompositionError) as caught:
        _resolve([("alpha", "^1.0.0")], [a, b, g], [pa, pb, pc])
    assert caught.value.code == "dependency_cycle"
    assert set(caught.value.details["cycle"]) == {"alpha@1.0.0", "beta@1.0.0", "gamma@1.0.0"}
    assert "alpha@1.0.0" in caught.value.message and "gamma@1.0.0" in caught.value.message


def test_optional_branches_are_omissions_not_failures():
    manifests, providers = _shared_world()
    plan = _resolve([("research", "^1.0.0")], manifests, providers)["plan"]
    assert {(o["capability"], o["code"]) for o in plan["omissions"]} == {
        ("spatial.store", "feature-not-selected")}
    without_store = [provider("noesis.geo", capabilities=[capability("spatial.relation")]), providers[1]]
    plan = _resolve([("research", "^1.0.0")], manifests, without_store,
                    features={"research": ["capture"]})["plan"]
    assert {(o["capability"], o["code"]) for o in plan["omissions"]} == {
        ("spatial.store", "unavailable-undeclared")}
    assert {b["capability"] for b in plan["bindings"]} == {"spatial.relation", "scholarly.lookup"}


def test_resolver_module_imports_nothing_that_performs_io():
    tree = ast.parse((ROOT / "src/composition/resolver.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported <= {"__future__", "collections.abc", "typing", "src.composition.contracts"}


def test_resolution_performs_no_file_or_network_access(monkeypatch):
    manifests, providers = _shared_world()

    def refuse(*_args, **_kwargs):
        raise AssertionError("resolver performed I/O")

    monkeypatch.setattr(builtins, "open", refuse)
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(Path, "read_text", refuse)
    result = _resolve([("osint", "^1.0.0"), ("research", "^1.0.0")], manifests, providers)
    assert result["status"] == "resolved"


# --------------------------------------------------------------------------- #
# C03.2 version rules and retained pins
# --------------------------------------------------------------------------- #

def test_retained_compatible_pin_survives_and_incompatible_pin_fails():
    old = provider("noesis.geo", "1.0.0")
    new = provider("noesis.geo", "1.1.0", capabilities=[capability("spatial.relation", "1.1.0")])
    consumer = manifest("osint", requires=[req("spatial.relation")])
    fresh = _resolve([("osint", "^1.0.0")], [consumer], [old, new])["plan"]
    assert fresh["bindings"][0]["provider_version"] == "1.1.0"
    retained = dict(fresh, bindings=[dict(fresh["bindings"][0], provider_version="1.0.0")])
    kept = _resolve([("osint", "^1.0.0")], [consumer], [old, new], retained=retained)["plan"]
    assert kept["bindings"][0]["provider_version"] == "1.0.0"
    assert kept["bindings"][0]["reason"] == "retained-pin"
    stricter = manifest("osint", "1.1.0", requires=[req("spatial.relation", "^1.1.0")])
    with pytest.raises(c.CompositionError) as caught:
        _resolve([("osint", "^1.0.0")], [stricter], [old, new],
                 retained={"bindings": retained["bindings"]})
    assert caught.value.code == "incompatible_retained_pin"
    assert "osint@1.1.0" in caught.value.message and "noesis.geo@1.0.0" in caught.value.message
    with pytest.raises(c.CompositionError) as caught:
        _resolve([("osint", "^1.0.0")], [stricter], [old, new], retained=retained)
    assert caught.value.code == "retained_pin_unavailable"


def test_retained_manifest_pin_is_preserved_or_rejected():
    v1 = manifest("osint", "1.0.0")
    v2 = manifest("osint", "1.2.0")
    plan = _resolve([("osint", "^1.0.0")], [v1, v2], [])["plan"]
    assert plan["roots"] == [{"name": "osint", "version": "1.2.0"}]
    retained = {"manifests": [{"name": "osint", "version": "1.0.0"}], "bindings": []}
    assert _resolve([("osint", "^1.0.0")], [v1, v2], [], retained=retained)["plan"]["roots"][0]["version"] == "1.0.0"
    with pytest.raises(c.CompositionError) as caught:
        _resolve([("osint", "~1.2.0")], [v1, v2], [], retained=retained)
    assert caught.value.code == "incompatible_retained_pin"


def test_conflicting_major_versions_of_one_provider_block_resolution():
    one = provider("noesis.geo", "1.0.0")
    two = provider("noesis.geo", "2.0.0")
    consumer = manifest("osint", requires=[req("spatial.relation", ">=1.0.0")])
    with pytest.raises(c.CompositionError) as caught:
        _resolve([("osint", "^1.0.0")], [consumer], [one, two])
    assert caught.value.code == "conflicting_major_versions"
    assert caught.value.details["provider_id"] == "noesis.geo"


def test_incompatible_ranges_and_missing_contracts_are_distinct_and_named():
    geo = provider("noesis.geo")
    wants_two = manifest("osint", requires=[req("spatial.relation", "^2.0.0")])
    with pytest.raises(c.CompositionError) as caught:
        _resolve([("osint", "^1.0.0")], [wants_two], [geo])
    assert caught.value.code == "incompatible_provider"
    assert "osint@1.0.0" in caught.value.message and "spatial.relation" in caught.value.message
    assert "1.0.0 is outside ^2.0.0" in caught.value.message
    missing = manifest("osint", contracts=[{"name": "nonexistent-contract", "range": "^1.0.0"}])
    with pytest.raises(c.CompositionError) as caught:
        _resolve([("osint", "^1.0.0")], [missing], [geo])
    assert caught.value.code == "missing_contract"
    too_new = manifest("osint", contracts=[{"name": "fixture-contract", "range": "^2.0.0"}])
    with pytest.raises(c.CompositionError) as caught:
        _resolve([("osint", "^1.0.0")], [too_new], [geo])
    assert caught.value.code == "incompatible_range"
    with pytest.raises(c.CompositionError) as caught:
        _resolve([("osint", "latest")], [too_new], [geo])
    assert caught.value.code == "floating_range"


def test_plan_locks_exact_versions_and_hashes():
    manifests, providers = _shared_world()
    plan = _resolve([("research", "^1.0.0")], manifests, providers,
                    source_packs={"geospatial-berlin": [{"version": "1.1.0", "manifest_hash": "h1"}]})["plan"]
    text = json.dumps(plan)
    assert "latest" not in text
    for item in plan["manifests"]:
        assert item["manifest_hash"].startswith("sha256:")
    for item in plan["providers"]:
        assert item["descriptor_hash"].startswith("sha256:")
        assert c.version_key(item["version"])


# --------------------------------------------------------------------------- #
# C03.3 bindings
# --------------------------------------------------------------------------- #

def test_ambiguity_is_a_result_naming_both_providers():
    a = provider("example.alpha-spatial")
    b = provider("example.beta-spatial")
    consumer = manifest("osint", requires=[req("spatial.relation")])
    for order in ([a, b], [b, a]):
        result = _resolve([("osint", "^1.0.0")], [consumer], order)
        assert result == {"status": "ambiguous", "ambiguities": [{
            "consumer": "osint@1.0.0", "capability": "spatial.relation",
            "providers": ["example.alpha-spatial@1.0.0", "example.beta-spatial@1.0.0"]}]}
    chosen = _resolve([("osint", "^1.0.0")], [consumer], [a, b],
                      provider_choices={"spatial.relation": "example.beta-spatial"})["plan"]
    assert chosen["bindings"][0]["provider_id"] == "example.beta-spatial"
    assert chosen["bindings"][0]["reason"] == "explicit"


def test_semantic_constraint_mismatch_is_rejected_with_the_constraint_named():
    planar = provider("example.planar", capabilities=[capability("spatial.relation", semantics={"algorithm": "planar-euclidean"})])
    consumer = manifest("osint", requires=[req("spatial.relation", semantics={"algorithm": "wgs84-stdlib-v1"})])
    with pytest.raises(c.CompositionError) as caught:
        _resolve([("osint", "^1.0.0")], [consumer], [planar])
    assert caught.value.code == "incompatible_provider"
    assert "semantic constraint algorithm='wgs84-stdlib-v1'" in caught.value.message


def test_conflicting_store_ownership_and_undeclared_binding():
    a = provider("example.a", capabilities=[capability("a.cap")], stores=["geometry"])
    b = provider("example.b", capabilities=[capability("b.cap")], stores=["geometry"])
    consumer = manifest("osint", requires=[req("a.cap"), req("b.cap")])
    with pytest.raises(c.CompositionError) as caught:
        _resolve([("osint", "^1.0.0")], [consumer], [a, b])
    assert caught.value.code == "conflicting_store_owner"
    assert caught.value.details["providers"] == ["example.a", "example.b"]
    lonely = manifest("osint", requires=[req("spatial.teleport")])
    with pytest.raises(c.CompositionError) as caught:
        _resolve([("osint", "^1.0.0")], [lonely], [a])
    assert caught.value.code == "undeclared_binding"


def test_bindings_record_reason_effect_and_contracts():
    manifests, providers = _shared_world()
    plan = _resolve([("osint", "^1.0.0")], manifests, providers)["plan"]
    (binding,) = plan["bindings"]
    assert binding["reason"] == "only-compatible"
    assert binding["effect"] == "read-only"
    assert binding["output_contract"] == {"name": "noesis-spatial-result-v1", "version": "1.0.0"}
    assert {"capability": "spatial.relation", "name": "noesis-spatial-result-v1", "version": "1.0.0"} in plan["output_contracts"]


# --------------------------------------------------------------------------- #
# C03.4 plans, resume and replay
# --------------------------------------------------------------------------- #

def test_resume_reports_current_changed_or_unavailable_without_rebinding():
    manifests, providers = _shared_world()
    plan = _resolve([("osint", "^1.0.0")], manifests, providers)["plan"]
    assert r.check_resume(plan, manifests=manifests, providers=providers, contracts=CONTRACTS)["status"] == "current"
    revised = provider("noesis.geo", stores=["geometry"], capabilities=[
        capability("spatial.relation", semantics={"algorithm": "wgs84-stdlib-v2"}),
        capability("spatial.store", effect="local-mutation", record_kinds=["geometry"])])
    changed = r.check_resume(plan, manifests=manifests, providers=[revised, providers[1]], contracts=CONTRACTS)
    assert changed["status"] == "new-plan-required"
    assert changed["changes"][0]["id"] == "noesis.geo@1.0.0"
    assert changed["changes"][0]["current_hash"] == revised["descriptor_hash"]
    gone = r.check_resume(plan, manifests=manifests, providers=[providers[1]], contracts=CONTRACTS)
    assert gone["status"] == "unavailable-for-replay"
    assert gone["unavailable"][0]["id"] == "noesis.geo@1.0.0"


def test_every_existing_pack_resolves_to_a_valid_plan():
    manifests = c.load_all_packs()
    providers = c.load_providers()
    contracts = c.contract_candidates()
    for item in manifests:
        result = r.resolve(roots=[{"name": item["name"], "range": item["version"]}],
                           manifests=manifests, providers=providers, contracts=contracts)
        assert result["status"] == "resolved", item["name"]
        c.validate_plan(result["plan"])
        overlay = (ROOT / "packs" / item["name"] / "composition.json").exists()
        expected = [] if overlay else [f"{item['name']}@{item['version']}"]
        assert [m for m in result["plan"]["conservative"] if m.startswith(item["name"] + "@")] == expected


RESOLUTION_CASES = sorted((ROOT / "tests/fixtures/composition/resolution").glob("*.json"))


@pytest.mark.parametrize("path", RESOLUTION_CASES, ids=lambda p: p.stem)
def test_resolution_fixture_corpus(path: Path):
    case = json.loads(path.read_text())
    kwargs = dict(roots=case["roots"], manifests=[c.validate_manifest(m) for m in case["manifests"]],
                  providers=[c.validate_provider(p) for p in case["providers"]],
                  contracts=c.contract_candidates(include_domain_packs=False),
                  features=case.get("features"), provider_choices=case.get("provider_choices"))
    if "expect_error" in case:
        with pytest.raises(c.CompositionError) as caught:
            r.resolve(**kwargs)
        assert caught.value.code == case["expect_error"]
    else:
        result = r.resolve(**kwargs)
        assert result["status"] == case["expect_status"]
        if result["status"] == "resolved":
            c.validate_plan(result["plan"])
