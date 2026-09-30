"""Offline harness for the Political campaign-finance features (#2209): authored responses replayed through the adapter.

Every file under ``tests/fixtures/campaign_finance`` is authored in the
provider's documented shape (OpenFEC v1 JSON, Electoral Commission CSV) and
names fictional committees, candidates and organisations only; individual
donors carry placeholder names that the parser discards. Nothing here is live
coverage. Responses go through :class:`CampaignFinanceAdapter` (the connector
the runtime compiles) and :class:`CampaignFinanceProjector`.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import duckdb

from src.ingestion.campaign_finance_sources import (
    FIXTURE_SECRET,
    CampaignFinanceAdapter,
    fixture_transport,
    requests_for,
)
from src.ingestion.source_packs import validate_source_pack
from src.kb.campaign_finance_records import CampaignFinanceProjector

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/campaign_finance"
PACK = ROOT / "config/source_packs/political.json"
NS = "global"
OWN_NS = "ownership"
READ = "knowledge:political:campaign-finance:read"
WRITE = "knowledge:political:campaign-finance:write"
REVIEW = "knowledge:political:campaign-finance:review"
INDIVIDUAL = "knowledge:political:campaign-finance:individual-items:read"
SCOPES = {
    READ, WRITE, f"namespace:{NS}:read", f"namespace:{NS}:write", f"namespace:{OWN_NS}:read",
    f"namespace:{OWN_NS}:write", "knowledge:ownership:read", "knowledge:ownership:write",
    "knowledge:subscriptions:read", "knowledge:subscriptions:write", "knowledge:political:lobbying:read",
    "knowledge:political:elections:read",
}
REVIEW_SCOPES = SCOPES | {REVIEW, "knowledge:ownership:review"}
READ_ONLY = {READ, f"namespace:{NS}:read"}
CANDIDATE = "P99000001"
CAMPAIGN = "C00999901"
PAC = "C00999902"
SUPER_PAC = "C00999903"
NOT_ACQUIRED = "C00999904"
UK_PARTY = "9901"
# source id -> ordered (unit, fixture file) pairs, as declared in config/source_packs/political.json
UNITS: dict[str, list[tuple[dict, str]]] = {
    "us-fec-committees": [({"id": c}, f"fec_committee_{c}_history.json") for c in (CAMPAIGN, PAC, SUPER_PAC)],
    "us-fec-candidates": [({"id": c}, f"fec_candidate_{c}_history.json") for c in (CANDIDATE, "P99000002")],
    "us-fec-filings": [({"committee_id": c, "cycle": 2100}, f"fec_filings_{c}_2100.json")
                       for c in (CAMPAIGN, PAC, SUPER_PAC)],
    "us-fec-schedule-a": [({"committee_id": c, "cycle": 2100}, f"fec_schedule_a_{c}_2100.json")
                          for c in (CAMPAIGN, SUPER_PAC)],
    "us-fec-schedule-b": [({"committee_id": CAMPAIGN, "cycle": 2100}, f"fec_schedule_b_{CAMPAIGN}_2100.json")],
    "us-fec-schedule-e": [({"committee_id": SUPER_PAC, "cycle": 2100}, f"fec_schedule_e_{SUPER_PAC}_2100.json")],
    "uk-ec-donations": [({"id": UK_PARTY, "from": "2099-01-01", "to": "2099-06-30"}, "ukec_donations_9901_2099H1.csv")],
    "uk-ec-spending": [({"id": UK_PARTY, "from": "2099-03-01", "to": "2099-06-30"}, "ukec_spending_9901_2099.csv")],
}
SOURCES = list(UNITS)
US_SOURCES = [s for s in SOURCES if s.startswith("us-")]
UK_SOURCES = [s for s in SOURCES if s.startswith("uk-")]
FORMATS = {
    "us-fec-committees": "openfec-committee-history-json", "us-fec-candidates": "openfec-candidate-history-json",
    "us-fec-filings": "openfec-filings-json", "us-fec-schedule-a": "openfec-schedule-a-json",
    "us-fec-schedule-b": "openfec-schedule-b-json", "us-fec-schedule-e": "openfec-schedule-e-json",
    "uk-ec-donations": "ukec-donations-csv", "uk-ec-spending": "ukec-spending-csv",
}
ENDPOINTS = {"openfec": "https://api.open.fec.gov/v1", "ukec": "https://search.electoralcommission.org.uk"}


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id: str) -> dict:
    return copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == source_id))


def fixture_body(filename: str, version: str = "v1") -> str:
    path = FIXTURES / "v2" / filename if version == "v2" and (FIXTURES / "v2" / filename).exists() \
        else FIXTURES / filename
    return path.read_text()


def native_pages(source_id: str, version: str = "v1") -> list[dict]:
    """The native responses for every declared unit, keyed by the request the adapter makes."""
    fmt = FORMATS[source_id]
    endpoint = ENDPOINTS["ukec" if fmt.startswith("ukec") else "openfec"]
    pages = []
    for unit, filename in UNITS[source_id]:
        path, params, paging = requests_for(fmt, unit)
        if paging is None:
            params = {**params, "page": 1}
        query = urlencode(sorted(params.items()))
        pages.append({
            "request": urlsplit(endpoint).path + path + ("?" + query if query else ""), "status": 200,
            "headers": {"Content-Type": "text/csv" if paging == "csv" else "application/json"},
            "body": fixture_body(filename, version)})
    return pages


def source_pack_fixture(source_id: str) -> dict:
    return {
        "captured": None,
        "native_pages": native_pages(source_id),
        "note": "Authored responses in the provider's documented shape (cycle 2100 and state EX are placeholders); "
                "every committee, candidate and organisation is fictional and individual donors carry placeholder "
                "names that the parser discards (CF01 minimisation).",
        "provider": "authored",
        "scenarios": ["authored-fixture", "fictional-parties", "minimised-individuals"],
    }


def adapter(source_id: str, version: str = "v1", item: dict | None = None) -> CampaignFinanceAdapter:
    return CampaignFinanceAdapter(item or source(source_id), transport=fixture_transport(native_pages(source_id,
                                                                                                        version)),
                                  secret=FIXTURE_SECRET)


def apply(conn, source_id: str, *, version: str = "v1", run_id: str | None = None) -> list[dict]:
    """Every unit of one source through the real adapter and projector; returns the per-page outcomes."""
    item = source(source_id)
    fetcher = adapter(source_id, version, item)
    projector = CampaignFinanceProjector(conn)
    outcomes, cursor = [], None
    for _ in fetcher.units:
        page = fetcher.fetch_page({"operation": "selection", "parameters": {}, "limit": 500}, cursor=cursor)
        outcomes += projector.project_page(run_id=run_id or f"run:{source_id}:{version}", manifest=None, source=item,
                                           records=page.records, documents=[], page_receipt=page.receipt,
                                           principal_id="operator")
        cursor = page.next_cursor
        if cursor is None:
            break
    return outcomes


def load_all(conn, *, version: str = "v1", run_id: str | None = None) -> None:
    for source_id in SOURCES:
        apply(conn, source_id, version=version, run_id=run_id)


def load_ownership(conn, namespace: str = OWN_NS):
    """Synthetic Corporate Ownership records: a US parent and subsidiary (a cited direct-parent assertion) and a UK
    company carrying the Companies House number the Commission states for a donor."""
    from src.kb.ownership_records import record
    from src.kb.ownership_store import OwnershipStore

    def entity(key, name, jurisdiction, identifiers, provider="gleif"):
        return record("legal_entity", key, {"provider": provider, "provider_record_id": key.split(":", 1)[1]},
                      name=name, jurisdiction=jurisdiction, identifiers=identifiers)

    records = [
        entity("lei:5299EXAMPLEINDUSTR01", "Example Industries Inc", "US",
               [{"scheme": "lei", "value": "5299EXAMPLEINDUSTR01"}]),
        entity("lei:5299EXAMPLEENERGY001", "Example Industries Energy LLC", "US",
               [{"scheme": "lei", "value": "5299EXAMPLEENERGY001"}]),
        entity("gb-coh:09990002", "Example Holdings Ltd", "GB", [{"scheme": "gb-coh", "value": "09990002"}],
               provider="companies-house"),
        record("ownership_assertion", "gleif:parent:direct:5299EXAMPLEENERGY001:5299EXAMPLEINDUSTR01",
               {"provider": "gleif", "provider_record_id": "rr-5299EXAMPLEENERGY001"},
               subject_key="lei:5299EXAMPLEENERGY001", assertion_kind="direct_parent",
               holder={"key": "lei:5299EXAMPLEINDUSTR01", "kind": "entity"},
               validity={"from": "2090-01-01", "to_status": "open"}),
    ]
    return OwnershipStore(conn).apply(namespace, records, run_id="ownership-fixture", observed_at_ms=0,
                                      principal_id="ownership-loader")


def load_lobbying(conn) -> None:
    """The US LDA filings and the UK consultant-lobbyist register through the lobbying feature's own adapters."""
    from tests.unit import legislation_harness as lh
    from tests.unit import lobbying_harness as lob

    lh.apply_lda(conn)
    lob.apply(conn, "uk-orcl", "uk_orcl_2099-04-30.csv")


def load_elections(conn) -> None:
    """The fictional US presidential and UK general election result files (#1908)."""
    from tests.unit import elections_harness as eh

    item = eh.source("gb")
    item["elections"]["election"]["name"] = "UK Parliamentary General Election 2099"
    eh.apply(conn, "us", eh.US)
    eh.apply(conn, "gb", eh.UK, item=item)
