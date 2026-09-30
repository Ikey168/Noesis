"""Shared offline harness for the Economics extractives tests (#2653): pinned fixtures through the real adapter.

Every file under ``tests/fixtures/extractives`` and ``tests/fixtures/source_packs/economic-extractives-*`` is
authored in the documented shape as known to the author for fictional companies, projects and figures (the Exampla
and Northwind groups of the ownership fixtures); nothing here is live coverage. Responses go through
:class:`ExtractivesAdapter` (the connector the runtime compiles) and :class:`ExtractivesProjector`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.extractives_sources import ExtractivesAdapter, fixture_transport
from src.ingestion.source_packs import validate_source_pack
from src.kb.extractives_store import ExtractivesProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/extractives"
PACK_PATH = ROOT / "config/source_packs/economic-extractives.json"
NS = "global"
OWN_NS = "ownership"
SCOPES = {
    "knowledge:extractives:read", "knowledge:extractives:write", "knowledge:extractives:review",
    "knowledge:ownership:read", "knowledge:trade:read", "knowledge:energy:read", "knowledge:economic:public-finance:read",
    "knowledge:infrastructure:read", "knowledge:subscriptions:read", "knowledge:subscriptions:write",
    "namespace:global:read", "namespace:global:write", f"namespace:{OWN_NS}:read", "namespace:energy:read",
    "namespace:infra:read",
}
SCOPES |= {"knowledge:ownership:write"}
REVIEW_SCOPES = SCOPES | {"knowledge:ownership:review"}
READ_ONLY = {"knowledge:extractives:read", "namespace:global:read"}
SOURCES = {"eiti": "eiti-summary-data", "usgs": "usgs-mineral-commodity-summaries",
           "bgs": "bgs-world-mineral-statistics"}
REVISIONS = {"eiti": "eiti_nl_2021_version_2.json", "usgs": "usgs_mcs2025_copper.json",
             "bgs": "bgs_wms_2019-2023_copper.json"}
FIRST_RETRIEVAL = 1_733_011_200_000  # 2024-12-01
SECOND_RETRIEVAL = 1_743_465_600_000  # 2025-04-01
NL_REPORT = "extractives:eiti:report:NL:2021-01-01_2021-12-31"
DE_REPORT = "extractives:eiti:report:DE:2021-01-01_2021-12-31"
INT_COMPANY = "extractives:eiti:company:NL:2021-01-01_2021-12-31:nl-kvk:99990003"
NORTHWIND = "extractives:eiti:company:NL:2021-01-01_2021-12-31:nl-kvk:99990077"
HOLD_COMPANY = "extractives:eiti:company:DE:2021-01-01_2021-12-31:gb-coh:09990001"
UK_COMPANY = "extractives:eiti:company:DE:2021-01-01_2021-12-31:name:exampla-uk-limited"
CIT_PAYMENT = "extractives:eiti:payment:NL:2021-01-01_2021-12-31:nl-kvk:99990003:1112e1:corporate-income-tax:p1"
NW_PAYMENT = "extractives:eiti:payment:NL:2021-01-01_2021-12-31:nl-kvk:99990077:1112e1:corporate-income-tax"
HOLD_ENTITY = "gleif:lei:213800EXAMPLAHOLDS95"
INT_ENTITY = "gleif:lei:724500EXAMPLAINTBV75"
# A published commodity-to-HS correspondence (authored in the shape of the HS headings BGS states for its trade
# statistics; the table must be verified against the BGS methodology before live use).
CONCORDANCE = {
    "label": "BGS World Mineral Statistics: HS headings used for trade statistics (authored fixture; verify)",
    "publisher": "British Geological Survey",
    "citation": {"url": "https://www.bgs.ac.uk/mineralsuk/statistics/world-mineral-statistics/",
                 "published_on": "2024-03-15", "locator": "trade statistics methodology, commodity table"},
    "rows": [
        {"commodity": "copper", "hs_code": "2603", "hs_edition": "HS2022", "label": "Copper ores and concentrates",
         "mapping_type": "1:n", "note": "ores and concentrates heading; metal content statistics are not HS goods"},
        {"commodity": "crude-petroleum", "hs_code": "2709", "hs_edition": "HS2022",
         "label": "Petroleum oils and oils obtained from bituminous minerals, crude", "mapping_type": "1:1"},
    ],
}


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads(PACK_PATH.read_text()))


def source(name: str, *, revision: bool = False) -> dict[str, Any]:
    item = copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[name]))
    if revision:
        item["extractives"]["documents"] = [json.loads((FIXTURES / REVISIONS[name]).read_text())["document"]]
    return item


def pages(name: str, revision: bool = False) -> list[dict[str, Any]]:
    if revision:
        return json.loads((FIXTURES / REVISIONS[name]).read_text())["native_pages"]
    return json.loads((ROOT / source(name)["fixture"]["path"]).read_text())["native_pages"]


def fetch(name: str, *, revision: bool = False, transport=None) -> list[list[dict[str, Any]]]:
    item = source(name, revision=revision)
    adapter = ExtractivesAdapter(item, transport=transport or fixture_transport(pages(name, revision)))
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
    projector = ExtractivesProjector(conn)
    if retrieved_at_ms is not None:
        projector.store.now = lambda: retrieved_at_ms
    results = []
    for records in fetch(name, revision=revision):
        results += projector.project_page(run_id=f"run:{name}:{'rev' if revision else 'first'}", manifest=None,
                                          source=item, records=records, documents=None, page_receipt=None,
                                          principal_id="svc")
    return results


def load_all(conn, *, revisions: bool = False) -> None:
    for name in ("eiti", "usgs", "bgs"):
        apply(conn, name, retrieved_at_ms=FIRST_RETRIEVAL)
    if revisions:
        for name in ("eiti", "usgs", "bgs"):
            apply(conn, name, revision=True, retrieved_at_ms=SECOND_RETRIEVAL)


def day_ms(day: str) -> int:
    from src.kb.extractives_records import day_ms as to_ms

    return to_ms(day)


def ownership(conn) -> None:
    """The Corporate Ownership fixtures (Exampla group) in the ownership namespace, reviewed (competition harness)."""
    from tests.unit import competition_harness as ch

    ch.ownership(conn)


def seed_trade(conn) -> dict[str, str]:
    """Trade series for HS 2603 (Comtrade, Germany) and CN 26030000 (Comext, Germany), authored from the trade
    fixtures' shapes with the product changed; fictional values, applied through the trade store."""
    from src.kb.trade_flows import TradeFlowStore
    from tests.unit import trade_harness as th

    store = TradeFlowStore(conn)
    out = {}
    for name, product, label in (("comtrade", "260300", "Copper ores and concentrates (fixture label)"),
                                 ("comext", "26030000", "Copper ores and concentrates (fixture label)")):
        records = th.fetch(name)[0]
        item = copy.deepcopy(next(r["trade_item"] for r in records
                                  if r["trade_item"]["reporter"]["code"] in ("276", "DE")))
        item["product"] = {"code": product, "label": label}
        header = copy.deepcopy(records[0]["trade_release"])
        header.update({"item_count": 1, "file_sha256": "f" * 63 + ("1" if name == "comtrade" else "2"),
                       "content_sha256": "e" * 64, "document": {"label": f"authored {name} copper ores fixture"}})
        result = store.apply_release(NS, header, [item], run_id=f"fixture:{name}:copper", source_id=f"fixture-{name}",
                                     retrieved_at_ms=FIRST_RETRIEVAL)
        (series_id,) = [r[0] for r in conn.execute(
            "SELECT series_id FROM trade_release_members WHERE namespace=? AND release_id=?",
            [NS, result["release_id"]]).fetchall()]
        out[name] = series_id
    return out


def seed_infrastructure(conn) -> str:
    """One GEM mine asset carrying the published identifier the D-EITI project states (authored, fictional)."""
    from src.kb.infrastructure_assets import InfrastructureStore, record

    value = record("gem", "gem:global-mine-tracker", "M9001", "mine", name="Exampla Kupfer Mine (fixture)",
                   source_url="https://globalenergymonitor.org/projects/global-mine-tracker/",
                   attribution="Global Energy Monitor (fixture)",
                   licence={"id": "cc-by-4.0", "terms_url": "https://creativecommons.org/licenses/by/4.0/"},
                   release={"key": "gmt-2024-06", "released_at": "2024-06-30", "basis": "declared_release"},
                   retrieved_at="2024-12-01T00:00:00Z", country="DE",
                   identifiers=[{"scheme": "gem-mine-id", "value": "M9001"}])
    InfrastructureStore(conn).apply("infra", [value], run_id="fixture:infra", principal_id="svc",
                                    scopes={"operator"})
    return conn.execute("SELECT asset_id FROM infra_assets WHERE namespace='infra'").fetchone()[0]


def reviewed(conn, *, revisions: bool = False) -> dict[str, Any]:
    """Ownership reviewed, extractives loaded, company candidates reviewed against the Exampla group.

    A reviewer accepts the exact-identifier candidates (published KvK and Companies House numbers), rejects the
    name-only candidate pointing at the same-name decoy and leaves the other name-only candidates pending, so
    Exampla UK Limited (no identifier in the report) and Northwind Offshore stay unmatched.
    """
    from src.kb.extractives_identity import ExtractivesIdentity

    ownership(conn)
    load_all(conn, revisions=revisions)
    identity = ExtractivesIdentity(conn)
    proposed = identity.propose_companies(NS, ownership_namespace=OWN_NS, principal_id="analyst", scopes=SCOPES)
    for view in proposed["candidates"]:
        if view["method"] == "exact-identifier":
            identity.review_company(NS, view["candidate_id"], "accept", "published register number agrees",
                                    principal_id="reviewer", scopes=REVIEW_SCOPES)
        elif "decoy" in view["ownership_key"]:
            identity.review_company(NS, view["candidate_id"], "reject", "a different register entity",
                                    principal_id="reviewer", scopes=REVIEW_SCOPES)
    return {"identity": identity, "proposed": proposed}


class Clock:
    def __init__(self, start: int = 1_760_000_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value
