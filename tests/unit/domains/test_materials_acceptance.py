"""Offline material-to-property-dossier acceptance (MT13, #2091).

Pinned, authored, fictional fixtures replay through the real source-pack
runtime and ``materials`` adapter with sockets blocked: rutile TiO2 in
Materials Project, JARVIS-DFT and OQMD (computed, different functionals), its
COD experimental structure, the anatase polymorph that must never merge, and
a WebBook species with measured and evaluated values at two temperatures.
The journey runs ingestion -> unit normalisation -> identity review ->
comparison (aligned vs not comparable) -> property-range search -> citation
links -> release diff -> restart replay, once with ``pint`` importable and
once with it blocked (the unit path never uses it).
"""

from __future__ import annotations

import builtins
import socket
import sys

import pytest

from src.kb.materials_comparison import MaterialsComparison
from src.kb.materials_identity import MaterialsIdentity, phase_groups
from src.kb.materials_queries import MaterialsQueries
from src.kb.materials_releases import release_changes
from src.kb.materials_store import MaterialsStore, record_key
from tests.unit.materials import harness as h
from tests.unit.materials.test_materials_citations import seed_paper, seed_standard

REVIEW = h.SCOPES | {
    "knowledge:ownership:read",
    "knowledge:ownership:write",
    "knowledge:ownership:review",
}
STANDARDS = h.SCOPES | {"knowledge:standards:read", "namespace:global:read"}
MP_RUTILE = record_key("materials-project", "mp-990001")
RUTILE = {
    MP_RUTILE,
    record_key("jarvis-dft", "JVASP-990101"),
    record_key("oqmd", "99000301"),
    record_key("cod", "9990001"),
}
ANATASE = {
    record_key("materials-project", "mp-990002"),
    record_key("jarvis-dft", "JVASP-990102"),
    record_key("oqmd", "99000302"),
    record_key("cod", "9990002"),
}
WEBBOOK = record_key("nist-webbook", "C9990001")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the offline journey must not open a socket")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(params=["pint-available", "pint-absent"])
def pint_mode(request, monkeypatch):
    if request.param == "pint-absent":
        real_import = builtins.__import__

        def blocked(name, *args, **kwargs):
            if name == "pint" or name.startswith("pint."):
                raise ImportError("pint is not installed in this lane")
            return real_import(name, *args, **kwargs)

        monkeypatch.setitem(sys.modules, "pint", None)
        monkeypatch.setattr(builtins, "__import__", blocked)
    return request.param


def test_material_to_cited_property_dossier_comparison_search_citations_and_release_diff(
    pint_mode,
):
    # 1. ingestion through the runtime, every source from its pinned fixture
    env = h.Env().load()
    store = MaterialsStore(env.conn, initialize=False)
    assert {e["provider"] for e in store.entries(h.NS)} == set(h.SOURCES)

    # 2. unit normalisation: exact, native kept, per-formula-unit -> per-atom explicit
    dossier = MaterialsQueries(env.conn).properties(h.NS, WEBBOOK, scopes=h.SCOPES)
    (enthalpy,) = [
        p
        for p in dossier["properties"]
        if p["property"]["property"] == "standard_enthalpy_of_formation"
    ]
    evaluated = next(v for v in enthalpy["values"] if v["method_class"] == "evaluated")
    assert (evaluated["value"], evaluated["unit"]) == ("-944.0", "kJ/mol")
    assert (
        evaluated["normalized"]["normalized_unit"] == "eV/atom"
        and evaluated["normalized"]["unit_status"] == "normalized"
    )
    assert evaluated["conditions"] == {
        "temperature": "298.15 K",
        "pressure": "100000 Pa",
        "phase": "solid",
        "orientation": "unstated",
    }
    assert evaluated["uncertainty"]["value"] == "0.8"

    # 3. identity review: stated cross-references and structures; the polymorph is never proposed
    identity = MaterialsIdentity(env.conn)
    proposed = identity.propose(h.NS, principal_id="alice", scopes=REVIEW)
    for candidate in proposed["candidates"]:
        pair = {candidate["left_key"], candidate["right_key"]}
        assert pair <= RUTILE or pair <= ANATASE
        assert candidate["state"] == "proposed"
    assert any(
        set(p["records"]) & RUTILE and set(p["records"]) & ANATASE
        for p in proposed["not_proposed_polymorphs"]
    )
    for candidate in proposed["candidates"]:
        if {candidate["left_key"], candidate["right_key"]} <= RUTILE:
            identity.review(
                h.NS,
                candidate["candidate_id"],
                "accept",
                "stated cross-reference / equal structure",
                principal_id="bob",
                scopes=REVIEW,
            )
    groups = phase_groups(env.conn, h.NS)
    assert len({groups[key] for key in RUTILE}) == 1 and not set(groups) & ANATASE

    # 4. comparison: aligned computed values with labelled functionals; measured structure-only COD adds none;
    #    the anatase values and the WebBook enthalpies are never aligned with rutile
    comparison = MaterialsComparison(env.conn).compare(
        h.NS, MP_RUTILE, "formation_energy_per_atom", scopes=h.SCOPES
    )
    (aligned,) = comparison["aligned"]
    assert (
        aligned["method_class"] == "computed" and aligned["mixed_functionals"] is True
    )
    assert set(aligned["functionals"]) == {"GGA/GGA+U", "r2SCAN", "OptB88vdW", "PBE"}
    assert {v["record_key"] for v in aligned["values"]} <= RUTILE
    by_formula = MaterialsComparison(env.conn).compare(
        h.NS, "TiO2", "band_gap", scopes=h.SCOPES
    )
    for group in by_formula["aligned"]:
        records = {v["record_key"] for v in group["values"]}
        assert records <= RUTILE or len(records) == 1
    assert any(
        item["value"]["record_key"] in ANATASE for item in by_formula["not_comparable"]
    )
    enthalpies = MaterialsComparison(env.conn).compare(
        h.NS, WEBBOOK, "standard_enthalpy_of_formation", scopes=h.SCOPES
    )
    assert enthalpies["aligned"] == [] and len(enthalpies["not_comparable"]) == 2
    heat = MaterialsComparison(env.conn).compare(
        h.NS, WEBBOOK, "heat_capacity_cp", scopes=h.SCOPES
    )
    assert heat["aligned"] == [] and isinstance(heat["n"], int)

    # 5. bounded property-range search with stated semantics
    search = MaterialsComparison(env.conn).search(
        h.NS,
        [{"property": "band_gap", "min": "1.7", "max": "1.9", "unit": "eV"}],
        method_class="computed",
        scopes=h.SCOPES,
    )
    assert {hit["record_key"] for hit in search["hits"]} == RUTILE - {
        record_key("cod", "9990001")
    }
    assert "absence is not evidence" in search["assumptions"][0]

    # 6. citation links by DOI and designation only
    document_id = seed_paper(env.conn, "10.99999/fict.webbook.001")
    seed_standard(env.conn, "ASTM E1269")
    cited = MaterialsQueries(env.conn).properties(
        h.NS, WEBBOOK, scopes=STANDARDS, standards_namespace="global"
    )
    resolutions = [
        c["resolution"]
        for p in cited["properties"]
        for v in p["values"]
        for c in v["citations"]["value_level"]
    ]
    assert {
        "kind": "paper",
        "status": "linked",
        "basis": "equal DOI",
        "document_id": document_id,
        "revision_id": next(
            r["revision_id"] for r in resolutions if r.get("document_id") == document_id
        ),
    } in resolutions
    assert any(r["kind"] == "standard" and r["status"] == "linked" for r in resolutions)
    assert any(r["kind"] == "reference-string" for r in resolutions)
    assert cited["datasets"][0]["citations"][0]["level"] == "dataset"

    # 7. release diff with both values cited
    env.mp_release_2()
    diff = release_changes(env.conn, h.NS, "materials-project", scopes=h.SCOPES)
    (changed,) = [c for c in diff["changes"] if c["change"] == "changed"]
    assert (changed["before"]["value"], changed["after"]["value"]) == ("1.780", "1.800")
    assert (
        changed["before"]["release"] == "2099.1.0"
        and changed["after"]["release"] == "2099.2.0"
    )

    # 8. a restart replaying every source adds nothing
    before = store.snapshot_id(h.NS)
    restarted = h.Env(env.conn, start_ms=env.clock + 1_000)
    restarted.run(
        [s for p, s in h.SOURCES.items() if p != "materials-project"], "restart-replay"
    )
    restarted.mp_release_2("mp-2099.2.0-replay")
    assert store.snapshot_id(h.NS) == before
