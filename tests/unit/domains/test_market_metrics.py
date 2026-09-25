"""Independent reference checks for revision-pinned market metrics."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import hashlib
import json
import math
import statistics
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.domains.market.entitlements import MarketEntitlementStore
from src.domains.market.financial_facts import MarketFinancialFactStore
from src.domains.market.instruments import MarketInstrumentStore
from src.domains.market.metrics import MarketMetricError, MarketMetricStore
from src.domains.market.prices import MarketPriceStore
from src.domains.market.quality import MarketQualityStore
from src.kb.quantitative import QuantitativeStore
from tests.unit.domains.market_entitlement_fixtures import FIXTURE_CAPABILITIES

NAMESPACE = "market:metrics-test"
PRINCIPAL = "analyst:metrics-test"
SCOPES = {"operator"}
T0 = int(datetime(2026, 1, 5, 15, 30, tzinfo=timezone.utc).timestamp() * 1000)
PUBLIC_CUTOFF = T0 + 40 * 86_400_000
NOW = T0 + 200 * 86_400_000
ACQUIRED_CUTOFF = NOW + 1_000


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


@pytest.fixture
def market():
    conn = duckdb.connect(":memory:")
    MarketEntitlementStore(conn, now=lambda: NOW).put_entitlement(
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

    def clock():
        return NOW

    instruments = MarketInstrumentStore(conn, now=clock)
    instruments.put_issuer(
        NAMESPACE,
        issuer_id="issuer:metrics",
        kg_entity_id="kg:issuer:metrics",
        display_name="Metrics Test Inc.",
        source_refs=[source_ref("issuer")],
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    securities = {
        "listing:subject": "security:subject",
        "listing:benchmark": "security:benchmark",
    }
    for listing_id, security_id in securities.items():
        security_ref = source_ref(security_id)
        instruments.put_security(
            NAMESPACE,
            issuer_id="issuer:metrics",
            security_id=security_id,
            security_type="common_equity",
            share_class="Common",
            denomination_currency="USD",
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
    prices = MarketPriceStore(conn, now=clock)
    facts = MarketFinancialFactStore(conn, now=clock)
    metrics = MarketMetricStore(conn, now=clock)
    return conn, instruments, prices, facts, metrics


def bar_payload(listing_id: str, day: date, close: float) -> dict:
    start = int(datetime.combine(day, time(14, 30), timezone.utc).timestamp() * 1000)
    public_at = start + 86_400_000
    ref = source_ref(f"bar:{listing_id}:{day.isoformat()}", public_at)
    return {
        "contract": "noesis-market-bar-v1",
        "namespace": NAMESPACE,
        "owner": None,
        "bar_id": "provider-bar-id",
        "listing_id": listing_id,
        "provider": "fixture-only",
        "interval": "1d",
        "bar_start_ms": start,
        "bar_end_ms": start + 6 * 60 * 60 * 1000 + 30 * 60 * 1000,
        "public_at_ms": public_at,
        "retrieved_at_ms": public_at,
        "revision_id": "provider-revision-id",
        "revision": 1,
        "provider_record_id": f"{listing_id}:{day.isoformat()}",
        "provider_revision_id": "fixture:1",
        "open": close,
        "high": close + 1,
        "low": max(0, close - 1),
        "close": close,
        "volume": 1000,
        "trade_count": None,
        "vwap": close,
        "currency": "USD",
        "price_basis": "unadjusted",
        "adjustment_method": None,
        "adjustment_cutoff_ms": None,
        "adjustment_action_revision_ids": [],
        "adjustment_calculation_id": None,
        "prior_revision_id": None,
        "source_refs": [ref],
        "recorded_at_ms": public_at,
        "record_hash": "0" * 64,
    }


def metric_by_name(report: dict, name: str) -> dict:
    return next(item for item in report["metrics"] if item["name"] == name)


def test_price_metrics_match_independent_reference_and_replay(market):
    conn, _, prices, _, metrics = market
    dates = [date(2026, 1, day) for day in (5, 6, 7, 8)]
    subject_closes = [100.0, 110.0, 99.0, 108.0]
    benchmark_closes = [100.0, 105.0, 100.0, 105.0]
    stored_bars = []
    for listing, closes in (
        ("listing:subject", subject_closes),
        ("listing:benchmark", benchmark_closes),
    ):
        for day, close in zip(dates, closes, strict=True):
            stored_bars.append(
                prices.put_bar(
                    NAMESPACE,
                    bar_payload(listing, day, close),
                    principal_id=PRINCIPAL,
                    scopes=SCOPES,
                )
            )

    report = metrics.calculate_price_metrics(
        NAMESPACE,
        "listing:subject",
        start_ms=T0 - 86_400_000,
        end_ms=T0 + 20 * 86_400_000,
        acquired_by_ms=ACQUIRED_CUTOFF,
        publicly_available_by_ms=PUBLIC_CUTOFF,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
        benchmark_listing_id="listing:benchmark",
        periods_per_year=4,
        risk_free_rate_annual="0.04",
    )

    subject_returns = [
        subject_closes[index] / subject_closes[index - 1] - 1
        for index in range(1, len(subject_closes))
    ]
    benchmark_returns = [
        benchmark_closes[index] / benchmark_closes[index - 1] - 1
        for index in range(1, len(benchmark_closes))
    ]
    std = statistics.stdev(subject_returns)
    assert metric_by_name(report, "price_return")["value"] == "0.08"
    assert metric_by_name(report, "total_return")["value"] == "0.08"
    assert float(metric_by_name(report, "maximum_drawdown")["value"]) == pytest.approx(
        -0.1
    )
    assert float(
        metric_by_name(report, "annualized_volatility")["value"]
    ) == pytest.approx(std * math.sqrt(4), abs=1e-11)
    assert float(metric_by_name(report, "correlation")["value"]) == pytest.approx(
        statistics.correlation(subject_returns, benchmark_returns), abs=1e-11
    )
    assert float(metric_by_name(report, "beta")["value"]) == pytest.approx(
        statistics.covariance(subject_returns, benchmark_returns)
        / statistics.variance(benchmark_returns),
        abs=1e-11,
    )
    assert metric_by_name(report, "sharpe_ratio")["status"] == "available"
    assert len(report["series"]) == 3
    assert set(item["revision_id"] for item in stored_bars).issubset(
        report["input_revision_ids"]
    )
    assert any(
        item.startswith("market-listing:") for item in report["input_revision_ids"]
    )
    assert report["quantitative_calculation_id"]

    schema_path = (
        Path(__file__).resolve().parents[3]
        / "contracts/schemas/jsonschema/noesis-market-metric-report-v1.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    Draft7Validator(schema).validate(report)
    replay = QuantitativeStore(conn, initialize=False).replay_calculation(
        NAMESPACE, report["quantitative_calculation_id"], scopes=SCOPES
    )
    assert replay["deterministic"] is True


def test_price_metric_receipt_marks_quarantined_source_exclusion_degraded(market):
    conn, _, prices, _, metrics = market
    retained = []
    for day, close in zip(
        (date(2026, 1, day) for day in (5, 6, 7, 8)),
        (100.0, 101.0, 103.0, 104.0),
        strict=True,
    ):
        retained.append(
            prices.put_bar(
                NAMESPACE,
                bar_payload("listing:subject", day, close),
                principal_id=PRINCIPAL,
                scopes=SCOPES,
            )
        )
    MarketQualityStore(conn).quarantine_revision(
        NAMESPACE,
        "price_bar",
        retained[1]["revision_id"],
        "fixture correction under review",
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )

    report = metrics.calculate_price_metrics(
        NAMESPACE,
        "listing:subject",
        start_ms=T0 - 86_400_000,
        end_ms=T0 + 20 * 86_400_000,
        acquired_by_ms=ACQUIRED_CUTOFF,
        publicly_available_by_ms=PUBLIC_CUTOFF,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )

    assert report["quality"]["state"] == "degraded"
    assert report["quality"]["quarantined_revision_ids"] == [
        retained[1]["revision_id"]
    ]
    assert retained[1]["revision_id"] not in report["input_revision_ids"]


def test_price_metrics_report_empty_and_insufficient_history_without_fabricating_returns(market):
    _, _, prices, _, metrics = market
    arguments = {
        "start_ms": T0 - 86_400_000,
        "end_ms": T0 + 20 * 86_400_000,
        "acquired_by_ms": ACQUIRED_CUTOFF,
        "publicly_available_by_ms": PUBLIC_CUTOFF,
        "principal_id": PRINCIPAL,
        "scopes": SCOPES,
    }

    with pytest.raises(MarketMetricError) as empty:
        metrics.calculate_price_metrics(NAMESPACE, "listing:subject", **arguments)
    assert empty.value.code == "price_history_unavailable"

    prices.put_bar(
        NAMESPACE,
        bar_payload("listing:subject", date(2026, 1, 5), 100.0),
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    with pytest.raises(MarketMetricError) as insufficient:
        metrics.calculate_price_metrics(NAMESPACE, "listing:subject", **arguments)
    assert insufficient.value.code == "insufficient_history"


def fact_payload(
    observation_id: str,
    concept: str,
    value: str,
    unit: str,
    period: dict,
    *,
    canonical_concept: str | None = None,
    public_at_ms: int = T0 + 15 * 86_400_000,
) -> dict:
    ref = source_ref(f"fact:{observation_id}", public_at_ms)
    return {
        "contract": "noesis-market-financial-fact-v1",
        "namespace": NAMESPACE,
        "owner": None,
        "fact_observation_id": observation_id,
        "issuer_id": "issuer:metrics",
        "revision_id": f"ignored:{observation_id}",
        "revision": 1,
        "filing_accession": f"accession:{observation_id}",
        "filing_form": "10-K",
        "taxonomy": "us-gaap",
        "concept": concept,
        "canonical_concept": canonical_concept,
        "statement": "income_statement"
        if period["kind"] == "duration"
        else "balance_sheet",
        "mapping_status": "mapped" if canonical_concept else "unmapped",
        "context_id": f"context:{observation_id}",
        "context_id_kind": "companyfacts_composite_key",
        "unit": unit,
        "period": period,
        "fiscal_year": 2025,
        "fiscal_period": "FY",
        "period_class": "annual" if period["kind"] == "duration" else "instant",
        "value_lexical": value,
        "scale": 0,
        "decimals": -3,
        "filed_at_ms": public_at_ms,
        "accepted_at_ms": public_at_ms,
        "public_at_ms": public_at_ms,
        "retrieved_at_ms": public_at_ms,
        "source_document_revision_id": f"filing:{observation_id}@1",
        "source_locator": f"companyfacts/{concept}/{observation_id}",
        "provider": "fixture-only",
        "prior_revision_id": None,
        "source_refs": [ref],
        "recorded_at_ms": public_at_ms,
        "record_hash": "0" * 64,
    }


def test_filed_fact_metrics_enforce_period_units_and_denominator_rules(market):
    conn, _, _, facts, metrics = market
    current_period = {
        "kind": "duration",
        "start_date": "2025-01-01",
        "end_date": "2025-12-31",
    }
    prior_period = {
        "kind": "duration",
        "start_date": "2024-01-01",
        "end_date": "2024-12-31",
    }
    instant_period = {"kind": "instant", "instant_date": "2025-12-31"}
    payloads = {
        "revenue_current": fact_payload(
            "revenue-current",
            "Revenues",
            "1200",
            "USD",
            current_period,
            canonical_concept="revenue",
        ),
        "revenue_prior": fact_payload(
            "revenue-prior",
            "Revenues",
            "1000",
            "USD",
            prior_period,
            canonical_concept="revenue",
        ),
        "operating_income": fact_payload(
            "operating-income",
            "OperatingIncomeLoss",
            "120",
            "USD",
            current_period,
            canonical_concept="operating_income",
        ),
        "assets_eur": fact_payload(
            "assets-eur",
            "Assets",
            "100",
            "EUR",
            instant_period,
            canonical_concept="assets",
        ),
        "assets_usd": fact_payload(
            "assets-usd",
            "Assets",
            "300",
            "USD",
            instant_period,
            canonical_concept="assets",
        ),
        "liabilities_zero": fact_payload(
            "liabilities-zero",
            "LiabilitiesCurrent",
            "0",
            "USD",
            instant_period,
            canonical_concept="current_liabilities",
        ),
        "liabilities_negative": fact_payload(
            "liabilities-negative",
            "LiabilitiesCurrent",
            "-200",
            "USD",
            instant_period,
            canonical_concept="current_liabilities",
        ),
    }
    stored = {
        name: facts.put_fact(
            NAMESPACE,
            payload,
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )
        for name, payload in payloads.items()
    }
    fx_ref = source_ref("fx-eurusd-2025-12-31", T0 + 15 * 86_400_000)
    fx = {
        "from": "EUR",
        "to": "USD",
        "rate": "1.2",
        "rate_id": "fx-rate:EURUSD:2025-12-31@1",
        "rate_kind": "spot",
        "period": instant_period,
        "observed_at_ms": T0 + 15 * 86_400_000,
        "public_at_ms": T0 + 15 * 86_400_000,
        "source_refs": [fx_ref],
    }

    report = metrics.calculate_fact_metrics(
        NAMESPACE,
        "issuer:metrics",
        [
            {
                "name": "revenue_growth_yoy",
                "kind": "growth",
                "numerator_fact_revision_id": stored["revenue_current"]["revision_id"],
                "denominator_fact_revision_id": stored["revenue_prior"]["revision_id"],
            },
            {
                "name": "operating_margin",
                "kind": "margin",
                "numerator_fact_revision_id": stored["operating_income"]["revision_id"],
                "denominator_fact_revision_id": stored["revenue_current"][
                    "revision_id"
                ],
            },
            {
                "name": "operating_margin_again",
                "kind": "margin",
                "numerator_fact_revision_id": stored["operating_income"]["revision_id"],
                "denominator_fact_revision_id": stored["revenue_current"][
                    "revision_id"
                ],
            },
            {
                "name": "eur_market_value_to_usd_revenue",
                "kind": "valuation_multiple",
                "numerator_fact_revision_id": stored["assets_eur"]["revision_id"],
                "denominator_fact_revision_id": stored["revenue_current"][
                    "revision_id"
                ],
                "fx_rate": fx,
            },
            {
                "name": "zero_liquidity_denominator",
                "kind": "liquidity",
                "numerator_fact_revision_id": stored["assets_usd"]["revision_id"],
                "denominator_fact_revision_id": stored["liabilities_zero"][
                    "revision_id"
                ],
            },
            {
                "name": "negative_leverage_denominator",
                "kind": "leverage",
                "numerator_fact_revision_id": stored["assets_usd"]["revision_id"],
                "denominator_fact_revision_id": stored["liabilities_negative"][
                    "revision_id"
                ],
            },
        ],
        acquired_by_ms=ACQUIRED_CUTOFF,
        publicly_available_by_ms=PUBLIC_CUTOFF,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )

    assert metric_by_name(report, "revenue_growth_yoy")["value"] == "0.2"
    assert metric_by_name(report, "operating_margin")["value"] == "0.1"
    assert metric_by_name(report, "operating_margin_again")["value"] == "0.1"
    assert metric_by_name(report, "eur_market_value_to_usd_revenue")["value"] == "0.1"
    assert (
        metric_by_name(report, "zero_liquidity_denominator")["reason_code"]
        == "zero_denominator"
    )
    assert (
        metric_by_name(report, "negative_leverage_denominator")["reason_code"]
        == "negative_denominator"
    )
    assert fx_ref["source_revision_id"] in report["input_revision_ids"]
    assert len(report["conversion_calculation_ids"]) == 1
    assert len(report["formula_calculation_ids"]) == 3
    assert (
        metric_by_name(report, "operating_margin")["formula_calculation_id"]
        == (metric_by_name(report, "operating_margin_again")["formula_calculation_id"])
    )
    assert report["formula_registry_revisions"]["growth"].startswith(
        "quantitative-metric-revision:"
    )
    assert report["quantitative_calculation_id"]
    quantitative = QuantitativeStore(conn, initialize=False)
    formula_metrics = [
        metric for metric in report["metrics"] if metric.get("formula_calculation_id")
    ]
    for formula_metric in formula_metrics:
        replay = quantitative.replay_calculation(
            NAMESPACE,
            formula_metric["formula_calculation_id"],
            scopes=SCOPES,
        )
        assert replay["deterministic"] is True
        assert replay["formula_revision_id"] == formula_metric["formula_revision_id"]
    schema_path = (
        Path(__file__).resolve().parents[3]
        / "contracts/schemas/jsonschema/noesis-market-metric-report-v1.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft7Validator(schema).validate(report)


def test_fact_metrics_reject_asof_inaccessible_revisions_and_period_mismatch(market):
    _, _, _, facts, metrics = market
    current_period = {
        "kind": "duration",
        "start_date": "2025-01-01",
        "end_date": "2025-12-31",
    }
    different_period = {
        "kind": "duration",
        "start_date": "2024-04-01",
        "end_date": "2024-12-31",
    }
    current = facts.put_fact(
        NAMESPACE,
        fact_payload(
            "period-current",
            "Revenues",
            "1200",
            "USD",
            current_period,
            canonical_concept="revenue",
        ),
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    prior = facts.put_fact(
        NAMESPACE,
        fact_payload(
            "period-prior",
            "Revenues",
            "1000",
            "USD",
            different_period,
            canonical_concept="revenue",
        ),
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )

    with pytest.raises(
        MarketMetricError, match="same concept and comparable prior-year durations"
    ):
        metrics.calculate_fact_metrics(
            NAMESPACE,
            "issuer:metrics",
            [
                {
                    "name": "bad_growth",
                    "kind": "growth",
                    "numerator_fact_revision_id": current["revision_id"],
                    "denominator_fact_revision_id": prior["revision_id"],
                }
            ],
            acquired_by_ms=ACQUIRED_CUTOFF,
            publicly_available_by_ms=PUBLIC_CUTOFF,
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )


def test_market_capability_facade_pages_history_and_facts(market):
    from src.domains.market.capabilities import MarketCapabilityService

    conn, _, prices, facts, _ = market
    for day, close in zip(
        (date(2026, 1, 5), date(2026, 1, 6)), (100.0, 105.0), strict=True
    ):
        prices.put_bar(
            NAMESPACE,
            bar_payload("listing:subject", day, close),
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )

    ticker = MarketCapabilityService(conn).invoke(
        "lookup_instrument",
        {
            "namespace": NAMESPACE,
            "symbol": "SUBJECT",
            "as_of_ms": T0 + 1,
            "acquired_by_ms": ACQUIRED_CUTOFF,
            "publicly_available_by_ms": PUBLIC_CUTOFF,
        },
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert ticker["ok"] is True
    assert ticker["result"]["candidates"][0]["object_id"] == "listing:subject"

    history_args = {
        "namespace": NAMESPACE,
        "listing_id": "listing:subject",
        "start_ms": T0 - 86_400_000,
        "end_ms": T0 + 20 * 86_400_000,
        "acquired_by_ms": ACQUIRED_CUTOFF,
        "publicly_available_by_ms": PUBLIC_CUTOFF,
        "page_size": 1,
    }
    first = MarketCapabilityService(conn).invoke(
        "price_history", history_args, principal_id=PRINCIPAL, scopes=SCOPES
    )
    second = MarketCapabilityService(conn).invoke(
        "price_history",
        {**history_args, "cursor": first["result"]["page"]["next_cursor"]},
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert first["ok"] is second["ok"] is True
    assert len(first["result"]["bars"]) == len(second["result"]["bars"]) == 1
    assert first["result"]["page"]["next_cursor"] == 1
    assert second["result"]["page"]["next_cursor"] is None

    fact_period = {
        "kind": "duration",
        "start_date": "2025-01-01",
        "end_date": "2025-12-31",
    }
    for suffix, value in (("a", "10"), ("b", "20")):
        facts.put_fact(
            NAMESPACE,
            fact_payload(
                f"capability-{suffix}",
                "Revenues",
                value,
                "USD",
                fact_period,
                canonical_concept="revenue",
            ),
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )
    statement_page = MarketCapabilityService(conn).invoke(
        "financial_statements",
        {
            "namespace": NAMESPACE,
            "issuer_id": "issuer:metrics",
            "acquired_by_ms": ACQUIRED_CUTOFF,
            "publicly_available_by_ms": PUBLIC_CUTOFF,
            "page_size": 1,
        },
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    assert statement_page["ok"] is True
    assert len(statement_page["result"]["facts"]) == 1
    assert statement_page["result"]["page"]["next_cursor"] == 1

    denied = MarketCapabilityService(conn).invoke(
        "lookup_instrument",
        {
            "namespace": NAMESPACE,
            "object_type": "listing",
            "object_id": "listing:subject",
            "acquired_by_ms": ACQUIRED_CUTOFF,
        },
        principal_id="analyst:unscoped",
        scopes=set(),
    )
    assert denied == {
        "ok": False,
        "error": {
            "code": "unauthorized",
            "message": "current market and namespace access is required",
        },
    }


def test_market_rest_and_mcp_share_auth_calculations_and_replay(market, monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path

    import duckdb
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    conn, _, prices, _, _ = market
    for day, close in zip(
        (date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)),
        (100.0, 105.0, 103.0),
        strict=True,
    ):
        prices.put_bar(
            NAMESPACE,
            bar_payload("listing:subject", day, close),
            principal_id=PRINCIPAL,
            scopes=SCOPES,
        )
    repo = Path(__file__).resolve().parents[3]
    route_path = repo / "src/api/routes/market_routes.py"
    spec = importlib.util.spec_from_file_location(
        "market_routes_test_module", route_path
    )
    route_module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    monkeypatch.setitem(sys.modules, spec.name, route_module)
    spec.loader.exec_module(route_module)

    from src.database import local_analytics_connector

    monkeypatch.setattr(local_analytics_connector, "_CONNECTION", conn)
    app = FastAPI()
    app.include_router(route_module.router)
    token = route_module.require_auth.create_access_token(
        {"sub": PRINCIPAL, "scopes": ["operator"]}
    )
    no_scope_token = route_module.require_auth.create_access_token(
        {"sub": PRINCIPAL, "scopes": []}
    )
    price_request = {
        "namespace": NAMESPACE,
        "listing_id": "listing:subject",
        "start_ms": T0 - 86_400_000,
        "end_ms": T0 + 20 * 86_400_000,
        "acquired_by_ms": ACQUIRED_CUTOFF,
        "publicly_available_by_ms": PUBLIC_CUTOFF,
        "periods_per_year": 252,
        "risk_free_rate_annual": "0.03",
    }
    with TestClient(app) as client:
        denied = client.get("/api/v1/market/readiness", params={"namespace": NAMESPACE})
        assert denied.status_code == 401
        no_scope = client.get(
            "/api/v1/market/readiness",
            params={"namespace": NAMESPACE},
            headers={"Authorization": f"Bearer {no_scope_token}"},
        )
        assert no_scope.status_code == 403
        response = client.get(
            "/api/v1/market/readiness",
            params={"namespace": NAMESPACE},
            headers={"Authorization": f"Bearer {token}"},
        )
        metric_response = client.post(
            "/api/v1/market/calculations/prices",
            json=price_request,
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert metric_response.status_code == 200

    class BorrowedConnection:
        def __init__(self, inner):
            self.inner = inner

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def close(self):
            return None

    monkeypatch.setenv("NOESIS_MCP_PRINCIPAL", PRINCIPAL)
    monkeypatch.setattr(duckdb, "connect", lambda *a, **kw: BorrowedConnection(conn))
    from tools.market_mcp.server import _run as mcp_run

    monkeypatch.setenv("NOESIS_MCP_SCOPES", "")
    no_scope_mcp = mcp_run("readiness", {"namespace": NAMESPACE})
    assert no_scope.json() == no_scope_mcp

    monkeypatch.setenv("NOESIS_MCP_SCOPES", "operator")
    mcp_result = mcp_run("readiness", {"namespace": NAMESPACE})
    assert mcp_result["ok"] is True, mcp_result
    assert response.json() == mcp_result

    mcp_metric = mcp_run("calculate_price_metrics", price_request)
    assert metric_response.json() == mcp_metric
    report = metric_response.json()["result"]
    replay = QuantitativeStore(conn, initialize=False).replay_calculation(
        NAMESPACE, report["quantitative_calculation_id"], scopes=SCOPES
    )
    assert replay["deterministic"] is True
