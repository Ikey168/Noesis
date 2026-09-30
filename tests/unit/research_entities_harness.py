"""Shared offline harness for the Science research-entities tests (#2579): pinned fixtures through the real adapter and
projector, plus fictional Scholarly, Funding and Ownership records in their stores' own shapes."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.research_entities_sources import (
    ResearchEntitiesAdapter,
    fixture_transport,
)
from src.ingestion.source_packs import validate_source_pack
from src.kb.research_entities_records import ResearchEntitiesProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/research_entities"
NS = "global"
OWN_NS = "ownership"
SCOPES = {
    "knowledge:research-entities:read",
    "knowledge:research-entities:write",
    "knowledge:research-entities:review",
    "knowledge:research-entities:researchers",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:ownership:read",
    "knowledge:ownership:write",
    "knowledge:ownership:review",
    "knowledge:funding:read",
    "knowledge:read",
    "namespace:global:read",
    "namespace:global:write",
    "namespace:ownership:read",
    "namespace:ownership:write",
}
READ_ONLY = {"knowledge:research-entities:read", "namespace:global:read"}
SOURCES = {
    "ror": "ror-organisations",
    "orcid": "orcid-public-records",
    "datacite": "datacite-research-datasets",
    "cordis": "cordis-horizon-projects",
}
LATER = {
    "ror": "ror_release_2099-03.json",
    "orcid": "orcid_update_2099-05.json",
    "datacite": "datacite_update_2099-06.json",
    "cordis": "cordis_update_2099-06.json",
}
A1, A2, M1, MISSING = (f"https://ror.org/{i}" for i in ("0re1ab101", "0re1ab202", "0re1ab303", "0re1ab404"))
R1, R2, R3 = "0009-0001-2345-6786", "0009-0002-3456-7892", "0009-0003-4567-8907"
FIRST_RETRIEVAL = 4_102_444_800_000  # 2100-01-01
SECOND_RETRIEVAL = 4_105_123_200_000  # 2100-02-01


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads((ROOT / "config/source_packs/research.json").read_text()))


def later_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / LATER[name]).read_text())


def source(name: str, *, later: bool = False) -> dict[str, Any]:
    item = copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[name]))
    if later:
        item["research_entities"]["documents"] = later_fixture(name)["documents"]
    return item


def pages(name: str, later: bool = False) -> list[dict[str, Any]]:
    if later:
        return later_fixture(name)["native_pages"]
    return json.loads((ROOT / source(name)["fixture"]["path"]).read_text())["native_pages"]


def fetch(name: str, *, later: bool = False, transport=None) -> list[list[dict[str, Any]]]:
    item = source(name, later=later)
    adapter = ResearchEntitiesAdapter(item, transport=transport or fixture_transport(pages(name, later)))
    out, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "registry-records", "parameters": {},
                                   "limit": item["budgets"]["max_results"]}, cursor=cursor)
        out.append([dict(r) for r in page.records])
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, name: str, *, later: bool = False, retrieved_at_ms: int | None = None) -> list[dict[str, Any]]:
    """Project every page of a source into the store (one release per page), as the runtime would."""
    item = source(name, later=later)
    projector = ResearchEntitiesProjector(conn)
    if retrieved_at_ms is not None:
        projector.store.now = lambda: retrieved_at_ms
    results = []
    for records in fetch(name, later=later):
        results += projector.project_page(run_id=f"run:{name}:{'later' if later else 'first'}", manifest=None,
                                          source=item, records=records, documents=None, page_receipt=None,
                                          principal_id="svc")
    return results


def load_all(conn, *, later: bool = False) -> None:
    for name in SOURCES:
        apply(conn, name, retrieved_at_ms=FIRST_RETRIEVAL)
    if later:
        for name in SOURCES:
            apply(conn, name, later=True, retrieved_at_ms=SECOND_RETRIEVAL)


def seed_science_and_funding(conn) -> dict[str, str]:
    """Scholarly works stating the fixture DOIs and a Funding award naming the CORDIS project (fictional records in
    the stores' own shapes); paper3 is deliberately absent."""
    from src.ingestion.document_store import _SCHEMA
    from src.kb.funding_opportunities import FundingOpportunityStore

    conn.execute(_SCHEMA)
    for number, title in ((1, "A fictional study of examples"), (2, "Another fictional study")):
        conn.execute(
            "INSERT INTO documents (document_id, source_type, title, url, content_hash, metadata) VALUES (?,?,?,?,?,?)",
            [f"doc:rent-paper-{number}", "paper", title, f"https://doi.org/10.9999/rent.paper{number}",
             f"sha-rent-paper-{number}", json.dumps({"doi": f"10.9999/rent.paper{number}"})])
    FundingOpportunityStore(conn)
    content = {"contract": "noesis-funding-record-v1", "record_kind": "award", "provider": "eu-ft",
               "provider_id": "101999001", "title": "EXAMPLIA grant agreement 101999001 (fictional)",
               "financial_terms": {"amount": "2400000", "currency": "EUR", "basis": "per-project"}}
    conn.execute("INSERT INTO funding_opportunities VALUES (?,?,?,?,?,?,?,?,?)",
                 ["fund:award:101999001", "global", "eu-ft", "award", "101999001", None, 1, 1, "listed"])
    conn.execute("INSERT INTO funding_opportunity_revisions VALUES (?,?,?,?)",
                 ["fund:award:101999001", 1, json.dumps(content), 1])
    return {"paper1": "doc:rent-paper-1", "paper2": "doc:rent-paper-2", "award": "fund:award:101999001"}


def seed_ownership(conn) -> dict[str, str]:
    """Ownership legal entities: one carrying the university's ISNI, one the participant's VAT number and a GLEIF
    record whose name only equals a ROR name (fictional records through the ownership store)."""
    from src.kb.ownership_records import CONTRACT
    from src.kb.ownership_store import OwnershipStore

    def entity(key, provider, name, identifiers):
        return {"contract": CONTRACT, "kind": "legal_entity", "record_key": key, "name": name,
                "jurisdiction": "DE", "identifiers": identifiers,
                "source": {"provider": provider, "provider_record_id": key.split(":", 1)[1],
                           "url": "https://example.invalid/" + key.split(":", 1)[1]}}

    records = [
        entity("open-ownership:rent-uni", "open-ownership", "Universitaet Beispielstadt",
               [{"scheme": "isni", "value": "0000000400000101"}]),
        entity("open-ownership:rent-exampla", "open-ownership", "Exampla Holding GmbH",
               [{"scheme": "eu-vat", "value": "DE 123456789"}]),
        entity("gleif:RENT00000000000000001", "gleif", "Fictional Polytechnic Beispielstadt",
               [{"scheme": "lei", "value": "RENT00000000000000001"}]),
    ]
    OwnershipStore(conn).apply(OWN_NS, records, run_id="own-fixture", observed_at_ms=FIRST_RETRIEVAL,
                               principal_id="svc")
    return {r["name"]: r["record_key"] for r in records}
