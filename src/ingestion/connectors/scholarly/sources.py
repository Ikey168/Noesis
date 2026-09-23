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
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, List, Mapping, Union

from services.ingest.common.document_model import Document
from src.ingestion.connectors.base import RawDocument, SourceRef
from src.ingestion.connectors.registry import register_connector
from src.ingestion.connectors.scholarly.base import (
    DEFAULT_LIMIT,
    ScholarlyConnector,
    ScholarlyQuery,
    ScholarlySource,
    _to_millis,
    enc,
)


def _win(q: ScholarlyQuery, default_days: int = 3650) -> tuple[str, str]:
    """Resolve an inclusive publication-date window as YYYY-MM-DD strings."""
    until = q.until or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    since = q.since or (datetime.strptime(until, "%Y-%m-%d") - timedelta(days=default_days)).strftime("%Y-%m-%d")
    return since, until


# --------------------------------------------------------------------------- #
# Declarative JSON-search sources
# --------------------------------------------------------------------------- #
OPENALEX = ScholarlySource(
    name="openalex",
    allowed_host="api.openalex.org",
    build_url=lambda q: (
        "https://api.openalex.org/works?search=" + enc(q.topic)
        + "&filter=from_publication_date:%s,to_publication_date:%s" % _win(q)
        + "&per_page=%d" % min(q.limit, 100)
    ),
    results_path="results",
    id_path="id",
    title_path="display_name",
    abstract_inverted_index_path="abstract_inverted_index",
    authors_path="authorships",
    author_name_key="author.display_name",
    date_path="publication_date",
    doi_path="doi",
    url_path="id",
    pdf_path="open_access.oa_url",
    venue_path="primary_location.source.display_name",
    references_path="referenced_works",
    citation_count_path="cited_by_count",
    language_path="language",
    optional_env="OPENALEX_API_KEY",
    api_key_header="Authorization",
    api_key_prefix="Bearer ",
)

CROSSREF = ScholarlySource(
    name="crossref",
    allowed_host="api.crossref.org",
    build_url=lambda q: (
        "https://api.crossref.org/works?query.bibliographic=" + enc(q.topic)
        + "&filter=from-pub-date:%s,until-pub-date:%s" % _win(q)
        + "&rows=%d&sort=score&order=desc" % q.limit
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
    references_path="reference",
    reference_id_path="DOI",
    citation_count_path="is-referenced-by-count",
)

SEMANTIC_SCHOLAR = ScholarlySource(
    name="semantic_scholar",
    allowed_host="api.semanticscholar.org",
    build_url=lambda q: (
        "https://api.semanticscholar.org/graph/v1/paper/search?query=" + enc(q.topic)
        + "&fields=title,abstract,authors,externalIds,url,venue,publicationDate,openAccessPdf,citationCount"
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
    citation_count_path="citationCount",
    optional_env="SEMANTIC_SCHOLAR_API_KEY",
    api_key_header="x-api-key",
)

EUROPE_PMC = ScholarlySource(
    name="europepmc",
    allowed_host="www.ebi.ac.uk",
    build_url=lambda q: (
        "https://www.ebi.ac.uk/europepmc/webservices/rest/search?query="
        + enc("%s AND (FIRST_PDATE:[%s TO %s])" % (q.topic, *_win(q)))
        + "&format=json&resultType=core&pageSize=%d" % min(q.limit, 100)
    ),
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

DOAJ = ScholarlySource(
    name="doaj",
    allowed_host="doaj.org",
    build_url=lambda q: (
        "https://doaj.org/api/search/articles/" + enc(q.topic)
        + "?pageSize=%d" % min(q.limit, 100)
    ),
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
        + "&rows=%d&wt=json" % min(q.limit, 100)
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
        + "&rows=%d&wt=json" % min(q.limit, 100)
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
        + "&size=%d&sort=bestmatch" % min(q.limit, 25)
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

CORE = ScholarlySource(
    name="core",
    allowed_host="api.core.ac.uk",
    build_url=lambda q: (
        "https://api.core.ac.uk/v3/search/works?q="
        + enc("%s AND publishedDate>=%s AND publishedDate<=%s" % (q.topic, *_win(q)))
        + "&limit=%d" % min(q.limit, 100)
    ),
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
                             build_url=lambda q: "")  # unused; custom fetch below

    _EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def discover(self, query: Union[str, Mapping[str, Any], None] = None) -> Iterable[SourceRef]:
        q = ScholarlyQuery.coerce(query)
        if q is None:
            return
        yield SourceRef(locator="pubmed", metadata={"source_id": "pubmed", "query": {
            "topic": q.topic, "since": q.since, "until": q.until, "limit": q.limit}})

    def _key_param(self) -> str:
        key = self._api_key or os.getenv("NCBI_API_KEY")
        return "&api_key=" + enc(key) if key else ""

    def fetch(self, ref: SourceRef) -> RawDocument:
        from src.ingestion.connectors.scholarly.base import _assert_public_https, _default_http_get, _user_agent
        q = ScholarlyQuery.coerce((ref.metadata or {}).get("query") or {})
        since, until = _win(q)
        term = "%s AND (%s[PDAT] : %s[PDAT])" % (q.topic, since.replace("-", "/"), until.replace("-", "/"))
        esearch = (f"{self._EUTILS}/esearch.fcgi?db=pubmed&retmode=json&sort=pub+date"
                   f"&retmax={min(q.limit, 100)}&term={enc(term)}" + self._key_param())
        headers = {"Accept": "application/json", "User-Agent": _user_agent()}
        get = self._http_get or _default_http_get
        _assert_public_https(esearch, self.SOURCE.allowed_host, self._resolver)
        ids = (json.loads(get(esearch, headers).decode("utf-8", "replace"))
               .get("esearchresult", {}).get("idlist", []))
        if not ids:
            return RawDocument(ref=ref, content=json.dumps({"result": {}}), content_type="application/json")
        esummary = (f"{self._EUTILS}/esummary.fcgi?db=pubmed&retmode=json"
                    f"&id={','.join(ids)}" + self._key_param())
        _assert_public_https(esummary, self.SOURCE.allowed_host, self._resolver)
        return RawDocument(ref=ref, content=get(esummary, headers), content_type="application/json")

    def parse(self, raw: RawDocument) -> List[Document]:
        content = raw.content
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        result = (json.loads(content) if content else {}).get("result", {})
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


#: All scholarly connector registry names provided by this module.
SCHOLARLY_SOURCES = [
    "openalex", "crossref", "semantic_scholar", "europepmc", "pubmed",
    "biorxiv", "medrxiv", "doaj", "core", "dblp", "hal", "plos", "zenodo",
]
