"""An entry as of a release with its cross-reference graph, and compounds with published activity against a
target (#2652, LS09 #2696, LS10 #2701)."""

from __future__ import annotations

import pytest

from src.kb.lifesci_links import LifeSciLinks
from src.kb.lifesci_queries import LifeSciQueries
from src.kb.lifesci_records import (
    INFERENCE_KEYS,
    LifeSciError,
    keys_in,
    personal_fields,
)
from tests.unit import lifesci_harness as h


@pytest.fixture(scope="module")
def queries():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    h.seed_all(conn)
    LifeSciLinks(conn).link_all(h.NS, scopes=h.SCOPES, principal_id="linker")
    return LifeSciQueries(conn, now=lambda: h.SECOND + 10)


def test_entry_as_of_a_release_selects_the_version_then_and_cites_each_version(queries):
    old = queries.entry(h.NS, "X9EXA1", scopes=h.READ_ONLY, release="2099_01")
    new = queries.entry(h.NS, "X9EXA1", scopes=h.READ_ONLY, release="2099_02")
    assert old["entry"]["version"]["marker"] == "entry-1" and new["entry"]["version"]["marker"] == "entry-2"
    assert old["entry"]["attributes"]["sequence_version"] == 1 and new["entry"]["attributes"]["sequence_version"] == 2
    assert [v["version_marker"] for v in old["history"]] == ["entry-1", "entry-2"]
    assert all(v["citation"]["revision_id"] and v["citation"]["source"] == "uniprot" for v in old["history"])
    cite = old["entry"]["citation"]
    assert cite["release"] == "2099_01" and cite["as_of"]["release"] == "2099_01" and cite["retrieved_at"]
    dated = queries.entry(h.NS, "X9EXA1", scopes=h.READ_ONLY, as_of="2099-02-01")
    assert dated["entry"]["version"]["marker"] == "entry-1"


def test_merged_and_obsolete_accessions_resolve_to_successors_with_history(queries):
    merged = queries.entry(h.NS, "X9EXA2", scopes=h.READ_ONLY)
    assert merged["entry"]["version"]["status"] == "merged"
    (successor,) = merged["resolved_to"]
    assert (successor["native_id"], successor["version_marker"], successor["via"]["kind"]) == ("X9EXA1", "entry-2",
                                                                                                "merged")
    assert [v["status"] for v in merged["history"]] == ["active", "merged"]
    before = queries.entry(h.NS, "X9EXA2", scopes=h.READ_ONLY, release="2099_01")
    assert before["entry"]["version"]["status"] == "active" and before["resolved_to"] == []
    obsolete = queries.entry(h.NS, "9EXB", scopes=h.READ_ONLY)
    assert obsolete["resolved_to"][0]["native_id"] == "9EXC"
    replaced = queries.entry(h.NS, "ncbi-gene:990000103", scopes=h.READ_ONLY)
    assert replaced["resolved_to"][0]["native_id"] == "990000101"


def test_cross_references_are_labelled_with_the_asserting_source(queries):
    answer = queries.entry(h.NS, "X9EXA1", scopes=h.READ_ONLY, release="2099_01")
    graph = answer["xref_graph"]
    outgoing = {(x["database"], x["id"]): x for x in graph["outgoing"]}
    assert outgoing[("PDB", "9EXA")]["asserted_by"] == "uniprot"
    assert outgoing[("PDB", "9EXA")]["resolution"] == "acquired"
    assert outgoing[("PDB", "9EXA")]["resolved"]["citation"]["source"] == "rcsb-pdb"
    assert outgoing[("NCBI Taxonomy", "99000001")]["relation"] == "organism"
    incoming = {(x["source"], x["native_id"]) for x in graph["incoming"]}
    assert incoming == {("rcsb-pdb", "9EXA"), ("rcsb-pdb", "9EXC"), ("chembl", "CHEMBL9900001")}
    assert all(x["asserted_by"] == x["source"] for x in graph["incoming"])
    assert {link["owner"] for link in graph["links"]} >= {"clinical", "literature"}


def test_compounds_for_a_target_are_published_values_side_by_side_never_aggregated(queries):
    answer = queries.compounds_for_target(h.NS, "X9EXA1", scopes=h.READ_ONLY, release="CHEMBL_99")
    assert answer["status"] == "answered" and answer["target_basis"].startswith("chembl-target-component")
    groups = {(g["assay_type"], g["activity_type"]): g["activities"] for g in answer["groups"]}
    assert set(groups) == {("B", "IC50"), ("B", "Ki"), ("F", "Inhibition")}
    (ki,) = groups[("B", "Ki")]
    assert ki["standard"]["relation"] == ">" and ki["standard"]["value"] == "10000" and ki["standard"]["units"] == "nM"
    assert ki["data_validity_comment"] is None
    assert groups[("F", "Inhibition")][0]["data_validity_comment"] == "Outside typical range"
    assert ki["compound"]["standard_inchi_key"] == "NORTHWINDCMPDB-UHFFFAOYSA-N"
    assert ki["document"]["native_id"] == "CHEMBL9900201" and ki["document"]["citation"]["revision_id"]
    assert ki["citation"]["source"] == "chembl" and ki["citation"]["release"] == "CHEMBL_99"
    assert keys_in(answer, INFERENCE_KEYS) == [] and personal_fields(answer) == []
    later = queries.compounds_for_target(h.NS, "CHEMBL9900001", scopes=h.READ_ONLY, release="CHEMBL_100")
    groups = {(g["assay_type"], g["activity_type"]): g["activities"] for g in later["groups"]}
    assert [a["activity_id"] for a in groups[("B", "IC50")]] == ["990000301", "990000304"]
    # 12.5 nM and 0.2 uM stay in their published units.
    assert {(a["standard"]["value"], a["standard"]["units"]) for a in groups[("B", "IC50")]} == {("12.5", "nM"),
                                                                                                 ("0.2", "uM")}
    assert groups[("B", "Ki")][0]["data_validity_comment"] == "Potential transcription error"


def test_subjects_without_records_and_bad_references(queries):
    assert queries.entry(h.NS, "X9ZZZ9", scopes=h.READ_ONLY)["status"] == "none_on_record"
    none = queries.compounds_for_target(h.NS, "X9NRW1", scopes=h.READ_ONLY, release="CHEMBL_99")
    assert none["status"] == "none_on_record" and "not evidence" in none["note"]
    ambiguous = queries.entry(h.NS, "99000001", scopes=h.READ_ONLY)
    assert ambiguous["status"] in {"answered", "ambiguous"}
    with pytest.raises(LifeSciError):
        queries.entry(h.NS, "not an accession", scopes=h.READ_ONLY)
    with pytest.raises(LifeSciError):
        queries.entry(h.NS, "X9EXA1", scopes={"knowledge:lifesci:read"})


def test_evidence_bundles_cite_source_revision_and_as_of_time(queries):
    answer = queries.entry(h.NS, "X9EXA1", scopes=h.READ_ONLY, release="2099_01")
    bundle = queries.export_bundle(answer, created_at_ms=h.SECOND)
    assert bundle["items"] and all(i["source"] and i["record_revision"] and i["as_of"] for i in bundle["items"])
    assert {i["source"] for i in bundle["items"]} >= {"uniprot", "rcsb-pdb", "chembl", "ncbi-gene", "ncbi-taxonomy"}
    target = queries.export_bundle(queries.compounds_for_target(h.NS, "CHEMBL9900001", scopes=h.READ_ONLY,
                                                                 release="CHEMBL_99"))
    assert {i["record_type"] for i in target["items"]} >= {"activity", "compound", "document", "target"}
