"""Offline Corporate Ownership harness: real runtime adapters over pinned authored fixtures.

The corporate-ownership source pack is installed and run through the real
``SourcePackRuntime`` with its pinned fixtures compiled into network-free
adapters (``fixture_adapters``), so cursors, budgets, run receipts, document
evidence, the LEI projector and the ownership projector all execute while
nothing leaves the process. Everything here is offline fixture evidence and
must never be reported as live coverage.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import duckdb

from src.ingestion.ownership_providers import FIXTURE_SECRET
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.kb import ownership_bundle

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/ownership"
NS = "ownership"
PRINCIPAL = "analyst"
REVIEWER = "reviewer"
SCOPES = {
    "knowledge:ownership:read", "knowledge:ownership:write", "knowledge:ingestion:execute",
    f"namespace:{NS}:read", f"namespace:{NS}:write", "knowledge:reports:read", "knowledge:reports:write",
}
REVIEW_SCOPES = SCOPES | {"knowledge:ownership:review"}
READ_ONLY = {"knowledge:ownership:read", f"namespace:{NS}:read"}
HOLD, UK, TRADE, INT = "213800EXAMPLAHOLDS95", "213800EXAMPLAUKLTD71", "213800EXAMPLATRADE88", "724500EXAMPLAINTBV75"
UK_KEYS = {"gleif": f"gleif:lei:{UK}", "ch": "companies-house:gb-coh:09990002",
           "bods": "open-ownership:statement:oo-fixture-ent-uk-1"}
HOLD_KEYS = {"gleif": f"gleif:lei:{HOLD}", "ch": "companies-house:gb-coh:09990001",
             "bods": "open-ownership:statement:oo-fixture-ent-hold-1", "sec": "sec-edgar:cik:0009999101"}
INT_KEYS = {"gleif": f"gleif:lei:{INT}", "bods": "open-ownership:statement:oo-fixture-ent-int-1",
            "ch_psc": "companies-house:psc:09990002:FIXPSC002"}
DECOY = "open-ownership:statement:oo-fixture-ent-decoy-1"
MARKET_NS = "market:ownership-test"
T0 = 1_735_689_600_000  # 2025-01-01T00:00:00Z


def ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


class Env:
    def __init__(self, path: str | None = None, now_iso: str = "2026-09-26T09:00:00+00:00") -> None:
        self.conn = duckdb.connect(path) if path else duckdb.connect()
        self.clock = ms(now_iso)

    def now(self) -> int:
        self.clock += 1
        return self.clock

    def install(self) -> dict:
        return ownership_bundle.install_source_pack(self.conn, principal_id="operator", scopes={"operator"},
                                                    accept_terms=True)

    def acquire(self, run_key: str = "r1", *, source_ids=None, scopes=SCOPES, principal=PRINCIPAL) -> dict:
        adapters = SourcePackRuntime(self.conn).fixture_adapters(ownership_bundle.SOURCE_PACK_ID, ROOT)
        return ownership_bundle.acquire(self.conn, NS, run_key=run_key, principal_id=principal, scopes=scopes,
                                        source_ids=source_ids, adapters=adapters,
                                        secret_resolver=lambda _ref: FIXTURE_SECRET,
                                        dns_resolver=lambda _host: ["8.8.8.8"], now=self.now)

    def ready(self) -> "Env":
        self.install()
        self.acquire()
        return self


def source_ref(name: str, at_ms: int) -> dict:
    return {"source_ref_id": f"src:{name}", "provider": "fixture-only", "provider_object_id": name,
            "source_revision_id": f"fixture:{name}:1", "public_at_ms": at_ms, "source_snapshot_id": f"snapshot:{name}",
            "source_url": None, "retrieved_at_ms": at_ms, "content_hash": hashlib.sha256(name.encode()).hexdigest(),
            "license_id": "fixture-only", "entitlement_id": "entitlement:fixture"}


def seed_market(conn) -> dict:
    """A fictional issuer (CIK -> LEI in the instrument master), one security and one corporate action."""
    from src.domains.market.actions import MarketCorporateActionStore
    from src.domains.market.instruments import MarketInstrumentStore
    from tests.unit.domains.market_entitlement_fixtures import register_market_entitlement

    clock = {"now": T0}
    register_market_entitlement(conn, MARKET_NS, "entitlement:fixture", "fixture-only", "fixture-only", now_ms=T0)
    instruments = MarketInstrumentStore(conn, now=lambda: clock["now"])
    ref = source_ref("issuer-exampla", T0)
    instruments.put_issuer(MARKET_NS, issuer_id="issuer:exampla", kg_entity_id="kg:issuer:exampla",
                           display_name="Exampla Holdings plc", source_refs=[ref], principal_id="operator",
                           scopes={"operator"},
                           identifiers=[{"scheme": "cik", "value": "0009999101", "valid_from_ms": 0, "valid_to_ms": None,
                                         "source_ref_id": ref["source_ref_id"]},
                                        {"scheme": "lei", "value": HOLD, "valid_from_ms": 0, "valid_to_ms": None,
                                         "source_ref_id": ref["source_ref_id"]}])
    security_ref = source_ref("security-exampla", T0)
    instruments.put_security(MARKET_NS, issuer_id="issuer:exampla", security_id="security:exampla-ord",
                             security_type="common_equity", share_class="Ordinary", denomination_currency="GBP",
                             source_refs=[security_ref], principal_id="operator", scopes={"operator"})
    listing_ref = source_ref("listing-exampla", T0)
    instruments.put_listing(MARKET_NS, listing_id="listing:exampla-xnys", security_id="security:exampla-ord",
                            mic="XNYS", currency="USD", timezone="America/New_York", valid_from_ms=T0, valid_to_ms=None,
                            ticker_assertions=[{"value": "EXMP", "valid_from_ms": T0, "valid_to_ms": None,
                                                "source_ref_id": listing_ref["source_ref_id"]}],
                            source_refs=[listing_ref], principal_id="operator", scopes={"operator"})
    public = ms("2025-05-01T12:00:00Z")
    clock["now"] = public
    action_ref = source_ref("action-dividend", public)
    MarketCorporateActionStore(conn, now=lambda: clock["now"]).put_action(MARKET_NS, {
        "contract": "noesis-market-corporate-action-v1", "namespace": MARKET_NS, "owner": None,
        "action_id": "action:exampla-div-2025", "issuer_id": "issuer:exampla", "security_id": "security:exampla-ord",
        "listing_id": "listing:exampla-xnys", "related_security_id": None, "related_listing_id": None,
        "revision_id": "ignored", "revision": 1, "provider": "fixture-provider", "provider_record_id": "record:div-2025",
        "action_type": "cash_dividend", "status": "confirmed", "announced_at_ms": public, "public_at_ms": public,
        "effective_date": "2025-05-20", "ex_date": "2025-05-20", "record_date": None, "payable_date": None,
        "ratio_numerator": None, "ratio_denominator": None, "cash_amount": "0.12", "cash_amount_basis": "per_share_before_action",
        "distribution_type": "regular", "currency": "USD", "prior_revision_id": None, "source_refs": [action_ref],
        "recorded_at_ms": public, "record_hash": "ignored"}, principal_id="operator", scopes={"operator"})
    return {"namespace": MARKET_NS, "as_of_ms": ms("2026-01-01T00:00:00Z"), "acquired_by_ms": ms("2026-01-01T00:00:00Z"),
            "principal_id": "operator", "scopes": {"operator"}, "security_ids": ["security:exampla-ord"]}


def candidate(service, left: str, right: str, *, scopes=SCOPES) -> dict:
    a, b = sorted((left, right))
    return next(c for c in service.candidates(NS, scopes=scopes) if (c["left_key"], c["right_key"]) == (a, b))
