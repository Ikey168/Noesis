"""Shared offline harness for the Economics business-statistics tests (#2738): pinned fixtures through the real adapter."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.business_statistics_sources import (
    FIXTURE_SECRET,
    BusinessStatisticsAdapter,
    fixture_transport,
)
from src.ingestion.source_packs import validate_source_pack
from src.kb.business_statistics_store import BusinessStatisticsProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/business"
NS = "global"
SCOPES = {
    "knowledge:business:read",
    "knowledge:business:write",
    "knowledge:business:review",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:labour:read",
    "knowledge:trade:read",
    "namespace:global:read",
    "namespace:global:write",
}
READ_ONLY = {"knowledge:business:read", "namespace:global:read"}
SOURCES = {"sts": "eurostat-sts", "bd": "eurostat-business-demography", "cbp": "us-census-cbp"}
REVISIONS = {"sts": "eurostat_sts_revision.json", "bd": "eurostat_bd_revision.json",
             "cbp": "census_cbp_revision.json", "rebase": "eurostat_sts_rebase.json"}
# Retrieval clocks (epoch ms): every fixture release (2024) precedes the retrieval that reads it.
FIRST_RETRIEVAL = 1_725_148_800_000  # 2024-09-01
SECOND_RETRIEVAL = 1_733_011_200_000  # 2024-12-01


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads((ROOT / "config/source_packs/economic.json").read_text()))


def source(name: str) -> dict[str, Any]:
    return next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[name])


def revision_source(name: str) -> dict[str, Any]:
    """The source as the operator declares it for a revision fixture (a re-declared release date, if any)."""
    item = source("sts" if name == "rebase" else name)
    declared = json.loads((FIXTURES / REVISIONS[name]).read_text()).get("release_declarations") or {}
    if declared:
        item = json.loads(json.dumps(item))
        for document in item["business_statistics"]["documents"]:
            if document.get("year") in declared:
                document["release"] = declared[document["year"]]
    return item


def pages(name: str, revision: bool = False) -> list[dict[str, Any]]:
    if revision:
        return json.loads((FIXTURES / REVISIONS[name]).read_text())["native_pages"]
    return json.loads((ROOT / source(name)["fixture"]["path"]).read_text())["native_pages"]


def fetch(name: str, *, revision: bool = False, transport=None, item=None,
          secret: str | None = FIXTURE_SECRET) -> list[list[dict[str, Any]]]:
    item = item or (revision_source(name) if revision else source(name))
    adapter = BusinessStatisticsAdapter(item, transport=transport or fixture_transport(pages(name, revision)),
                                        secret=secret)
    out, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "release", "parameters": {}, "limit": item["budgets"]["max_results"]},
                                  cursor=cursor)
        out.append([dict(r) for r in page.records])
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, name: str, *, revision: bool = False, retrieved_at_ms: int | None = None) -> list[dict[str, Any]]:
    """Project every page of a source (one release per page), as the runtime would."""
    item = revision_source(name) if revision else source(name)
    projector = BusinessStatisticsProjector(conn)
    if retrieved_at_ms is not None:
        projector.store.now = lambda: retrieved_at_ms
    results = []
    for records in fetch(name, revision=revision, item=item):
        results += projector.project_page(run_id=f"run:{name}:{'rev' if revision else 'first'}", manifest=None,
                                          source=item, records=records, documents=None, page_receipt=None,
                                          principal_id="svc")
    return results


def load_all(conn, *, revisions: bool = False, rebase: bool = False) -> None:
    for name in SOURCES:
        apply(conn, name, retrieved_at_ms=FIRST_RETRIEVAL)
    if revisions:
        for name in SOURCES:
            apply(conn, name, revision=True, retrieved_at_ms=SECOND_RETRIEVAL)
    if rebase:
        apply(conn, "rebase", revision=True, retrieved_at_ms=SECOND_RETRIEVAL)


def day_ms(day: str) -> int:
    from datetime import date, datetime

    return int(datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=UTC).timestamp() * 1000)


def series_by(conn, provider: str, **match: Any) -> dict[str, Any]:
    """The one series of a provider whose indicator code, classification code, adjustment or unit code match."""
    from src.kb.business_statistics_store import BusinessStatisticsStore

    def ok(series):
        checks = {
            "indicator": series["indicator"]["code"], "classification": series["classification"]["code"],
            "version": series["classification"]["version"], "adjustment": series["adjustment"],
            "unit": series["unit"]["code"],
        }
        return all(checks[k] == v for k, v in match.items())

    found = [s for s in BusinessStatisticsStore(conn).find_series(NS, provider=provider) if ok(s)]
    assert len(found) == 1, (provider, match, [s["native_key"] for s in found])
    return found[0]


PLACES = {
    "de": ("Germany", "country", {"iso3166-1-alpha3": "DEU", "iso3166-1-alpha2": "DE", "m49": "276"}),
    "ca": ("California", "state", {"us-fips-state": "06"}),
}


def register_places(conn, geo_namespace: str = "global", keys=tuple(PLACES)) -> dict[str, str]:
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


def concordance_tables() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "concordances.json").read_text())["tables"]
