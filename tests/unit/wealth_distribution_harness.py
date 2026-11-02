"""Shared offline harness for the Society ``society.wealth`` tests (#2882): pinned fixtures through the real adapter.

Every response under ``tests/fixtures/source_packs/society-wealth-*`` (first releases) and
``tests/fixtures/wealth_distribution`` (revisions) is authored in the publisher's documented shape (WID API JSON, ECB
csvdata, the DFA archive member) with synthetic values only: reference periods 2093-2098 and release dates in
2098-2099, so nothing can be mistaken for a published figure. Nothing here is live coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.source_packs import validate_source_pack
from src.ingestion.wealth_distribution_sources import (
    FIXTURE_SECRET,
    WealthAdapter,
    fixture_transport,
)
from src.kb.wealth_distribution_store import WealthProjector, WealthStore

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "config/source_packs/society-wealth-distribution.json"
REVISIONS = ROOT / "tests/fixtures/wealth_distribution"
NS = "global"
READ = "knowledge:wealth:read"
WRITE = "knowledge:wealth:write"
REVIEW = "knowledge:wealth:review"
SCOPES = {READ, WRITE, REVIEW, "namespace:global:read", "namespace:global:write"}
READ_ONLY = {READ, "namespace:global:read"}
SOURCES = {"wid": "wid-wealth-distribution", "dwa": "ecb-distributional-wealth-accounts",
           "dfa": "fed-distributional-financial-accounts"}
FIRST_RETRIEVAL = 4_069_008_000_000  # 2098-12-10
SECOND_RETRIEVAL = 4_084_646_400_000  # 2099-06-09
DE = {"scheme": "iso3166-1-alpha2", "code": "DE"}
FR = {"scheme": "iso3166-1-alpha2", "code": "FR"}
US = {"scheme": "iso3166-1-alpha2", "code": "US"}


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads(PACK.read_text()))


def revision(name: str) -> dict[str, Any]:
    return json.loads((REVISIONS / f"{SOURCES[name]}_revision.json").read_text())


def source(name: str, *, revised: bool = False) -> dict[str, Any]:
    item = json.loads(json.dumps(next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[name])))
    if revised:
        item["wealth"]["documents"] = revision(name)["documents"]
    return item


def pages(name: str, *, revised: bool = False) -> list[dict[str, Any]]:
    if revised:
        return revision(name)["native_pages"]
    return json.loads((ROOT / source(name)["fixture"]["path"]).read_text())["native_pages"]


def fetch(name: str, *, revised: bool = False, transport=None, item=None) -> list[list[dict[str, Any]]]:
    item = item or source(name, revised=revised)
    adapter = WealthAdapter(item, transport=transport or fixture_transport(pages(name, revised=revised)),
                            secret=FIXTURE_SECRET)
    out, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "release", "parameters": {},
                                   "limit": item["budgets"]["max_results"]}, cursor=cursor)
        out.append([dict(r) for r in page.records])
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, name: str, *, revised: bool = False, retrieved_at_ms: int | None = None) -> list[dict[str, Any]]:
    """Project every page of a source (one release per page), as the runtime would."""
    item = source(name, revised=revised)
    projector = WealthProjector(conn)
    retrieved = retrieved_at_ms or (SECOND_RETRIEVAL if revised else FIRST_RETRIEVAL)
    results = []
    for records in fetch(name, revised=revised):
        results += projector.project_page(run_id=f"run:{name}:{'rev' if revised else 'first'}", manifest=None,
                                          source=item, records=records, documents=[{"ingested_at": retrieved}],
                                          page_receipt=None, principal_id="svc")
    return results


def load_all(conn, *, revisions: bool = False) -> None:
    for name in SOURCES:
        apply(conn, name)
    if revisions:
        for name in SOURCES:
            apply(conn, name, revised=True)


def store(conn) -> WealthStore:
    return WealthStore(conn)


def series(conn, provider: str, native_key: str) -> dict[str, Any]:
    found = [s for s in WealthStore(conn).find_series(NS, provider=provider) if s["native_key"] == native_key]
    assert len(found) == 1, [s["native_key"] for s in WealthStore(conn).find_series(NS, provider=provider)]
    return found[0]
