"""Bounded Deutsche Digitale Bibliothek and Europeana acquisition for cultural primary sources.

These connectors extend the existing scientific primary-evidence source pack;
they are not a separate pack. Each source declares a ``cultural`` block with an
explicit query scope (query string plus provider filters), a page size and a
record ceiling, so the runtime's budgets, retries, checkpoints and quarantine
apply unchanged.

Records keep the provider's own identifiers, aggregation identity, rights
statement and asset links. Previews, derivatives and originals are listed as
separate representations with their own URLs; nothing is downloaded here.
The native field names were pinned from the providers' public documentation
(DDB Solr search index, Europeana Search API v2) and must be re-validated with
a live run; the fixtures are authored envelopes in those shapes.
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
RECORD_CONTRACT = "noesis-cultural-object-v1"
CULTURAL_CONNECTORS = frozenset({"ddb", "europeana"})
MAX_ROWS = 100
MAX_RECORDS = 500
PROVIDER_CONTRACTS = {
    "ddb": {
        "documentation": "https://pro.deutsche-digitale-bibliothek.de/faq",
        "access": "DDB API search index (api.deutsche-digitale-bibliothek.de/2/search/index/search/select), Solr JSON",
        "authentication": "API key (oauth_consumer_key) from a DDB account; operator step",
        "pagination": "start/rows",
        "identifiers": ["DDB item ID (32-character)", "provider (institution) ID"],
        "rights": "per-item license URI; missing rights means link-only",
        "status": "unverified-live",
    },
    "europeana": {
        "documentation": "https://pro.europeana.eu/page/get-api",
        "access": "Europeana Search API v2 (api.europeana.eu/record/v2/search.json)",
        "authentication": "wskey API key (operator step)",
        "pagination": "cursor (cursor=* then nextCursor)",
        "identifiers": ["Europeana record ID (/dataset/local)", "dataProvider", "aggregator provider", "edmIsShownAt"],
        "rights": "per-item rights statement URI (rightsstatements.org / Creative Commons)",
        "status": "unverified-live",
    },
}
DDB_ITEM = re.compile(r"deutsche-digitale-bibliothek\.de/item/([A-Z0-9]{32})")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _first(value: Any) -> Any:
    items = _list(value)
    return items[0] if items else None


def _https(value: Any) -> str | None:
    text = str(value or "").strip()
    parts = urlsplit(text)
    return text if parts.scheme in {"https", "http"} and parts.hostname else None


def cultural_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    cultural = dict(source.get("cultural") or {})
    query = dict(cultural.get("query") or {})
    if not str(query.get("q") or "").strip():
        raise SourcePackError("unbounded_source", "cultural sources pin an explicit query")
    rows = int(cultural.get("rows") or 0)
    ceiling = int(cultural.get("max_records") or 0)
    if not 1 <= rows <= MAX_ROWS or not 1 <= ceiling <= MAX_RECORDS:
        raise SourcePackError("unbounded_source", f"cultural sources set rows 1-{MAX_ROWS} and max_records 1-{MAX_RECORDS}")
    if any(not isinstance(v, (str, int)) for v in dict(query.get("filters") or {}).values()):
        raise SourcePackError("invalid_mapping", "cultural filters are scalar provider fields")
    return cultural


class _CulturalAdapter:
    accepts_transport = True
    provider = ""

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.cultural = cultural_declaration(self.source)
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.secret = secret
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "cultural": {"provider": self.provider, "query": self.cultural["query"]},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _scope(self) -> str:
        return _digest({"endpoint": self.source["endpoint"], "query": self.cultural["query"],
                        "rows": self.cultural["rows"]})

    def _call(self, params: Mapping[str, Any], headers: Mapping[str, str]):
        from src.ingestion.source_pack_runtime import _retry_after_ms

        if not self.secret:
            raise SourcePackError("authentication_failed", f"{self.provider} requires an API key (credential missing)")
        response = self.transport(url=self.source["endpoint"], params=dict(params), headers=dict(headers),
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        status = int(response.get("status", 200))
        headers_ = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "source response exceeds its byte limit")
        if status == 429:
            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(headers_.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"{self.provider} rejected the API key")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"{self.provider} returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"{self.provider} returned HTTP {status}")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourcePackError("schema_drift", f"{self.provider} returned a non-JSON body") from exc
        return payload, raw, headers_

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "cultural runs use the pinned query, not ad-hoc parameters")
        state = {} if cursor is None else json.loads(cursor)
        if cursor is not None and state.get("scope") != self._scope():
            raise SourcePackError("cursor_drift", "cultural cursor belongs to a different query")
        fetched = int(state.get("fetched", 0))
        if fetched >= int(self.cultural["max_records"]):
            return RuntimePage((), None, 0, receipt={"status": 200, "stopped": "max_records"})
        payload, raw, headers = self._call(*self.request(state))
        items, total, next_state = self.parse(payload, state)
        seen = set(state.get("seen") or [])
        records, duplicates = [], 0
        for item in items:
            record = self.record(item, raw)
            if record["id"] in seen:
                duplicates += 1
                continue
            seen.add(record["id"])
            records.append(record)
        records = records[: int(self.cultural["max_records"]) - fetched]
        fetched += len(records)
        info = {"response_sha256": hashlib.sha256(raw).hexdigest(), "total_results": total,
                "page_items": len(items), "duplicates_skipped": duplicates, "fetched": fetched,
                "quota_remaining": headers.get("x-ratelimit-remaining")}
        more = next_state is not None and fetched < int(self.cultural["max_records"]) and items
        next_cursor = json.dumps({**next_state, "fetched": fetched, "scope": self._scope(),
                                  "seen": sorted(seen)[-2000:]}, sort_keys=True) if more else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt={"status": 200, **info,
                                                                         "final_page": not more})

    # subclasses
    def request(self, state: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        raise NotImplementedError

    def parse(self, payload: Any, state: Mapping[str, Any]):
        raise NotImplementedError

    def record(self, item: Mapping[str, Any], raw: bytes) -> dict[str, Any]:
        raise NotImplementedError

    def _wrap(self, native_id: str, fields: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
        record = {
            "contract": RECORD_CONTRACT, "provider": self.provider, "provider_record_id": native_id,
            **fields, "native": dict(item), "native_sha256": _digest(item),
            "query_scope": self._scope(),
        }
        titles = [t["value"] for t in record["titles"]]
        return {"id": f"{self.provider}:{native_id}", "title": titles[0] if titles else native_id,
                "language": (record["languages"] or ["und"])[0], "url": record["source_url"],
                "content": " \n".join(titles + [d["value"] for d in record["descriptions"]]),
                "cultural_record": record}


def _date(original: Any, method: str) -> dict[str, Any] | None:
    text = str(original or "").strip()
    if not text:
        return None
    years = [int(y) for y in re.findall(r"(?<!\d)(\d{4})(?!\d)", text)]
    exact = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    normalized = ({"start": text, "end": text, "precision": "day"} if exact else
                  {"start": f"{min(years):04d}", "end": f"{max(years):04d}",
                   "precision": "year" if len(set(years)) == 1 else "year-range"} if years else None)
    return {"original": text, "normalized": normalized, "method": method if normalized else "unparsed"}


class DdbAdapter(_CulturalAdapter):
    provider = "ddb"

    def request(self, state):
        query = self.cultural["query"]
        filters = " AND ".join(f'{k}:"{v}"' for k, v in sorted(dict(query.get("filters") or {}).items()))
        params = {"q": query["q"] + (f" AND {filters}" if filters else ""), "rows": self.cultural["rows"],
                  "start": int(state.get("start", 0)), "sort": "id asc"}
        return params, {"Accept": "application/json", "Authorization": f'OAuth oauth_consumer_key="{self.secret}"'}

    def parse(self, payload, state):
        response = payload.get("response") if isinstance(payload, Mapping) else None
        if not isinstance(response, Mapping) or not isinstance(response.get("docs"), list):
            raise SourcePackError("schema_drift", "DDB response lacks response.docs")
        start = int(state.get("start", 0))
        total = int(response.get("numFound") or 0)
        nxt = start + len(response["docs"])
        return response["docs"], total, ({"start": nxt} if nxt < total else None)

    def record(self, item, raw):
        native_id = str(item.get("id") or "")
        if not re.fullmatch(r"[A-Z0-9]{32}", native_id):
            raise SourcePackError("schema_drift", "DDB document lacks a 32-character item id")
        language = [str(v) for v in _list(item.get("language_fct") or item.get("language"))]
        title_lang = language[0] if len(language) == 1 else None
        places = [{"role": "depicted", "name": str(name), "provider_place_id": None, "coordinates": None}
                  for name in _list(item.get("place_fct"))]
        if item.get("latitude") is not None and item.get("longitude") is not None:
            # The record's single coordinate pair belongs to its first named place.
            coordinates = {"lat": item["latitude"], "lon": item["longitude"], "crs": "EPSG:4326",
                           "precision": "provider-unspecified"}
            if places:
                places[0]["coordinates"] = coordinates
            else:
                places.append({"role": "depicted", "name": None, "provider_place_id": None,
                               "coordinates": coordinates})
        rights = _first(item.get("license") or item.get("license_url"))
        representations = []
        for role, key in (("preview", "preview"), ("thumbnail", "thumbnail"), ("original", "digitalisat_url")):
            url = _https(_first(item.get(key)))
            if url:
                representations.append({"role": role, "url": url, "media_type": _first(item.get("media_type")),
                                        "rights": rights})
        return self._wrap(native_id, {
            "source_url": f"https://www.deutsche-digitale-bibliothek.de/item/{native_id}",
            "aggregation": {"aggregator": "Deutsche Digitale Bibliothek", "aggregator_record_id": native_id},
            "institution": {"id": item.get("provider_id"), "name": _first(item.get("provider_fct"))},
            "collection": {"hierarchy": [str(v) for v in _list(item.get("collection_fct"))]},
            "object_type": _first(item.get("type_fct")),
            "titles": [{"value": str(t), "language": title_lang} for t in _list(item.get("label") or item.get("title"))],
            "descriptions": [{"value": str(d), "language": title_lang} for d in _list(item.get("description"))],
            "creators": [{"name": str(c), "role": "creator", "authority_id": None}
                         for c in _list(item.get("creator_fct") or item.get("creator"))],
            "subjects": [str(s) for s in _list(item.get("keywords_fct") or item.get("subject"))],
            "dates": [d for d in (_date(t, "year-extraction-v1") for t in _list(item.get("time_fct") or item.get("date")))
                      if d],
            "languages": language,
            "places": places,
            "rights": {"statement": rights, "attribution": _first(item.get("provider_fct"))},
            "representations": representations,
            "same_as": [],
            "provider_revision": item.get("last_update") or item.get("timestamp"),
        }, item)


class EuropeanaAdapter(_CulturalAdapter):
    provider = "europeana"

    def request(self, state):
        query = self.cultural["query"]
        params = {"query": query["q"], "rows": self.cultural["rows"], "cursor": state.get("cursor", "*"),
                  "profile": "standard", "wskey": self.secret}
        filters = [f'{k}:"{v}"' for k, v in sorted(dict(query.get("filters") or {}).items())]
        if filters:
            params["qf"] = filters
        return params, {"Accept": "application/json"}

    def parse(self, payload, state):
        if not isinstance(payload, Mapping) or payload.get("success") is not True or not isinstance(payload.get("items"), list):
            if isinstance(payload, Mapping) and payload.get("success") is False:
                error = str(payload.get("error") or "")
                if re.search(r"api\s*key|wskey", error, re.IGNORECASE):
                    raise SourcePackError("authentication_failed", "Europeana rejected the API key")
            raise SourcePackError("schema_drift", "Europeana response lacks success and items")
        nxt = payload.get("nextCursor")
        return payload["items"], int(payload.get("totalResults") or 0), ({"cursor": nxt} if nxt else None)

    def record(self, item, raw):
        native_id = str(item.get("id") or "")
        if not re.fullmatch(r"/[A-Za-z0-9_]+/[^\s]+", native_id):
            raise SourcePackError("schema_drift", "Europeana item lacks a record id")
        titles = []
        for language, values in sorted(dict(item.get("dcTitleLangAware") or {}).items()):
            titles += [{"value": str(v), "language": None if language == "def" else language} for v in _list(values)]
        if not titles:
            titles = [{"value": str(t), "language": None} for t in _list(item.get("title"))]
        descriptions = []
        for language, values in sorted(dict(item.get("dcDescriptionLangAware") or {}).items()):
            descriptions += [{"value": str(v), "language": None if language == "def" else language}
                             for v in _list(values)]
        lats, lons = _list(item.get("edmPlaceLatitude")), _list(item.get("edmPlaceLongitude"))
        labels = _list(item.get("edmPlaceLabel"))
        places = []
        for index, label in enumerate(labels or ([None] if lats else [])):
            coordinates = None
            if index < len(lats) and index < len(lons):
                coordinates = {"lat": lats[index], "lon": lons[index], "crs": "EPSG:4326",
                               "precision": "provider-unspecified"}
            places.append({"role": "depicted", "name": label, "provider_place_id": _first(item.get("edmPlace")),
                           "coordinates": coordinates})
        rights = _first(item.get("rights"))
        shown_at = [u for u in (_https(v) for v in _list(item.get("edmIsShownAt"))) if u]
        same_as = [f"ddb:{m.group(1)}" for u in shown_at if (m := DDB_ITEM.search(u))]
        representations = []
        for role, key in (("preview", "edmPreview"), ("original", "edmIsShownBy")):
            for url in (_https(v) for v in _list(item.get(key))):
                if url:
                    representations.append({"role": role, "url": url, "media_type": None, "rights": rights})
        return self._wrap(native_id, {
            "source_url": _https(item.get("guid")) or f"https://www.europeana.eu/item{native_id}",
            "aggregation": {"aggregator": _first(item.get("provider")), "aggregator_record_id": native_id,
                            "shown_at": shown_at},
            "institution": {"id": None, "name": _first(item.get("dataProvider"))},
            "collection": {"hierarchy": [str(v) for v in _list(item.get("europeanaCollectionName"))]},
            "object_type": item.get("type"),
            "titles": titles, "descriptions": descriptions,
            "creators": [{"name": str(c), "role": "creator", "authority_id": None} for c in _list(item.get("dcCreator"))],
            "subjects": [str(s) for s in _list(item.get("dcSubject"))],
            "dates": [d for d in (_date(v, "year-extraction-v1") for v in _list(item.get("year"))
                                  + _list(item.get("edmTimespanLabel"))) if d],
            "languages": [str(v) for v in _list(item.get("language"))],
            "places": places,
            "rights": {"statement": rights, "attribution": _first(item.get("dataProvider"))},
            "representations": representations,
            "same_as": same_as,
            "provider_revision": item.get("timestamp_update"),
        }, item)


ADAPTERS = {"ddb": DdbAdapter, "europeana": EuropeanaAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored envelopes keyed by page position (start for DDB, cursor for Europeana)."""

    by_key = {str(page["page"]): page for page in pages}

    def transport(*, url, params, headers, timeout):
        del url, timeout
        key = str(params.get("start", params.get("cursor", "")))
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        credential = params.get("wskey") or headers.get("Authorization")
        if page.get("requires_secret", True) and not credential:
            return {"status": 401, "headers": {}, "content": b""}
        body = page.get("body")
        content = body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = ADAPTERS[source["connector"]](source, transport=fixture_transport(list(fixture["native_pages"])),
                                            secret="fixture-credential")
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
