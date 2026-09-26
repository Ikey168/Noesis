from __future__ import annotations

import duckdb

from src.domains.market.capabilities import MarketCapabilityService
from src.kb.quantitative import CALCULATE_SCOPE, READ_SCOPE


def test_quantitative_capability_surface_runs_gate_workflow_and_replays_export():
    conn = duckdb.connect(":memory:")
    service = MarketCapabilityService(conn)
    scopes = {CALCULATE_SCOPE, READ_SCOPE}
    bars = [{"listing_id": "listing:subject", "bar_start_ms": i, "close": 100 + i, "revision_id": f"bar:{i}"} for i in range(10)]
    event_args = {
        "namespace": "market:gate",
        "events": [{"event_id": "event:1", "event_at_ms": 4, "source_revision_ids": ["event:rev"]}],
        "bars": bars,
        "benchmark_bars": [{**row, "close": row["close"] * 0.99} for row in bars],
        "estimation_window": 2,
        "event_window": [0, 1],
        "owner": "alice",
    }
    event = service.invoke("market_event_study", event_args, principal_id="alice", scopes=scopes)
    assert event["ok"]
    run_id = event["result"]["run_id"]
    inspected = service.invoke("inspect_market_quantitative_run", {"namespace": "market:gate", "run_id": run_id}, principal_id="alice", scopes=scopes)
    exported = service.invoke("export_market_quantitative_run", {"namespace": "market:gate", "run_id": run_id}, principal_id="alice", scopes=scopes)
    assert inspected["ok"] and exported["ok"]
    assert exported["result"]["replay"]["deterministic"]
    assert exported["result"]["artifact"]["record_hash"] == inspected["result"]["record_hash"]

    factor = service.invoke(
        "market_factor_analysis",
        {
            "namespace": "market:gate",
            "observations": [
                {"at_ms": i, "listing_id": "listing:subject", "factors": {"market": i / 100}, "return": i / 1000}
                for i in range(6)
            ],
            "factor_definitions": {"market": {"definition": "fixture market factor"}},
            "owner": "alice",
        },
        principal_id="alice",
        scopes=scopes,
    )
    backtest = service.invoke(
        "market_backtest",
        {
            "namespace": "market:gate",
            "strategy_id": "fixture",
            "strategy_version": "1",
            "signals": [{"listing_id": "listing:subject", "signal_at_ms": 1, "execution_at_ms": 2, "exit_at_ms": 3, "signal": 1, "execution_price": 100, "exit_price": 101}],
            "owner": "alice",
        },
        principal_id="alice",
        scopes=scopes,
    )
    walk = service.invoke(
        "market_walk_forward",
        {
            "namespace": "market:gate",
            "samples": [{"target_at_ms": i, "features_available_at_ms": max(0, i - 1), "features": {"x": i}, "label": i + 1} for i in range(8)],
            "feature_names": ["x"],
            "train_window": 3,
            "test_window": 1,
            "gap": 0,
            "owner": "alice",
        },
        principal_id="alice",
        scopes=scopes,
    )
    portfolio = service.invoke(
        "market_portfolio",
        {
            "namespace": "market:gate",
            "portfolio_id": "portfolio:gate",
            "version": 1,
            "holdings": [{"listing_id": "listing:subject", "quantity": 1, "currency": "USD", "cash": 100}],
            "transactions": [],
            "base_currency": "USD",
            "as_of_ms": 2,
            "owner": "alice",
        },
        principal_id="alice",
        scopes=scopes,
    )
    risk = service.invoke(
        "market_risk_report",
        {
            "namespace": "market:gate",
            "portfolio": {"positions": [{"listing_id": "listing:subject", "market_value": 100, "sector": "tech", "geography": "US", "currency": "USD", "adv_value": 50}]},
            "returns": [{"return": value} for value in (0.01, -0.02, 0.03)],
            "owner": "alice",
        },
        principal_id="alice",
        scopes=scopes,
    )
    assert all(item["ok"] for item in (factor, backtest, walk, portfolio, risk))
    conn.close()
