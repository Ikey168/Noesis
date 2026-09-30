"""Offline harness for the Legal regulatory enforcement features (#2651): authored responses, real adapter.

Every file under ``tests/fixtures/enforcement`` is authored in the publisher's
documented or observed shape for fictional actions, respondents, facilities and
amounts (the Exampla and Northwind groups of the ownership fixtures; the
individuals are placeholders the adapter never stores); nothing here is live
coverage. Responses go through :class:`EnforcementAdapter` (the connector the
runtime compiles) and :class:`EnforcementProjector`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb

from src.ingestion.enforcement_sources import EnforcementAdapter, fixture_transport
from src.ingestion.source_packs import validate_source_pack
from src.kb.enforcement import EnforcementProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/enforcement"
PACK = ROOT / "config/source_packs/legal.json"
NS = "global"
OWN_NS = "ownership"
SOURCES = ("sec-enforcement-releases", "fca-final-notices", "epa-echo-enforcement-cases",
           "edpb-art60-final-decisions")
SEC_LR = "enforcement:action:us-sec:LR-99901"
SEC_AP = "enforcement:action:us-sec:34-99902"
FCA_EX = "enforcement:action:uk-fca:exampla-uk-limited-2025"
FCA_NW = "enforcement:action:uk-fca:northwind-brokers-limited-2024"
EPA_EX = "enforcement:action:us-epa:09-2025-9901"
EPA_NW = "enforcement:action:us-epa:05-2024-9902"
EDPB_EX = "enforcement:action:eu-dpa-nl:exampla-intermediate-bv-security-of-processing"
EDPB_ANON = "enforcement:action:eu-dpa-ie:anonymised-controller-reprimand-2025"
SCOPES = {
    "knowledge:legal:read", "knowledge:legal:write", f"namespace:{NS}:read", f"namespace:{NS}:write",
    "knowledge:ownership:read", "knowledge:ownership:write", f"namespace:{OWN_NS}:read", f"namespace:{OWN_NS}:write",
    "knowledge:subscriptions:read", "knowledge:subscriptions:write", "knowledge:ingestion:execute",
    "namespace:competition:read",
}
REVIEW_SCOPES = SCOPES | {"knowledge:ownership:review", "knowledge:legal:review"}
READ_ONLY = {"knowledge:legal:read", f"namespace:{NS}:read"}
# The later publisher revisions: an amended FCA notice, a settled EPA case and a withdrawn EDPB register entry.
V2 = {
    "fca-final-notices": {"/publication/final-notices/exampla-uk-limited-2025.pdf": "v2/fca_exampla-uk-limited-2025.txt"},
    "epa-echo-enforcement-cases": {
        "/echo/case_rest_services.get_case_info?output=JSON&p_id=09-2025-9901": "v2/echo_09-2025-9901.json"},
}
GONE = {"edpb-art60-final-decisions": "/art-60-final-decisions/anonymised-controller-reprimand-2025_en"}
HOLD_ENTITY = "gleif:lei:213800EXAMPLAHOLDS95"
INT_ENTITY = "gleif:lei:724500EXAMPLAINTBV75"
UK_ENTITY = "gleif:lei:213800EXAMPLAUKLTD71"
TRADE_ENTITY = "gleif:lei:213800EXAMPLATRADE88"
SEC_FILER = "sec-edgar:cik:0009999101"


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id: str) -> dict:
    return copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == source_id))


def native_pages(source_id: str, overrides: dict[str, str] | None = None, *, bodies: dict[str, str] | None = None,
                 gone: bool = False) -> list[dict]:
    item = source(source_id)
    pages = json.loads((ROOT / item["fixture"]["path"]).read_text())["native_pages"]
    for page in pages:
        if overrides and page["request"] in overrides:
            page["body"] = (FIXTURES / overrides[page["request"]]).read_text()
        if bodies and page["request"] in bodies:
            page["body"] = bodies[page["request"]]
        if gone and GONE.get(source_id) == page["request"]:
            page["status"], page["body"] = 410, "Gone"
    return pages


def fetch(source_id: str, *, overrides=None, bodies=None, gone: bool = False, item: dict | None = None) -> list:
    item = item or source(source_id)
    adapter = EnforcementAdapter(item, transport=fixture_transport(native_pages(source_id, overrides, bodies=bodies,
                                                                                gone=gone)))
    out, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "records", "parameters": {}, "limit": 100}, cursor=cursor)
        out.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return out


def records(source_id: str, **kwargs) -> list[dict]:
    return [item["enforcement_record"] for page in fetch(source_id, **kwargs) for item in page.records]


def apply(conn, source_id: str, *, run_id: str | None = None, v2: bool = False, overrides=None, bodies=None,
          item: dict | None = None, now=None) -> list[dict]:
    item = item or source(source_id)
    overrides = dict(overrides or {})
    if v2:
        overrides.update(V2.get(source_id, {}))
    projector = EnforcementProjector(conn)
    if now is not None:
        projector.store.now = now
    results = []
    for page in fetch(source_id, overrides=overrides, bodies=bodies, item=item, gone=v2):
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
    from tests.unit.competition_harness import ownership as competition_ownership

    competition_ownership(conn, review=review)


def seed_legal(conn, namespace: str = "global") -> dict[str, str]:
    """Legal works for the GDPR, 15 U.S.C. and FSMA 2000, as the Legal providers would project them.

    Test data only: the rows stand in for acquired works so exact identifier links can be exercised offline.
    """
    from src.kb.legal import LegalStore

    LegalStore(conn)
    works = {"gdpr": ("32016R0679", {"celex": "32016R0679"}, "Regulation (EU) 2016/679 (fixture)"),
             "usc15": ("usc:15:78j", {"usc_provision": "usc:15:78j"}, "15 U.S.C. § 78j (fixture)"),
             "fsma": ("ukpga/2000/8", {"legislation_gov_uk": "ukpga/2000/8"},
                      "Financial Services and Markets Act 2000 (fixture)")}
    out = {}
    for name, (ident, identifiers, title) in works.items():
        work_id = f"legal-work:fixture-{name}"
        conn.execute("INSERT OR IGNORE INTO legal_works VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     [work_id, namespace, "cellar" if name == "gdpr" else "fixture", "EU" if name == "gdpr" else
                      ("US" if name == "usc15" else "GB"), "regulation" if name == "gdpr" else "statute",
                      "normative", f"fixture:{ident}", json.dumps(identifiers), "fixture", title, "run:fixture", 1])
        out[name] = work_id
    return out


def reviewed(conn, *, legal: bool = True, competition: bool = True) -> dict:
    """Ownership reviewed, enforcement loaded, respondents reviewed against the Exampla group, links made.

    A reviewer accepts the CIK candidate against the SEC EDGAR filer and the candidates whose ownership record sits in
    an Exampla group cluster (Holdings, UK, Trading, Intermediate) - the low-evidence name candidates after checking
    - and rejects the others (the same-name decoy). Northwind respondents have no candidates and stay unmatched.
    """
    from src.kb.enforcement_identity import EnforcementIdentity
    from src.kb.enforcement_links import EnforcementLinks
    from src.kb.ownership_identity import OwnershipIdentityService

    ownership(conn)
    if competition:
        from tests.unit import competition_harness as ch

        ch.apply(conn, "ec-competition-cases")
    load_all(conn)
    identity = EnforcementIdentity(conn)
    proposed = identity.propose(NS, ownership_namespace=OWN_NS, principal_id="analyst", scopes=SCOPES)
    clusters = OwnershipIdentityService(conn).clusters(OWN_NS)
    good = {clusters.get(k, k) for k in (HOLD_ENTITY, INT_ENTITY, UK_ENTITY, TRADE_ENTITY)}
    for view in proposed["candidates"]:
        ok = clusters.get(view["ownership_key"], view["ownership_key"]) in good or view["ownership_key"] == SEC_FILER
        identity.review(NS, view["candidate_id"], "accept" if ok else "reject",
                        "identifiers and register name checked" if ok else "a different register entity",
                        principal_id="reviewer", scopes=REVIEW_SCOPES)
    works = seed_legal(conn) if legal else {}
    EnforcementLinks(conn).link(NS, scopes=SCOPES)
    return {"identity": identity, "works": works, "proposed": proposed}


class Clock:
    def __init__(self, start: int = 1_760_000_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value
