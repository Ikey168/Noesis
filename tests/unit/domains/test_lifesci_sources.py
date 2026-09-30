"""UniProt, NCBI Gene/Taxonomy, RCSB PDB and ChEMBL acquisition through the life-sciences connector, offline
(#2652, LS01 #2656, LS03 #2666, LS04 #2671, LS05 #2676, LS06 #2681)."""

from __future__ import annotations

import copy
import json

import pytest

from src.ingestion import lifesci_sources as ls
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.kb.lifesci_records import personal_fields
from tests.unit import lifesci_harness as h


def statements(provider, revision=False):
    return [r["lifesci_statement"] for records, _ in h.fetch(provider, revision=revision) for r in records]


def by_id(provider, revision=False):
    return {(s["record_type"], s["native_id"]): s for s in statements(provider, revision)}


def test_contracts_live_status_bounded_coverage_and_minimisation_are_recorded():
    assert set(ls.PROVIDER_CONTRACTS) == set(h.SOURCES)
    for provider, contract in ls.PROVIDER_CONTRACTS.items():
        for field in ("entry_points", "authentication", "licence", "rate_limits", "revision_model",
                      "corrections_and_removals", "personal_data"):
            assert contract[field], (provider, field)
        assert ls.LIVE_VERIFICATION[provider]["status"] == "unverified-live"
    assert "NOESIS_NCBI_API_KEY" in ls.PROVIDER_CONTRACTS["ncbi-gene"]["authentication"]
    assert set(ls.PERSONAL_DATA_DECISION) >= {"stored", "excluded", "enforcement", "retention", "access"}
    manifest = h.manifest()
    ours = [s for s in manifest["sources"] if s["connector"] == "life-sciences"]
    assert {s["life_sciences"]["live_verification"] for s in ours} == {"unverified-live"}
    audit = (h.ROOT / "docs/development/life-sciences-evidence/source-audit.md").read_text()
    assert "not re-verified live" in audit and "Data minimisation" in audit


def test_pinned_fixtures_replay_to_their_recorded_output_hashes():
    manifest = h.manifest()
    ours = [s for s in manifest["sources"] if s["connector"] == "life-sciences"]
    report = SourcePackConformance(h.ROOT).offline({**manifest, "sources": ours})
    assert report["valid"] and report["coverage"]["verified"] == 5


def test_uniprot_keeps_versions_labels_secondary_accessions_and_merges():
    first = by_id("uniprot")
    exa1 = first[("protein", "X9EXA1")]
    assert exa1["version"] == {"marker": "entry-1", "basis": "entry-version", "order": [1], "date": "2099-01-10"}
    assert exa1["release"] == {"label": "2099_01", "published_on": "2099-01-12", "basis": "provider"}
    assert exa1["attributes"]["reviewed"] == "reviewed"
    assert first[("protein", "X9NRW1")]["attributes"]["reviewed"] == "unreviewed"
    assert first[("protein", "X9NRW1")]["attributes"]["entry_type"] == "UniProtKB unreviewed (TrEMBL)"
    assert {(x["database"], x["id"]) for x in exa1["xrefs"]} >= {("PDB", "9EXA"), ("GeneID", "990000101"),
                                                                 ("ChEMBL", "CHEMBL9900001")}
    assert {c["kind"] for c in exa1["citations"]} == {"pubmed", "doi"}
    assert exa1["attributes"]["sequence"]["value"] and "references[].citation.authors" in exa1["excluded_fields"]
    second = by_id("uniprot", revision=True)
    assert second[("protein", "X9EXA1")]["attributes"]["sequence_version"] == 2
    assert second[("protein", "X9EXA1")]["attributes"]["secondary_accessions"] == ["X9EXA2"]
    merged = second[("protein", "X9EXA2")]
    assert (merged["status"], merged["successors"], merged["status_published"]) == ("merged", ["X9EXA1"],
                                                                                    "Inactive:MERGED")


def test_ncbi_gene_and_taxonomy_keep_status_successors_and_lineage():
    genes = by_id("ncbi-gene", revision=True)
    replaced = genes[("gene", "990000103")]
    assert (replaced["status"], replaced["successors"], replaced["status_published"]) == ("replaced",
                                                                                          ["990000101"], "1")
    assert genes[("gene", "990000101")]["xrefs"] == [{"database": "NCBI Taxonomy", "id": "99000001",
                                                      "relation": "organism"}]
    taxa = by_id("ncbi-taxonomy")
    species = taxa[("taxon", "99000001")]["attributes"]
    assert species["rank"] == "species" and species["lineage"] == "cellular organisms; Eukaryota; Exampla"
    assert [t["tax_id"] for t in species["lineage_ex"]] == ["131567", "2759", "99000000"]
    assert taxa[("taxon", "99000009")]["status"] == "merged"
    assert taxa[("taxon", "99000009")]["successors"] == ["99000001"]


def test_pdb_keeps_method_resolution_revision_history_entity_mappings_and_obsoletion():
    first = by_id("rcsb-pdb")
    exa = first[("structure", "9EXA")]
    assert exa["attributes"]["methods"] == ["X-RAY DIFFRACTION"] and exa["attributes"]["resolution"] == ["1.8"]
    assert exa["attributes"]["entities"][0]["uniprot_accessions"] == ["X9EXA1"]
    assert exa["xrefs"] == [{"database": "UniProt", "id": "X9EXA1", "relation": "entity-reference",
                             "properties": {"entity_id": "1"}}]
    obsolete = first[("structure", "9EXB")]
    assert (obsolete["status"], obsolete["successors"]) == ("obsolete", ["9EXC"])
    second = by_id("rcsb-pdb", revision=True)[("structure", "9EXA")]
    assert second["version"]["marker"] == "rev-1.1" and len(second["attributes"]["revision_history"]) == 2


def test_chembl_keeps_release_values_relations_units_validity_and_document_citations():
    first = by_id("chembl")
    activity = first[("activity", "990000302")]["attributes"]
    assert activity["standard"] == {"type": "Ki", "relation": ">", "value": "10000", "units": "nM", "flag": 1}
    assert first[("activity", "990000303")]["attributes"]["data_validity_comment"] == "Outside typical range"
    assert first[("activity", "990000301")]["citations"] == [{"kind": "chembl-document", "id": "CHEMBL9900201"}]
    assert {s["release"]["label"] for s in first.values()} == {"CHEMBL_99"}
    compound = first[("compound", "CHEMBL9900101")]["attributes"]
    assert compound["structures"]["standard_inchi_key"] == h.EXAMPLINIB_KEY
    assert "max_phase" not in compound and "molecule_properties" not in compound
    document = first[("document", "CHEMBL9900201")]
    assert {c["kind"] for c in document["citations"]} == {"pubmed", "doi"} and "authors" in document["excluded_fields"]


def test_every_statement_is_free_of_personal_fields_even_though_the_fixtures_carry_them():
    raw = json.dumps([h.pages(p) for p in h.SOURCES])
    assert "Quell" in raw and "audit_author" in raw  # the fixtures carry names to prove they are dropped
    for provider in h.SOURCES:
        for statement in statements(provider):
            assert personal_fields(statement) == [] and "Quell" not in json.dumps(statement)


def test_chembl_release_mismatch_and_truncated_activity_lists_are_refused():
    source = h.source("chembl")
    with pytest.raises(SourcePackError) as exc:
        h.fetch("chembl", transport=ls.fixture_transport(h.pages("chembl", revision=True) + h.pages("chembl")[1:]))
    assert "release_mismatch" in str(exc.value)
    pages = copy.deepcopy(h.pages("chembl"))
    for page in pages:
        if "activity.json" in page["request"]:
            body = json.loads(page["body"])
            body["page_meta"]["total_count"] = 999
            page["body"] = json.dumps(body)
    adapter = ls.LifeSciAdapter(source, transport=ls.fixture_transport(pages), today=lambda: ls.FIXTURE_DAY)
    with pytest.raises(SourcePackError) as exc:
        adapter.fetch_page({"operation": "release", "parameters": {}, "limit": 50}, cursor="2")
    assert exc.value.code == "budget_exhausted"


def test_ncbi_api_key_is_sent_only_as_a_parameter_and_never_recorded():
    seen = []
    replay = ls.fixture_transport(h.pages("ncbi-gene"))

    def transport(**kwargs):
        seen.append(dict(kwargs["params"]))
        return replay(**kwargs)

    adapter = ls.LifeSciAdapter(h.source("ncbi-gene"), transport=transport, secret="secret-key-value",
                                today=lambda: ls.FIXTURE_DAY)
    page = adapter.fetch_page({"operation": "release", "parameters": {}, "limit": 50}, cursor=None)
    assert seen[0]["api_key"] == "secret-key-value"
    assert "secret-key-value" not in json.dumps([dict(r) for r in page.records]) + json.dumps(page.receipt)
    assert page.receipt["api_key_used"] is True and adapter.describe()["life_sciences"]["api_key"] == "configured"
    uniprot = ls.LifeSciAdapter(h.source("uniprot"), transport=replay, secret="x")
    assert uniprot.describe()["life_sciences"]["api_key"] == "absent"


def test_rate_limits_hosts_and_declarations_are_enforced():
    def limited(**_):
        return {"status": 429, "headers": {"Retry-After": "30"}, "content": b"", "origin": "fixture"}

    adapter = ls.LifeSciAdapter(h.source("uniprot"), transport=limited)
    with pytest.raises(SourcePackError) as exc:
        adapter.fetch_page({"operation": "release", "parameters": {}, "limit": 50}, cursor=None)
    assert exc.value.code == "rate_limited"
    bad = h.source("uniprot")
    bad["endpoint"] = "https://mirror.example.org"
    with pytest.raises(SourcePackError, match="documented"):
        ls.LifeSciAdapter(bad)
    unbounded = h.source("uniprot")
    unbounded["life_sciences"]["documents"][0]["accessions"] = [f"X9EXA{i}" for i in range(1, 10)] * 3
    with pytest.raises(SourcePackError):
        ls.LifeSciAdapter(unbounded)
    with pytest.raises(SourcePackError) as exc:
        ls.LifeSciAdapter(h.source("uniprot")).fetch_page({"operation": "release", "parameters": {"q": "*"}},
                                                          cursor=None)
    assert exc.value.code == "parameter_forbidden"


def test_pages_carry_receipts_and_fixture_evidence_origin():
    for provider in h.SOURCES:
        for records, receipt in h.fetch(provider):
            assert receipt["evidence_origin"] == "fixture" and receipt["requests"]
            assert all(r["lifesci_page"]["live_verification"] == "unverified-live" for r in records)
