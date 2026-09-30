"""Offline harness for the News fact-checks provider (#2659): authored responses replayed through the adapter.

Every file under ``tests/fixtures/fact_checks`` is authored in the provider's
documented shape (Google ``claims:search`` JSON, the Data Commons ClaimReview
``DataFeed`` and the IFCN signatories listing) and names fictional publishers,
claimants and claims only; placeholder reviewer names, images and job titles
exist to prove that the parsers discard them. Nothing here is live coverage.
Responses go through :class:`FactChecksAdapter` (the connector the runtime
compiles) and :class:`FactChecksProjector`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import duckdb

from src.ingestion.fact_checks_sources import (
    FIXTURE_SECRET,
    FactChecksAdapter,
    fixture_transport,
    requests_for,
)
from src.ingestion.source_packs import validate_source_pack
from src.kb.fact_checks_records import FactChecksProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/fact_checks"
PACK = ROOT / "config/source_packs/osint.json"
NS = "global"
READ = "knowledge:news:fact-checks:read"
WRITE = "knowledge:news:fact-checks:write"
REVIEW = "knowledge:news:fact-checks:review"
CLAIMANT = "knowledge:news:fact-checks:claimant:read"
SCOPES = {READ, WRITE, f"namespace:{NS}:read", f"namespace:{NS}:write", "knowledge:subscriptions:read",
          "knowledge:subscriptions:write", "knowledge:citation:read"}
REVIEW_SCOPES = SCOPES | {REVIEW, CLAIMANT}
READ_ONLY = {READ, f"namespace:{NS}:read"}
GOOGLE, DATACOMMONS, IFCN = "fact-checks-google-claim-search", "fact-checks-datacommons-feed", \
    "fact-checks-ifcn-signatories"
SOURCES = [GOOGLE, DATACOMMONS, IFCN]
SEALS = "Tidal turbines in Example Bay killed 4,000 seals last year."
APPEARANCE = "https://news.example.com/2025/03/01/mayor-seals-speech"
SEALS_REVIEW = "https://factcheck.example.org/2025/03/tidal-turbines-seals/"
# source id -> ordered (unit, [fixture file per page]) pairs, as declared in config/source_packs/osint.json
UNITS: dict[str, list[tuple[dict, list[str]]]] = {
    GOOGLE: [({"query": "tidal turbine", "language": "en", "max_age_days": 365},
              ["google_claims_tidal_p1.json", "google_claims_tidal_p2.json"]),
             ({"publisher_site": "claimwatch.example.com", "max_age_days": 365}, ["google_claims_claimwatch.json"])],
    DATACOMMONS: [({"path": "/datacommons-feeds/claimreview/latest/data.json",
                    "publisher_sites": ["factcheck.example.org", "verifica.example.net", "claimwatch.example.com"],
                    "from": "2025-01-01", "to": "2025-12-31"}, ["datacommons_claimreview_2025-06.json"])],
    IFCN: [({"path": "/signatories"}, ["ifcn_signatories.html"])],
}
FORMATS = {GOOGLE: "google-factcheck-claimsearch-json", DATACOMMONS: "datacommons-claimreview-feed-json",
           IFCN: "ifcn-signatories-html"}
ENDPOINTS = {GOOGLE: "https://factchecktools.googleapis.com/v1alpha1", DATACOMMONS: "https://storage.googleapis.com",
             IFCN: "https://ifcncodeofprinciples.poynter.org"}
NEXT_TOKEN = "page-2-token"


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id: str) -> dict:
    return copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == source_id))


def fixture_body(filename: str, version: str = "v1") -> str:
    path = FIXTURES / "v2" / filename if version == "v2" and (FIXTURES / "v2" / filename).exists() \
        else FIXTURES / filename
    return path.read_text()


def native_pages(source_id: str, version: str = "v1") -> list[dict]:
    """The native responses for every declared unit, keyed by the request the adapter makes."""
    fmt = FORMATS[source_id]
    pages = []
    for unit, files in UNITS[source_id]:
        path, params, _ = requests_for(fmt, unit)
        for index, filename in enumerate(files):
            request = dict(params) if index == 0 else {**params, "pageToken": NEXT_TOKEN}
            query = urlencode(sorted(request.items()))
            pages.append({
                "request": urlsplit(ENDPOINTS[source_id]).path + path + ("?" + query if query else ""),
                "status": 200,
                "headers": {"Content-Type": "text/html" if filename.endswith(".html") else "application/json"},
                "body": fixture_body(filename, version)})
    return pages


def source_pack_fixture(source_id: str) -> dict:
    return {
        "captured": None,
        "native_pages": native_pages(source_id),
        "note": "Authored responses in the provider's documented shape; every publisher, site, claimant and claim is "
                "fictional, and placeholder reviewer names, images and job titles are discarded by the parser (FC01 "
                "minimisation).",
        "provider": "authored",
        "scenarios": ["authored-fixture", "fictional-publishers", "ratings-verbatim", "minimised-personal-fields"],
    }


def adapter(source_id: str, version: str = "v1", item: dict | None = None) -> FactChecksAdapter:
    return FactChecksAdapter(item or source(source_id), transport=fixture_transport(native_pages(source_id, version)),
                             secret=FIXTURE_SECRET)


def apply(conn, source_id: str, *, version: str = "v1", run_id: str | None = None) -> list[dict]:
    """Every unit of one source through the real adapter and projector; returns the per-page outcomes."""
    item = source(source_id)
    fetcher = adapter(source_id, version, item)
    projector = FactChecksProjector(conn)
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


def load_all(conn, *, version: str = "v1", run_id: str | None = None) -> None:
    for source_id in SOURCES:
        apply(conn, source_id, version=version, run_id=run_id)


def load_news(conn) -> None:
    """A synthetic news warehouse: the article the seals claim appeared in, two argument claims and entities."""
    from src.database.local_warehouse_seed import ensure_schema
    from src.kb.entities import add_manual_alias, ensure_entity_schema

    ensure_schema(conn)
    ensure_entity_schema(conn)
    columns = {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE "
                                          "table_name='documents'").fetchall()}
    for document_id, url, title in (
            ("doc-seals", APPEARANCE, "Mayor claims turbines killed thousands of seals"),
            ("doc-other", "https://news.example.com/2025/04/02/harbour-dredging", "Harbour dredging begins")):
        values = {"document_id": document_id, "source_type": "news", "url": url, "canonical_url": url,
                  "content_hash": f"sha256:{document_id}", "title": title, "content": title, "language": "en"}
        keep = [c for c in values if c in columns]
        conn.execute(f"INSERT INTO documents ({', '.join(keep)}) VALUES ({', '.join('?' * len(keep))})",
                     [values[c] for c in keep])
    conn.execute("INSERT INTO argument_claims (claim_id, claim_text, document_id, source_type) VALUES "
                 "('claim-seals', 'Tidal turbines in Example Bay killed 4,000 seals last year', 'doc-seals', 'news'), "
                 "('claim-dredging', 'Harbour dredging will start in April', 'doc-other', 'news'), "
                 "('claim-array', 'The tidal array supplies half of the region''s electricity', 'doc-other', 'news')")
    add_manual_alias(conn, "Q999999901", "Alex Example", "person")
    add_manual_alias(conn, "Example Energy Council", "Example Energy Council", "organization")


def load_source_identity(conn, namespace: str = NS) -> str:
    """A reviewed source identity whose domain alias is the Example Fact Check site."""
    from src.kb.source_identity import (
        READ_SCOPE,
        REVIEW_SCOPE,
        WRITE_SCOPE,
        SourceIdentityStore,
    )

    store = SourceIdentityStore(conn)
    scopes = {READ_SCOPE, WRITE_SCOPE, REVIEW_SCOPE}
    identity = store.register(namespace, "publication", "Example Fact Check", principal_id="curator", scopes=scopes)
    store.decide_alias(namespace, identity["source_id"], "domain", "factcheck.example.org",
                       reason="the publication's own site, reviewed", reviewer_id="curator", scopes=scopes)
    return identity["source_id"]


def accepted_world(*, news: bool = True, version: str = "v1"):
    """Everything loaded and every non-lexical match proposal accepted by a reviewer."""
    from src.kb.fact_checks_identity import FactCheckIdentity

    conn = connection()
    load_all(conn)
    if version == "v2":
        load_all(conn, version="v2", run_id="run:v2")
    if news:
        load_news(conn)
        load_source_identity(conn)
    identity = FactCheckIdentity(conn)
    proposed = identity.propose(NS, principal_id="alice", scopes=REVIEW_SCOPES)
    for match in proposed["matches"]:
        if match["method"] == "lexical-overlap":
            continue
        identity.review(NS, match["match_id"], "accept", "fixture review", principal_id="rev", scopes=REVIEW_SCOPES)
    return conn
