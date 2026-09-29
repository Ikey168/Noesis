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
import re
import socket
from html import unescape
import urllib.parse
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, List, Mapping, Optional, Sequence, Union

from services.ingest.common.document_model import Document
from src.ingestion.connectors.base import (
    Connector,
    PermanentFetchError,
    RawDocument,
    SourceRef,
)

CONTRACT = "noesis-scholarly-source-v1"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_TIMEOUT_S = 30
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
            return cls(
                topic=str(topic),
                since=_norm_date(query.get("since") or query.get("from")),
                until=_norm_date(query.get("until") or query.get("to")),
                limit=max(1, min(limit, MAX_LIMIT)),
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
    abstract_inverted_index_path: Optional[str] = None
    authors_path: Optional[str] = None
    author_name_key: Optional[str] = None  # when authors are objects
    date_path: str = "published"
    doi_path: Optional[str] = None
    url_path: Optional[str] = None
    pdf_path: Optional[str] = None
    venue_path: Optional[str] = None
    references_path: Optional[str] = None
    reference_id_path: Optional[str] = None
    citation_count_path: Optional[str] = None
    language_path: Optional[str] = None
    default_language: str = "en"
    requires_env: Optional[str] = None  # env var holding an API key, if any
    optional_env: Optional[str] = None  # use a key when available, allow keyless requests
    api_key_header: Optional[str] = None  # send key in this header ...
    api_key_prefix: str = ""              # ... with this prefix (e.g. "Bearer ")
    api_key_query: Optional[str] = None   # ... or as this query parameter
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    # Optional per-source hooks for APIs that don't fit the pure JSON-path model.
    transform_records: Optional[Callable[[Any], Sequence[Mapping[str, Any]]]] = None


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


def _abstract_from_inverted_index(value: Any) -> Optional[str]:
    """Restore the word order of an OpenAlex abstract, when one is supplied."""
    if not isinstance(value, Mapping):
        return None
    positions: dict[int, str] = {}
    for word, offsets in value.items():
        if not isinstance(word, str) or not isinstance(offsets, list):
            return None
        for offset in offsets:
            if type(offset) is not int or offset < 0 or offset > 20_000:
                return None
            positions[offset] = word
    if not positions or max(positions) + 1 != len(positions):
        return None
    return " ".join(positions[index] for index in range(len(positions)))


def _clean_abstract(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    plain = re.sub(r"<[^>]*>", " ", value)
    plain = " ".join(unescape(unescape(plain)).split())
    return plain or None


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


def _date_precision(value: Any) -> str:
    if isinstance(value, list):
        parts = value[0] if value and isinstance(value[0], list) else value
        n = sum(part is not None for part in parts)
        if n >= 3:
            return "day"
        if n == 2:
            return "month"
        return "year" if n == 1 else "unknown"
    text = str(value or "")
    if len(text) == 4 and text.isdigit():
        return "year"
    if len(text) == 7 and text[4] in "-/":
        return "month"
    return "day" if text else "unknown"


def _publication_end_ms(start_ms: int, precision: str) -> int:
    if precision == "day":
        return (start_ms // 86_400_000 + 1) * 86_400_000 - 1
    if precision in {"year", "month"}:
        start = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
        year, month = start.year, start.month
        if precision == "year":
            year, month = year + 1, 1
        elif month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1000) - 1
    return start_ms


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
        self._http_get = http_get or _default_http_get
        self._resolver = dns_resolver
        self._api_key = api_key

    # -- interface ---------------------------------------------------------- #
    def discover(self, query: Union[str, Mapping[str, Any], None] = None) -> Iterable[SourceRef]:
        coerced = ScholarlyQuery.coerce(query)
        if coerced is None:
            return
        url = self.SOURCE.build_url(coerced)
        yield SourceRef(
            locator=url,
            title=f"{self.SOURCE.name}:{coerced.topic}",
            metadata={"source_id": self.SOURCE.name, "query": _query_dict(coerced)},
        )

    def fetch(self, ref: SourceRef) -> RawDocument:
        import os

        source = self.SOURCE
        key_env = source.requires_env or source.optional_env
        key = self._api_key or (os.getenv(key_env) if key_env else None)
        if source.requires_env and not key:
            raise PermanentFetchError(
                f"{source.name} needs {source.requires_env}; skipping (set the key to enable)"
            )
        url = ref.locator
        headers = {"Accept": "application/json", "User-Agent": _user_agent(), **dict(source.extra_headers)}
        if key and source.api_key_header:
            headers[source.api_key_header] = f"{source.api_key_prefix}{key}"
        if key and source.api_key_query:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{source.api_key_query}={enc(key)}"
        _assert_public_https(url, source.allowed_host, self._resolver)
        try:
            payload = self._http_get(url, headers)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                hint = (
                    f"; configure {key_env} for a dedicated quota"
                    if key_env and not key else ""
                )
                raise PermanentFetchError(
                    f"{source.name} rate limited this request (HTTP 429){hint}"
                ) from exc
            raise PermanentFetchError(f"{source.name} returned HTTP {exc.code}") from exc
        return RawDocument(ref=ref, content=payload, content_type="application/json")

    def parse(self, raw: RawDocument) -> List[Document]:
        source = self.SOURCE
        content = raw.content
        if isinstance(content, bytes):
            content = content.decode("utf-8", "replace")
        try:
            body = json.loads(content)
        except json.JSONDecodeError:
            return []
        records = (source.transform_records(body) if source.transform_records
                   else _get(body, source.results_path, body if not source.results_path else []))
        if not isinstance(records, list):
            return []

        query = ScholarlyQuery.coerce((raw.ref.metadata or {}).get("query") or {})
        lo, hi = _millis_bounds(query) if query else (None, None)

        documents: List[Document] = []
        for record in records:
            document = self._record_to_document(record, raw.fetched_at)
            if document is None:
                continue
            published = document.created_at
            precision = document.metadata.get("publication_date_precision", "unknown")
            if lo is not None and (published is None or _publication_end_ms(published, precision) < lo):
                continue
            if hi is not None and (published is None or published > hi):
                continue
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
        if not abstract and source.abstract_inverted_index_path:
            abstract = _abstract_from_inverted_index(
                _get(record, source.abstract_inverted_index_path)
            )
        abstract = _clean_abstract(abstract)
        url = _get(record, source.url_path or "") or (
            f"https://doi.org/{str(doi).replace('https://doi.org/', '')}" if doi else None)
        pdf_url = _get(record, source.pdf_path or "")
        venue = _get(record, source.venue_path or "")
        if isinstance(venue, list):
            venue = next((str(item).strip() for item in venue if str(item).strip()), None)
        language = _get(record, source.language_path or "") or source.default_language
        raw_references = _get(record, source.references_path or "")
        references = []
        if isinstance(raw_references, list):
            for item in raw_references:
                identifier = (
                    _get(item, source.reference_id_path)
                    if isinstance(item, Mapping) else item
                )
                if identifier:
                    references.append(str(identifier).strip())
        citation_count = _get(record, source.citation_count_path or "")

        raw_date = _get(record, source.date_path)
        metadata = {
            "source_api": source.name,
            "work_identifier": work_id,
            "doi": (str(doi).replace("https://doi.org/", "") if doi else None),
            "venue": venue,
            "references": references or None,
            "citations": citation_count if type(citation_count) is int and citation_count >= 0 else None,
            "content_coverage": "abstract-only" if abstract else "metadata-only",
            "external_id": str(raw_id),
            "publication_date_precision": _date_precision(raw_date),
        }
        metadata = {k: v for k, v in metadata.items() if v not in (None, "", [])}

        return Document(
            document_id=_document_id(source.name, raw_id, doi),
            source_type="paper",
            language=str(language)[:8] or "en",
            ingested_at=fetched_at,
            source_id=source.name,
            url=str(url) if url else None,
            title=unescape(unescape(str(title))),
            content=str(abstract) if abstract else None,
            content_ref=str(pdf_url) if pdf_url else None,
            authors=authors,
            created_at=_to_millis(raw_date),
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

    normalized_doi = str(doi).strip().lower() if doi else ""
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/"):
        normalized_doi = normalized_doi.removeprefix(prefix)
    basis = "doi:" + normalized_doi if normalized_doi else f"{source_name}:{raw_id}"
    return "paper:" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:24]


def _query_dict(query: ScholarlyQuery) -> dict:
    return {"topic": query.topic, "since": query.since, "until": query.until, "limit": query.limit}


def _user_agent() -> str:
    import os

    contact = os.getenv("NOESIS_SCHOLARLY_CONTACT", "noesis@example.org")
    return f"noesis-scholarly-connector/1.0 (mailto:{contact})"


def enc(value: str) -> str:
    """URL-encode a query fragment (exposed for source specs)."""
    return urllib.parse.quote(str(value), safe="")
