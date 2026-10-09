"""Concrete scholarly-source connectors (source_type="paper").

Each source is a :class:`ScholarlySource` spec bound to a registered
:class:`ScholarlyConnector` subclass, resolvable by ``get_connector(<name>)``.
All recency is by **publication date**. Sources with a native date filter apply
it in the query; every source is also post-filtered on publication date by the
base class, so a `{topic, since, until}` window is honoured uniformly.

Add a new source by appending a spec + a one-line subclass at the bottom.
"""
from __future__ import annotations

import json
import os
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, List, Mapping, Optional, Union

from services.ingest.common.document_model import Document
from src.ingestion.connectors.base import PermanentFetchError, RawDocument, SourceRef
from src.ingestion.connectors.registry import register_connector
from src.ingestion.connectors.scholarly.base import (
    DEFAULT_LIMIT,
    HARD_MAX_LIMIT,
    ScholarlyConnector,
    ScholarlyQuery,
    ScholarlySource,
    _get,
    _to_millis,
    enc,
    get_with_retry,
)


def _with_param(url: str, key: str, value: Any) -> str:
    """Return ``url`` with query parameter ``key`` set to ``value`` (added or replaced)."""
    parts = urllib.parse.urlsplit(url)
    params = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != key]
    params.append((key, str(value)))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(params, quote_via=urllib.parse.quote)))


def _param(url: str, key: str) -> Optional[str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get(key)


def _win(q: ScholarlyQuery, default_days: int = 3650) -> tuple[str, str]:
    """Resolve an inclusive publication-date window as YYYY-MM-DD strings."""
    until = q.until or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    since = q.since or (datetime.strptime(until, "%Y-%m-%d") - timedelta(days=default_days)).strftime("%Y-%m-%d")
    return since, until


# --------------------------------------------------------------------------- #
# Declarative JSON-search sources
# --------------------------------------------------------------------------- #
def _openalex_url(q: ScholarlyQuery) -> str:
    window = "from_publication_date:%s,to_publication_date:%s" % _win(q)
    if q.scope == "title_abstract":
        if "," in q.topic:
            raise ValueError("openalex title_abstract scope: commas are not allowed in the topic (filter syntax)")
        head = "https://api.openalex.org/works?filter=title_and_abstract.search:" + enc(q.topic) + "," + window
    else:
        head = "https://api.openalex.org/works?search=" + enc(q.topic) + "&filter=" + window
    return head + "&per-page=%d&sort=publication_date:desc&cursor=*" % min(q.limit, 200)


def _openalex_next(q: ScholarlyQuery, body: Any, url: str, fetched: int) -> Optional[str]:
    cursor = ((body or {}).get("meta") or {}).get("next_cursor")
    return _with_param(url, "cursor", cursor) if cursor else None


OPENALEX = ScholarlySource(
    name="openalex",
    allowed_host="api.openalex.org",
    build_url=_openalex_url,
    next_page=_openalex_next,
    total_path="meta.count",
    max_limit=HARD_MAX_LIMIT,
    scopes=("title_abstract",),
    results_path="results",
    id_path="id",
    title_path="display_name",
    authors_path="authorships",
    author_name_key="author.display_name",
    date_path="publication_date",
    doi_path="doi",
    url_path="id",
    pdf_path="open_access.oa_url",
    venue_path="primary_location.source.display_name",
    language_path="language",
)

CROSSREF = ScholarlySource(
    name="crossref",
    allowed_host="api.crossref.org",
    build_url=lambda q: (
        "https://api.crossref.org/works?query=" + enc(q.topic)
        + "&filter=from-pub-date:%s,until-pub-date:%s,type:journal-article" % _win(q)
        + "&rows=%d&sort=published&order=desc" % q.limit
    ),
    results_path="message.items",
    id_path="DOI",
    title_path="title",
    abstract_path="abstract",
    authors_path="author",  # {given, family} handled by base
    date_path="published.date-parts.0",  # [y, m, d]
    doi_path="DOI",
    url_path="URL",
    venue_path="container-title",
)

SEMANTIC_SCHOLAR = ScholarlySource(
    name="semantic_scholar",
    allowed_host="api.semanticscholar.org",
    build_url=lambda q: (
        "https://api.semanticscholar.org/graph/v1/paper/search?query=" + enc(q.topic)
        + "&fields=title,abstract,authors,externalIds,url,venue,publicationDate,openAccessPdf"
        + "&publicationDateOrYear=%s:%s" % _win(q)
        + "&limit=%d" % min(q.limit, 100)
    ),
    results_path="data",
    id_path="paperId",
    title_path="title",
    abstract_path="abstract",
    authors_path="authors",
    author_name_key="name",
    date_path="publicationDate",
    doi_path="externalIds.DOI",
    url_path="url",
    pdf_path="openAccessPdf.url",
    venue_path="venue",
    requires_env="SEMANTIC_SCHOLAR_API_KEY",  # optional but strongly rate-limited without
    api_key_header="x-api-key",
)
# S2 works without a key (lower rate limit); drop the hard requirement.
SEMANTIC_SCHOLAR.requires_env = None

def _europepmc_url(q: ScholarlyQuery) -> str:
    topic = "TITLE_ABS:(%s)" % q.topic if q.scope == "title_abstract" else q.topic
    return ("https://www.ebi.ac.uk/europepmc/webservices/rest/search?query="
            + enc("%s AND (FIRST_PDATE:[%s TO %s])" % (topic, *_win(q)))
            + "&format=json&resultType=core&pageSize=%d&cursorMark=*" % min(q.limit, 1000))


def _europepmc_next(q: ScholarlyQuery, body: Any, url: str, fetched: int) -> Optional[str]:
    nxt = (body or {}).get("nextCursorMark")
    return _with_param(url, "cursorMark", nxt) if nxt and nxt != _param(url, "cursorMark") else None


EUROPE_PMC = ScholarlySource(
    name="europepmc",
    allowed_host="www.ebi.ac.uk",
    build_url=_europepmc_url,
    next_page=_europepmc_next,
    total_path="hitCount",
    max_limit=HARD_MAX_LIMIT,
    scopes=("title_abstract",),
    results_path="resultList.result",
    id_path="id",
    title_path="title",
    abstract_path="abstractText",
    authors_path="authorList.author",
    author_name_key="fullName",
    date_path="firstPublicationDate",
    doi_path="doi",
    venue_path="journalInfo.journal.title",
)

def _doaj_url(q: ScholarlyQuery) -> str:
    """DOAJ has no date parameter, so the window goes into the query as a
    ``bibjson.year`` range (year-granular; the post-filter enforces the exact
    dates). DOAJ rejects wildcards ("disallowed Lucene features")."""
    since, until = _win(q)
    query = "(%s) AND bibjson.year:[%s TO %s]" % (q.topic, since[:4], until[:4])
    return ("https://doaj.org/api/search/articles/" + enc(query)
            + "?pageSize=%d&page=1&sort=created_date:desc" % min(q.limit, 100))


def _doaj_next(q: ScholarlyQuery, body: Any, url: str, fetched: int) -> Optional[str]:
    total = (body or {}).get("total") or 0
    page = int(_param(url, "page") or 1)
    size = int(_param(url, "pageSize") or 100)
    return _with_param(url, "page", page + 1) if page * size < total else None


DOAJ = ScholarlySource(
    name="doaj",
    allowed_host="doaj.org",
    build_url=_doaj_url,
    next_page=_doaj_next,
    total_path="total",
    max_limit=HARD_MAX_LIMIT,
    results_path="results",
    id_path="id",
    title_path="bibjson.title",
    abstract_path="bibjson.abstract",
    authors_path="bibjson.author",
    author_name_key="name",
    date_path="bibjson.year",
    venue_path="bibjson.journal.title",
)

DBLP = ScholarlySource(
    name="dblp",
    allowed_host="dblp.org",
    build_url=lambda q: (
        "https://dblp.org/search/publ/api?q=" + enc(q.topic)
        + "&format=json&h=%d" % min(q.limit, 100)
    ),
    results_path="result.hits.hit",
    id_path="info.key",
    title_path="info.title",
    authors_path="info.authors.author",  # {@pid, text} or str
    author_name_key="text",
    date_path="info.year",
    doi_path="info.doi",
    url_path="info.ee",
    venue_path="info.venue",
)

HAL = ScholarlySource(
    name="hal",
    allowed_host="api.archives-ouvertes.fr",
    build_url=lambda q: (
        "https://api.archives-ouvertes.fr/search/?q=" + enc(q.topic)
        + "&fq=" + enc("producedDate_s:[%sT00:00:00Z TO %sT23:59:59Z]" % _win(q))
        + "&fl=" + enc("title_s,abstract_s,authFullName_s,doiId_s,uri_s,producedDate_s,journalTitle_s")
        + "&sort=" + enc("producedDate_s desc") + "&rows=%d&wt=json" % min(q.limit, 100)
    ),
    results_path="response.docs",
    id_path="uri_s",
    title_path="title_s",
    abstract_path="abstract_s",
    authors_path="authFullName_s",  # list of strings
    date_path="producedDate_s",
    doi_path="doiId_s",
    url_path="uri_s",
    venue_path="journalTitle_s",
)

PLOS = ScholarlySource(
    name="plos",
    allowed_host="api.plos.org",
    build_url=lambda q: (
        "https://api.plos.org/search?q=" + enc("everything:%s" % q.topic)
        + "&fq=" + enc("publication_date:[%sT00:00:00Z TO %sT23:59:59Z]" % _win(q))
        + "&fl=" + enc("id,title_display,abstract,author_display,publication_date,journal")
        + "&sort=" + enc("publication_date desc") + "&rows=%d&wt=json" % min(q.limit, 100)
    ),
    results_path="response.docs",
    id_path="id",
    title_path="title_display",
    abstract_path="abstract",
    authors_path="author_display",  # list of strings
    date_path="publication_date",
    doi_path="id",  # PLOS id is the DOI
    venue_path="journal",
)

ZENODO = ScholarlySource(
    name="zenodo",
    allowed_host="zenodo.org",
    build_url=lambda q: (
        "https://zenodo.org/api/records?q="
        + enc("%s AND publication_date:[%s TO %s]" % (q.topic, *_win(q)))
        + "&size=%d&sort=mostrecent" % min(q.limit, 100)
    ),
    results_path="hits.hits",
    id_path="id",
    title_path="metadata.title",
    abstract_path="metadata.description",
    authors_path="metadata.creators",
    author_name_key="name",
    date_path="metadata.publication_date",
    doi_path="doi",
    url_path="links.self_html",
    venue_path="metadata.journal.title",
)

def _core_query(q: ScholarlyQuery) -> str:
    """CORE v3 rejects ``publishedDate`` comparisons (HTTP 500), so filter by
    year and let the publication-date post-filter enforce the exact window.
    The topic is parenthesised because CORE ORs bare terms."""
    since, until = _win(q)
    return "(%s) AND yearPublished>=%s AND yearPublished<=%s" % (q.topic, since[:4], until[:4])


def _core_records(body: Any) -> list:
    """CORE often omits ``publishedDate`` but sets ``yearPublished``; without a
    date the post-filter would drop the record, so fall back to the year."""
    records = body.get("results") if isinstance(body, dict) else None
    if not isinstance(records, list):
        return []
    return [
        {**r, "publishedDate": str(r["yearPublished"])}
        if isinstance(r, dict) and not r.get("publishedDate") and r.get("yearPublished")
        else r
        for r in records
    ]


def _core_next(q: ScholarlyQuery, body: Any, url: str, fetched: int) -> Optional[str]:
    total = (body or {}).get("totalHits") or 0
    offset = int(_param(url, "offset") or 0) + int(_param(url, "limit") or 100)
    return _with_param(url, "offset", offset) if offset < total else None


CORE = ScholarlySource(
    name="core",
    allowed_host="api.core.ac.uk",
    build_url=lambda q: (
        "https://api.core.ac.uk/v3/search/works/?q="
        + enc(_core_query(q))
        + "&limit=%d&offset=0" % min(q.limit, 100)
    ),
    next_page=_core_next,
    total_path="totalHits",
    max_limit=HARD_MAX_LIMIT,
    transform_records=_core_records,
    results_path="results",
    id_path="id",
    title_path="title",
    abstract_path="abstract",
    authors_path="authors",
    author_name_key="name",
    date_path="publishedDate",
    doi_path="doi",
    url_path="downloadUrl",
    venue_path="publisher",
    requires_env="CORE_API_KEY",
    api_key_header="Authorization",
    api_key_prefix="Bearer ",
)


def _spec_connector(spec: ScholarlySource):
    cls = type(
        "".join(p.capitalize() for p in spec.name.split("_")) + "Connector",
        (ScholarlyConnector,),
        {"name": spec.name, "SOURCE": spec, "__doc__": f"{spec.name} scholarly connector (source_type=paper)."},
    )
    return register_connector(cls)


OpenalexConnector = _spec_connector(OPENALEX)
CrossrefConnector = _spec_connector(CROSSREF)
SemanticScholarConnector = _spec_connector(SEMANTIC_SCHOLAR)
EuropepmcConnector = _spec_connector(EUROPE_PMC)
DoajConnector = _spec_connector(DOAJ)
DblpConnector = _spec_connector(DBLP)
HalConnector = _spec_connector(HAL)
PlosConnector = _spec_connector(PLOS)
ZenodoConnector = _spec_connector(ZENODO)
CoreConnector = _spec_connector(CORE)


# --------------------------------------------------------------------------- #
# PubMed — NCBI E-utilities (esearch -> esummary, JSON, two-step)
# --------------------------------------------------------------------------- #
@register_connector
class PubmedConnector(ScholarlyConnector):
    """PubMed via NCBI E-utilities. Optional NCBI_API_KEY raises the rate limit."""

    name = "pubmed"
    source_type = "paper"
    SOURCE = ScholarlySource(name="pubmed", allowed_host="eutils.ncbi.nlm.nih.gov",
                             build_url=lambda q: "",  # unused; custom fetch below
                             max_limit=HARD_MAX_LIMIT)  # esearch returns up to 10,000 ids
    SUMMARY_BATCH = 200  # ids per esummary request (keeps URLs short)

    _EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def discover(self, query: Union[str, Mapping[str, Any], None] = None) -> Iterable[SourceRef]:
        from src.ingestion.connectors.scholarly.base import _for_source, _query_dict
        q = ScholarlyQuery.coerce(query)
        if q is None:
            return
        q = _for_source(q, self.SOURCE)
        yield SourceRef(locator="pubmed", metadata={"source_id": "pubmed", "query": _query_dict(q)})

    def _key_param(self) -> str:
        key = self._api_key or os.getenv("NCBI_API_KEY")
        return "&api_key=" + enc(key) if key else ""

    def fetch(self, ref: SourceRef) -> RawDocument:
        from src.ingestion.connectors.scholarly.base import _assert_public_https, _default_http_get, _user_agent
        q = ScholarlyQuery.coerce((ref.metadata or {}).get("query") or {})
        since, until = _win(q)
        term = "%s AND (%s[PDAT] : %s[PDAT])" % (q.topic, since.replace("-", "/"), until.replace("-", "/"))
        esearch = (f"{self._EUTILS}/esearch.fcgi?db=pubmed&retmode=json&sort=pub+date"
                   f"&retmax={min(q.limit, HARD_MAX_LIMIT)}&term={enc(term)}" + self._key_param())
        headers = {"Accept": "application/json", "User-Agent": _user_agent()}
        raw_get = self._http_get or _default_http_get
        get = lambda url, headers: get_with_retry(raw_get, url, headers)
        _assert_public_https(esearch, self.SOURCE.allowed_host, self._resolver)
        found = json.loads(get(esearch, headers).decode("utf-8", "replace")).get("esearchresult", {})
        ids = found.get("idlist", [])[:q.limit]
        total = int(found["count"]) if str(found.get("count", "")).isdigit() else None
        result: dict = {"uids": []}
        for i in range(0, len(ids), self.SUMMARY_BATCH):
            esummary = (f"{self._EUTILS}/esummary.fcgi?db=pubmed&retmode=json"
                        f"&id={','.join(ids[i:i + self.SUMMARY_BATCH])}" + self._key_param())
            _assert_public_https(esummary, self.SOURCE.allowed_host, self._resolver)
            part = json.loads(get(esummary, headers).decode("utf-8", "replace")).get("result", {})
            result["uids"].extend(part.get("uids", []))
            result.update({k: v for k, v in part.items() if k != "uids"})
        return RawDocument(ref=ref, content=json.dumps({"result": result, "total": total}),
                           content_type="application/json")

    def parse(self, raw: RawDocument) -> List[Document]:
        content = raw.content
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        payload = json.loads(content) if content else {}
        result, total = payload.get("result", {}), payload.get("total")
        q = ScholarlyQuery.coerce((raw.ref.metadata or {}).get("query") or {})
        lo = _to_millis(q.since) if q and q.since else None
        hi = (_to_millis(q.until) + 86_400_000 - 1) if q and q.until else None
        docs: List[Document] = []
        for pmid in result.get("uids", []):
            rec = result.get(pmid, {})
            if not rec.get("title"):
                continue
            doi = next((x.get("value") for x in rec.get("articleids", [])
                        if x.get("idtype") == "doi"), None)
            published = _to_millis(rec.get("sortpubdate") or rec.get("pubdate"))
            if lo is not None and (published is None or published < lo):
                continue
            if hi is not None and (published is None or published > hi):
                continue
            from src.ingestion.connectors.scholarly.base import _document_id
            docs.append(Document(
                document_id=_document_id("pubmed", pmid, doi),
                source_type="paper", language="en", ingested_at=raw.fetched_at,
                source_id="pubmed", url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                title=str(rec["title"]).rstrip("."),
                content=None,  # esummary carries no abstract; efetch would
                authors=[a.get("name") for a in rec.get("authors", []) if a.get("name")],
                created_at=published,
                metadata={k: v for k, v in {
                    "source_api": "pubmed", "external_id": pmid,
                    "doi": doi, "work_identifier": ("doi:" + doi.lower()) if doi else f"pubmed:{pmid}",
                    "venue": rec.get("fulljournalname") or rec.get("source"),
                    "content_coverage": "metadata-only",
                    "source_total_results": total,
                }.items() if v}))
        return docs


# --------------------------------------------------------------------------- #
# bioRxiv / medRxiv — preprint servers (date-range API, topic filtered locally)
# --------------------------------------------------------------------------- #
class _RxivConnector(ScholarlyConnector):
    """Preprint servers expose papers by date range, not topic search, so we
    page a date window and filter titles/abstracts by the topic locally."""

    source_type = "paper"
    SERVER = ""  # "biorxiv" | "medrxiv"
    _HOST = "api.biorxiv.org"

    def discover(self, query: Union[str, Mapping[str, Any], None] = None) -> Iterable[SourceRef]:
        q = ScholarlyQuery.coerce(query)
        if q is None:
            return
        since, until = _win(q, default_days=60)  # preprint windows are short by nature
        yield SourceRef(locator=f"https://{self._HOST}/details/{self.SERVER}/{since}/{until}/0",
                        metadata={"source_id": self.name, "query": {
                            "topic": q.topic, "since": since, "until": until, "limit": q.limit}})

    def parse(self, raw: RawDocument) -> List[Document]:
        from src.ingestion.connectors.scholarly.base import _document_id
        content = raw.content
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        collection = (json.loads(content) if content else {}).get("collection", []) or []
        q = ScholarlyQuery.coerce((raw.ref.metadata or {}).get("query") or {})
        topic = (q.topic if q else "").lower()
        limit = q.limit if q else DEFAULT_LIMIT
        docs: List[Document] = []
        for rec in collection:
            hay = f"{rec.get('title', '')} {rec.get('abstract', '')} {rec.get('category', '')}".lower()
            if topic and topic not in hay:
                continue
            doi = rec.get("doi")
            docs.append(Document(
                document_id=_document_id(self.name, doi or rec.get("title", ""), doi),
                source_type="paper", language="en", ingested_at=raw.fetched_at,
                source_id=self.name,
                url=f"https://doi.org/{doi}" if doi else None,
                title=rec.get("title"),
                content=rec.get("abstract") or None,
                authors=[a.strip() for a in str(rec.get("authors", "")).split(";") if a.strip()],
                created_at=_to_millis(rec.get("date")),
                metadata={k: v for k, v in {
                    "source_api": self.name, "doi": doi, "venue": self.SERVER,
                    "category": rec.get("category"),
                    "work_identifier": ("doi:" + doi.lower()) if doi else f"{self.name}:{rec.get('title')}",
                    "content_coverage": "abstract-only" if rec.get("abstract") else "metadata-only",
                }.items() if v}))
            if len(docs) >= limit:
                break
        return docs


@register_connector
class BiorxivConnector(_RxivConnector):
    """bioRxiv preprints (source_type=paper), topic-filtered over a date window."""
    name = "biorxiv"
    SERVER = "biorxiv"
    SOURCE = ScholarlySource(name="biorxiv", allowed_host="api.biorxiv.org", build_url=lambda q: "")


@register_connector
class MedrxivConnector(_RxivConnector):
    """medRxiv preprints (source_type=paper), topic-filtered over a date window."""
    name = "medrxiv"
    SERVER = "medrxiv"
    SOURCE = ScholarlySource(name="medrxiv", allowed_host="api.biorxiv.org", build_url=lambda q: "")


# --------------------------------------------------------------------------- #
# Scopus — Elsevier Scopus Search API (STANDARD view, paged, key required)
# --------------------------------------------------------------------------- #
@register_connector
class ScopusConnector(ScholarlyConnector):
    """Scopus via the Elsevier Scopus Search API (needs ELSEVIER_API_KEY).

    Works with a key that has no institutional entitlement: the STANDARD view
    returns title, first author, venue, cover date and DOI, but no abstract, and
    at most 25 records per request, so ``fetch`` pages with ``start``.

    The topic is a Scopus advanced-search expression; a plain topic is wrapped in
    ``TITLE-ABS-KEY(...)``. Scopus filters dates by year only (``date=YYYY-YYYY``)
    and its cover date is the issue date, which can lie after online
    publication. Records are therefore kept when their cover date is after the
    window's ``until`` and flagged ``cover_date_after_window`` for screening;
    records before ``since`` are dropped. ``source_total_results`` reports the
    API's hit count, so a truncated harvest is visible in the search receipt.
    """

    name = "scopus"
    source_type = "paper"
    SOURCE = ScholarlySource(name="scopus", allowed_host="api.elsevier.com",
                             build_url=lambda q: "", requires_env="ELSEVIER_API_KEY",
                             max_limit=5000, scopes=("title_abstract",),
                             api_key_header="X-ELS-APIKey")

    _SEARCH = "https://api.elsevier.com/content/search/scopus"
    PAGE_SIZE = 25  # STANDARD-view maximum without an institutional entitlement
    MAX_RESULTS = 5000  # Scopus Search API: start + count may not exceed 5,000
    _FIELD_CODES = ("TITLE-ABS-KEY(", "TITLE(", "ABS(", "KEY(", "ALL(", "AUTH(", "DOI(", "SRCTITLE(")

    def discover(self, query: Union[str, Mapping[str, Any], None] = None) -> Iterable[SourceRef]:
        from src.ingestion.connectors.scholarly.base import _for_source, _query_dict
        q = ScholarlyQuery.coerce(query)
        if q is None:
            return
        q = _for_source(q, self.SOURCE)
        yield SourceRef(locator="scopus", title=f"scopus:{q.topic}", metadata={"source_id": "scopus", "query": _query_dict(q)})

    def _expression(self, topic: str, scope: Optional[str] = None) -> str:
        upper = topic.upper()
        if any(code in upper for code in self._FIELD_CODES):
            return topic
        return f"TITLE-ABS({topic})" if scope == "title_abstract" else f"TITLE-ABS-KEY({topic})"

    def fetch(self, ref: SourceRef) -> RawDocument:
        from src.ingestion.connectors.scholarly.base import _assert_public_https, _default_http_get, _user_agent
        key = self._api_key or os.getenv("ELSEVIER_API_KEY")
        if not key:
            raise PermanentFetchError("scopus needs ELSEVIER_API_KEY; skipping (set the key to enable)")
        q = ScholarlyQuery.coerce((ref.metadata or {}).get("query") or {})
        since, until = _win(q)
        headers = {"Accept": "application/json", "User-Agent": _user_agent(), "X-ELS-APIKey": key}
        raw_get = self._http_get or _default_http_get
        get = lambda url, headers: get_with_retry(raw_get, url, headers)
        entries: List[Mapping[str, Any]] = []
        total = None
        start = 0
        while len(entries) < q.limit:
            url = self._SEARCH + "?" + urllib.parse.urlencode({
                "query": self._expression(q.topic, q.scope), "date": f"{since[:4]}-{until[:4]}",
                "sort": "-coverDate", "view": "STANDARD", "start": start,
                "count": min(self.PAGE_SIZE, q.limit - len(entries))})
            _assert_public_https(url, self.SOURCE.allowed_host, self._resolver)
            page = json.loads(get(url, headers).decode("utf-8", "replace")).get("search-results", {})
            total = int(page.get("opensearch:totalResults") or 0)
            batch = [e for e in page.get("entry", []) if "error" not in e]
            entries.extend(batch)
            start += len(batch)
            if not batch or start >= total:
                break
        body = {"total": total, "entries": entries[:q.limit]}
        return RawDocument(ref=ref, content=json.dumps(body), content_type="application/json")

    def parse(self, raw: RawDocument) -> List[Document]:
        from src.ingestion.connectors.scholarly.base import _document_id
        content = raw.content
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        body = json.loads(content) if content else {}
        q = ScholarlyQuery.coerce((raw.ref.metadata or {}).get("query") or {})
        lo = _to_millis(q.since) if q and q.since else None
        hi = (_to_millis(q.until) + 86_400_000 - 1) if q and q.until else None
        docs: List[Document] = []
        for rec in body.get("entries", []):
            eid, title = rec.get("eid"), rec.get("dc:title")
            if not eid or not title:
                continue
            doi = rec.get("prism:doi")
            published = _to_millis(rec.get("prism:coverDate"))
            if lo is not None and (published is None or published < lo):
                continue
            link = next((l.get("@href") for l in rec.get("link", []) if l.get("@ref") == "scopus"), None)
            docs.append(Document(
                document_id=_document_id("scopus", eid, doi),
                source_type="paper", language="en", ingested_at=raw.fetched_at,
                source_id="scopus", url=link or (f"https://doi.org/{doi}" if doi else None),
                title=str(title),
                content=None,  # STANDARD view carries no abstract
                authors=[rec["dc:creator"]] if rec.get("dc:creator") else [],
                created_at=published,
                metadata={k: v for k, v in {
                    "source_api": "scopus", "external_id": eid,
                    "doi": doi, "work_identifier": ("doi:" + doi.lower()) if doi else f"scopus:{eid}",
                    "venue": rec.get("prism:publicationName"),
                    "document_type": rec.get("subtypeDescription"),
                    "content_coverage": "metadata-only",
                    "authors_coverage": "first-author-only",
                    "cover_date_after_window": bool(hi is not None and published is not None and published > hi),
                    "source_total_results": body.get("total"),
                }.items() if v not in (None, "", [], False)}))
        return docs


#: All scholarly connector registry names provided by this module.
SCHOLARLY_SOURCES = [
    "openalex", "crossref", "semantic_scholar", "europepmc", "pubmed",
    "biorxiv", "medrxiv", "doaj", "core", "dblp", "hal", "plos", "zenodo", "scopus",
]
