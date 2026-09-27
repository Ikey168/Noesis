"""Offline Clinical Evidence harness: real runtime, adapters and stores over authored fixtures.

Provider responses are served from the ``clinical-evidence`` source pack's
authored native-page fixtures (``tests/fixtures/source_packs/clinical-*.json``,
built from ``tests/fixtures/clinical``) through injected transports, so the
source-pack runtime, native adapters, projector, record store, Science
connectors' parsers, paper families, Crossref notices, methodology provenance,
ontology and subscriptions all run for real while nothing leaves the process.
Receipts record ``execution: injected``; this is never live coverage.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests" / "fixtures" / "clinical"
PACK_FIXTURES = ROOT / "tests" / "fixtures" / "source_packs"
PACK = ROOT / "config" / "source_packs" / "clinical-evidence.json"
NS = "clinical"
BASE_SCOPES = {
    "knowledge:clinical:read", "knowledge:clinical:write", "knowledge:ingestion:execute",
    f"namespace:{NS}:read", f"namespace:{NS}:write",
    "knowledge:methodology:read", "knowledge:methodology:write", "knowledge:methodology:extract",
    "knowledge:paper-family:read", "knowledge:paper-family:write",
    "knowledge:schema:read", "knowledge:schema:register",
    "knowledge:subscriptions:read", "knowledge:subscriptions:write",
    "knowledge:reviews:read", "knowledge:reviews:write",
}
QUESTION = {"population": "adults with type 2 diabetes", "condition": "type 2 diabetes", "intervention": "noetiglutide",
            "comparator": "placebo", "outcomes": ["HbA1c"]}


def ms(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def load(name):
    path = FIXTURES / name
    return json.loads(path.read_text()) if name.endswith(".json") else path.read_text()


class FixtureWeb:
    """Request path+query -> native page; swap bodies to simulate registry changes or outages."""

    def __init__(self):
        self.pages = {}
        for path in sorted(PACK_FIXTURES.glob("clinical-*.json")):
            for page in json.loads(path.read_text())["native_pages"]:
                self.pages[page["request"]] = {"status": page.get("status", 200), "body": page["body"]}
        self.calls = []

    def set(self, request, body=None, *, status=200, fixture=None):
        self.pages[request] = {"status": status, "body": load(fixture) if fixture else body}

    def transport(self, *, url, params, headers, timeout, **_):
        from urllib.parse import parse_qsl, urlencode, urlsplit

        self.calls.append({"url": url, "params": dict(params)})
        parts = urlsplit(url)
        # A server sees URL and parameters as one query; credentials are not part of the resource.
        query = urlencode(parse_qsl(parts.query, keep_blank_values=True)
                          + [(k, v) for k, v in dict(params).items() if k != "api_key"])
        page = self.pages.get(parts.path + (f"?{query}" if query else ""))
        if page is None:
            return {"status": 404, "headers": {}, "content": b""}
        if page["status"] >= 500:
            return {"status": page["status"], "headers": {}, "content": b""}
        body = page["body"]
        content = json.dumps(body).encode() if isinstance(body, (dict, list)) else str(body).encode()
        return {"status": page["status"], "headers": {}, "content": content}


class Env:
    def __init__(self, path=None, now_iso="2026-09-27T09:00:00+00:00"):
        from src.ingestion.source_pack_runtime import SourcePackRuntime
        from src.ingestion.source_packs import SourcePackStore, validate_source_pack

        self.conn = duckdb.connect(path) if path else duckdb.connect()
        self.clock = ms(now_iso)
        self.web = FixtureWeb()
        self.manifest = validate_source_pack(json.loads(PACK.read_text()))
        SourcePackStore(self.conn).install(self.manifest, principal_id="operator", enable=True, now_ms=1)
        self.runtime = SourcePackRuntime(self.conn, now=self.now)
        for source in self.manifest["sources"]:
            self.runtime.accept_license(self.manifest["pack_id"], source["source_id"], principal_id="operator")
        self.documents = {}

    def now(self):
        self.clock += 1
        return self.clock

    def scopes(self, *extra):
        docs = {f"document:{d}:read" for d in self.documents.values()}
        return BASE_SCOPES | docs | set(extra)

    def acquire(self, run_key, source_ids=None):
        """Run the clinical source pack (selected sources) through the real runtime with fixture transports."""
        manifest, _ = self.runtime._manifest("clinical-evidence")
        selected = [s for s in manifest["sources"] if not source_ids or s["source_id"] in source_ids]
        adapters = {s["source_id"]: self.runtime.factory.compile(s, transport=self.web.transport) for s in selected}
        return self.runtime.run({"pack_id": "clinical-evidence", "run_key": run_key, "operation": "records",
                                 "source_ids": [s["source_id"] for s in selected], "max_pages": 50,
                                 "max_results": 500, "mode": "backfill", "backfill": {"from_ms": 0}},
                                principal_id="operator", adapters=adapters)

    def seed_publications(self):
        """Paper documents as the Science providers produce them, with declared registry identifiers."""
        from src.ingestion.connectors.base import RawDocument, SourceRef
        from src.ingestion.connectors.paper import trial_registry
        from src.ingestion.connectors.scholarly.sources import MedrxivConnector, PubmedConnector
        from src.ingestion.document_store import DocumentStore
        from src.ingestion.europepmc_api import records as europepmc_records
        from services.ingest.common.document_model import Document

        store = DocumentStore(self.conn)
        payloads = []
        ref = SourceRef(locator="pubmed", metadata={"source_id": "pubmed", "query": {"topic": "noetiglutide"}})
        pubmed = PubmedConnector().parse(RawDocument(ref=ref, content=(FIXTURES / "pubmed_esummary.json").read_bytes(),
                                                     fetched_at=self.clock))
        efetch = trial_registry.parse_pubmed_databanks((FIXTURES / "pubmed_efetch.xml").read_bytes())
        for doc in pubmed:
            pmid = doc.metadata["external_id"]
            extra = efetch.get(pmid, {})
            payload = trial_registry.with_registry_identifiers(
                doc, extra.get("accessions") or [], publication_types=extra.get("publication_types") or [])
            payload["content"] = " ".join(section["text"] for section in extra.get("abstract") or []) or None
            payload["metadata"]["content_representation"] = "plain-text-abstract"
            payloads.append(payload)
            self.documents[f"pubmed:{pmid}"] = payload["document_id"]
        annotations = trial_registry.parse_europepmc_accessions(load("europepmc_annotations.json"))
        mapped, _ = europepmc_records(load("europepmc_search.json"), cursor=None, limit=25)
        for item in mapped:
            doc = Document(document_id="spdoc:europepmc:" + item["id"], source_type="paper", source_id="europe-pmc",
                           language="en", ingested_at=self.clock, url=item["url"], title=item["title"],
                           content=item["content"], authors=item["authors"],
                           metadata={"source_pack_native_json": json.dumps(item, sort_keys=True),
                                     "content_representation": item["content_representation"]})
            payload = trial_registry.with_registry_identifiers(doc, annotations.get(item["id"], []))
            payloads.append(payload)
            self.documents["europepmc:" + item["id"]] = payload["document_id"]
        collection = load("medrxiv_details.json")["collection"]
        ref = SourceRef(locator="medrxiv", metadata={"source_id": "medrxiv", "query": {
            "topic": "noetiglutide", "since": "2020-01-01", "until": "2020-12-31"}})
        for doc, record in zip(MedrxivConnector().parse(RawDocument(
                ref=ref, content=(FIXTURES / "medrxiv_details.json").read_bytes(), fetched_at=self.clock)), collection):
            payload = trial_registry.with_registry_identifiers(
                doc, [], related_resources=trial_registry.rxiv_related_resources(record))
            payload["metadata"]["content_representation"] = "plain-text-abstract"
            payloads.append(payload)
            self.documents["medrxiv:" + record["doi"]] = payload["document_id"]
        outcome = store.upsert(payloads)
        assert not outcome.invalid, outcome.dead_letter
        return outcome

    def seed_retraction(self):
        from src.ingestion.crossref_notices import CrossrefNoticeCollection

        raw = (FIXTURES / "crossref_retraction.json").read_bytes()
        collection = CrossrefNoticeCollection(
            self.conn, "clinical-fixture-notices", from_date="2023-01-01", until_date="2023-12-31",
            targets={"10.5555/noetic2.2021": self.documents["pubmed:99000002"]}, rows=20, max_pages=1,
            transport=lambda **_: {"status": 200, "content": raw})
        state = collection.step()
        for (notice_document,) in self.conn.execute("SELECT notice_document_id FROM crossref_notices").fetchall():
            self.documents["notice:" + notice_document] = notice_document
        return state

    def publication_documents(self):
        from src.ingestion.document_store import DocumentStore

        store = DocumentStore(self.conn)
        docs = []
        for document_id in sorted(self.documents.values()):
            doc = store.get(document_id)
            revision = self.conn.execute(
                "SELECT revision_id FROM document_revision_records WHERE document_id=? ORDER BY revision DESC LIMIT 1",
                [document_id]).fetchone()[0]
            docs.append({**doc, "revision_id": revision})
        return docs

    def align_terms(self, scopes=None):
        from src.kb.clinical_terms import ClinicalTerms

        terms = ClinicalTerms(self.conn, now=self.now)
        mesh = load("mesh_subset.json")
        scopes = scopes or self.scopes()
        terms.publish_mesh(mesh["descriptors"], mesh["version"], principal_id="alice", scopes=scopes)
        return terms.align(NS, principal_id="alice", scopes=scopes, mesh_version=mesh["version"],
                           curations=load("term_curations.json")["curations"])

    def link(self, observation="link-1", principal_id="alice", scopes=None):
        from src.kb.clinical_publications import PublicationLinker

        return PublicationLinker(self.conn, now=self.now).link(
            NS, principal_id=principal_id, scopes=scopes or self.scopes(), observation_id=observation)

    def build_map(self, request_key="question-1", principal_id="alice", scopes=None, question=None):
        from src.kb.clinical_evidence import EvidenceMapService

        return EvidenceMapService(self.conn, now=self.now).build(
            NS, question or QUESTION, principal_id=principal_id, scopes=scopes or self.scopes(),
            request_key=request_key, documents=self.publication_documents())

    def import_prospero(self, protocol_id=None, principal_id="alice"):
        from src.kb.clinical_reviews import import_prospero_registration

        return import_prospero_registration(self.conn, NS, load("prospero_CRD42099000001.json"),
                                            principal_id=principal_id, scopes=self.scopes(), protocol_id=protocol_id)

    def review_protocol(self, principal_id="alice"):
        from src.kb.systematic_reviews import SystematicReviewStore

        return SystematicReviewStore(self.conn).create(NS, "noetiglutide-review", {
            "question": "Noetiglutide versus placebo or active comparators in adults with type 2 diabetes",
            "inclusion": ["randomised trials", "adults with type 2 diabetes"], "exclusion": ["non-randomised studies"],
            "databases": ["MEDLINE", "ClinicalTrials.gov"], "search_expressions": ["noetiglutide AND diabetes"],
            "date_from": "2015-01-01", "date_to": "2026-09-01", "reviewers": [principal_id, "bob"],
            "fields": ["hba1c"]}, principal_id=principal_id, scopes=self.scopes())

    def journey(self):
        """Acquire everything, seed publications and notices, align terms, import PROSPERO, link publications."""
        receipt = self.acquire("r1")
        self.seed_publications()
        self.seed_retraction()
        aligned = self.align_terms()
        protocol = self.review_protocol()
        self.import_prospero(protocol_id=protocol["protocol_id"])
        linked = self.link()
        return receipt, aligned, linked
