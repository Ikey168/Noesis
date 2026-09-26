"""Bounded native CELLAR, federal-court (RII) and Berlin legal acquisition for the Legal pack.

The parsers are the existing, reviewed ones in
:mod:`src.ingestion.regional_providers` (#1480, #1481, #1482); this module only
adapts them to the source-pack runtime so budgets, retries, quarantine,
checkpoints and projection apply. Each source declares an explicit, bounded
``legal`` selection:

* ``cellar`` - up to 20 CELEX numbers and 1-24 languages, paged SPARQL rows.
* ``rii`` - explicit ``doknr`` identities and/or one bounded index window
  (court and modified-since filters) from the official RII table of contents.
* ``berlin-law`` - explicit documented download or rendered-page URLs on
  gesetze.berlin.de, each with its official ID, format and historical state.

Nothing here infers that an instrument is in force: records keep
``is_current_law`` unknown and the Legal store only records sourced dates.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
LEGAL_CONNECTORS = frozenset({"cellar", "rii", "berlin-law"})
MAX_SELECTION = 50
JURISDICTIONS = {"cellar": "EU", "rii": "DE", "berlin-law": "DE-BE"}
PROVIDER_CONTRACTS = {
    "cellar": {
        "provider": "cellar",
        "jurisdiction": "EU",
        "access": "Public CELLAR SPARQL endpoint (publications.europa.eu/webapi/rdf/sparql), explicit CELEX selections",
        "formats": ["application/sparql-results+json"],
        "document_classes": ["legislation", "case-law", "corrigenda"],
        "identifiers": ["CELEX", "ELI", "ECLI", "CELLAR work/expression/manifestation/item URIs"],
        "authentication": "none",
        "coverage": "Selected EU works; language expressions and manifestations stay distinct; current law is not inferred",
        "prior_live_evidence": "docs/development/workflow-review-evidence/cellar-native-2026-09-09.json",
    },
    "rii": {
        "provider": "german-courts",
        "jurisdiction": "DE",
        "access": "Official Rechtsprechung im Internet table of contents (rii-toc.xml) and per-decision ZIP/XML downloads",
        "formats": ["application/xml", "application/zip"],
        "document_classes": ["federal court decisions"],
        "identifiers": ["doknr", "docket number (Aktenzeichen)", "ECLI where published"],
        "authentication": "none",
        "coverage": "Decisions of the federal courts published on rechtsprechung-im-internet.de, not all German case law",
        "prior_live_evidence": "docs/development/workflow-review-evidence/court-streaming-live-2026-09-08.json",
    },
    "berlin-law": {
        "provider": "berlin-law",
        "jurisdiction": "DE-BE",
        "access": "Documented public portal downloads (juris XML ZIP) and rendered judgment pages on gesetze.berlin.de; no unrestricted API",
        "formats": ["application/zip (juris XML)", "text/html (rendered judgment)"],
        "document_classes": ["laws", "regulations", "court decisions", "gazette publications (explicit import)"],
        "identifiers": ["juris doknr (jlr-…/NJRE…)", "GVBl. reference"],
        "authentication": "none",
        "coverage": "Selected official Berlin publications; historical versions remain historical; editorial text is excluded",
        "prior_live_evidence": "docs/development/workflow-review-evidence/berlin-native-2026-09-09.json",
    },
}


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def legal_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    legal = dict(source.get("legal") or {})
    connector = source["connector"]
    selection = legal.get("selection")
    if connector == "cellar":
        celex = list(dict(selection or {}).get("celex") or [])
        languages = list(dict(selection or {}).get("languages") or [])
        if not 1 <= len(celex) <= 20 or any(not re.fullmatch(r"[0-9A-Z()._-]{5,50}", c) for c in celex):
            raise SourcePackError("unbounded_source", "cellar sources select 1-20 CELEX numbers")
        if not 1 <= len(languages) <= 24 or any(not re.fullmatch(r"[A-Z]{3}", lang) for lang in languages):
            raise SourcePackError("invalid_mapping", "cellar sources select 1-24 three-letter languages")
    elif connector == "rii":
        decisions = list(dict(selection or {}).get("decisions") or [])
        index = dict(selection or {}).get("index")
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", d) for d in decisions):
            raise SourcePackError("invalid_mapping", "rii decisions are official doknr identities")
        if index is not None and not 1 <= int(dict(index).get("limit") or 0) <= MAX_SELECTION:
            raise SourcePackError("unbounded_source", f"rii index windows select 1-{MAX_SELECTION} decisions")
        if not decisions and index is None or len(decisions) > MAX_SELECTION:
            raise SourcePackError("unbounded_source", "rii sources need explicit decisions or one bounded index window")
    elif connector == "berlin-law":
        items = list(selection or [])
        if not 1 <= len(items) <= MAX_SELECTION:
            raise SourcePackError("unbounded_source", f"berlin-law sources select 1-{MAX_SELECTION} publications")
        for item in items:
            if item.get("format") not in {"juris-xml-zip", "rendered-html"} or not item.get("official_id") \
                    or type(item.get("historical")) not in {bool, type(None)}:
                raise SourcePackError(
                    "invalid_mapping",
                    "berlin-law selections name official_id, format (juris-xml-zip|rendered-html) and historical",
                )
    return legal


class _LegalAdapter:
    accepts_transport = True
    connector = ""

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        del secret
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.legal = legal_declaration(self.source)
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT,
            "source_id": source["source_id"],
            "connector": source["connector"],
            "endpoint": source["endpoint"],
            "operations": list(source["operations"]),
            "source_hash": source["source_hash"],
            "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"],
            "limits": source["budgets"],
            "legal": {"jurisdiction": JURISDICTIONS[source["connector"]], "selection": self.legal.get("selection")},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _scope(self) -> str:
        return _digest({"endpoint": self.source["endpoint"], "selection": self.legal.get("selection")})

    def _get(self, url: str, params: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None):
        host = (urlsplit(url).hostname or "").casefold()
        if host != (urlsplit(self.source["endpoint"]).hostname or "").casefold():
            raise SourcePackError("network_policy", "legal sources fetch only from their declared host")
        response = self.transport(url=url, params=dict(params or {}), headers=dict(headers or {}),
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        status = int(response.get("status", 200))
        headers_ = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "source response exceeds its byte limit")
        if status == 429:
            from src.ingestion.source_pack_runtime import _retry_after_ms

            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(headers_.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"{self.connector} refused access (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"{self.connector} returned HTTP {status}")
        return status, raw

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "legal runs use the pinned selection, not ad-hoc parameters")

    def _cursor(self, cursor: str | None) -> dict[str, Any]:
        if cursor is None:
            return {}
        try:
            state = json.loads(cursor)
        except ValueError as exc:
            raise SourcePackError("cursor_drift", "legal cursor is not a valid checkpoint") from exc
        if not isinstance(state, dict) or state.get("scope") != self._scope():
            raise SourcePackError("cursor_drift", "legal cursor belongs to a different selection")
        return state

    @staticmethod
    def _wrap(record: Mapping[str, Any], page_info: Mapping[str, Any]) -> dict[str, Any]:
        text = "\n\n".join(part["text"] for part in record.get("sections") or [] if part.get("text"))
        return {
            # One document per published source location: two versions of one
            # work (e.g. historical and current text) are distinct documents.
            "id": (f"{record['provider']}:{record['kind']}:{record['language']}:{record['provider_id']}:"
                   + hashlib.sha256(str(record["source_url"]).encode()).hexdigest()[:12]),
            "title": record["title"],
            "language": record["language"],
            "url": record["source_url"],
            "published_at": record.get("published_at"),
            **({"content": text} if text else {}),
            "legal_record": dict(record),
            "legal_page": dict(page_info),
        }

    def _page(self, records, next_state, raw_bytes, receipt):
        from src.ingestion.source_pack_runtime import RuntimePage

        next_cursor = None if next_state is None else json.dumps({**next_state, "scope": self._scope()},
                                                                 sort_keys=True)
        return RuntimePage(tuple(records), next_cursor, raw_bytes, receipt=receipt)


def _provider_error(exc: Exception) -> SourcePackError:
    code = getattr(exc, "code", "schema_drift")
    mapped = {"schema_drift": "schema_drift", "source_identity": "schema_drift", "input_limit": "response_too_large",
              "unavailable_text": "schema_drift", "unsupported_format": "schema_drift",
              "unsupported_archive": "schema_drift", "archive_limit": "response_too_large",
              "empty_source": "schema_drift"}.get(code, "schema_drift")
    return SourcePackError(mapped, f"{code}: {exc}")


class CellarLegalAdapter(_LegalAdapter):
    connector = "cellar"

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.regional_providers import (
            ProviderError,
            cellar_query,
            parse_cellar_results,
        )

        self._check(request)
        state = self._cursor(cursor)
        selection = self.legal["selection"]
        offset = int(state.get("offset", 0))
        limit = min(int(selection.get("page_size") or 100), int(self.definition["limits"]["max_results"]))
        query = cellar_query(selection["celex"], languages=tuple(selection["languages"]), offset=offset, limit=limit)
        status, raw = self._get(self.source["endpoint"],
                                {"query": query, "format": "application/sparql-results+json"},
                                {"Accept": "application/sparql-results+json"})
        if status >= 400:
            raise SourcePackError("schema_drift", f"CELLAR returned HTTP {status}")
        try:
            payload = json.loads(raw)
            result = parse_cellar_results(payload, celex_ids=selection["celex"],
                                          languages=tuple(selection["languages"]), offset=offset, limit=limit)
        except (ValueError, ProviderError) as exc:
            raise _provider_error(exc) if isinstance(exc, ProviderError) else SourcePackError(
                "schema_drift", "CELLAR returned a non-JSON result") from exc
        info = {"offset": offset, "limit": limit, "query_sha256": _digest(query),
                "response_sha256": hashlib.sha256(raw).hexdigest(), "rows": len(payload["results"]["bindings"]),
                "final_page": result["next_offset"] is None}
        records = [self._wrap(record, info) for record in result["records"]]
        next_state = None if result["next_offset"] is None else {"offset": result["next_offset"]}
        return self._page(records, next_state, len(raw), {"status": status, **info})


class RiiDecisionAdapter(_LegalAdapter):
    connector = "rii"
    INDEX_PATH = "/rii-toc.xml"

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.regional_providers import (
            ProviderError,
            parse_court_download,
            parse_court_index,
        )

        self._check(request)
        state = self._cursor(cursor)
        selection = self.legal["selection"]
        base = self.source["endpoint"].rstrip("/")
        if "queue" not in state:
            queue = list(selection.get("decisions") or [])
            index_info = None
            if selection.get("index"):
                window = dict(selection["index"])
                status, raw = self._get(base + self.INDEX_PATH)
                if status >= 400:
                    raise SourcePackError("schema_drift", f"court index returned HTTP {status}")
                try:
                    parsed = parse_court_index(raw, offset=0, limit=int(window["limit"]), court=window.get("court"),
                                               since=window.get("since"),
                                               max_index_bytes=int(self.definition["limits"]["max_bytes"]))
                except ProviderError as exc:
                    raise _provider_error(exc) from exc
                for row in parsed["records"]:
                    identity = re.search(r"jb-([A-Za-z0-9_-]+)\.zip$", row["url"]).group(1)
                    if identity not in queue:
                        queue.append(identity)
                index_info = {"index_sha256": parsed["index_sha256"], "matched": parsed["total_selected"],
                              "selected": len(parsed["records"])}
                queue = queue[:MAX_SELECTION]
                return self._page([], {"queue": queue, "i": 0} if queue else None, len(raw),
                                  {"status": status, "index": index_info, "queue_size": len(queue)})
            state = {"queue": queue[:MAX_SELECTION], "i": 0}
        queue, index = list(state["queue"]), int(state["i"])
        if index >= len(queue):
            return self._page([], None, 0, {"status": 200, "queue_size": len(queue)})
        identity = queue[index]
        status, raw = self._get(f"{base}/jportal/docs/bsjrs/jb-{identity}.zip")
        info = {"decision": identity, "queue_index": index, "queue_size": len(queue),
                "response_sha256": hashlib.sha256(raw).hexdigest(), "final_page": index + 1 >= len(queue)}
        records, outcome = [], "returned"
        if status in {404, 410}:
            outcome = "not_found"
        elif status >= 400:
            raise SourcePackError("schema_drift", f"court download returned HTTP {status}")
        else:
            try:
                record = parse_court_download(raw)
            except (ProviderError, zipfile.BadZipFile) as exc:
                raise (_provider_error(exc) if isinstance(exc, ProviderError)
                       else SourcePackError("schema_drift", "court download is not a valid archive")) from exc
            if record["provider_id"] != identity:
                raise SourcePackError("schema_drift", "court response returned another decision")
            records = [self._wrap(record, info)]
        next_state = None if index + 1 >= len(queue) else {"queue": queue, "i": index + 1}
        return self._page(records, next_state, len(raw), {"status": status, "outcome": outcome, **info})


class BerlinLegalAdapter(_LegalAdapter):
    connector = "berlin-law"

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.regional_providers import (
            ProviderError,
            parse_berlin_juris_html,
            parse_berlin_juris_xml,
        )

        self._check(request)
        state = self._cursor(cursor)
        items = list(self.legal["selection"])
        index = int(state.get("i", 0))
        if index >= len(items):
            return self._page([], None, 0, {"status": 200})
        item = dict(items[index])
        status, raw = self._get(item["url"])
        info = {"official_id": item["official_id"], "format": item["format"], "selection_index": index,
                "selection_size": len(items), "response_sha256": hashlib.sha256(raw).hexdigest(),
                "final_page": index + 1 >= len(items)}
        records, outcome = [], "returned"
        if status in {404, 410}:
            outcome = "not_found"
        elif status >= 400:
            raise SourcePackError("schema_drift", f"Berlin portal returned HTTP {status}")
        else:
            try:
                if item["format"] == "juris-xml-zip":
                    xml = _single_xml_member(raw)
                    parsed = parse_berlin_juris_xml(xml, source_url=item["url"], historical=item.get("historical"))
                else:
                    parsed = parse_berlin_juris_html(raw, source_url=item["url"], official_id=item["official_id"])
            except ProviderError as exc:
                raise _provider_error(exc) from exc
            for record in parsed:
                if record["provider_id"] != item["official_id"]:
                    raise SourcePackError("schema_drift", "Berlin publication returned another official ID")
            records = [self._wrap(record, info) for record in parsed]
        next_state = None if index + 1 >= len(items) else {"i": index + 1}
        return self._page(records, next_state, len(raw), {"status": status, "outcome": outcome, **info})


def _single_xml_member(raw: bytes) -> bytes:
    if not raw.startswith(b"PK"):
        return raw
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        if len(members) != 1 or not members[0].filename.endswith(".xml") or members[0].file_size > 20_000_000 \
                or ".." in members[0].filename or members[0].filename.startswith("/"):
            raise SourcePackError("schema_drift", "Berlin download must contain exactly one XML member")
        with archive.open(members[0]) as stream:
            return stream.read(20_000_001)


ADAPTERS = {"cellar": CellarLegalAdapter, "rii": RiiDecisionAdapter, "berlin-law": BerlinLegalAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay native envelopes keyed by URL path (+ SPARQL offset for CELLAR)."""

    def key_of(url: str, params: Mapping[str, Any]) -> str:
        parts = urlsplit(url)
        offset = re.search(r"OFFSET (\d+)\s*$", str(params.get("query") or ""))
        return parts.path + ("?" + parts.query if parts.query else "") + (f"#offset={offset.group(1)}" if offset else "")

    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        page = by_key.get(key_of(url, params))
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key_of(url, params)}")
        body = page.get("body")
        if page.get("body_encoding") == "base64":
            import base64

            content = base64.b64decode(body)
        elif isinstance(body, str):
            content = body.encode()
        elif body is None:
            content = b""
        else:
            content = json.dumps(body, ensure_ascii=False).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = ADAPTERS[source["connector"]](source, transport=fixture_transport(list(fixture["native_pages"])))
    operation = min(source["operations"])
    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page({"operation": operation, "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records
