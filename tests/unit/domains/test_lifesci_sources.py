"""UniProt, NCBI, RCSB PDB and ChEMBL acquisition through the real adapter and runtime (#2652, LS01, LS03-LS06)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.lifesci_sources import (
    BOUNDED_COVERAGE,
    FIXTURE_SECRET,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    LifeSciSourceAdapter,
    fixture_transport,
    selection_entries,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from tests.unit import lifesci_harness as h


def fetch(provider: str, *, transport=None, secret=FIXTURE_SECRET):
    source = h.source(provider)
    adapter = LifeSciSourceAdapter(source, transport=transport or fixture_transport(h.pages(provider)), secret=secret)
    pages, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "reference", "parameters": {},
                                   "limit": source["budgets"]["max_results"]}, cursor=cursor)
        pages.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return pages


def statements(pages):
    return [r["lifesci_record"] for p in pages for r in p.records]


def test_audit_contracts_live_state_and_bounded_coverage_are_recorded_per_source():
    assert set(PROVIDER_CONTRACTS) == {"uniprot", "ncbi", "pdb", "chembl"}
    for contract in PROVIDER_CONTRACTS.values():
        for field in ("endpoints", "authentication", "licence", "redistribution", "rate_limits", "versioning",
                      "corrections_and_removals", "terms_url"):
            assert contract[field], field
    assert {v["status"] for v in LIVE_VERIFICATION.values()} == {"unverified-live"}
    assert "computed structure models (AlphaFold, ModelArchive)" in BOUNDED_COVERAGE["excluded"]
    manifest = json.loads((h.ROOT / "config/source_packs/scientific.json").read_text())
    declared = {s["source_id"]: s for s in manifest["sources"] if s.get("connector") == "life-sciences"}
    assert set(declared) == set(h.SOURCES.values())
    assert {s["life_sciences"]["live_verification"] for s in declared.values()} == {"unverified-live"}


def test_pinned_fixtures_replay_deterministically_through_the_conformance_runner():
    manifest = json.loads((h.ROOT / "config/source_packs/scientific.json").read_text())
    manifest["sources"] = [s for s in manifest["sources"] if s.get("connector") == "life-sciences"]
    report = SourcePackConformance(h.ROOT).offline(manifest)
    assert report["valid"], report["sources"]


def test_uniprot_entries_keep_versions_labels_release_and_merged_successors():
    pages = fetch("uniprot")
    by_key = {(s["record_type"], s["record_key"]): s for s in statements(pages)}
    entry = by_key[("protein", "P0DZZ1")]["as_published"]
    assert (entry["entry_version"], entry["sequence_version"], entry["release"]) == (20, 2, "2099_01")
    assert entry["reviewed"] is True and by_key[("protein", "A0AZZ1ZZZ2")]["as_published"]["reviewed"] is False
    assert {"database": "PDB", "id": "9ZZ2"}.items() <= entry["cross_references"][0].items()
    assert entry["citations"][0] == {"reference_number": 1, "pubmed_id": "99990011",
                                     "doi": "10.5555/fict.lifesci.2099.1",
                                     "title": "A fictional kinase from a fictional fungus.",
                                     "journal": "J. Fict. Biol.", "year": "2091"}
    merged = by_key[("protein", "Q9ZZZ1")]
    assert merged["effective"]["event"] == "obsoleted"
    assert merged["as_published"]["inactive_reason"] == {"type": "MERGED", "successors": ["P0DZZ1"]}
    versions = sorted(s["as_published"]["entry_version"] for s in statements(pages)
                      if s["record_type"] == "entry_version")
    assert versions == [18, 19, 20]
    receipt = pages[0].receipt
    assert receipt["personal_fields_dropped"] == ["uniprot:authors"]
    assert set(receipt["excluded_fields_dropped"]) == {"uniprot:comments", "uniprot:features", "uniprot:keywords"}
    assert receipt["live_verification"] == "unverified-live" and receipt["evidence_origin"] == "fixture"
    assert "Placeholder" not in json.dumps(statements(pages))


def test_ncbi_genes_and_taxa_keep_successors_lineage_and_the_key_stays_in_a_header():
    seen = []
    replay = fixture_transport(h.pages("ncbi"))

    def transport(**kwargs):
        seen.append(kwargs)
        return replay(**kwargs)

    pages = fetch("ncbi", transport=transport)
    by_key = {(s["record_type"], s["record_key"]): s for s in statements(pages)}
    assert by_key[("gene", "99990009")]["as_published"]["status"] == "replaced"
    assert by_key[("gene", "99990009")]["as_published"]["replaced_by"] == "99990001"
    taxon = by_key[("taxon", "999001")]["as_published"]
    assert [n["tax_id"] for n in taxon["lineage"]][:3] == ["1", "131567", "2759"] and taxon["rank"] == "species"
    assert by_key[("taxon", "999002")]["as_published"]["merged_into"] == "999001"
    assert all(call["headers"].get("api-key") == FIXTURE_SECRET for call in seen)
    assert FIXTURE_SECRET not in json.dumps([p.receipt for p in pages] + statements(pages))
    assert all("api_key" not in call["url"] and "api-key" not in call["params"] for call in seen)
    no_key = fetch("ncbi", secret=None)
    assert len(statements(no_key)) == len(statements(pages))


def test_pdb_structures_keep_revision_history_methods_resolution_mappings_and_obsolescence():
    pages = fetch("pdb")
    by_key = {s["record_key"]: s["as_published"] for s in statements(pages)}
    xray = by_key["9ZZ2"]
    assert xray["experimental_methods"] == ["X-RAY DIFFRACTION"] and xray["resolution_angstrom"] == ["2.1"]
    assert [(r["major"], r["minor"]) for r in xray["revision_history"]] == [(1, 0), (1, 1)]
    assert xray["entities"][0]["uniprot_accessions"] == ["P0DZZ1"]
    assert xray["primary_citation"]["doi"] == "10.5555/fict.lifesci.pdb.9zz2"
    assert by_key["9ZZ3"]["supersedes"] == ["9ZZ1"]
    assert by_key["9ZZ3"]["entities"][1]["uniprot_accessions"] == []
    assert by_key["9ZZ1"]["status"] == "obsolete" and by_key["9ZZ1"]["superseded_by"] == ["9ZZ3"]
    assert pages[0].receipt["personal_fields_dropped"] == ["pdb:audit_author", "pdb:rcsb_authors"]


def test_chembl_records_are_keyed_by_id_and_release_with_values_as_published_and_cited_documents():
    pages = fetch("chembl")
    items = statements(pages)
    assert {s["as_published"]["release"] for s in items} == {"ChEMBL_99"}
    activities = {s["record_key"]: s["as_published"] for s in items if s["record_type"] == "activity"}
    ki = activities["99990002"]
    assert ki["published"] == {"type": "Ki", "relation": ">", "value": "10", "units": "uM"}
    assert ki["standard"]["value"] == "10000.0" and ki["standard"]["units"] == "nM"
    assert ki["data_validity_comment"] == "Outside typical range"
    assert {a["document_chembl_id"] for a in activities.values()} == {
        s["record_key"] for s in items if s["record_type"] == "document"}
    compound = next(s for s in items if s["record_key"] == "CHEMBL9990101")["as_published"]
    assert compound["standard_inchikey"] == "ZZZZZZZZZZZZZA-UHFFFAOYSA-N"
    activity_page = pages[-1].receipt
    assert "activity:pchembl_value" in activity_page["excluded_fields_dropped"]
    assert "document:authors" in activity_page["personal_fields_dropped"]
    assert activity_page["snapshot"]["complete"] is True and activity_page["snapshot"]["release"] == "ChEMBL_99"
    assert "pchembl" not in json.dumps(items) and "Placeholder" not in json.dumps(items)


def test_selections_are_explicit_bounded_and_on_the_provider_host():
    source = h.source("chembl")
    source["life_sciences"]["selection"] = source["life_sciences"]["selection"][1:]
    with pytest.raises(SourcePackError):  # the release is read first
        selection_entries(source)
    source = h.source("chembl")
    source["life_sciences"]["selection"][-1]["limit"] = 1000
    with pytest.raises(SourcePackError):
        selection_entries(source)
    source = h.source("pdb")
    source["life_sciences"]["selection"] = [{"kind": "entry", "pdb_id": "AF_AFP0DZZ1F1"}]
    with pytest.raises(SourcePackError):  # computed structure models are excluded
        selection_entries(source)
    source = h.source("uniprot")
    source["endpoint"] = "https://mirror.example.org"
    with pytest.raises(SourcePackError):
        selection_entries(source)
    adapter = LifeSciSourceAdapter(h.source("uniprot"), transport=fixture_transport([]))
    page = adapter.fetch_page({"operation": "reference", "parameters": {}, "limit": 25}, cursor=None)
    assert page.receipt["outcome"] == "not_found" and page.records == ()
    with pytest.raises(SourcePackError):
        adapter.fetch_page({"operation": "reference", "parameters": {"accession": "X"}}, cursor=None)


def test_rate_limits_and_schema_drift_fail_closed():
    def limited(**_):
        return {"status": 429, "headers": {"Retry-After": "2"}, "content": b"", "origin": "fixture"}

    adapter = LifeSciSourceAdapter(h.source("uniprot"), transport=limited)
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "reference", "parameters": {}}, cursor=None)
    assert caught.value.code == "rate_limited"
    broken = [{"request": "/uniprotkb/P0DZZ1?format=json", "body": {"entryType": "x"}}]
    adapter = LifeSciSourceAdapter(h.source("uniprot"), transport=fixture_transport(broken))
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "reference", "parameters": {}}, cursor=None)
    assert caught.value.code == "schema_drift"


def test_runtime_runs_record_receipts_and_replays_add_nothing():
    env = h.Env().loaded()
    runs = env.store.runs(h.NS)
    assert {r["provider"] for r in runs} == {"uniprot", "ncbi", "pdb", "chembl"}
    assert {r["status"] for r in runs} == {"complete"} and {r["evidence_origin"] for r in runs} == {"fixture"}
    before = env.store.generation(h.NS)
    assert env.run("lifesci-replay")["status"] == "complete"
    assert env.store.generation(h.NS) == before
