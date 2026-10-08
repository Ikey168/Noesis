"""Abstract backfill by DOI for metadata-only scholarly records.

Some sources return no abstract (Scopus STANDARD view without an entitlement,
PubMed esummary). This module looks the abstract up by DOI, in order:

1. OpenAlex: batched ``filter=doi:a|b|…`` (abstract rebuilt from
   ``abstract_inverted_index``),
2. Crossref: ``/works/{doi}`` per DOI (JATS markup stripped),
3. Semantic Scholar: ``POST /graph/v1/paper/batch``.

Every abstract carries the provider it came from. A DOI no provider can
supply stays without an abstract and is reported as missing; nothing is
inferred. A provider error (rate limit, outage) is recorded and the next
provider is tried. Network safety follows the scholarly connectors:
credential-free HTTPS, a host allowlist per provider, public addresses only.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from services.ingest.common.document_model import Document
from src.ingestion.connectors.scholarly.base import (
    _TIMEOUT_S,
    _MAX_BYTES,
    _assert_public_https,
    _default_http_get,
    _user_agent,
)
from src.ingestion.connectors.base import PermanentFetchError

CONTRACT = "noesis-abstract-backfill-v1"
PROVIDERS = ("openalex", "crossref", "semantic_scholar")
HOSTS = {
    "openalex": "api.openalex.org",
    "crossref": "api.crossref.org",
    "semantic_scholar": "api.semanticscholar.org",
}
OPENALEX_BATCH = 50
SEMANTIC_SCHOLAR_BATCH = 500
MAX_DOIS = 1000
_DOI = re.compile(r"^10\.\d{4,9}/\S+$")


def normalise_doi(value: Any) -> Optional[str]:
    """Return a lower-case bare DOI (``10.xxxx/…``) or ``None`` if it is not one."""
    if not isinstance(value, str):
        return None
    doi = value.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
    doi = doi.strip().lower()
    return doi if _DOI.match(doi) else None


def rebuild_inverted_abstract(inverted: Mapping[str, Sequence[int]]) -> str:
    """Rebuild an OpenAlex ``abstract_inverted_index`` into text, validating positions."""
    positions: Dict[int, str] = {}
    for word, offsets in (inverted or {}).items():
        for offset in offsets:
            if not isinstance(offset, int) or offset < 0 or offset > 100000 or offset in positions:
                raise ValueError("invalid abstract position")
            positions[offset] = word
    if positions and set(positions) != set(range(len(positions))):
        raise ValueError("abstract positions are not contiguous")
    return " ".join(positions[i] for i in range(len(positions)))


class _JatsText(HTMLParser):
    """Collects text; block-level JATS/HTML tags (namespaced or not) become breaks."""

    _BLOCK = {"p", "title", "sec", "div", "h4", "br", "list-item"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.split(":")[-1] in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag.split(":")[-1] in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def strip_jats(markup: str) -> str:
    """Plain text from a Crossref JATS abstract, without a leading "Abstract" heading."""
    parser = _JatsText()
    parser.feed(markup or "")
    parser.close()
    text = re.sub(r"\s+", " ", "".join(parser.parts)).strip()
    return re.sub(r"^abstract[:.]?\s+", "", text, flags=re.IGNORECASE)


def _default_http_post(url: str, headers: Mapping[str, str], body: bytes) -> bytes:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
        return response.read(_MAX_BYTES + 1)[:_MAX_BYTES]


class AbstractBackfill:
    """Look up abstracts by DOI; ``http_get``/``http_post``/``dns_resolver`` are injectable for tests."""

    def __init__(
        self,
        http_get: Optional[Callable[[str, Mapping[str, str]], bytes]] = None,
        http_post: Optional[Callable[[str, Mapping[str, str], bytes], bytes]] = None,
        dns_resolver: Optional[Callable[[str], List[str]]] = None,
        contact: Optional[str] = None,
        openalex_key: Optional[str] = None,
        semantic_scholar_key: Optional[str] = None,
    ):
        self._get = http_get or _default_http_get
        self._post = http_post or _default_http_post
        self._resolver = dns_resolver
        self._contact = contact or os.getenv("NOESIS_SCHOLARLY_CONTACT")
        self._openalex_key = (openalex_key or os.getenv("NOESIS_OPENALEX_API_KEY")
                              or os.getenv("OPENALEX_API_KEY"))
        self._s2_key = semantic_scholar_key or os.getenv("SEMANTIC_SCHOLAR_API_KEY")

    # -- public ------------------------------------------------------------- #
    def fetch(self, dois: Iterable[Any], providers: Sequence[str] = PROVIDERS) -> Dict[str, Any]:
        """Look up abstracts for ``dois``; returns results keyed by normalised DOI."""
        unknown = [p for p in providers if p not in PROVIDERS]
        if unknown:
            raise ValueError(f"unknown provider(s): {', '.join(unknown)}")
        wanted: List[str] = []
        invalid: List[str] = []
        for value in dois:
            doi = normalise_doi(value)
            if doi is None:
                invalid.append(str(value))
            elif doi not in wanted:
                wanted.append(doi)
        if len(wanted) > MAX_DOIS:
            raise ValueError(f"at most {MAX_DOIS} DOIs per call")

        results: Dict[str, Dict[str, Any]] = {}
        errors: Dict[str, str] = {}
        requests: Dict[str, int] = {p: 0 for p in providers}
        for provider in providers:
            pending = [d for d in wanted if d not in results]
            if not pending:
                break
            try:
                found = getattr(self, "_" + provider)(pending, requests)
            except (PermanentFetchError, urllib.error.URLError, OSError, ValueError) as exc:
                errors[provider] = _describe(exc)
                continue
            retrieved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for doi, (abstract, provider_id) in found.items():
                if doi in pending and abstract:
                    results[doi] = {"abstract": abstract, "provider": provider,
                                    "provider_id": provider_id, "retrieved_at": retrieved_at}
        return {
            "contract": CONTRACT,
            "providers": list(providers),
            "requested": len(wanted),
            "found": len(results),
            "results": results,
            "missing": [d for d in wanted if d not in results],
            "invalid": invalid,
            "errors": errors,
            "requests": requests,
        }

    def fill(self, documents: Iterable[Document],
             providers: Sequence[str] = PROVIDERS) -> Tuple[List[Document], Dict[str, Any]]:
        """Return copies of ``documents`` with abstracts filled where missing, plus the report.

        Documents that already have content, or carry no DOI, are returned unchanged.
        """
        documents = list(documents)
        targets = [normalise_doi((d.metadata or {}).get("doi")) for d in documents]
        report = self.fetch([t for d, t in zip(documents, targets) if t and not d.content],
                            providers=providers)
        filled: List[Document] = []
        for document, doi in zip(documents, targets):
            hit = report["results"].get(doi) if doi and not document.content else None
            if hit is None:
                filled.append(document)
                continue
            metadata = dict(document.metadata or {})
            metadata.update({
                "content_coverage": "abstract-only",
                "abstract_source": hit["provider"],
                "abstract_provider_id": hit["provider_id"],
                "abstract_retrieved_at": hit["retrieved_at"],
            })
            filled.append(dataclasses.replace(document, content=hit["abstract"], metadata=metadata))
        return filled, report

    # -- providers ------------------------------------------------------------ #
    def _openalex(self, dois: List[str], requests: Dict[str, int]) -> Dict[str, Tuple[str, str]]:
        found: Dict[str, Tuple[str, str]] = {}
        batchable = [d for d in dois if "|" not in d and "," not in d]
        for start in range(0, len(batchable), OPENALEX_BATCH):
            batch = batchable[start:start + OPENALEX_BATCH]
            params = {"filter": "doi:" + "|".join(batch), "per-page": str(len(batch)),
                      "select": "id,doi,abstract_inverted_index"}
            if self._contact:
                params["mailto"] = self._contact
            if self._openalex_key:
                params["api_key"] = self._openalex_key
            body = self._json_get("openalex", "https://api.openalex.org/works?"
                                  + urllib.parse.urlencode(params, safe=":|/"))
            requests["openalex"] += 1
            for item in body.get("results") or []:
                doi = normalise_doi(item.get("doi"))
                if doi and item.get("abstract_inverted_index"):
                    found[doi] = (rebuild_inverted_abstract(item["abstract_inverted_index"]),
                                  str(item.get("id") or ""))
        return found

    def _crossref(self, dois: List[str], requests: Dict[str, int]) -> Dict[str, Tuple[str, str]]:
        found: Dict[str, Tuple[str, str]] = {}
        suffix = "?" + urllib.parse.urlencode({"mailto": self._contact}) if self._contact else ""
        for doi in dois:
            url = "https://api.crossref.org/works/" + urllib.parse.quote(doi, safe="") + suffix
            requests["crossref"] += 1
            try:
                body = self._json_get("crossref", url)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue
                raise
            abstract = strip_jats((body.get("message") or {}).get("abstract") or "")
            if abstract:
                found[doi] = (abstract, "https://doi.org/" + doi)
        return found

    def _semantic_scholar(self, dois: List[str], requests: Dict[str, int]) -> Dict[str, Tuple[str, str]]:
        found: Dict[str, Tuple[str, str]] = {}
        url = "https://api.semanticscholar.org/graph/v1/paper/batch?fields=abstract,externalIds"
        _assert_public_https(url, HOSTS["semantic_scholar"], self._resolver)
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "User-Agent": _user_agent()}
        if self._s2_key:
            headers["x-api-key"] = self._s2_key
        for start in range(0, len(dois), SEMANTIC_SCHOLAR_BATCH):
            batch = dois[start:start + SEMANTIC_SCHOLAR_BATCH]
            payload = json.dumps({"ids": ["DOI:" + d for d in batch]}).encode()
            requests["semantic_scholar"] += 1
            body = json.loads(self._post(url, headers, payload).decode("utf-8", "replace"))
            if not isinstance(body, list):
                raise ValueError("Semantic Scholar batch response is not a list")
            for doi, item in zip(batch, body):
                if isinstance(item, Mapping) and item.get("abstract"):
                    found[doi] = (str(item["abstract"]).strip(), str(item.get("paperId") or ""))
        return found

    def _json_get(self, provider: str, url: str) -> Any:
        _assert_public_https(url, HOSTS[provider], self._resolver)
        headers = {"Accept": "application/json", "User-Agent": _user_agent()}
        return json.loads(self._get(url, headers).decode("utf-8", "replace"))


def _describe(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    return f"{type(exc).__name__}: {str(exc)[:200]}"
