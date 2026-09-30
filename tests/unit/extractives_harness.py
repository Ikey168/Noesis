"""Shared offline harness for the Economics extractives tests (#2653): pinned fixtures through the real adapter.

Every EITI, USGS and BGS response is authored in the documented shape with fictional values; the ownership side
reuses the Corporate Ownership fixtures (the Exampla group). Offline evidence only, never live coverage.
"""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.extractives_sources import ExtractivesAdapter, fixture_transport
from src.ingestion.source_packs import validate_source_pack
from src.kb.extractives_store import ExtractivesProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/extractives"
NS = "global"
OWN_NS = "ownership"
INFRA_NS = "infra"
SCOPES = {
    "knowledge:extractives:read",
    "knowledge:extractives:write",
    "knowledge:extractives:review",
    "knowledge:ownership:read",
    "knowledge:ownership:write",
    "knowledge:ownership:review",
    "knowledge:infrastructure:read",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "namespace:global:read",
    "namespace:global:write",
    "namespace:ownership:read",
}
READ_ONLY = {"knowledge:extractives:read", "namespace:global:read"}
SOURCES = {
    "eiti": "eiti-summary-data",
    "usgs": "usgs-mineral-commodity-summaries",
    "bgs": "bgs-world-mineral-statistics",
}
# Which declared documents make the first publication round; the rest arrive later.
FIRST = {"eiti": slice(0, 2), "usgs": slice(0, 1), "bgs": slice(0, 2)}
LATER = {"usgs": slice(1, 2), "bgs": slice(2, 3)}
FIRST_RETRIEVAL = 1_748_736_000_000  # 2025-06-01
SECOND_RETRIEVAL = 1_764_547_200_000  # 2025-12-01
HOLD_ENTITY = "gleif:lei:213800EXAMPLAHOLDS95"
INT_ENTITY = "gleif:lei:724500EXAMPLAINTBV75"


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads((ROOT / "config/source_packs/economic.json").read_text()))


def source(name: str, documents: slice | None = None) -> dict[str, Any]:
    item = next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[name])
    if documents is not None:
        item = json.loads(json.dumps(item))
        item["extractives"]["documents"] = item["extractives"]["documents"][documents]
    return item


def pages(name: str, revision: bool = False) -> list[dict[str, Any]]:
    if revision:
        return json.loads((FIXTURES / f"{name}_revision.json").read_text())["native_pages"]
    return json.loads((ROOT / source(name)["fixture"]["path"]).read_text())["native_pages"]


def fetch(item: dict[str, Any], native_pages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    adapter = ExtractivesAdapter(item, transport=fixture_transport(native_pages))
    out, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "release", "parameters": {}, "limit": item["budgets"]["max_results"]},
                                  cursor=cursor)
        out.append([dict(r) for r in page.records])
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, name: str, *, documents: slice | None = None, revision: bool = False,
          retrieved_at_ms: int | None = None) -> list[dict[str, Any]]:
    """Project every page of a source (one release per page), as the runtime would."""
    item = source(name, documents)
    if revision:
        item = source(name, slice(1, 2))
    projector = ExtractivesProjector(conn)
    if retrieved_at_ms is not None:
        projector.store.now = lambda: retrieved_at_ms
    results = []
    for records in fetch(item, pages(name, revision)):
        results += projector.project_page(run_id=f"run:{name}:{'rev' if revision else 'first'}", manifest=None,
                                          source=item, records=records, documents=None, page_receipt=None,
                                          principal_id="svc")
    return results


def load_first(conn) -> None:
    for name, documents in FIRST.items():
        apply(conn, name, documents=documents, retrieved_at_ms=FIRST_RETRIEVAL)


def load_later(conn) -> None:
    for name, documents in LATER.items():
        apply(conn, name, documents=documents, retrieved_at_ms=SECOND_RETRIEVAL)
    apply(conn, "eiti", revision=True, retrieved_at_ms=SECOND_RETRIEVAL)


def load_all(conn) -> None:
    load_first(conn)
    load_later(conn)


def day_ms(day: str) -> int:
    from datetime import date, datetime

    return int(datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=UTC).timestamp() * 1000)


def import_concordances(conn) -> list[dict[str, Any]]:
    from src.kb.extractives_identity import ExtractivesIdentity

    tables = json.loads((FIXTURES / "concordances.json").read_text())["tables"]
    identity = ExtractivesIdentity(conn)
    return [identity.import_concordance(NS, table, principal_id="op", scopes=SCOPES) for table in tables]


def ownership(conn) -> None:
    """The Corporate Ownership fixtures (Exampla group) in the ownership namespace, reviewed."""
    from tests.unit.competition_harness import ownership as load_ownership

    load_ownership(conn)


def review_all(identity, assertions, *, reason: str = "published identifier or cited table checked") -> None:
    for assertion in assertions:
        if assertion["state"] == "proposed":
            identity.review(NS, assertion["assertion_id"], "accept", reason, principal_id="reviewer", scopes=SCOPES)


def reviewed(conn, *, ownership_store: bool = True, infrastructure: bool = True) -> dict[str, Any]:
    """Everything acquired, then reviewed: identifier company candidates accepted and name-only ones rejected,
    commodity, country and project proposals accepted."""
    from src.kb.extractives_identity import ExtractivesIdentity

    load_all(conn)
    if ownership_store:
        ownership(conn)
    import_concordances(conn)
    assets = seed_infrastructure(conn) if infrastructure else {}
    identity = ExtractivesIdentity(conn)
    proposed = identity.propose_companies(NS, ownership_namespace=OWN_NS, principal_id="analyst", scopes=SCOPES)
    for view in proposed["candidates"]:
        ok = view["method"] == "exact-identifier"
        identity.review_company(NS, view["candidate_id"], "accept" if ok else "reject",
                                "published identifier agrees" if ok else "a name alone is not an identity",
                                principal_id="reviewer", scopes=SCOPES)
    review_all(identity, identity.propose_commodities(NS, principal_id="analyst", scopes=SCOPES)["assertions"])
    review_all(identity, identity.propose_countries(NS, principal_id="analyst", scopes=SCOPES)["assertions"])
    projects = identity.propose_projects(NS, infra_namespace=INFRA_NS, principal_id="analyst", scopes=SCOPES)
    review_all(identity, projects["assertions"], reason="published identifier or coordinates checked")
    return {"identity": identity, "assets": assets}


def seed_infrastructure(conn) -> dict[str, str]:
    """Two fictional GEM mine assets: one sharing the project's published id, one at a project's coordinates."""
    from src.kb.infrastructure_assets import InfrastructureStore, asset_id, record

    store = InfrastructureStore(conn)
    common = {"source_url": "https://globalenergymonitor.org/projects/global-mining-tracker/",
              "attribution": "Global Energy Monitor (fixture)",
              "licence": {"id": "cc-by-4.0", "terms_url": "https://creativecommons.org/licenses/by/4.0/"},
              "release": {"key": "fixture-2025-01", "released_at": "2025-01-15", "basis": "declared_release"},
              "retrieved_at": "2025-02-01T00:00:00Z", "country": "PE",
              "geometry_receipt": {"crs_published": "EPSG:4326", "precision_m": 11.132,
                                   "precision_basis": "coordinates published to 4 decimal places"}}
    records = [
        record("gem", "global-mining-tracker-fixture", "M-FIX-1", "mine", name="Cerro Ejemplo mine (fixture)",
               identifiers=[{"scheme": "gem-mine-id", "value": "M-FIX-1"}],
               geometry={"type": "Point", "coordinates": [-70.1234, -15.5678]}, **common),
        record("gem", "global-mining-tracker-fixture", "M-FIX-2", "mine", name="North pit (fixture)",
               identifiers=[{"scheme": "gem-mine-id", "value": "M-FIX-2"}],
               geometry={"type": "Point", "coordinates": [-71.5, -16.25]}, **{
                   **common, "geometry_receipt": {"crs_published": "EPSG:4326", "precision_m": 1113.2,
                                                  "precision_basis": "coordinates published to 2 decimal places"}}),
    ]
    store.apply(INFRA_NS, records, run_id="fixture", principal_id="op",
                scopes={"knowledge:infrastructure:write", "knowledge:infrastructure:read",
                        f"namespace:{INFRA_NS}:write"})
    return {n: asset_id(INFRA_NS, "gem", "global-mining-tracker-fixture", n) for n in ("M-FIX-1", "M-FIX-2")}


def seed_trade(conn, namespace: str = "global") -> str:
    """One Economics trade series of HS 260300 (test data standing in for an acquired Comtrade series)."""
    from src.kb.trade_flows import TradeFlowStore

    TradeFlowStore(conn)
    series_id = "tf-series:fixture-per-chn-260300"
    conn.execute("INSERT INTO trade_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 [namespace, series_id, "un-comtrade", "m49", "604", "{}", "m49", "156", "{}", "X", "export", None,
                  "260300", "Copper ores and concentrates", "HS", "HS2022", None, "annual", "FOB", "reported",
                  "reporter", "{}", "value", "{}", "{}", "tf-release:fixture", 1])
    conn.execute("INSERT INTO trade_vintages VALUES (?,?,?,?,?,?,?,?,?,?)",
                 [namespace, "tf-vintage:fixture-1", series_id, "tf-release:fixture", 1, "declared_release", 1, "x",
                  1, 1])
    return series_id


def seed_energy(conn, namespace: str = "global") -> str:
    """The Energy series the BGS crude-petroleum document names (test data standing in for an acquired series)."""
    from src.kb.energy_store import EnergyStore

    EnergyStore(conn)
    series_id = "energy-series:fixture-per-crude"
    conn.execute("INSERT INTO energy_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 [series_id, namespace, "balance", "eia", "international", "INTL.FIXTURE.PER.CRUDE.A", "country",
                  "iso3166-1-alpha3", "PER", None, "{}", "TBPD", 1])
    conn.execute("INSERT INTO energy_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ["energy-vintage:fixture-per-crude-1", series_id, namespace, 1, "fixture", "declared_release", 1, 1,
                  1, "final", "x", "x", "{}", None, None, 1])
    return series_id


def seed_public_finance(conn, namespace: str = "global") -> str:
    """A budget revenue line whose key the Peru report cites (test data standing in for an acquired line)."""
    from src.kb.public_finance import PublicFinanceStore

    PublicFinanceStore(conn)
    line_id = "pf-line:fixture-pe-royalties"
    conn.execute("INSERT INTO public_finance_lines VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 [namespace, line_id, "fixture", "pe-siaf-ingresos", json.dumps({"code": "1.3.1.1.1"}),
                  json.dumps({"code": "Regalias mineras"}), "revenue", None, "pe-siaf-ingresos:1.3.1.1.1",
                  "pf-release:fixture", 1])
    return line_id
