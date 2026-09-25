"""Bounded market-operations receipts for SLOs, budgets, and recovery drills.

The operations layer deliberately records measurements and recovery evidence; it
does not pretend that fixture measurements are production telemetry.  Provider
and deployment adapters can submit normalized observations without gaining a
second source-of-truth for market data.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import time
from typing import Any

OPERATIONS_READ_SCOPE = "market:operations:read"
OPERATIONS_WRITE_SCOPE = "market:operations:write"
OPERATIONS_EXECUTE_SCOPE = "market:operations:execute"

MAX_ROWS = 1000
MAX_JSON_BYTES = 2_000_000
METRICS = frozenset(
    {
        "freshness_ms",
        "coverage_ratio",
        "reconciliation_error",
        "query_latency_ms",
        "job_success_ratio",
        "notification_delivery_ratio",
        "throughput_rows_per_second",
        "cost_micros",
        "bytes_processed",
    }
)
SCENARIOS = frozenset(
    {"source_outage", "partial_corruption", "interrupted_backfill", "restore"}
)

_DDL = """
CREATE TABLE IF NOT EXISTS market_ops_measurements(
 namespace TEXT NOT NULL, measurement_id TEXT NOT NULL, owner TEXT NOT NULL,
 metric TEXT NOT NULL, provider TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
 value DOUBLE NOT NULL, target_json TEXT NOT NULL, dimensions_json TEXT NOT NULL,
 source_revision_ids_json TEXT NOT NULL, status TEXT NOT NULL,
 record_hash TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, measurement_id));
CREATE TABLE IF NOT EXISTS market_ops_budgets(
 namespace TEXT NOT NULL, budget_id TEXT NOT NULL, owner TEXT NOT NULL,
 period_start_ms BIGINT NOT NULL, period_end_ms BIGINT NOT NULL,
 max_requests BIGINT NOT NULL, max_cost_micros BIGINT NOT NULL,
 used_requests BIGINT NOT NULL, used_cost_micros BIGINT NOT NULL,
 status TEXT NOT NULL, record_hash TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, budget_id));
CREATE TABLE IF NOT EXISTS market_ops_budget_ledger(
 namespace TEXT NOT NULL, charge_id TEXT NOT NULL, budget_id TEXT NOT NULL,
 requests BIGINT NOT NULL, cost_micros BIGINT NOT NULL, charged_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, charge_id));
CREATE TABLE IF NOT EXISTS market_ops_recovery_drills(
 namespace TEXT NOT NULL, drill_id TEXT NOT NULL, owner TEXT NOT NULL,
 scenario TEXT NOT NULL, failure_injected BOOLEAN NOT NULL,
 expected_steps_json TEXT NOT NULL, observed_steps_json TEXT NOT NULL,
 status TEXT NOT NULL, restored BOOLEAN NOT NULL, duration_ms BIGINT NOT NULL,
 replay_hash TEXT NOT NULL, record_hash TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, drill_id));
CREATE TABLE IF NOT EXISTS market_ops_backups(
 namespace TEXT NOT NULL, backup_id TEXT NOT NULL, owner TEXT NOT NULL,
 manifest_json TEXT NOT NULL, content_json TEXT NOT NULL, content_hash TEXT NOT NULL,
 status TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, backup_id));
CREATE TABLE IF NOT EXISTS market_ops_runbooks(
 namespace TEXT NOT NULL, runbook_id TEXT NOT NULL, owner TEXT NOT NULL,
 scenario TEXT NOT NULL, steps_json TEXT NOT NULL, revision BIGINT NOT NULL,
 record_hash TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, runbook_id, revision));
CREATE TABLE IF NOT EXISTS market_ops_repairs(
 namespace TEXT NOT NULL, repair_id TEXT NOT NULL, owner TEXT NOT NULL,
 runbook_id TEXT NOT NULL, status TEXT NOT NULL, input_json TEXT NOT NULL,
 outcome_json TEXT NOT NULL, record_hash TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, repair_id));
CREATE INDEX IF NOT EXISTS idx_market_ops_measurements_metric
 ON market_ops_measurements(namespace, metric, provider, observed_at_ms);
"""


class MarketOperationsError(ValueError):
    """Typed market operations failure."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def ensure_market_operations_schema(conn: Any) -> None:
    conn.execute(_DDL)


def _canonical(value: Any) -> str:
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise MarketOperationsError("invalid_request", "operations input must be finite JSON") from exc
    if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
        raise MarketOperationsError("bound_exceeded", "operations payload exceeds 2 MiB")
    return encoded


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, name: str, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MarketOperationsError("invalid_request", f"{name} must be bounded text")
    return value.strip()


def _int(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise MarketOperationsError("invalid_request", f"{name} must be an integer >= {minimum}")
    return value


def _number(value: Any, name: str, *, minimum: float | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise MarketOperationsError("invalid_request", f"{name} must be finite") from exc
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        raise MarketOperationsError("invalid_request", f"{name} must be finite and >= {minimum}")
    return number


def _rows(value: Any, name: str, maximum: int = MAX_ROWS) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) > maximum:
        raise MarketOperationsError("bound_exceeded", f"{name} must contain at most {maximum} rows")
    result = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise MarketOperationsError("invalid_request", f"{name}[{index}] must be an object")
        result.append(dict(row))
    return result


def _authorize(namespace: str, principal_id: str, scopes: set[str], *, write: bool = False, execute: bool = False) -> None:
    _text(namespace, "namespace", 100)
    _text(principal_id, "principal_id", 200)
    if "operator" in scopes:
        return
    required = OPERATIONS_EXECUTE_SCOPE if execute else OPERATIONS_WRITE_SCOPE if write else OPERATIONS_READ_SCOPE
    if required not in scopes or f"namespace:{namespace}:{'write' if write or execute else 'read'}" not in scopes:
        raise MarketOperationsError("unauthorized", "market operations and namespace access is required")


def _owner(owner: str | None, principal_id: str, scopes: set[str]) -> str:
    actual = principal_id if owner is None else _text(owner, "owner", 200)
    if actual != principal_id and "operator" not in scopes:
        raise MarketOperationsError("unauthorized", "operations receipt belongs to another principal")
    return actual


def _assert_existing_owner(existing: str, owner: str, scopes: set[str]) -> None:
    if existing != owner and "operator" not in scopes:
        raise MarketOperationsError("unauthorized", "operations receipt belongs to another principal")


def _percentile(values: list[float], percentile: float) -> float:
    values = sorted(values)
    if not values:
        raise MarketOperationsError("not_found", "no measurements are available")
    index = min(len(values) - 1, max(0, math.ceil(percentile * len(values)) - 1))
    return values[index]


class MarketOperationsStore:
    """Persist normalized telemetry and operator evidence with explicit limits."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            ensure_market_operations_schema(conn)

    def record_measurements(
        self,
        namespace: str,
        *,
        measurements: Sequence[Mapping[str, Any]],
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        rows = _rows(measurements, "measurements")
        accepted = []
        for index, raw in enumerate(rows):
            item = dict(raw)
            measurement_id = _text(item.get("measurement_id"), f"measurements[{index}].measurement_id", 200)
            metric = _text(item.get("metric"), f"measurements[{index}].metric", 80)
            if metric not in METRICS:
                raise MarketOperationsError("invalid_request", f"unsupported metric: {metric}")
            provider = _text(item.get("provider", "local-fixture"), f"measurements[{index}].provider", 200)
            observed_at_ms = _int(item.get("observed_at_ms"), f"measurements[{index}].observed_at_ms")
            minimum = None if metric == "reconciliation_error" else 0.0
            value = _number(item.get("value"), f"measurements[{index}].value", minimum=minimum)
            status = _text(item.get("status", "observed"), f"measurements[{index}].status", 30)
            if status not in {"observed", "degraded", "unavailable"}:
                raise MarketOperationsError("invalid_request", "measurement status is unsupported")
            target = item.get("target", {})
            dimensions = item.get("dimensions", {})
            source_revision_ids = item.get("source_revision_ids", [])
            if not isinstance(target, Mapping) or not isinstance(dimensions, Mapping):
                raise MarketOperationsError("invalid_request", "target and dimensions must be objects")
            if not isinstance(source_revision_ids, list) or any(not isinstance(value, str) for value in source_revision_ids):
                raise MarketOperationsError("invalid_request", "source_revision_ids must be text")
            normalized = {
                "measurement_id": measurement_id,
                "metric": metric,
                "provider": provider,
                "observed_at_ms": observed_at_ms,
                "value": value,
                "target": dict(target),
                "dimensions": dict(dimensions),
                "source_revision_ids": sorted(set(source_revision_ids)),
                "status": status,
            }
            record_hash = _digest(normalized)
            prior = self.conn.execute(
                "SELECT owner,record_hash FROM market_ops_measurements WHERE namespace=? AND measurement_id=?",
                [namespace, measurement_id],
            ).fetchone()
            if prior:
                _assert_existing_owner(str(prior[0]), owner, scopes)
                if prior[1] != record_hash:
                    raise MarketOperationsError("revision_conflict", "measurement identity is immutable")
                accepted.append({**normalized, "record_hash": record_hash, "idempotent": True})
                continue
            recorded_at_ms = _int(self.now(), "recorded_at_ms")
            self.conn.execute(
                "INSERT INTO market_ops_measurements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, measurement_id, owner, metric, provider, observed_at_ms, value, _canonical(dict(target)), _canonical(dict(dimensions)), _canonical(sorted(set(source_revision_ids))), status, record_hash, recorded_at_ms],
            )
            accepted.append({**normalized, "record_hash": record_hash, "recorded_at_ms": recorded_at_ms, "idempotent": False})
        return {"contract": "noesis-market-operations-measurement-v1", "namespace": namespace, "owner": owner, "measurements": accepted, "count": len(accepted)}

    def evaluate_slos(
        self,
        namespace: str,
        *,
        targets: Mapping[str, Mapping[str, Any]],
        window_start_ms: int,
        window_end_ms: int,
        provider: str | None,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=False)
        owner = _owner(owner, principal_id, scopes)
        start, end = _int(window_start_ms, "window_start_ms"), _int(window_end_ms, "window_end_ms")
        if end < start:
            raise MarketOperationsError("invalid_request", "window_end_ms precedes window_start_ms")
        if not isinstance(targets, Mapping) or not targets:
            raise MarketOperationsError("invalid_request", "targets must be a nonempty object")
        results = {}
        for metric, raw_target in targets.items():
            if metric not in METRICS or not isinstance(raw_target, Mapping):
                raise MarketOperationsError("invalid_request", "targets must map known metrics to objects")
            target = _number(raw_target.get("value", raw_target.get("target")), f"targets.{metric}.value")
            direction = _text(raw_target.get("direction", "max"), f"targets.{metric}.direction", 10)
            if direction not in {"min", "max"}:
                raise MarketOperationsError("invalid_request", "SLO direction must be min or max")
            params: list[Any] = [namespace, metric, start, end]
            query = "SELECT value,status,provider,observed_at_ms FROM market_ops_measurements WHERE namespace=? AND metric=? AND observed_at_ms BETWEEN ? AND ?"
            if provider is not None:
                query += " AND provider=?"
                params.append(_text(provider, "provider", 200))
            rows = self.conn.execute(query, params).fetchall()
            values = [float(row[0]) for row in rows]
            statuses = {str(row[1]) for row in rows}
            if not values:
                results[metric] = {"state": "no_data", "target": target, "direction": direction, "count": 0}
                continue
            aggregation = _text(raw_target.get("aggregation", "p95" if metric in {"freshness_ms", "query_latency_ms", "cost_micros"} else "min" if metric.endswith("ratio") else "max"), f"targets.{metric}.aggregation", 10)
            if aggregation == "p95":
                observed = _percentile(values, 0.95)
            elif aggregation == "min":
                observed = min(values)
            elif aggregation == "max":
                observed = max(values)
            elif aggregation == "avg":
                observed = sum(values) / len(values)
            else:
                raise MarketOperationsError("invalid_request", "unsupported SLO aggregation")
            passed = observed >= target if direction == "min" else observed <= target
            results[metric] = {"state": "pass" if passed and not statuses.intersection({"degraded", "unavailable"}) else "degraded" if passed else "fail", "target": target, "direction": direction, "aggregation": aggregation, "observed": observed, "count": len(values), "statuses": sorted(statuses)}
        return {"contract": "noesis-market-slo-report-v1", "namespace": namespace, "owner": owner, "window": {"start_ms": start, "end_ms": end}, "provider": provider, "slos": results, "limitations": ["SLO results describe submitted observations; missing production telemetry is not treated as healthy."]}

    def provider_health(
        self,
        namespace: str,
        *,
        provider: str | None,
        window_start_ms: int,
        window_end_ms: int,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=False)
        start, end = _int(window_start_ms, "window_start_ms"), _int(window_end_ms, "window_end_ms")
        params: list[Any] = [namespace, start, end]
        query = "SELECT provider,metric,status,value,observed_at_ms FROM market_ops_measurements WHERE namespace=? AND observed_at_ms BETWEEN ? AND ?"
        if provider is not None:
            query += " AND provider=?"
            params.append(_text(provider, "provider", 200))
        rows = self.conn.execute(query, params).fetchall()
        grouped: dict[str, list[tuple[Any, ...]]] = {}
        for row in rows:
            grouped.setdefault(str(row[0]), []).append(row[1:])
        providers = {}
        for name, entries in grouped.items():
            statuses = [str(entry[1]) for entry in entries]
            providers[name] = {"state": "unavailable" if all(value == "unavailable" for value in statuses) else "degraded" if "degraded" in statuses or "unavailable" in statuses else "healthy", "measurement_count": len(entries), "metrics": sorted({str(entry[0]) for entry in entries}), "last_observed_at_ms": max(int(entry[3]) for entry in entries), "unavailable_count": statuses.count("unavailable"), "degraded_count": statuses.count("degraded")}
        return {"contract": "noesis-market-provider-health-v1", "namespace": namespace, "window": {"start_ms": start, "end_ms": end}, "providers": providers, "limitations": ["Provider health is based on retained normalized observations and does not probe a provider implicitly."]}

    def save_budget(
        self,
        namespace: str,
        *,
        budget_id: str,
        period_start_ms: int,
        period_end_ms: int,
        max_requests: int,
        max_cost_micros: int,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        budget_id = _text(budget_id, "budget_id", 200)
        start, end = _int(period_start_ms, "period_start_ms"), _int(period_end_ms, "period_end_ms")
        if end <= start:
            raise MarketOperationsError("invalid_request", "budget period must be positive")
        max_requests, max_cost_micros = _int(max_requests, "max_requests"), _int(max_cost_micros, "max_cost_micros")
        prior = self.conn.execute("SELECT owner,used_requests,used_cost_micros FROM market_ops_budgets WHERE namespace=? AND budget_id=?", [namespace, budget_id]).fetchone()
        if prior:
            _assert_existing_owner(str(prior[0]), owner, scopes)
        used_requests, used_cost = (int(prior[1]), int(prior[2])) if prior else (0, 0)
        body = {"budget_id": budget_id, "period_start_ms": start, "period_end_ms": end, "max_requests": max_requests, "max_cost_micros": max_cost_micros, "used_requests": used_requests, "used_cost_micros": used_cost}
        record_hash = _digest(body)
        if prior:
            existing = self.conn.execute("SELECT record_hash FROM market_ops_budgets WHERE namespace=? AND budget_id=?", [namespace, budget_id]).fetchone()
            if existing[0] != record_hash:
                raise MarketOperationsError("revision_conflict", "budget identity is immutable for the current period")
            return {"contract": "noesis-market-budget-v1", "namespace": namespace, "owner": owner, **body, "status": "active", "record_hash": record_hash, "idempotent": True}
        recorded = _int(self.now(), "recorded_at_ms")
        self.conn.execute("INSERT INTO market_ops_budgets VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [namespace, budget_id, owner, start, end, max_requests, max_cost_micros, 0, 0, "active", record_hash, recorded])
        return {"contract": "noesis-market-budget-v1", "namespace": namespace, "owner": owner, **body, "status": "active", "record_hash": record_hash, "recorded_at_ms": recorded, "idempotent": False}

    def consume_budget(
        self,
        namespace: str,
        *,
        budget_id: str,
        charge_id: str,
        requests: int,
        cost_micros: int,
        at_ms: int,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, execute=True)
        budget_id, charge_id = _text(budget_id, "budget_id", 200), _text(charge_id, "charge_id", 200)
        requests, cost_micros, at_ms = _int(requests, "requests"), _int(cost_micros, "cost_micros"), _int(at_ms, "at_ms")
        row = self.conn.execute("SELECT owner,period_start_ms,period_end_ms,max_requests,max_cost_micros,used_requests,used_cost_micros,status FROM market_ops_budgets WHERE namespace=? AND budget_id=?", [namespace, budget_id]).fetchone()
        if row is None:
            raise MarketOperationsError("not_found", "budget is unavailable")
        _assert_existing_owner(str(row[0]), principal_id, scopes)
        if not (int(row[1]) <= at_ms < int(row[2])) or row[7] != "active":
            raise MarketOperationsError("budget_exhausted", "budget is outside its active period")
        prior = self.conn.execute("SELECT requests,cost_micros FROM market_ops_budget_ledger WHERE namespace=? AND charge_id=?", [namespace, charge_id]).fetchone()
        if prior:
            if int(prior[0]) != requests or int(prior[1]) != cost_micros:
                raise MarketOperationsError("revision_conflict", "charge identity has different usage")
            return self._budget_result(namespace, budget_id, "idempotent")
        if int(row[5]) + requests > int(row[3]) or int(row[6]) + cost_micros > int(row[4]):
            raise MarketOperationsError("budget_exhausted", "request or cost budget would be exceeded")
        self.conn.execute("INSERT INTO market_ops_budget_ledger VALUES (?,?,?,?,?,?)", [namespace, charge_id, budget_id, requests, cost_micros, at_ms])
        self.conn.execute("UPDATE market_ops_budgets SET used_requests=used_requests+?,used_cost_micros=used_cost_micros+? WHERE namespace=? AND budget_id=?", [requests, cost_micros, namespace, budget_id])
        return self._budget_result(namespace, budget_id, "charged")

    def _budget_result(self, namespace: str, budget_id: str, charge_status: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT owner,period_start_ms,period_end_ms,max_requests,max_cost_micros,used_requests,used_cost_micros,status,record_hash FROM market_ops_budgets WHERE namespace=? AND budget_id=?", [namespace, budget_id]).fetchone()
        return {"contract": "noesis-market-budget-v1", "namespace": namespace, "budget_id": budget_id, "owner": row[0], "period_start_ms": int(row[1]), "period_end_ms": int(row[2]), "max_requests": int(row[3]), "max_cost_micros": int(row[4]), "used_requests": int(row[5]), "used_cost_micros": int(row[6]), "status": row[7], "record_hash": row[8], "charge_status": charge_status}

    def record_recovery_drill(
        self,
        namespace: str,
        *,
        drill_id: str,
        scenario: str,
        failure_injected: bool,
        expected_steps: Sequence[Mapping[str, Any]],
        observed_steps: Sequence[Mapping[str, Any]],
        status: str,
        restored: bool,
        duration_ms: int,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        drill_id, scenario, status = _text(drill_id, "drill_id", 200), _text(scenario, "scenario", 40), _text(status, "status", 20)
        if scenario not in SCENARIOS or status not in {"passed", "failed", "blocked"}:
            raise MarketOperationsError("invalid_request", "unsupported recovery scenario or status")
        expected, observed = _rows(expected_steps, "expected_steps", 100), _rows(observed_steps, "observed_steps", 100)
        duration_ms = _int(duration_ms, "duration_ms")
        body = {"scenario": scenario, "failure_injected": bool(failure_injected), "expected_steps": expected, "observed_steps": observed, "status": status, "restored": bool(restored), "duration_ms": duration_ms}
        replay_hash = _digest(body)
        record_hash = _digest({"drill_id": drill_id, **body})
        prior = self.conn.execute("SELECT owner,record_hash FROM market_ops_recovery_drills WHERE namespace=? AND drill_id=?", [namespace, drill_id]).fetchone()
        if prior:
            _assert_existing_owner(str(prior[0]), owner, scopes)
            if prior[1] != record_hash:
                raise MarketOperationsError("revision_conflict", "recovery drill identity is immutable")
            return {"contract": "noesis-market-recovery-drill-v1", "namespace": namespace, "drill_id": drill_id, "owner": owner, **body, "replay_hash": replay_hash, "record_hash": record_hash, "idempotent": True}
        recorded = _int(self.now(), "recorded_at_ms")
        self.conn.execute("INSERT INTO market_ops_recovery_drills VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [namespace, drill_id, owner, scenario, bool(failure_injected), _canonical(expected), _canonical(observed), status, bool(restored), duration_ms, replay_hash, record_hash, recorded])
        return {"contract": "noesis-market-recovery-drill-v1", "namespace": namespace, "drill_id": drill_id, "owner": owner, **body, "replay_hash": replay_hash, "record_hash": record_hash, "recorded_at_ms": recorded, "idempotent": False}

    def create_backup(
        self,
        namespace: str,
        *,
        backup_id: str,
        manifest: Mapping[str, Any],
        content: Mapping[str, Any],
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        backup_id = _text(backup_id, "backup_id", 200)
        if not isinstance(manifest, Mapping) or not isinstance(content, Mapping):
            raise MarketOperationsError("invalid_request", "backup manifest and content must be objects")
        manifest_json, content_json = _canonical(dict(manifest)), _canonical(dict(content))
        content_hash = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
        prior = self.conn.execute("SELECT owner,content_hash,manifest_json FROM market_ops_backups WHERE namespace=? AND backup_id=?", [namespace, backup_id]).fetchone()
        if prior:
            _assert_existing_owner(str(prior[0]), owner, scopes)
            if prior[1] != content_hash or prior[2] != manifest_json:
                raise MarketOperationsError("revision_conflict", "backup identity is immutable")
            return {"contract": "noesis-market-backup-v1", "namespace": namespace, "backup_id": backup_id, "owner": owner, "manifest": dict(manifest), "content_hash": content_hash, "status": "available", "idempotent": True}
        recorded = _int(self.now(), "recorded_at_ms")
        self.conn.execute("INSERT INTO market_ops_backups VALUES (?,?,?,?,?,?,?,?)", [namespace, backup_id, owner, manifest_json, content_json, content_hash, "available", recorded])
        return {"contract": "noesis-market-backup-v1", "namespace": namespace, "backup_id": backup_id, "owner": owner, "manifest": dict(manifest), "content_hash": content_hash, "status": "available", "recorded_at_ms": recorded, "idempotent": False}

    def restore_backup(
        self,
        namespace: str,
        *,
        backup_id: str,
        expected_content_hash: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, execute=True)
        row = self.conn.execute("SELECT owner,manifest_json,content_json,content_hash,status,recorded_at_ms FROM market_ops_backups WHERE namespace=? AND backup_id=?", [namespace, _text(backup_id, "backup_id", 200)]).fetchone()
        if row is None:
            raise MarketOperationsError("not_found", "backup is unavailable")
        _assert_existing_owner(str(row[0]), principal_id, scopes)
        if expected_content_hash is not None and _text(expected_content_hash, "expected_content_hash", 64) != row[3]:
            raise MarketOperationsError("integrity_failure", "backup content hash does not match")
        actual = hashlib.sha256(str(row[2]).encode("utf-8")).hexdigest()
        if actual != row[3]:
            raise MarketOperationsError("integrity_failure", "backup content is corrupted")
        return {"contract": "noesis-market-backup-restore-v1", "namespace": namespace, "backup_id": backup_id, "owner": row[0], "manifest": json.loads(row[1]), "content": json.loads(row[2]), "content_hash": row[3], "status": "restored", "recorded_at_ms": int(row[5]), "limitations": ["This operation restores the retained operations payload; deployment-specific database restore remains an operator runbook step."]}

    def prune_audit(
        self,
        namespace: str,
        *,
        before_ms: int,
        dry_run: bool,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Preview or execute bounded retention of operations audit rows.

        Backups, budgets, runbooks, and repair receipts are retained as operator
        evidence. High-volume telemetry and charge ledger rows are the only
        records eligible for this explicit, cutoff-based cleanup.
        """
        _authorize(namespace, principal_id, scopes, execute=True)
        before_ms = _int(before_ms, "before_ms")
        counts = {}
        tables = {
            "measurements": ("market_ops_measurements", "recorded_at_ms"),
            "budget_ledger": ("market_ops_budget_ledger", "charged_at_ms"),
        }
        for name, (table, clock) in tables.items():
            counts[name] = int(self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE namespace=? AND {clock}<?", [namespace, before_ms]).fetchone()[0])
        deleted = 0
        if not dry_run:
            for table, clock in tables.values():
                deleted += len(self.conn.execute(f"DELETE FROM {table} WHERE namespace=? AND {clock}<? RETURNING 1", [namespace, before_ms]).fetchall())
        return {"contract": "noesis-market-audit-prune-v1", "namespace": namespace, "before_ms": before_ms, "dry_run": bool(dry_run), "eligible": counts, "deleted": deleted, "retained_evidence": ["recovery_drills", "backups", "budgets", "runbooks", "repairs"]}

    def save_runbook(
        self,
        namespace: str,
        *,
        runbook_id: str,
        revision: int,
        scenario: str,
        steps: Sequence[Mapping[str, Any]],
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        runbook_id, scenario = _text(runbook_id, "runbook_id", 200), _text(scenario, "scenario", 40)
        if scenario not in SCENARIOS:
            raise MarketOperationsError("invalid_request", "unsupported runbook scenario")
        revision = _int(revision, "revision", minimum=1)
        normalized = _rows(steps, "steps", 100)
        body = {"runbook_id": runbook_id, "revision": revision, "scenario": scenario, "steps": normalized}
        record_hash = _digest(body)
        prior = self.conn.execute("SELECT owner,record_hash FROM market_ops_runbooks WHERE namespace=? AND runbook_id=? AND revision=?", [namespace, runbook_id, revision]).fetchone()
        if prior:
            _assert_existing_owner(str(prior[0]), owner, scopes)
            if prior[1] != record_hash:
                raise MarketOperationsError("revision_conflict", "runbook revision is immutable")
            return {"contract": "noesis-market-runbook-v1", "namespace": namespace, "owner": owner, **body, "record_hash": record_hash, "idempotent": True}
        recorded = _int(self.now(), "recorded_at_ms")
        self.conn.execute("INSERT INTO market_ops_runbooks VALUES (?,?,?,?,?,?,?,?)", [namespace, runbook_id, owner, scenario, _canonical(normalized), revision, record_hash, recorded])
        return {"contract": "noesis-market-runbook-v1", "namespace": namespace, "owner": owner, **body, "record_hash": record_hash, "recorded_at_ms": recorded, "idempotent": False}

    def record_repair(
        self,
        namespace: str,
        *,
        repair_id: str,
        runbook_id: str,
        status: str,
        inputs: Mapping[str, Any],
        outcome: Mapping[str, Any],
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner = _owner(owner, principal_id, scopes)
        repair_id, runbook_id, status = _text(repair_id, "repair_id", 200), _text(runbook_id, "runbook_id", 200), _text(status, "status", 20)
        if status not in {"started", "passed", "failed", "blocked"} or not isinstance(inputs, Mapping) or not isinstance(outcome, Mapping):
            raise MarketOperationsError("invalid_request", "invalid repair status or payload")
        body = {"repair_id": repair_id, "runbook_id": runbook_id, "status": status, "inputs": dict(inputs), "outcome": dict(outcome)}
        record_hash = _digest(body)
        prior = self.conn.execute("SELECT owner,record_hash FROM market_ops_repairs WHERE namespace=? AND repair_id=?", [namespace, repair_id]).fetchone()
        if prior:
            _assert_existing_owner(str(prior[0]), owner, scopes)
            if prior[1] != record_hash:
                raise MarketOperationsError("revision_conflict", "repair identity is immutable")
            return {"contract": "noesis-market-repair-v1", "namespace": namespace, "owner": owner, **body, "record_hash": record_hash, "idempotent": True}
        recorded = _int(self.now(), "recorded_at_ms")
        self.conn.execute("INSERT INTO market_ops_repairs VALUES (?,?,?,?,?,?,?,?,?)", [namespace, repair_id, owner, runbook_id, status, _canonical(dict(inputs)), _canonical(dict(outcome)), record_hash, recorded])
        return {"contract": "noesis-market-repair-v1", "namespace": namespace, "owner": owner, **body, "record_hash": record_hash, "recorded_at_ms": recorded, "idempotent": False}


__all__ = [
    "MarketOperationsError",
    "MarketOperationsStore",
    "OPERATIONS_EXECUTE_SCOPE",
    "OPERATIONS_READ_SCOPE",
    "OPERATIONS_WRITE_SCOPE",
    "ensure_market_operations_schema",
]
