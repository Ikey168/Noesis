"""Materials Project, JARVIS-DFT, OQMD, NIST WebBook and COD adapters through the runtime (MT04-MT07)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.materials_sources import (
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    MaterialsAdapter,
    esd,
    fixture_transport,
    replay_native_fixture,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.kb.materials_store import MaterialsStore
from tests.unit.materials import fixture_builder as fb
from tests.unit.materials import harness as h


@pytest.fixture(scope="module")
def env():
    return h.Env().load()


def values(env, provider, native_id, prop=None):
    store = MaterialsStore(env.conn, initialize=False)
    (entry,) = [
        e for e in store.entries(h.NS, provider=provider) if e["native_id"] == native_id
    ]
    return [
        v
        for v in store.current_values(h.NS, entry_ids=[entry["entry_id"]])
        if prop in (None, v["property"])
    ]


def test_pinned_fixtures_replay_deterministically_through_the_real_adapter():
    result = SourcePackConformance(h.ROOT).offline(h.manifest())
    assert (
        result["valid"]
        and result["coverage"]["configured"] == result["coverage"]["verified"] == 5
    )


def test_every_source_runs_through_the_runtime_and_projects(env):
    store = MaterialsStore(env.conn, initialize=False)
    providers = {e["provider"] for e in store.entries(h.NS)}
    assert providers == set(h.SOURCES)
    for provider in h.SOURCES:
        assert store.provider_state(h.NS, provider)["last_execution"] == "source-pack"
    value = values(env, "materials-project", "mp-990001", "band_gap")[0]
    assert value["document_id"].startswith(
        "spdoc:"
    )  # every value cites the runtime document it was read from


def test_materials_project_values_carry_functional_mixing_scheme_and_release(env):
    energies = values(
        env, "materials-project", "mp-990001", "formation_energy_per_atom"
    )
    assert sorted((v["method"]["functional"], v["value"]) for v in energies) == [
        ("GGA/GGA+U", "-3.510"),
        ("r2SCAN", "-3.620"),
    ]
    assert all(
        v["method_class"] == "computed" and v["release"]["label"] == "2099.1.0"
        for v in energies
    )
    assert all(v["release"]["basis"] == "provider-stated" for v in energies)
    (bulk,) = values(env, "materials-project", "mp-990001", "bulk_modulus")
    assert (
        bulk["conditions"]["orientation"]["value"]
        == "polycrystalline Voigt-Reuss-Hill average"
    )
    entry = MaterialsStore(env.conn, initialize=False).entry(h.NS, bulk["entry_id"])
    structure = entry["content"]["structure"]
    assert (
        structure["space_group"] == {"number": 136, "symbol": "P4_2/mnm"}
        and structure["method_class"] == "computed"
    )
    assert "sites" not in structure  # no structure-file mirroring
    assert (
        entry["content"]["references"][0]["doi"] == "10.1063/1.4812323"
    )  # dataset-level attribution


def test_jarvis_keeps_functionals_apart_and_skips_na_and_other_systems(env):
    gaps = values(env, "jarvis-dft", "JVASP-990101", "band_gap")
    assert sorted(v["method"]["functional"] for v in gaps) == ["OptB88vdW", "TBmBJ"]
    assert all(v["method"]["functional"] != "DFT" for v in gaps)
    assert not values(
        env, "jarvis-dft", "JVASP-990101", "shear_modulus"
    )  # "na" is missing, never zero
    store = MaterialsStore(env.conn, initialize=False)
    assert "JVASP-990199" not in {
        e["native_id"] for e in store.entries(h.NS, provider="jarvis-dft")
    }
    entry = store.entry(h.NS, gaps[0]["entry_id"])
    assert (
        entry["content"]["structure"]["volume"]["value"] == "63.942912000"
    )  # exact determinant of the lattice


def test_oqmd_entries_are_pbe_with_their_fit_and_icsd_reference(env):
    (energy,) = values(env, "oqmd", "99000301", "formation_energy_per_atom")
    assert (
        energy["method"]["functional"] == "PBE"
        and energy["method"]["mixing_scheme"] == "fit:standard"
    )
    entry = MaterialsStore(env.conn, initialize=False).entry(h.NS, energy["entry_id"])
    assert entry["content"]["identifiers"] == {"icsd": ["99001"]}


def test_webbook_rows_are_measured_or_evaluated_with_conditions_uncertainty_and_references(
    env,
):
    enthalpies = values(
        env, "nist-webbook", "C9990001", "standard_enthalpy_of_formation"
    )
    by_class = {v["method_class"]: v for v in enthalpies}
    assert set(by_class) == {"measured", "evaluated"}
    evaluated = by_class["evaluated"]
    assert evaluated["uncertainty"] == {"value": "0.8", "kind": "± as published"}
    assert evaluated["conditions"]["temperature"]["normalized_value"] == "298.15"
    assert evaluated["conditions"]["pressure"]["normalized_value"] == "100000"
    assert evaluated["references"][0]["doi"] == "10.99999/fict.webbook.001"
    assert (
        evaluated["normalized"]["normalized_unit"] == "eV/atom"
    )  # kJ/mol per formula unit -> per atom (3 atoms)
    heat = values(env, "nist-webbook", "C9990001", "heat_capacity_cp")
    assert sorted(v["conditions"]["temperature"]["normalized_value"] for v in heat) == [
        "298.15",
        "500",
    ]
    assert all(
        any(r.get("standard") == "ASTM E1269" for r in v["references"]) for v in heat
    )
    (fusion,) = values(env, "nist-webbook", "C9990001", "fusion_temperature")
    assert fusion["method"] == {"class": "measured", "technique": "unstated"}
    assert "doi" not in fusion["references"][0]  # kept as a reference string


def test_cod_structures_are_measured_with_measurement_conditions_and_publication(env):
    store = MaterialsStore(env.conn, initialize=False)
    (cod,) = [
        e for e in store.entries(h.NS, provider="cod") if e["native_id"] == "9990001"
    ]
    entry = store.entry(h.NS, cod["entry_id"])
    structure = entry["content"]["structure"]
    assert (
        structure["method_class"] == "measured"
        and structure["lattice"]["a"] == "4.5937"
    )
    assert structure["measurement"]["temperature"]["normalized_value"] == "295"
    assert structure["measurement"]["pressure"]["normalized_value"] == "101325"
    assert (
        entry["release"]["label"] == "rev-990100"
        and entry["release"]["order_basis"] == "source release ordinal"
    )
    assert entry["content"]["references"][0]["doi"] == "10.99999/fict.cod.001"
    assert esd("4.5937(3)") == ("4.5937", "0.0003")


def test_reacquisition_is_idempotent(env):
    before = MaterialsStore(env.conn, initialize=False).snapshot_id(h.NS)
    env.run(list(h.SOURCES.values()), "materials-fixtures-again")
    assert MaterialsStore(env.conn, initialize=False).snapshot_id(h.NS) == before


def test_bounds_credentials_and_failures_fail_closed():
    mp = h.source(h.SOURCES["materials-project"])
    keyless = MaterialsAdapter(mp, transport=fixture_transport(fb.mp_pages()))
    with pytest.raises(SourcePackError) as missing:
        keyless.fetch_page({"operation": "properties", "parameters": {}}, cursor=None)
    assert missing.value.code == "authentication_failed"
    unbounded = json.loads(json.dumps(mp))
    unbounded["materials"]["max_records"] = 5000
    with pytest.raises(SourcePackError) as error:
        MaterialsAdapter(
            unbounded, transport=fixture_transport(fb.mp_pages()), secret="k"
        )
    assert error.value.code == "unbounded_source"
    failing = [dict(page, status=503) for page in fb.mp_pages()]
    adapter = MaterialsAdapter(mp, transport=fixture_transport(failing), secret="k")
    with pytest.raises(SourcePackError) as down:
        adapter.fetch_page({"operation": "properties", "parameters": {}}, cursor=None)
    assert down.value.code == "source_unavailable"


def test_a_failed_run_leaves_values_and_marks_the_provider_stale():
    env = h.Env().load()
    store = MaterialsStore(env.conn, initialize=False)
    before = store.snapshot_id(h.NS)
    failing = env.adapter(
        h.SOURCES["oqmd"], [dict(page, status=503) for page in fb.oqmd_pages()]
    )
    env.run([h.SOURCES["oqmd"]], "oqmd-down", adapters={h.SOURCES["oqmd"]: failing})
    assert (
        store.snapshot_id(h.NS) == before
        and store.provider_state(h.NS, "oqmd")["stale"] is True
    )


def test_contracts_and_live_state_are_declared_for_every_source():
    assert {
        "materials-project",
        "jarvis-dft",
        "oqmd",
        "nist-webbook",
        "cod",
        "nist-srd",
        "aflow",
        "nomad",
        "excluded",
    } <= set(PROVIDER_CONTRACTS)
    assert all(state["status"] == "unverified" for state in LIVE_VERIFICATION.values())
    records = replay_native_fixture(
        h.source(h.SOURCES["cod"]), {"native_pages": fb.cod_pages()}
    )
    assert "None" not in json.dumps(records)
