"""Shared offline harness for the Economics labour-statistics tests (#2219): pinned fixtures through the real adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.labour_sources import FIXTURE_SECRET, LabourStatisticsAdapter, fixture_transport
from src.ingestion.source_packs import validate_source_pack
from src.kb.labour_statistics import LabourProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/labour"
NS = "global"
SCOPES = {
    "knowledge:labour:read",
    "knowledge:labour:write",
    "knowledge:labour:review",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:demographics:read",
    "namespace:global:read",
    "namespace:global:write",
}
READ_ONLY = {"knowledge:labour:read", "namespace:global:read"}
SOURCES = {
    "ilostat": "ilostat-labour-indicators",
    "oecd": "oecd-labour-statistics",
    "eurostat": "eurostat-lfs-labour",
    "bls": "bls-public-data-api",
}
# Retrieval clocks (epoch ms) for the first acquisitions and the re-publications.
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
        return json.loads((FIXTURES / f"{name}_revision.json").read_text())["native_pages"]
    return json.loads((ROOT / source(name)["fixture"]["path"]).read_text())["native_pages"]


def fetch(name: str, *, revision: bool = False, transport=None, item=None) -> list[list[dict[str, Any]]]:
    item = item or source(name)
    adapter = LabourStatisticsAdapter(item, transport=transport or fixture_transport(pages(name, revision)),
                                      secret=FIXTURE_SECRET)
    out, cursor = [], None
    while True:
        page = adapter.fetch_page(
            {"operation": "release", "parameters": {}, "limit": item["budgets"]["max_results"]}, cursor=cursor
        )
        out.append([dict(r) for r in page.records])
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, name: str, *, revision: bool = False, retrieved_at_ms: int | None = None,
          item=None) -> list[dict[str, Any]]:
    """Project every page of a source (one release per page), as the runtime would."""
    item = item or source(name)
    projector = LabourProjector(conn)
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
    from datetime import date, datetime, timezone

    return int(datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)


def series_by_key(conn, provider: str, native_key: str) -> dict[str, Any]:
    from src.kb.labour_statistics import LabourStore

    return next(s for s in LabourStore(conn).find_series(NS, provider=provider) if s["native_key"] == native_key)


PLACES = {
    "de": ("Germany", "country", {"iso3166-1-alpha3": "DEU", "iso3166-1-alpha2": "DE"}),
    "us": ("United States", "country", {"iso3166-1-alpha3": "USA", "iso3166-1-alpha2": "US"}),
    "ca": ("California", "state", {"us-fips-state": "06"}),
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


def import_concordances(conn) -> list[dict[str, Any]]:
    from src.kb.labour_identity import LabourIdentity

    tables = json.loads((FIXTURES / "concordances.json").read_text())["tables"]
    identity = LabourIdentity(conn)
    return [identity.import_concordance(NS, table, principal_id="op", scopes=SCOPES) for table in tables]
