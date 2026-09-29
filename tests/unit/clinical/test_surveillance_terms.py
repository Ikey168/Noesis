"""Surveillance conditions aligned to MeSH and ICD through the clinical term crosswalks (I06)."""

from __future__ import annotations

import pytest

from src.kb.clinical_terms import (
    MESH_MODULE,
    ClinicalTerms,
    icd_module,
    surveillance_icd_module,
    surveillance_mesh_module,
)
from src.kb.ontology import OntologyAlignmentStore
from tests.unit.clinical import surveillance_harness as h


@pytest.fixture(scope="module")
def aligned():
    env = h.Env()
    env.load_all()
    return env, h.align_terms(env)


def kinds(mappings):
    return {(m["source"], m["target"], m["kind"]) for m in mappings}


def test_icd_is_a_versioned_module_beside_mesh_in_the_same_ontology_store(aligned):
    env, result = aligned
    icd = result["icd"]
    assert (
        icd["icd"]["name"] == icd_module("who") == "clinical-icd10.who"
        and icd["icd"]["version"] == "2019.0.0"
    )
    assert kinds(icd["mappings"]) == {
        ("A15-A19", "D014376", "equivalent"),
        ("A15", "D014397", "narrower"),
        ("A31.0", "D014376", "incompatible"),
    }
    ontology = OntologyAlignmentStore(env.conn, initialize=False)
    names = {
        m["name"]
        for m in ontology.registry.export(scopes={"knowledge:schema:read"})["modules"]
    }
    assert {MESH_MODULE, "clinical-icd10.who", "clinical-icd10-mesh.who"} <= names
    tables = {
        r[0]
        for r in env.conn.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }
    assert not [
        t for t in tables if "icd" in t or "mesh" in t
    ]  # no second ontology or term store


def test_source_terms_are_crosswalked_with_kinds_preserved_and_unmapped_terms_listed(
    aligned,
):
    _, result = aligned
    surveillance = result["surveillance"]
    assert kinds(surveillance["mappings"]["mesh"]) >= {
        ("sv:ecdc-health-topic:Tuberculosis", "D014376", "equivalent"),
        ("sv:eurostat-icd10:A15-A19_B90", "D014376", "equivalent"),
        ("sv:gho-indicator:NOE_TB_INC_EST", "D014376", "equivalent"),
        ("sv:rki-meldekategorie:Tuberkulose", "D014376", "equivalent"),
        ("sv:ecdc-health-topic:Legionnaires' disease", "D014376", "incompatible"),
    }
    rki = next(
        m
        for m in surveillance["mappings"]["mesh"]
        if m["source"] == "sv:rki-meldekategorie:Tuberkulose"
    )
    assert (
        rki["evidence"][0]["rule"] == "curation"
    )  # a German name is never a label match
    assert kinds(surveillance["mappings"]["icd"]) == {
        ("sv:destatis-icd10:A15-A19", "A15-A19", "equivalent"),
        ("sv:eurostat-icd10:A15-A19_B90", "A15-A19", "narrower"),
    }
    assert [u["concept_id"] for u in surveillance["unmapped"]] == [
        "sv:ecdc-health-topic:Legionnaires' disease"
    ]
    assert surveillance["mesh_crosswalk"]["name"] == surveillance_mesh_module(h.NS)
    assert surveillance["icd_crosswalk"]["name"] == surveillance_icd_module(h.NS)


def test_expansion_finds_every_sources_series_with_explained_steps(aligned):
    env, _ = aligned
    expansion = ClinicalTerms(env.conn, initialize=False).expand_surveillance(
        h.NS, "tuberculosis", scopes=h.READ_ONLY
    )
    assert expansion["mesh_ids"] == ["D014376", "D014397"]
    assert {"A15-A19", "A15", "A19"} <= set(
        expansion["icd_codes"]
    ) and "A31.0" not in expansion["icd_codes"]
    providers = {s["provider"] for s in expansion["series"]}
    assert providers == {
        "rki-open-data",
        "who-gho",
        "eurostat-health",
        "destatis-health",
        "ecdc-atlas",
    }
    assert all(s["steps"] for s in expansion["series"])
    destatis = next(
        s for s in expansion["series"] if s["provider"] == "destatis-health"
    )
    assert "published-icd-code" in destatis["steps"][0]
    # Kinds stay distinguished; the incompatible term is blocked and the unmapped one is a gap.
    assert {s["kind"] for s in expansion["series"] if s["provider"] == "who-gho"} == {
        "estimate",
        "observation",
    }
    assert [b["concept_id"] for b in expansion["blocked"]] == [
        "sv:ecdc-health-topic:Legionnaires' disease"
    ]
    assert [g["concept_id"] for g in expansion["unmapped_terms"]] == [
        "sv:ecdc-health-topic:Legionnaires' disease"
    ]
    assert not [
        s
        for s in expansion["series"]
        if s["condition"]["code"] == "Legionnaires' disease"
    ]


def test_a_source_native_name_starts_the_expansion_when_no_vocabulary_names_it(aligned):
    env, _ = aligned
    expansion = ClinicalTerms(env.conn, initialize=False).expand_surveillance(
        h.NS, "Tuberkulose", scopes=h.READ_ONLY
    )
    assert "D014376" in expansion["mesh_ids"]
    assert {s["provider"] for s in expansion["series"]} >= {
        "rki-open-data",
        "eurostat-health",
        "destatis-health",
    }


def test_realignment_is_idempotent_and_needs_write_scopes(aligned):
    env, first = aligned
    again = h.align_terms(env)
    assert (
        again["surveillance"]["terms_module"] == first["surveillance"]["terms_module"]
    )
    with pytest.raises(Exception) as caught:
        ClinicalTerms(env.conn, initialize=False).align_surveillance(
            h.NS, principal_id="x", scopes=h.READ_ONLY, mesh_version="2026.1.0"
        )
    assert getattr(caught.value, "code", "") == "unauthorized"


def test_an_icd_scope_change_is_recorded_on_the_case_definition_revision(aligned):
    env, _ = aligned
    (series,) = [
        s for s in env.series(provider="rki-open-data", geography_code="09184")
    ]
    history = env.store().definition_history(h.NS, series["definition_key"])
    assert [r["content"]["icd_scope"] for r in history["revisions"]] == [
        ["A15-A19"],
        ["A15-A19", "A31.0"],
    ]
    # Earlier values are not relabelled: their condition code is the source's own.
    assert series["condition"]["code"] == "Tuberkulose"
