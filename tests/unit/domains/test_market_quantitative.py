from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.domains.market.quantitative import (
    MarketQuantitativeError,
    MarketQuantitativeStore,
)
from src.kb.quantitative import CALCULATE_SCOPE, READ_SCOPE

ROOT = Path(__file__).resolve().parents[3]
NS = "market:quant-fixture"
SCOPES = {CALCULATE_SCOPE, READ_SCOPE}


def bars(multiplier: float = 1.0):
    return [{"listing_id": "listing:subject", "bar_start_ms": index, "close": multiplier * (100 + index), "revision_id": f"bar:{index}"} for index in range(12)]


def test_event_study_factor_and_walk_forward_receipts_are_reproducible():
    conn = duckdb.connect(":memory:")
    store = MarketQuantitativeStore(conn, now=lambda: 10_000)
    event = store.event_study(
        NS,
        events=[{"event_id": "earnings-1", "event_at_ms": 5, "source_revision_ids": ["event:1"], "confounders": ["macro-release"]}],
        bars=bars(),
        benchmark_bars=bars(0.99),
        estimation_window=3,
        event_window=(-1, 2),
        placebo_events=[{"event_id": "placebo-1", "event_at_ms": 8}],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert event["sample_size"] == 1
    assert event["events"][0]["cumulative_abnormal_return"] is not None
    assert event["multiple_comparisons"]["method"] == "bonferroni"
    assert event["overlapping_event_pairs"] == []
    assert event["placebo_events"][0]["status"] == "compared"
    assert store.inspect_run(NS, event["run_id"], principal_id="alice", scopes=SCOPES)["run_id"] == event["run_id"]

    observations = []
    for index in range(12):
        factors = {
            "market": (index - 5) * 0.01,
            "size": ((index * 3) % 5 - 2) * 0.02,
            "value": ((index * 5) % 7 - 3) * 0.015,
            "momentum": ((index * 7) % 9 - 4) * 0.01,
            "quality": ((index * 11) % 6 - 2) * 0.012,
        }
        observations.append({"at_ms": index, "listing_id": f"listing:{index % 2}", "factors": factors, "return": 0.01 + 0.5 * factors["market"] + 0.2 * factors["size"] - 0.1 * factors["value"] + 0.3 * factors["momentum"] + 0.15 * factors["quality"]})
    factor = store.factor_analysis(
        NS,
        observations=observations,
        factor_definitions={name: {"definition": name, "version": "1"} for name in ("market", "size", "value", "momentum", "quality")},
        split_at_ms=8,
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert factor["exposure"]["market"] == pytest.approx(0.5, abs=1e-8)
    assert factor["out_of_sample"]["sample_size"] == 4

    samples = [{"target_at_ms": index, "features_available_at_ms": max(0, index - 1), "features": {"signal": float(index)}, "label": float(index + 1)} for index in range(12)]
    walk = store.walk_forward(
        NS,
        samples=samples,
        feature_names=["signal"],
        train_window=4,
        test_window=2,
        gap=1,
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert walk["stability"]["evaluated_folds"] >= 1
    assert walk["input_manifest"]["input_hash"]
    for result, name in (
        (event, "noesis-market-event-study-v1.json"),
        (factor, "noesis-market-factor-analysis-v1.json"),
        (walk, "noesis-market-walk-forward-v1.json"),
    ):
        Draft7Validator(json.loads((ROOT / "contracts/schemas/jsonschema" / name).read_text())).validate(result)
    conn.close()


def test_backtest_portfolio_and_risk_report_keep_costs_missing_inputs_and_exposures_visible():
    conn = duckdb.connect(":memory:")
    store = MarketQuantitativeStore(conn, now=lambda: 10_000)
    with pytest.raises(MarketQuantitativeError, match="another principal"):
        store.record_portfolio(
            NS,
            portfolio_id="portfolio:forbidden",
            version=1,
            holdings=[],
            transactions=[],
            base_currency="USD",
            as_of_ms=1,
            fx_rates={},
            valuations=[],
            benchmark_returns=[],
            owner="bob",
            principal_id="alice",
            scopes=SCOPES,
        )
    backtest = store.backtest(
        NS,
        signals=[
            {"listing_id": "listing:a", "signal_at_ms": 1, "execution_at_ms": 2, "exit_at_ms": 3, "signal": 1, "execution_price": 100, "exit_price": 110, "notional": 10_000, "source_revision_ids": ["bar:a"]},
            {"listing_id": "listing:b", "signal_at_ms": 1, "execution_at_ms": 2, "exit_at_ms": 3, "signal": -1, "execution_price": 100, "exit_price": 101},
            {"listing_id": "listing:c", "signal_at_ms": 1, "execution_at_ms": 2, "exit_at_ms": 3, "signal": 1, "execution_price": 100},
        ],
        strategy_id="fixture-strategy",
        strategy_version="v1",
        commission_bps=10,
        slippage_bps=5,
        initial_capital=100_000,
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert backtest["leakage_checks"]["passed"]
    assert len(backtest["missing_inputs"]) == 1
    assert backtest["final_equity"] != backtest["initial_capital"]

    portfolio = store.record_portfolio(
        NS,
        portfolio_id="portfolio:fixture",
        version=1,
        holdings=[
            {"listing_id": "listing:a", "quantity": 10, "currency": "USD", "cash": 1000, "sector": "tech", "geography": "US"},
            {"listing_id": "listing:b", "quantity": 5, "currency": "EUR", "sector": "health", "geography": "EU"},
        ],
        transactions=[{"at_ms": 1, "listing_id": "listing:a", "quantity": 2, "price": 100, "currency": "USD", "fee": 1, "source_revision_ids": ["txn:a"]}],
        base_currency="USD",
        as_of_ms=2,
        fx_rates={"EUR": 1.1},
        valuations=[{"at_ms": 1, "value": 10_000}, {"at_ms": 2, "value": 10_500, "external_flow": 100}],
        benchmark_returns=[{"return": 0.02}],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert portfolio["reconciliation"]["status"] == "complete"
    assert portfolio["time_weighted_return"] is not None

    risk = store.risk_report(
        NS,
        portfolio={"positions": [
            {"listing_id": "listing:a", "market_value": 700, "sector": "tech", "geography": "US", "currency": "USD", "adv_value": 350},
            {"listing_id": "listing:b", "market_value": 300, "sector": "health", "geography": "EU", "currency": "EUR", "adv_value": 100},
        ]},
        returns=[{"at_ms": index, "return": value} for index, value in enumerate([0.01, -0.02, 0.03, -0.01, 0.0, -0.04])],
        scenarios=[{"name": "rate-shock", "shocks": {"listing:a": -0.1, "listing:b": -0.2}}],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert risk["historical_expected_shortfall"] >= risk["historical_var"]
    assert risk["concentration"]["hhi"] > 0
    assert risk["scenarios"][0]["contributions"]

    schema_names = [
        "noesis-market-event-study-v1.json",
        "noesis-market-factor-analysis-v1.json",
        "noesis-market-backtest-v1.json",
        "noesis-market-portfolio-v1.json",
        "noesis-market-risk-report-v1.json",
    ]
    for result, name in zip((backtest, portfolio, risk), schema_names[2:], strict=True):
        Draft7Validator(json.loads((ROOT / "contracts/schemas/jsonschema" / name).read_text())).validate(result)
    conn.close()


def test_quantitative_leakage_and_collinearity_are_blocked():
    conn = duckdb.connect(":memory:")
    store = MarketQuantitativeStore(conn)
    with pytest.raises(MarketQuantitativeError, match="look-ahead"):
        store.backtest(
            NS,
            signals=[{"listing_id": "listing:a", "signal_at_ms": 2, "execution_at_ms": 2, "signal": 1, "execution_price": 100, "exit_price": 101}],
            strategy_id="bad",
            strategy_version="1",
            owner="alice",
            principal_id="alice",
            scopes=SCOPES,
        )
    with pytest.raises(MarketQuantitativeError, match="collinear"):
        store.factor_analysis(
            NS,
            observations=[{"at_ms": index, "listing_id": "a", "factors": {"x": 1, "y": 1}, "return": index / 100} for index in range(5)],
            factor_definitions={"x": {}, "y": {}},
            split_at_ms=None,
            owner="alice",
            principal_id="alice",
            scopes=SCOPES,
        )
    conn.close()
