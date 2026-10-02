"""Shared offline harness for the Society ``society.income`` tests (#2583): pinned fixtures through the real adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.income_distribution_sources import (
    IncomeDistributionAdapter,
    fixture_transport,
)
from src.ingestion.source_packs import validate_source_pack
from src.kb.income_distribution_store import (
    IncomeDistributionProjector,
    IncomeDistributionStore,
)

ROOT = Path(__file__).resolve().parents[2]
REVISIONS = ROOT / "tests/fixtures/income_distribution"
NS = "global"
SCOPES = {
    "knowledge:income:read",
    "knowledge:income:write",
    "knowledge:income:review",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "namespace:global:read",
    "namespace:global:write",
}
READ_ONLY = {"knowledge:income:read", "namespace:global:read"}
SOURCES = {
    "pip": "pip-country-estimates",
    "pip-region": "pip-regional-aggregates",
    "silc": "eurostat-silc-income-poverty",
    "oecd": "oecd-idd-income-distribution",
}
FIRST_RETRIEVAL = 4_068_230_400_000  # 2098-12-01
SECOND_RETRIEVAL = 4_086_028_800_000  # 2099-06-25


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads((ROOT / "config/source_packs/society.json").read_text()))


def source(name: str, *, revision: bool = False) -> dict[str, Any]:
    item = next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[name])
    if revision:
        body = json.loads((REVISIONS / f"{SOURCES[name]}_revision.json").read_text())
        item = json.loads(json.dumps(item))
        item["income_distribution"]["documents"] = body["documents"]
    return item


def pages(name: str, *, revision: bool = False) -> list[dict[str, Any]]:
    if revision:
        return json.loads((REVISIONS / f"{SOURCES[name]}_revision.json").read_text())["native_pages"]
    return json.loads((ROOT / source(name)["fixture"]["path"]).read_text())["native_pages"]


def fetch(name: str, *, revision: bool = False, transport=None) -> list[list[dict[str, Any]]]:
    item = source(name, revision=revision)
    adapter = IncomeDistributionAdapter(item, transport=transport or fixture_transport(pages(name, revision=revision)))
    out, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "release", "parameters": {},
                                   "limit": item["budgets"]["max_results"]}, cursor=cursor)
        out.append([dict(r) for r in page.records])
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, name: str, *, revision: bool = False, retrieved_at_ms: int | None = None) -> list[dict[str, Any]]:
    """Project every page of a source (one release per page), as the runtime would."""
    item = source(name, revision=revision)
    projector = IncomeDistributionProjector(conn)
    documents = [{"ingested_at": retrieved_at_ms}] if retrieved_at_ms is not None else None
    results = []
    for records in fetch(name, revision=revision):
        results += projector.project_page(run_id=f"run:{name}:{'rev' if revision else 'first'}", manifest=None,
                                          source=item, records=records, documents=documents, page_receipt=None,
                                          principal_id="svc")
    return results


def load_all(conn, *, revisions: bool = False) -> None:
    for name in SOURCES:
        apply(conn, name, retrieved_at_ms=FIRST_RETRIEVAL)
    if revisions:
        for name in ("pip", "silc", "oecd"):
            apply(conn, name, revision=True, retrieved_at_ms=SECOND_RETRIEVAL)


def day_ms(day: str) -> int:
    from src.kb.income_distribution_records import to_ms

    return to_ms(day)


def store(conn) -> IncomeDistributionStore:
    return IncomeDistributionStore(conn)


def series(conn, provider: str, native_key_part: str, *, ppp: int | None = None) -> dict[str, Any]:
    found = [s for s in IncomeDistributionStore(conn).find_series(NS, provider=provider)
             if native_key_part in s["native_key"] and (ppp is None or s["key"].get("ppp_base_year") == ppp)]
    assert len(found) == 1, [s["native_key"] for s in found]
    return found[0]


PLACES = {
    "de": ("Germany", "country", {"iso3166-1-alpha3": "DEU", "iso3166-1-alpha2": "DE"}),
    "eca": ("Europe and Central Asia", "region", {"wb-region": "ECA"}),
    "be": ("Berlin", "region", {"nuts": "DE30"}),
}


def register_places(conn, geo_namespace: str = "geo", keys=tuple(PLACES)) -> dict[str, str]:
    """Register the fixture places in the Geospatial store; returns key -> place_id."""
    from src.kb.geospatial import GeospatialStore

    places = GeospatialStore(conn)
    out = {}
    for key in keys:
        name, kind, ids = PLACES[key]
        place = places.register_place(geo_namespace, name, kind, names=[{"value": name, "language": "en"}],
                                      source_ids=ids, parent_ids=[], principal_id="op",
                                      scopes={"knowledge:geospatial:write"}, place_key=f"fixture:{key}")
        out[key] = place["place_id"]
    return out
