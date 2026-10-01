"""Shared offline harness for the Society income-distribution tests (#2583): pinned fixtures through the real adapter."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.income_distribution_sources import (
    IncomeDistributionAdapter,
    fixture_transport,
)
from src.ingestion.source_packs import validate_source_pack
from src.kb.income_distribution_store import IncomeProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/income_distribution"
NS = "global"
SCOPES = {
    "knowledge:income:read",
    "knowledge:income:write",
    "knowledge:income:review",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:demographics:read",
    "knowledge:labour:read",
    "namespace:global:read",
    "namespace:global:write",
}
READ_ONLY = {"knowledge:income:read", "namespace:global:read"}
SOURCES = {
    "pip": "worldbank-pip-poverty-inequality",
    "eusilc": "eurostat-eu-silc-income",
    "oecd": "oecd-income-distribution-database",
}
# Retrieval clocks (epoch ms) for the first acquisitions and the re-publications.
FIRST_RETRIEVAL = 4_099_766_400_000  # 2099-12-01
SECOND_RETRIEVAL = 4_102_444_800_000  # 2100-01-01


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads((ROOT / "config/source_packs/society.json").read_text()))


def source(name: str, *, revision: bool = False) -> dict[str, Any]:
    item = next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[name])
    if revision:
        documents = json.loads((FIXTURES / f"{name}_revision.json").read_text()).get("documents")
        if documents:  # a later PIP release is a newly pinned request (IP03)
            item = {**item, "income_distribution": {**item["income_distribution"], "documents": documents}}
    return item


def pages(name: str, revision: bool = False) -> list[dict[str, Any]]:
    if revision:
        return json.loads((FIXTURES / f"{name}_revision.json").read_text())["native_pages"]
    return json.loads((ROOT / source(name)["fixture"]["path"]).read_text())["native_pages"]


def fetch(name: str, *, revision: bool = False, transport=None, item=None) -> list[list[dict[str, Any]]]:
    item = item or source(name, revision=revision)
    adapter = IncomeDistributionAdapter(item, transport=transport or fixture_transport(pages(name, revision)))
    out, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "release", "parameters": {}, "limit": item["budgets"]["max_results"]},
                                  cursor=cursor)
        out.append([dict(r) for r in page.records])
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, name: str, *, revision: bool = False, retrieved_at_ms: int | None = None,
          item=None) -> list[dict[str, Any]]:
    """Project every page of a source (one release per page), as the runtime would."""
    item = item or source(name, revision=revision)
    projector = IncomeProjector(conn)
    if retrieved_at_ms is not None:
        projector.store.now = lambda: retrieved_at_ms
    results = []
    for records in fetch(name, revision=revision, item=item):
        results += projector.project_page(run_id=f"run:{name}:{'rev' if revision else 'first'}", manifest=None,
                                          source=item, records=records, documents=None, page_receipt=None,
                                          principal_id="svc")
    return results


def load_all(conn, *, revisions: bool = False) -> None:
    for name in SOURCES:
        apply(conn, name, retrieved_at_ms=FIRST_RETRIEVAL)
    if revisions:
        for name in SOURCES:
            apply(conn, name, revision=True, retrieved_at_ms=SECOND_RETRIEVAL)


def day_ms(day: str) -> int:
    from datetime import date, datetime

    return int(datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=UTC).timestamp() * 1000)


def series_where(conn, provider: str, **match) -> dict[str, Any]:
    """The one series of a provider whose view matches every given field (``area`` compares the area code)."""
    from src.kb.income_distribution_store import IncomeStore

    found = []
    for series in IncomeStore(conn).find_series(NS, provider=provider):
        view = {**series, "area": series["area"]["code"], "concept": series["indicator"]["concept"]}
        if all(view.get(k) == v for k, v in match.items()):
            found.append(series)
    assert len(found) == 1, (provider, match, [s["native_key"] for s in found])
    return found[0]


PLACES = {
    "de": ("Germany", "country", {"iso3166-1-alpha3": "DEU", "iso3166-1-alpha2": "DE"}),
    "at": ("Austria", "country", {"iso3166-1-alpha3": "AUT", "iso3166-1-alpha2": "AT"}),
    "id": ("Indonesia", "country", {"iso3166-1-alpha3": "IDN", "iso3166-1-alpha2": "ID"}),
    "us": ("United States", "country", {"iso3166-1-alpha3": "USA", "iso3166-1-alpha2": "US"}),
    "be": ("Berlin", "region", {"nuts": "DE30"}),
    "ssf": ("Sub-Saharan Africa", "region", {"wb-region": "SSF"}),
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
