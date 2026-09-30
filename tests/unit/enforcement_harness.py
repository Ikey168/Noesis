"""Offline harness for the Legal enforcement provider (#2651): authored responses, real adapter and projector.

Every file under ``tests/fixtures/enforcement`` is authored in the publisher's
documented or observed shape for fictional actions, respondents and figures
(the Exampla group of the ownership fixtures, years 2097-2099); nothing here is
live coverage. Responses go through :class:`EnforcementAdapter` (the connector
the runtime compiles) and :class:`EnforcementProjector`.
"""

from __future__ import annotations

import base64
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
NS = "enforcement"
OWN_NS = "ownership"
SOURCES = ("sec-litigation-releases", "sec-administrative-proceedings", "fca-final-notices", "epa-echo-cases",
           "edpb-art60-decisions")
SEC_LR = "enforcement:action:us-sec:LR-99901"
SEC_AP = "enforcement:action:us-sec:34-99902"
FCA_EXAMPLA = "enforcement:action:uk-fca:exampla-uk-limited-2099"
FCA_NORTHWIND = "enforcement:action:uk-fca:northwind-payments-limited-2099"
FCA_PERSON = "enforcement:action:uk-fca:jordan-example-2099"
EPA = "enforcement:action:us-epa-echo:04-2099-0101"
EDPB_IE = "enforcement:action:edpb:99901"
EDPB_NL = "enforcement:action:edpb:99902"
HOLD_ENTITY = "gleif:lei:213800EXAMPLAHOLDS95"
UK_ENTITY = "gleif:lei:213800EXAMPLAUKLTD71"
INT_ENTITY = "gleif:lei:724500EXAMPLAINTBV75"
SEC_CIK_ENTITY = "sec-edgar:cik:0009999101"
PERSON_ENTITY = "open-ownership:statement:oo-fixture-per-1"
SCOPES = {
    "knowledge:legal:read", "knowledge:legal:write", f"namespace:{NS}:read", f"namespace:{NS}:write",
    "knowledge:ownership:read", "knowledge:ownership:write", f"namespace:{OWN_NS}:read", f"namespace:{OWN_NS}:write",
    "namespace:global:read", "namespace:competition:read", "knowledge:subscriptions:read",
    "knowledge:subscriptions:write", "knowledge:ingestion:execute",
}
REVIEW_SCOPES = SCOPES | {"knowledge:ownership:review"}
READ_ONLY = {"knowledge:legal:read", f"namespace:{NS}:read"}
# The later publisher revisions: the SEC final judgment, the corrected FCA notice, the closed ECHO case with its cost
# recovery published and the EDPB register entry the register no longer serves.
V2 = {
    "sec-litigation-releases": {"/enforcement-litigation/litigation-releases/lr-99901": "v2/sec_lr-99901.html"},
    "fca-final-notices": {"/publication/final-notices/exampla-uk-limited-2099.pdf":
                          "v2/fca_exampla-uk-limited-2099.pdf"},
    "epa-echo-cases": {"/echo/case_rest_services.get_case_report?output=JSON&p_id=04-2099-0101":
                       "v2/echo_04-2099-0101.json"},
}
V2_STATUS = {"edpb-art60-decisions": {
    "/our-work-tools/consistency-findings/register-for-article-60-final-decisions/decision-no-99902_en": 404}}


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id: str) -> dict:
    return copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == source_id))


def native_pages(source_id: str, overrides: dict[str, str] | None = None, *, statuses: dict[str, int] | None = None,
                 bodies: dict[str, str] | None = None) -> list[dict]:
    item = source(source_id)
    pages = json.loads((ROOT / item["fixture"]["path"]).read_text())["native_pages"]
    for page in pages:
        if overrides and page["request"] in overrides:
            path = FIXTURES / overrides[page["request"]]
            if path.suffix == ".pdf":
                page.pop("body", None)
                page["body_base64"] = base64.b64encode(path.read_bytes()).decode()
            else:
                page.pop("body_base64", None)
                page["body"] = path.read_text()
        if bodies and page["request"] in bodies:
            page.pop("body_base64", None)
            page["body"] = bodies[page["request"]]
        if statuses and page["request"] in statuses:
            page.update(status=statuses[page["request"]], body="Not Found")
            page.pop("body_base64", None)
    return pages


def v2_pages(source_id: str) -> list[dict]:
    return native_pages(source_id, V2.get(source_id), statuses=V2_STATUS.get(source_id))


def fetch(source_id: str, *, pages: list[dict] | None = None, item: dict | None = None) -> list:
    item = item or source(source_id)
    adapter = EnforcementAdapter(item, transport=fixture_transport(pages or native_pages(source_id)))
    out, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "records", "parameters": {}, "limit": 100}, cursor=cursor)
        out.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, source_id: str, *, run_id: str | None = None, v2: bool = False, pages: list[dict] | None = None,
          now=None) -> list[dict]:
    item = source(source_id)
    projector = EnforcementProjector(conn)
    if now is not None:
        projector.store.now = now
    results = []
    for page in fetch(source_id, pages=pages or (v2_pages(source_id) if v2 else None), item=item):
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
    from tests.unit import competition_harness

    competition_harness.ownership(conn, review=review)


def seed_legal(conn, namespace: str = "global") -> dict[str, str]:
    """Legal works for the GDPR, FSMA 2000 and the Securities Exchange Act, as the Legal providers would hold them.

    Test data only: the rows stand in for acquired Legal works so exact citation links can be exercised offline.
    """
    from src.kb.legal import LegalStore

    LegalStore(conn)
    works = {"gdpr": ("EU", "32016R0679", {"celex": "32016R0679"}, "Regulation (EU) 2016/679 (fixture)"),
             "fsma": ("UK", "ukpga/2000/8", {"legislation_gov_uk": "ukpga/2000/8"},
                      "Financial Services and Markets Act 2000 (fixture)")}
    out = {}
    for name, (jurisdiction, native, identifiers, title) in works.items():
        work_id = f"legal-work:fixture-{name}"
        conn.execute("INSERT OR IGNORE INTO legal_works VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     [work_id, namespace, "fixture", jurisdiction, "regulation", "normative", f"fixture:{native}",
                      json.dumps(identifiers), "fixture", title, "run:fixture", 1])
        out[name] = work_id
    return out


def reviewed(conn, *, legal: bool = True) -> dict:
    """Ownership reviewed, enforcement loaded, respondent candidates reviewed against the Exampla group, linked.

    A reviewer accepts the candidates whose ownership record sits in the Holdings, UK or Intermediate clusters and
    the exact CIK candidate; the decoy stays rejected. Northwind Payments has no candidate and stays unmatched.
    """
    from src.kb.enforcement_identity import EnforcementIdentity
    from src.kb.enforcement_links import EnforcementLinks
    from src.kb.ownership_identity import OwnershipIdentityService

    ownership(conn)
    load_all(conn)
    identity = EnforcementIdentity(conn)
    proposed = identity.propose(NS, ownership_namespace=OWN_NS, principal_id="analyst", scopes=SCOPES)
    clusters = OwnershipIdentityService(conn).clusters(OWN_NS)
    good = {clusters.get(HOLD_ENTITY), clusters.get(UK_ENTITY), clusters.get(INT_ENTITY), SEC_CIK_ENTITY}
    for view in proposed["candidates"]:
        ok = clusters.get(view["ownership_key"], view["ownership_key"]) in good
        identity.review(NS, view["candidate_id"], "accept" if ok else "reject",
                        "identifier and register name checked" if ok else "a different register entity",
                        principal_id="reviewer", scopes=REVIEW_SCOPES)
    works = seed_legal(conn) if legal else {}
    EnforcementLinks(conn).link(NS, ownership_namespace=OWN_NS, scopes=SCOPES)
    return {"identity": identity, "proposed": proposed, "works": works}


class Clock:
    def __init__(self, start: int = 4_102_444_800_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value
