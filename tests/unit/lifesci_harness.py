"""Offline harness for the Science life-sciences features (#2652): authored payloads, real adapter and runtime.

Payloads under ``tests/fixtures/source_packs/lifesci-*.json`` (built by
:mod:`tests.unit.lifesci_fixture_builder`) are authored in each provider's
documented response shape; identifiers and values are fictional. They run
through :class:`LifeSciSourceAdapter` (the connector the runtime compiles) and
:class:`LifeSciProjector` inside :class:`SourcePackRuntime`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any
from unittest import mock

import duckdb

from src.ingestion.lifesci_sources import FIXTURE_SECRET, fixture_transport
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.lifesci_store import LifeSciStore

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "config/source_packs/scientific.json"
NS = "global"
READ = {"knowledge:lifesci:read", f"namespace:{NS}:read"}
WRITE = READ | {"knowledge:lifesci:write", f"namespace:{NS}:write"}
REVIEW = WRITE | {"knowledge:lifesci:review"}
ALL = REVIEW | {
    "knowledge:subscriptions:read", "knowledge:subscriptions:write", "knowledge:entity-history:read",
    "knowledge:entity-history:write", "knowledge:entity-history:review", "knowledge:entity-history:execute",
    "knowledge:substances:read", "knowledge:clinical:read", "knowledge:environment:read",
    "namespace:environment:read", "namespace:chemicals:read", "namespace:clinical:read",
}
SOURCES = {"uniprot": "uniprot-proteins", "ncbi": "ncbi-genes-taxonomy", "pdb": "rcsb-pdb-structures",
           "chembl": "chembl-bioactivity"}
LATER = json.loads((ROOT / "tests/fixtures/lifesci/later_payloads.json").read_text())
PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # resolver stub
START = 4_072_000_000_000  # 2099-01-13


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(provider: str) -> dict:
    return copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[provider]))


def pages(provider: str, *, later: bool = False) -> list[dict[str, Any]]:
    items = copy.deepcopy(json.loads((ROOT / source(provider)["fixture"]["path"]).read_text())["native_pages"])
    if later:
        overrides = LATER.get(provider) or {}
        for page in items:
            if page["request"] in overrides:
                page.update(copy.deepcopy(overrides[page["request"]]))
    return items


class Env:
    """One deployment with the scientific source pack installed and the life-sciences licences accepted."""

    def __init__(self, conn: Any | None = None, *, clock: int = START) -> None:
        self.conn = conn if conn is not None else duckdb.connect(":memory:")
        self.value = manifest()
        SourcePackStore(self.conn).install(self.value, principal_id="operator", enable=True, now_ms=10)
        self.clock = clock
        self.runtime = SourcePackRuntime(self.conn, now=self.tick, sleep=lambda _d: None)
        for source_id in SOURCES.values():
            self.runtime.accept_license(self.value["pack_id"], source_id, principal_id="operator")
        self.store = LifeSciStore(self.conn, now=self.tick)

    def tick(self) -> int:
        self.clock += 1_000
        return self.clock

    def advance(self, days: float) -> None:
        self.clock += int(days * 86_400_000)

    def adapters(self, providers, later: bool = False) -> dict:
        installed = {s["source_id"]: s for s in self.runtime._manifest(self.value["pack_id"])[0]["sources"]}
        return {SOURCES[p]: self.runtime.factory.compile(installed[SOURCES[p]],
                                                         transport=fixture_transport(pages(p, later=later)),
                                                         secret=FIXTURE_SECRET) for p in providers}

    def run(self, key: str = "lifesci-1", *, providers=tuple(SOURCES), later: bool = False) -> dict:
        request = {"pack_id": self.value["pack_id"], "run_key": key, "operation": "reference",
                   "max_results": 1000, "max_bytes": 20_000_000, "timeout_ms": 60_000,
                   "source_ids": [SOURCES[p] for p in providers]}
        # The projector's store dates retrievals by the deployment clock, so as-of answers are reproducible.
        with mock.patch("src.kb.lifesci_store.time.time", lambda: self.clock / 1000):
            return self.runtime.run(request, principal_id="operator", adapters=self.adapters(providers, later),
                                    dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: FIXTURE_SECRET)

    def loaded(self) -> Env:
        result = self.run()
        assert result["status"] == "complete", result
        return self

    def later(self, key: str = "lifesci-2") -> dict:
        self.advance(60)
        result = self.run(key, later=True)
        assert result["status"] == "complete", result
        return result

    # ------------------------------------------------------------------ other packs (fictional records)

    def seed_substance(self, inchikey: str = "ZZZZZZZZZZZZZA-UHFFFAOYSA-N", namespace: str = NS) -> str:
        """A Chemicals (PubChem) substance publishing the compound's InChIKey."""
        from src.kb.substances_records import statement
        from src.kb.substances_store import SubstanceStore

        subject = {"key": "pubchem:cid:999001", "kind": "single-component", "name": "fictinib"}
        source = {"url": "https://pubchem.ncbi.nlm.nih.gov/compound/999001", "locator": "/PC_Compounds/0",
                  "attribution": "PubChem (fictional fixture)"}
        SubstanceStore(self.conn, now=self.tick).apply(
            namespace, statement("identifier", "pubchem", subject, f"inchikey:{inchikey}",
                                 {"scheme": "inchikey", "value": inchikey}, source=source))
        return subject["key"]

    def seed_biodiversity_taxon(self, *, cross_reference: bool = True, namespace: str = NS) -> str:
        """A GBIF backbone taxon for the fixture organism, optionally publishing the NCBI Tax ID."""
        from src.kb.biodiversity_records import statement
        from src.kb.biodiversity_store import BiodiversityStore

        refs = [{"scheme": "ncbi-taxon", "value": "999001", "field": "identifiers",
                 "basis": "cross-reference published by GBIF"}] if cross_reference else None
        store = BiodiversityStore(self.conn, now=self.tick)
        taxon = statement("taxon_identity", "gbif", "9990001", subject_name="Exemplomyces fictus",
                          source={"url": "https://api.gbif.org/v1/species/9990001", "evidence_origin": "fixture"},
                          as_published={"key_scheme": "gbif-taxon-key", "native_key": "9990001",
                                        "scientific_name": "Exemplomyces fictus", "authorship": "Fictor 2090",
                                        "rank": "species", "status": "accepted", "status_class": "accepted",
                                        "cross_references": refs})
        occurrence = statement(
            "occurrence", "gbif", "9990000001", subject_name="Exemplomyces fictus",
            source={"url": "https://www.gbif.org/occurrence/9990000001", "evidence_origin": "fixture"},
            as_published={"gbif_id": "9990000001", "dataset_key": "fict-dataset", "taxon_key": "9990001",
                          "scientific_name": "Exemplomyces fictus", "country_code": "DE",
                          "generalisation": {"generalised": False}})
        store.observe(namespace, [taxon, occurrence])
        return "gbif:9990001"

    def seed_medicine(self, namespace: str = NS) -> str:
        """An EMA medicinal-product record whose regulatory record names the ChEMBL compound and target."""
        from src.kb.clinical_medicines import record
        from src.kb.clinical_records import ClinicalRecordStore, record_id

        product = record("medicinal-product", provider="ema", native_id="EMEA/H/C/999001", jurisdiction="EU",
                         authority="EMA", source_url="https://www.ema.europa.eu/en/medicines/human/EPAR/fictinib",
                         native_version={"version": None, "date": "2099-02-01", "basis": "epar-revision"},
                         name="Fictinib Fixture", active_substances=[{"name": "fictinib", "locator": {"row": 1}}],
                         identifiers=[{"kind": "chembl", "value": "CHEMBL9990101", "locator": {"row": 2}},
                                      {"kind": "chembl-target", "value": "CHEMBL9990201", "locator": {"row": 3}}])
        ClinicalRecordStore(self.conn, now=self.tick).ingest(
            namespace, "ema-epar", [product], observation_id="fixture-medicine", observed_at_ms=self.tick(),
            scopes={"operator"})
        return record_id(namespace, product)

    def seed_paper(self, doi: str = "10.5555/fict.lifesci.2099.1") -> str:
        from services.ingest.common.document_model import Document
        from src.ingestion.document_store import DocumentStore

        document = Document(document_id="lifesci-doc:fictional-kinase", source_type="paper", source_id="crossref",
                            language="en", ingested_at=self.tick(), url="https://example.org/fictional-kinase",
                            title="A fictional kinase from a fictional fungus.", content="Fictional abstract",
                            authors=[], metadata={"content_representation": "plain-text-abstract", "doi": doi})
        outcome = DocumentStore(self.conn).upsert([document.to_dict()])
        assert not outcome.invalid, outcome.dead_letter
        return document.document_id
