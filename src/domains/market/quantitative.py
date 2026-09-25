"""Bounded, manifest-driven quantitative market research calculations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import InvalidOperation
import hashlib
import json
import math
import statistics
import time
from typing import Any

from src.kb.quantitative import CALCULATE_SCOPE, READ_SCOPE
from src.domains.market.entitlements import recheck_stored_receipt_rights, withheld_receipt

QUANT_READ_SCOPE = READ_SCOPE
QUANT_CALCULATE_SCOPE = CALCULATE_SCOPE
EVENT_FORMULA_VERSION = "noesis-market-event-study-v1"
FACTOR_FORMULA_VERSION = "noesis-market-factor-analysis-v1"
BACKTEST_FORMULA_VERSION = "noesis-market-backtest-v1"
WALK_FORWARD_FORMULA_VERSION = "noesis-market-walk-forward-v1"
PORTFOLIO_FORMULA_VERSION = "noesis-market-portfolio-v1"
RISK_FORMULA_VERSION = "noesis-market-risk-report-v1"
MAX_ROWS = 20_000
MAX_EVENTS = 500
MAX_FACTORS = 32
MAX_POSITIONS = 500

_DDL = """
CREATE TABLE IF NOT EXISTS market_quantitative_runs(
 namespace TEXT NOT NULL, run_id TEXT NOT NULL, owner TEXT NOT NULL,
 kind TEXT NOT NULL, input_hash TEXT NOT NULL, input_json TEXT NOT NULL,
 result_json TEXT NOT NULL, record_hash TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, run_id), UNIQUE(namespace, kind, input_hash));
CREATE TABLE IF NOT EXISTS market_portfolio_revisions(
 namespace TEXT NOT NULL, portfolio_id TEXT NOT NULL, version BIGINT NOT NULL,
 owner TEXT NOT NULL, input_json TEXT NOT NULL, result_json TEXT NOT NULL,
 record_hash TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, portfolio_id, version));
CREATE TABLE IF NOT EXISTS market_portfolio_audit(
 audit_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, portfolio_id TEXT NOT NULL,
 version BIGINT NOT NULL, owner TEXT NOT NULL, operation TEXT NOT NULL,
 recorded_at_ms BIGINT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_market_quant_runs_owner
 ON market_quantitative_runs(namespace, owner, kind, recorded_at_ms);
"""


class MarketQuantitativeError(ValueError):
    """Typed quantitative request or calculation failure."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def ensure_market_quantitative_schema(conn: Any) -> None:
    conn.execute(_DDL)


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise MarketQuantitativeError("invalid_request", "quantitative input must be JSON-safe") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, name: str, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MarketQuantitativeError("invalid_request", f"{name} must be bounded text")
    return value.strip()


def _millis(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise MarketQuantitativeError("invalid_request", f"{name} must be a nonnegative epoch millisecond")
    return value


def _number(value: Any, name: str = "value") -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise MarketQuantitativeError("invalid_request", f"{name} must be finite") from exc
    if not math.isfinite(result):
        raise MarketQuantitativeError("invalid_request", f"{name} must be finite")
    return result


def _bounded_rows(value: Any, name: str, maximum: int = MAX_ROWS) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) > maximum:
        raise MarketQuantitativeError("bound_exceeded", f"{name} must contain at most {maximum} rows")
    result = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise MarketQuantitativeError("invalid_request", f"{name}[{index}] must be an object")
        result.append(dict(row))
    return result


def _authorize(namespace: str, principal_id: str, scopes: set[str], *, calculate: bool = False) -> None:
    _text(namespace, "namespace", 100)
    _text(principal_id, "principal_id", 200)
    if "operator" in scopes:
        return
    required = QUANT_CALCULATE_SCOPE if calculate else QUANT_READ_SCOPE
    if required not in scopes:
        raise MarketQuantitativeError("unauthorized", f"{required} scope is required")


def _check_owner(owner: str, principal_id: str, scopes: set[str]) -> str:
    owner = _text(owner, "owner", 200)
    if owner != principal_id and "operator" not in scopes:
        raise MarketQuantitativeError("unauthorized", "quantitative result belongs to another principal")
    return owner


def _returns(bars: Sequence[Mapping[str, Any]], name: str = "bars") -> list[dict[str, Any]]:
    rows = []
    prior = None
    for row in sorted(bars, key=lambda item: (int(item.get("bar_start_ms", 0)), str(item.get("revision_id", "")))):
        at = _millis(row.get("bar_start_ms"), f"{name}.bar_start_ms")
        close = _number(row.get("close"), f"{name}.close")
        if close <= 0:
            raise MarketQuantitativeError("invalid_request", f"{name}.close must be positive")
        if prior is not None:
            rows.append({
                "at_ms": at,
                "return": close / prior["close"] - 1.0,
                "revision_id": row.get("revision_id"),
                "listing_id": row.get("listing_id"),
            })
        prior = {"at_ms": at, "close": close}
    return rows


def _mean_std(values: Sequence[float]) -> tuple[float | None, float | None]:
    if not values:
        return None, None
    mean = statistics.mean(values)
    return mean, statistics.stdev(values) if len(values) > 1 else 0.0


def _normal_interval(value: float | None, spread: float | None, sample: int, alpha: float) -> dict[str, float | None]:
    if value is None or spread is None or sample < 2:
        return {"low": None, "high": None, "alpha": alpha}
    # A bounded normal approximation is explicit; this is not a guarantee.
    margin = 1.96 * spread / math.sqrt(sample)
    return {"low": value - margin, "high": value + margin, "alpha": alpha}


def _solve_linear(design: list[list[float]], target: list[float]) -> tuple[list[float], float]:
    if not design or len(design) != len(target):
        raise MarketQuantitativeError("insufficient_history", "linear model has no observations")
    columns = len(design[0])
    if any(len(row) != columns for row in design):
        raise MarketQuantitativeError("invalid_request", "linear design rows have inconsistent dimensions")
    matrix = [[sum(row[i] * row[j] for row in design) for j in range(columns)] + [sum(row[i] * y for row, y in zip(design, target, strict=True))] for i in range(columns)]
    for pivot in range(columns):
        pivot_row = max(range(pivot, columns), key=lambda row: abs(matrix[row][pivot]))
        if abs(matrix[pivot_row][pivot]) < 1e-12:
            raise MarketQuantitativeError("collinear_factors", "factor design is singular or collinear")
        matrix[pivot], matrix[pivot_row] = matrix[pivot_row], matrix[pivot]
        scale = matrix[pivot][pivot]
        matrix[pivot] = [value / scale for value in matrix[pivot]]
        for row in range(columns):
            if row == pivot:
                continue
            scale = matrix[row][pivot]
            if scale:
                matrix[row] = [left - scale * right for left, right in zip(matrix[row], matrix[pivot], strict=True)]
    coefficients = [matrix[row][-1] for row in range(columns)]
    residuals = [y - sum(coef * x for coef, x in zip(coefficients, [1.0, *features], strict=True)) for features, y in zip([row[1:] for row in design], target, strict=True)]
    return coefficients, statistics.pstdev(residuals) if len(residuals) > 1 else 0.0


class MarketQuantitativeStore:
    """Persist deterministic quantitative receipts with immutable input manifests."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            ensure_market_quantitative_schema(conn)

    def _persist(self, namespace: str, kind: str, owner: str, input_payload: Mapping[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        input_hash = _digest(input_payload)
        run_id = f"market-{kind}-run:" + _digest([namespace, kind, input_hash])[:32]
        prior = self.conn.execute(
            "SELECT result_json,record_hash,recorded_at_ms FROM market_quantitative_runs WHERE namespace=? AND run_id=?",
            [namespace, run_id],
        ).fetchone()
        if prior:
            return {**json.loads(prior[0]), "run_id": run_id, "record_hash": prior[1], "recorded_at_ms": int(prior[2]), "idempotent": True}
        result = {
            **result,
            "run_id": run_id,
            "owner": owner,
            "input_manifest": {"input_hash": input_hash, "kind": kind, "formula_version": result.get("formula_version")},
        }
        encoded = _canonical(result)
        record_hash = _digest(result)
        recorded = _millis(self.now(), "recorded_at_ms")
        self.conn.execute(
            "INSERT INTO market_quantitative_runs VALUES (?,?,?,?,?,?,?,?,?)",
            [namespace, run_id, owner, kind, input_hash, _canonical(input_payload), encoded, record_hash, recorded],
        )
        return {**result, "record_hash": record_hash, "recorded_at_ms": recorded, "idempotent": False}

    def inspect_run(self, namespace: str, run_id: str, *, principal_id: str, scopes: set[str], operation: str = "derive") -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes)
        row = self.conn.execute(
            "SELECT owner,result_json,record_hash,recorded_at_ms FROM market_quantitative_runs WHERE namespace=? AND run_id=?",
            [namespace, _text(run_id, "run_id", 200)],
        ).fetchone()
        if row is None or (row[0] != principal_id and "operator" not in scopes):
            raise MarketQuantitativeError("not_found", "quantitative run is unavailable")
        result = json.loads(row[1])
        rights = recheck_stored_receipt_rights(
            self.conn, namespace, result, operation=operation,
            principal_id=principal_id, scopes=scopes, now_ms=int(self.now()),
        )
        if rights["state"] == "withheld":
            return withheld_receipt(
                f"noesis-market-{result.get('kind', 'quantitative')}-v1", "quantitative_run", run_id, row[2], rights
            )
        return {**result, "record_hash": row[2], "recorded_at_ms": int(row[3]), "current_rights": rights}

    def export_run(self, namespace: str, run_id: str, *, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        result = self.inspect_run(namespace, run_id, principal_id=principal_id, scopes=scopes, operation="export")
        if result.get("withheld"):
            return {**result, "contract": "noesis-market-quantitative-export-v1", "artifact": None}
        return {
            "contract": "noesis-market-quantitative-export-v1",
            "namespace": namespace,
            "run_id": run_id,
            "record_hash": result.get("record_hash"),
            "formula_version": result.get("formula_version"),
            "input_manifest": result.get("input_manifest"),
            "artifact": result,
            "replay": {"deterministic": True, "input_snapshot_retained": True},
            "limitations": ["Export contains derived results and retained input manifests; provider source payload rights are checked by the originating domain store."],
        }

    def _source_manifest(self, rows: Sequence[Mapping[str, Any]]) -> list[str]:
        return sorted({
            str(value)
            for row in rows
            for value in row.get("source_revision_ids", [])
            if isinstance(value, str) and value
        })

    def event_study(
        self,
        namespace: str,
        *,
        events: Sequence[Mapping[str, Any]],
        bars: Sequence[Mapping[str, Any]],
        benchmark_bars: Sequence[Mapping[str, Any]] | None,
        estimation_window: int,
        event_window: tuple[int, int],
        alpha: float = 0.05,
        placebo_events: Sequence[Mapping[str, Any]] | None = None,
        benchmark_id: str | None = None,
        owner: str,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, calculate=True)
        owner = _check_owner(owner, principal_id, scopes)
        event_rows = _bounded_rows(events, "events", MAX_EVENTS)
        if not event_rows:
            raise MarketQuantitativeError("invalid_request", "events must not be empty")
        if type(estimation_window) is not int or estimation_window < 2 or estimation_window > 5000:
            raise MarketQuantitativeError("invalid_request", "estimation_window must be between 2 and 5000")
        if not isinstance(event_window, (list, tuple)) or len(event_window) != 2 or not all(type(item) is int for item in event_window):
            raise MarketQuantitativeError("invalid_request", "event_window must contain two integer session offsets")
        event_start, event_end = event_window
        if event_start > event_end or event_end - event_start > 100:
            raise MarketQuantitativeError("invalid_request", "event_window is invalid or too wide")
        alpha = _number(alpha, "alpha")
        if not 0 < alpha < 1:
            raise MarketQuantitativeError("invalid_request", "alpha must be between zero and one")
        asset_returns = _returns(bars, "bars")
        benchmark_returns = _returns(benchmark_bars or [], "benchmark_bars")
        bench_by_at = {row["at_ms"]: row["return"] for row in benchmark_returns}
        results = []
        aligned_events: list[int] = []
        for raw in event_rows:
            event_id = _text(raw.get("event_id"), "event.event_id", 200)
            event_at = _millis(raw.get("event_at_ms"), "event.event_at_ms")
            index = next((idx for idx, row in enumerate(asset_returns) if row["at_ms"] >= event_at), None)
            if index is None or index < estimation_window or index + event_end >= len(asset_returns):
                results.append({"event_id": event_id, "status": "insufficient_history", "sample_size": 0, "source_revision_ids": raw.get("source_revision_ids", [])})
                continue
            aligned_events.append(index)
            estimation = [
                asset_returns[pos]["return"] - bench_by_at.get(asset_returns[pos]["at_ms"], 0.0)
                for pos in range(index - estimation_window, index)
                if asset_returns[pos]["at_ms"] in bench_by_at or benchmark_bars is None
            ]
            expected, spread = _mean_std(estimation)
            window = [
                asset_returns[pos]["return"] - bench_by_at.get(asset_returns[pos]["at_ms"], 0.0) - (expected or 0.0)
                for pos in range(max(0, index + event_start), index + event_end + 1)
                if asset_returns[pos]["at_ms"] in bench_by_at or benchmark_bars is None
            ]
            car = sum(window) if window else None
            results.append({
                "event_id": event_id,
                "status": "compared" if window else "insufficient_history",
                "aligned_event_at_ms": asset_returns[index]["at_ms"],
                "estimation_sample_size": len(estimation),
                "event_sample_size": len(window),
                "expected_abnormal_return": expected,
                "estimation_stddev": spread,
                "abnormal_returns": window,
                "cumulative_abnormal_return": car,
                "interval": _normal_interval(car, spread, len(window), alpha / max(len(event_rows), 1)),
                "source_revision_ids": sorted(set(raw.get("source_revision_ids", [])) | set(self._source_manifest(bars))),
                "confounders": list(raw.get("confounders", []))[:20],
            })
        overlaps = []
        for left in range(len(aligned_events)):
            for right in range(left + 1, len(aligned_events)):
                if abs(aligned_events[left] - aligned_events[right]) <= event_end - event_start:
                    overlaps.append([left, right])
        placebo = []
        for raw in _bounded_rows(placebo_events or [], "placebo_events", MAX_EVENTS):
            event_id = _text(raw.get("event_id"), "placebo.event_id", 200)
            event_at = _millis(raw.get("event_at_ms"), "placebo.event_at_ms")
            index = next((idx for idx, row in enumerate(asset_returns) if row["at_ms"] >= event_at), None)
            if index is None or index < estimation_window or index + event_end >= len(asset_returns):
                placebo.append({"event_id": event_id, "event_at_ms": event_at, "status": "insufficient_history", "cumulative_abnormal_return": None})
                continue
            estimation = [
                asset_returns[pos]["return"] - bench_by_at.get(asset_returns[pos]["at_ms"], 0.0)
                for pos in range(index - estimation_window, index)
                if asset_returns[pos]["at_ms"] in bench_by_at or benchmark_bars is None
            ]
            expected = statistics.mean(estimation) if estimation else 0.0
            window = [
                asset_returns[pos]["return"] - bench_by_at.get(asset_returns[pos]["at_ms"], 0.0) - expected
                for pos in range(max(0, index + event_start), index + event_end + 1)
                if asset_returns[pos]["at_ms"] in bench_by_at or benchmark_bars is None
            ]
            placebo.append({
                "event_id": event_id,
                "event_at_ms": event_at,
                "aligned_event_at_ms": asset_returns[index]["at_ms"],
                "status": "compared" if window else "insufficient_history",
                "sample_size": len(window),
                "cumulative_abnormal_return": sum(window) if window else None,
            })
        payload = {
            "events": event_rows,
            "bars": list(bars),
            "benchmark_bars": list(benchmark_bars or []),
            "estimation_window": estimation_window,
            "event_window": [event_start, event_end],
            "alpha": alpha,
            "benchmark_id": benchmark_id,
            "placebo_events": placebo,
        }
        return self._persist(namespace, "event-study", owner, payload, {
            "contract": "noesis-market-event-study-v1",
            "namespace": namespace,
            "formula_version": EVENT_FORMULA_VERSION,
            "benchmark_id": benchmark_id,
            "estimation_window": estimation_window,
            "event_window": [event_start, event_end],
            "multiple_comparisons": {"method": "bonferroni", "alpha": alpha / max(len(event_rows), 1), "family_size": len(event_rows)},
            "events": results,
            "overlapping_event_pairs": overlaps,
            "placebo_events": placebo,
            "sample_size": sum(1 for item in results if item["status"] == "compared"),
            "limitations": ["Abnormal returns are an association under the selected benchmark and windows, not causal proof.", "Intervals use a normal approximation and expose sparse samples.", "Overlapping events and supplied confounders remain visible rather than silently removed."],
        })

    def factor_analysis(
        self,
        namespace: str,
        *,
        observations: Sequence[Mapping[str, Any]],
        factor_definitions: Mapping[str, Any] | None,
        split_at_ms: int | None,
        owner: str,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, calculate=True)
        owner = _check_owner(owner, principal_id, scopes)
        rows = _bounded_rows(observations, "observations", MAX_ROWS)
        if not rows:
            raise MarketQuantitativeError("invalid_request", "observations must not be empty")
        names = list((factor_definitions or {name: {"definition": name} for name in ("market", "size", "value", "momentum", "quality")}).keys())
        if not 1 <= len(names) <= MAX_FACTORS or any(not isinstance(name, str) or not name for name in names):
            raise MarketQuantitativeError("invalid_request", "factor definitions must be bounded named factors")
        normalized = []
        missing = 0
        for row in rows:
            at = _millis(row.get("at_ms", row.get("date_ms")), "observation.at_ms")
            if split_at_ms is not None and not isinstance(split_at_ms, int):
                raise MarketQuantitativeError("invalid_request", "split_at_ms must be an integer")
            factors = row.get("factors")
            if not isinstance(factors, Mapping) or row.get("return") is None or any(name not in factors for name in names):
                missing += 1
                continue
            normalized.append({"at_ms": at, "listing_id": _text(row.get("listing_id"), "observation.listing_id", 200), "return": _number(row["return"], "observation.return"), "factors": {name: _number(factors[name], f"factor.{name}") for name in names}, "source_revision_ids": list(row.get("source_revision_ids", []))})
        if len(normalized) < len(names) + 2:
            raise MarketQuantitativeError("insufficient_history", "factor model has too few complete observations", missing_count=missing)
        fit_rows = [row for row in normalized if split_at_ms is None or row["at_ms"] < split_at_ms]
        test_rows = [row for row in normalized if split_at_ms is not None and row["at_ms"] >= split_at_ms]
        if len(fit_rows) < len(names) + 1:
            raise MarketQuantitativeError("insufficient_history", "factor training sample is too small")
        design = [[1.0, *[row["factors"][name] for name in names]] for row in fit_rows]
        coefficients, residual_std = _solve_linear(design, [row["return"] for row in fit_rows])
        predictions = [sum(coef * x for coef, x in zip(coefficients, [1.0, *[row["factors"][name] for name in names]], strict=True)) for row in test_rows]
        actual = [row["return"] for row in test_rows]
        out_of_sample = None if not test_rows else {"sample_size": len(test_rows), "rmse": math.sqrt(statistics.mean([(left - right) ** 2 for left, right in zip(actual, predictions, strict=True)])), "mae": statistics.mean([abs(left - right) for left, right in zip(actual, predictions, strict=True)])}
        exposure = {name: coefficients[index + 1] for index, name in enumerate(names)}
        payload = {"observations": rows, "factor_definitions": factor_definitions or {}, "split_at_ms": split_at_ms}
        return self._persist(namespace, "factor-analysis", owner, payload, {
            "contract": "noesis-market-factor-analysis-v1",
            "namespace": namespace,
            "formula_version": FACTOR_FORMULA_VERSION,
            "factor_definitions": factor_definitions or {name: {"definition": name} for name in names},
            "factors": names,
            "exposure": exposure,
            "intercept": coefficients[0],
            "residual_stddev": residual_std,
            "sample_size": len(fit_rows),
            "missing_count": missing,
            "out_of_sample": out_of_sample,
            "assumptions": ["OLS with an intercept over the supplied dated cross-section/time series.", "Exposure is descriptive and not a causal claim."],
            "limitations": ["Singular or collinear designs are rejected.", "Factor definitions and mappings are caller-supplied and remain part of the manifest."],
        })

    def backtest(
        self,
        namespace: str,
        *,
        signals: Sequence[Mapping[str, Any]],
        strategy_id: str,
        strategy_version: str,
        commission_bps: float = 0.0,
        slippage_bps: float = 0.0,
        borrow_bps: float = 0.0,
        initial_capital: float = 100_000.0,
        benchmark_returns: Sequence[Mapping[str, Any]] | None = None,
        owner: str,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, calculate=True)
        owner = _check_owner(owner, principal_id, scopes)
        rows = _bounded_rows(signals, "signals", MAX_ROWS)
        if not rows:
            raise MarketQuantitativeError("invalid_request", "signals must not be empty")
        strategy_id = _text(strategy_id, "strategy_id", 200)
        strategy_version = _text(strategy_version, "strategy_version", 100)
        commission_bps, slippage_bps, borrow_bps = (_number(value, name) for value, name in ((commission_bps, "commission_bps"), (slippage_bps, "slippage_bps"), (borrow_bps, "borrow_bps")))
        if min(commission_bps, slippage_bps, borrow_bps) < 0 or max(commission_bps, slippage_bps, borrow_bps) > 10_000:
            raise MarketQuantitativeError("invalid_request", "transaction cost assumptions are outside the safe bound")
        capital = _number(initial_capital, "initial_capital")
        if capital <= 0:
            raise MarketQuantitativeError("invalid_request", "initial_capital must be positive")
        trades = []
        leakage = []
        missing = []
        for index, row in enumerate(rows):
            signal_at = _millis(row.get("signal_at_ms"), f"signals[{index}].signal_at_ms")
            execution_at = _millis(row.get("execution_at_ms"), f"signals[{index}].execution_at_ms")
            if execution_at <= signal_at:
                leakage.append({"index": index, "reason": "execution_not_after_signal", "signal_at_ms": signal_at, "execution_at_ms": execution_at})
                continue
            direction = _number(row.get("signal", 0), f"signals[{index}].signal")
            if direction == 0:
                continue
            entry = row.get("execution_price")
            exit_price = row.get("exit_price")
            if entry is None or exit_price is None:
                missing.append({"index": index, "listing_id": row.get("listing_id"), "reason": "delisted_or_missing_exit_price"})
                continue
            entry, exit_price = _number(entry, "execution_price"), _number(exit_price, "exit_price")
            if entry <= 0 or exit_price <= 0:
                raise MarketQuantitativeError("invalid_request", "execution prices must be positive")
            gross = direction * (exit_price / entry - 1.0)
            notional = abs(_number(row.get("notional", capital), "notional"))
            turnover = notional / capital
            cost = turnover * (commission_bps + slippage_bps + borrow_bps) / 10_000.0
            net = gross - cost
            trades.append({
                "listing_id": _text(row.get("listing_id"), "signal.listing_id", 200),
                "signal_at_ms": signal_at,
                "execution_at_ms": execution_at,
                "exit_at_ms": _millis(row.get("exit_at_ms", execution_at), "exit_at_ms"),
                "direction": direction,
                "gross_return": gross,
                "cost": cost,
                "net_return": net,
                "turnover": turnover,
                "source_revision_ids": list(row.get("source_revision_ids", [])),
                "adjustment_revision_ids": list(row.get("adjustment_revision_ids", [])),
            })
        if leakage:
            raise MarketQuantitativeError("lookahead_detected", "signal execution contains look-ahead leakage", violations=leakage[:20])
        equity = capital
        equity_curve = [{"at_ms": 0, "equity": equity}]
        for trade in sorted(trades, key=lambda item: (item["exit_at_ms"], item["listing_id"])):
            equity *= 1.0 + trade["net_return"]
            equity_curve.append({"at_ms": trade["exit_at_ms"], "equity": equity})
        returns = [trade["net_return"] for trade in trades]
        benchmark = [
            _number(item.get("return"), "benchmark.return")
            for item in _bounded_rows(benchmark_returns or [], "benchmark_returns", MAX_ROWS)
        ]
        payload = {
            "signals": rows,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "costs": {"commission_bps": commission_bps, "slippage_bps": slippage_bps, "borrow_bps": borrow_bps},
            "initial_capital": capital,
            "benchmark_returns": list(benchmark_returns or []),
        }
        return self._persist(namespace, "backtest", owner, payload, {
            "contract": "noesis-market-backtest-v1",
            "namespace": namespace,
            "formula_version": BACKTEST_FORMULA_VERSION,
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "trades": trades,
            "missing_inputs": missing,
            "leakage_checks": {"passed": True, "violations": []},
            "initial_capital": capital,
            "final_equity": equity,
            "total_return": equity / capital - 1.0,
            "trade_count": len(trades),
            "turnover": sum(returns and [trade["turnover"] for trade in trades] or [0.0]),
            "benchmark_total_return": math.prod(1.0 + value for value in benchmark) - 1.0 if benchmark else None,
            "equity_curve": equity_curve,
            "cost_sensitivity": {"commission_bps": commission_bps, "slippage_bps": slippage_bps, "borrow_bps": borrow_bps},
            "limitations": ["Execution is represented by caller-supplied post-signal prices; no order-book fill model is inferred.", "Delisted or missing exits remain explicit missing inputs.", "Corporate-action revision IDs must be supplied by the caller when adjusted prices are used."],
        })

    def walk_forward(
        self,
        namespace: str,
        *,
        samples: Sequence[Mapping[str, Any]],
        feature_names: Sequence[str],
        train_window: int,
        test_window: int,
        gap: int,
        owner: str,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, calculate=True)
        owner = _check_owner(owner, principal_id, scopes)
        rows = _bounded_rows(samples, "samples", MAX_ROWS)
        names = [_text(name, "feature_name", 100) for name in feature_names]
        if not 1 <= len(names) <= MAX_FACTORS:
            raise MarketQuantitativeError("invalid_request", "feature_names must be bounded")
        if any(type(value) is not int or value < 1 for value in (train_window, test_window)) or type(gap) is not int or gap < 0:
            raise MarketQuantitativeError("invalid_request", "walk-forward windows are invalid")
        normalized = []
        leakage = []
        for index, row in enumerate(sorted(rows, key=lambda item: int(item.get("target_at_ms", 0)))):
            target_at = _millis(row.get("target_at_ms"), f"samples[{index}].target_at_ms")
            available_at = _millis(row.get("features_available_at_ms", row.get("available_at_ms")), f"samples[{index}].features_available_at_ms")
            if available_at > target_at:
                leakage.append({"index": index, "reason": "feature_after_label", "available_at_ms": available_at, "target_at_ms": target_at})
                continue
            features = row.get("features")
            if not isinstance(features, Mapping) or row.get("label") is None or any(name not in features for name in names):
                continue
            normalized.append({"target_at_ms": target_at, "features_available_at_ms": available_at, "features": {name: _number(features[name], f"feature.{name}") for name in names}, "label": _number(row["label"], "label"), "source_revision_ids": list(row.get("source_revision_ids", []))})
        if leakage:
            raise MarketQuantitativeError("lookahead_detected", "feature availability leaks past the label", violations=leakage[:20])
        folds = []
        cursor = train_window + gap
        while cursor < len(normalized):
            train = normalized[max(0, cursor - gap - train_window): max(0, cursor - gap)]
            test = normalized[cursor: cursor + test_window]
            if len(train) < train_window or not test:
                break
            design = [[1.0, *[row["features"][name] for name in names]] for row in train]
            try:
                coefficients, residual_std = _solve_linear(design, [row["label"] for row in train])
            except MarketQuantitativeError:
                folds.append({"status": "blocked", "train_size": len(train), "test_size": len(test), "reason": "collinear_or_insufficient_training"})
                cursor += test_window
                continue
            predictions = [sum(coef * x for coef, x in zip(coefficients, [1.0, *[row["features"][name] for name in names]], strict=True)) for row in test]
            actual = [row["label"] for row in test]
            baseline = statistics.mean(row["label"] for row in train)
            folds.append({"status": "evaluated", "train_size": len(train), "test_size": len(test), "test_start_at_ms": test[0]["target_at_ms"], "rmse": math.sqrt(statistics.mean([(left - right) ** 2 for left, right in zip(actual, predictions, strict=True)])), "mae": statistics.mean([abs(left - right) for left, right in zip(actual, predictions, strict=True)]), "baseline_rmse": math.sqrt(statistics.mean([(value - baseline) ** 2 for value in actual])), "residual_stddev": residual_std, "predictions": predictions, "labels": actual})
            cursor += test_window
        if not folds:
            raise MarketQuantitativeError("insufficient_history", "no walk-forward fold can be evaluated")
        payload = {"samples": rows, "feature_names": names, "train_window": train_window, "test_window": test_window, "gap": gap}
        return self._persist(namespace, "walk-forward", owner, payload, {
            "contract": "noesis-market-walk-forward-v1",
            "namespace": namespace,
            "formula_version": WALK_FORWARD_FORMULA_VERSION,
            "feature_names": names,
            "train_window": train_window,
            "test_window": test_window,
            "gap": gap,
            "folds": folds,
            "sample_size": len(normalized),
            "stability": {"evaluated_folds": sum(1 for fold in folds if fold["status"] == "evaluated"), "blocked_folds": sum(1 for fold in folds if fold["status"] == "blocked")},
            "multiple_testing_note": "Fold-level results are not independent hypothesis tests; no aggregate significance claim is made.",
            "limitations": ["The model is a deterministic linear baseline; model selection and tuning history remain caller inputs.", "Held-out results are descriptive and not a guarantee of future performance."],
        })

    def record_portfolio(
        self,
        namespace: str,
        *,
        portfolio_id: str,
        version: int,
        holdings: Sequence[Mapping[str, Any]],
        transactions: Sequence[Mapping[str, Any]],
        base_currency: str,
        as_of_ms: int,
        fx_rates: Mapping[str, Any] | None,
        valuations: Sequence[Mapping[str, Any]] | None,
        benchmark_returns: Sequence[Mapping[str, Any]] | None,
        owner: str,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, calculate=True)
        owner = _check_owner(owner, principal_id, scopes)
        portfolio_id = _text(portfolio_id, "portfolio_id", 200)
        base_currency = _text(base_currency, "base_currency", 20).upper()
        if type(version) is not int or version < 1:
            raise MarketQuantitativeError("invalid_request", "portfolio version must be positive")
        as_of_ms = _millis(as_of_ms, "as_of_ms")
        holding_rows = _bounded_rows(holdings, "holdings", MAX_POSITIONS)
        transaction_rows = _bounded_rows(transactions, "transactions", MAX_ROWS)
        fx_rates = dict(fx_rates or {})
        positions: dict[str, dict[str, Any]] = {}
        cash = 0.0
        for row in holding_rows:
            listing_id = _text(row.get("listing_id"), "holding.listing_id", 200)
            quantity = _number(row.get("quantity", 0), "holding.quantity")
            currency = _text(row.get("currency", base_currency), "holding.currency", 20).upper()
            positions[listing_id] = {"listing_id": listing_id, "quantity": quantity, "currency": currency, "market_value": row.get("market_value"), "return": row.get("return"), "sector": row.get("sector"), "geography": row.get("geography"), "factor_exposures": dict(row.get("factor_exposures", {})) if isinstance(row.get("factor_exposures"), Mapping) else {}}
            cash += _number(row.get("cash", 0), "holding.cash")
        reconciliation = []
        for index, row in enumerate(sorted(transaction_rows, key=lambda item: int(item.get("at_ms", 0)))):
            at = _millis(row.get("at_ms"), f"transaction[{index}].at_ms")
            if at > as_of_ms:
                raise MarketQuantitativeError("invalid_request", "transaction occurs after portfolio cutoff")
            listing_id = _text(row.get("listing_id"), "transaction.listing_id", 200)
            quantity = _number(row.get("quantity", 0), "transaction.quantity")
            price = _number(row.get("price", 0), "transaction.price")
            fee = _number(row.get("fee", 0), "transaction.fee")
            currency = _text(row.get("currency", base_currency), "transaction.currency", 20).upper()
            if price < 0 or fee < 0:
                raise MarketQuantitativeError("invalid_request", "transaction price and fee must be nonnegative")
            rate = 1.0 if currency == base_currency else _number(fx_rates.get(currency), f"fx_rates.{currency}")
            if rate <= 0:
                raise MarketQuantitativeError("invalid_request", "FX rates must be positive")
            notional = quantity * price * rate
            cash -= notional + fee * rate
            position = positions.setdefault(listing_id, {"listing_id": listing_id, "quantity": 0.0, "currency": currency, "market_value": None, "return": None, "sector": row.get("sector"), "geography": row.get("geography"), "factor_exposures": {}})
            position["quantity"] += quantity
            reconciliation.append({"at_ms": at, "listing_id": listing_id, "quantity": quantity, "notional_base": notional, "fee_base": fee * rate, "source_revision_ids": list(row.get("source_revision_ids", []))})
        active_positions = [position for position in positions.values() if abs(position["quantity"]) > 1e-12]
        attribution: dict[str, dict[str, float]] = {"security": {}, "sector": {}, "currency": {}}
        valued = [row for row in active_positions if row.get("market_value") is not None]
        valued_total = sum(abs(_number(row["market_value"], "holding.market_value")) for row in valued)
        if valued_total:
            for row in valued:
                weight = _number(row["market_value"], "holding.market_value") / valued_total
                contribution = weight * (_number(row.get("return", 0), "holding.return"))
                attribution["security"][str(row["listing_id"])] = contribution
                for dimension in ("sector", "currency"):
                    key = str(row.get(dimension) or "unknown")
                    attribution[dimension][key] = attribution[dimension].get(key, 0.0) + contribution
        valuation_rows = _bounded_rows(valuations or [], "valuations", MAX_ROWS)
        twr = None
        mwr = None
        if valuation_rows:
            ordered = sorted(valuation_rows, key=lambda item: int(item.get("at_ms", 0)))
            values = [None if row.get("value") is None else _number(row.get("value"), "valuation.value") for row in ordered]
            if any(value is not None and value < 0 for value in values):
                raise MarketQuantitativeError("invalid_request", "portfolio valuation cannot be negative")
            periods = []
            for previous, current in zip(ordered, ordered[1:]):
                prior_value = None if previous.get("value") is None else _number(previous.get("value"), "valuation.value")
                current_value = None if current.get("value") is None else _number(current.get("value"), "valuation.value")
                flow = _number(current.get("external_flow", 0), "valuation.external_flow")
                if prior_value is None or current_value is None or prior_value <= 0:
                    continue
                periods.append((current_value - flow) / prior_value)
            if periods:
                twr = math.prod(periods) - 1.0
            cashflows = [] if any(value is None for value in values) else [-values[0], *[_number(row.get("external_flow", 0), "valuation.external_flow") for row in ordered[1:-1]], values[-1] - _number(ordered[-1].get("external_flow", 0), "valuation.external_flow")]
            if cashflows and any(cashflows) and cashflows[0] < 0 and cashflows[-1] > 0:
                low, high = -0.9999, 10.0
                for _ in range(80):
                    rate = (low + high) / 2
                    npv = sum(flow / ((1.0 + rate) ** index) for index, flow in enumerate(cashflows))
                    if npv > 0:
                        low = rate
                    else:
                        high = rate
                mwr = (low + high) / 2
        input_payload = {"portfolio_id": portfolio_id, "version": version, "holdings": holding_rows, "transactions": transaction_rows, "base_currency": base_currency, "as_of_ms": as_of_ms, "fx_rates": fx_rates, "valuations": valuation_rows, "benchmark_returns": list(benchmark_returns or [])}
        result = {
            "contract": "noesis-market-portfolio-v1",
            "namespace": namespace,
            "formula_version": PORTFOLIO_FORMULA_VERSION,
            "portfolio_id": portfolio_id,
            "owner": owner,
            "version": version,
            "as_of_ms": as_of_ms,
            "base_currency": base_currency,
            "positions": active_positions,
            "cash": cash,
            "transactions": reconciliation,
            "reconciliation": {"transaction_count": len(reconciliation), "unresolved_missing_price_count": sum(1 for row in valuation_rows if row.get("value") is None), "status": "complete" if valuation_rows and all(row.get("value") is not None for row in valuation_rows) else "partial"},
            "time_weighted_return": twr,
            "money_weighted_return": mwr,
            "benchmark_relative": None if twr is None or not benchmark_returns else twr - (math.prod(1.0 + _number(row.get("return"), "benchmark.return") for row in benchmark_returns) - 1.0),
            "attribution": attribution,
            "source_revision_ids": self._source_manifest([*holding_rows, *transaction_rows, *valuation_rows]),
            "input_manifest": {"input_hash": _digest(input_payload), "kind": "portfolio", "formula_version": PORTFOLIO_FORMULA_VERSION},
            "limitations": ["FX uses the supplied point estimate per non-base currency; dated FX revision rows should be included in the manifest for historical replay.", "Corporate-action quantities must be represented by explicit transaction rows or caller-supplied adjusted holdings.", "A partial valuation history does not become a complete performance record."],
        }
        prior = self.conn.execute("SELECT record_hash,result_json FROM market_portfolio_revisions WHERE namespace=? AND portfolio_id=? AND version=?", [namespace, portfolio_id, version]).fetchone()
        encoded = _canonical(result)
        record_hash = _digest(result)
        if prior:
            if prior[0] != record_hash:
                raise MarketQuantitativeError("revision_conflict", "portfolio version is immutable")
            return {**json.loads(prior[1]), "record_hash": prior[0], "idempotent": True}
        self.conn.execute("INSERT INTO market_portfolio_revisions VALUES (?,?,?,?,?,?,?,?)", [namespace, portfolio_id, version, owner, _canonical(input_payload), encoded, record_hash, _millis(self.now(), "recorded_at_ms")])
        self.conn.execute(
            "INSERT INTO market_portfolio_audit VALUES (?,?,?,?,?,?,?)",
            [
                "market-portfolio-audit:" + _digest([namespace, portfolio_id, version, record_hash])[:32],
                namespace,
                portfolio_id,
                version,
                owner,
                "record",
                _millis(self.now(), "audit_at_ms"),
            ],
        )
        return {**result, "record_hash": record_hash, "idempotent": False}

    def risk_report(
        self,
        namespace: str,
        *,
        portfolio: Mapping[str, Any],
        returns: Sequence[Mapping[str, Any]],
        scenarios: Sequence[Mapping[str, Any]] | None,
        confidence: float = 0.95,
        owner: str,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, calculate=True)
        owner = _check_owner(owner, principal_id, scopes)
        if not isinstance(portfolio, Mapping):
            raise MarketQuantitativeError("invalid_request", "portfolio must be an object")
        confidence = _number(confidence, "confidence")
        if not 0.5 <= confidence < 1:
            raise MarketQuantitativeError("invalid_request", "confidence must be between 0.5 and 1")
        return_rows = _bounded_rows(returns, "returns", MAX_ROWS)
        values = [_number(row.get("return"), "returns.return") for row in return_rows]
        if len(values) < 2:
            raise MarketQuantitativeError("insufficient_history", "risk report requires at least two returns")
        holdings = _bounded_rows(portfolio.get("positions", []), "portfolio.positions", MAX_POSITIONS)
        total = sum(abs(_number(row.get("market_value", row.get("value", 0)), "position.market_value")) for row in holdings)
        if total <= 0:
            raise MarketQuantitativeError("invalid_request", "portfolio positions have no market value")
        exposures: dict[str, float] = {}
        concentration = []
        for row in holdings:
            value = _number(row.get("market_value", row.get("value", 0)), "position.market_value")
            weight = value / total
            concentration.append({"listing_id": row.get("listing_id"), "weight": weight})
            for dimension in ("sector", "geography", "currency"):
                key = f"{dimension}:{row.get(dimension, 'unknown')}"
                exposures[key] = exposures.get(key, 0.0) + weight
            if isinstance(row.get("factor_exposures"), Mapping):
                for factor, factor_value in row["factor_exposures"].items():
                    exposures[f"factor:{factor}"] = exposures.get(f"factor:{factor}", 0.0) + weight * _number(factor_value, f"factor_exposures.{factor}")
        ordered = sorted(values)
        index = max(0, min(len(ordered) - 1, math.ceil((1.0 - confidence) * len(ordered)) - 1))
        var = -ordered[index]
        tail = [value for value in ordered if value <= ordered[index]]
        expected_shortfall = -statistics.mean(tail) if tail else None
        equity = 1.0
        peak = 1.0
        drawdown = 0.0
        for value in values:
            equity *= 1.0 + value
            peak = max(peak, equity)
            drawdown = min(drawdown, equity / peak - 1.0)
        scenario_rows = []
        for scenario in _bounded_rows(scenarios or [], "scenarios", 100):
            name = _text(scenario.get("name"), "scenario.name", 100)
            shocks = scenario.get("shocks", {})
            if not isinstance(shocks, Mapping):
                raise MarketQuantitativeError("invalid_request", "scenario.shocks must be an object")
            contribution = []
            total_loss = 0.0
            for row in holdings:
                listing_id = row.get("listing_id")
                shock = _number(shocks.get(listing_id, 0), "scenario.shock")
                value = _number(row.get("market_value", row.get("value", 0)), "position.market_value")
                loss = value * shock / total
                total_loss += loss
                contribution.append({"listing_id": listing_id, "shock": shock, "contribution": loss})
            scenario_rows.append({"name": name, "loss": total_loss, "contributions": contribution})
        input_payload = {"portfolio": dict(portfolio), "returns": return_rows, "scenarios": list(scenarios or []), "confidence": confidence}
        return self._persist(namespace, "risk-report", owner, input_payload, {
            "contract": "noesis-market-risk-report-v1",
            "namespace": namespace,
            "formula_version": RISK_FORMULA_VERSION,
            "confidence": confidence,
            "sample_size": len(values),
            "exposures": exposures,
            "concentration": {"hhi": sum(row["weight"] ** 2 for row in concentration), "positions": sorted(concentration, key=lambda row: -row["weight"])},
            "drawdown": drawdown,
            "liquidity": {"status": "available" if all(row.get("adv_value") is not None for row in holdings) else "incomplete", "missing_adv_count": sum(1 for row in holdings if row.get("adv_value") is None), "days_to_liquidate": {str(row.get("listing_id")): None if not row.get("adv_value") else abs(_number(row.get("market_value", 0), "position.market_value")) / _number(row["adv_value"], "position.adv_value") for row in holdings}},
            "historical_var": var,
            "historical_expected_shortfall": expected_shortfall,
            "scenarios": scenario_rows,
            "limitations": ["Historical VaR and expected shortfall describe the supplied sample and are not guaranteed bounds.", "Scenario results are linear shock calculations unless the input explicitly supplies a nonlinear valuation.", "Unknown or missing mappings remain in the exposure and liquidity output."],
        })
