"""Offline harness for the Legal treaties provider (#2581): authored responses, real adapter.

Every file under ``tests/fixtures/treaties`` is authored in the provider's
documented page or result shape for fictional treaties (UNTC ``XXIX-99``, CETS
No. 990, CELEX ``22099A0101(01)``; 2098-2100); nothing here is live coverage.
Responses go through :class:`TreatiesAdapter` (the connector the runtime
compiles) and :class:`TreatiesProjector`. The UN Treaty Collection entry is
declined in the pack; :func:`permitted_untc` is a *test-only* copy with an
accepted licence decision so the UNTC parser is exercised offline.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb

from src.ingestion.source_packs import validate_source_pack
from src.ingestion.treaties_sources import TreatiesAdapter, fixture_transport
from src.kb.treaties_store import TreatiesProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/treaties"
PACK = ROOT / "config/source_packs/legal.json"
NS = "global"
UNTC = "untc-multilateral-status"
CELLAR = "cellar-eu-international-agreements"
COE = "coe-treaty-office-charts"
SOURCES = (CELLAR, COE)
UNTC_TREATY = "treaties:treaty:untc:XXIX-99"
COE_TREATY = "treaties:treaty:coe:990"
CELLAR_TREATY = "treaties:treaty:cellar:22099A0101(01)"
UNTC_REQUEST = "/Pages/ViewDetails.aspx?chapter=29&clang=_en&mtdsg_no=XXIX-99"
SCOPES = {
    "knowledge:legal:read", "knowledge:legal:write", f"namespace:{NS}:read", f"namespace:{NS}:write",
    "knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
}
REVIEW_SCOPES = SCOPES | {"knowledge:legal:review", "knowledge:geospatial:review"}
READ_ONLY = {"knowledge:legal:read", f"namespace:{NS}:read"}
# The later depositary revisions: a corrected ratification date, a signature no longer listed and a reservation
# withdrawal (UNTC); a denunciation and a withdrawal of a reservation (Council of Europe); a new linked act (CELLAR).
V2 = {
    UNTC: {UNTC_REQUEST: "v2/untc_XXIX-99.html"},
    COE: {"/en/web/conventions/full-list?module=signatures-by-treaty&treatynum=990": "v2/coe_990_chart.html",
          "/en/web/conventions/full-list?module=declarations-by-treaty&treatynum=990": "v2/coe_990_declarations.html"},
    CELLAR: {"/webapi/rdf/sparql#agreement": "v2/cellar_agreement.json"},
}


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id: str) -> dict:
    return copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == source_id))


def permitted_untc() -> dict:
    """Test-only: the UNTC source as an operator holding written permission would record it."""
    item = source(UNTC)
    item["treaties"]["licence_decision"] = {"status": "accepted", "recorded": "2099-01-01",
                                            "reference": "test-only: written permission on file (fixture)"}
    item["treaties"]["live_verification"] = "unverified-live"
    return item


def native_pages(source_id: str, *, v2: bool = False) -> list[dict]:
    if source_id == UNTC:
        pages = [{"request": UNTC_REQUEST, "body": (FIXTURES / "untc_XXIX-99.html").read_text(), "status": 200}]
    else:
        pages = json.loads((ROOT / source(source_id)["fixture"]["path"]).read_text())["native_pages"]
    for page in pages:
        if v2 and page["request"] in V2.get(source_id, {}):
            page["body"] = (FIXTURES / V2[source_id][page["request"]]).read_text()
    return pages


def item_for(source_id: str) -> dict:
    return permitted_untc() if source_id == UNTC else source(source_id)


def fetch(source_id: str, *, v2: bool = False, item: dict | None = None) -> list:
    item = item or item_for(source_id)
    adapter = TreatiesAdapter(item, transport=fixture_transport(native_pages(source_id, v2=v2)))
    out, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "records", "parameters": {}}, cursor=cursor)
        out.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return out


def records(source_id: str, *, v2: bool = False) -> list[dict]:
    return [item["treaty_record"] for page in fetch(source_id, v2=v2) for item in page.records]


def apply(conn, source_id: str, *, v2: bool = False, run_id: str | None = None) -> list[dict]:
    item = item_for(source_id)
    projector = TreatiesProjector(conn)
    results = []
    for page in fetch(source_id, v2=v2, item=item):
        results += projector.project_page(run_id=run_id or f"run:{source_id}:{'v2' if v2 else 'v1'}", manifest=None,
                                          source=item, records=page.records, documents=[],
                                          page_receipt=dict(page.receipt), principal_id="operator")
    return results


def load_all(conn, *, v2: bool = False, untc: bool = True) -> None:
    for source_id in (UNTC, *SOURCES) if untc else SOURCES:
        apply(conn, source_id, v2=v2)


def connection():
    return duckdb.connect(":memory:")


class Clock:
    def __init__(self, start: int = 4_102_444_800_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value


def seed_places(conn, namespace: str = NS) -> dict[str, str]:
    """Geospatial places carrying ISO 3166-1 codes (Germany, France) and one without a code (test data only)."""
    from src.kb.geospatial import GeospatialStore

    geo = GeospatialStore(conn)
    scopes = {"knowledge:geospatial:write", "knowledge:geospatial:read"}
    out = {}
    for label, name, ids in (("DE", "Germany", {"iso3166-1-alpha2": "DE", "iso3166-1-alpha3": "DEU"}),
                             ("FR", "France", {"iso3166-1-alpha2": "FR", "iso3166-1-alpha3": "FRA"}),
                             ("EXL", "Examplestan", {"note": "fixture place without a published code"})):
        placed = geo.register_place(namespace, name, "country", names=[{"value": name, "language": "en"}],
                                    source_ids=ids, parent_ids=[], principal_id="operator", scopes=scopes)
        out[label] = placed["place_id"]
    return out


def seed_legal_act(conn, namespace: str = NS) -> str:
    """A Legal work for the concluding Council decision 32099D0042, as the Legal CELLAR provider would project it.

    The fixture pack acquires no such act through ``legal.core``; this row stands in for one so the ``eu-act``
    citation link can be exercised offline. Test data only.
    """
    from src.kb.legal import LegalStore

    LegalStore(conn)
    work_id = "legal-work:fixture-32099d0042"
    conn.execute("INSERT OR IGNORE INTO legal_works VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 [work_id, namespace, "cellar", "EU", "legislation", "normative", "32099D0042",
                  json.dumps({"celex": "32099D0042"}), "Council of the European Union",
                  "Council Decision (EU) 2099/42 (fixture)", "run:fixture", 1])
    return work_id


def seed_sanctions_bases(conn, namespace: str = NS) -> None:
    """Two list legal-basis citations: one citing CETS No. 990, one citing a treaty with no record (test data)."""
    from src.kb.sanctions import SanctionsStore

    SanctionsStore(conn)
    for basis_id, citation in (("sanctions-basis:fixture-990", "Council Decision 2099/7 implementing CETS No. 990"),
                               ("sanctions-basis:fixture-991", "Framework measures under CETS No. 991")):
        conn.execute("INSERT OR IGNORE INTO sanctions_legal_bases VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     [namespace, basis_id, "eu-fsf", citation, None, None, None, "unresolved", None, None, "{}",
                      "sanctions-snapshot:fixture-1"])


def seed_trade_area(conn, place_id: str, namespace: str = NS) -> str:
    """An accepted Trade flows area assertion mapping Eurostat GEO DE to the Germany place (test data only)."""
    from src.kb.trade_identity import TradeIdentity

    TradeIdentity(conn)
    assertion_id = "tf-identity:fixture-de"
    conn.execute("INSERT OR IGNORE INTO trade_identity_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 [namespace, assertion_id, "area", json.dumps({"scheme": "eurostat-geo", "code": "DE"}),
                  json.dumps({"scheme": "eurostat-geo", "code": "DE"}), json.dumps({"place_id": place_id}),
                  "published-code", "{}", "accepted", None, "[]", "operator", 1])
    return assertion_id
