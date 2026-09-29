"""Offline harness for the Political legislation features (#2208): authored responses through the real adapter.

Every file under ``tests/fixtures/legislation`` is authored in the provider's
documented shape for a fictional bill in a placeholder Congress (156th) and UK
session (2098-99); nothing here is live coverage. Responses go through
:class:`LegislationAdapter` (the connector the runtime compiles) and
:class:`LegislationProjector`, which commits each record as an official-record
document revision; dossiers are built by the existing
:class:`LegislativeDossierStore`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb

from src.ingestion.legislation_sources import FIXTURE_SECRET, LegislationAdapter, fixture_transport
from src.ingestion.source_packs import validate_source_pack
from src.kb.legislation import LegislationProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/legislation"
PACK = ROOT / "config/source_packs/political.json"
NS = "global"
DOSSIER_NS = "research"
US_BILL = "us-bill:156-hr-9901"
UK_BILL = "uk-bill:3901"
US_SOURCES = ("us-congress-gov-bills", "us-congress-gov-house-votes", "us-senate-roll-calls", "us-govinfo-bills",
              "us-govinfo-billstatus")
UK_SOURCES = ("uk-parliament-bills", "uk-commons-divisions", "uk-lords-divisions", "uk-hansard-debates")
SOURCES = US_SOURCES + UK_SOURCES
SCOPES = {
    "knowledge:political:legislation:read", "knowledge:political:legislation:write",
    f"namespace:{NS}:read", f"namespace:{NS}:write",
    "knowledge:political:dossier:read", "knowledge:political:dossier:write",
    f"namespace:{DOSSIER_NS}:read", f"namespace:{DOSSIER_NS}:write",
    "knowledge:ownership:read", "knowledge:ownership:write",
    "knowledge:subscriptions:read", "knowledge:subscriptions:write",
    "knowledge:political:lobbying:read", "knowledge:political:lobbying:write",
    "knowledge:legal:read", "knowledge:legal:write",
}
REVIEW_SCOPES = SCOPES | {"knowledge:political:legislation:review", "knowledge:ownership:review",
                          "knowledge:political:lobbying:review"}
READ_ONLY = {"knowledge:political:legislation:read", f"namespace:{NS}:read"}
# The later provider revisions (the bill became law, Royal Assent, a corrected division list).
V2 = {
    "us-congress-gov-bills": {"/v3/bill/156/hr/9901?format=json": "v2/congress_bill_hr9901.json",
                              "/v3/bill/156/hr/9901/actions?format=json&limit=250":
                                  "v2/congress_bill_hr9901_actions.json"},
    "uk-parliament-bills": {"/api/v1/Bills/3901": "v2/uk_bill_3901.json",
                            "/api/v1/Bills/3901/Stages?Take=250": "v2/uk_bill_3901_stages.json",
                            "/api/v1/Bills/3901/Publications": "v2/uk_bill_3901_publications.json"},
    "uk-commons-divisions": {"/data/division/1701.json": "v2/uk_commons_division_1701.json"},
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


def fetch(source_id: str, *, overrides=None, bodies=None, item: dict | None = None, extra_pages=None,
          secret: str | None = FIXTURE_SECRET) -> list:
    item = item or source(source_id)
    pages = native_pages(source_id, overrides, bodies=bodies) + list(extra_pages or [])
    adapter = LegislationAdapter(item, transport=fixture_transport(pages), secret=secret)
    out, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": "selection", "parameters": {}, "limit": 50}, cursor=cursor)
        out.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return out


def apply(conn, source_id: str, *, run_id: str | None = None, v2: bool = False, overrides=None, bodies=None,
          item: dict | None = None, extra_pages=None) -> list[dict]:
    item = item or source(source_id)
    overrides = dict(overrides or {})
    if v2:
        overrides.update(V2.get(source_id, {}))
    projector = LegislationProjector(conn)
    results = []
    for index, page in enumerate(fetch(source_id, overrides=overrides, bodies=bodies, item=item,
                                       extra_pages=extra_pages)):
        results += projector.project_page(run_id=run_id or f"run:{source_id}:{'v2' if v2 else 'v1'}",
                                          manifest=None, source=item, records=page.records, documents=[],
                                          page_receipt={**page.receipt, "unit_index": index}, principal_id="operator")
    return results


def apply_lda(conn, *, run_id: str = "run:us-senate-lda") -> dict:
    """The US LDA filings (lobbying register ``us-lda``) through the lobbying feature's own adapter and projector."""
    from src.ingestion.lobbying_sources import LobbyingRegisterAdapter
    from src.ingestion.lobbying_sources import fixture_transport as lobbying_transport
    from src.kb.lobbying import LobbyingProjector

    item = source("us-senate-lda")
    pages = json.loads((ROOT / item["fixture"]["path"]).read_text())["native_pages"]
    fetched = LobbyingRegisterAdapter(item, transport=lobbying_transport(pages)).fetch_page(
        {"operation": "export", "parameters": {}, "limit": 100}, cursor=None)
    return LobbyingProjector(conn).project_page(run_id=run_id, manifest=None, source=item, records=fetched.records,
                                                documents=[], page_receipt=fetched.receipt,
                                                principal_id="operator")[0]


def seed_uk_act(conn, namespace: str = DOSSIER_NS) -> str:
    """A Legal work carrying the Act citation, as a future UK legislation provider would project it.

    The Legal pack ships no UK or US statute provider yet; this row stands in for one so the enactment link can be
    exercised offline. It is test data only.
    """
    from src.kb.legal import LegalStore

    LegalStore(conn)
    work_id = "legal-work:fixture-ukpga-2099-5"
    conn.execute("INSERT INTO legal_works VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 [work_id, namespace, "fixture-uk-statutes", "GB", "act", "normative", "ukpga/2099/5",
                  json.dumps({"official_id": "ukpga/2099/5", "citation": "2099 c. 5"}), "Parliament of the "
                  "United Kingdom", "Example Heat Networks Act 2099", "run:fixture", 1])
    return work_id


def load_all(conn, *, v2: bool = False) -> None:
    for source_id in SOURCES:
        apply(conn, source_id, v2=v2)


def connection():
    return duckdb.connect(":memory:")


class Clock:
    def __init__(self, start: int = 4_080_000_000_000) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value
