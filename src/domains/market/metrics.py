"""Versioned point-in-time market returns and reported-fact metrics.

Market metrics use the existing market source stores for authorization and
revision selection, the corporate-action service for raw daily prices, and the
shared quantitative ledger for durable calculation lineage. Formula versions
are deliberately explicit in each returned metric so a later implementation
can supersede a formula without changing old receipts.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.domains.market.quality import summarize_input_quality

MARKET_METRICS_FORMULA_VERSION = "noesis-market-metrics-v1"
PRICE_FORMULAS = {
    "price_return": "noesis-market-price-return-v1",
    "total_return": "noesis-market-total-return-v1",
    "annualized_volatility": "noesis-market-volatility-sample-v1",
    "maximum_drawdown": "noesis-market-drawdown-peak-to-trough-v1",
    "correlation": "noesis-market-correlation-inner-join-v1",
    "beta": "noesis-market-beta-inner-join-v1",
    "sharpe_ratio": "noesis-market-sharpe-annual-risk-free-v1",
}
FACT_FORMULAS = {
    "growth": "noesis-market-growth-yoy-v1",
    "margin": "noesis-market-margin-ratio-v1",
    "leverage": "noesis-market-leverage-ratio-v1",
    "liquidity": "noesis-market-liquidity-ratio-v1",
    "valuation_multiple": "noesis-market-valuation-multiple-v1",
}
MAX_PRICE_ROWS = 20_000
MAX_FACT_REQUESTS = 200
MAX_METRIC_NAME = 120
_DAY_MS = 86_400_000


class MarketMetricError(ValueError):
    """A typed market metric request, source, or formula failure."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


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
        raise MarketMetricError("invalid_request", "request must be JSON-safe") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MarketMetricError("invalid_number", f"{field} must be decimal") from exc
    if not result.is_finite():
        raise MarketMetricError("invalid_number", f"{field} must be finite")
    return result


def _text(value: Any, field: str, *, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MarketMetricError("invalid_request", f"{field} must be bounded text")
    return value.strip()


def _millis(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise MarketMetricError(
            "invalid_request", f"{field} must be epoch milliseconds"
        )
    return value


def _decimal_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    with localcontext() as context:
        context.prec = max(50, len(value.as_tuple().digits) + 16)
        rounded = value.quantize(Decimal("0.000000000001"))
    text = format(rounded, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _unavailable(
    name: str, formula: str, reason: str, *, unit: str = "ratio"
) -> dict[str, Any]:
    return {
        "name": name,
        "value": None,
        "unit": unit,
        "status": "unavailable",
        "reason_code": reason,
        "formula_version": formula,
    }


@lru_cache(maxsize=1)
def _report_validator():
    from jsonschema import Draft7Validator, FormatChecker

    path = (
        Path(__file__).resolve().parents[3]
        / "contracts/schemas/jsonschema/noesis-market-metric-report-v1.json"
    )
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema, format_checker=FormatChecker())


def _validate_report(payload: Mapping[str, Any]) -> None:
    errors = sorted(
        _report_validator().iter_errors(payload),
        key=lambda error: (tuple(str(part) for part in error.path), error.message),
    )
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.path) or "$"
        raise MarketMetricError(
            "contract_invalid",
            f"noesis-market-metric-report-v1 rejected {location}: {first.message}",
            path=location,
        )


def _available(
    name: str,
    value: Decimal,
    unit: str,
    formula: str,
    **details: Any,
) -> dict[str, Any]:
    return {
        "name": name,
        "value": _decimal_text(value),
        "unit": unit,
        "status": "available",
        "formula_version": formula,
        **details,
    }


def _sample_std(values: Sequence[Decimal]) -> Decimal | None:
    if len(values) < 2:
        return None
    mean = sum(values, Decimal(0)) / Decimal(len(values))
    variance = sum(((value - mean) ** 2 for value in values), Decimal(0)) / Decimal(
        len(values) - 1
    )
    if variance < 0:
        return None
    return variance.sqrt()


class MarketMetricStore:
    """Point-in-time financial metrics built over existing market stores."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            # Metrics add no second source or calculation ledger. They rely on
            # the existing stores' additive schemas and shared quant tables.
            from src.domains.market.actions import ensure_market_action_schema
            from src.domains.market.financial_facts import (
                ensure_market_financial_fact_schema,
            )
            from src.domains.market.instruments import ensure_market_instrument_schema
            from src.domains.market.prices import ensure_market_price_schema

            ensure_market_instrument_schema(conn)
            ensure_market_price_schema(conn)
            ensure_market_action_schema(conn)
            ensure_market_financial_fact_schema(conn)

    @staticmethod
    def _authorize_calculation(
        namespace: str, principal_id: str, scopes: set[str]
    ) -> None:
        _text(namespace, "namespace", limit=100)
        _text(principal_id, "principal_id", limit=200)
        if "operator" not in scopes:
            from src.kb.quantitative import CALCULATE_SCOPE

            if CALCULATE_SCOPE not in scopes:
                raise MarketMetricError(
                    "unauthorized", "quantitative calculation access is required"
                )

    def _derive_sources(
        self,
        namespace: str,
        source_refs: Sequence[Mapping[str, Any]],
        *,
        principal_id: str,
        scopes: set[str],
    ) -> list[dict[str, Any]]:
        from src.domains.market.entitlements import (
            MarketEntitlementError,
            authorize_market_sources,
        )

        try:
            return authorize_market_sources(
                self.conn,
                namespace,
                [dict(ref) for ref in source_refs],
                operation="derive",
                principal_id=principal_id,
                scopes=scopes,
                now_ms=_millis(self.now(), "now_ms"),
            )
        except MarketEntitlementError as exc:
            raise MarketMetricError(exc.code, exc.message, **exc.details) from exc

    @staticmethod
    def _price_scope(namespace: str, principal_id: str, scopes: set[str]) -> None:
        from src.domains.market.prices import MarketPriceError, MarketPriceStore

        try:
            MarketPriceStore._authorize(namespace, principal_id, scopes, write=False)
        except MarketPriceError as exc:
            raise MarketMetricError(exc.code, exc.message, **exc.details) from exc

    def _daily_returns(
        self,
        namespace: str,
        listing_id: str,
        *,
        start_ms: int,
        end_ms: int,
        acquired_by_ms: int,
        public_cutoff_ms: int,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        from src.domains.market.actions import (
            MarketActionError,
            MarketCorporateActionStore,
        )
        from src.domains.market.instruments import (
            MarketInstrumentError,
            MarketInstrumentStore,
        )
        from src.domains.market.prices import MarketPriceError, MarketPriceStore

        instrument_store = MarketInstrumentStore(self.conn, initialize=False)
        price_store = MarketPriceStore(self.conn, initialize=False, now=self.now)
        try:
            listing = instrument_store.get_instrument(
                namespace,
                "listing",
                listing_id,
                acquired_by_ms=acquired_by_ms,
                publicly_available_by_ms=public_cutoff_ms,
                principal_id=principal_id,
                scopes=scopes,
            )
            security = instrument_store.get_instrument(
                namespace,
                "security",
                listing["security_id"],
                acquired_by_ms=acquired_by_ms,
                publicly_available_by_ms=public_cutoff_ms,
                principal_id=principal_id,
                scopes=scopes,
            )
            price_page = price_store.get_bars(
                namespace,
                listing_id,
                start_ms=start_ms,
                end_ms=end_ms,
                acquired_by_ms=acquired_by_ms,
                publicly_available_by_ms=public_cutoff_ms,
                principal_id=principal_id,
                scopes=scopes,
                limit=MAX_PRICE_ROWS,
                include_page=True,
            )
        except (MarketInstrumentError, MarketPriceError) as exc:
            raise MarketMetricError(exc.code, exc.message, **exc.details) from exc
        identity_refs = list(listing.get("source_refs", [])) + list(
            security.get("source_refs", [])
        )
        identity_decisions = self._derive_sources(
            namespace,
            identity_refs,
            principal_id=principal_id,
            scopes=scopes,
        )
        identity_decisions_by_ref = {
            item["source_ref_id"]: item for item in identity_decisions
        }
        identity_revision_ids = [listing["revision_id"], security["revision_id"]]
        identity_revision_ids.extend(
            str(ref["source_revision_id"])
            for ref in identity_refs
            if ref.get("source_revision_id")
        )
        entitlement_decisions = [
            {
                "source_ref_id": str(ref["source_ref_id"]),
                "provider": str(ref["provider"]),
                "license_id": str(ref["license_id"]),
                "entitlement_id": str(ref["entitlement_id"]),
                "policy_revision_id": identity_decisions_by_ref[
                    str(ref["source_ref_id"])
                ]["policy_revision_id"],
                "authorized_operation": "derive",
            }
            for ref in identity_refs
        ]
        raw_bars = list(price_page.get("items", []))
        bars = [bar for bar in raw_bars if bar.get("interval") == "1d"]
        if not bars:
            raise MarketMetricError(
                "price_history_unavailable", "no accessible daily bars cover this range"
            )
        if len(bars) >= MAX_PRICE_ROWS:
            raise MarketMetricError(
                "range_too_large",
                "daily history reaches the row bound and may be truncated; narrow the date range",
            )
        if any(bar.get("currency") != listing["currency"] for bar in bars):
            raise MarketMetricError(
                "currency_mismatch", "bar currency differs from listing currency"
            )
        bases = {str(bar.get("price_basis")) for bar in bars}
        if len(bases) != 1:
            raise MarketMetricError(
                "mixed_price_basis", "price history mixes adjustment bases"
            )
        basis = next(iter(bases))
        action_ids: list[str] = []
        series_by_date: dict[str, dict[str, Any]] = {}
        if basis == "unadjusted":
            try:
                adjusted = MarketCorporateActionStore(
                    self.conn, initialize=False, now=self.now
                ).calculate_adjusted_series(
                    namespace,
                    listing["security_id"],
                    listing_id,
                    bars,
                    listing_timezone=str(listing.get("timezone") or "UTC"),
                    acquired_by_ms=acquired_by_ms,
                    publicly_available_by_ms=public_cutoff_ms,
                    principal_id=principal_id,
                    scopes=scopes,
                )
            except MarketActionError as exc:
                raise MarketMetricError(exc.code, exc.message, **exc.details) from exc
            action_ids = list(adjusted["action_revision_ids"])
            entitlement_decisions.extend(adjusted["source_entitlements"])
            for row in adjusted["series"]:
                series_by_date[row["session_date"]] = {
                    "date": row["session_date"],
                    "price_close": _decimal(
                        row["split_adjusted_close"], "split_adjusted_close"
                    ),
                    "total_close": _decimal(
                        row["total_return_adjusted_close"],
                        "total_return_adjusted_close",
                    ),
                    "bar_revision_id": row["bar_revision_id"],
                }
        elif basis in {"split_adjusted", "total_return_adjusted"}:
            try:
                venue_timezone = ZoneInfo(str(listing.get("timezone") or "UTC"))
            except ZoneInfoNotFoundError as exc:
                raise MarketMetricError(
                    "invalid_listing_timezone",
                    "listing timezone is not a known IANA zone",
                ) from exc
            refs = [ref for bar in bars for ref in bar.get("source_refs", [])]
            decisions = self._derive_sources(
                namespace,
                refs,
                principal_id=principal_id,
                scopes=scopes,
            )
            decision_by_ref = {item["source_ref_id"]: item for item in decisions}
            for bar in bars:
                session_date = datetime.fromtimestamp(
                    bar["bar_start_ms"] / 1000, tz=venue_timezone
                ).date()
                date_key = session_date.isoformat()
                if date_key in series_by_date:
                    raise MarketMetricError(
                        "duplicate_session",
                        "multiple price bars map to one local session date",
                    )
                close = _decimal(bar.get("close"), "close")
                if close <= 0:
                    raise MarketMetricError(
                        "nonpositive_close", "adjusted price closes must be positive"
                    )
                adjustment_cutoff = bar.get("adjustment_cutoff_ms")
                if (
                    not bar.get("adjustment_method")
                    or not bar.get("adjustment_calculation_id")
                    or type(adjustment_cutoff) is not int
                    or adjustment_cutoff > public_cutoff_ms
                ):
                    raise MarketMetricError(
                        "adjustment_provenance_unavailable",
                        "provider-adjusted bars require a method and calculation reference whose adjustment cutoff is no later than the public cutoff",
                    )
                action_ids.extend(
                    str(item) for item in bar.get("adjustment_action_revision_ids", [])
                )
                series_by_date[date_key] = {
                    "date": date_key,
                    "price_close": close if basis == "split_adjusted" else None,
                    "total_close": close if basis == "total_return_adjusted" else None,
                    "bar_revision_id": bar["revision_id"],
                }
                for ref in bar.get("source_refs", []):
                    decision = decision_by_ref.get(str(ref.get("source_ref_id")))
                    if decision:
                        entitlement_decisions.append(
                            {
                                "source_ref_id": str(ref["source_ref_id"]),
                                "provider": str(ref["provider"]),
                                "license_id": str(ref["license_id"]),
                                "entitlement_id": str(ref["entitlement_id"]),
                                "policy_revision_id": decision["policy_revision_id"],
                                "authorized_operation": "derive",
                            }
                        )
        else:
            raise MarketMetricError(
                "unsupported_price_basis", "daily adjusted or raw prices are required"
            )

        ordered = [series_by_date[key] for key in sorted(series_by_date)]
        if len(ordered) < 2:
            raise MarketMetricError(
                "insufficient_history", "at least two daily prices are required"
            )
        for row in ordered:
            for field in ("price_close", "total_close"):
                if row[field] is not None and row[field] <= 0:
                    raise MarketMetricError(
                        "nonpositive_close", "price denominators must be positive"
                    )
        daily = []
        for previous, current in zip(ordered, ordered[1:]):
            entry = {
                "period": current["date"],
                "period_start": previous["date"],
                "period_end": current["date"],
                "input_revision_ids": [
                    previous["bar_revision_id"],
                    current["bar_revision_id"],
                ],
                "calendar_days_elapsed": (
                    date.fromisoformat(current["date"])
                    - date.fromisoformat(previous["date"])
                ).days,
            }
            for close_key, return_key in (
                ("price_close", "price_return"),
                ("total_close", "total_return"),
            ):
                if previous[close_key] is None or current[close_key] is None:
                    entry[return_key] = None
                else:
                    entry[return_key] = (
                        current[close_key] / previous[close_key]
                    ) - Decimal(1)
            daily.append(entry)
        return {
            "listing": listing,
            "bars": bars,
            "series": ordered,
            "daily_returns": daily,
            "action_revision_ids": action_ids,
            "entitlement_decisions": entitlement_decisions,
            "identity_revision_ids": identity_revision_ids,
            "price_basis": basis,
            "quality_exclusions": list(price_page.get("quality_exclusions", [])),
        }

    def calculate_price_metrics(
        self,
        namespace: str,
        listing_id: str,
        *,
        start_ms: int,
        end_ms: int,
        acquired_by_ms: int,
        publicly_available_by_ms: int,
        principal_id: str,
        scopes: set[str],
        benchmark_listing_id: str | None = None,
        periods_per_year: int = 252,
        risk_free_rate_annual: Any | None = None,
    ) -> dict[str, Any]:
        """Calculate daily price and total returns from an as-of daily history.

        Raw bars are adjusted through the existing corporate-action engine.
        Provider-adjusted bars retain only the return family their explicit
        `price_basis` supports. Benchmark returns use an exact inner join of
        local session-date keys, without forward filling or FX conversion.
        """

        self._authorize_calculation(namespace, principal_id, scopes)
        self._price_scope(namespace, principal_id, scopes)
        start_ms, end_ms = _millis(start_ms, "start_ms"), _millis(end_ms, "end_ms")
        acquired_by_ms = _millis(acquired_by_ms, "acquired_by_ms")
        public_cutoff_ms = _millis(publicly_available_by_ms, "publicly_available_by_ms")
        if start_ms >= end_ms or public_cutoff_ms > acquired_by_ms:
            raise MarketMetricError(
                "invalid_request", "date range or as-of cutoffs are invalid"
            )
        if type(periods_per_year) is not int or not 1 <= periods_per_year <= 1000:
            raise MarketMetricError(
                "invalid_request", "periods_per_year must be between 1 and 1000"
            )
        if benchmark_listing_id == listing_id:
            raise MarketMetricError(
                "invalid_request", "benchmark listing must differ from subject"
            )
        if risk_free_rate_annual is not None:
            rf = _decimal(risk_free_rate_annual, "risk_free_rate_annual")
            if rf <= -1:
                raise MarketMetricError(
                    "invalid_request", "annual risk-free rate must exceed -1"
                )
        else:
            rf = None

        with localcontext() as context:
            context.prec = 50
            subject = self._daily_returns(
                namespace,
                _text(listing_id, "listing_id", limit=200),
                start_ms=start_ms,
                end_ms=end_ms,
                acquired_by_ms=acquired_by_ms,
                public_cutoff_ms=public_cutoff_ms,
                principal_id=principal_id,
                scopes=scopes,
            )
            benchmark = None
            if benchmark_listing_id is not None:
                benchmark = self._daily_returns(
                    namespace,
                    _text(benchmark_listing_id, "benchmark_listing_id", limit=200),
                    start_ms=start_ms,
                    end_ms=end_ms,
                    acquired_by_ms=acquired_by_ms,
                    public_cutoff_ms=public_cutoff_ms,
                    principal_id=principal_id,
                    scopes=scopes,
                )
                if subject["listing"]["currency"] != benchmark["listing"]["currency"]:
                    raise MarketMetricError(
                        "currency_conversion_unsupported",
                        "benchmark comparison requires matching listing currencies; no FX conversion was supplied",
                    )

            metrics: list[dict[str, Any]] = []
            daily = subject["daily_returns"]
            for close_key, metric_name, return_key in (
                ("price_close", "price_return", "price_return"),
                ("total_close", "total_return", "total_return"),
            ):
                closes = [row[close_key] for row in subject["series"]]
                if any(value is None for value in closes):
                    metrics.append(
                        _unavailable(
                            metric_name,
                            PRICE_FORMULAS[metric_name],
                            "price_basis_unavailable",
                        )
                    )
                else:
                    metrics.append(
                        _available(
                            metric_name,
                            (closes[-1] / closes[0]) - Decimal(1),
                            "ratio",
                            PRICE_FORMULAS[metric_name],
                            observation_count=len(daily),
                            convention="end_adjusted_close / start_adjusted_close - 1",
                        )
                    )

            volatility_values = [
                row["total_return"] for row in daily if row["total_return"] is not None
            ]
            volatility = _sample_std(volatility_values)
            if volatility is None:
                metrics.append(
                    _unavailable(
                        "annualized_volatility",
                        PRICE_FORMULAS["annualized_volatility"],
                        "insufficient_return_observations",
                        unit="ratio_per_sqrt_year",
                    )
                )
            else:
                annualized_volatility = volatility * Decimal(periods_per_year).sqrt()
                metrics.append(
                    _available(
                        "annualized_volatility",
                        annualized_volatility,
                        "ratio_per_sqrt_year",
                        PRICE_FORMULAS["annualized_volatility"],
                        observation_count=len(volatility_values),
                        periods_per_year=periods_per_year,
                        estimator="sample_standard_deviation",
                    )
                )

            total_closes = [row["total_close"] for row in subject["series"]]
            if any(value is None for value in total_closes):
                metrics.append(
                    _unavailable(
                        "maximum_drawdown",
                        PRICE_FORMULAS["maximum_drawdown"],
                        "total_return_basis_unavailable",
                    )
                )
            else:
                peak = total_closes[0]
                max_drawdown = Decimal(0)
                for close in total_closes:
                    peak = max(peak, close)
                    max_drawdown = min(max_drawdown, close / peak - Decimal(1))
                metrics.append(
                    _available(
                        "maximum_drawdown",
                        max_drawdown,
                        "ratio",
                        PRICE_FORMULAS["maximum_drawdown"],
                        observation_count=len(total_closes),
                        convention="minimum(adjusted_close / prior_running_peak - 1)",
                    )
                )

            paired_returns: list[tuple[Decimal, Decimal]] = []
            common_dates: list[str] = []
            if benchmark is not None:
                subject_by_date = {
                    (item["period_start"], item["period_end"]): item["total_return"]
                    for item in daily
                }
                benchmark_by_date = {
                    (item["period_start"], item["period_end"]): item["total_return"]
                    for item in benchmark["daily_returns"]
                }
                common_dates = sorted(subject_by_date.keys() & benchmark_by_date.keys())
                paired_returns = [
                    (subject_by_date[day], benchmark_by_date[day])
                    for day in common_dates
                    if subject_by_date[day] is not None
                    and benchmark_by_date[day] is not None
                ]
                if len(paired_returns) < 2:
                    metrics.extend(
                        [
                            _unavailable(
                                "correlation",
                                PRICE_FORMULAS["correlation"],
                                "insufficient_aligned_returns",
                            ),
                            _unavailable(
                                "beta",
                                PRICE_FORMULAS["beta"],
                                "insufficient_aligned_returns",
                            ),
                        ]
                    )
                else:
                    subject_values = [item[0] for item in paired_returns]
                    benchmark_values = [item[1] for item in paired_returns]
                    subject_std = _sample_std(subject_values)
                    benchmark_std = _sample_std(benchmark_values)
                    if benchmark_std is None or not benchmark_std:
                        metrics.extend(
                            [
                                _unavailable(
                                    "correlation",
                                    PRICE_FORMULAS["correlation"],
                                    "zero_variance",
                                ),
                                _unavailable(
                                    "beta",
                                    PRICE_FORMULAS["beta"],
                                    "zero_benchmark_variance",
                                ),
                            ]
                        )
                    else:
                        mean_subject = sum(subject_values, Decimal(0)) / Decimal(
                            len(paired_returns)
                        )
                        mean_benchmark = sum(benchmark_values, Decimal(0)) / Decimal(
                            len(paired_returns)
                        )
                        covariance = sum(
                            (
                                (left - mean_subject) * (right - mean_benchmark)
                                for left, right in paired_returns
                            ),
                            Decimal(0),
                        ) / Decimal(len(paired_returns) - 1)
                        if subject_std is None or not subject_std:
                            metrics.append(
                                _unavailable(
                                    "correlation",
                                    PRICE_FORMULAS["correlation"],
                                    "zero_subject_variance",
                                )
                            )
                        else:
                            metrics.append(
                                _available(
                                    "correlation",
                                    covariance / (subject_std * benchmark_std),
                                    "ratio",
                                    PRICE_FORMULAS["correlation"],
                                    observation_count=len(paired_returns),
                                    alignment="exact inner join on listing-local start and end session dates; no forward fill",
                                )
                            )
                        metrics.append(
                            _available(
                                "beta",
                                covariance / (benchmark_std**2),
                                "ratio",
                                PRICE_FORMULAS["beta"],
                                observation_count=len(paired_returns),
                                alignment="exact inner join on listing-local start and end session dates; no forward fill",
                            )
                        )

            if rf is not None:
                if volatility is None:
                    metrics.append(
                        _unavailable(
                            "sharpe_ratio",
                            PRICE_FORMULAS["sharpe_ratio"],
                            "insufficient_return_observations",
                        )
                    )
                elif not volatility:
                    metrics.append(
                        _unavailable(
                            "sharpe_ratio",
                            PRICE_FORMULAS["sharpe_ratio"],
                            "zero_return_volatility",
                        )
                    )
                else:
                    # Geometric conversion maps an annual effective rate to one
                    # observation interval and avoids a linear approximation.
                    period_rf = (
                        (Decimal(1) + rf).ln() / Decimal(periods_per_year)
                    ).exp() - Decimal(1)
                    mean_return = sum(volatility_values, Decimal(0)) / Decimal(
                        len(volatility_values)
                    )
                    metrics.append(
                        _available(
                            "sharpe_ratio",
                            ((mean_return - period_rf) / volatility)
                            * Decimal(periods_per_year).sqrt(),
                            "ratio",
                            PRICE_FORMULAS["sharpe_ratio"],
                            observation_count=len(volatility_values),
                            periods_per_year=periods_per_year,
                            risk_free_rate_annual=_decimal_text(rf),
                            risk_free_conversion="geometric effective annual to per-observation",
                        )
                    )

        input_revision_ids = [
            bar["revision_id"]
            for data in (subject, benchmark)
            if data
            for bar in data["bars"]
        ]
        input_revision_ids.extend(
            str(ref["source_revision_id"])
            for data in (subject, benchmark)
            if data
            for bar in data["bars"]
            for ref in bar.get("source_refs", [])
            if ref.get("source_revision_id")
        )
        input_revision_ids.extend(
            revision
            for data in (subject, benchmark)
            if data
            for revision in data["identity_revision_ids"]
        )
        action_revision_ids = sorted(
            {
                revision
                for data in (subject, benchmark)
                if data
                for revision in data["action_revision_ids"]
            }
        )
        input_revision_ids.extend(action_revision_ids)
        entitlement_map = {}
        for data in (subject, benchmark):
            if data:
                for item in data["entitlement_decisions"]:
                    key = str(item.get("source_ref_id") or item.get("entitlement_id"))
                    entitlement_map[key] = {
                        "source_ref_id": str(item["source_ref_id"]),
                        "provider": str(item["provider"]),
                        "license_id": str(item["license_id"]),
                        "entitlement_id": str(item["entitlement_id"]),
                        "policy_revision_id": str(item["policy_revision_id"]),
                        "authorized_operation": "derive",
                    }
        assumptions = {
            "return_frequency": "daily observations from venue-local session dates",
            "period_alignment": "benchmark returns inner-joined on identical ISO start and end session dates; no forward fill",
            "annualization": {
                "periods_per_year": periods_per_year,
                "volatility": "sample standard deviation times sqrt(periods_per_year)",
            },
            "adjusted_price_convention": "raw bars use point-in-time split-adjusted and dividend-reinvested series; provider-adjusted bars support only their declared basis",
            "currency_conversion": "none; benchmark comparisons require equal listing currencies",
            "risk_free_rate_annual": _decimal_text(rf),
            "missing_observations": "no synthetic or forward-filled prices; observed interval gaps are exposed per return row and each observed return counts as one annualization period",
        }
        request = {
            "listing_id": listing_id,
            "benchmark_listing_id": benchmark_listing_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "public_cutoff_ms": public_cutoff_ms,
            "acquired_cutoff_ms": acquired_by_ms,
            "periods_per_year": periods_per_year,
            "risk_free_rate_annual": _decimal_text(rf),
            "subject_price_basis": subject["price_basis"],
            "benchmark_price_basis": benchmark["price_basis"] if benchmark else None,
        }
        result = {
            "formula_version": MARKET_METRICS_FORMULA_VERSION,
            "formula_versions": dict(PRICE_FORMULAS),
            "currency": subject["listing"]["currency"],
            "metrics": metrics,
            "series": [
                {
                    **item,
                    "price_return": _decimal_text(item["price_return"]),
                    "total_return": _decimal_text(item["total_return"]),
                }
                for item in daily
            ],
            "assumptions": assumptions,
            "action_revision_ids": action_revision_ids,
            "source_entitlements": [
                entitlement_map[key] for key in sorted(entitlement_map)
            ],
        }
        return self._record_report(
            namespace,
            request,
            result,
            input_revision_ids,
            principal_id=principal_id,
            scopes=scopes,
            quality_exclusions=[
                exclusion
                for data in (subject, benchmark)
                if data
                for exclusion in data.get("quality_exclusions", [])
            ],
        )

    @staticmethod
    def _period(fact: Mapping[str, Any]) -> tuple[str, date, date | None]:
        period = fact.get("period") or {}
        if period.get("kind") == "instant":
            try:
                return "instant", date.fromisoformat(period["instant_date"]), None
            except (KeyError, ValueError) as exc:
                raise MarketMetricError(
                    "invalid_fact_period", "instant fact date is invalid"
                ) from exc
        if period.get("kind") == "duration":
            try:
                return (
                    "duration",
                    date.fromisoformat(period["start_date"]),
                    date.fromisoformat(period["end_date"]),
                )
            except (KeyError, ValueError) as exc:
                raise MarketMetricError(
                    "invalid_fact_period", "duration fact dates are invalid"
                ) from exc
        raise MarketMetricError(
            "invalid_fact_period", "fact must have an instant or duration period"
        )

    @staticmethod
    def _fact_value(fact: Mapping[str, Any]) -> Decimal:
        scale = fact.get("scale", 0)
        if type(scale) is not int or abs(scale) > 24:
            raise MarketMetricError(
                "unsupported_scale", "fact scale is outside the safe calculation bound"
            )
        return _decimal(fact["value_lexical"], "value_lexical") * (Decimal(10) ** scale)

    def calculate_fact_metrics(
        self,
        namespace: str,
        issuer_id: str,
        requests: Sequence[Mapping[str, Any]],
        *,
        acquired_by_ms: int,
        publicly_available_by_ms: int,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Calculate sourced ratios from exact as-of fact revision identities.

        Requests identify their inputs by revision ID. Ratio metrics require
        matching periods; growth requires the same reported concept and duration
        length in comparable year-over-year periods. A valuation multiple pairs
        same-date instants or an instant value with trailing duration ending no
        more than 366 days earlier. Currency changes need an explicit sourced FX
        rate object, and zero or negative denominators produce typed unavailable
        values instead of infinities or misleading multiples.
        """

        self._authorize_calculation(namespace, principal_id, scopes)
        from src.domains.market.financial_facts import (
            MarketFinancialFactError,
            MarketFinancialFactStore,
        )
        from src.domains.market.instruments import (
            MarketInstrumentError,
            MarketInstrumentStore,
        )

        acquired_by_ms = _millis(acquired_by_ms, "acquired_by_ms")
        public_cutoff_ms = _millis(publicly_available_by_ms, "publicly_available_by_ms")
        if public_cutoff_ms > acquired_by_ms:
            raise MarketMetricError(
                "invalid_request", "public cutoff cannot exceed acquisition cutoff"
            )
        if not isinstance(requests, Sequence) or isinstance(requests, (str, bytes)):
            raise MarketMetricError(
                "invalid_request", "requests must be a bounded sequence"
            )
        if not 1 <= len(requests) <= MAX_FACT_REQUESTS:
            raise MarketMetricError(
                "invalid_request", "request count is outside the bound"
            )
        try:
            issuer = MarketInstrumentStore(self.conn, initialize=False).get_instrument(
                namespace,
                "issuer",
                _text(issuer_id, "issuer_id", limit=200),
                acquired_by_ms=acquired_by_ms,
                publicly_available_by_ms=public_cutoff_ms,
                principal_id=principal_id,
                scopes=scopes,
            )
        except MarketInstrumentError as exc:
            raise MarketMetricError(exc.code, exc.message, **exc.details) from exc
        issuer_refs = list(issuer.get("source_refs", []))
        issuer_decisions = self._derive_sources(
            namespace,
            issuer_refs,
            principal_id=principal_id,
            scopes=scopes,
        )
        issuer_decision_by_ref = {
            item["source_ref_id"]: item for item in issuer_decisions
        }
        fact_store = MarketFinancialFactStore(self.conn, initialize=False, now=self.now)
        try:
            facts = fact_store.get_facts(
                namespace,
                _text(issuer_id, "issuer_id", limit=200),
                acquired_by_ms=acquired_by_ms,
                publicly_available_by_ms=public_cutoff_ms,
                principal_id=principal_id,
                scopes=scopes,
                require_complete=True,
            )
        except MarketFinancialFactError as exc:
            raise MarketMetricError(exc.code, exc.message, **exc.details) from exc
        by_revision = {fact["revision_id"]: fact for fact in facts}
        metric_names: set[str] = set()
        used_facts: dict[str, dict[str, Any]] = {}
        used_rates: list[dict[str, Any]] = []
        metrics: list[dict[str, Any]] = []
        conversion_ids: list[str] = []
        formula_calculation_ids: list[str] = []
        formula_registry_revisions: dict[str, str] = {}
        formula_registry_metrics: dict[str, dict[str, Any]] = {}
        from src.kb.quantitative import (
            WRITE_SCOPE,
            QuantitativeError,
            QuantitativeStore,
        )

        quantitative = QuantitativeStore(self.conn, initialize=True, now=self.now)

        def register_formula(kind: str, formula_version: str) -> dict[str, Any]:
            registered_metric = formula_registry_metrics.get(kind)
            if registered_metric:
                return registered_metric
            expression = (
                "current / previous - 1"
                if kind == "growth"
                else "numerator / denominator"
            )
            try:
                registered = quantitative.register_metric(
                    namespace,
                    f"market_{kind}_{formula_version.rsplit('-', 1)[-1]}",
                    f"Versioned filed-fact {kind} formula ({formula_version}).",
                    "ratio",
                    principal_id="system:market-formula-registry",
                    scopes={WRITE_SCOPE},
                    formula={
                        "expression": expression,
                        "input_dimensions": {},
                        "domain": "market",
                        "formula_version": formula_version,
                    },
                    idempotency_key=f"noesis-market-formula:{formula_version}",
                    producer={"name": "noesis-market-metrics", "version": "1.0.0"},
                    policy={"comparability": "explicit-period-and-unit-v1"},
                )
            except QuantitativeError as exc:
                raise MarketMetricError(exc.code, exc.message, **exc.details) from exc
            formula_registry_revisions[kind] = registered["revision_id"]
            formula_registry_metrics[kind] = registered
            return registered

        def load_fact(revision_id: Any) -> dict[str, Any]:
            revision_id = _text(revision_id, "fact_revision_id", limit=300)
            fact = by_revision.get(revision_id)
            if fact is None:
                raise MarketMetricError(
                    "fact_revision_unavailable",
                    "requested fact revision is not accessible at both as-of cutoffs",
                    revision_id=revision_id,
                )
            used_facts[revision_id] = fact
            return fact

        for raw in requests:
            if not isinstance(raw, Mapping):
                raise MarketMetricError(
                    "invalid_request", "each metric request must be an object"
                )
            name = _text(raw.get("name"), "name", limit=MAX_METRIC_NAME)
            kind = _text(raw.get("kind"), "kind", limit=40)
            if name in metric_names:
                raise MarketMetricError(
                    "duplicate_metric", "metric request names must be unique", name=name
                )
            metric_names.add(name)
            formula = FACT_FORMULAS.get(kind)
            if formula is None:
                raise MarketMetricError(
                    "unsupported_metric",
                    "unsupported filed-fact metric kind",
                    kind=kind,
                )
            numerator = load_fact(raw.get("numerator_fact_revision_id"))
            denominator = load_fact(raw.get("denominator_fact_revision_id"))
            numerator_value = self._fact_value(numerator)
            denominator_value = self._fact_value(denominator)
            numerator_kind, numerator_start, numerator_end = self._period(numerator)
            denominator_kind, denominator_start, denominator_end = self._period(
                denominator
            )
            if kind == "growth":
                same_concept = (
                    numerator.get("canonical_concept")
                    and numerator.get("canonical_concept")
                    == denominator.get("canonical_concept")
                ) or (
                    numerator.get("taxonomy") == denominator.get("taxonomy")
                    and numerator.get("concept") == denominator.get("concept")
                )
                duration_match = (
                    numerator_kind == denominator_kind == "duration"
                    and numerator_end is not None
                    and denominator_end is not None
                    and 350 <= (numerator_end - denominator_end).days <= 380
                    and abs(
                        (numerator_end - numerator_start).days
                        - (denominator_end - denominator_start).days
                    )
                    <= 3
                )
                if not same_concept or not duration_match:
                    raise MarketMetricError(
                        "period_or_concept_mismatch",
                        "growth requires the same concept and comparable prior-year durations",
                        name=name,
                    )
            elif kind == "valuation_multiple":
                if numerator_kind != "instant":
                    raise MarketMetricError(
                        "period_mismatch",
                        "valuation numerator must be an instant fact",
                        name=name,
                    )
                if denominator_kind == "instant":
                    valid_period = numerator_start == denominator_start
                else:
                    valid_period = (
                        denominator_end is not None
                        and 0 <= (numerator_start - denominator_end).days <= 366
                        and 350 <= (denominator_end - denominator_start).days <= 366
                    )
                if not valid_period:
                    raise MarketMetricError(
                        "period_mismatch",
                        "valuation denominator must share the instant date or be a trailing duration ending within 366 days",
                        name=name,
                    )
            elif kind == "margin" and numerator_kind != "duration":
                raise MarketMetricError(
                    "period_mismatch", "margin inputs must be duration facts", name=name
                )
            elif kind in {"leverage", "liquidity"} and numerator_kind != "instant":
                raise MarketMetricError(
                    "period_mismatch",
                    "leverage and liquidity inputs must be instant facts",
                    name=name,
                )
            elif (
                numerator_kind != denominator_kind
                or numerator_start != denominator_start
                or numerator_end != denominator_end
            ):
                raise MarketMetricError(
                    "period_mismatch",
                    "ratio inputs must have identical reporting periods",
                    name=name,
                )

            registered_formula = register_formula(kind, formula)
            fx_rate = raw.get("fx_rate")
            this_conversion_id = None
            numerator_unit = str(numerator["unit"])
            denominator_unit = str(denominator["unit"])
            if numerator_unit != denominator_unit:
                if kind == "growth":
                    raise MarketMetricError(
                        "historical_currency_conversion_unsupported",
                        "growth across different reporting currencies needs a separate sourced FX rate for each period",
                        name=name,
                    )
                if not isinstance(fx_rate, Mapping):
                    raise MarketMetricError(
                        "unit_mismatch",
                        "ratio inputs use different units; provide a source-pinned currency FX rate when both are currencies",
                        name=name,
                    )
                refs = fx_rate.get("source_refs")
                if not isinstance(refs, list) or not refs:
                    raise MarketMetricError(
                        "fx_source_required",
                        "FX conversion requires source references",
                        name=name,
                    )
                expected_rate_kind = (
                    "spot" if numerator_kind == "instant" else "period_average"
                )
                if (
                    fx_rate.get("period") != numerator.get("period")
                    or fx_rate.get("rate_kind") != expected_rate_kind
                    or type(fx_rate.get("observed_at_ms")) is not int
                    or int(fx_rate["observed_at_ms"]) > public_cutoff_ms
                    or type(fx_rate.get("public_at_ms")) is not int
                    or int(fx_rate["public_at_ms"]) > public_cutoff_ms
                ):
                    raise MarketMetricError(
                        "fx_period_mismatch",
                        "FX rate must be public by the cutoff and match the exact input period using spot for instants or period average for durations",
                        name=name,
                    )
                rate_value = _decimal(fx_rate.get("rate"), "fx_rate.rate")
                if rate_value <= 0:
                    raise MarketMetricError(
                        "invalid_fx_rate", "FX rate must be positive", name=name
                    )
                for ref in refs:
                    ref_public = ref.get("public_at_ms")
                    ref_retrieved = ref.get("retrieved_at_ms")
                    if (
                        type(ref_public) is not int
                        or ref_public > public_cutoff_ms
                        or type(ref_retrieved) is not int
                        or ref_retrieved > acquired_by_ms
                    ):
                        raise MarketMetricError(
                            "fx_source_not_asof",
                            "every FX source reference must be public and acquired by the requested cutoffs",
                            name=name,
                        )
                    if not ref.get("source_revision_id"):
                        raise MarketMetricError(
                            "fx_source_revision_required",
                            "FX rate provenance must name its source revision",
                            name=name,
                        )
                decisions = self._derive_sources(
                    namespace,
                    refs,
                    principal_id=principal_id,
                    scopes=scopes,
                )
                decision_by_ref = {item["source_ref_id"]: item for item in decisions}
                for ref in refs:
                    decision = decision_by_ref.get(str(ref.get("source_ref_id") or ""))
                    if decision:
                        used_rates.append(
                            {
                                "source_ref_id": str(ref["source_ref_id"]),
                                "provider": str(ref["provider"]),
                                "license_id": str(ref["license_id"]),
                                "entitlement_id": str(ref["entitlement_id"]),
                                "policy_revision_id": decision["policy_revision_id"],
                                "authorized_operation": "derive",
                            }
                        )
                rate = {
                    "from": _text(fx_rate.get("from"), "fx_rate.from", limit=3).upper(),
                    "to": _text(fx_rate.get("to"), "fx_rate.to", limit=3).upper(),
                    "rate": str(rate_value),
                    "rate_id": _text(
                        fx_rate.get("rate_id"), "fx_rate.rate_id", limit=300
                    ),
                }
                try:
                    conversion = quantitative.convert(
                        namespace,
                        numerator_value,
                        numerator_unit,
                        denominator_unit,
                        scopes=scopes,
                        principal_id=principal_id,
                        precision=12,
                        rate=rate,
                    )
                except QuantitativeError as exc:
                    raise MarketMetricError(
                        exc.code, exc.message, **exc.details
                    ) from exc
                numerator_value = _decimal(
                    conversion["result"]["value"], "converted_value"
                )
                conversion_ids.append(conversion["calculation_id"])
                this_conversion_id = conversion["calculation_id"]
                used_rates.append(
                    {
                        "rate_id": rate["rate_id"],
                        "from": rate["from"],
                        "to": rate["to"],
                        "value": rate["rate"],
                        "conversion_calculation_id": conversion["calculation_id"],
                    }
                )

            if denominator_value == 0:
                metric = _unavailable(name, formula, "zero_denominator")
            elif denominator_value < 0:
                metric = _unavailable(name, formula, "negative_denominator")
            elif kind in {
                "growth",
                "margin",
                "leverage",
                "liquidity",
                "valuation_multiple",
            }:
                if kind == "growth":
                    formula_inputs = {
                        "current": {
                            "value": str(numerator_value),
                            "observation_id": numerator["revision_id"],
                        },
                        "previous": {
                            "value": str(denominator_value),
                            "observation_id": denominator["revision_id"],
                        },
                    }
                    if this_conversion_id:
                        formula_inputs["current"]["conversion_calculation_id"] = (
                            this_conversion_id
                        )
                else:
                    formula_inputs = {
                        "numerator": {
                            "value": str(numerator_value),
                            "observation_id": numerator["revision_id"],
                        },
                        "denominator": {
                            "value": str(denominator_value),
                            "observation_id": denominator["revision_id"],
                        },
                    }
                    if this_conversion_id:
                        formula_inputs["numerator"]["conversion_calculation_id"] = (
                            this_conversion_id
                        )
                try:
                    formula_receipt = quantitative.evaluate_formula(
                        namespace,
                        registered_formula["metric_id"],
                        formula_inputs,
                        scopes=scopes,
                        principal_id=principal_id,
                        precision=12,
                    )
                except QuantitativeError as exc:
                    raise MarketMetricError(
                        exc.code, exc.message, **exc.details
                    ) from exc
                formula_calculation_ids.append(formula_receipt["calculation_id"])
                value = _decimal(formula_receipt["result"]["value"], "formula_value")
                metric = _available(
                    name,
                    value,
                    "ratio",
                    formula,
                    numerator_fact_revision_id=numerator["revision_id"],
                    denominator_fact_revision_id=denominator["revision_id"],
                    numerator_unit=numerator_unit,
                    denominator_unit=denominator_unit,
                    converted_numerator_unit=(
                        denominator_unit if numerator_unit != denominator_unit else None
                    ),
                    period=dict(numerator["period"]),
                    denominator_policy="zero and negative denominators are unavailable",
                    formula_revision_id=registered_formula["revision_id"],
                    formula_calculation_id=formula_receipt["calculation_id"],
                )
            else:  # pragma: no cover - guarded by the formula registry above
                raise MarketMetricError(
                    "unsupported_metric", "unsupported filed-fact metric kind"
                )
            if metric["status"] == "unavailable":
                metric.update(
                    {
                        "numerator_fact_revision_id": numerator["revision_id"],
                        "denominator_fact_revision_id": denominator["revision_id"],
                        "period": dict(numerator["period"]),
                        "numerator_unit": numerator_unit,
                        "denominator_unit": denominator_unit,
                        "formula_revision_id": registered_formula["revision_id"],
                    }
                )
            metrics.append(metric)

        facts_used = [used_facts[key] for key in sorted(used_facts)]
        source_refs = [
            dict(ref) for fact in facts_used for ref in fact.get("source_refs", [])
        ]
        fact_decisions = self._derive_sources(
            namespace,
            source_refs,
            principal_id=principal_id,
            scopes=scopes,
        )
        source_entitlements = {}
        for ref in issuer_refs:
            decision = issuer_decision_by_ref.get(str(ref.get("source_ref_id") or ""))
            if decision:
                source_entitlements[str(ref["source_ref_id"])] = {
                    "source_ref_id": str(ref["source_ref_id"]),
                    "provider": str(ref["provider"]),
                    "license_id": str(ref["license_id"]),
                    "entitlement_id": str(ref["entitlement_id"]),
                    "policy_revision_id": decision["policy_revision_id"],
                    "authorized_operation": "derive",
                }
        for fact in facts_used:
            decisions = {item["source_ref_id"]: item for item in fact_decisions}
            for ref in fact.get("source_refs", []):
                decision = decisions.get(str(ref.get("source_ref_id") or ""))
                if decision:
                    source_entitlements[str(ref["source_ref_id"])] = {
                        "source_ref_id": str(ref["source_ref_id"]),
                        "provider": str(ref["provider"]),
                        "license_id": str(ref["license_id"]),
                        "entitlement_id": str(ref["entitlement_id"]),
                        "policy_revision_id": decision["policy_revision_id"],
                        "authorized_operation": "derive",
                    }
        fx_source_revision_ids = [
            str(ref["source_revision_id"])
            for request_item in requests
            if isinstance(request_item.get("fx_rate"), Mapping)
            for ref in request_item["fx_rate"].get("source_refs", [])
            if ref.get("source_revision_id")
        ]
        issuer_source_revision_ids = [
            str(ref["source_revision_id"])
            for ref in issuer_refs
            if ref.get("source_revision_id")
        ]
        fact_source_revision_ids = [
            str(ref["source_revision_id"])
            for fact in facts_used
            for ref in fact.get("source_refs", [])
            if ref.get("source_revision_id")
        ]
        input_revision_ids = [
            issuer["revision_id"],
            *issuer_source_revision_ids,
            *used_facts,
            *fact_source_revision_ids,
            *fx_source_revision_ids,
        ]
        input_revision_ids = sorted(set(item for item in input_revision_ids if item))
        result = {
            "formula_version": MARKET_METRICS_FORMULA_VERSION,
            "formula_versions": dict(FACT_FORMULAS),
            "currency_conversion": "QuantitativeStore registered currency conversion using the supplied rate and derive-authorized source references; missing rates fail closed",
            "metrics": metrics,
            "conversion_calculation_ids": sorted(set(conversion_ids)),
            "formula_calculation_ids": sorted(set(formula_calculation_ids)),
            "formula_registry_revisions": dict(
                sorted(formula_registry_revisions.items())
            ),
            "issuer_revision_id": issuer["revision_id"],
            "source_entitlements": [
                source_entitlements[key] for key in sorted(source_entitlements)
            ]
            + used_rates,
        }
        request = {
            "issuer_id": issuer_id,
            "requests": [dict(item) for item in requests],
            "public_cutoff_ms": public_cutoff_ms,
            "acquired_cutoff_ms": acquired_by_ms,
            "fact_revision_ids": sorted(used_facts),
        }
        return self._record_report(
            namespace,
            request,
            result,
            input_revision_ids,
            principal_id=principal_id,
            scopes=scopes,
            calculation_input_ids=[
                *conversion_ids,
                *formula_calculation_ids,
                *formula_registry_revisions.values(),
            ],
        )

    def _record_report(
        self,
        namespace: str,
        request: Mapping[str, Any],
        result: Mapping[str, Any],
        input_revision_ids: Sequence[str],
        *,
        principal_id: str,
        scopes: set[str],
        calculation_input_ids: Sequence[str] = (),
        quality_exclusions: Sequence[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        from src.kb.quantitative import QuantitativeError, QuantitativeStore

        calculation_inputs = sorted(
            set(str(item) for item in input_revision_ids)
            | set(str(item) for item in calculation_input_ids)
        )
        candidate_id = "quantitative-calculation:" + "0" * 24
        quality = summarize_input_quality(
            self.conn,
            namespace,
            input_revision_ids,
            excluded_findings=quality_exclusions,
        )
        calculation_payload = {**dict(result), "quality": quality}
        report_body = {
            "contract": "noesis-market-metric-report-v1",
            "namespace": namespace,
            "owner": None if "operator" in scopes else principal_id,
            "calculation_id": candidate_id,
            "formula_version": MARKET_METRICS_FORMULA_VERSION,
            "quantitative_calculation_id": candidate_id,
            "calculation_hash": "0" * 64,
            "recorded_at_ms": _millis(self.now(), "now_ms"),
            "public_cutoff_ms": int(request["public_cutoff_ms"]),
            "acquired_cutoff_ms": int(request["acquired_cutoff_ms"]),
            "input_revision_ids": sorted(set(str(item) for item in input_revision_ids)),
            "quality": quality,
            **calculation_payload,
        }
        _validate_report(
            report_body
        )
        try:
            receipt = QuantitativeStore(
                self.conn, initialize=True, now=self.now
            ).record_domain_calculation(
                namespace,
                "market-metrics",
                dict(request),
                calculation_payload,
                input_ids=calculation_inputs,
                principal_id=principal_id,
                scopes=scopes,
                formula_revision_id=MARKET_METRICS_FORMULA_VERSION,
            )
        except QuantitativeError as exc:
            raise MarketMetricError(exc.code, exc.message, **exc.details) from exc
        return {
            "contract": "noesis-market-metric-report-v1",
            "namespace": namespace,
            "owner": None if "operator" in scopes else principal_id,
            "calculation_id": receipt["calculation_id"],
            "formula_version": MARKET_METRICS_FORMULA_VERSION,
            "quantitative_calculation_id": receipt["calculation_id"],
            "calculation_hash": receipt["calculation_hash"],
            "recorded_at_ms": int(receipt["created_at_ms"]),
            "public_cutoff_ms": int(request["public_cutoff_ms"]),
            "acquired_cutoff_ms": int(request["acquired_cutoff_ms"]),
            "input_revision_ids": sorted(set(str(item) for item in input_revision_ids)),
            **dict(result),
            "quality": quality,
        }


__all__ = [
    "FACT_FORMULAS",
    "MARKET_METRICS_FORMULA_VERSION",
    "MarketMetricError",
    "MarketMetricStore",
    "PRICE_FORMULAS",
]
