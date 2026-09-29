"""Fixture-only checks for typed, historical market screens."""

from __future__ import annotations

from datetime import date
import json

import duckdb
from jsonschema import Draft7Validator
import pytest

from src.domains.market.entitlements import MarketEntitlementStore
from src.domains.market.financial_facts import MarketFinancialFactStore
from src.domains.market.instruments import MarketInstrumentStore
from src.domains.market.prices import MarketPriceStore
from src.domains.market.screeners import MarketScreenerStore
from tests.unit.domains.market_entitlement_fixtures import FIXTURE_CAPABILITIES
from tests.unit.domains.test_market_dashboard import (
    ACQUIRED_CUTOFF,
    NAMESPACE,
    PRINCIPAL,
    PUBLIC_CUTOFF,
    SCOPES,
    T0,
    bar,
    source_ref,
)


@pytest.fixture
def market():
    conn = duckdb.connect(":memory:")
    MarketEntitlementStore(conn, now=lambda: ACQUIRED_CUTOFF).put_entitlement(
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
    instruments = MarketInstrumentStore(conn, now=lambda: ACQUIRED_CUTOFF)
    for issuer_id, security_id, listing_id in (
        ("issuer:screen-subject", "security:screen-subject", "listing:screen-subject"),
        ("issuer:screen-peer", "security:screen-peer", "listing:screen-peer"),
    ):
        instruments.put_issuer(
            NAMESPACE,
            issuer_id=issuer_id,
            kg_entity_id=f"kg:{issuer_id}",
            display_name=issuer_id,
            source_refs=[source_ref(issuer_id)],
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )
        instruments.put_security(
            NAMESPACE,
            issuer_id=issuer_id,
            security_id=security_id,
            security_type="common_equity",
            share_class="Common",
            denomination_currency="USD",
            industry_code="TECH",
            source_refs=[source_ref(security_id)],
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )
        listing_ref = source_ref(listing_id)
        instruments.put_listing(
            NAMESPACE,
            listing_id=listing_id,
            security_id=security_id,
            mic="XNAS",
            currency="USD",
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
        instruments.put_universe_membership(
            NAMESPACE,
            universe_id="universe:tech",
            security_id=security_id,
            membership_status="included",
            valid_from_ms=T0 - 86_400_000,
            valid_to_ms=None,
            source_refs=[source_ref(f"membership:{security_id}")],
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )
    prices = MarketPriceStore(conn, now=lambda: ACQUIRED_CUTOFF)
    for listing_id, closes in (
        ("listing:screen-subject", [100, 105]),
        ("listing:screen-peer", [90, 99]),
    ):
        for day, close in zip((date(2026, 1, 5), date(2026, 1, 6)), closes, strict=True):
            prices.put_bar(NAMESPACE, bar(listing_id, day, close), principal_id=PRINCIPAL, scopes=SCOPES)
    MarketFinancialFactStore(conn, now=lambda: ACQUIRED_CUTOFF)
    return conn


def test_screener_saves_revision_runs_historically_and_explains_each_result(market):
    store = MarketScreenerStore(market, now=lambda: ACQUIRED_CUTOFF)
    criteria = {
        "filters": [{"field": "price.close", "operator": "gte", "value": 100}],
        "ranking": {"field": "price.close", "direction": "desc"},
        "missing_policy": "exclude",
        "limit": 10,
    }
    saved = store.save_query(
        NAMESPACE,
        "screen:technology",
        criteria,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    result = store.run(
        NAMESPACE,
        universe_id="universe:tech",
        as_of_ms=T0 + 10 * 86_400_000,
        start_ms=T0 - 86_400_000,
        end_ms=T0 + 20 * 86_400_000,
        acquired_by_ms=ACQUIRED_CUTOFF,
        publicly_available_by_ms=PUBLIC_CUTOFF,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        query_id="screen:technology",
    )

    assert saved["revision"] == 1
    assert result["contract"] == "noesis-market-screener-run-v1"
    assert result["query_revision_id"] == saved["revision_id"]
    assert result["counts"]["passed"] == 1
    assert result["counts"]["failed"] == 1
    passed = next(item for item in result["results"] if item["state"] == "passed")
    assert passed["checks"][0]["input_revision_ids"]
    assert result["run_id"]
    assert result["input_snapshot"]["input_revision_ids"]
    exported = store.export_run(
        NAMESPACE,
        result["run_id"],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert exported["contract"] == "noesis-market-screener-export-v1"
    assert len(exported["results"]) == 1
    Draft7Validator(
        json.loads(open("contracts/schemas/jsonschema/noesis-market-screener-export-v1.json", encoding="utf-8").read())
    ).validate(exported)

    updated = store.save_query(
        NAMESPACE,
        "screen:technology",
        {**criteria, "filters": [{"field": "price.close", "operator": "gte", "value": 110}]},
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert updated["revision"] == 2

    run_schema = json.loads(
        open("contracts/schemas/jsonschema/noesis-market-screener-run-v1.json", encoding="utf-8").read()
    )
    Draft7Validator(run_schema).validate(result)


def test_screener_marks_stale_price_as_missing_and_rejects_malformed_limit(market):
    store = MarketScreenerStore(market, now=lambda: ACQUIRED_CUTOFF)
    with pytest.raises(Exception, match="criteria.limit"):
        store.save_query(
            NAMESPACE,
            "bad-limit",
            {"filters": [{"field": "price.close", "operator": "gte", "value": 1}], "limit": "ten"},
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )
    result = store.run(
        NAMESPACE,
        universe_id="universe:tech",
        as_of_ms=T0 + 10 * 86_400_000,
        start_ms=T0 - 86_400_000,
        end_ms=T0 + 20 * 86_400_000,
        acquired_by_ms=ACQUIRED_CUTOFF,
        publicly_available_by_ms=PUBLIC_CUTOFF,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        criteria={
            "filters": [{"field": "price.close", "operator": "gte", "value": 1}],
            "stale_after_ms": 86_400_000,
            "missing_policy": "exclude",
        },
    )
    assert result["counts"]["excluded_missing"] == 2
    assert all(item["checks"][0]["reason"] == "stale_data" for item in result["results"])
