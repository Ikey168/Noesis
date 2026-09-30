"""Offline harness for the News pack's fact-checks provider (#2659): authored responses replayed through the adapter.

Every file under ``tests/fixtures/fact_checks`` is authored in the provider's
documented shape (Fact Check Tools ``claims:search`` JSON, Data Commons
ClaimReview ``DataFeed`` JSON-LD) or an assumed IFCN listing markup, and names
fictional publishers, claimants and claims only. Nothing here is live coverage.
Responses go through :class:`FactChecksAdapter` (the connector the runtime
compiles) and :class:`FactCheckProjector`.
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
from src.kb.fact_checks_records import FactCheckProjector, day_ms

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/fact_checks"
PACK = ROOT / "config/source_packs/osint.json"
NS = "global"
READ = "knowledge:news:fact-checks:read"
WRITE = "knowledge:news:fact-checks:write"
REVIEW = "knowledge:news:fact-checks:review"
SCOPES = {READ, WRITE, f"namespace:{NS}:read", f"namespace:{NS}:write", "knowledge:subscriptions:read",
          "knowledge:subscriptions:write", "knowledge:source-identity:read", "knowledge:claim-timeline:read",
          "knowledge:citation:read"}
REVIEW_SCOPES = SCOPES | {REVIEW}
READ_ONLY = {READ, f"namespace:{NS}:read"}
GOOGLE = "news-fact-checks-google"
DATACOMMONS = "news-fact-checks-datacommons"
IFCN = "news-fact-checks-ifcn"
SOURCES = [GOOGLE, DATACOMMONS, IFCN]
ENDPOINTS = {GOOGLE: "https://factchecktools.googleapis.com", DATACOMMONS: "https://storage.googleapis.com",
             IFCN: "https://ifcncodeofprinciples.poynter.org"}
FORMATS = {GOOGLE: "google-factcheck-claims-search-json", DATACOMMONS: "datacommons-claimreview-feed-jsonld",
           IFCN: "ifcn-signatories-html"}
# source id -> ordered (unit, [fixture files, one per page]), as declared in config/source_packs/osint.json
UNITS: dict[str, list[tuple[dict, list[str]]]] = {
    GOOGLE: [({"query": "Northwind widgets", "language": "en"}, ["google_search_northwind_widgets.json"]),
             ({"publisher_site": "fabrikam-facts.example"},
              ["google_search_site_fabrikam_p1.json", "google_search_site_fabrikam_p2.json"])],
    DATACOMMONS: [({"release": "latest", "publishers": ["factdesk.example", "contoso-check.example"],
                    "from": "2099-01-01", "to": "2099-12-31"}, ["datacommons_claimreview_latest.json"])],
    IFCN: [({"page": "signatories"}, ["ifcn_signatories.html"])],
}
# Observation times of the two acquisitions (v1 and the later v2 responses).
OBSERVED = {"v1": day_ms("2099-07-20", end=False), "v2": day_ms("2099-08-10", end=False)}
C1 = "Northwind Widgets exported five million widgets in 2098."
C2 = "Exampla City cut its bus fares by half in 2099."
C3 = "Contoso Energy tripled its solar output last year."
NEWS_ARTICLE = "https://news.example/2099/05/31/widget-exports"
SOCIAL_POST = "https://x.com/robinsample_fake/status/99001"


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
    base = urlsplit(ENDPOINTS[source_id]).path
    pages = []
    for unit, files in UNITS[source_id]:
        path, params = requests_for(fmt, unit)
        for index, filename in enumerate(files):
            body = fixture_body(filename, version)
            request = dict(params)
            if index:
                request["pageToken"] = json.loads(fixture_body(files[index - 1], version))["nextPageToken"]
            query = urlencode(sorted(request.items()))
            pages.append({"request": base + path + ("?" + query if query else ""), "status": 200,
                          "headers": {"Content-Type": "text/html" if filename.endswith(".html")
                                      else "application/json"},
                          "body": body})
    return pages


def source_pack_fixture(source_id: str) -> dict:
    return {
        "captured": None,
        "native_pages": native_pages(source_id),
        "note": "Authored responses in the provider's documented shape (the IFCN listing markup is assumed); every "
                "publisher, claimant, claim and URL is fictional, and the personal fields present on purpose are "
                "dropped by the parser (FC01 minimisation).",
        "provider": "authored",
        "scenarios": ["authored-fixture", "fictional-publishers", "minimised-claimants"],
    }


def adapter(source_id: str, version: str = "v1", item: dict | None = None) -> FactChecksAdapter:
    return FactChecksAdapter(item or source(source_id), transport=fixture_transport(native_pages(source_id, version)),
                             secret=FIXTURE_SECRET)


def apply(conn, source_id: str, *, version: str = "v1", run_id: str | None = None,
          observed_at_ms: int | None = None) -> list[dict]:
    """Every unit of one source through the real adapter and projector; returns the per-page outcomes."""
    item = source(source_id)
    fetcher = adapter(source_id, version, item)
    projector = FactCheckProjector(conn)
    outcomes, cursor = [], None
    for _ in fetcher.units:
        page = fetcher.fetch_page({"operation": "selection", "parameters": {}, "limit": 500}, cursor=cursor)
        outcomes += projector.project_page(
            run_id=run_id or f"run:{source_id}:{version}", manifest=None, source=item, records=page.records,
            documents=[], page_receipt=page.receipt, principal_id="operator",
            observed_at_ms=OBSERVED[version] if observed_at_ms is None else observed_at_ms)
        cursor = page.next_cursor
        if cursor is None:
            break
    return outcomes


def load_all(conn, *, version: str = "v1", run_id: str | None = None) -> None:
    for source_id in SOURCES:
        apply(conn, source_id, version=version, run_id=run_id)


def load_news(conn) -> None:
    """A news article whose URL a fact-check cites as an appearance (stored with ``www.`` and a tracking parameter
    stripped the other way round), an argument claim extracted from it, a claim timeline state and a canonical
    entity registered under a Wikidata-identifier id and a name alias."""
    from src.database.local_warehouse_seed import ensure_schema
    from src.ingestion.corrections import ensure_schema as ensure_revision_schema
    from src.kb.entities import add_manual_alias, register_canonical_entity

    ensure_schema(conn)  # documents, argument_claims and the news_articles view
    conn.execute("INSERT INTO documents (document_id, source_type, language, url, content_hash, title) VALUES "
                 "('doc-widgets', 'news', 'en', ?, 'sha-widgets-1', 'Widget exports hit record, spokesperson says')",
                 [NEWS_ARTICLE + "?utm_medium=email"])
    ensure_revision_schema(conn)
    conn.execute("INSERT INTO document_revisions VALUES ('doc-widgets', 1, 'sha-widgets-1', NULL, 'initial', 0)")
    conn.execute("INSERT INTO document_revisions VALUES ('doc-widgets', 2, 'sha-widgets-2', NULL, 'correction', 1)")
    conn.execute("INSERT INTO argument_claims (claim_id, claim_text, document_id, source_type, confidence) VALUES "
                 "('claim-widgets', 'Northwind Widgets exported five million widgets in 2098', 'doc-widgets', "
                 "'news', 0.9)")
    conn.execute("INSERT INTO argument_claims (claim_id, claim_text, document_id, source_type, confidence) VALUES "
                 "('claim-weather', 'It rained in Exampla on Tuesday', 'doc-widgets', 'news', 0.8)")
    register_canonical_entity(conn, "ent-wikidata-q99999901", "Robin Sample", "person")
    add_manual_alias(conn, "Exampla City Council", "Exampla City Council", "organization")
    from src.kb.claim_timelines import READ_SCOPE as TIMELINE_READ
    from src.kb.claim_timelines import WRITE_SCOPE as TIMELINE_WRITE
    from src.kb.claim_timelines import ClaimTimelineStore

    ClaimTimelineStore(conn).capture_state(
        NS, "claim-widgets", principal_id="curator", scopes={TIMELINE_READ, TIMELINE_WRITE}, stance="supports",
        evidence=[{"citation": "doc-widgets", "document_revision_id": "document-revision:doc-widgets:1"}],
        source_id="doc-widgets", source_revision_id="document-revision:doc-widgets:1", observed_at_ms=0)
    from src.kb.citation_preservation import CAPTURE_SCOPE, CitationPreservationStore
    from src.kb.citation_preservation import READ_SCOPE as CITATION_READ
    from src.kb.citation_preservation import WRITE_SCOPE as CITATION_WRITE

    CitationPreservationStore(conn).record_capture(NS, {
        "archive_id": "internet-archive", "archive_kind": "memento-archive", "resolver": "timetravel",
        "uri_r": "https://www.news.example/2099/05/31/widget-exports",
        "uri_m": "https://web.archive.org/web/20990601000000/https://www.news.example/2099/05/31/widget-exports",
        "memento_datetime": "Mon, 01 Jun 2099 00:00:00 GMT", "status": 200, "mimetype": "text/html", "digests": [],
        "receipt": {"request_id": "fixture-capture-1", "adapter": "fixture", "evidence_origin": "fixture"}},
        principal_id="curator", scopes={CAPTURE_SCOPE, CITATION_READ, CITATION_WRITE})


def load_source_identity(conn) -> str:
    """A source identity for the Exampla Fact Desk publication with a reviewed domain alias."""
    from src.kb.source_identity import (
        READ_SCOPE,
        REVIEW_SCOPE,
        WRITE_SCOPE,
        SourceIdentityStore,
    )

    store = SourceIdentityStore(conn)
    identity = store.register(NS, "publication", "Exampla Fact Desk", principal_id="curator",
                              scopes={WRITE_SCOPE, READ_SCOPE}, native_ids={"website": "factdesk.example"})
    store.decide_alias(NS, identity["source_id"], "domain", "https://www.factdesk.example/",
                       reason="publisher's own website, reviewed", reviewer_id="curator",
                       scopes={REVIEW_SCOPE, READ_SCOPE})
    return identity["source_id"]


def world(*, v2: bool = False, news: bool = True, sources: bool = True):
    """Everything loaded: fact-check sources (v1, optionally v2), news, claims, entities and a source identity."""
    conn = connection()
    load_all(conn)
    if v2:
        load_all(conn, version="v2")
    if news:
        load_news(conn)
    if sources:
        load_source_identity(conn)
    return conn


def accepted_world(*, v2: bool = True):
    """The world with every identifier-, domain- and appearance-based candidate accepted by a reviewer, the
    unrelated claim sharing the article rejected, and name-only claimant candidates left unreviewed."""
    from src.kb.fact_checks_identity import FactCheckIdentity

    conn = world(v2=v2)
    identity = FactCheckIdentity(conn)
    for candidate in identity.propose(NS, principal_id="alice", scopes=SCOPES)["candidates"]:
        if candidate["method"] == "name-as-published":
            continue
        decision = "reject" if candidate["right_key"] == "claim-weather" else "accept"
        identity.review(NS, candidate["candidate_id"], decision, "fixture review", principal_id="rev",
                        scopes=REVIEW_SCOPES)
    return conn
