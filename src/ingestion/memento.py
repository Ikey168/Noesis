"""Bounded Memento (RFC 7089) resolution across web archives with durable receipts (#2226).

WA03 resolves TimeGates (``Accept-Datetime``) and TimeMaps (link-format and
JSON) through the Time Travel aggregator. The aggregator is recorded as the
**resolver**. The archive that holds each memento (identified by its URI-M
host) is recorded as the **archive**. WA05 configures the UK Web Archive and the
other national archives selected in the WA01 audit for direct TimeMap reads.
Reading-room-only captures are recorded with that access condition and never
fetched. WA06 encodes the archive.today access decision: while it is excluded,
every resolution lists it as ``excluded_by_access_decision`` and withholds any
of its mementos an aggregator returns.

Everything is bounded by ``BOUNDED_COVERAGE`` (requests per archive per
resolution, TimeMap bytes and mementos) and leaves a receipt in
``memento_acquisitions``. A request id is idempotent: replaying it returns the
stored receipt without touching the network. Missing, unavailable, excluded and
blocked archives are reported per archive, never hidden. There is no crawling,
no bulk TimeMap harvesting and no workaround of robots, exclusion, reading-room
or anti-automation controls. Live access is ``unverified-live`` until WA15
(#2348). See ``docs/roadmaps/platform-web-archives-source-audit.md``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from email.utils import format_datetime
from typing import Any
from urllib.parse import urlsplit

RESOLUTION_CONTRACT = "noesis-web-archive-resolution-v1"
ADAPTER_VERSION = "memento-v1"
USER_AGENT = "Noesis/0.1 (+https://github.com/Ikey168/Noesis; web-archive provenance)"
AGGREGATOR = "timetravel"

# WA01 contracts and access decisions (docs/roadmaps/platform-web-archives-source-audit.md).
ARCHIVES: dict[str, dict[str, Any]] = {
    "timetravel": {
        "kind": "aggregator",
        "name": "Memento Time Travel aggregator (LANL / ODU)",
        "access_decision": "in_scope",
        "timegate": "https://timetravel.mementoweb.org/timegate/",
        "timemap_link": "https://timetravel.mementoweb.org/timemap/link/",
        "timemap_json": "https://timetravel.mementoweb.org/timemap/json/",
        "api_json": "https://timetravel.mementoweb.org/api/json/",
        "hosts": ["timetravel.mementoweb.org"],
        "formats": ["link-format", "json"],
        "digest": "none published",
        "rate_limit": "no published hard limit; Noesis 1 request/s, 2 per resolution",
        "terms": "public research service; moderate use per the Time Travel API guide",
    },
    "internet-archive": {
        "kind": "memento-archive",
        "name": "Internet Archive Wayback Machine",
        "access_decision": "in_scope",
        "timegate": "https://web.archive.org/web/",
        "timemap_link": "https://web.archive.org/web/timemap/link/",
        "timemap_json": "https://web.archive.org/web/timemap/json/",
        "cdx": "https://web.archive.org/cdx/search/cdx",
        "hosts": ["web.archive.org"],
        "formats": ["link-format", "json", "cdx-json"],
        "digest": "CDX digest: SHA-1 of the payload, base32, published",
        "rate_limit": "throttles heavy clients (HTTP 429); Noesis 3 requests per resolution, 15 per minute",
        "terms": "archive.org Terms of Use; robots-based and administrative exclusions honoured as published",
    },
    "uk-web-archive": {
        "kind": "memento-archive",
        "name": "UK Web Archive (British Library and UK legal deposit libraries)",
        "access_decision": "in_scope",
        "timegate": "https://www.webarchive.org.uk/wayback/archive/",
        "timemap_link": "https://www.webarchive.org.uk/wayback/archive/timemap/link/",
        "hosts": ["www.webarchive.org.uk", "webarchive.org.uk"],
        "formats": ["link-format"],
        "digest": "none published through Memento",
        "rate_limit": "Noesis 2 requests per resolution, 6 per minute",
        "terms": "UKWA terms of use; most legal-deposit captures are viewable only in reading rooms",
        # Legal-deposit-only captures replay only on reading-room terminals. Patterns are
        # unverified-live until WA15 (#2348) confirms them.
        "reading_room_patterns": [r"^https?://(?:www\.)?webarchive\.org\.uk/wayback/(?:ld|ldukwa|en/ld)/",
                                  r"^https?://ldwa\."],
    },
    "arquivo-pt": {
        "kind": "memento-archive",
        "name": "Arquivo.pt (Portuguese web archive, FCT)",
        "access_decision": "in_scope",
        "timegate": "https://arquivo.pt/wayback/",
        "timemap_link": "https://arquivo.pt/wayback/timemap/link/",
        "hosts": ["arquivo.pt"],
        "formats": ["link-format"],
        "digest": "CDX digest published; not used (TimeMap only)",
        "rate_limit": "Noesis 2 requests per resolution, 6 per minute",
        "terms": "open API with published terms of use",
    },
    "library-of-congress": {
        "kind": "memento-archive",
        "name": "Library of Congress web archives",
        "access_decision": "deferred",
        "decision_reason": "collection embargoes and permissions-based access need a terms review",
        "timemap_link": "https://webarchive.loc.gov/all/timemap/link/",
        "hosts": ["webarchive.loc.gov"],
        "formats": ["link-format"],
        "digest": "none published through Memento",
        "rate_limit": "not reviewed",
        "terms": "not reviewed",
    },
    "vefsafn-is": {
        "kind": "memento-archive",
        "name": "Vefsafn.is (Icelandic web archive)",
        "access_decision": "deferred",
        "decision_reason": "terms not yet reviewed",
        "timemap_link": "https://vefsafn.is/timemap/link/",
        "hosts": ["vefsafn.is", "wayback.vefsafn.is"],
        "formats": ["link-format"],
        "digest": "none published",
        "rate_limit": "not reviewed",
        "terms": "not reviewed",
    },
    "bnf": {
        "kind": "memento-archive",
        "name": "Bibliotheque nationale de France web legal deposit",
        "access_decision": "excluded",
        "decision_reason": "no public Memento endpoint; legal-deposit captures are consultable on site only",
        "hosts": [],
        "formats": [],
        "digest": "none",
        "rate_limit": "n/a",
        "terms": "on-site consultation only",
    },
    "archive-today": {
        "kind": "memento-archive",
        "name": "archive.today (archive.ph and mirrors)",
        "access_decision": "excluded",
        "decision_reason": "automated access would work around CAPTCHA and anti-automation controls, and the "
                           "service does not publish how it honours publisher exclusion requests",
        "timegate": "https://archive.ph/timegate/",
        "timemap_link": "https://archive.ph/timemap/",
        "hosts": ["archive.ph", "archive.today", "archive.is", "archive.li", "archive.vn", "archive.fo",
                  "archive.md"],
        "formats": ["link-format"],
        "digest": "none",
        "rate_limit": "aggressive, unpublished; CAPTCHA challenges",
        "terms": "no published API terms",
    },
    "common-crawl": {
        "kind": "crawl-corpus",
        "name": "Common Crawl (crawl corpus, not a Memento archive)",
        "access_decision": "in_scope",
        "index": "https://index.commoncrawl.org/",
        "hosts": ["index.commoncrawl.org", "data.commoncrawl.org"],
        "formats": ["cdx-json-lines", "warc"],
        "digest": "CDX digest: SHA-1 of the payload, base32, published; recomputed on a single WARC range read",
        "rate_limit": "index throttles heavy clients (HTTP 503); Noesis 1 index lookup per crawl, 2 crawls",
        "terms": "Common Crawl Terms of Use",
    },
}
ACCESS_DECISIONS = ("in_scope", "deferred", "excluded")
VERIFICATION_URLS = (
    "https://datatracker.ietf.org/doc/html/rfc7089",
    "https://timetravel.mementoweb.org/guide/api/",
    "https://www.gov.uk/government/organisations/hm-treasury",
    "https://arquivo.pt/",
    "https://example.com/",
)
BOUNDED_COVERAGE = {
    "requests_per_resolution": {"timetravel": 2, "internet-archive": 3, "uk-web-archive": 2, "arquivo-pt": 2,
                                "archive-today": 2, "common-crawl": 1},
    "max_crawls_per_resolution": 2,
    "max_timemap_bytes": 2_000_000,
    "max_mementos_per_timemap": 5_000,
    "timeout_s": 20,
    "save_page_now_per_namespace_per_day": 5,
    "monitor_checks": "one resolution, one live-URL check and one check per pinned capture per run",
    "verification_urls": list(VERIFICATION_URLS),
}
SAVE_PAGE_NOW = {
    "operation": "write",
    "endpoint": "https://web.archive.org/save",
    "status_endpoint": "https://web.archive.org/save/status/",
    "auth": "Authorization: LOW {access}:{secret} from NOESIS_IA_S3_ACCESS_KEY / NOESIS_IA_S3_SECRET_KEY",
    "scope": "knowledge:citation:archive-request",
    "feature": "save-page-now",
    "feature_default": False,
    "budget_per_namespace_per_day": 5,
    "status_polls_per_call": 1,
    "refusals": ["error:robots-txt", "error:blocked-url", "error:blocked", "error:no-access",
                 "error:blocked-client-ip", "error:filesize-limit"],
    "terms": "archive.org Terms of Use; per-account concurrency and daily capture caps",
    "no_retry_elsewhere": True,
}
LIVE_VERIFICATION = {
    archive_id: {"status": "unverified-live", "evidence": "published documentation only; WA15 (#2348) outstanding",
                 "urls": list(VERIFICATION_URLS)}
    for archive_id, spec in ARCHIVES.items() if spec["access_decision"] == "in_scope"
}
LIVE_VERIFICATION["save-page-now"] = {"status": "unverified-live", "urls": list(VERIFICATION_URLS),
                                      "evidence": "published documentation only; WA15 (#2348) outstanding"}
EXCLUSIONS = ("no bypass of robots, exclusion, paywalls, reading-room or anti-automation controls",
              "no bulk mirroring, crawling or TimeMap harvesting",
              "no retry of a refused or excluded URL through another archive or proxy")

_CAPTCHA = re.compile(rb"captcha|cf-challenge|challenge-platform|are you a robot", re.I)
_EXCLUDED = re.compile(rb"blocked site|robots\.txt|excluded|accesscontrol|not archived by request", re.I)


class MementoError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def validate_url(url: str) -> str:
    parts = urlsplit(str(url or ""))
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password
            or len(url) > 4096):
        raise MementoError("invalid_url", "an http(s) URL without credentials is required")
    return url


def archive_for(uri_m: str, archives: Mapping[str, Mapping[str, Any]] = ARCHIVES) -> str:
    """The archive that holds a memento, by its URI-M host; unknown hosts are ``unaudited:<host>``."""
    host = (urlsplit(uri_m).hostname or "").lower()
    for archive_id, spec in archives.items():
        if spec["kind"] != "aggregator" and host in spec.get("hosts", []):
            return archive_id
    return "unaudited:" + host


def decision_outcome(archive: Mapping[str, Any]) -> str:
    """The per-archive outcome an access decision imposes (excluded or deferred archives are never queried)."""
    return {"excluded": "excluded_by_access_decision", "deferred": "deferred_by_access_decision"}[
        archive["access_decision"]]


def access_condition(archive: Mapping[str, Any], uri_m: str) -> str:
    if any(re.search(p, uri_m) for p in archive.get("reading_room_patterns", [])):
        return "reading-room-only"
    return "open"


# ---------------------------------------------------------------------- parsing

_LINK = re.compile(r'<([^>]*)>((?:\s*;\s*[A-Za-z_*][A-Za-z0-9_*-]*\s*=\s*(?:"[^"]*"|[^;,<]*))*)')
_ATTR = re.compile(r';\s*([A-Za-z_*][A-Za-z0-9_*-]*)\s*=\s*(?:"([^"]*)"|([^;,<]*))')


def parse_link_format(text: str | bytes) -> list[dict[str, Any]]:
    """Parse RFC 6690 / RFC 7089 link-format (TimeMaps and ``Link`` headers)."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    links = []
    for match in _LINK.finditer(text):
        attrs: dict[str, Any] = {}
        for key, quoted, bare in _ATTR.findall(match.group(2)):
            attrs[key.lower()] = quoted if quoted else bare.strip()
        attrs["rel"] = set(str(attrs.get("rel", "")).split())
        links.append({"uri": match.group(1).strip(), **attrs})
    return links


def _stamp_ms(value: str) -> int:
    from src.kb.citation_preservation import _datetime_ms

    return _datetime_ms(value)


def parse_timemap(raw: bytes, content_type: str = "") -> dict[str, Any]:
    """Mementos from a link-format TimeMap, a Time Travel JSON TimeMap or Wayback JSON rows."""
    text = raw.decode("utf-8", errors="replace").strip()
    result: dict[str, Any] = {"original": None, "timegate": None, "mementos": []}
    if "json" in content_type or text[:1] in "[{":
        value = json.loads(text) if text else {}
        if isinstance(value, list):  # Wayback timemap/json: header row then rows
            if not value:
                return result
            header = [str(h) for h in value[0]]
            for row in value[1:]:
                item = dict(zip(header, row, strict=False))
                if "timestamp" in item and "original" in item:
                    result["mementos"].append({
                        "uri_m": f"https://web.archive.org/web/{item['timestamp']}/{item['original']}",
                        "datetime_ms": _stamp_ms(str(item["timestamp"])), "original": item["original"]})
            return result
        result["original"] = value.get("original_uri")
        result["timegate"] = value.get("timegate_uri")
        listing = (value.get("mementos") or {}).get("list") or []
        for item in listing:
            uris = item.get("uri")
            for uri in uris if isinstance(uris, list) else [uris]:
                if uri:
                    result["mementos"].append({"uri_m": uri, "datetime_ms": _stamp_ms(item["datetime"])})
        return result
    for link in parse_link_format(text):
        rel = link["rel"]
        if "original" in rel:
            result["original"] = link["uri"]
        elif "timegate" in rel:
            result["timegate"] = link["uri"]
        if "memento" in rel and link.get("datetime"):
            result["mementos"].append({"uri_m": link["uri"], "datetime_ms": _stamp_ms(link["datetime"])})
    return result


def http_date(at: str | int) -> str:
    from datetime import UTC, datetime

    ms = _stamp_ms(at) if not isinstance(at, int) else at
    return format_datetime(datetime.fromtimestamp(ms / 1000, tz=UTC), usegmt=True)


def classify(status: int, raw: bytes) -> str:
    """Map an archive response to a per-archive outcome; nothing is retried or worked around."""
    if status == 429 or _CAPTCHA.search(raw[:65536]):
        return "blocked_by_archive"
    if status == 404:
        return "no_capture_on_record"
    if status == 403 or (status in {400, 451} and _EXCLUDED.search(raw[:65536])):
        return "excluded_by_archive"
    if 200 <= status < 400:
        return "ok"
    return "archive_unavailable"


# ---------------------------------------------------------------------- transport and budgets


def _request(*, url: str, params: Mapping[str, Any], headers: Mapping[str, str], timeout: float,
             max_bytes: int = 2_000_000) -> Mapping[str, Any]:
    """GET without following redirects, so TimeGate ``302`` answers are read as published."""
    query = urllib.parse.urlencode(params)
    target = url + ("?" + query if query else "")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, response_headers, newurl):  # noqa: ARG002
            return None

    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(urllib.request.Request(target, headers=dict(headers), method="GET"),  # noqa: S310
                         timeout=timeout) as response:
            content = response.read(max_bytes + 1)
            return {"status": response.status, "headers": dict(response.headers), "content": content,
                    "final_url": response.geturl()}
    except urllib.error.HTTPError as exc:
        try:
            return {"status": exc.code, "headers": dict(exc.headers or {}), "content": exc.read(65536)}
        finally:
            exc.close()


class Budget:
    """Per-archive request and byte budget for one resolution; exhaustion stops acquisition."""

    def __init__(self, archive_id: str, transport: Callable[..., Mapping[str, Any]], *,
                 max_requests: int | None = None, max_bytes: int | None = None, timeout_s: float | None = None):
        self.archive_id = archive_id
        self.transport = transport
        self.max_requests = max_requests or BOUNDED_COVERAGE["requests_per_resolution"].get(archive_id, 2)
        self.max_bytes = max_bytes or BOUNDED_COVERAGE["max_timemap_bytes"]
        self.timeout_s = timeout_s or BOUNDED_COVERAGE["timeout_s"]
        self.requests: list[dict[str, Any]] = []

    def get(self, url: str, *, params: Mapping[str, Any] | None = None,
            headers: Mapping[str, str] | None = None, hosts: Sequence[str] = ()) -> tuple[int, dict[str, str], bytes]:
        if len(self.requests) >= self.max_requests:
            raise MementoError("budget_exhausted", f"{self.archive_id} request budget exhausted")
        if hosts and (urlsplit(url).hostname or "") not in hosts:
            raise MementoError("host_not_configured", "request outside the archive's configured hosts")
        entry = {"url": url, "params": dict(params or {})}
        self.requests.append(entry)
        try:
            response = self.transport(url=url, params=dict(params or {}),
                                      headers={"User-Agent": USER_AGENT, **dict(headers or {})},
                                      timeout=self.timeout_s, max_bytes=self.max_bytes)
        except Exception as exc:  # noqa: BLE001 - transport failure is an unavailable archive, never retried
            entry.update(status=None, failure_type=type(exc).__name__)
            return 0, {}, b""
        raw = response.get("content", b"")
        raw = raw.encode() if isinstance(raw, str) else bytes(raw or b"")
        status = int(response.get("status", 200))
        entry.update(status=status, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        if len(raw) > self.max_bytes:
            entry["truncated_response"] = True
            raise MementoError("response_too_large", "archive response exceeds the TimeMap byte budget")
        return status, {k.lower(): v for k, v in (response.get("headers") or {}).items()}, raw


# ---------------------------------------------------------------------- resolution


_DDL = ("CREATE TABLE IF NOT EXISTS memento_acquisitions "
        "(request_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL, receipt TEXT NOT NULL)")


class MementoClient:
    """Resolve a URL's captures across archives into citation-preservation capture records."""

    def __init__(self, conn: Any, namespace: str, *, principal_id: str, scopes: Iterable[str],
                 transport: Callable[..., Mapping[str, Any]] | None = None,
                 archives: Mapping[str, Mapping[str, Any]] | None = None,
                 evidence_origin: str | None = None, now: Callable[[], int] | None = None) -> None:
        from src.kb.citation_preservation import CitationPreservationStore

        self.conn, self.namespace = conn, namespace
        self.principal_id, self.scopes = principal_id, set(scopes)
        self.transport = transport or _request
        self.archives = {k: dict(v) for k, v in (archives or ARCHIVES).items()}
        for spec in self.archives.values():
            if spec["access_decision"] not in ACCESS_DECISIONS:
                raise MementoError("invalid_archive_config", "access decision is in_scope, deferred or excluded")
        self.evidence_origin = evidence_origin or ("live" if transport is None else "injected")
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = CitationPreservationStore(conn, now=self.now)
        conn.execute(_DDL)

    # -- receipts

    def _replay(self, request_id: str, inputs: Any) -> dict[str, Any] | None:
        if not request_id:
            raise MementoError("invalid_request", "a request id is required")
        row = self.conn.execute("SELECT input_hash, receipt FROM memento_acquisitions WHERE request_id=?",
                                [request_id]).fetchone()
        if row:
            if row[0] != _digest(inputs):
                raise MementoError("request_id_conflict", "request id already used with different inputs")
            return {**json.loads(row[1]), "replayed": True}
        return None

    def _keep(self, request_id: str, inputs: Any, receipt: Mapping[str, Any]) -> dict[str, Any]:
        self.conn.execute("INSERT OR IGNORE INTO memento_acquisitions VALUES (?,?,?)",
                          [request_id, _digest(inputs), _canonical(receipt)])
        return {**receipt, "replayed": False}

    def receipt(self, request_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT receipt FROM memento_acquisitions WHERE request_id=?",
                                [request_id]).fetchone()
        return json.loads(row[0]) if row else None

    def _capture_receipt(self, request_id: str, adapter: str = ADAPTER_VERSION) -> dict[str, Any]:
        return {"request_id": request_id, "adapter": adapter, "retrieved_at_ms": self.now(),
                "evidence_origin": self.evidence_origin}

    # -- records

    def record(self, capture: Mapping[str, Any]) -> dict[str, Any]:
        return self.store.record_capture(self.namespace, capture, principal_id=self.principal_id,
                                         scopes=self.scopes)

    def snapshot(self, uri_r: str, archive_id: str, *, resolver: str, outcome: str,
                 capture_ids: Sequence[str] = (), truncated: bool = False,
                 detail: Mapping[str, Any] | None = None, receipt: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.store.record_timemap(self.namespace, uri_r, archive_id, resolver=resolver, outcome=outcome,
                                         capture_ids=capture_ids, truncated=truncated, detail=detail,
                                         receipt=receipt, retrieved_at_ms=self.now(),
                                         principal_id=self.principal_id, scopes=self.scopes)

    def _bounded(self, mementos: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
        limit = BOUNDED_COVERAGE["max_mementos_per_timemap"]
        return mementos[:limit], len(mementos) > limit

    # -- WA03: aggregator TimeMap and TimeGate

    def resolve_aggregator(self, uri_r: str, *, request_id: str, fmt: str = "json") -> dict[str, Any]:
        """Read the aggregator's TimeMap once; record each memento under the archive that holds it."""
        uri_r = validate_url(uri_r)
        spec = self.archives[AGGREGATOR]
        budget = Budget(AGGREGATOR, self.transport)
        receipt = self._capture_receipt(request_id)
        endpoint = spec["timemap_json"] if fmt == "json" else spec["timemap_link"]
        failure = None
        try:
            status, headers, raw = budget.get(endpoint + uri_r, hosts=spec["hosts"],
                                              headers={"Accept": "application/json" if fmt == "json"
                                                       else "application/link-format"})
            outcome = classify(status, raw) if status else "archive_unavailable"
        except MementoError as exc:
            outcome, failure, headers, raw = "archive_unavailable", exc.code, {}, b""
        mementos, truncated = [], False
        if outcome == "ok":
            try:
                mementos, truncated = self._bounded(parse_timemap(raw, headers.get("content-type", ""))["mementos"])
            except (ValueError, KeyError, TypeError) as exc:
                outcome, failure = "archive_unavailable", "unparseable_timemap:" + type(exc).__name__
        grouped: dict[str, list[dict[str, Any]]] = {}
        for memento in mementos:
            grouped.setdefault(archive_for(memento["uri_m"], self.archives), []).append(memento)
        per_archive: dict[str, dict[str, Any]] = {}
        withheld: dict[str, int] = {}
        for archive_id, spec_a in self.archives.items():
            if spec_a["kind"] != "memento-archive":
                continue
            found = grouped.pop(archive_id, [])
            if spec_a["access_decision"] != "in_scope":
                if found:
                    withheld[archive_id] = len(found)
                per_archive[archive_id] = {"outcome": decision_outcome(spec_a),
                                           "reason": spec_a.get("decision_reason"), "withheld_mementos": len(found)}
                continue
            if outcome != "ok":
                per_archive[archive_id] = {"outcome": "not_resolved",
                                           "reason": f"aggregator {outcome}"}
                continue
            if not found:
                snap = self.snapshot(uri_r, archive_id, resolver=AGGREGATOR, outcome="not_reported_by_aggregator",
                                     receipt=receipt)
                per_archive[archive_id] = {"outcome": "not_reported_by_aggregator", "timemap_id": snap["timemap_id"],
                                           "reason": "the aggregator listed no memento; not proof that none exists"}
                continue
            ids = [self.record({"archive_id": archive_id, "archive_kind": "memento-archive", "resolver": AGGREGATOR,
                                "uri_r": uri_r, "uri_m": m["uri_m"], "memento_at_ms": m["datetime_ms"],
                                "access_condition": access_condition(spec_a, m["uri_m"]),
                                "receipt": receipt})["capture_id"] for m in found]
            snap = self.snapshot(uri_r, archive_id, resolver=AGGREGATOR, outcome="captures", capture_ids=ids,
                                 truncated=truncated, receipt=receipt)
            per_archive[archive_id] = {"outcome": "captures", "capture_ids": sorted(set(ids)),
                                       "timemap_id": snap["timemap_id"], "truncated": truncated}
        for archive_id, found in grouped.items():  # hosts no audit covers: withheld, never recorded
            withheld[archive_id] = len(found)
        aggregate = self.snapshot(uri_r, AGGREGATOR, resolver=AGGREGATOR,
                                  outcome="captures" if outcome == "ok" and mementos else
                                  ("no_capture_on_record" if outcome in {"ok", "no_capture_on_record"}
                                   else outcome),
                                  capture_ids=[i for v in per_archive.values() for i in v.get("capture_ids", [])],
                                  truncated=truncated, detail={"withheld": withheld, "failure": failure},
                                  receipt=receipt)
        return {"resolver": AGGREGATOR, "outcome": outcome, "timemap_id": aggregate["timemap_id"],
                "archives": per_archive, "withheld": withheld, "truncated": truncated,
                "requests": budget.requests, "failure": failure}

    def timegate(self, uri_r: str, at: str | int, *, archive_id: str = AGGREGATOR, request_id: str) -> dict[str, Any]:
        """RFC 7089 datetime negotiation: ``Accept-Datetime`` to a TimeGate, reading the 302 as published."""
        uri_r = validate_url(uri_r)
        spec = self.archives[archive_id]
        if spec["access_decision"] != "in_scope" or not spec.get("timegate"):
            return {"archive_id": archive_id, "outcome": decision_outcome(spec)
                    if spec["access_decision"] != "in_scope" else "not_supported"}
        budget = Budget(archive_id, self.transport, max_requests=1)
        status, headers, raw = budget.get(spec["timegate"] + uri_r, hosts=spec["hosts"],
                                          headers={"Accept-Datetime": http_date(at)})
        outcome = classify(status, raw) if status else "archive_unavailable"
        answer: dict[str, Any] = {"archive_id": archive_id, "resolver": archive_id, "outcome": outcome,
                                  "accept_datetime": http_date(at), "requests": budget.requests}
        if outcome != "ok":
            return answer
        location = headers.get("location") or (headers.get("content-location"))
        memento_datetime = headers.get("memento-datetime")
        links = parse_link_format(headers.get("link", ""))
        if not location:
            location = next((link["uri"] for link in links if "memento" in link["rel"]), None)
        if location and not memento_datetime:
            memento_datetime = next((link.get("datetime") for link in links
                                     if link["uri"] == location and link.get("datetime")), None)
        if not location or not memento_datetime:
            # A TimeGate answer without its memento datetime is reported, not guessed.
            return {**answer, "outcome": "no_datetime_published", "location": location}
        holder = archive_for(location, self.archives) if spec["kind"] == "aggregator" else archive_id
        holder_spec = self.archives.get(holder)
        if not holder_spec or holder_spec["access_decision"] != "in_scope":
            return {**answer, "outcome": "withheld", "archive_id": holder,
                    "reason": "memento held by an archive without an in-scope access decision"}
        capture = self.record({"archive_id": holder, "archive_kind": "memento-archive", "resolver": archive_id,
                               "uri_r": uri_r, "uri_m": location, "memento_datetime": memento_datetime,
                               "access_condition": access_condition(holder_spec, location),
                               "receipt": self._capture_receipt(request_id)})
        return {**answer, "archive_id": holder, "capture_id": capture["capture_id"], "uri_m": location,
                "memento_datetime": capture["memento_datetime"]}

    # -- WA05 / WA06: direct archive TimeMaps

    def resolve_archive(self, uri_r: str, archive_id: str, *, request_id: str) -> dict[str, Any]:
        """Read one archive's own TimeMap. Failures stay per archive and never fail the resolution."""
        uri_r = validate_url(uri_r)
        spec = self.archives.get(archive_id)
        if spec is None or spec["kind"] != "memento-archive":
            raise MementoError("unknown_archive", f"{archive_id!r} is not a configured Memento archive")
        if spec["access_decision"] != "in_scope":
            outcome = decision_outcome(spec)
            snap = self.snapshot(uri_r, archive_id, resolver=archive_id, outcome=outcome,
                                 detail={"reason": spec.get("decision_reason")})
            return {"outcome": outcome, "reason": spec.get("decision_reason"), "timemap_id": snap["timemap_id"],
                    "requests": []}
        if archive_id == "internet-archive":
            from src.ingestion.wayback import acquire_wayback_mementos

            return acquire_wayback_mementos(self, uri_r, request_id=request_id)
        receipt = self._capture_receipt(request_id)
        budget = Budget(archive_id, self.transport)
        failure = None
        try:
            status, headers, raw = budget.get(spec["timemap_link"] + uri_r, hosts=spec["hosts"],
                                              headers={"Accept": "application/link-format"})
            outcome = classify(status, raw) if status else "archive_unavailable"
        except MementoError as exc:
            outcome, failure, headers, raw = "archive_unavailable", exc.code, {}, b""
        if outcome == "ok":
            try:
                mementos, truncated = self._bounded(parse_timemap(raw, headers.get("content-type", ""))["mementos"])
            except (ValueError, KeyError, TypeError) as exc:
                mementos, truncated = [], False
                outcome, failure = "archive_unavailable", "unparseable_timemap:" + type(exc).__name__
        else:
            mementos, truncated = [], False
        if outcome != "ok":
            detail = {"http_status": budget.requests[-1].get("status") if budget.requests else None,
                      "failure": failure, "retry_elsewhere": False}
            snap = self.snapshot(uri_r, archive_id, resolver=archive_id, outcome=outcome, detail=detail,
                                 receipt=receipt)
            return {"outcome": outcome, "timemap_id": snap["timemap_id"], "detail": detail,
                    "requests": budget.requests}
        foreign = [m for m in mementos if archive_for(m["uri_m"], self.archives) != archive_id]
        own = [m for m in mementos if m not in foreign]
        ids = [self.record({"archive_id": archive_id, "archive_kind": "memento-archive", "resolver": archive_id,
                            "uri_r": uri_r, "uri_m": m["uri_m"], "memento_at_ms": m["datetime_ms"],
                            "access_condition": access_condition(spec, m["uri_m"]),
                            "receipt": receipt})["capture_id"] for m in own]
        reading_room = sum(1 for m in own if access_condition(spec, m["uri_m"]) == "reading-room-only")
        snap = self.snapshot(uri_r, archive_id, resolver=archive_id,
                             outcome="captures" if ids else "no_capture_on_record", capture_ids=ids,
                             truncated=truncated,
                             detail={"reading_room_only": reading_room, "foreign_mementos_withheld": len(foreign)},
                             receipt=receipt)
        return {"outcome": "captures" if ids else "no_capture_on_record", "capture_ids": sorted(set(ids)),
                "timemap_id": snap["timemap_id"], "truncated": truncated, "reading_room_only": reading_room,
                "requests": budget.requests}

    # -- combined resolution

    def resolve(self, uri_r: str, *, request_id: str, at: str | None = None, via_aggregator: bool = True,
                archives: Sequence[str] | None = None, crawls: Sequence[str] = ()) -> dict[str, Any]:
        """Resolve a URL across archives: every configured archive gets an explicit per-archive outcome."""
        uri_r = validate_url(uri_r)
        crawls = list(crawls)
        if len(crawls) > BOUNDED_COVERAGE["max_crawls_per_resolution"]:
            raise MementoError("budget_exhausted", "too many crawls for one resolution")
        direct = list(archives) if archives is not None else ([] if via_aggregator else [
            a for a, s in self.archives.items() if s["kind"] == "memento-archive" and s["access_decision"] == "in_scope"])
        inputs = {"uri_r": uri_r, "at": at, "via_aggregator": via_aggregator, "archives": direct, "crawls": crawls,
                  "namespace": self.namespace}
        prior = self._replay(request_id, inputs)
        if prior:
            return prior
        started = self.now()
        report: dict[str, dict[str, Any]] = {}
        aggregator = None
        if via_aggregator:
            aggregator = self.resolve_aggregator(uri_r, request_id=f"{request_id}:{AGGREGATOR}")
            report.update({k: {**v, "resolver": AGGREGATOR} for k, v in aggregator["archives"].items()})
        for archive_id in direct:
            try:
                result = self.resolve_archive(uri_r, archive_id, request_id=f"{request_id}:{archive_id}")
            except MementoError as exc:
                result = {"outcome": "archive_unavailable", "failure": exc.code}
            report[archive_id] = {**result, "resolver": archive_id}
        for archive_id, spec in self.archives.items():
            if spec["kind"] == "memento-archive" and archive_id not in report:
                report[archive_id] = ({"outcome": "not_queried"} if spec["access_decision"] == "in_scope" else
                                      {"outcome": decision_outcome(spec), "reason": spec.get("decision_reason")})
        crawl_results = []
        if crawls:
            from src.ingestion.common_crawl import lookup_url_captures

            for crawl in crawls:
                crawl_results.append(lookup_url_captures(self, uri_r, crawl=crawl,
                                                         request_id=f"{request_id}:common-crawl:{crawl}"))
        report["common-crawl"] = ({"outcome": "not_queried", "archive_kind": "crawl-corpus"} if not crawls else {
            "archive_kind": "crawl-corpus", "resolver": "common-crawl",
            "outcome": "captures" if any(r["capture_ids"] for r in crawl_results) else (
                "no_capture_on_record" if all(r["outcome"] == "no_capture_on_record" for r in crawl_results)
                else "archive_unavailable"),
            "capture_ids": sorted({i for r in crawl_results for i in r["capture_ids"]}),
            "crawls": crawl_results, "timegate_answer": False})
        timegate = None
        if at is not None and via_aggregator:
            try:
                timegate = self.timegate(uri_r, at, request_id=f"{request_id}:timegate")
            except MementoError as exc:
                timegate = {"outcome": "archive_unavailable", "failure": exc.code}
        receipt = {"contract": RESOLUTION_CONTRACT, "request_id": request_id, "namespace": self.namespace,
                   "uri_r": uri_r, "at": at, "resolver": AGGREGATOR if via_aggregator else None,
                   "archives": {k: report[k] for k in sorted(report)},
                   "withheld": (aggregator or {}).get("withheld", {}), "timegate": timegate,
                   "adapter_version": ADAPTER_VERSION, "evidence_origin": self.evidence_origin,
                   "started_at_ms": started, "finished_at_ms": self.now(),
                   "live_verification": {k: v["status"] for k, v in LIVE_VERIFICATION.items()},
                   "exclusions": list(EXCLUSIONS)}
        return self._keep(request_id, inputs, receipt)
