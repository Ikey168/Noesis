"""Offline harness for the Corporate Ownership competition feature (#2217): authored responses, real adapter.

Every file under ``tests/fixtures/competition`` is authored in the publisher's
documented or observed shape for fictional cases, awards, parties and amounts
(the Exampla and Northwind groups of the ownership fixtures); nothing here is
live coverage. Responses go through :class:`CompetitionAdapter` (the connector
the runtime compiles) and :class:`CompetitionProjector`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb

from src.ingestion.competition_sources import CompetitionAdapter, fixture_transport
from src.ingestion.source_packs import validate_source_pack
from src.kb.competition import CompetitionProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/competition"
PACK = ROOT / "config/source_packs/corporate-ownership.json"
NS = "competition"
OWN_NS = "ownership"
SOURCES = ("ec-competition-cases", "eu-state-aid-tam", "uk-cma-cases", "us-ftc-cases", "us-doj-atr-cases")
MERGER = "competition:case:ec:M.99001"
AID_CASE = "competition:case:ec:SA.99002"
CMA = "competition:case:uk-cma:exampla-northwind-merger-inquiry"
FTC = "competition:case:us-ftc:2510001"
DOJ = "competition:case:us-doj:us-v-northwind-widgets"
AWARD_INT = "competition:award:eu-tam:NL:NL-2025-000101"
AWARD_NW = "competition:award:eu-tam:NL:NL-2025-000102"
AWARD_OTHER = "competition:award:eu-tam:NL:NL-2025-000301"
SCOPES = {
    "knowledge:ownership:read", "knowledge:ownership:write", f"namespace:{NS}:read", f"namespace:{NS}:write",
    f"namespace:{OWN_NS}:read", f"namespace:{OWN_NS}:write", "knowledge:legal:read", "namespace:global:read",
    "knowledge:subscriptions:read", "knowledge:subscriptions:write", "knowledge:ingestion:execute",
}
REVIEW_SCOPES = SCOPES | {"knowledge:ownership:review"}
READ_ONLY = {"knowledge:ownership:read", f"namespace:{NS}:read"}
# The later publisher revisions: the merger's final decision and appeal, a corrected and a withdrawn award, and the
# CMA case's acceptance of undertakings.
V2 = {
    "ec-competition-cases": {"/api/cases/M.99001": "v2/ec_case_M.99001.json"},
    "eu-state-aid-tam": {
        "/competition/transparency/public/api/awards?countryCode=NL&page=0&saNumber=SA.99002&size=100":
            "v2/tam_NL_SA.99002.json"},
    "uk-cma-cases": {"/api/content/cma-cases/exampla-northwind-merger-inquiry":
                     "v2/cma_exampla-northwind-merger-inquiry.json"},
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


def fetch(source_id: str, *, overrides=None, bodies=None, item: dict | None = None) -> list:
    item = item or source(source_id)
    adapter = CompetitionAdapter(item, transport=fixture_transport(native_pages(source_id, overrides, bodies=bodies)))
    out, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "competition", "parameters": {}, "limit": 100}, cursor=cursor)
        out.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, source_id: str, *, run_id: str | None = None, v2: bool = False, overrides=None, bodies=None,
          item: dict | None = None, now=None) -> list[dict]:
    item = item or source(source_id)
    overrides = dict(overrides or {})
    if v2:
        overrides.update(V2.get(source_id, {}))
    projector = CompetitionProjector(conn)
    if now is not None:
        projector.store.now = now
        projector.store.store.now = now
    results = []
    for page in fetch(source_id, overrides=overrides, bodies=bodies, item=item):
        results += projector.project_page(run_id=run_id or f"run:{source_id}:{'v2' if v2 else 'v1'}", manifest=None,
                                          source=item, records=page.records, documents=[],
                                          page_receipt=dict(page.receipt), principal_id="operator")
    return results


def load_all(conn, *, v2: bool = False, now=None) -> None:
    for source_id in SOURCES:
        apply(conn, source_id, v2=v2, now=now)


def connection():
    return duckdb.connect(":memory:")


def ownership(conn, *, review: bool = True):
    """The Corporate Ownership fixtures (Exampla group) in the ownership namespace, optionally reviewed."""
    from src.ingestion.ownership_providers import FIXTURE_SECRET
    from src.ingestion.source_pack_runtime import SourcePackRuntime
    from src.kb import ownership_bundle
    from src.kb.ownership_identity import OwnershipIdentityService
    from tests.unit.ownership import harness as own

    ownership_bundle.install_source_pack(conn, principal_id="operator", scopes={"operator"}, accept_terms=True)
    adapters = SourcePackRuntime(conn).fixture_adapters(ownership_bundle.SOURCE_PACK_ID, ROOT)
    ownership_bundle.acquire(conn, OWN_NS, run_key="ownership", principal_id=own.PRINCIPAL, scopes=own.SCOPES,
                             adapters=adapters, secret_resolver=lambda _ref: FIXTURE_SECRET,
                             dns_resolver=lambda _host: ["8.8.8.8"])
    if review:
        service = OwnershipIdentityService(conn)
        for item in service.propose(OWN_NS, principal_id=own.PRINCIPAL, scopes=own.SCOPES)["candidates"]:
            decoy = own.DECOY in (item["left_key"], item["right_key"])
            service.review(OWN_NS, item["candidate_id"], "reject" if decoy else "accept",
                           "decoy: no register number" if decoy else "identifiers agree",
                           principal_id=own.REVIEWER, scopes=own.REVIEW_SCOPES)


def seed_legal(conn, namespace: str = "global") -> dict[str, str]:
    """Legal works for the EU Merger Regulation, GBER and TFEU Article 107, as the CELLAR provider would project them.

    Test data only: the rows stand in for acquired CELLAR works so exact CELEX links can be exercised offline.
    """
    from src.kb.legal import LegalStore

    LegalStore(conn)
    works = {"32004R0139": "Council Regulation (EC) No 139/2004 (fixture)",
             "32014R0651": "Commission Regulation (EU) No 651/2014 (fixture)",
             "12016E107": "Treaty on the Functioning of the European Union, Article 107 (fixture)"}
    out = {}
    for celex, title in works.items():
        work_id = f"legal-work:fixture-{celex}"
        conn.execute("INSERT OR IGNORE INTO legal_works VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     [work_id, namespace, "cellar", "EU", "regulation" if "R" in celex else "treaty", "normative",
                      f"cellar:fixture:{celex}", json.dumps({"celex": celex}), "European Union", title,
                      "run:fixture", 1])
        out[celex] = work_id
    return out


HOLD_ENTITY = "gleif:lei:213800EXAMPLAHOLDS95"
INT_ENTITY = "gleif:lei:724500EXAMPLAINTBV75"
UK_ENTITY = "gleif:lei:213800EXAMPLAUKLTD71"
TRADE_ENTITY = "gleif:lei:213800EXAMPLATRADE88"


def reviewed(conn, *, legal: bool = True) -> dict:
    """Ownership reviewed, competition loaded, candidates reviewed against the Exampla group, citations linked.

    A reviewer accepts candidates whose ownership record sits in the Holdings or Intermediate clusters (identifier
    candidates and, after checking, the low-evidence name candidates) and rejects the others (the SEC record and the
    same-name decoy). Northwind parties have no candidates and stay unmatched.
    """
    from src.kb.competition_citations import CompetitionCitations
    from src.kb.competition_identity import CompetitionIdentity
    from src.kb.ownership_identity import OwnershipIdentityService

    ownership(conn)
    load_all(conn)
    identity = CompetitionIdentity(conn)
    proposed = identity.propose(NS, ownership_namespace=OWN_NS, principal_id="analyst", scopes=SCOPES)
    clusters = OwnershipIdentityService(conn).clusters(OWN_NS)
    good = {clusters.get(HOLD_ENTITY), clusters.get(INT_ENTITY)}
    for view in proposed["candidates"]:
        ok = clusters.get(view["ownership_key"], view["ownership_key"]) in good
        identity.review(NS, view["candidate_id"], "accept" if ok else "reject",
                        "register identifiers and name checked" if ok else "different register entity",
                        principal_id="reviewer", scopes=REVIEW_SCOPES)
    works = seed_legal(conn) if legal else {}
    CompetitionCitations(conn).link(NS, scopes=SCOPES)
    return {"identity": identity, "works": works}


class Clock:
    def __init__(self, start: int = 1_760_000_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value
