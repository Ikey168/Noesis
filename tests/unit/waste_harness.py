"""Shared offline harness for the environment.waste tests (#2740): pinned fixtures through the real adapter.

The ``environment.core`` facility records the transfer rows attach to are the authored Berlin fixture facilities
(``... (authored fixture)``); every waste value is synthetic (reference years 2094-2097, releases 2098-2099).
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.source_packs import validate_source_pack
from src.ingestion.waste_sources import WasteAdapter, fixture_transport
from src.kb.waste_store import WasteProjector

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "packs/climate-environment/source_packs/climate-environment-waste.json"
FIXTURES = ROOT / "tests/fixtures/waste"
NS = "environment"
SCOPES = {
    "knowledge:waste:read",
    "knowledge:waste:write",
    "knowledge:waste:review",
    "knowledge:environment:read",
    "knowledge:environment:write",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:substances:read",
    "knowledge:products:read",
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
}
READ_ONLY = {"knowledge:waste:read", f"namespace:{NS}:read"}
REVIEWER = "reviewer-b"
SOURCES = ("eurostat-waste", "eurostat-circular-economy", "eea-industry-waste-transfers", "oecd-municipal-waste")
REVISIONS = {"eurostat-waste": "eurostat_waste_revision.json", "eurostat-circular-economy": "eurostat_cei_revision.json",
             "eea-industry-waste-transfers": "eea_transfers_revision.json", "oecd-municipal-waste": "oecd_revision.json"}
FACILITY_1 = "DE.UBA.PRTR/000000901.FACILITY"
FACILITY_2 = "DE.UBA.PRTR/000000902.FACILITY"
UNKNOWN_FACILITY = "DE.UBA.PRTR/000000999.FACILITY"
MERCURY_CAS = "7439-97-6"


def day_ms(day: str) -> int:
    return int(datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=UTC).timestamp() * 1000)


# Retrieval clocks: every fixture release (2098-2099) precedes the retrieval that reads it.
FIRST_RETRIEVAL = day_ms("2098-12-01")
SECOND_RETRIEVAL = day_ms("2099-06-01")


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(provider: str) -> dict[str, Any]:
    return next(s for s in manifest()["sources"] if s["source_id"] == provider)


def revision_source(provider: str) -> dict[str, Any]:
    """The source as the operator declares it for a revision fixture (a re-declared release date, if any)."""
    item = source(provider)
    declared = json.loads((FIXTURES / REVISIONS[provider]).read_text()).get("release_declarations") or {}
    if declared:
        item = json.loads(json.dumps(item))
        for document in item["waste"]["documents"]:
            if document.get("flow") in declared:
                document["release"] = declared[document["flow"]]
    return item


def pages(provider: str, revision: bool = False) -> list[dict[str, Any]]:
    if revision:
        return json.loads((FIXTURES / REVISIONS[provider]).read_text())["native_pages"]
    return json.loads((ROOT / source(provider)["fixture"]["path"]).read_text())["native_pages"]


def fetch(provider: str, *, revision: bool = False, transport=None, item=None) -> list[list[dict[str, Any]]]:
    item = item or (revision_source(provider) if revision else source(provider))
    adapter = WasteAdapter(item, transport=transport or fixture_transport(pages(provider, revision)))
    out, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "release", "parameters": {}, "limit": item["budgets"]["max_results"]},
                                  cursor=cursor)
        out.append([dict(r) for r in page.records])
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, provider: str, *, revision: bool = False, retrieved_at_ms: int | None = None, transport=None,
          item=None) -> list[dict[str, Any]]:
    """Project every page of a source (one release per page), as the runtime would."""
    item = item or (revision_source(provider) if revision else source(provider))
    projector = WasteProjector(conn)
    documents = [{"ingested_at": retrieved_at_ms}] if retrieved_at_ms is not None else None
    results = []
    for records in fetch(provider, revision=revision, item=item, transport=transport):
        results += projector.project_page(run_id=f"run:{provider}:{'rev' if revision else 'first'}", manifest=None,
                                          source=item, records=records, documents=documents, page_receipt=None,
                                          principal_id="svc")
    return results


def load_facilities(conn) -> dict[str, str]:
    """The environment.core facility records (authored Berlin fixtures) the transfer rows attach to."""
    from src.kb import environment_records as er
    from src.kb.environment_store import EnvironmentStore

    store = EnvironmentStore(conn, now=lambda: FIRST_RETRIEVAL)
    records = [
        er.facility("eea-industry", FACILITY_1, "HKW Mitte (authored fixture)", source_url="https://industry.eea.europa.eu/",
                    geometry={"type": "Point", "coordinates": [13.4262, 52.5105]},
                    operator={"name": "Beispiel Waerme Berlin GmbH (authored fixture)"},
                    identifiers={"inspire_id": FACILITY_1}, reporting_year=2096),
        er.facility("eea-industry", FACILITY_2, "HKW Klingenberg (authored fixture)",
                    source_url="https://industry.eea.europa.eu/",
                    geometry={"type": "Point", "coordinates": [13.4951, 52.4925]},
                    operator={"name": "Beispiel Energie Berlin AG (authored fixture)"},
                    identifiers={"inspire_id": FACILITY_2}, reporting_year=2096),
    ]
    store.apply(NS, records, run_id="run:environment-core:fixture", principal_id="svc",
                scopes={"knowledge:environment:write", "knowledge:environment:read", f"namespace:{NS}:write",
                        f"namespace:{NS}:read"}, observed_at_ms=FIRST_RETRIEVAL)
    return {i: store.find(NS, "facility", "eea-industry", i) for i in (FACILITY_1, FACILITY_2)}


def load_all(conn, *, revisions: bool = False, facilities: bool = True) -> None:
    if facilities:
        load_facilities(conn)
    for provider in SOURCES:
        apply(conn, provider, retrieved_at_ms=FIRST_RETRIEVAL)
    if revisions:
        for provider in SOURCES:
            apply(conn, provider, revision=True, retrieved_at_ms=SECOND_RETRIEVAL)


PLACES = {
    "de": ("Germany", "country", {"iso3166-1-alpha2": "DE", "iso3166-1-alpha3": "DEU", "nuts": "DE"}),
    "fr": ("France", "country", {"iso3166-1-alpha2": "FR", "iso3166-1-alpha3": "FRA", "nuts": "FR"}),
    "it": ("Italy", "country", {"iso3166-1-alpha2": "IT", "iso3166-1-alpha3": "ITA", "nuts": "IT"}),
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
                                      scopes={"knowledge:geospatial:write"}, place_key=f"fixture:waste:{key}")
        out[key] = place["place_id"]
    return out


def seed_chemicals(conn, namespace: str = NS) -> str:
    """A Chemicals substance publishing the CAS number the EEA document cites; returns its subject key."""
    from src.kb.substances_records import statement
    from src.kb.substances_store import SubstanceStore

    subject = {"key": "pubchem:cid:99000201", "kind": "unknown", "name": "mercury (authored fixture)"}
    src = {"url": "https://echa.europa.eu/", "locator": "/", "attribution": "authored test record (fictional)"}
    SubstanceStore(conn).observe(namespace, [
        statement("substance", "pubchem", subject, "element", {"preferred_name": "mercury"}, source=src),
        statement("identifier", "pubchem", subject, f"cas:{MERCURY_CAS}", {"scheme": "cas", "value": MERCURY_CAS},
                  source=src)])
    return subject["key"]


PACKAGING_URL = "https://ec.europa.eu/eurostat/databrowser/view/env_waspac/default/table"


def seed_products(conn, namespace: str = NS) -> str:
    """The Products provider's document link to the cited packaging dataset (as the Products store would hold it)."""
    from src.kb.products import _DDL

    conn.execute(_DDL)
    conn.execute("INSERT INTO product_document_links VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ["fixture-link:packaging", namespace, "fixture-variant:packaging", "fixture-revision:packaging",
                  "dataset", PACKAGING_URL, "en", "text/html", json.dumps({"label": "packaging waste (fixture)"}),
                  json.dumps({"pointer": "/documents/0"})])
    return "fixture-link:packaging"


def series_by(conn, provider: str, **match: Any) -> dict[str, Any]:
    """The one series of a provider whose area code, dataset or key parts match."""
    from src.kb.waste_store import WasteStore

    def ok(series):
        checks = {"area": series["area"]["code"], "dataset": series["dataset"],
                  "hazard": series["hazard"]["code"], "operation": series["operation"]["code"],
                  "concept": series["indicator"]["concept"]}
        return all(checks[k] == v for k, v in match.items())

    found = [s for s in WasteStore(conn).find_series(NS, provider=provider) if ok(s)]
    assert len(found) == 1, (provider, match, [s["native_key"] for s in found])
    return found[0]
