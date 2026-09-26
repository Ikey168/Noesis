"""Bounded GLEIF LEI acquisition (API v1, JSON:API) for company identity enrichment.

A source selects up to 50 LEIs. Each runtime page reads one part of one LEI:
the LEI record, the direct and ultimate parent relationships and the direct
and ultimate parent reporting exceptions. A part GLEIF does not hold (HTTP 404)
is ``none_reported``, never an inferred absence of a parent. GLEIF needs no
credential; the Golden Copy / delta files are the bulk alternative and are
not used for this bounded profile.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-lei-part-v1"
PARTS = ("record", "direct-parent-relationship", "ultimate-parent-relationship",
         "direct-parent-reporting-exception", "ultimate-parent-reporting-exception")
PATHS = {"record": "/lei-records/{lei}", **{part: "/lei-records/{lei}/" + part for part in PARTS[1:]}}
LEI = re.compile(r"^[0-9A-Z]{18}[0-9]{2}$")
MAX_LEIS = 50
FIXTURE_SECRET = None
PROVIDER_CONTRACTS = {
    "gleif": {
        "documentation": "https://www.gleif.org/en/lei-data/gleif-api",
        "access": "GLEIF API v1 lei-records, parent relationships and reporting exceptions (JSON:API)",
        "authentication": "none",
        "bulk_alternative": "Golden Copy and delta files (not used for the bounded profile)",
        "status": "unverified-live",
    },
    "official-registers": {
        "status": "not-implemented",
        "reason": "No selected EU/German/Berlin register offers a documented free machine API for bounded "
                  "acceptance (Handelsregister/Unternehmensregister are fee-based web portals); the LEI "
                  "record's registration authority and registered-as number are kept as the official pointer",
    },
    "opencorporates": {"status": "reused", "reason": "existing regional provider (#1483); enrichment, not an "
                                                     "official registry"},
}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def lei_valid(value: str) -> bool:
    """ISO 17442 check digits (mod 97 over the alphanumeric expansion)."""

    if not LEI.match(value or ""):
        return False
    return int("".join(str(int(ch, 36)) for ch in value)) % 97 == 1


class GleifAdapter:
    accepts_transport = True

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        del secret
        from functools import partial

        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        lei = dict(self.source.get("lei") or {})
        leis = list(lei.get("leis") or [])
        if not 1 <= len(leis) <= MAX_LEIS or any(not lei_valid(value) for value in leis):
            raise SourcePackError("unbounded_source", f"gleif sources select 1-{MAX_LEIS} valid LEIs")
        parts = list(lei.get("parts") or PARTS)
        if set(parts) - set(PARTS):
            raise SourcePackError("invalid_mapping", f"gleif parts are drawn from {PARTS}")
        self.transport = transport or partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.work = [(value, part) for value in leis for part in parts]
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "lei": {"leis": len(leis), "parts": parts},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _scope(self) -> str:
        return _digest({"endpoint": self.source["endpoint"], "work": self.work})

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"} or dict(
                request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "LEI runs use the pinned selection")
        state = {} if cursor is None else json.loads(cursor)
        if cursor is not None and state.get("scope") != self._scope():
            raise SourcePackError("cursor_drift", "LEI cursor belongs to a different selection")
        index = int(state.get("i", 0))
        if index >= len(self.work):
            return RuntimePage((), None, 0, receipt={"status": 200})
        lei, part = self.work[index]
        url = self.source["endpoint"].rstrip("/") + PATHS[part].format(lei=lei)
        if urlsplit(url).hostname != urlsplit(self.source["endpoint"]).hostname:
            raise SourcePackError("network_policy", "GLEIF requests stay on the declared host")
        response = self.transport(url=url, params={}, headers={"Accept": "application/vnd.api+json"},
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        status = int(response.get("status", 200))
        headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "source response exceeds its byte limit")
        if status == 429:
            raise SourcePackError("rate_limited", "GLEIF rate limit reached",
                                  retry_after_ms=_retry_after_ms(headers.get("retry-after")))
        if status >= 500:
            raise SourcePackError("source_unavailable", f"GLEIF returned HTTP {status}")
        info = {"lei": lei, "part": part, "response_sha256": hashlib.sha256(raw).hexdigest(),
                "work_index": index, "work_size": len(self.work)}
        records, outcome = [], "returned"
        if status == 404:
            outcome = "none_reported"
        elif status >= 400:
            raise SourcePackError("schema_drift", f"GLEIF returned HTTP {status}")
        else:
            try:
                payload = json.loads(raw)
                data = payload["data"]
                attributes = data["attributes"]
            except (ValueError, KeyError, TypeError) as exc:
                raise SourcePackError("schema_drift", "GLEIF response lacks data.attributes") from exc
            if part == "record" and attributes.get("lei") != lei:
                raise SourcePackError("schema_drift", "GLEIF returned another LEI")
            name = dict(dict(attributes.get("entity") or {}).get("legalName") or {}).get("name")
            records = [{"id": f"gleif:{lei}:{part}", "title": name or f"LEI {lei} {part}", "language": "en",
                        "url": f"https://search.gleif.org/#/record/{lei}",
                        "lei_record": {"contract": RECORD_CONTRACT, "provider": "gleif", "lei": lei, "part": part,
                                       "type": data.get("type"), "attributes": attributes,
                                       "raw_sha256": hashlib.sha256(raw).hexdigest()}}]
        more = index + 1 < len(self.work)
        next_cursor = json.dumps({"i": index + 1, "scope": self._scope()}, sort_keys=True) if more else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt={"status": status, "outcome": outcome, **info})


ADAPTERS = {"gleif": GleifAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    by_path = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del params, headers, timeout
        page = by_path.get(urlsplit(url).path)
        if page is None:
            return {"status": 404, "headers": {}, "content": b""}
        body = page.get("body")
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": json.dumps(body).encode() if isinstance(body, (dict, list)) else (body or "").encode()}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = GleifAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {}}, cursor=cursor)
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records
