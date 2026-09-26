"""Fixture-only acceptance checks for company dashboards and peer selection."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import hashlib
import json

import duckdb
from jsonschema import Draft7Validator
import pytest

from src.domains.market.dashboard import MarketCompanyDashboardStore, MarketDashboardError
from src.domains.market.entitlements import MarketEntitlementStore
from src.domains.market.financial_facts import MarketFinancialFactStore
from src.domains.market.instruments import MarketInstrumentStore
from src.domains.market.prices import MarketPriceStore
from src.domains.market.quality import MarketQualityStore
from tests.unit.domains.market_entitlement_fixtures import FIXTURE_CAPABILITIES

NAMESPACE = "market:dashboard-test"
PRINCIPAL = "analyst:dashboard-test"
SCOPES = {"operator"}
T0 = int(datetime(2026, 1, 5, tzinfo=timezone.utc).timestamp() * 1000)
PUBLIC_CUTOFF = T0 + 120 * 86_400_000
ACQUIRED_CUTOFF = PUBLIC_CUTOFF + 1_000


def source_ref(name: str, public_at_ms: int = T0) -> dict:
    return {
        "source_ref_id": f"src:{name}",
        "provider": "fixture-only",
        "provider_object_id": name,
        "source_revision_id": f"fixture:{name}:1",
        "public_at_ms": public_at_ms,
        "source_snapshot_id": f"snapshot:{name}",
        "source_url": None,
        "retrieved_at_ms": public_at_ms,
        "content_hash": hashlib.sha256(name.encode()).hexdigest(),
        "license_id": "fixture-only",
        "entitlement_id": "entitlement:fixture",
    }


def bar(listing_id: str, day: date, close: float) -> dict:
    start = int(datetime.combine(day, time(14, 30), timezone.utc).timestamp() * 1000)
    public_at = start + 86_400_000
    return {
        "contract": "noesis-market-bar-v1",
        "namespace": NAMESPACE,
        "owner": None,
        "bar_id": "provider-bar-id",
        "listing_id": listing_id,
        "provider": "fixture-only",
        "interval": "1d",
        "bar_start_ms": start,
        "bar_end_ms": start + 23 * 60 * 60 * 1000,
        "public_at_ms": public_at,
        "retrieved_at_ms": public_at,
        "revision_id": "provider-revision-id",
        "revision": 1,
        "provider_record_id": f"{listing_id}:{day.isoformat()}",
        "provider_revision_id": "fixture:1",
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": 1000,
        "trade_count": None,
        "vwap": close,
        "currency": "USD" if listing_id != "listing:eur" else "EUR",
        "price_basis": "unadjusted",
        "adjustment_method": None,
        "adjustment_cutoff_ms": None,
        "adjustment_action_revision_ids": [],
        "adjustment_calculation_id": None,
        "prior_revision_id": None,
        "source_refs": [source_ref(f"bar:{listing_id}:{day.isoformat()}", public_at)],
        "recorded_at_ms": public_at,
        "record_hash": "0" * 64,
    }


def financial_fact(accession: str, concept: str, canonical: str, value: str, public_at_ms: int) -> dict:
    fact_id = f"fact:{accession}:{concept}:annual-2025:USD"
    return {
        "contract": "noesis-market-financial-fact-v1",
        "namespace": NAMESPACE,
        "owner": None,
        "fact_observation_id": fact_id,
        "issuer_id": "issuer:subject",
        "revision_id": f"{fact_id}@source",
        "revision": 1,
        "filing_accession": accession,
        "filing_form": "10-K",
        "taxonomy": "us-gaap",
        "concept": concept,
        "canonical_concept": canonical,
        "statement": "income_statement",
        "mapping_status": "mapped",
        "context_id": "context:annual-2025",
        "context_id_kind": "companyfacts_composite_key",
        "unit": "USD",
        "period": {"kind": "duration", "start_date": "2025-01-01", "end_date": "2025-12-31"},
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "period_class": "annual",
        "value_lexical": value,
        "scale": 0,
        "decimals": -3,
        "filed_at_ms": public_at_ms,
        "accepted_at_ms": public_at_ms,
        "public_at_ms": public_at_ms,
        "retrieved_at_ms": public_at_ms,
        "source_document_revision_id": f"filing:{accession}",
        "source_locator": f"companyfacts/{accession}/{concept}",
        "provider": "fixture-only",
        "prior_revision_id": None,
        "source_refs": [source_ref(f"fact:{accession}:{concept}", public_at_ms)],
        "recorded_at_ms": public_at_ms,
        "record_hash": "0" * 64,
    }


@pytest.fixture
def market():
    clock = {"now": T0 + 20 * 86_400_000}
    conn = duckdb.connect(":memory:")
    MarketEntitlementStore(conn, now=lambda: clock["now"]).put_entitlement(
        NAMESPACE,
        "entitlement:fixture",
        provider="fixture-only",
        license_id="fixture-only",
        capabilities=FIXTURE_CAPABILITIES,
        evidence_ref="synthetic:rights-review:v1",
        decision_ref="synthetic:reviewer:v1",
        principal_id="fixture-reviewer",
        scopes={"operator"},
        effective_at_ms=0,
    )
    instruments = MarketInstrumentStore(conn, now=lambda: clock["now"])
    for issuer_id, security_id, industry, currency, listing_id in (
        ("issuer:subject", "security:subject", "TECH", "USD", "listing:subject"),
        ("issuer:peer", "security:peer", "TECH", "USD", "listing:peer"),
        ("issuer:eur", "security:eur", "TECH", "EUR", "listing:eur"),
    ):
        issuer_ref = source_ref(issuer_id)
        instruments.put_issuer(
            NAMESPACE,
            issuer_id=issuer_id,
            kg_entity_id=f"kg:{issuer_id}",
            display_name=issuer_id,
            source_refs=[issuer_ref],
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )
        security_ref = source_ref(security_id)
        instruments.put_security(
            NAMESPACE,
            issuer_id=issuer_id,
            security_id=security_id,
            security_type="common_equity",
            share_class="Common",
            denomination_currency=currency,
            industry_code=industry,
            source_refs=[security_ref],
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )
        listing_ref = source_ref(listing_id)
        instruments.put_listing(
            NAMESPACE,
            listing_id=listing_id,
            security_id=security_id,
            mic="XNAS",
            currency=currency,
            timezone="America/New_York",
            valid_from_ms=T0 - 86_400_000,
            valid_to_ms=None,
            ticker_assertions=[
                {
                    "value": listing_id.rsplit(":", 1)[-1].upper(),
                    "valid_from_ms": T0 - 86_400_000,
                    "valid_to_ms": None,
                    "source_ref_id": listing_ref["source_ref_id"],
                }
            ],
            source_refs=[listing_ref],
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )
    prices = MarketPriceStore(conn, now=lambda: clock["now"])
    for listing_id, closes in (("listing:subject", [100, 105]), ("listing:peer", [90, 99])):
        for day, close in zip((date(2026, 1, 5), date(2026, 1, 6)), closes, strict=True):
            prices.put_bar(NAMESPACE, bar(listing_id, day, close), principal_id=PRINCIPAL, scopes=SCOPES)
    return conn, clock, prices


def test_dashboard_preserves_panels_provenance_metrics_and_currency_exclusions(market):
    conn, _, _ = market
    dashboard = MarketCompanyDashboardStore(conn, initialize=True).build_dashboard(
        NAMESPACE,
        "listing:subject",
        start_ms=T0 - 86_400_000,
        end_ms=T0 + 20 * 86_400_000,
        acquired_by_ms=ACQUIRED_CUTOFF,
        publicly_available_by_ms=PUBLIC_CUTOFF,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        peer_listing_ids=["listing:peer", "listing:eur", "listing:missing"],
        industry_code="TECH",
        include_calculations=True,
    )

    assert dashboard["contract"] == "noesis-market-company-dashboard-v1"
    assert dashboard["peer_count"] == 1
    assert [item["role"] for item in dashboard["panels"]] == ["subject", "peer"]
    assert dashboard["panels"][0]["prices"]["state"] == "available"
    assert dashboard["panels"][0]["metrics"]["price"]["quantitative_calculation_id"]
    assert dashboard["freshness"]["source_ref_count"] >= 6
    reasons = {item["reason"] for item in dashboard["exclusions"]}
    assert {"currency_mismatch", "identity_unavailable"}.issubset(reasons)
    assert dashboard["drilldown"]["source_revisions"]

    schema = json.loads(
        open("contracts/schemas/jsonschema/noesis-market-company-dashboard-v1.json", encoding="utf-8").read()
    )
    Draft7Validator(schema).validate(dashboard)


def test_dashboard_surfaces_quarantined_price_as_degraded_input(market):
    conn, _, prices = market
    retained = prices.get_bars(
        NAMESPACE,
        "listing:subject",
        start_ms=T0 - 86_400_000,
        end_ms=T0 + 20 * 86_400_000,
        acquired_by_ms=ACQUIRED_CUTOFF,
        publicly_available_by_ms=PUBLIC_CUTOFF,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    MarketQualityStore(conn).quarantine_revision(
        NAMESPACE,
        "price_bar",
        retained[0]["revision_id"],
        "fixture provider disagreement",
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )

    dashboard = MarketCompanyDashboardStore(conn, initialize=True).build_dashboard(
        NAMESPACE,
        "listing:subject",
        start_ms=T0 - 86_400_000,
        end_ms=T0 + 20 * 86_400_000,
        acquired_by_ms=ACQUIRED_CUTOFF,
        publicly_available_by_ms=PUBLIC_CUTOFF,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        include_calculations=False,
    )
    panel = dashboard["panels"][0]

    assert panel["quality"]["state"] == "degraded"
    assert panel["quality"]["quarantined_revision_ids"] == [
        retained[0]["revision_id"]
    ]
    assert panel["prices"]["page"]["quality_exclusion_count"] == 1
    assert [row["revision_id"] for row in panel["prices"]["items"]] == [
        retained[1]["revision_id"]
    ]


def test_dashboard_historical_cutoff_returns_empty_panel_and_visible_error(market):
    conn, clock, prices = market
    late = bar("listing:subject", date(2026, 2, 1), 120)
    prices.put_bar(NAMESPACE, late, principal_id=PRINCIPAL, scopes=SCOPES)
    clock["now"] = ACQUIRED_CUTOFF + 100
    dashboard = MarketCompanyDashboardStore(conn, initialize=True).build_dashboard(
        NAMESPACE,
        "listing:subject",
        start_ms=T0 - 86_400_000,
        end_ms=T0 + 40 * 86_400_000,
        acquired_by_ms=ACQUIRED_CUTOFF,
        publicly_available_by_ms=T0 + 5 * 86_400_000,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        peer_listing_ids=["listing:missing"],
        include_calculations=False,
    )

    prices_panel = dashboard["panels"][0]["prices"]
    assert prices_panel["state"] == "available"
    assert all(item["bar_start_ms"] < T0 + 5 * 86_400_000 for item in prices_panel["items"])
    assert dashboard["panels"][0]["metrics"]["price"]["status"] == "not_requested"
    assert any(item["reason"] == "identity_unavailable" for item in dashboard["exclusions"])


def test_dashboard_selects_restatements_at_public_cutoff_and_calculates_ratios(market):
    conn, clock, _ = market
    facts = MarketFinancialFactStore(conn, now=lambda: clock["now"])
    original_revenue = facts.put_fact(
        NAMESPACE,
        financial_fact("accession:original", "Revenues", "revenue", "100", T0 + 7 * 86_400_000),
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    operating_income = facts.put_fact(
        NAMESPACE,
        financial_fact("accession:original", "OperatingIncomeLoss", "operating_income", "40", T0 + 7 * 86_400_000),
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    amended_revenue = facts.put_fact(
        NAMESPACE,
        financial_fact("accession:amended", "Revenues", "revenue", "120", T0 + 35 * 86_400_000),
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    clock["now"] = T0 + 40 * 86_400_000

    def build(cutoff):
        return MarketCompanyDashboardStore(conn, initialize=True, now=lambda: clock["now"]).build_dashboard(
            NAMESPACE,
            "listing:subject",
            start_ms=T0 - 86_400_000,
            end_ms=T0 + 50 * 86_400_000,
            acquired_by_ms=ACQUIRED_CUTOFF,
            publicly_available_by_ms=cutoff,
            principal_id=PRINCIPAL,
            scopes=SCOPES,
            include_calculations=True,
        )["panels"][0]

    before = build(T0 + 20 * 86_400_000)
    after = build(T0 + 40 * 86_400_000)
    assert original_revenue["revision_id"] in before["drilldown"]["input_revision_ids"]
    assert amended_revenue["revision_id"] not in before["drilldown"]["input_revision_ids"]
    assert amended_revenue["revision_id"] in after["drilldown"]["input_revision_ids"]
    assert original_revenue["revision_id"] in after["drilldown"]["input_revision_ids"]
    assert operating_income["revision_id"] in after["drilldown"]["input_revision_ids"]

    def margin(panel):
        return next(item for item in panel["metrics"]["facts"]["metrics"] if item["name"] == "operating_margin")

    assert float(margin(before)["value"]) == pytest.approx(0.4)
    assert float(margin(after)["value"]) == pytest.approx(1 / 3)


def test_dashboard_exposes_empty_error_and_namespace_denial_states(market, monkeypatch):
    conn, _, _ = market
    store = MarketCompanyDashboardStore(conn, initialize=True)
    request = {
        "start_ms": T0 + 50 * 86_400_000,
        "end_ms": T0 + 60 * 86_400_000,
        "acquired_by_ms": ACQUIRED_CUTOFF,
        "publicly_available_by_ms": PUBLIC_CUTOFF,
        "principal_id": PRINCIPAL,
        "scopes": SCOPES,
        "include_calculations": False,
    }
    empty = store.build_dashboard(NAMESPACE, "listing:subject", **request)
    assert empty["panels"][0]["prices"]["state"] == "empty"
    assert empty["panels"][0]["statements"]["state"] == "empty"

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("fixture provider unavailable")

    monkeypatch.setattr(MarketPriceStore, "get_bars", unavailable)
    failed = store.build_dashboard(NAMESPACE, "listing:subject", **request)
    assert failed["panels"][0]["prices"]["state"] == "error"
    assert failed["panels"][0]["errors"][0]["panel"] == "prices"

    denied_request = {**request, "scopes": set()}
    with pytest.raises(MarketDashboardError, match="namespace access"):
        store.build_dashboard(NAMESPACE, "listing:subject", **denied_request)
