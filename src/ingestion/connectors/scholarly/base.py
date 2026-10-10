"""Declarative scholarly-source connector base.

A large, growing family of scholarly APIs (OpenAlex, Crossref, Semantic Scholar,
Europe PMC, PubMed, bioRxiv/medRxiv, DOAJ, CORE, DBLP, HAL, OpenAIRE, PLOS,
Zenodo, …) all answer the same shape of question -- *"papers about <topic>
published between <since> and <until>"* -- and return JSON records that map onto
the same ``source_type="paper"`` ``Document``. Rather than a bespoke module per
source, each source is described by a :class:`ScholarlySource` spec and gets a
thin registered :class:`ScholarlyConnector` subclass.

Recency here is by **publication date** (``Document.created_at``), not ingestion
time: every source's query is date-windowed where the API allows, and parsed
records are additionally post-filtered on publication date so the window is
enforced uniformly even when an API ignores it.

Network safety mirrors the declarative REST connector: credential-free HTTPS
only, host allowlisted per source, and the resolved address must be public
(no SSRF to private/loopback ranges). ``http_get`` is injectable for offline
tests.
"""
from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, List, Mapping, Optional, Sequence, Union, Tuple

from services.ingest.common.document_model import Document
from src.ingestion.connectors.base import (
    Connector,
    PermanentFetchError,
    RawDocument,
    SourceRef,
)

CONTRACT = "noesis-scholarly-source-v1"
DEFAULT_LIMIT = 50
#: Default per-source ceiling (one request's worth for non-paging sources).
MAX_LIMIT = 200
#: Absolute ceiling for any query; paging sources raise their own ``max_limit``
#: up to this value so systematic searches can retrieve a full result set.
HARD_MAX_LIMIT = 10_000
#: Search scopes a query may request; each source declares which it supports.
SCOPES = ("title_abstract",)
#: Result orderings a query may request instead of the source default.
ORDERS = ("relevance",)
_TIMEOUT_S = 30
#: Transient server errors are retried with exponential backoff before a
#: paged query is abandoned (a systematic harvest can make hundreds of requests).
_RETRY_STATUSES = (500, 502, 503, 504)
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_S = 2.0
_MAX_BYTES = 8 * 1024 * 1024


# --------------------------------------------------------------------------- #
# Query
# --------------------------------------------------------------------------- #
@dataclass
class ScholarlyQuery:
    """A topic + publication-date window. ``since``/``until`` are ``YYYY-MM-DD``."""

    topic: str
    since: Optional[str] = None
    until: Optional[str] = None
    limit: int = DEFAULT_LIMIT
    #: ``None`` = the API's default fields; ``"title_abstract"`` = restrict
    #: matching to title and abstract (sources must support it explicitly).
    scope: Optional[str] = None
    #: ``None`` = the source's default order (usually newest first);
    #: ``"relevance"`` = the API's relevance ranking (sources must support it).
    order: Optional[str] = None

    @classmethod
    def coerce(cls, query: Union[str, Mapping[str, Any], "ScholarlyQuery", None]) -> Optional["ScholarlyQuery"]:
        if query is None:
            return None
        if isinstance(query, ScholarlyQuery):
            return query
        if isinstance(query, str):
            return cls(topic=query)
        if isinstance(query, Mapping):
            topic = query.get("topic") or query.get("q") or query.get("query")
            if not topic:
                return None
            limit = int(query.get("limit", DEFAULT_LIMIT) or DEFAULT_LIMIT)
            scope = query.get("scope") or None
            if scope is not None and scope not in SCOPES:
                raise ValueError(f"unknown search scope {scope!r}; expected one of {SCOPES}")
            order = query.get("order") or None
            if order is not None and order not in ORDERS:
                raise ValueError(f"unknown result order {order!r}; expected one of {ORDERS}")
            return cls(
                topic=str(topic),
                since=_norm_date(query.get("since") or query.get("from")),
                until=_norm_date(query.get("until") or query.get("to")),
                limit=max(1, min(limit, HARD_MAX_LIMIT)),
                scope=scope,
                order=order,
            )
        return None


# --------------------------------------------------------------------------- #
# Source spec
# --------------------------------------------------------------------------- #
@dataclass
class ScholarlySource:
    """Declarative description of one scholarly API.

    ``build_url`` turns a :class:`ScholarlyQuery` into a request URL (and is the
    single place per-source query syntax lives). ``results_path`` locates the
    record list in the JSON response; the ``*_path`` fields are dotted paths into
    each record. ``date_path`` must resolve to a publication date the connector
    can parse (ISO date/datetime, epoch, or ``[year, month, day]`` parts).
    """

    name: str
    allowed_host: str
    build_url: Callable[[ScholarlyQuery], str]
    results_path: str = ""
    id_path: str = "id"
    title_path: str = "title"
    abstract_path: Optional[str] = None
    authors_path: Optional[str] = None
    author_name_key: Optional[str] = None  # when authors are objects
    date_path: str = "published"
    doi_path: Optional[str] = None
    url_path: Optional[str] = None
    pdf_path: Optional[str] = None
    venue_path: Optional[str] = None
    language_path: Optional[str] = None
    default_language: str = "en"
    requires_env: Optional[str] = None  # env var holding an API key, if any
    api_key_header: Optional[str] = None  # send key in this header ...
    api_key_prefix: str = ""              # ... with this prefix (e.g. "Bearer ")
    api_key_query: Optional[str] = None   # ... or as this query parameter
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    # Optional per-source hooks for APIs that don't fit the pure JSON-path model.
    transform_records: Optional[Callable[[Any], Sequence[Mapping[str, Any]]]] = None
    #: Most records one query may return from this source. Non-paging sources
    #: keep the one-request default; paging sources raise it.
    max_limit: int = MAX_LIMIT
    #: Dotted path to the API's total hit count, recorded on every document as
    #: ``source_total_results`` so a truncated result set is always visible.
    total_path: Optional[str] = None
    #: ``next_page(query, body, url, fetched) -> next URL or None``; when set,
    #: ``fetch`` follows pages until ``query.limit`` records or the end.
    next_page: Optional[Callable[["ScholarlyQuery", Any, str, int], Optional[str]]] = None
    #: Search scopes (see :data:`SCOPES`) this source can honour.
    scopes: Tuple[str, ...] = ()
    #: Result orders (see :data:`ORDERS`) this source can honour.
    orders: Tuple[str, ...] = ()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _norm_date(value: Any) -> Optional[str]:
    if not value:
        return None
    text = str(value)[:10]
    try:
        datetime.strptime(text, "%Y-%m-%d")
        return text
    except ValueError:
        return None


def _get(record: Any, path: Optional[str], default: Any = None) -> Any:
    if not path:
        return default
    current = record
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return default
        if current is None:
            return default
    return current


def _to_millis(value: Any) -> Optional[int]:
    """Parse a publication date (ISO string, epoch seconds/ms, or [y,m,d]) to ms."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return int(v * 1000) if v < 1e12 else int(v)
    if isinstance(value, (list, tuple)) and value:
        parts = list(value) + [1, 1]
        try:
            y, m, d = int(parts[0]), int(parts[1] or 1), int(parts[2] or 1)
            return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)
        except (ValueError, TypeError):
            return None
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S",
                "%Y/%m/%d %H:%M",  # PubMed esummary sortpubdate, e.g. "2019/01/01 00:00"
                "%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y"):
        try:
            dt = datetime.strptime(text[:len(fmt) + 6], fmt) if "%z" in fmt else datetime.strptime(text[:len(text)], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            continue
    return None


def _millis_bounds(query: ScholarlyQuery) -> tuple[Optional[int], Optional[int]]:
    lo = _to_millis(query.since) if query.since else None
    hi = None
    if query.until:
        hi = _to_millis(query.until)
        if hi is not None:
            hi += 86_400_000 - 1  # inclusive end-of-day
    return lo, hi


def _assert_public_https(url: str, allowed_host: str,
                         resolver: Optional[Callable[[str], List[str]]] = None) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise PermanentFetchError("scholarly source URL must be credential-free HTTPS")
    host = parsed.hostname.casefold()
    allowed = allowed_host.casefold()
    if host != allowed and not host.endswith("." + allowed):
        raise PermanentFetchError(f"host {host!r} is not allowlisted for this source ({allowed})")
    resolve = resolver or (lambda h: [i[4][0] for i in socket.getaddrinfo(h, 443, type=socket.SOCK_STREAM)])
    for address in resolve(host):
        ip = ipaddress.ip_address(address)
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
                or ip.is_reserved or ip.is_unspecified):
            raise PermanentFetchError("scholarly source resolves to a non-public address")


def _default_http_get(url: str, headers: Mapping[str, str]) -> bytes:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
        return response.read(_MAX_BYTES + 1)[:_MAX_BYTES]


# --------------------------------------------------------------------------- #
# Connector
# --------------------------------------------------------------------------- #
class ScholarlyConnector(Connector):
    """Base for one declarative scholarly source. Subclasses set ``SOURCE``.

    ``query`` (to ``discover``/``harvest``) is a topic string or a mapping
    ``{topic, since?, until?, limit?}`` (see :class:`ScholarlyQuery`).
    """

    source_type = "paper"
    SOURCE: ScholarlySource  # set by subclasses

    def __init__(self, http_get: Optional[Callable[[str, Mapping[str, str]], bytes]] = None,
                 dns_resolver: Optional[Callable[[str], List[str]]] = None,
                 api_key: Optional[str] = None):
        from src.ingestion.quota import metered_get

        self._http_get = metered_get(http_get or _default_http_get)
        self._resolver = dns_resolver
        self._api_key = api_key

    # -- interface ---------------------------------------------------------- #
    def discover(self, query: Union[str, Mapping[str, Any], None] = None) -> Iterable[SourceRef]:
        coerced = ScholarlyQuery.coerce(query)
        if coerced is None:
            return
        coerced = _for_source(coerced, self.SOURCE)
        url = self.SOURCE.build_url(coerced)
        yield SourceRef(
            locator=url,
            title=f"{self.SOURCE.name}:{coerced.topic}",
            metadata={"source_id": self.SOURCE.name, "query": _query_dict(coerced)},
        )

    def fetch(self, ref: SourceRef) -> RawDocument:
        import os

        source = self.SOURCE
        key = self._api_key or (os.getenv(source.requires_env) if source.requires_env else None)
        if source.requires_env and not key:
            raise PermanentFetchError(
                f"{source.name} needs {source.requires_env}; skipping (set the key to enable)"
            )
        headers = {"Accept": "application/json", "User-Agent": _user_agent(), **dict(source.extra_headers)}
        if key and source.api_key_header:
            headers[source.api_key_header] = f"{source.api_key_prefix}{key}"

        def get(url: str) -> bytes:
            if key and source.api_key_query:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}{source.api_key_query}={enc(key)}"
            _assert_public_https(url, source.allowed_host, self._resolver)
            return get_with_retry(self._http_get, url, headers)

        if source.next_page is None:
            return RawDocument(ref=ref, content=get(ref.locator), content_type="application/json")

        # Paging source: follow pages until the query limit or the end of results.
        query = ScholarlyQuery.coerce((ref.metadata or {}).get("query") or {})
        records: List[Any] = []
        total = None
        unretrieved = 0
        url: Optional[str] = ref.locator
        pages = 0

        while url and len(records) < query.limit:
            try:
                body = json.loads(get(url).decode("utf-8", "replace"))
            except urllib.error.HTTPError as exc:
                # A later page that keeps failing after retries ends the query: keep
                # what was read and record the shortfall, so the gap is visible in
                # the search receipt instead of losing the whole result set.
                if pages == 0 or exc.code not in _RETRY_STATUSES:
                    raise
                if total is not None:
                    unretrieved = max(min(total, query.limit) - len(records), 0)
                break
            pages += 1
            if total is None and source.total_path:
                total = _as_int(_get(body, source.total_path))
            batch = _page_records(source, body)
            if not batch:
                break
            records.extend(batch)
            url = source.next_page(query, body, url, len(records))
        combined = {"__noesis_paged__": pages, "records": records[:query.limit], "total": total,
                    "unretrieved": unretrieved}
        return RawDocument(ref=ref, content=json.dumps(combined), content_type="application/json")

    def parse(self, raw: RawDocument) -> List[Document]:
        source = self.SOURCE
        content = raw.content
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        try:
            body = json.loads(content)
        except json.JSONDecodeError:
            return []
        unretrieved = None
        if isinstance(body, dict) and "__noesis_paged__" in body:
            records, total = body.get("records") or [], _as_int(body.get("total"))
            unretrieved = _as_int(body.get("unretrieved")) or None
        else:
            records = _page_records(source, body)
            total = _as_int(_get(body, source.total_path)) if source.total_path else None
        if not isinstance(records, list):
            return []

        query = ScholarlyQuery.coerce((raw.ref.metadata or {}).get("query") or {})
        lo, hi = _millis_bounds(query) if query else (None, None)

        documents: List[Document] = []
        for record in records:
            document = self._record_to_document(record, raw.fetched_at)
            if document is None:
                continue
            year = _year_only(_get(record, source.date_path))
            if year is not None:
                # A year-only date can't be placed inside the year, so the record is
                # kept when its year overlaps the window and left for screening
                # (e.g. a DOAJ "2022" record for a window starting 2022-11-30).
                if lo is not None and year < _utc_year(lo):
                    continue
                if hi is not None and year > _utc_year(hi):
                    continue
            else:
                published = document.created_at
                if lo is not None and (published is None or published < lo):
                    continue
                if hi is not None and (published is None or published > hi):
                    continue
            extra = {"source_total_results": total, "search_scope": query.scope if query else None,
                     "result_order": query.order if query else None,
                     "publication_date_precision": "year" if year is not None else None,
                     "source_unretrieved_records": unretrieved}
            document.metadata = {**document.metadata, **{k: v for k, v in extra.items() if v is not None}}
            documents.append(document)
        return documents

    # -- mapping ------------------------------------------------------------ #
    def _record_to_document(self, record: Mapping[str, Any], fetched_at: int) -> Optional[Document]:
        source = self.SOURCE
        title = _get(record, source.title_path)
        if isinstance(title, list):
            title = title[0] if title else None
        if not title:
            return None

        raw_id = _get(record, source.id_path) or _get(record, source.doi_path or "") or _get(record, source.url_path or "")
        if not raw_id:
            return None
        doi = _get(record, source.doi_path or "")
        work_id = ("doi:" + str(doi).lower().replace("https://doi.org/", "")
                   if doi else f"{source.name}:{raw_id}")

        authors = _extract_authors(record, source)
        abstract = _get(record, source.abstract_path or "")
        if isinstance(abstract, list):
            abstract = " ".join(str(x) for x in abstract)
        url = _get(record, source.url_path or "") or (
            f"https://doi.org/{str(doi).replace('https://doi.org/', '')}" if doi else None)
        pdf_url = _get(record, source.pdf_path or "")
        venue = _get(record, source.venue_path or "")
        language = _get(record, source.language_path or "") or source.default_language

        metadata = {
            "source_api": source.name,
            "work_identifier": work_id,
            "doi": (str(doi).replace("https://doi.org/", "") if doi else None),
            "venue": venue,
            "content_coverage": "abstract-only" if abstract else "metadata-only",
            "external_id": str(raw_id),
        }
        metadata = {k: v for k, v in metadata.items() if v not in (None, "", [])}

        return Document(
            document_id=_document_id(source.name, raw_id, doi),
            source_type="paper",
            language=str(language)[:8] or "en",
            ingested_at=fetched_at,
            source_id=source.name,
            url=str(url) if url else None,
            title=str(title),
            content=str(abstract) if abstract else None,
            content_ref=str(pdf_url) if pdf_url else None,
            authors=authors,
            created_at=_to_millis(_get(record, source.date_path)),
            metadata=metadata,
        )


# --------------------------------------------------------------------------- #
# Small utilities used by the connector
# --------------------------------------------------------------------------- #
def _extract_authors(record: Mapping[str, Any], source: ScholarlySource) -> List[str]:
    raw = _get(record, source.authors_path or "")
    if not raw:
        return []
    names: List[str] = []
    for item in raw if isinstance(raw, list) else [raw]:
        if isinstance(item, Mapping):
            name = (_get(item, source.author_name_key) if source.author_name_key else None) \
                or item.get("name") or item.get("display_name") or item.get("full_name") \
                or " ".join(str(item.get(k, "")) for k in ("given", "family")).strip()
            if name:
                names.append(str(name))
        elif item:
            names.append(str(item))
    return names


def _document_id(source_name: str, raw_id: str, doi: Any) -> str:
    import hashlib

    if doi:
        # One id per DOI across sources: OpenAlex gives "https://doi.org/10.x", Crossref "10.x".
        doi = str(doi).strip().lower()
        for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi:"):
            if doi.startswith(prefix):
                doi = doi[len(prefix):]
    basis = ("doi:" + doi if doi else f"{source_name}:{raw_id}")
    return "paper:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:24]


def get_with_retry(get: Callable[[str, Mapping[str, str]], bytes], url: str,
                   headers: Mapping[str, str]) -> bytes:
    """Call ``get``, retrying transient 5xx responses with exponential backoff."""
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return get(url, headers)
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRY_STATUSES or attempt == _RETRY_ATTEMPTS - 1:
                raise
            time.sleep(_RETRY_BACKOFF_S * (2 ** attempt))
    raise AssertionError("unreachable")


def _query_dict(query: ScholarlyQuery) -> dict:
    out = {"topic": query.topic, "since": query.since, "until": query.until, "limit": query.limit}
    if query.scope:
        out["scope"] = query.scope
    if query.order:
        out["order"] = query.order
    return out


def _for_source(query: ScholarlyQuery, source: "ScholarlySource") -> ScholarlyQuery:
    """Clamp the limit to what ``source`` can return and refuse unsupported scopes."""
    if query.scope and query.scope not in source.scopes:
        raise ValueError(f"{source.name} does not support search scope {query.scope!r}")
    if query.order and query.order not in source.orders:
        raise ValueError(f"{source.name} does not support result order {query.order!r}")
    return dataclasses.replace(query, limit=max(1, min(query.limit, source.max_limit)))


def _page_records(source: "ScholarlySource", body: Any) -> List[Any]:
    records = (source.transform_records(body) if source.transform_records
               else _get(body, source.results_path, body if not source.results_path else []))
    return records if isinstance(records, list) else []


def _year_only(value: Any) -> Optional[int]:
    """The year, if ``value`` is a bare year ("2022", 2022 or [2022]); else ``None``."""
    if isinstance(value, (list, tuple)):
        value = value[0] if len(value) == 1 else None
    text = str(value).strip() if value is not None else ""
    return int(text) if len(text) == 4 and text.isdigit() else None


def _utc_year(millis: int) -> int:
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).year


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _user_agent() -> str:
    import os

    contact = os.getenv("NOESIS_SCHOLARLY_CONTACT", "noesis@example.org")
    return f"noesis-scholarly-connector/1.0 (mailto:{contact})"


def enc(value: str) -> str:
    """URL-encode a query fragment (exposed for source specs)."""
    return urllib.parse.quote(str(value), safe="")
