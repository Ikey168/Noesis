"""Offline Public Procurement harness: the real source-pack runtime, pinned authored fixtures.

Every provider response is served from authored fixtures
(``tests/fixtures/source_packs/procurement-*.json`` for the first
observation, ``tests/fixtures/procurement/*-round2.json`` for the second)
through the real native adapters, so budgets, cursors, receipts, watermarks,
the document store and the procurement projector all run while nothing leaves
the process. Receipts record ``execution: injected``; this is never live
coverage.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path

import duckdb

from src.ingestion.procurement_providers import FIXTURE_SECRET, OcdsAdapter, TedAdapter, fixture_transport
from src.ingestion.source_pack_runtime import FixturePageAdapter, SourcePackRuntime
from src.ingestion.source_packs import SourcePackError, SourcePackStore, validate_source_pack

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/procurement"
PACK = ROOT / "config/source_packs/procurement.json"
NS = "procurement"
SCOPES = frozenset({
    "knowledge:procurement:read", "knowledge:procurement:write", "knowledge:ingestion:execute",
    f"namespace:{NS}:read", f"namespace:{NS}:write",
    "knowledge:projects:read", "knowledge:projects:write", "knowledge:reports:read", "knowledge:reports:write",
    "knowledge:subscriptions:read", "knowledge:subscriptions:write", "knowledge:companies:read",
    "knowledge:entity-history:read", "knowledge:entity-history:write",
})
REVIEWER_SCOPES = SCOPES | {"knowledge:procurement:review", "knowledge:entity-history:review", "knowledge:entity-history:execute"}
LEIS = json.loads((FIXTURES / "identities.json").read_text())["leis"]
TED_N1 = "0f5a2c1e-1111-4b11-9111-000000000001"
TED_PIN = "0f5a2c1e-1111-4b11-9111-000000000002"
TED_CLOSED = "0f5a2c1e-1111-4b11-9111-000000000003"
TED_CANTEEN = "0f5a2c1e-1111-4b11-9111-000000000004"
UK_TENDER = "ocds-h6vhtk-0fx001"
SAM_SOLICITATION = "FX-26-R-0001"
BUYER = "Bezirksamt Fixture-Mitte von Berlin"


def ms(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def manifest():
    return validate_source_pack(json.loads(PACK.read_text()))


class Env:
    def __init__(self, now_iso="2026-09-27T09:00:00+00:00", path=None, *, restart_of=None):
        """A fresh offline deployment, or (``restart_of``) the same database reopened after a restart."""
        self.conn = duckdb.connect(path or ":memory:")
        self.clock = ms(now_iso) if restart_of is None else restart_of.clock
        self.ticks = 0 if restart_of is None else restart_of.ticks
        self.rounds = 0 if restart_of is None else restart_of.rounds
        self.pack = manifest()
        if restart_of is None:
            SourcePackStore(self.conn).install(self.pack, principal_id="operator", enable=True, now_ms=self.clock)
        self.runtime = SourcePackRuntime(self.conn, now=self.tick, sleep=lambda _d: None)
        if restart_of is None:
            for source in self.pack["sources"]:
                self.runtime.accept_license("procurement", source["source_id"], principal_id="operator")

    def now(self):
        return self.clock

    def tick(self):
        self.ticks += 1
        return self.clock + self.ticks

    def source(self, source_id):
        return copy.deepcopy(next(s for s in self.pack["sources"] if s["source_id"] == source_id))

    def adapters(self, round_=1, fail=()):
        adapters = self.runtime.fixture_adapters("procurement", ROOT)
        if round_ == 2:
            adapters["ted-notices"] = TedAdapter(self.source("ted-notices"), transport=fixture_transport(
                json.loads((FIXTURES / "ted-round2.json").read_text())["native_pages"]))
            adapters["uk-fts-ocds"] = OcdsAdapter(self.source("uk-fts-ocds"), transport=fixture_transport(
                json.loads((FIXTURES / "uk-fts-round2.json").read_text())["native_pages"]))
        for source_id in fail:
            adapters[source_id] = FixturePageAdapter(self.source(source_id), [[]], failures={
                0: SourcePackError("source_unavailable", "fixture outage")})
        return adapters

    def acquire(self, round_=1, *, fail=(), sources=None):
        self.rounds += 1
        return self.runtime.run(
            {"pack_id": "procurement", "run_key": f"run-{self.rounds}", "operation": "notices",
             "source_ids": sources or [s["source_id"] for s in self.pack["sources"]], "max_results": 1000,
             "max_bytes": 50_000_000, "max_pages": 10, "timeout_ms": 60_000, "retries": 0},
            principal_id="operator", adapters=self.adapters(round_, fail), secret_resolver=lambda _ref: FIXTURE_SECRET,
            dns_resolver=lambda _host: ["8.8.8.8"])

    def notices(self):
        from src.kb.procurement_notices import ProcurementNoticeStore

        return ProcurementNoticeStore(self.conn, now=self.now)

    def key(self, provider, procedure_id):
        from src.kb.procurement_notices import procedure_key

        return procedure_key(NS, provider, procedure_id)


SUPPLIER_FACTS = {
    "supplier.legal_name": {"value": "Nordlicht Fixture IT GmbH"},
    "supplier.establishment_country": {"value": "DE"},
    "supplier.jurisdictions": {"value": ["DE", "AT"]},
    "supplier.cpv_interests": {"value": ["72250000", "72222300", "72400000"]},
    "supplier.size": {"value": "small"},
    "supplier.sme": {"value": True},
    "supplier.employees": {"value": 38},
    "supplier.annual_turnover": {"value": {"amount": "1500000", "currency": "EUR"},
                                 "evidence": [{"kind": "document", "id": "annual-accounts-2025 (fixture)"}]},
    "supplier.certifications": {"value": ["ISO 9001", "ISO/IEC 20000-1"]},
    "supplier.references_count": {"value": 4},
    "supplier.past_contracts": {"value": [{"title": "Sovereign cloud migration for Stadt Fixturestadt", "buyer": "Stadt Fixturestadt",
                                           "year": 2025, "value": {"amount": "210000", "currency": "EUR"}, "cpv": "72222300",
                                           "reference_available": True}]},
    "supplier.languages": {"value": ["de", "en"]},
    "exclusion.criminal_conviction": {"value": False},
    "exclusion.tax_arrears": {"value": False},
    "exclusion.social_security_arrears": {"value": False},
    "exclusion.insolvency": {"value": False},
    "exclusion.professional_misconduct": {"value": False},
    "preferences.timezone": {"value": "Europe/Berlin"},
    "preferences.max_effort": {"value": "high"},
    "preferences.min_days_to_deadline": {"value": 14},
    "preferences.min_contract_value": {"value": {"amount": "50000", "currency": "EUR"}},
    "preferences.max_contract_value": {"value": {"amount": "1000000", "currency": "EUR"}},
}


def supplier_profile(env, *, owner="alice", scopes=SCOPES, key="supplier", overrides=None, drop=()):
    """A clearly synthetic German IT SME (Nordlicht Fixture IT GmbH)."""
    from src.kb.procurement_profiles import ProcurementProfileStore

    store = ProcurementProfileStore(env.conn, now=env.now)
    profile = store.create(NS, key, label="Synthetic supplier: Nordlicht Fixture IT GmbH (fixture)", principal_id=owner, scopes=scopes)
    facts = {k: v for k, v in {**SUPPLIER_FACTS, **(overrides or {})}.items() if k not in drop}
    return store.update(NS, profile["profile_id"], "initial", 1, set_facts=facts, principal_id=owner, scopes=scopes)
