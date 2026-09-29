"""Offline harness for the Legal courts and justice-statistics features (#2218): authored responses, real adapter.

Every file under ``tests/fixtures/courts_justice`` is authored in the provider's
documented shape for fictional dockets, parties, places and figures (years
2096-2099); nothing here is live coverage. Responses go through
:class:`CourtsJusticeAdapter` (the connector the runtime compiles) and
:class:`CourtsJusticeProjector`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb

from src.ingestion.courts_justice_sources import FIXTURE_SECRET, CourtsJusticeAdapter, fixture_transport
from src.ingestion.source_packs import validate_source_pack
from src.kb.courts_justice import CourtsJusticeProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/courts_justice"
PACK = ROOT / "config/source_packs/legal.json"
NS = "global"
DOCKET = "courts:docket:courtlistener:70001"
CLUSTER = "courts:cluster:courtlistener:80001"
PRIOR_CLUSTER = "courts:cluster:courtlistener:80002"
COURT_SOURCES = ("courtlistener-dockets", "courtlistener-opinions")
STAT_SOURCES = ("fbi-cde-summarized", "police-uk-street-crime", "eurostat-crime-iccs")
SOURCES = COURT_SOURCES + STAT_SOURCES
SCOPES = {
    "knowledge:legal:read", "knowledge:legal:write", f"namespace:{NS}:read", f"namespace:{NS}:write",
    "knowledge:ownership:read", "knowledge:ownership:write", "knowledge:geospatial:read",
    "knowledge:geospatial:write", "knowledge:subscriptions:read", "knowledge:subscriptions:write",
}
REVIEW_SCOPES = SCOPES | {"knowledge:legal:review", "knowledge:ownership:review", "knowledge:geospatial:review"}
READ_ONLY = {"knowledge:legal:read", f"namespace:{NS}:read"}
POLY = "51.500,-0.100:51.510,-0.100:51.510,-0.090"
# The later provider revisions: two more docket entries and termination, a re-released FBI vintage with a revised
# month, a revised police.uk month and a new Eurostat release.
V2 = {
    "courtlistener-dockets": {
        "/api/rest/v4/dockets/70001/": "v2/cl_docket_70001.json",
        "/api/rest/v4/docket-entries/?docket=70001&page_size=100": "v2/cl_docket_70001_entries.json"},
    "fbi-cde-summarized": {
        "/crime/fbi/cde/summarized/state/EX/burglary?from=01-2098&to=03-2098": "v2/fbi_state_EX_burglary.json"},
    "police-uk-street-crime": {
        "/api/crime-last-updated": "v2/police_last_updated.json",
        "/api/crimes-street/all-crime?date=2099-01&poly=51.500%2C-0.100%3A51.510%2C-0.100%3A51.510%2C-0.090":
            "v2/police_crimes_2099-01.json"},
    "eurostat-crime-iccs": {
        "/eurostat/api/dissemination/statistics/1.0/data/crim_off_cat?format=JSON&geo=DE&iccs=ICCS0401":
            "v2/eurostat_crim_off_cat_DE.json",
        "/eurostat/api/dissemination/statistics/1.0/data/crim_off_cat?format=JSON&geo=FR&iccs=ICCS0401":
            "v2/eurostat_crim_off_cat_FR.json"},
}


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id: str) -> dict:
    return copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == source_id))


def native_pages(source_id: str, overrides: dict[str, str] | None = None, *, bodies: dict[str, str] | None = None
                 ) -> list[dict]:
    item = source(source_id)
    pages = json.loads((ROOT / item["fixture"]["path"]).read_text())["native_pages"]
    for page in pages:
        if overrides and page["request"] in overrides:
            page["body"] = (FIXTURES / overrides[page["request"]]).read_text()
        if bodies and page["request"] in bodies:
            page["body"] = bodies[page["request"]]
    return pages


def fetch(source_id: str, *, overrides=None, bodies=None, item: dict | None = None,
          secret: str | None = FIXTURE_SECRET) -> list:
    item = item or source(source_id)
    adapter = CourtsJusticeAdapter(item, transport=fixture_transport(native_pages(source_id, overrides, bodies=bodies)),
                                   secret=secret)
    out, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "records", "parameters": {}, "limit": 50}, cursor=cursor)
        out.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, source_id: str, *, run_id: str | None = None, v2: bool = False, overrides=None, bodies=None,
          item: dict | None = None) -> list[dict]:
    item = item or source(source_id)
    overrides = dict(overrides or {})
    if v2:
        overrides.update(V2.get(source_id, {}))
    projector = CourtsJusticeProjector(conn)
    results = []
    for page in fetch(source_id, overrides=overrides, bodies=bodies, item=item):
        results += projector.project_page(run_id=run_id or f"run:{source_id}:{'v2' if v2 else 'v1'}", manifest=None,
                                          source=item, records=page.records, documents=[],
                                          page_receipt=dict(page.receipt), principal_id="operator")
    return results


def load_all(conn, *, v2: bool = False) -> None:
    for source_id in SOURCES:
        apply(conn, source_id, v2=v2)


def seed_us_code(conn, namespace: str = NS) -> str:
    """A Legal work for 42 U.S.C. (title level), as a future US Code provider would project it.

    The Legal pack ships no US Code provider; this row stands in for one so provision-level citation links can be
    exercised offline. It is test data only.
    """
    from src.kb.legal import LegalStore

    LegalStore(conn)
    work_id = "legal-work:fixture-usc-title-42"
    conn.execute("INSERT OR IGNORE INTO legal_works VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 [work_id, namespace, "fixture-us-code", "US", "statute", "normative", "usc:42",
                  json.dumps({"usc_title": "42", "usc_provisions": ["usc:42:1983"]}), "Office of the Law Revision "
                  "Counsel", "Title 42 - The Public Health and Welfare (fixture)", "run:fixture", 1])
    return work_id


def connection():
    return duckdb.connect(":memory:")


class Clock:
    def __init__(self, start: int = 4_090_000_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value


def seed_ownership(conn, namespace: str = NS) -> str:
    """A US ownership legal-entity record named like the docket's plaintiff (test data only)."""
    from src.kb.ownership_records import record
    from src.kb.ownership_store import OwnershipStore

    OwnershipStore(conn).apply(namespace, [record(
        "legal_entity", "sec-edgar:sec-cik:0009990001", {"provider": "sec-edgar", "provider_record_id": "0009990001"},
        name="Example Data Co.", jurisdiction="US", identifiers=[{"scheme": "sec-cik", "value": "0009990001"}])],
        run_id="seed", observed_at_ms=1, principal_id="operator")
    return "sec-edgar:sec-cik:0009990001"


def seed_places(conn, namespace: str = NS) -> dict[str, str]:
    """Geospatial places carrying published codes: Germany and France (NUTS) once, the placeholder state EX twice."""
    from src.kb.geospatial import GeospatialStore

    geo = GeospatialStore(conn)
    scopes = {"knowledge:geospatial:write", "knowledge:geospatial:read"}
    out = {}
    for label, name, ids in (("DE", "Germany", {"nuts": "DE"}), ("EX-a", "Examplestate", {"us-state": "EX"}),
                             ("EX-b", "Examplestate (historic)", {"us-state": "EX", "note": "b"}),
                             ("FR", "France", {"nuts": "FR"})):
        placed = geo.register_place(namespace, name, "admin", names=[{"value": name, "language": "en"}],
                                    source_ids=ids, parent_ids=[], principal_id="operator", scopes=scopes)
        out[label] = placed["place_id"]
    return out
