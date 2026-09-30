"""Shared offline harness for the Economics logistics tests (#2229): pinned fixtures through the real adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.logistics_sources import LogisticsAdapter, fixture_transport
from src.ingestion.source_packs import validate_source_pack
from src.kb.logistics_series import LogisticsProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/logistics"
PACK_PATH = ROOT / "config/source_packs/economic-logistics.json"
NS = "global"
SCOPES = {
    "knowledge:logistics:read",
    "knowledge:logistics:write",
    "knowledge:logistics:review",
    "knowledge:trade:read",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "namespace:global:read",
    "namespace:global:write",
}
READ_ONLY = {"knowledge:logistics:read", "namespace:global:read"}
SOURCES = {
    "unlocode": "unece-unlocode-ports",
    "unctad": "unctadstat-maritime-series",
    "eurostat": "eurostat-maritime-transport",
    "bls": "bls-ppi-deep-sea-freight",
}
# Later releases: (fixture file, index of the declared document the release replaces).
REVISIONS = {
    "unlocode": ("unlocode_release_2099-2.json", 0),
    "unctad": ("unctad_port_calls_revision_2099-11.json", 0),
    "bls": ("bls_ppi_revision_2099-10.json", 0),
}
FIRST_RETRIEVAL = 4_099_766_400_000  # 2099-12-01
SECOND_RETRIEVAL = 4_102_444_800_000  # 2100-01-01
# A Hamburg crosswalk row in the shape of Eurostat's port code list (authored; the code list must be verified).
CROSSWALK = {
    "publisher": "Eurostat (maritime reporting-port code list)",
    "source_scheme": "eurostat-port",
    "citation": {"title": "Maritime transport statistics - list of reporting ports (authored fixture; verify)",
                 "url": "https://ec.europa.eu/eurostat/cache/metadata/en/mar_esms.htm", "published_on": "2099-01-31"},
    "rows": [{"source_code": "DE001", "unlocode": "DEHAM", "label": "Hamburg"}],
}


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads(PACK_PATH.read_text()))


def source(name: str, *, revision: bool = False) -> dict[str, Any]:
    item = next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[name])
    if revision:
        file, index = REVISIONS[name]
        item = json.loads(json.dumps(item))
        item["logistics"]["documents"] = [json.loads((FIXTURES / file).read_text())["document"]]
        del index
    return item


def pages(name: str, revision: bool = False) -> list[dict[str, Any]]:
    if revision:
        return json.loads((FIXTURES / REVISIONS[name][0]).read_text())["native_pages"]
    return json.loads((ROOT / source(name)["fixture"]["path"]).read_text())["native_pages"]


def fetch(name: str, *, revision: bool = False, transport=None) -> list[list[dict[str, Any]]]:
    item = source(name, revision=revision)
    adapter = LogisticsAdapter(item, transport=transport or fixture_transport(pages(name, revision)))
    out, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "release", "parameters": {}, "limit": item["budgets"]["max_results"]},
                                  cursor=cursor)
        out.append([dict(r) for r in page.records])
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, name: str, *, revision: bool = False, retrieved_at_ms: int | None = None) -> list[dict[str, Any]]:
    """Project every page of a source into the store (one release per page), as the runtime would."""
    item = source(name, revision=revision)
    projector = LogisticsProjector(conn)
    if retrieved_at_ms is not None:
        projector.store.now = lambda: retrieved_at_ms
    results = []
    for records in fetch(name, revision=revision):
        results += projector.project_page(run_id=f"run:{name}:{'rev' if revision else 'first'}", manifest=None,
                                          source=item, records=records, documents=None, page_receipt=None,
                                          principal_id="svc")
    return results


def load_all(conn, *, revisions: bool = False) -> None:
    for name in ("unlocode", "unctad", "eurostat", "bls"):
        apply(conn, name, retrieved_at_ms=FIRST_RETRIEVAL)
    if revisions:
        for name in ("unlocode", "unctad", "bls"):
            apply(conn, name, revision=True, retrieved_at_ms=SECOND_RETRIEVAL)


def day_ms(day: str) -> int:
    from src.kb.logistics_records import day_ms as to_ms

    return to_ms(day)
