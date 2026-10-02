"""Shared offline harness for the Science life-sciences tests (#2652): pinned synthetic fixtures through the real
adapter and projector, and synthetic Chemicals, Clinical, Biodiversity and literature records for cross-pack
identity and links. Every accession, organism, compound and title is fictional."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.lifesci_sources import FIXTURE_DAY, LifeSciAdapter, fixture_transport
from src.ingestion.source_packs import validate_source_pack
from src.kb.lifesci_store import LifeSciProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/life_sciences"
NS = "global"
SCOPES = {
    "knowledge:lifesci:read",
    "knowledge:lifesci:write",
    "knowledge:lifesci:review",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:clinical:read",
    "knowledge:chemicals:read",
    "knowledge:environment:read",
    "knowledge:read",
    "namespace:global:read",
    "namespace:global:write",
}
READ_ONLY = {"knowledge:lifesci:read", "namespace:global:read"}
SOURCES = {
    "uniprot": "uniprot-lifesci-proteins",
    "ncbi-gene": "ncbi-gene-lifesci",
    "ncbi-taxonomy": "ncbi-taxonomy-lifesci",
    "rcsb-pdb": "rcsb-pdb-lifesci-structures",
    "chembl": "chembl-lifesci-bioactivity",
}
REVISIONS = {"uniprot": "uniprot.json", "ncbi-gene": "ncbi_gene.json", "rcsb-pdb": "rcsb_pdb.json",
             "chembl": "chembl.json"}
FIRST = 4_102_444_800_000  # 2100-01-01
SECOND = 4_105_123_200_000  # 2100-02-01


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads((ROOT / "config/source_packs/scientific.json").read_text()))


def revision_fixture(provider: str) -> dict[str, Any]:
    return json.loads((FIXTURES / "v2" / REVISIONS[provider]).read_text())


def source(provider: str, *, revision: bool = False) -> dict[str, Any]:
    item = copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[provider]))
    if revision:
        item["life_sciences"] = revision_fixture(provider)["life_sciences"]
    return item


def pages(provider: str, revision: bool = False) -> list[dict[str, Any]]:
    if revision:
        return revision_fixture(provider)["native_pages"]
    return json.loads((ROOT / source(provider)["fixture"]["path"]).read_text())["native_pages"]


def fetch(provider: str, *, revision: bool = False, transport=None) -> list[tuple[list[dict[str, Any]], dict]]:
    item = source(provider, revision=revision)
    adapter = LifeSciAdapter(item, transport=transport or fixture_transport(pages(provider, revision)),
                             today=lambda: FIXTURE_DAY)
    out, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "release", "parameters": {},
                                   "limit": item["budgets"]["max_results"]}, cursor=cursor)
        out.append(([dict(r) for r in page.records], dict(page.receipt)))
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, provider: str, *, revision: bool = False, at_ms: int | None = None) -> list[dict[str, Any]]:
    item = source(provider, revision=revision)
    projector = LifeSciProjector(conn)
    if at_ms is not None:
        projector.store.now = lambda: at_ms
    results = []
    for records, receipt in fetch(provider, revision=revision):
        results.append(projector.project_page(run_id=f"run:{provider}:{'v2' if revision else 'v1'}", manifest=None,
                                              source=item, records=records, documents=None, page_receipt=receipt,
                                              principal_id="svc"))
    return results


def load_all(conn, *, revisions: bool = False) -> None:
    for provider in SOURCES:
        apply(conn, provider, at_ms=FIRST)
    if revisions:
        for provider in REVISIONS:
            apply(conn, provider, revision=True, at_ms=SECOND)


# ------------------------------------------------------------------ other packs (synthetic)

EXAMPLINIB_KEY = "EXAMPLAKINASEA-UHFFFAOYSA-N"


def seed_chemicals(conn) -> str:
    """A PubChem-style substance publishing the Examplinib InChIKey; returns its subject key."""
    from src.kb.substances_records import statement
    from src.kb.substances_store import SubstanceStore

    subject = {"key": "pubchem:cid:99000101", "kind": "unknown", "name": "examplinib"}
    src = {"url": "https://pubchem.ncbi.nlm.nih.gov/compound/99000101", "locator": "/",
           "attribution": "authored test record (fictional)"}
    SubstanceStore(conn).observe(NS, [
        statement("substance", "pubchem", subject, "compound", {"preferred_name": "examplinib"}, source=src),
        statement("identifier", "pubchem", subject, f"inchikey:{EXAMPLINIB_KEY}",
                  {"scheme": "inchikey", "value": EXAMPLINIB_KEY}, source=src)])
    return subject["key"]


def seed_biodiversity(conn) -> None:
    """GBIF identities (one publishing the NCBI Tax ID, one not) and occurrences of Exampla fictiva."""
    from src.kb import biodiversity_records as br
    from src.kb.biodiversity_store import BiodiversityStore

    src = {"url": "https://api.gbif.org/v1/species/9900001"}
    identity = br.statement("taxon_identity", "gbif", "9900001", subject_name="Exampla fictiva", source=src,
                            as_published={"key_scheme": "gbif-taxon-key", "native_key": "9900001",
                                          "scientific_name": "Exampla fictiva", "rank": "species",
                                          "status": "accepted", "status_class": "accepted",
                                          "cross_references": [{"scheme": "ncbi-taxon-id", "value": "99000001",
                                                                "field": "identifiers",
                                                                "basis": "cross-reference published by GBIF"}]})
    other = br.statement("taxon_identity", "gbif", "9900100", subject_name="Northwindia borealis",
                         source={"url": "https://api.gbif.org/v1/species/9900100"},
                         as_published={"key_scheme": "gbif-taxon-key", "native_key": "9900100",
                                       "scientific_name": "Northwindia borealis", "rank": "species",
                                       "status": "accepted", "status_class": "accepted"})
    occurrence = br.statement("occurrence", "gbif", "99000777", subject_name="Exampla fictiva",
                              source={"url": "https://api.gbif.org/v1/occurrence/99000777"},
                              as_published={"gbif_id": "99000777", "dataset_key": "fictional-dataset",
                                            "taxon_key": "9900001", "accepted_taxon_key": "9900001",
                                            "scientific_name": "Exampla fictiva", "event_date": "2099-05-01",
                                            "generalisation": br.generalisation(coordinates_published=False)})
    BiodiversityStore(conn).observe(NS, [identity, other, occurrence])


def seed_clinical(conn) -> None:
    """A medicinal product naming Examplinib's ChEMBL ID and a label revision citing the target accession."""
    from src.kb.clinical_medicines import record
    from src.kb.clinical_records import ClinicalRecordStore

    ema = {"provider": "ema", "native_id": "EMEA/H/C/099001", "jurisdiction": "EU", "authority": "EMA",
           "source_url": "https://www.ema.europa.eu/en/medicines/human/EPAR/examplinib-fixture"}
    product = record("medicinal-product", **ema, native_version={"version": None, "date": "2099-06-01",
                                                                 "basis": "epar-revision"},
                     name="Examplinib Fictiva", active_substances=[{"name": "examplinib"}],
                     identifiers=[{"kind": "chembl-id", "value": "CHEMBL9900101"}])
    label = record("label-revision", **ema, native_version={"version": "1", "date": "2099-06-01",
                                                            "basis": "epar-revision"},
                   product={"provider": "ema", "native_id": "EMEA/H/C/099001"},
                   document={"kind": "smpc", "id": "EMEA/H/C/099001:smpc", "version": "1",
                             "effective_date": "2099-06-01"},
                   sections=[{"code": "5.1", "code_system": "smpc", "title": "Pharmacodynamic properties",
                              "text": "Examplinib inhibits Exampla kinase 1 (UniProt X9EXA1) in a fictional label.",
                              "locator": {"url": ema["source_url"], "lines": [1, 2]}}])
    ClinicalRecordStore(conn).ingest(NS, "ema-epar", [product, label], observation_id="seed", observed_at_ms=FIRST,
                                     scopes={"knowledge:clinical:write", "knowledge:ingestion:execute",
                                             f"namespace:{NS}:write"})


def seed_literature(conn) -> None:
    from src.ingestion.document_store import DocumentStore

    DocumentStore(conn).upsert([{
        "document_id": "paper:exampla-2099-001", "source_type": "paper", "language": "en", "ingested_at": 1,
        "created_at": 1, "source_id": "papers", "url": "https://doi.org/10.99999/exampla.2099.001",
        "title": "Exampla kinase inhibitors (fictional)", "content": "Authored fictional abstract.", "authors": [],
        "metadata": {"doi": "10.99999/exampla.2099.001"}}])


def seed_all(conn) -> None:
    seed_chemicals(conn)
    seed_biodiversity(conn)
    seed_clinical(conn)
    seed_literature(conn)
