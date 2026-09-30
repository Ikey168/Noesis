"""Offline harness for web-archive provenance tests (#2226): a routing fake transport over synthetic fixtures."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from src.kb.citation_preservation import (
    ARCHIVE_REQUEST_SCOPE,
    CAPTURE_SCOPE,
    READ_SCOPE,
    REPAIR_SCOPE,
    WRITE_SCOPE,
)

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests/fixtures/web_archives"
NS = "research"
URL = "https://example.org/report"
CITED = "https://www.example.org/report/?utm_source=newsletter"
CRAWL = "CC-MAIN-2024-10"
READ_ONLY = {READ_SCOPE, f"namespace:{NS}:read"}
SCOPES = {READ_SCOPE, WRITE_SCOPE, CAPTURE_SCOPE, REPAIR_SCOPE, f"namespace:{NS}:read", f"namespace:{NS}:write",
          "knowledge:subscriptions:read", "knowledge:subscriptions:write"}
ARCHIVE_WRITE = SCOPES | {ARCHIVE_REQUEST_SCOPE}
IA_MEMENTO = "http://web.archive.org/web/20240215093000/https://example.org/report"


def text(name: str) -> bytes:
    return (FIX / name).read_bytes()


def routes() -> dict[str, Any]:
    """URL prefix -> response. The longest matching prefix wins."""
    return {
        "https://timetravel.mementoweb.org/timemap/json/": {
            "status": 200, "headers": {"Content-Type": "application/json"}, "content": text("timetravel_timemap.json")},
        "https://timetravel.mementoweb.org/timegate/": {
            "status": 302,
            "headers": {"Location": IA_MEMENTO, "Vary": "accept-datetime",
                        "Link": f'<{URL}>; rel="original", <{IA_MEMENTO}>; rel="memento"; '
                                'datetime="Thu, 15 Feb 2024 09:30:00 GMT"'},
            "content": b""},
        "https://web.archive.org/web/timemap/link/": {
            "status": 200, "headers": {"Content-Type": "application/link-format"}, "content": text("ia_timemap.link")},
        "https://web.archive.org/cdx/search/cdx": {
            "status": 200, "headers": {"Content-Type": "application/json"}, "content": text("ia_cdx.json")},
        "https://www.webarchive.org.uk/wayback/archive/timemap/link/": {
            "status": 200, "headers": {"Content-Type": "application/link-format"},
            "content": text("ukwa_timemap.link")},
        "https://arquivo.pt/wayback/timemap/link/": {"status": 404, "headers": {}, "content": b"Not Found"},
        "https://archive.ph/timemap/": {
            "status": 200, "headers": {"Content-Type": "application/link-format"},
            "content": text("archive_today_timemap.link")},
        "https://index.commoncrawl.org/": {
            "status": 200, "headers": {"Content-Type": "text/x-ndjson"}, "content": text("cc_index.jsonl")},
    }


class FakeArchives:
    """Injected transport: never touches the network; records every request it answers."""

    def __init__(self, overrides: dict[str, Any] | None = None) -> None:
        self.routes = {**routes(), **(overrides or {})}
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *, url, params, headers, timeout, max_bytes, **extra):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {}), **extra})
        match = max((p for p in self.routes if url.startswith(p)), key=len, default=None)
        if match is None:
            raise ConnectionError(f"no fixture route for {url}")
        response = self.routes[match]
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(url=url, params=params, headers=headers, **extra)
        return dict(response)

    def hosts(self) -> list[str]:
        from urllib.parse import urlsplit

        return [urlsplit(c["url"]).hostname for c in self.calls]


def connect() -> Any:
    return duckdb.connect()


def clock(start: int = 1_700_000_000_000):
    ticks = iter(range(start, start + 10_000_000_000, 1_000))
    return lambda: next(ticks)


def client(conn, transport=None, *, archives=None, scopes=None, principal="alice", now=None):
    from src.ingestion.memento import MementoClient

    return MementoClient(conn, NS, principal_id=principal, scopes=scopes or SCOPES,
                         transport=transport or FakeArchives(), archives=archives, evidence_origin="fixture",
                         now=now or clock())
