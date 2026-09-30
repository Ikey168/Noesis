"""Offline harness for the Legal treaties provider (#2581): authored responses through the real adapter and store.

Every file under ``tests/fixtures/treaties`` is authored in the element
structure and SPARQL shape the TR01 audit records, for fictional treaties,
states (Exampland, Northwind Republic, Southland, Oldland; user-assigned codes
XEA/XNW), acts and dates (2090-2099); nothing here is live coverage. Responses
go through :class:`TreatiesAdapter` (the connector the runtime compiles) and
:class:`TreatiesProjector`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb

from src.ingestion.source_packs import validate_source_pack
from src.ingestion.treaties_sources import (
    TreatiesAdapter,
    fixture_transport,
    request_key,
    requests_for,
)
from src.kb.treaties_records import TreatiesProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/treaties"
PACK = ROOT / "config/source_packs/legal.json"
NS = "global"
UNTC = "treaties:untc:treaty:XXVII-99"
EU = "treaties:eu-cellar:treaty:22090A0510(01)"
COE = "treaties:coe:treaty:999"
SOURCES = ("untc-treaty-status", "eu-cellar-agreements", "coe-treaty-office")
SCOPES = {
    "knowledge:legal:read", "knowledge:legal:write", f"namespace:{NS}:read", f"namespace:{NS}:write",
    "knowledge:ownership:read", "knowledge:ownership:write", "knowledge:geospatial:read",
    "knowledge:geospatial:write", "knowledge:subscriptions:read", "knowledge:subscriptions:write",
}
REVIEW_SCOPES = SCOPES | {"knowledge:legal:review", "knowledge:ownership:review"}
READ_ONLY = {"knowledge:legal:read", f"namespace:{NS}:read"}
# The later depositary status: Southland's ratification, a corrected accession date, a new declaration and note,
# the Oldland row no longer shown (UNTC); an entry-into-force date now stated (CELLAR); Southland's ratification
# and entry into force (Council of Europe).
V2 = {"untc-treaty-status": {"status": "v2/untc_XXVII-99.html"},
      "eu-cellar-agreements": {"agreement": "v2/cellar_agreement_22090A0510-01.json"},
      "coe-treaty-office": {"chart": "v2/coe_999_chart.html"}}


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id: str) -> dict:
    return copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == source_id))


def request_keys(item: dict) -> dict[str, str]:
    declared = item["treaties"]
    unit = declared["selection"][next(iter(declared["selection"]))][0]
    base = "/".join(item["endpoint"].split("/", 3)[:3])
    return {name: request_key(base + path, params)
            for name, (path, params) in requests_for(declared["format"], unit).items()}


def native_pages(source_id: str, *, v2: bool = False, bodies: dict[str, str] | None = None) -> list[dict]:
    item = source(source_id)
    pages = json.loads((ROOT / item["fixture"]["path"]).read_text())["native_pages"]
    keys = {key: name for name, key in request_keys(item).items()}
    for page in pages:
        name = keys.get(page["request"])
        if v2 and name in V2.get(source_id, {}):
            page["body"] = (FIXTURES / V2[source_id][name]).read_text()
        if bodies and name in bodies:
            page["body"] = bodies[name]
    return pages


def fetch(source_id: str, *, v2: bool = False, bodies=None, item: dict | None = None) -> list:
    item = item or source(source_id)
    adapter = TreatiesAdapter(item, transport=fixture_transport(native_pages(source_id, v2=v2, bodies=bodies)))
    out, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "records", "parameters": {}, "limit": 500}, cursor=cursor)
        out.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return out


def records(source_id: str, **kwargs) -> list[dict]:
    return [item["treaty_record"] for page in fetch(source_id, **kwargs) for item in page.records]


def apply(conn, source_id: str, *, run_id: str | None = None, v2: bool = False, bodies=None,
          observed_at_ms: int | None = None) -> list[dict]:
    item = source(source_id)
    projector = TreatiesProjector(conn)
    results = []
    for page in fetch(source_id, v2=v2, bodies=bodies, item=item):
        documents = [{"ingested_at": observed_at_ms}] if observed_at_ms is not None else []
        results += projector.project_page(run_id=run_id or f"run:{source_id}:{'v2' if v2 else 'v1'}", manifest=None,
                                          source=item, records=page.records, documents=documents,
                                          page_receipt=dict(page.receipt), principal_id="operator")
    return results


def load_all(conn, *, v2: bool = False, observed_at_ms: int | None = None) -> None:
    for source_id in SOURCES:
        apply(conn, source_id, v2=v2, observed_at_ms=observed_at_ms)


def connection():
    return duckdb.connect(":memory:")


class Clock:
    def __init__(self, start: int = 4_090_000_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value


def seed_places(conn, namespace: str = NS) -> dict[str, str]:
    """Geospatial places carrying published ISO 3166 codes (user-assigned XE/XEA, XN/XNW) and a name-only place."""
    from src.kb.geospatial import GeospatialStore

    geo = GeospatialStore(conn)
    scopes = {"knowledge:geospatial:write", "knowledge:geospatial:read"}
    out = {}
    for label, name, ids in (("XEA", "Exampland", {"iso3166-1-alpha2": "XE", "iso3166-1-alpha3": "XEA"}),
                             ("XNW", "Northwind Republic", {"iso3166-1-alpha2": "XN", "iso3166-1-alpha3": "XNW"}),
                             ("SOUTH", "Southland", {"fixture": "southland"})):
        placed = geo.register_place(namespace, name, "country", names=[{"value": name, "language": "en"}],
                                    source_ids=ids, parent_ids=[], principal_id="operator", scopes=scopes)
        out[label] = placed["place_id"]
    return out


def seed_legal_work(conn, celex: str = "32093D0202", namespace: str = NS) -> str:
    """A Legal work for one EU act the agreement's CELLAR record cites (as the legal.core CELLAR projection makes).

    Test data only: the Council decision is fictional.
    """
    from src.kb.legal import LegalStore

    LegalStore(conn)
    work_id = f"legal-work:fixture-{celex}"
    conn.execute("INSERT OR IGNORE INTO legal_works VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 [work_id, namespace, "cellar", "EU", "decision", "normative", celex,
                  json.dumps({"celex": celex}), "Council of the European Union",
                  "Council Decision concluding the Convention on Example Data Cooperation (fixture)", "run:fixture", 1])
    return work_id


def seed_sanctions_basis(conn, citation: str, *, celex: str | None = None, namespace: str = NS) -> str:
    """A sanctions legal-basis row whose citation names a treaty by an exact identifier (test data only)."""
    from src.kb.sanctions import SanctionsStore

    SanctionsStore(conn)
    basis_id = "sanctions-basis:fixture-" + (celex or "untc")
    conn.execute("INSERT OR IGNORE INTO sanctions_legal_bases VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 [namespace, basis_id, "eu-fsf", citation, celex, None, None, "unresolved", None, None, "{}",
                  "snapshot:fixture"])
    return basis_id


def seed_trade_reporter(conn, iso3: str = "XEA", namespace: str = NS) -> str:
    """A trade series whose Comtrade reporter states the ISO alpha-3 code (test data only)."""
    from src.kb.trade_flows import TradeFlowStore

    TradeFlowStore(conn)
    series_id = f"trade-series:fixture-{iso3}"
    reporter = json.dumps({"scheme": "m49", "code": "999", "label": "Exampland (fixture)", "iso3": iso3})
    partner = json.dumps({"scheme": "m49", "code": "0", "label": "World"})
    conn.execute("INSERT OR IGNORE INTO trade_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 [namespace, series_id, "comtrade", "m49", "999", reporter, "m49", "0", partner, "X", "export",
                  "Export", "TOTAL", "All commodities", "HS", "H6", "TOTAL", "A", "FOB", "declared", "reporter",
                  "{}", "value", "{}", "{}", "trade-release:fixture", 1])
    return series_id
