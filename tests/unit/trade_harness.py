"""Shared offline harness for the Economics trade-flow tests (#2210): pinned fixtures through the real adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.source_packs import validate_source_pack
from src.ingestion.trade_sources import FIXTURE_SECRET, TradeFlowsAdapter, fixture_transport
from src.kb.trade_flows import TradeFlowProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/trade"
NS = "global"
SCOPES = {
    "knowledge:trade:read",
    "knowledge:trade:write",
    "knowledge:trade:review",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:legal:read",
    "knowledge:legal:write",
    "namespace:global:read",
    "namespace:global:write",
}
READ_ONLY = {"knowledge:trade:read", "namespace:global:read"}
SOURCES = {
    "comtrade": "un-comtrade-trade-flows",
    "comext": "eurostat-comext-trade-flows",
    "wits": "wits-classification-concordances",
}
REVISIONS = {"comtrade": "comtrade_revision_2099-09.json", "comext": "comext_revision_2099-04.json"}
# Retrieval clocks (epoch ms) used for the first acquisitions and the revisions.
FIRST_RETRIEVAL = 4_099_766_400_000  # 2099-12-01
SECOND_RETRIEVAL = 4_102_444_800_000  # 2100-01-01


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads((ROOT / "config/source_packs/economic.json").read_text()))


def source(name: str) -> dict[str, Any]:
    return next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[name])


def pages(name: str, revision: bool = False) -> list[dict[str, Any]]:
    if revision:
        return json.loads((FIXTURES / REVISIONS[name]).read_text())["native_pages"]
    item = source(name)
    return json.loads((ROOT / item["fixture"]["path"]).read_text())["native_pages"]


def fetch(name: str, *, revision: bool = False, transport=None) -> list[list[dict[str, Any]]]:
    """Every page's records for the source's declared documents."""
    item = source(name)
    adapter = TradeFlowsAdapter(
        item, transport=transport or fixture_transport(pages(name, revision)), secret=FIXTURE_SECRET
    )
    out, cursor = [], None
    while True:
        page = adapter.fetch_page(
            {"operation": "release", "parameters": {}, "limit": item["budgets"]["max_results"]}, cursor=cursor
        )
        out.append([dict(r) for r in page.records])
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, name: str, *, revision: bool = False, retrieved_at_ms: int | None = None) -> list[dict[str, Any]]:
    """Project every page of a source into the store (one release per page), as the runtime would."""
    item = source(name)
    projector = TradeFlowProjector(conn)
    if retrieved_at_ms is not None:
        projector.store.now = lambda: retrieved_at_ms
    results = []
    for records in fetch(name, revision=revision):
        results += projector.project_page(
            run_id=f"run:{name}:{'rev' if revision else 'first'}",
            manifest=None,
            source=item,
            records=records,
            documents=None,
            page_receipt=None,
            principal_id="svc",
        )
    return results


def load_all(conn, *, revisions: bool = False) -> None:
    apply(conn, "wits", retrieved_at_ms=FIRST_RETRIEVAL)
    apply(conn, "comtrade", retrieved_at_ms=FIRST_RETRIEVAL)
    apply(conn, "comext", retrieved_at_ms=FIRST_RETRIEVAL)
    if revisions:
        apply(conn, "comtrade", revision=True, retrieved_at_ms=SECOND_RETRIEVAL)
        apply(conn, "comext", revision=True, retrieved_at_ms=SECOND_RETRIEVAL)


def day_ms(day: str) -> int:
    from datetime import date, datetime, timezone

    return int(datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
