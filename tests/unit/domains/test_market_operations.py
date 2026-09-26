from __future__ import annotations

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.domains.market.operations import (
    OPERATIONS_EXECUTE_SCOPE,
    OPERATIONS_READ_SCOPE,
    OPERATIONS_WRITE_SCOPE,
    MarketOperationsError,
    MarketOperationsStore,
)
from src.domains.market.capabilities import MarketCapabilityService


NS = "market:operations-fixture"
SCOPES = {
    OPERATIONS_READ_SCOPE,
    OPERATIONS_WRITE_SCOPE,
    OPERATIONS_EXECUTE_SCOPE,
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
}


def test_measurements_slos_health_and_budgets_are_bounded_and_replayable():
    conn = duckdb.connect(":memory:")
    store = MarketOperationsStore(conn, now=lambda: 1000)
    measurements = store.record_measurements(
        NS,
        measurements=[
            {"measurement_id": "m:latency:1", "metric": "query_latency_ms", "provider": "fixture", "observed_at_ms": 100, "value": 10, "status": "observed"},
            {"measurement_id": "m:latency:2", "metric": "query_latency_ms", "provider": "fixture", "observed_at_ms": 200, "value": 20, "status": "observed"},
            {"measurement_id": "m:coverage:1", "metric": "coverage_ratio", "provider": "fixture", "observed_at_ms": 200, "value": 0.8, "status": "degraded"},
            {"measurement_id": "m:cost:1", "metric": "cost_micros", "provider": "fixture", "observed_at_ms": 200, "value": 12, "status": "observed"},
        ],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    replay = store.record_measurements(
        NS,
        measurements=[{"measurement_id": "m:latency:1", "metric": "query_latency_ms", "provider": "fixture", "observed_at_ms": 100, "value": 10, "status": "observed"}],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert measurements["count"] == 4 and replay["measurements"][0]["idempotent"]
    slos = store.evaluate_slos(
        NS,
        targets={"query_latency_ms": {"value": 25, "direction": "max"}, "coverage_ratio": {"value": 0.75, "direction": "min"}},
        window_start_ms=0,
        window_end_ms=500,
        provider="fixture",
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert slos["slos"]["query_latency_ms"]["state"] == "pass"
    assert slos["slos"]["coverage_ratio"]["state"] == "degraded"
    health = store.provider_health(NS, provider=None, window_start_ms=0, window_end_ms=500, principal_id="alice", scopes=SCOPES)
    assert health["providers"]["fixture"]["state"] == "degraded"

    budget = store.save_budget(NS, budget_id="budget:1", period_start_ms=0, period_end_ms=1000, max_requests=2, max_cost_micros=100, owner="alice", principal_id="alice", scopes=SCOPES)
    charge = store.consume_budget(NS, budget_id="budget:1", charge_id="charge:1", requests=1, cost_micros=40, at_ms=100, principal_id="alice", scopes=SCOPES)
    charge_replay = store.consume_budget(NS, budget_id="budget:1", charge_id="charge:1", requests=1, cost_micros=40, at_ms=100, principal_id="alice", scopes=SCOPES)
    assert budget["status"] == "active" and charge["charge_status"] == "charged" and charge_replay["charge_status"] == "idempotent"
    with pytest.raises(MarketOperationsError, match="exceeded"):
        store.consume_budget(NS, budget_id="budget:1", charge_id="charge:2", requests=2, cost_micros=40, at_ms=100, principal_id="alice", scopes=SCOPES)
    conn.close()


def test_recovery_backup_runbook_and_repair_contracts():
    conn = duckdb.connect(":memory:")
    store = MarketOperationsStore(conn, now=lambda: 2000)
    drills = [
        store.record_recovery_drill(NS, drill_id=f"drill:{scenario}", scenario=scenario, failure_injected=True, expected_steps=[{"step": 1, "action": "quarantine"}], observed_steps=[{"step": 1, "result": "ok"}], status="passed", restored=True, duration_ms=30, owner="alice", principal_id="alice", scopes=SCOPES)
        for scenario in ("source_outage", "partial_corruption", "interrupted_backfill", "restore")
    ]
    backup = store.create_backup(NS, backup_id="backup:1", manifest={"tables": ["market_ops_measurements"]}, content={"measurement_ids": ["m:1"]}, owner="alice", principal_id="alice", scopes=SCOPES)
    restored = store.restore_backup(NS, backup_id="backup:1", expected_content_hash=backup["content_hash"], principal_id="alice", scopes=SCOPES)
    runbook = store.save_runbook(NS, runbook_id="runbook:1", revision=1, scenario="restore", steps=[{"action": "verify_hash"}], owner="alice", principal_id="alice", scopes=SCOPES)
    repair = store.record_repair(NS, repair_id="repair:1", runbook_id="runbook:1", status="passed", inputs={"backup_id": "backup:1"}, outcome={"restored": True}, owner="alice", principal_id="alice", scopes=SCOPES)
    assert all(item["replay_hash"] for item in drills) and restored["status"] == "restored" and runbook["revision"] == 1 and repair["status"] == "passed"
    preview = store.prune_audit(NS, before_ms=2500, dry_run=True, principal_id="alice", scopes=SCOPES)
    assert preview["dry_run"] and preview["deleted"] == 0
    with pytest.raises(MarketOperationsError, match="does not match"):
        store.restore_backup(NS, backup_id="backup:1", expected_content_hash="0" * 64, principal_id="alice", scopes=SCOPES)
    conn.close()


def test_operations_outputs_validate_against_contracts():
    conn = duckdb.connect(":memory:")
    store = MarketOperationsStore(conn, now=lambda: 3000)
    values = [
        ("noesis-market-operations-measurement-v1", store.record_measurements(NS, measurements=[{"measurement_id": "m:1", "metric": "job_success_ratio", "provider": "fixture", "observed_at_ms": 1, "value": 1}], owner="alice", principal_id="alice", scopes=SCOPES), "noesis-market-operations-measurement-v1.json"),
        ("noesis-market-slo-report-v1", store.evaluate_slos(NS, targets={"job_success_ratio": {"value": 1, "direction": "min"}}, window_start_ms=0, window_end_ms=2, provider="fixture", owner="alice", principal_id="alice", scopes=SCOPES), "noesis-market-slo-report-v1.json"),
        ("noesis-market-provider-health-v1", store.provider_health(NS, provider="fixture", window_start_ms=0, window_end_ms=2, principal_id="alice", scopes=SCOPES), "noesis-market-provider-health-v1.json"),
    ]
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    for contract, result, filename in values:
        assert result["contract"] == contract
        Draft7Validator(__import__("json").loads((root / "contracts/schemas/jsonschema" / filename).read_text())).validate(result)
    conn.close()


def test_operations_capability_surface_uses_shared_store():
    conn = duckdb.connect(":memory:")
    service = MarketCapabilityService(conn)
    recorded = service.invoke(
        "record_market_operations_measurements",
        {"namespace": NS, "measurements": [{"measurement_id": "m:cap", "metric": "coverage_ratio", "provider": "fixture", "observed_at_ms": 1, "value": 1}]},
        principal_id="alice",
        scopes=SCOPES,
    )
    health = service.invoke(
        "market_provider_health",
        {"namespace": NS, "provider": "fixture", "window_start_ms": 0, "window_end_ms": 2},
        principal_id="alice",
        scopes=SCOPES,
    )
    assert recorded["ok"] and health["ok"] and health["result"]["providers"]["fixture"]["state"] == "healthy"
    conn.close()
