"""Offline harness for the Science research-entities features (#2579): authored responses replayed through the adapter.

Every file under ``tests/fixtures/research_entities`` is authored in the registry's documented shape (ROR v2 dump
JSON, ORCID v3.0 record JSON, DataCite JSON:API, CORDIS semicolon CSV exports) and names fictional organisations,
researchers, datasets and projects only (Exampla, Northwind); the personal fields the RE01 minimisation decision
excludes carry placeholder values that the parser must discard. Nothing here is live coverage. Responses go through
:class:`ResearchEntitiesAdapter` (the connector the runtime compiles) and :class:`ResearchEntityProjector`.
"""

from __future__ import annotations

import base64
import copy
import io
import json
import zipfile
from pathlib import Path

import duckdb

from src.ingestion.research_entities_sources import (
    FIXTURE_SECRET,
    ResearchEntitiesAdapter,
    fixture_request,
    fixture_transport,
)
from src.ingestion.source_packs import validate_source_pack
from src.kb.research_entities_records import ResearchEntityProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/research_entities"
PACK = ROOT / "config/source_packs/research.json"
NS = "global"
OWN_NS = "ownership"
READ = "knowledge:science:research-entities:read"
WRITE = "knowledge:science:research-entities:write"
RESEARCHERS = "knowledge:science:research-entities:researchers:read"
SCOPES = {
    READ, WRITE, RESEARCHERS, f"namespace:{NS}:read", f"namespace:{NS}:write", f"namespace:{OWN_NS}:read",
    f"namespace:{OWN_NS}:write", "knowledge:ownership:read", "knowledge:ownership:write",
    "knowledge:subscriptions:read", "knowledge:subscriptions:write", "knowledge:funding:read", "knowledge:read",
}
REVIEW_SCOPES = SCOPES | {"knowledge:ownership:review"}
NO_RESEARCHERS = SCOPES - {RESEARCHERS}
READ_ONLY = {READ, f"namespace:{NS}:read"}

ROR_SOURCE, ORCID_SOURCE, DATACITE_SOURCE, CORDIS_SOURCE = (
    "research-entities-ror", "research-entities-orcid", "research-entities-datacite", "research-entities-cordis")
SOURCES = [ROR_SOURCE, ORCID_SOURCE, DATACITE_SOURCE, CORDIS_SOURCE]
FORMATS = {ROR_SOURCE: "ror-dump-zip", ORCID_SOURCE: "orcid-record-json", DATACITE_SOURCE: "datacite-doi-json",
           CORDIS_SOURCE: "cordis-projects-csv-zip"}
EXAMPLA = "https://ror.org/0zzexa101"
MARINE = "https://ror.org/0zzexa202"
HOSPITAL = "https://ror.org/0zzexa505"
NORTHWIND_POLY = "https://ror.org/0zznwd303"
NORTHWIND_TECH = "https://ror.org/0zznwd404"
ADA, CY, BO = "0000-0009-9999-0011", "0000-0009-9999-002X", "0000-0009-9999-0038"
DS1, DS2 = "10.99999/exampla.ds.001", "10.99999/exampla.ds.002"
PAPER1, PAPER2 = "10.99998/exampla.paper.001", "10.99998/exampla.paper.002"
EXAMPLAR, NORTHWAVE = "101999001", "101999002"
PIC_EXAMPLA, PIC_NORTHWIND, PIC_INDUSTRIES = "999999901", "999999902", "999999903"
# Placeholder personal values in the fixtures that must never reach a record, receipt, answer or notice.
PERSONAL = ("Fictional biography text", "ada@exampla-university.example", "fictional-keyword", "A. Exampla-Alias",
            "https://ada.example", "Exampla Grammar School", "Private fictional draft", "Hidden Fictional Employer",
            "SCOPUS-PLACEHOLDER-77", "PERSONAL NAME OF SOURCE", "Doe, Sample", "Keeper, Data", "1 Example Street",
            "fictional/contact")


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id: str, *, releases: int | None = None) -> dict:
    """A declared source; ``releases`` keeps only the first N ROR releases (the world before the next release)."""
    item = copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == source_id))
    if releases is not None:
        selection = item["research_entities"]["selection"]
        selection["releases"] = selection["releases"][:releases]
    return item


def ror_zip(release_label: str, member: str) -> bytes:
    """A stored (uncompressed, fixed-date) zip holding the release's dump member, so its bytes are reproducible."""
    body = (FIXTURES / f"ror_{release_label}.json").read_bytes()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        info = zipfile.ZipInfo(member, date_time=(2099, 1, 1, 0, 0, 0))
        archive.writestr(info, body)
    return buffer.getvalue()


def cordis_zip(version: str = "v1") -> bytes:
    base = FIXTURES / "v2" if version == "v2" else FIXTURES
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for member, name in (("project.csv", "cordis_horizon_project.csv"),
                             ("organization.csv", "cordis_horizon_organization.csv")):
            archive.writestr(zipfile.ZipInfo(member, date_time=(2099, 1, 1, 0, 0, 0)), (base / name).read_bytes())
    return buffer.getvalue()


def _text(name: str, version: str) -> str:
    path = FIXTURES / "v2" / name
    return path.read_text() if version == "v2" and path.exists() else (FIXTURES / name).read_text()


def native_pages(source_id: str, version: str = "v1", item: dict | None = None) -> list[dict]:
    """The native response for every declared unit, keyed by the request the adapter makes."""
    item = item or source(source_id)
    declared = item["research_entities"]
    fmt = declared["format"]
    pages = []
    for unit in declared["selection"][{"ror-dump-zip": "releases", "orcid-record-json": "orcids",
                                        "datacite-doi-json": "dois",
                                        "cordis-projects-csv-zip": "programmes"}[fmt]]:
        unit = unit if isinstance(unit, dict) else {"id": unit}
        page = {"request": fixture_request(fmt, unit, item["endpoint"]), "status": 200, "headers": {}}
        if fmt == "ror-dump-zip":
            page["body_base64"] = base64.b64encode(ror_zip(unit["label"], unit["member"])).decode()
            page["headers"] = {"Content-Type": "application/zip"}
        elif fmt == "cordis-projects-csv-zip":
            page["body_base64"] = base64.b64encode(cordis_zip(version)).decode()
            page["headers"] = {"Content-Type": "application/zip"}
        else:
            name = (f"orcid_{unit['id']}.json" if fmt == "orcid-record-json"
                    else f"datacite_{unit['id'].split('/', 1)[1]}.json")
            body = _text(name, version)
            page["body"] = body
            page["headers"] = {"Content-Type": "application/json"}
            code = json.loads(body).get("response-code") or (
                404 if (json.loads(body).get("errors") or [{}])[0].get("status") == "404" else None)
            if code:
                page["status"] = int(code)
        pages.append(page)
    return pages


def source_pack_fixture(source_id: str) -> dict:
    return {
        "captured": None,
        "native_pages": native_pages(source_id),
        "note": "Authored responses in the registry's documented shape (verify against a live response); every "
                "organisation, researcher, dataset, project and identifier is fictional, and personal fields outside "
                "the RE01 minimisation decision carry placeholders that the parser discards.",
        "provider": "authored",
        "scenarios": ["authored-fixture", "fictional-entities", "minimised-researchers"],
    }


def adapter(source_id: str, version: str = "v1", item: dict | None = None,
            secret: str | None = FIXTURE_SECRET) -> ResearchEntitiesAdapter:
    item = item or source(source_id)
    return ResearchEntitiesAdapter(item, transport=fixture_transport(native_pages(source_id, version, item)),
                                   secret=secret)


def fetch(source_id: str, version: str = "v1", item: dict | None = None) -> tuple[list[dict], list[dict]]:
    fetcher = adapter(source_id, version, item)
    records, receipts, cursor = [], [], None
    for _ in fetcher.units:
        page = fetcher.fetch_page({"operation": "selection", "parameters": {}, "limit": 500}, cursor=cursor)
        records += [r["research_entity_record"] for r in page.records]
        receipts.append(page.receipt)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records, receipts


def apply(conn, source_id: str, *, version: str = "v1", run_id: str | None = None, item: dict | None = None,
          observed_at_ms: int | None = None) -> list[dict]:
    """Every unit of one source through the real adapter and projector; returns the per-page outcomes."""
    item = item or source(source_id)
    fetcher = adapter(source_id, version, item)
    projector = ResearchEntityProjector(conn)
    if observed_at_ms is not None:
        projector.store.now = lambda: observed_at_ms
    outcomes, cursor = [], None
    for _ in fetcher.units:
        page = fetcher.fetch_page({"operation": "selection", "parameters": {}, "limit": 500}, cursor=cursor)
        outcomes += projector.project_page(run_id=run_id or f"run:{source_id}:{version}", manifest=None, source=item,
                                           records=page.records, documents=[], page_receipt=page.receipt,
                                           principal_id="operator")
        cursor = page.next_cursor
        if cursor is None:
            break
    return outcomes


# 2099-03-05 and 2099-06-05 (UTC) in milliseconds: the observation times of the first and second acquisition.
FIRST_RUN_MS = 4076467200000
SECOND_RUN_MS = 4084416000000


def load_all(conn, *, version: str = "v1", run_id: str | None = None) -> None:
    """The first acquisition: ROR release v9.1 only and the v1 responses of ORCID, DataCite and CORDIS."""
    at = FIRST_RUN_MS if version == "v1" else SECOND_RUN_MS
    for source_id in SOURCES:
        item = source(source_id, releases=1) if source_id == ROR_SOURCE and version == "v1" else None
        apply(conn, source_id, version=version, run_id=run_id or f"run:{version}", item=item, observed_at_ms=at)


def load_second(conn) -> None:
    """The second acquisition: both ROR releases (v9.2 new) and the v2 responses."""
    load_all(conn, version="v2", run_id="run:v2")


def seed_papers(conn, dois=(PAPER1, PAPER2)) -> dict[str, str]:
    """Scholarly literature records: paper documents whose metadata states their DOI (fictional)."""
    from src.ingestion.document_store import _SCHEMA

    conn.execute(_SCHEMA)
    ids = {}
    for number, doi in enumerate(dois, start=1):
        document_id = f"doc:exampla-paper-{number}"
        conn.execute("INSERT INTO documents (document_id, source_type, title, url, content_hash, metadata) "
                     "VALUES (?,?,?,?,?,?)",
                     [document_id, "paper", f"Fictional Exampla paper {number}", f"https://doi.org/{doi}",
                      f"sha256:fixture-{number}", json.dumps({"doi": doi})])
        ids[doi] = document_id
    return ids


def seed_funding(conn) -> str:
    """A Funding & grants topic record (eu-ft) stating the CORDIS project's topic identifier (fictional)."""
    from src.kb.funding_opportunities import FundingOpportunityStore

    FundingOpportunityStore(conn)
    content = {"contract": "noesis-funding-record-v1", "record_kind": "call", "provider": "eu-ft",
               "provider_id": "HORIZON-CL6-2094-EXAMPLE-01", "identifier": "HORIZON-CL6-2094-EXAMPLE-01",
               "title": "Fictional coastal resilience topic"}
    conn.execute("INSERT INTO funding_opportunities VALUES (?,?,?,?,?,?,?,?,?)",
                 ["fund:topic:example-01", "global", "eu-ft", "call", "HORIZON-CL6-2094-EXAMPLE-01", None, 1, 1,
                  "listed"])
    conn.execute("INSERT INTO funding_opportunity_revisions VALUES (?,?,?,?)",
                 ["fund:topic:example-01", 1, json.dumps(content), 1])
    return "fund:topic:example-01"


def load_ownership(conn, namespace: str = OWN_NS):
    """Synthetic Corporate Ownership legal entities: Exampla University (with an ISNI) and Example Industries GmbH
    (with the VAT number CORDIS publishes for the participant)."""
    from src.kb.ownership_records import record
    from src.kb.ownership_store import OwnershipStore

    records = [
        record("legal_entity", "lei:5299EXAMPLAUNIV00001", {"provider": "gleif",
                                                            "provider_record_id": "5299EXAMPLAUNIV00001"},
               name="Exampla University", jurisdiction="DE",
               identifiers=[{"scheme": "lei", "value": "5299EXAMPLAUNIV00001"},
                            {"scheme": "isni", "value": "0000000499990101"}]),
        record("legal_entity", "lei:5299EXAMPLEINDGMBH01", {"provider": "gleif",
                                                            "provider_record_id": "5299EXAMPLEINDGMBH01"},
               name="Example Industries GmbH", jurisdiction="DE",
               identifiers=[{"scheme": "lei", "value": "5299EXAMPLEINDGMBH01"},
                            {"scheme": "vat", "value": "DE999999903"}]),
    ]
    return OwnershipStore(conn).apply(namespace, records, run_id="ownership-fixture", observed_at_ms=0,
                                      principal_id="ownership-loader")


def accepted_world(*, second: bool = False, ownership: bool = True, papers: bool = True, funding: bool = True):
    """Everything loaded and every identifier-based candidate accepted by a reviewer; name-only candidates stay
    proposed except the Northwind one, which a reviewer rejects."""
    from src.kb.research_entities_identity import ResearchEntityIdentity
    from src.kb.research_entities_links import ResearchEntityLinks

    conn = connection()
    load_all(conn)
    if second:
        load_second(conn)
    if ownership:
        load_ownership(conn)
    if papers:
        seed_papers(conn)
    if funding:
        seed_funding(conn)
    identity = ResearchEntityIdentity(conn)
    proposed = identity.propose(NS, principal_id="alice", scopes=SCOPES,
                                ownership_namespace=OWN_NS if ownership else None)
    for candidate in proposed["candidates"]:
        if candidate["low_evidence"]:
            continue
        identity.review(NS, candidate["candidate_id"], "accept", "fixture review of published identifiers",
                        principal_id="reviewer", scopes=REVIEW_SCOPES)
    ResearchEntityLinks(conn).link(NS, principal_id="alice", scopes=SCOPES,
                                   ownership_namespace=OWN_NS if ownership else None)
    return conn
