"""Shared offline harness for the Economics tourism-statistics tests (#2739): pinned fixtures through the real adapter."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.source_packs import validate_source_pack
from src.ingestion.tourism_sources import TourismStatisticsAdapter, fixture_transport
from src.kb.tourism_store import TourismProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/tourism"
NS = "global"
SCOPES = {
    "knowledge:tourism:read",
    "knowledge:tourism:write",
    "knowledge:tourism:review",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:labour:read",
    "namespace:global:read",
    "namespace:global:write",
}
READ_ONLY = {"knowledge:tourism:read", "namespace:global:read"}
GEO_NS = "geo"  # Geospatial feature imports write to a caller namespace, never global
SOURCES = {"occupancy": "eurostat-tourism-occupancy", "capacity": "eurostat-tourism-capacity"}
REVISIONS = {"occupancy": "occupancy_revision.json", "capacity": "capacity_revision.json",
             "nuts2024": "capacity_nuts2024.json"}
# Retrieval clocks (epoch ms): every fixture release (2024) precedes the retrieval that reads it.
FIRST_RETRIEVAL = 1_711_929_600_000  # 2024-04-01
SECOND_RETRIEVAL = 1_719_792_000_000  # 2024-07-01
THIRD_RETRIEVAL = 1_727_740_800_000  # 2024-10-01


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads((ROOT / "config/source_packs/economic.json").read_text()))


def source(name: str) -> dict[str, Any]:
    return next(s for s in manifest()["sources"] if s.get("source_id") == SOURCES[name])


def revision_source(name: str) -> dict[str, Any]:
    """The source as the operator declares it for a revision fixture (a re-declared NUTS version, if any)."""
    item = json.loads(json.dumps(source("capacity" if name == "nuts2024" else name)))
    declared = json.loads((FIXTURES / REVISIONS[name]).read_text()).get("release_declarations") or {}
    if declared.get("nuts_version"):
        for document in item["tourism_statistics"]["documents"]:
            document["area"]["nuts_version"] = declared["nuts_version"]
    return item


def pages(name: str, revision: bool = False) -> list[dict[str, Any]]:
    if revision:
        return json.loads((FIXTURES / REVISIONS[name]).read_text())["native_pages"]
    return json.loads((ROOT / source(name)["fixture"]["path"]).read_text())["native_pages"]


def fetch(name: str, *, revision: bool = False, transport=None, item=None) -> list[list[dict[str, Any]]]:
    item = item or (revision_source(name) if revision else source(name))
    adapter = TourismStatisticsAdapter(item, transport=transport or fixture_transport(pages(name, revision)))
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
    projector = TourismProjector(conn)
    if retrieved_at_ms is not None:
        projector.store.now = lambda: retrieved_at_ms
    results = []
    for records in fetch(name, revision=revision, item=item):
        results += projector.project_page(run_id=f"run:{name}:{'rev' if revision else 'first'}", manifest=None,
                                          source=item, records=records, documents=None, page_receipt=None,
                                          principal_id="svc")
    return results


def load_all(conn, *, revisions: bool = False, nuts2024: bool = False) -> None:
    for name in SOURCES:
        apply(conn, name, retrieved_at_ms=FIRST_RETRIEVAL)
    if revisions:
        for name in SOURCES:
            apply(conn, name, revision=True, retrieved_at_ms=SECOND_RETRIEVAL)
    if nuts2024:
        apply(conn, "nuts2024", revision=True, retrieved_at_ms=THIRD_RETRIEVAL)


def day_ms(day: str) -> int:
    from datetime import date, datetime

    return int(datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=UTC).timestamp() * 1000)


def series_by(conn, provider: str | None = None, **match: Any) -> dict[str, Any]:
    """The one series whose dataset, indicator code, residence, area code or NUTS version match."""
    from src.kb.tourism_store import TourismStore

    def ok(series):
        checks = {
            "dataset": series["dataset"], "indicator": series["indicator"]["code"],
            "concept": series["indicator"]["concept"], "residence": series["residence"]["code"],
            "area": series["area"]["code"], "nuts_version": series["area"]["nuts_version"],
        }
        return all(checks[k] == v for k, v in match.items())

    found = [s for s in TourismStore(conn).find_series(NS, provider=provider) if ok(s)]
    assert len(found) == 1, (provider, match, [s["native_key"] for s in found])
    return found[0]


PLACES = {
    "de": ("Germany", "country", {"iso3166-1-alpha3": "DEU", "iso3166-1-alpha2": "DE", "m49": "276"}),
    "berlin": ("Berlin", "region", {"nuts": "DE30", "nuts-version": "2021"}),
    "berlin-2024": ("Berlin (NUTS 2024)", "region", {"nuts-2024": "DE30"}),
}


def register_places(conn, geo_namespace: str = "global", keys=("de", "berlin")) -> dict[str, str]:
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


def register_boundaries(conn, version: str = "2021", codes=("DE", "DE30"), geo_namespace: str = GEO_NS) -> None:
    """Import authored NUTS boundary features (a GISCO-shaped collection; geometry is a placeholder square)."""
    from src.kb.geospatial_features import GeospatialFeatureStore

    square = [[[13.1, 52.3], [13.8, 52.3], [13.8, 52.7], [13.1, 52.7], [13.1, 52.3]]]
    collection = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": f"{code}_{version}", "geometry": {"type": "Polygon", "coordinates": square},
         "properties": {"NUTS_ID": code, "LEVL_CODE": len(code) - 2, "NUTS_NAME": code}} for code in codes]}
    GeospatialFeatureStore(conn).import_feature_collection(
        geo_namespace, json.dumps(collection), provider="gisco", collection=f"gisco:nuts:{version}",
        source_crs="EPSG:4326", title_property="NUTS_NAME", principal_id="op",
        scopes={"knowledge:geospatial:write", "knowledge:geospatial:read", f"namespace:{geo_namespace}:write"})


def correspondence_tables() -> list[dict[str, Any]]:
    return json.loads((FIXTURES / "nuts_correspondence.json").read_text())["tables"]


def load_labour_accommodation(conn, *, retrieved_at_ms: int = SECOND_RETRIEVAL,
                              area_scheme: str | None = None) -> list[dict[str, Any]]:
    """An operator-declared Eurostat LFS document for NACE Rev.2 section I through the real labour adapter
    (``area_scheme`` re-declares the place codes under another scheme, so only an accepted match links them)."""
    from src.ingestion.labour_sources import LabourStatisticsAdapter
    from src.ingestion.labour_sources import fixture_transport as labour_transport
    from src.kb.labour_statistics import LabourProjector

    fixture = json.loads((FIXTURES / "labour_accommodation.json").read_text())
    item = json.loads(json.dumps(next(s for s in manifest()["sources"] if s.get("source_id") ==
                                      "eurostat-lfs-labour")))
    document = json.loads(json.dumps(fixture["document"]))
    if area_scheme:
        document["area"]["scheme"] = area_scheme
    item["labour_statistics"]["documents"] = [document]
    adapter = LabourStatisticsAdapter(item, transport=labour_transport(fixture["native_pages"]))
    page = adapter.fetch_page({"operation": "release", "parameters": {}, "limit": item["budgets"]["max_results"]},
                              cursor=None)
    projector = LabourProjector(conn)
    projector.store.now = lambda: retrieved_at_ms
    return projector.project_page(run_id="run:labour-accommodation", manifest=None, source=item,
                                  records=[dict(r) for r in page.records], documents=None, page_receipt=None,
                                  principal_id="svc")
