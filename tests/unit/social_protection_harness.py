"""Shared offline harness for the Society ``society.social-protection`` tests (#2741): pinned fixtures through the real
adapter.

Every response under ``tests/fixtures/source_packs/society-*`` (first releases) and ``tests/fixtures/social_protection``
(revisions) is authored in the publisher's documented SDMX-CSV shape with synthetic values only: reference years
2094-2098 and release dates in 2098-2099, so nothing can be mistaken for a published figure. Nothing here is live
coverage.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.social_protection_sources import (
    SocialProtectionAdapter,
    fixture_transport,
)
from src.ingestion.source_packs import validate_source_pack
from src.kb.social_protection_store import (
    SocialProtectionProjector,
    SocialProtectionStore,
)

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "config/source_packs/society-social-protection.json"
REVISIONS = ROOT / "tests/fixtures/social_protection"
NS = "global"
READ = "knowledge:social-protection:read"
WRITE = "knowledge:social-protection:write"
REVIEW = "knowledge:social-protection:review"
SCOPES = {
    READ,
    WRITE,
    REVIEW,
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:demographics:read",
    "knowledge:public-finance:read",
    "namespace:global:read",
    "namespace:global:write",
}
READ_ONLY = {READ, "namespace:global:read"}
SOURCES = {"esspros": "eurostat-esspros", "socx": "oecd-socx", "ilo": "ilo-social-protection-coverage"}
FIRST_RETRIEVAL = 4_069_008_000_000  # 2098-12-10
SECOND_RETRIEVAL = 4_084_646_400_000  # 2099-06-09


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads(PACK.read_text()))


def revision(name: str) -> dict[str, Any]:
    return json.loads((REVISIONS / f"{SOURCES[name]}_revision.json").read_text())


def source(name: str, *, revised: bool = False) -> dict[str, Any]:
    item = json.loads(json.dumps(next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[name])))
    if revised:
        item["social_protection"]["documents"] = revision(name)["documents"]
    return item


def pages(name: str, *, revised: bool = False) -> list[dict[str, Any]]:
    if revised:
        return revision(name)["native_pages"]
    return json.loads((ROOT / source(name)["fixture"]["path"]).read_text())["native_pages"]


def fetch(name: str, *, revised: bool = False, transport=None, item=None) -> list[list[dict[str, Any]]]:
    item = item or source(name, revised=revised)
    adapter = SocialProtectionAdapter(item, transport=transport or fixture_transport(pages(name, revised=revised)))
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
    projector = SocialProtectionProjector(conn)
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


def store(conn) -> SocialProtectionStore:
    return SocialProtectionStore(conn)


def series(conn, provider: str, native_key: str) -> dict[str, Any]:
    found = [s for s in SocialProtectionStore(conn).find_series(NS, provider=provider)
             if s["native_key"] == native_key]
    assert len(found) == 1, [s["native_key"] for s in SocialProtectionStore(conn).find_series(NS, provider=provider)]
    return found[0]


PLACES = {
    "de": ("Germany", "country", {"iso3166-1-alpha3": "DEU", "iso3166-1-alpha2": "DE"}),
    "fr": ("France", "country", {"iso3166-1-alpha3": "FRA", "iso3166-1-alpha2": "FR"}),
    "pt": ("Portugal", "country", {"iso3166-1-alpha3": "PRT", "iso3166-1-alpha2": "PT"}),
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


def accept_places(conn, *, keys=("de", "fr")) -> dict[str, str]:
    """Register places, propose matches by published code and accept them as another principal."""
    from src.kb.social_protection_identity import SocialProtectionIdentity

    places = register_places(conn, keys=keys)
    identity = SocialProtectionIdentity(conn)
    for assertion in identity.propose_places(NS, principal_id="proposer", scopes=SCOPES)["proposed"]:
        identity.review(NS, assertion["assertion_id"], "accept", "published ISO code", principal_id="reviewer",
                        scopes=SCOPES)
    return places
