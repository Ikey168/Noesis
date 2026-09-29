"""Bounded analytics for market asset classes not covered by the equity store.

The module is intentionally provider-neutral.  It accepts normalized fixture or
adapter payloads, keeps source revisions and entitlement decisions visible, and
persists an immutable calculation receipt.  It does not pretend that a local
fixture is a licensed feed: missing provider evidence is returned as
``rights_unverified`` and never upgraded to production readiness.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import InvalidOperation
import hashlib
import json
import math
import statistics
import time
from typing import Any
from src.domains.market.entitlements import recheck_stored_receipt_rights, withheld_receipt

SPECIALIZED_READ_SCOPE = "market:specialized:read"
SPECIALIZED_WRITE_SCOPE = "market:specialized:write"
SPECIALIZED_FORMULA_VERSION = "noesis-market-specialized-v1"
MAX_ROWS = 20_000
MAX_SCENARIOS = 500

_DDL = """
CREATE TABLE IF NOT EXISTS market_specialized_runs (
 namespace TEXT NOT NULL,
 run_id TEXT NOT NULL,
 owner TEXT NOT NULL,
 kind TEXT NOT NULL,
 input_hash TEXT NOT NULL,
 input_json TEXT NOT NULL,
 result_json TEXT NOT NULL,
 record_hash TEXT NOT NULL,
 recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, run_id),
 UNIQUE(namespace, kind, input_hash)
);
CREATE INDEX IF NOT EXISTS idx_market_specialized_runs_owner
 ON market_specialized_runs(namespace, owner, kind, recorded_at_ms);
"""


class MarketSpecializedError(ValueError):
    """Typed failure safe to return through REST and MCP boundaries."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def ensure_market_specialized_schema(conn: Any) -> None:
    conn.execute(_DDL)


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise MarketSpecializedError(
            "invalid_request", "specialized input must be finite JSON"
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, name: str, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MarketSpecializedError("invalid_request", f"{name} must be bounded text")
    return value.strip()


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, InvalidOperation) as exc:
        raise MarketSpecializedError("invalid_request", f"{name} must be finite") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise MarketSpecializedError("invalid_request", f"{name} must be finite and valid")
    return result


def _millis(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise MarketSpecializedError(
            "invalid_request", f"{name} must be a nonnegative epoch millisecond"
        )
    return value


def _rows(value: Any, name: str, maximum: int = MAX_ROWS) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise MarketSpecializedError("invalid_request", f"{name} must be a list")
    if len(value) > maximum:
        raise MarketSpecializedError(
            "bound_exceeded", f"{name} must contain at most {maximum} rows"
        )
    result: list[dict[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            raise MarketSpecializedError("invalid_request", f"{name}[{index}] must be an object")
        result.append(dict(row))
    return result


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise MarketSpecializedError("invalid_request", f"{name} must be an object")
    return dict(value)


def _source_ids(*values: Any) -> list[str]:
    result: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            value = [value]
        if isinstance(value, (str, bytes)):
            value = [value]
        if isinstance(value, Sequence):
            for row in value:
                if isinstance(row, Mapping):
                    for key in ("source_revision_id", "revision_id"):
                        item = row.get(key)
                        if isinstance(item, str) and item:
                            result.add(item)
                    for item in row.get("source_revision_ids", []):
                        if isinstance(item, str) and item:
                            result.add(item)
                elif isinstance(row, str) and row:
                    result.add(row)
    return sorted(result)


def _authorize(namespace: str, principal_id: str, scopes: set[str], *, write: bool) -> None:
    _text(namespace, "namespace", 100)
    _text(principal_id, "principal_id", 200)
    if "operator" in scopes:
        return
    required = SPECIALIZED_WRITE_SCOPE if write else SPECIALIZED_READ_SCOPE
    if required not in scopes:
        raise MarketSpecializedError("unauthorized", f"{required} scope is required")


def _owner(owner: str | None, principal_id: str, scopes: set[str]) -> str:
    value = _text(owner or principal_id, "owner", 200)
    if value != principal_id and "operator" not in scopes:
        raise MarketSpecializedError("unauthorized", "specialized result belongs to another principal")
    return value


def _rights(
    conn: Any,
    namespace: str,
    source_entitlements: Any,
    *,
    principal_id: str,
    scopes: set[str],
) -> dict[str, Any]:
    refs = _rows(source_entitlements or [], "source_entitlements", maximum=256)
    if not refs:
        return {
            "status": "rights_unverified",
            "decisions": [],
            "limitations": [
                "No provider entitlement references were supplied; fixture calculations are not a license or production coverage claim."
            ],
        }
    from src.domains.market.entitlements import MarketEntitlementStore

    decisions = MarketEntitlementStore(conn, initialize=True).authorize_sources(
        namespace,
        refs,
        operation="derive",
        principal_id=principal_id,
        scopes=scopes,
    )
    return {"status": "authorized", "decisions": decisions, "limitations": []}


def _year_fraction(start_ms: int, end_ms: int, convention: str) -> float:
    if end_ms < start_ms:
        raise MarketSpecializedError("invalid_request", "end date must follow start date")
    days = (end_ms - start_ms) / 86_400_000
    convention = convention.upper()
    if convention == "30/360":
        start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).date()
        end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).date()
        return max(0.0, ((end.year - start.year) * 360 + (end.month - start.month) * 30 + (min(end.day, 30) - min(start.day, 30))) / 360)
    if convention == "ACT/365":
        return days / 365.0
    if convention == "ACT/ACT":
        return days / 365.25
    raise MarketSpecializedError("invalid_request", "unsupported day_count convention")


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _black_scholes(
    spot: float,
    strike: float,
    time_years: float,
    rate: float,
    dividend: float,
    volatility: float,
    option_type: str,
) -> dict[str, float]:
    if min(spot, strike, time_years, volatility) <= 0:
        raise MarketSpecializedError("invalid_request", "option terms must be positive")
    sign = 1.0 if option_type == "call" else -1.0
    root_t = math.sqrt(time_years)
    d1 = (math.log(spot / strike) + (rate - dividend + 0.5 * volatility**2) * time_years) / (volatility * root_t)
    d2 = d1 - volatility * root_t
    nd1 = _normal_cdf(sign * d1)
    nd2 = _normal_cdf(sign * d2)
    discount_r = math.exp(-rate * time_years)
    discount_q = math.exp(-dividend * time_years)
    price = sign * (spot * discount_q * nd1 - strike * discount_r * nd2)
    delta = sign * discount_q * _normal_cdf(sign * d1)
    density = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    gamma = discount_q * density / (spot * volatility * root_t)
    vega = spot * discount_q * density * root_t
    theta = (
        -spot * discount_q * density * volatility / (2.0 * root_t)
        - sign * rate * strike * discount_r * nd2
        + sign * dividend * spot * discount_q * nd1
    )
    rho = sign * strike * time_years * discount_r * nd2
    return {
        "price": price,
        "delta": delta,
        "gamma": gamma,
        "vega": vega,
        "theta": theta,
        "rho": rho,
        "d1": d1,
        "d2": d2,
    }


class MarketSpecializedStore:
    """Immutable, owner-scoped receipts for fixed-income and other assets."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            ensure_market_specialized_schema(conn)

    def _persist(
        self,
        namespace: str,
        kind: str,
        owner: str,
        input_payload: Mapping[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        input_hash = _digest(input_payload)
        run_id = f"market-{kind}-run:{_digest([namespace, kind, input_hash])[:32]}"
        prior = self.conn.execute(
            "SELECT result_json,record_hash,recorded_at_ms FROM market_specialized_runs WHERE namespace=? AND run_id=?",
            [namespace, run_id],
        ).fetchone()
        if prior:
            return {
                **json.loads(prior[0]),
                "run_id": run_id,
                "record_hash": prior[1],
                "recorded_at_ms": int(prior[2]),
                "idempotent": True,
            }
        body = {
            **result,
            "run_id": run_id,
            "owner": owner,
            "input_manifest": {
                "input_hash": input_hash,
                "kind": kind,
                "formula_version": result.get("formula_version", SPECIALIZED_FORMULA_VERSION),
                "source_revision_ids": result.get("source_revision_ids", []),
            },
        }
        record_hash = _digest(body)
        recorded_at_ms = _millis(self.now(), "recorded_at_ms")
        self.conn.execute(
            "INSERT INTO market_specialized_runs VALUES (?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                run_id,
                owner,
                kind,
                input_hash,
                _canonical(input_payload),
                _canonical(body),
                record_hash,
                recorded_at_ms,
            ],
        )
        return {**body, "record_hash": record_hash, "recorded_at_ms": recorded_at_ms, "idempotent": False}

    def inspect_run(self, namespace: str, run_id: str, *, principal_id: str, scopes: set[str], operation: str = "derive") -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=False)
        row = self.conn.execute(
            "SELECT owner,result_json,record_hash,recorded_at_ms FROM market_specialized_runs WHERE namespace=? AND run_id=?",
            [namespace, _text(run_id, "run_id")],
        ).fetchone()
        if row is None or (row[0] != principal_id and "operator" not in scopes):
            raise MarketSpecializedError("not_found", "specialized run is unavailable")
        result = json.loads(row[1])
        rights = recheck_stored_receipt_rights(
            self.conn, namespace, result, operation=operation,
            principal_id=principal_id, scopes=scopes, now_ms=int(self.now()),
        )
        if rights["state"] == "withheld":
            return withheld_receipt(
                str(result.get("contract") or "noesis-market-specialized-run-v1"), "specialized_run", run_id, row[2], rights
            )
        return {**result, "record_hash": row[2], "recorded_at_ms": int(row[3]), "current_rights": rights}

    def export_run(self, namespace: str, run_id: str, *, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        result = self.inspect_run(namespace, run_id, principal_id=principal_id, scopes=scopes, operation="export")
        if result.get("withheld"):
            return {**result, "contract": "noesis-market-specialized-export-v1", "artifact": None}
        return {
            "contract": "noesis-market-specialized-export-v1",
            "namespace": namespace,
            "run_id": run_id,
            "record_hash": result["record_hash"],
            "formula_version": result.get("formula_version"),
            "input_manifest": result.get("input_manifest"),
            "artifact": result,
            "replay": {"deterministic": True, "input_snapshot_retained": True},
            "limitations": [
                "The originating adapter must recheck current source rights before redistributing provider payloads."
            ],
        }

    def fixed_income(
        self,
        namespace: str,
        *,
        instrument: Mapping[str, Any],
        cashflows: Sequence[Mapping[str, Any]] | None = None,
        quote: Mapping[str, Any] | None = None,
        curve: Sequence[Mapping[str, Any]] | None = None,
        scenarios: Sequence[Mapping[str, Any]] | None = None,
        credit_evidence: Sequence[Mapping[str, Any]] | None = None,
        source_entitlements: Sequence[Mapping[str, Any]] | None = None,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner_value = _owner(owner, principal_id, scopes)
        terms = _mapping(instrument, "instrument")
        instrument_id = _text(terms.get("instrument_id"), "instrument.instrument_id")
        face = _number(terms.get("face_value", 100.0), "instrument.face_value", positive=True)
        frequency = terms.get("coupon_frequency", 2)
        if type(frequency) is not int or not 1 <= frequency <= 12:
            raise MarketSpecializedError("invalid_request", "coupon_frequency must be 1..12")
        day_count = str(terms.get("day_count", "30/360")).upper()
        valuation_at_ms = _millis(terms.get("valuation_at_ms"), "instrument.valuation_at_ms") if terms.get("valuation_at_ms") is not None else None
        maturity_at_ms = _millis(terms.get("maturity_at_ms"), "instrument.maturity_at_ms") if terms.get("maturity_at_ms") is not None else None
        coupon_rate = _number(terms.get("coupon_rate", 0.0), "instrument.coupon_rate")
        cashflow_rows = _rows(cashflows or [], "cashflows", maximum=MAX_ROWS)
        normalized_flows: list[dict[str, float]] = []
        for index, row in enumerate(cashflow_rows):
            at = _number(row.get("time_years"), f"cashflows[{index}].time_years", positive=True) if row.get("time_years") is not None else None
            if at is None:
                at_ms = _millis(row.get("at_ms"), f"cashflows[{index}].at_ms")
                if valuation_at_ms is None:
                    raise MarketSpecializedError("invalid_request", "valuation_at_ms is required for dated cashflows")
                at = _year_fraction(valuation_at_ms, at_ms, day_count)
            amount = _number(row.get("amount"), f"cashflows[{index}].amount")
            if at <= 0 or amount < 0:
                raise MarketSpecializedError("invalid_request", "cashflows must have positive time and nonnegative amount")
            normalized_flows.append({"time_years": at, "amount": amount})
        if not normalized_flows:
            maturity_years = terms.get("maturity_years")
            if maturity_years is None and valuation_at_ms is not None and maturity_at_ms is not None:
                maturity_years = _year_fraction(valuation_at_ms, maturity_at_ms, day_count)
            if maturity_years is not None:
                maturity_years = _number(maturity_years, "instrument.maturity_years", positive=True)
                periods = max(1, math.ceil(maturity_years * frequency))
                coupon = face * coupon_rate / frequency
                normalized_flows = [
                    {"time_years": min(maturity_years, index / frequency), "amount": coupon}
                    for index in range(1, periods + 1)
                ]
                normalized_flows[-1]["time_years"] = maturity_years
                normalized_flows[-1]["amount"] += face
        curve_rows = _rows(curve or [], "curve", maximum=256)
        credit_rows = _rows(credit_evidence or [], "credit_evidence", maximum=256)
        source_ids = _source_ids(terms, quote or {}, cashflow_rows, curve_rows, credit_rows)
        rights = _rights(self.conn, namespace, source_entitlements, principal_id=principal_id, scopes=scopes)
        missing: list[str] = []
        if not normalized_flows:
            missing.append("cashflows_or_maturity_terms")
        for field in ("currency", "settlement_calendar", "business_day_convention"):
            if terms.get(field) in (None, ""):
                missing.append(field)
        if terms.get("issuer_id") in (None, "") and not credit_rows:
            missing.append("issuer_or_credit_evidence")
        quote_data = _mapping(quote or {}, "quote")
        clean = quote_data.get("clean_price")
        quote_quality: dict[str, Any] = {"status": "not_provided"}
        if clean is not None:
            quote_at = quote_data.get("quote_at_ms")
            as_of = quote_data.get("as_of_ms", terms.get("valuation_at_ms"))
            stale_after = quote_data.get("stale_after_ms", terms.get("stale_after_ms"))
            if quote_at is not None and as_of is not None and stale_after is not None:
                quote_at = _millis(quote_at, "quote.quote_at_ms")
                as_of = _millis(as_of, "quote.as_of_ms")
                stale_after = _millis(stale_after, "quote.stale_after_ms")
                quote_quality = {
                    "status": "stale" if as_of - quote_at > stale_after else "fresh",
                    "quote_at_ms": quote_at,
                    "as_of_ms": as_of,
                    "stale_after_ms": stale_after,
                }
            else:
                quote_quality = {"status": "not_assessed"}
        accrued = 0.0
        if clean is not None:
            clean = _number(clean, "quote.clean_price")
            if terms.get("last_coupon_at_ms") is not None and terms.get("next_coupon_at_ms") is not None:
                last_coupon = _millis(terms["last_coupon_at_ms"], "instrument.last_coupon_at_ms")
                next_coupon = _millis(terms["next_coupon_at_ms"], "instrument.next_coupon_at_ms")
                settlement = _millis(terms.get("settlement_at_ms", valuation_at_ms), "instrument.settlement_at_ms")
                coupon_amount = face * coupon_rate / frequency
                accrued = coupon_amount * _year_fraction(last_coupon, settlement, day_count) / max(_year_fraction(last_coupon, next_coupon, day_count), 1e-12)
        dirty = None if clean is None else clean + accrued
        ytm = quote_data.get("yield")
        if ytm is not None:
            ytm = _number(ytm, "quote.yield")
        elif dirty is not None and normalized_flows:
            low, high = -frequency + 1e-9, 100.0
            target = dirty
            for _ in range(100):
                mid = (low + high) / 2
                price = sum(flow["amount"] / (1.0 + mid / frequency) ** (flow["time_years"] * frequency) for flow in normalized_flows)
                if price > target:
                    low = mid
                else:
                    high = mid
            ytm = (low + high) / 2
        if clean is None and ytm is None:
            missing.append("clean_price_or_yield")
        analytics: dict[str, Any] = {"clean_price": clean, "accrued_interest": accrued, "dirty_price": dirty, "yield": ytm}
        if ytm is not None and normalized_flows:
            discount = [flow["amount"] / (1.0 + ytm / frequency) ** (flow["time_years"] * frequency) for flow in normalized_flows]
            price = sum(discount)
            macaulay = sum(flow["time_years"] * pv for flow, pv in zip(normalized_flows, discount, strict=True)) / max(price, 1e-12)
            modified = macaulay / (1.0 + ytm / frequency)
            convexity = sum(flow["time_years"] * (flow["time_years"] + 1.0 / frequency) * pv for flow, pv in zip(normalized_flows, discount, strict=True)) / max(price * (1.0 + ytm / frequency) ** 2, 1e-12)
            analytics.update({"model_price": price, "macaulay_duration": macaulay, "modified_duration": modified, "convexity": convexity})
        curve_points = curve_rows
        curve_yield = None
        if terms.get("curve_yield") is not None:
            curve_yield = _number(terms["curve_yield"], "instrument.curve_yield")
        elif curve_points and normalized_flows:
            curve_points = sorted(({"tenor_years": _number(row.get("tenor_years"), "curve.tenor_years", positive=True), "yield": _number(row.get("yield"), "curve.yield")} for row in curve_points), key=lambda row: row["tenor_years"])
            target_tenor = normalized_flows[-1]["time_years"]
            if target_tenor <= curve_points[0]["tenor_years"]:
                curve_yield = curve_points[0]["yield"]
            elif target_tenor >= curve_points[-1]["tenor_years"]:
                curve_yield = curve_points[-1]["yield"]
            else:
                for left, right in zip(curve_points, curve_points[1:], strict=False):
                    if left["tenor_years"] <= target_tenor <= right["tenor_years"]:
                        weight = (target_tenor - left["tenor_years"]) / (right["tenor_years"] - left["tenor_years"])
                        curve_yield = left["yield"] + weight * (right["yield"] - left["yield"])
                        break
        if curve_yield is not None and ytm is not None:
            analytics["spread_bps"] = (ytm - curve_yield) * 10_000
            analytics["curve_yield"] = curve_yield
        stress_results = []
        for scenario in _rows(scenarios or [], "scenarios", maximum=MAX_SCENARIOS):
            shift = _number(scenario.get("yield_shift_bps", 0.0), "scenario.yield_shift_bps") / 10_000
            base_price = analytics.get("model_price")
            shifted_price = None
            if base_price is not None:
                shifted_price = base_price - analytics.get("modified_duration", 0.0) * base_price * shift + 0.5 * analytics.get("convexity", 0.0) * base_price * shift * shift
            stress_results.append({"name": _text(scenario.get("name", f"shift-{shift}"), "scenario.name"), "yield_shift_bps": shift * 10_000, "price": shifted_price, "price_change": None if base_price is None or shifted_price is None else shifted_price - base_price})
        unsupported = bool(terms.get("callable", False) or terms.get("structured", False))
        result = {
            "contract": "noesis-market-fixed-income-analytics-v1",
            "asset_class": "fixed_income",
            "formula_version": SPECIALIZED_FORMULA_VERSION,
            "status": "unsupported_instrument" if unsupported else ("insufficient_inputs" if missing else "complete"),
            "instrument": {"instrument_id": instrument_id, "issuer_id": terms.get("issuer_id"), "currency": terms.get("currency"), "face_value": face, "coupon_rate": coupon_rate, "coupon_frequency": frequency, "day_count": day_count, "maturity_at_ms": maturity_at_ms, "settlement_at_ms": terms.get("settlement_at_ms"), "settlement_days": terms.get("settlement_days"), "settlement_calendar": terms.get("settlement_calendar"), "business_day_convention": terms.get("business_day_convention"), "callable": bool(terms.get("callable", False)), "structured": bool(terms.get("structured", False))},
            "cashflows": normalized_flows,
            "analytics": analytics,
            "quote_quality": quote_quality,
            "credit_evidence": credit_rows,
            "model_scope": {"status": "unsupported" if unsupported else "supported_plain_bond", "option_adjusted": False},
            "stress": stress_results,
            "missing_inputs": missing,
            "source_revision_ids": source_ids,
            "source_rights": rights,
            "limitations": [
                "Callable and structured products are terms-preserving inputs only; no option-adjusted spread model is applied.",
                "Calendar and settlement conventions beyond the declared day-count basis require a provider adapter and independent pricing validation.",
            ],
        }
        payload = {"instrument": terms, "cashflows": cashflow_rows, "quote": quote_data, "curve": curve_rows, "scenarios": list(scenarios or []), "credit_evidence": credit_rows, "source_revision_ids": source_ids}
        return self._persist(namespace, "fixed-income", owner_value, payload, result)

    def fx_commodity(
        self,
        namespace: str,
        *,
        observations: Sequence[Mapping[str, Any]],
        conversion: Mapping[str, Any] | None = None,
        trades: Sequence[Mapping[str, Any]] | None = None,
        roll_schedule: Sequence[Mapping[str, Any]] | None = None,
        research_sources: Sequence[Mapping[str, Any]] | None = None,
        source_entitlements: Sequence[Mapping[str, Any]] | None = None,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner_value = _owner(owner, principal_id, scopes)
        raw = _rows(observations, "observations")
        normalized: list[dict[str, Any]] = []
        for index, row in enumerate(raw):
            asset_type = _text(row.get("asset_type", "fx_spot"), f"observations[{index}].asset_type")
            at_ms = _millis(row.get("at_ms"), f"observations[{index}].at_ms")
            value = _number(row.get("rate", row.get("price")), f"observations[{index}].price")
            base = _text(row.get("base", row.get("base_currency", "")), f"observations[{index}].base")
            quote_currency = _text(row.get("quote", row.get("quote_currency", row.get("currency", ""))), f"observations[{index}].quote")
            original_value = value
            direction = str(row.get("quote_direction", "quote_per_base")).lower()
            if asset_type.startswith("fx"):
                if direction in {"base_per_quote", "reverse", "inverted"}:
                    if value == 0:
                        raise MarketSpecializedError("invalid_request", "an FX quote cannot be zero")
                    value = 1.0 / value
                elif direction not in {"quote_per_base", "direct"}:
                    raise MarketSpecializedError("invalid_request", "unsupported FX quote direction")
            if asset_type.startswith("fx") and value <= 0:
                raise MarketSpecializedError("invalid_request", "an FX rate must be positive")
            normalized.append({"at_ms": at_ms, "asset_type": asset_type, "value": value, "base": base, "quote": quote_currency, "quote_direction": "quote_per_base" if asset_type.startswith("fx") else direction, "original_value": original_value, "original_quote_direction": direction, "venue": row.get("venue"), "contract_id": row.get("contract_id"), "expiry_at_ms": row.get("expiry_at_ms"), "delivery": row.get("delivery"), "source_revision_ids": _source_ids(row)})
        normalized.sort(key=lambda row: (row["at_ms"], str(row.get("contract_id") or "")))
        synthetic: list[dict[str, Any]] = []
        futures = [row for row in normalized if row["asset_type"] in {"commodity_future", "fx_forward"}]
        schedule = sorted(_rows(roll_schedule or [], "roll_schedule", maximum=MAX_SCENARIOS), key=lambda row: _millis(row.get("roll_at_ms"), "roll_schedule.roll_at_ms"))
        if futures and schedule:
            for row in futures:
                active = None
                for roll in schedule:
                    if row["at_ms"] >= _millis(roll.get("roll_at_ms"), "roll_schedule.roll_at_ms"):
                        active = roll.get("contract_id")
                if active is None or active == row.get("contract_id"):
                    synthetic.append({**row, "synthetic": True, "raw_contract_id": row.get("contract_id")})
            synthetic.sort(key=lambda row: row["at_ms"])
        chart_rows = synthetic or [row for row in normalized if row["asset_type"] in {"commodity_spot", "commodity_future", "fx_spot", "fx_forward"}]
        chart_returns: list[float] = []
        for previous, current in zip(chart_rows, chart_rows[1:], strict=False):
            if previous["value"] != 0:
                chart_returns.append(current["value"] / previous["value"] - 1.0)
        pnl: list[dict[str, Any]] = []
        for index, trade in enumerate(_rows(trades or [], "trades", maximum=MAX_SCENARIOS)):
            entry = _number(trade.get("entry_price"), f"trades[{index}].entry_price")
            exit_price = _number(trade.get("exit_price"), f"trades[{index}].exit_price")
            quantity = _number(trade.get("quantity"), f"trades[{index}].quantity")
            direction = 1.0 if trade.get("side", "long") == "long" else -1.0
            multiplier = _number(trade.get("multiplier", 1.0), f"trades[{index}].multiplier", positive=True)
            pnl.append({"trade_id": trade.get("trade_id", f"trade-{index}"), "pnl": direction * (exit_price - entry) * quantity * multiplier, "synthetic": False})
        conversion_result: dict[str, Any] | None = None
        conversion_data = _mapping(conversion or {}, "conversion")
        if conversion_data:
            amount = _number(conversion_data.get("amount"), "conversion.amount")
            rate = _number(conversion_data.get("rate"), "conversion.rate", positive=True)
            direction = str(conversion_data.get("direction", "from_to")).lower()
            converted = amount * rate if direction in {"from_to", "base_to_quote"} else amount / rate
            conversion_result = {"amount": amount, "converted": converted, "from_currency": conversion_data.get("from_currency"), "to_currency": conversion_data.get("to_currency"), "rate": rate, "direction": direction}
        result = {
            "contract": "noesis-market-fx-commodity-analytics-v1",
            "asset_class": "fx_commodity",
            "formula_version": SPECIALIZED_FORMULA_VERSION,
            "status": "complete" if normalized else "no_data",
            "observations": normalized,
            "continuous_series": chart_rows,
            "synthetic_series": bool(synthetic),
            "returns": chart_returns,
            "tradable_pnl": pnl,
            "conversion": conversion_result,
            "research_sources": [dict(row) for row in _rows(research_sources or [], "research_sources", maximum=256)],
            "source_revision_ids": _source_ids(raw, research_sources or []),
            "source_rights": _rights(self.conn, namespace, source_entitlements, principal_id=principal_id, scopes=scopes),
            "limitations": [
                "Continuous series are synthetic chart references; tradable P&L remains tied to raw contract IDs and explicit rolls.",
                "Venue calendars, delivery notices, storage costs and basis require provider-specific contract metadata.",
            ],
        }
        payload = {"observations": raw, "conversion": dict(conversion or {}), "trades": list(trades or []), "roll_schedule": list(roll_schedule or []), "research_sources": list(research_sources or [])}
        return self._persist(namespace, "fx-commodity", owner_value, payload, result)

    def derivatives(
        self,
        namespace: str,
        *,
        contract: Mapping[str, Any],
        quote: Mapping[str, Any] | None = None,
        surface: Sequence[Mapping[str, Any]] | None = None,
        scenarios: Sequence[Mapping[str, Any]] | None = None,
        source_entitlements: Sequence[Mapping[str, Any]] | None = None,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner_value = _owner(owner, principal_id, scopes)
        terms = _mapping(contract, "contract")
        option_type = str(terms.get("option_type", "call")).lower()
        if option_type not in {"call", "put"}:
            raise MarketSpecializedError("invalid_request", "option_type must be call or put")
        exercise = str(terms.get("exercise_style", "european")).lower()
        spot = _number(terms.get("underlying_price"), "contract.underlying_price", positive=True)
        strike = _number(terms.get("strike"), "contract.strike", positive=True)
        time_years = _number(terms.get("time_to_expiry_years"), "contract.time_to_expiry_years", positive=True)
        rate = _number(terms.get("rate", 0.0), "contract.rate")
        dividend = _number(terms.get("dividend_yield", 0.0), "contract.dividend_yield")
        volatility = _number(terms.get("volatility", 0.2), "contract.volatility", positive=True)
        multiplier = _number(terms.get("multiplier", 1.0), "contract.multiplier", positive=True)
        model = _black_scholes(spot, strike, time_years, rate, dividend, volatility, option_type)
        quote_data = _mapping(quote or {}, "quote")
        market_price = None if quote_data.get("price") is None else _number(quote_data.get("price"), "quote.price")
        quote_quality: dict[str, Any] = {"status": "not_provided"}
        if market_price is not None:
            quote_at = quote_data.get("quote_at_ms")
            as_of = quote_data.get("as_of_ms", terms.get("valuation_at_ms"))
            stale_after = quote_data.get("stale_after_ms", terms.get("stale_after_ms"))
            if quote_at is not None and as_of is not None and stale_after is not None:
                quote_at = _millis(quote_at, "quote.quote_at_ms")
                as_of = _millis(as_of, "quote.as_of_ms")
                stale_after = _millis(stale_after, "quote.stale_after_ms")
                quote_quality = {
                    "status": "stale" if as_of - quote_at > stale_after else "fresh",
                    "quote_at_ms": quote_at,
                    "as_of_ms": as_of,
                    "stale_after_ms": stale_after,
                }
            else:
                quote_quality = {"status": "not_assessed"}
        intrinsic = max(0.0, (spot - strike) if option_type == "call" else (strike - spot))
        discounted_upper = spot * math.exp(-dividend * time_years) if option_type == "call" else strike * math.exp(-rate * time_years)
        lower = max(0.0, (spot * math.exp(-dividend * time_years) - strike * math.exp(-rate * time_years)) if option_type == "call" else (strike * math.exp(-rate * time_years) - spot * math.exp(-dividend * time_years)))
        quote_status = "not_provided"
        if market_price is not None:
            if market_price < lower - 1e-8 or market_price > discounted_upper + 1e-8:
                quote_status = "invalid_bounds"
            else:
                quote_status = "valid"
            if quote_quality["status"] == "stale":
                quote_status = "stale"
        implied_vol = None
        if market_price is not None and quote_status == "valid" and exercise == "european":
            low, high = 1e-8, 8.0
            if _black_scholes(spot, strike, time_years, rate, dividend, high, option_type)["price"] < market_price:
                quote_status = "implied_vol_out_of_range"
            else:
                for _ in range(100):
                    mid = (low + high) / 2
                    price = _black_scholes(spot, strike, time_years, rate, dividend, mid, option_type)["price"]
                    if price < market_price:
                        low = mid
                    else:
                        high = mid
                implied_vol = (low + high) / 2
        surface_rows = []
        for index, row in enumerate(_rows(surface or [], "surface", maximum=MAX_ROWS)):
            surface_terms = {**terms, **row}
            surface_quote = {"price": row.get("price")} if row.get("price") is not None else {}
            try:
                surface_result = self.derivatives(namespace, contract=surface_terms, quote=surface_quote, surface=[], scenarios=[], source_entitlements=source_entitlements, owner=owner_value, principal_id=principal_id, scopes=scopes)
                surface_rows.append({"index": index, "status": surface_result.get("quote_status"), "implied_volatility": surface_result.get("implied_volatility")})
            except MarketSpecializedError as exc:
                surface_rows.append({"index": index, "status": exc.code, "message": exc.message})
        scenario_results = []
        for scenario in _rows(scenarios or [], "scenarios", maximum=MAX_SCENARIOS):
            shifted = _black_scholes(spot * (1 + _number(scenario.get("spot_change", 0.0), "scenario.spot_change")), strike, time_years, rate + _number(scenario.get("rate_change", 0.0), "scenario.rate_change"), dividend, max(1e-8, volatility + _number(scenario.get("volatility_change", 0.0), "scenario.volatility_change")), option_type)
            scenario_results.append({"name": _text(scenario.get("name", "scenario"), "scenario.name"), "price": shifted["price"] * multiplier, "price_change": (shifted["price"] - model["price"]) * multiplier})
        source_ids = _source_ids(terms, quote_data, surface or [])
        result = {
            "contract": "noesis-market-derivatives-analytics-v1",
            "asset_class": "derivatives",
            "formula_version": SPECIALIZED_FORMULA_VERSION,
            "status": "unsupported_exercise_style" if exercise not in {"european"} else "complete",
            "contract_terms": {"underlying": terms.get("underlying"), "strike": strike, "expiry_at_ms": terms.get("expiry_at_ms"), "exercise_style": exercise, "settlement_style": terms.get("settlement_style"), "multiplier": multiplier, "corporate_action_adjustment": terms.get("corporate_action_adjustment")},
            "model": {key: value * multiplier if key in {"price", "delta", "gamma", "vega", "theta", "rho"} else value for key, value in model.items()},
            "implied_volatility": implied_vol,
            "quote_status": quote_status,
            "quote_quality": quote_quality,
            "no_arbitrage_bounds": {"lower": lower, "upper": discounted_upper, "intrinsic": intrinsic},
            "surface": surface_rows,
            "scenarios": scenario_results,
            "source_revision_ids": source_ids,
            "source_rights": _rights(self.conn, namespace, source_entitlements, principal_id=principal_id, scopes=scopes),
            "limitations": [
                "The local model is Black-Scholes European valuation; American, barrier, Asian, structured and early-exercise products are not priced.",
                "Rates, dividends, corporate actions and quotes must be supplied with dated source revisions by a provider adapter.",
            ],
        }
        payload = {"contract": terms, "quote": quote_data, "surface": list(surface or []), "scenarios": list(scenarios or [])}
        return self._persist(namespace, "derivatives", owner_value, payload, result)

    def digital_asset(
        self,
        namespace: str,
        *,
        asset: Mapping[str, Any],
        observations: Sequence[Mapping[str, Any]],
        supply_events: Sequence[Mapping[str, Any]] | None = None,
        chain_events: Sequence[Mapping[str, Any]] | None = None,
        source_entitlements: Sequence[Mapping[str, Any]] | None = None,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner_value = _owner(owner, principal_id, scopes)
        identity = _mapping(asset, "asset")
        chain_id = _text(identity.get("chain_id"), "asset.chain_id")
        address_value = identity.get("contract_address", identity.get("address"))
        if address_value in (None, ""):
            native_id = _text(
                identity.get("native_asset_id", identity.get("symbol")),
                "asset.native_asset_id",
            ).lower()
            address = None
            asset_kind = "native"
            asset_id = f"{chain_id}:native:{native_id}"
        else:
            address = _text(address_value, "asset.contract_address").lower()
            asset_kind = "contract"
            asset_id = f"{chain_id}:contract:{address}"
        wrapped_asset_of = identity.get("wrapped_asset_of")
        if wrapped_asset_of not in (None, "") and asset_kind != "contract":
            raise MarketSpecializedError(
                "invalid_request", "a wrapped asset must have a contract address"
            )
        normalized_identity = {"asset_id": asset_id, "asset_kind": asset_kind, "chain_id": chain_id, "contract_address": address, "native_asset_id": native_id if asset_kind == "native" else None, "symbol": identity.get("symbol"), "wrapped_asset_of": wrapped_asset_of, "venue_scope": identity.get("venue_scope")}
        prices = _rows(observations, "observations")
        normalized_prices = []
        by_time: defaultdict[int, list[float]] = defaultdict(list)
        for index, row in enumerate(prices):
            at = _millis(row.get("at_ms"), f"observations[{index}].at_ms")
            price = _number(row.get("price"), f"observations[{index}].price", positive=True)
            venue = _text(row.get("venue"), f"observations[{index}].venue")
            normalized_prices.append({"at_ms": at, "price": price, "venue": venue, "volume": row.get("volume"), "source_revision_ids": _source_ids(row)})
            by_time[at].append(price)
        disagreements = []
        for at, values in sorted(by_time.items()):
            if len(values) > 1:
                disagreement = (max(values) - min(values)) / max(statistics.mean(values), 1e-12)
                disagreements.append({"at_ms": at, "relative_disagreement": disagreement, "status": "disputed" if disagreement > float(identity.get("disagreement_tolerance", 0.02)) else "within_tolerance"})
        supply = _number(identity.get("initial_supply", 0.0), "asset.initial_supply")
        supply_history = [{"at_ms": 0, "supply": supply}]
        supply_rows = _rows(supply_events or [], "supply_events", maximum=MAX_ROWS)
        for index, event in enumerate(sorted(supply_rows, key=lambda row: _millis(row.get("at_ms"), "supply_events.at_ms"))):
            delta = _number(event.get("delta", 0.0), f"supply_events[{index}].delta")
            if event.get("event_type") == "redenomination":
                factor = _number(event.get("factor"), f"supply_events[{index}].factor", positive=True)
                supply *= factor
            else:
                supply += delta
            if supply < 0:
                raise MarketSpecializedError("invalid_request", "supply cannot become negative")
            supply_history.append({"at_ms": _millis(event.get("at_ms"), f"supply_events[{index}].at_ms"), "supply": supply, "event_type": event.get("event_type")})
        blocks: dict[int, str] = {}
        reorgs = []
        for index, event in enumerate(_rows(chain_events or [], "chain_events", maximum=MAX_ROWS)):
            block = event.get("block_number")
            block_hash = _text(event.get("block_hash"), f"chain_events[{index}].block_hash")
            if type(block) is not int or block < 0:
                raise MarketSpecializedError("invalid_request", "block_number must be nonnegative integer")
            previous = blocks.get(block)
            if previous is not None and previous != block_hash:
                reorgs.append({"block_number": block, "previous_hash": previous, "replacement_hash": block_hash, "recovered": bool(event.get("canonical", False))})
            blocks[block] = block_hash
        volumes = [_number(row["volume"], "observations.volume") for row in normalized_prices if row.get("volume") is not None]
        disputed = sum(1 for row in disagreements if row["status"] == "disputed")
        reliability = max(0.0, min(1.0, (1.0 if normalized_prices else 0.0) * (1.0 - min(1.0, disputed / max(1, len(disagreements))) * 0.5) * (1.0 if not reorgs or all(row["recovered"] for row in reorgs) else 0.5)))
        result = {
            "contract": "noesis-market-digital-asset-analysis-v1",
            "asset_class": "digital_asset",
            "formula_version": SPECIALIZED_FORMULA_VERSION,
            "status": "complete" if normalized_identity and normalized_prices else "no_data",
            "identity": normalized_identity,
            "calendar": {"type": "continuous_24_7", "holidays": []},
            "observations": normalized_prices,
            "price_disagreements": disagreements,
            "supply_history": supply_history,
            "current_supply": supply,
            "chain_reorganizations": reorgs,
            "liquidity": {"observations": len(volumes), "volume_sum": sum(volumes) if volumes else None, "source_reliability": reliability},
            "source_revision_ids": _source_ids(identity, prices, supply_events or [], chain_events or []),
            "source_rights": _rights(self.conn, namespace, source_entitlements, principal_id=principal_id, scopes=scopes),
            "limitations": [
                "Chain-specific finality, indexer completeness, bridge semantics and protocol evidence remain adapter inputs.",
                "Liquidity is a source-observation indicator, not an executable venue guarantee.",
            ],
        }
        payload = {"asset": identity, "observations": prices, "supply_events": list(supply_events or []), "chain_events": list(chain_events or [])}
        return self._persist(namespace, "digital-asset", owner_value, payload, result)

    def intraday_replay(
        self,
        namespace: str,
        *,
        events: Sequence[Mapping[str, Any]],
        recovered_events: Sequence[Mapping[str, Any]] | None = None,
        entitlement_tier: str = "delayed",
        target_events_per_second: float | None = None,
        source_entitlements: Sequence[Mapping[str, Any]] | None = None,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner_value = _owner(owner, principal_id, scopes)
        raw = _rows(events, "events")
        recovered = _rows(recovered_events or [], "recovered_events")
        combined = [(row, False) for row in raw] + [(row, True) for row in recovered]
        seen_sequences: dict[int, dict[str, Any]] = {}
        out_of_order = 0
        duplicate_sequences = 0
        last_exchange_at = -1
        corrections = 0
        latencies = []
        raw_sequences: set[int] = set()
        recovered_input_sequences: set[int] = set()
        for index, (row, is_recovered) in enumerate(combined):
            sequence = row.get("sequence")
            if type(sequence) is not int or sequence < 0:
                raise MarketSpecializedError("invalid_request", f"events[{index}].sequence must be nonnegative integer")
            if is_recovered:
                recovered_input_sequences.add(sequence)
            else:
                raw_sequences.add(sequence)
            exchange_at = _millis(row.get("exchange_at_ms"), f"events[{index}].exchange_at_ms")
            received_at = _millis(row.get("received_at_ms"), f"events[{index}].received_at_ms")
            if exchange_at < last_exchange_at:
                out_of_order += 1
            last_exchange_at = max(last_exchange_at, exchange_at)
            if row.get("correction_of") is not None:
                corrections += 1
            if sequence in seen_sequences:
                duplicate_sequences += 1
            latencies.append(max(0, received_at - exchange_at))
            event = {"sequence": sequence, "exchange_at_ms": exchange_at, "received_at_ms": received_at, "event_type": row.get("event_type", "trade"), "price": row.get("price"), "size": row.get("size"), "revision_id": row.get("revision_id"), "correction_of": row.get("correction_of"), "recovered": is_recovered}
            seen_sequences[sequence] = event
        ordered = [seen_sequences[key] for key in sorted(seen_sequences)]
        detected_gaps = []
        for left, right in zip(sorted(raw_sequences), sorted(raw_sequences)[1:], strict=False):
            if right > left + 1:
                detected_gaps.extend(range(left + 1, right))
        recovered_sequences = sorted(set(detected_gaps).intersection(recovered_input_sequences))
        unrecovered = sorted(set(detected_gaps) - set(seen_sequences))
        buckets: defaultdict[int, int] = defaultdict(int)
        for row in ordered:
            buckets[row["exchange_at_ms"] // 1000] += 1
        max_rate = max(buckets.values(), default=0)
        target = None if target_events_per_second is None else _number(target_events_per_second, "target_events_per_second", positive=True)
        backpressure = "not_assessed" if target is None else ("pass" if max_rate <= target else "degraded")
        replay_hash = _digest(ordered)
        result = {
            "contract": "noesis-market-intraday-replay-v1",
            "asset_class": "intraday",
            "formula_version": SPECIALIZED_FORMULA_VERSION,
            "status": "complete" if ordered and not unrecovered else ("degraded" if ordered else "no_data"),
            "entitlement": {"tier": _text(entitlement_tier, "entitlement_tier"), "real_time": entitlement_tier.lower() in {"real_time", "realtime"}},
            "events": ordered,
            "replay": {"replay_hash": replay_hash, "input_count": len(raw), "recovered_count": len(recovered), "gaps": detected_gaps, "recovered_sequences": recovered_sequences, "unrecovered_sequences": unrecovered, "bounded": True},
            "quality": {"out_of_order_count": out_of_order, "duplicate_sequence_count": duplicate_sequences, "correction_count": corrections, "p95_latency_ms": _percentile(latencies, 0.95), "max_events_per_second": max_rate, "backpressure": backpressure},
            "source_revision_ids": _source_ids(raw, recovered),
            "source_rights": _rights(self.conn, namespace, source_entitlements, principal_id=principal_id, scopes=scopes),
            "limitations": [
                "A local replay validates ordering and recovery semantics only; exchange reconnect, capacity, retention cost and real-time rights require deployment evidence.",
                "Events are retained as normalized receipt data; provider-specific raw payloads remain an adapter responsibility.",
            ],
        }
        payload = {"events": raw, "recovered_events": recovered, "entitlement_tier": entitlement_tier, "target_events_per_second": target_events_per_second}
        return self._persist(namespace, "intraday", owner_value, payload, result)

    def international_coverage(
        self,
        namespace: str,
        *,
        markets: Sequence[Mapping[str, Any]],
        taxonomy_mappings: Sequence[Mapping[str, Any]] | None = None,
        cross_listings: Sequence[Mapping[str, Any]] | None = None,
        translations: Sequence[Mapping[str, Any]] | None = None,
        source_entitlements: Sequence[Mapping[str, Any]] | None = None,
        owner: str | None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _authorize(namespace, principal_id, scopes, write=True)
        owner_value = _owner(owner, principal_id, scopes)
        market_rows = _rows(markets, "markets", maximum=256)
        matrix = []
        for index, row in enumerate(market_rows):
            market_id = _text(row.get("market_id"), f"markets[{index}].market_id")
            required = {"provider", "calendar", "currency", "history", "filings", "accounting_standard"}
            missing = sorted(field for field in required if row.get(field) in (None, "", []))
            matrix.append({"market_id": market_id, "provider": row.get("provider"), "calendar": row.get("calendar"), "currency": row.get("currency"), "history": row.get("history"), "filings": row.get("filings"), "accounting_standard": row.get("accounting_standard"), "state": "ready_for_live_review" if not missing else "fixture_or_credential_blocked", "missing": missing, "limitations": row.get("limitations", [])})
        mappings = _rows(taxonomy_mappings or [], "taxonomy_mappings", maximum=MAX_ROWS)
        mapping_warnings = [row for row in mappings if not row.get("source_revision_id") or not row.get("target_concept")]
        listings = _rows(cross_listings or [], "cross_listings", maximum=MAX_ROWS)
        translation_rows = _rows(translations or [], "translations", maximum=MAX_ROWS)
        result = {
            "contract": "noesis-market-international-coverage-v1",
            "asset_class": "international_equities",
            "formula_version": SPECIALIZED_FORMULA_VERSION,
            "status": "complete" if matrix and not any(row["missing"] for row in matrix) else "partial",
            "coverage_matrix": matrix,
            "taxonomy_mappings": mappings,
            "mapping_warnings": mapping_warnings,
            "cross_listings": listings,
            "translations": translation_rows,
            "source_revision_ids": _source_ids(market_rows, mappings, listings, translation_rows),
            "source_rights": _rights(self.conn, namespace, source_entitlements, principal_id=principal_id, scopes=scopes),
            "limitations": [
                "Withholding, corporate-action and local filing behavior must be supplied per venue; missing fields remain visible rather than inferred.",
                "A complete local matrix is not evidence of licensed provider coverage or analyst validation.",
            ],
        }
        payload = {"markets": market_rows, "taxonomy_mappings": mappings, "cross_listings": listings, "translations": translation_rows}
        return self._persist(namespace, "international-coverage", owner_value, payload, result)


__all__ = [
    "MarketSpecializedError",
    "MarketSpecializedStore",
    "SPECIALIZED_READ_SCOPE",
    "SPECIALIZED_WRITE_SCOPE",
    "SPECIALIZED_FORMULA_VERSION",
    "ensure_market_specialized_schema",
]
