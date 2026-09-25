"""Revision-aware market data quality, quarantine and bounded repair plans."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Sequence
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

READ_SCOPE = "market:quality:read"
WRITE_SCOPE = "market:quality:write"
CALCULATE_SCOPE = "market:quality:calculate"
REVIEW_SCOPE = "market:quality:review"
ASSESSMENT_CONTRACT = "noesis-market-quality-assessment-v1"
QUARANTINE_CONTRACT = "noesis-market-quality-quarantine-v1"
REPAIR_CONTRACT = "noesis-market-repair-plan-v1"
CANDIDATE_CONTRACT = "noesis-market-quality-candidate-v1"
CANDIDATE_LIST_CONTRACT = "noesis-market-quality-candidate-list-v1"
_QUARANTINABLE = {
    "price_bar": "market_price_bar_revisions",
    "corporate_action": "market_corporate_action_revisions",
    "financial_fact": "market_financial_fact_revisions",
}
_SOURCE_SCOPES = {
    "price_bar": "market:prices:read",
    "corporate_action": "market:actions:read",
    "financial_fact": "market:financial-facts:read",
}
_SAFE_PROVIDER_RECORD_ID = re.compile(r"^[A-Za-z0-9._:-]{1,180}$")
_DDL = """
CREATE TABLE IF NOT EXISTS market_quality_assessments (
 assessment_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 request_key TEXT NOT NULL, request_hash TEXT NOT NULL, input_hash TEXT NOT NULL,
 report_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
 UNIQUE(namespace, owner, request_key));
CREATE TABLE IF NOT EXISTS market_quality_quarantine (
 namespace TEXT NOT NULL, object_kind TEXT NOT NULL, revision_id TEXT NOT NULL,
 status TEXT NOT NULL, reason TEXT NOT NULL, finding_ids_json TEXT NOT NULL,
 reviewer_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
 updated_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, object_kind, revision_id));
CREATE TABLE IF NOT EXISTS market_quality_quarantine_events (
 event_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, object_kind TEXT NOT NULL,
 revision_id TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
 reviewer_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS market_quality_candidates (
 candidate_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, request_key TEXT NOT NULL,
 provider TEXT NOT NULL, listing_id TEXT NOT NULL, interval TEXT NOT NULL,
 provider_record_id TEXT, record_hash TEXT NOT NULL, error_code TEXT NOT NULL,
 status TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
 UNIQUE(namespace, request_key, record_hash));
CREATE TABLE IF NOT EXISTS market_quality_repair_plans (
 plan_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 request_key TEXT NOT NULL, request_hash TEXT NOT NULL, status TEXT NOT NULL,
 plan_json TEXT NOT NULL, result_json TEXT, created_at_ms BIGINT NOT NULL,
 updated_at_ms BIGINT NOT NULL,
 UNIQUE(namespace, owner, request_key));
"""


class MarketQualityError(ValueError):
    """Typed market-quality failure safe for API and tool surfaces."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


def ensure_market_quality_schema(conn: Any) -> None:
    """Create additive assessment, quarantine and repair-plan tables."""

    conn.execute(_DDL)
    from src.domains.market.entitlements import ensure_market_entitlement_schema

    ensure_market_entitlement_schema(conn)


def summarize_input_quality(
    conn: Any,
    namespace: str,
    revision_ids: Sequence[str],
    *,
    excluded_findings: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Return a read-only quality signal for already authorized inputs.

    Consumers use this after their source stores have applied entitlement and
    quarantine filtering. The summary retains revision IDs and reasons only;
    it never reintroduces quarantined source payloads.
    """
    unique_ids = sorted({value for value in revision_ids if isinstance(value, str) and value})
    safe_exclusions = [
        {
            "object_kind": str(item.get("object_kind") or "unknown"),
            "revision_id": str(item.get("revision_id") or ""),
            "status": "quarantined",
            "reason": str(item.get("reason") or "quarantined input excluded")[:300],
        }
        for item in excluded_findings
        if isinstance(item, dict) and item.get("revision_id")
    ]
    if not unique_ids:
        return {
            "state": "degraded" if safe_exclusions else "not_assessed",
            "input_revision_count": 0,
            "quarantined_revision_ids": [
                item["revision_id"] for item in safe_exclusions
            ],
            "findings": safe_exclusions,
        }
    try:
        rows = conn.execute(
            "SELECT object_kind,revision_id,status,reason FROM market_quality_quarantine WHERE namespace=? AND revision_id IN (SELECT * FROM UNNEST(?))",
            [namespace, unique_ids],
        ).fetchall()
    except Exception:  # noqa: BLE001 - quality tables may not yet exist
        return {
            "state": "degraded" if safe_exclusions else "not_assessed",
            "input_revision_count": len(unique_ids),
            "quarantined_revision_ids": [
                item["revision_id"] for item in safe_exclusions
            ],
            "findings": safe_exclusions,
        }
    quarantined = [
        {"object_kind": str(row[0]), "revision_id": str(row[1]), "status": str(row[2]), "reason": str(row[3])}
        for row in rows
        if row[2] == "quarantined"
    ]
    by_revision = {
        item["revision_id"]: item for item in [*quarantined, *safe_exclusions]
    }
    quarantined = [by_revision[key] for key in sorted(by_revision)]
    return {
        "state": "degraded" if quarantined else "clear",
        "input_revision_count": len(unique_ids),
        "quarantined_revision_ids": [item["revision_id"] for item in quarantined],
        "findings": quarantined,
    }


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
        raise MarketQualityError("invalid_request", "request must be JSON-safe") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, name: str, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MarketQualityError(
            "invalid_request", f"{name} must be bounded nonempty text"
        )
    return value.strip()


def _ms(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise MarketQualityError("invalid_request", f"{name} must be epoch milliseconds")
    return value


def _decimal(value: Any) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MarketQualityError("invalid_number", "market value is not decimal-compatible") from exc
    if not number.is_finite():
        raise MarketQualityError("invalid_number", "market value must be finite")
    return number


def _quality_auth(
    namespace: str,
    principal_id: str,
    scopes: set[str],
    required: str,
    *,
    write: bool,
) -> None:
    _text(namespace, "namespace", 100)
    _text(principal_id, "principal_id", 200)
    if "operator" in scopes:
        return
    namespace_scopes = (
        {f"namespace:{namespace}:write"}
        if write
        else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    )
    if required not in scopes or not (namespace_scopes & scopes):
        raise MarketQualityError(
            "unauthorized", "current market quality and namespace access is required"
        )


def _finding(rule: str, severity: str, summary: str, revision_ids=(), **evidence):
    body = {
        "rule": rule,
        "severity": severity,
        "summary": summary,
        "revision_ids": sorted(set(revision_ids)),
        "evidence": evidence,
    }
    return {"finding_id": "market-quality-finding:" + _digest(body)[:24], **body}


class MarketQualityStore:
    """Persist small quality receipts and quarantine decisions by source revision.

    Source payloads stay in their authoritative stores. This store retains only
    revision/hash references, quality findings, review decisions and backfill
    plan receipts.
    """

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            ensure_market_quality_schema(conn)

    def _require_source_operation(
        self,
        namespace: str,
        source_refs: list[dict[str, Any]],
        operation: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> list[dict[str, Any]]:
        if not source_refs:
            return []
        from src.domains.market.entitlements import (
            MarketEntitlementError,
            authorize_market_sources,
        )

        try:
            decisions = authorize_market_sources(
                self.conn,
                namespace,
                source_refs,
                operation=operation,
                principal_id=principal_id,
                scopes=scopes,
                now_ms=_ms(self.now(), "now_ms"),
            )
            by_source_ref = {
                item["source_ref_id"]: item for item in decisions
            }
            for ref in source_refs:
                decision = by_source_ref.get(str(ref.get("source_ref_id") or ""))
                if decision:
                    ref["policy_revision_id"] = decision["policy_revision_id"]
                    ref["authorized_operation"] = operation
            return decisions
        except MarketEntitlementError as exc:
            raise MarketQualityError(exc.code, exc.message, **exc.details) from exc

    @staticmethod
    def _source_entitlements(record: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "source_ref_id": str(ref["source_ref_id"]),
                "provider": str(ref["provider"]),
                "license_id": str(ref["license_id"]),
                "entitlement_id": str(ref["entitlement_id"]),
                "retrieved_at_ms": int(ref["retrieved_at_ms"]),
            }
            for ref in record.get("source_refs", [])
        ]

    def _save_assessment(
        self,
        namespace: str,
        request_key: str,
        request: dict[str, Any],
        input_refs: list[dict[str, Any]],
        *,
        principal_id: str,
        findings: list[dict[str, Any]],
        coverage: dict[str, Any],
        freshness: dict[str, Any],
    ) -> dict[str, Any]:
        request_hash = _digest([namespace, principal_id, request])
        input_hash = _digest([input_refs, findings, coverage, freshness])
        prior = self.conn.execute(
            "SELECT request_hash,report_json FROM market_quality_assessments "
            "WHERE namespace=? AND owner=? AND request_key=?",
            [namespace, principal_id, request_key],
        ).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise MarketQualityError(
                    "idempotency_conflict", "request_key identifies another quality assessment"
                )
            report = json.loads(prior[1])
            if report["input_hash"] != input_hash:
                raise MarketQualityError(
                    "assessment_inputs_changed",
                    "quality inputs changed; use a new request_key",
                )
            return {**report, "idempotent": True}
        status = (
            "degraded"
            if any(item["severity"] in {"warning", "error", "critical"} for item in findings)
            else "healthy"
        )
        assessment_id = "market-quality:" + _digest(
            [namespace, principal_id, request_key]
        )[:32]
        report = {
            "contract": ASSESSMENT_CONTRACT,
            "assessment_id": assessment_id,
            "namespace": namespace,
            "owner": principal_id,
            "request_key": request_key,
            "request": request,
            "status": status,
            "coverage": coverage,
            "freshness": freshness,
            "input_refs": input_refs,
            "input_hash": input_hash,
            "findings": sorted(findings, key=lambda item: (item["rule"], item["finding_id"])),
            "created_at_ms": _ms(self.now(), "created_at_ms"),
            "request_hash": request_hash,
        }
        report["assessment_hash"] = _digest(report)
        self.conn.execute(
            "INSERT INTO market_quality_assessments VALUES (?,?,?,?,?,?,?,?)",
            [
                assessment_id,
                namespace,
                principal_id,
                request_key,
                request_hash,
                input_hash,
                _canonical(report),
                report["created_at_ms"],
            ],
        )
        return report

    def assess_price_range(
        self,
        namespace: str,
        request_key: str,
        listing_id: str,
        *,
        interval: str,
        start_ms: int,
        end_ms: int,
        publicly_available_by_ms: int,
        acquired_by_ms: int,
        stale_after_ms: int,
        principal_id: str,
        scopes: set[str],
        source_disagreement_fraction: float = 0.02,
        max_move_fraction: float = 0.75,
        calendar_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, Any]:
        """Assess retained bars, optional venue-session coverage and provider agreement."""

        _quality_auth(namespace, principal_id, scopes, CALCULATE_SCOPE, write=True)
        request_key = _text(request_key, "request_key", 200)
        listing_id = _text(listing_id, "listing_id", 200)
        interval = _text(interval, "interval", 20)
        start_ms, end_ms = _ms(start_ms, "start_ms"), _ms(end_ms, "end_ms")
        public_cutoff = _ms(publicly_available_by_ms, "publicly_available_by_ms")
        acquired_cutoff = _ms(acquired_by_ms, "acquired_by_ms")
        stale_after_ms = _ms(stale_after_ms, "stale_after_ms", minimum=1)
        if start_ms >= end_ms or public_cutoff > acquired_cutoff:
            raise MarketQualityError("invalid_request", "time range or availability cutoffs are invalid")
        for name, value in (
            ("source_disagreement_fraction", source_disagreement_fraction),
            ("max_move_fraction", max_move_fraction),
        ):
            if type(value) not in (int, float) or not 0 <= value <= 10:
                raise MarketQualityError("invalid_request", f"{name} must be between zero and ten")
        if (calendar_id is None) != (start_date is None or end_date is None):
            raise MarketQualityError(
                "invalid_request", "calendar_id, start_date and end_date must be supplied together"
            )
        if calendar_id is not None and interval != "1d":
            raise MarketQualityError(
                "invalid_request", "venue-session coverage currently supports daily bars"
            )

        from src.domains.market.instruments import MarketInstrumentStore
        from src.domains.market.prices import MarketPriceStore

        identity = MarketInstrumentStore(self.conn).get_instrument(
            namespace,
            "listing",
            listing_id,
            acquired_by_ms=acquired_cutoff,
            publicly_available_by_ms=public_cutoff,
            principal_id=principal_id,
            scopes=scopes,
        )
        if identity is None:
            raise MarketQualityError("listing_unavailable", "listing is unavailable at the requested cutoffs")
        price_store = MarketPriceStore(self.conn)
        bars = price_store.get_bars(
            namespace,
            listing_id,
            start_ms=start_ms,
            end_ms=end_ms,
            acquired_by_ms=acquired_cutoff,
            publicly_available_by_ms=public_cutoff,
            principal_id=principal_id,
            scopes=scopes,
        )
        bars = [bar for bar in bars if bar.get("interval") == interval]
        quarantined_rows = self.conn.execute(
            """SELECT source.bar_id,source.revision_id,source.record_hash,source.payload_json
               FROM market_price_bar_revisions source
               JOIN market_quality_quarantine q
                 ON q.namespace=source.namespace AND q.object_kind='price_bar'
                AND q.revision_id=source.revision_id AND q.status='quarantined'
               WHERE source.namespace=? AND source.listing_id=? AND source.interval=?
                 AND source.bar_start_ms>=? AND source.bar_start_ms<?
                 AND source.recorded_at_ms<=? AND source.public_at_ms<=?
                 AND NOT EXISTS (
                   SELECT 1 FROM market_price_bar_revisions newer
                   WHERE newer.namespace=source.namespace AND newer.bar_id=source.bar_id
                     AND newer.revision>source.revision AND newer.recorded_at_ms<=?
                     AND newer.public_at_ms IS NOT NULL AND newer.public_at_ms<=?
                 )""",
            [
                namespace,
                listing_id,
                interval,
                start_ms,
                end_ms,
                acquired_cutoff,
                public_cutoff,
                acquired_cutoff,
                public_cutoff,
            ],
        ).fetchall()
        quarantined_refs = []
        for bar_id, revision_id, record_hash, encoded in quarantined_rows:
            payload = json.loads(encoded)
            if price_store._record_readable(
                namespace, payload, principal_id, scopes, public_cutoff
            ):
                quarantined_refs.append(
                    {
                        "kind": "price_bar",
                        "object_id": bar_id,
                        "revision_id": revision_id,
                        "record_hash": record_hash,
                        "source_entitlements": self._source_entitlements(payload),
                    }
                )
        now_ms = _ms(self.now(), "now_ms")
        findings = []
        if quarantined_refs:
            findings.append(
                _finding(
                    "quarantined_source_input",
                    "warning",
                    "One or more latest source revisions are quarantined and omitted from this range.",
                    [item["revision_id"] for item in quarantined_refs],
                    quarantined_count=len(quarantined_refs),
                )
            )
        from src.domains.market.prices import MAX_HISTORY_ROWS

        if not bars:
            findings.append(_finding("price_history_missing", "error", "No retained bars match the selected range."))
        elif len(bars) == MAX_HISTORY_ROWS:
            findings.append(
                _finding(
                    "price_query_row_limit_reached",
                    "warning",
                    "Selected price history reached the store row limit and may be truncated.",
                    [bar["revision_id"] for bar in bars],
                )
            )
        refs = [
            {
                "kind": "listing",
                "object_id": listing_id,
                "revision_id": identity["revision_id"],
                "record_hash": identity["record_hash"],
                "public_at_ms": max(
                    (
                        int(item["public_at_ms"])
                        for item in identity.get("source_refs", [])
                        if item.get("public_at_ms") is not None
                    ),
                    default=None,
                ),
                "retrieved_at_ms": identity.get("recorded_at_ms"),
                "source_entitlements": self._source_entitlements(identity),
            }
        ]
        refs.extend(
            {
                "kind": "price_bar",
                "object_id": bar["bar_id"],
                "revision_id": bar["revision_id"],
                "record_hash": bar["record_hash"],
                "provider": bar["provider"],
                "public_at_ms": bar.get("public_at_ms"),
                "retrieved_at_ms": bar.get("retrieved_at_ms"),
                "source_entitlements": self._source_entitlements(bar),
            }
            for bar in bars
        )
        refs.extend(quarantined_refs)
        self._require_source_operation(
            namespace,
            [
                source
                for item in refs
                for source in item.get("source_entitlements", [])
            ],
            "derive",
            principal_id=principal_id,
            scopes=scopes,
        )
        grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        duplicate_keys: set[tuple[str, str, str, int]] = set()
        for bar in bars:
            grouped[(bar["interval"], int(bar["bar_start_ms"]))].append(bar)
            duplicate_key = (
                str(bar["provider"]),
                str(bar.get("provider_record_id") or bar["bar_id"]),
                str(bar["interval"]),
                int(bar["bar_start_ms"]),
            )
            if duplicate_key in duplicate_keys:
                findings.append(
                    _finding(
                        "duplicate_bar",
                        "error",
                        "Multiple selected records share a provider bar identity.",
                        [bar["revision_id"]],
                        bar_start_ms=bar["bar_start_ms"],
                    )
                )
            duplicate_keys.add(duplicate_key)
            high, low = _decimal(bar["high"]), _decimal(bar["low"])
            opening, closing = _decimal(bar["open"]), _decimal(bar["close"])
            if closing <= 0:
                findings.append(
                    _finding(
                        "nonpositive_close_review",
                        "warning",
                        "A selected common-equity bar has a nonpositive close.",
                        [bar["revision_id"]],
                        close=str(closing),
                    )
                )
            if high < max(opening, closing, low):
                findings.append(
                    _finding(
                        "invalid_ohlc_range",
                        "critical",
                        "High is below another OHLC value.",
                        [bar["revision_id"]],
                    )
                )
            if low > min(opening, closing, high):
                findings.append(
                    _finding(
                        "invalid_ohlc_range",
                        "critical",
                        "Low is above another OHLC value.",
                        [bar["revision_id"]],
                    )
                )
            if str(bar.get("currency")) != str(identity.get("currency")):
                findings.append(
                    _finding(
                        "listing_currency_mismatch",
                        "critical",
                        "Bar currency differs from the selected listing currency.",
                        [bar["revision_id"], identity["revision_id"]],
                        bar_currency=bar.get("currency"),
                        listing_currency=identity.get("currency"),
                    )
                )
        ordered = sorted(bars, key=lambda item: (item["bar_start_ms"], item["provider"]))
        prior_by_provider: dict[str, dict[str, Any]] = {}
        for bar in ordered:
            prior = prior_by_provider.get(str(bar["provider"]))
            if prior:
                previous_close = _decimal(prior["close"])
                close = _decimal(bar["close"])
                if previous_close != 0:
                    move = abs(close / previous_close - Decimal(1))
                    if move > Decimal(str(max_move_fraction)):
                        findings.append(
                            _finding(
                                "implausible_price_move_review",
                                "warning",
                                "Close-to-close move exceeds the review threshold; verify corporate actions and source data.",
                                [prior["revision_id"], bar["revision_id"]],
                                move_fraction=float(move),
                                threshold_fraction=float(max_move_fraction),
                            )
                        )
            prior_by_provider[str(bar["provider"])] = bar
        for (bar_interval, bar_start), same_time in grouped.items():
            if len({item["provider"] for item in same_time}) < 2:
                continue
            currencies = {str(item.get("currency")) for item in same_time}
            if len(currencies) > 1:
                findings.append(
                    _finding(
                        "provider_currency_disagreement",
                        "error",
                        "Providers report different currencies for the same listing bar.",
                        [item["revision_id"] for item in same_time],
                        bar_start_ms=bar_start,
                        currencies=sorted(currencies),
                    )
                )
                continue
            closes = [_decimal(item["close"]) for item in same_time]
            low_close, high_close = min(closes), max(closes)
            denominator = max(abs(low_close), abs(high_close), Decimal("0.000000000001"))
            difference = (high_close - low_close) / denominator
            if difference > Decimal(str(source_disagreement_fraction)):
                findings.append(
                    _finding(
                        "provider_price_disagreement",
                        "error",
                        "Provider closes differ beyond the configured reconciliation tolerance.",
                        [item["revision_id"] for item in same_time],
                        interval=bar_interval,
                        bar_start_ms=bar_start,
                        difference_fraction=float(difference),
                        tolerance_fraction=float(source_disagreement_fraction),
                    )
                )

        latest_retrieved = max((int(bar["retrieved_at_ms"]) for bar in bars), default=None)
        age = None if latest_retrieved is None else now_ms - latest_retrieved
        freshness_status = "unknown" if age is None else "stale" if age > stale_after_ms else "fresh"
        if age is not None and age < 0:
            findings.append(
                _finding(
                    "future_retrieval_time",
                    "error",
                    "A selected bar has a retrieval time later than the assessment clock.",
                    [bar["revision_id"] for bar in bars if bar["retrieved_at_ms"] > now_ms],
                )
            )
        elif freshness_status == "stale":
            findings.append(
                _finding(
                    "stale_price_history",
                    "warning",
                    "Latest retained bar exceeds the configured freshness threshold.",
                    [max(bars, key=lambda item: item["retrieved_at_ms"])["revision_id"]],
                    age_ms=age,
                    stale_after_ms=stale_after_ms,
                )
            )

        coverage = {"status": "unverified", "counts": {}, "calendar_revision_ids": []}
        if calendar_id is not None:
            start_date = _text(start_date, "start_date", 10)
            end_date = _text(end_date, "end_date", 10)
            try:
                date.fromisoformat(start_date)
                date.fromisoformat(end_date)
            except ValueError as exc:
                raise MarketQualityError("invalid_request", "coverage dates must use ISO format") from exc
            calendar = price_store.coverage(
                namespace,
                listing_id,
                calendar_id=calendar_id,
                start_date=start_date,
                end_date=end_date,
                acquired_by_ms=acquired_cutoff,
                publicly_available_by_ms=public_cutoff,
                principal_id=principal_id,
                scopes=scopes,
            )
            coverage = {
                "status": "verified" if not any(
                    item["status"] in {"missing_bar", "unknown_session", "unknown_coverage"}
                    for item in calendar["coverage"]
                ) else "partial",
                "counts": calendar["counts"],
                "calendar_revision_ids": sorted(
                    {
                        item["calendar_revision_id"]
                        for item in calendar["coverage"]
                        if item.get("calendar_revision_id")
                    }
                ),
            }
            for day in calendar["coverage"]:
                if day["status"] in {"missing_bar", "unknown_session", "unknown_coverage"}:
                    findings.append(
                        _finding(
                            "session_coverage_gap",
                            "warning" if day["status"] != "missing_bar" else "error",
                            "Venue session coverage is missing or unknown.",
                            [day["calendar_revision_id"]] if day.get("calendar_revision_id") else [],
                            session_date=day["session_date"],
                            coverage_status=day["status"],
                        )
                    )
            refs.extend(
                {
                    "kind": "trading_session",
                    "object_id": item["session_date"],
                    "revision_id": item["calendar_revision_id"],
                }
                for item in calendar["coverage"]
                if item.get("calendar_revision_id")
            )
        else:
            findings.append(
                _finding(
                    "session_coverage_unverified",
                    "warning",
                    "No exchange calendar was supplied to verify expected daily sessions.",
                )
            )
        request = {
            "object_kind": "price_range",
            "listing_id": listing_id,
            "interval": interval,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "publicly_available_by_ms": public_cutoff,
            "acquired_by_ms": acquired_cutoff,
            "stale_after_ms": stale_after_ms,
            "source_disagreement_fraction": float(source_disagreement_fraction),
            "max_move_fraction": float(max_move_fraction),
            "calendar_id": calendar_id,
            "start_date": start_date,
            "end_date": end_date,
        }
        return self._save_assessment(
            namespace,
            request_key,
            request,
            refs,
            principal_id=principal_id,
            findings=findings,
            coverage={
                "matched_rows": len(bars),
                "quarantined_rows": len(quarantined_refs),
                **coverage,
            },
            freshness={
                "status": freshness_status,
                "latest_retrieved_at_ms": latest_retrieved,
                "age_ms": age,
                "stale_after_ms": stale_after_ms,
            },
        )

    def assess_financial_facts(
        self,
        namespace: str,
        request_key: str,
        issuer_id: str,
        *,
        publicly_available_by_ms: int,
        acquired_by_ms: int,
        stale_after_ms: int,
        principal_id: str,
        scopes: set[str],
        taxonomy: str | None = None,
        concept: str | None = None,
        filing_form: str | None = None,
    ) -> dict[str, Any]:
        """Check filing fact freshness, units, and same-period filing differences."""

        _quality_auth(namespace, principal_id, scopes, CALCULATE_SCOPE, write=True)
        request_key = _text(request_key, "request_key", 200)
        issuer_id = _text(issuer_id, "issuer_id", 200)
        public_cutoff = _ms(publicly_available_by_ms, "publicly_available_by_ms")
        acquired_cutoff = _ms(acquired_by_ms, "acquired_by_ms")
        stale_after_ms = _ms(stale_after_ms, "stale_after_ms", minimum=1)
        if public_cutoff > acquired_cutoff:
            raise MarketQualityError("invalid_request", "public cutoff cannot be later than acquisition cutoff")
        filters = {
            key: _text(value, key, 200)
            for key, value in {
                "taxonomy": taxonomy,
                "concept": concept,
                "filing_form": filing_form,
            }.items()
            if value is not None
        }
        from src.domains.market.financial_facts import (
            MAX_FINANCIAL_FACT_ROWS,
            MarketFinancialFactStore,
        )

        fact_store = MarketFinancialFactStore(self.conn)
        facts = fact_store.get_facts(
            namespace,
            issuer_id,
            acquired_by_ms=acquired_cutoff,
            publicly_available_by_ms=public_cutoff,
            principal_id=principal_id,
            scopes=scopes,
            require_complete=True,
            **filters,
        )
        quarantine_clauses = [
            "source.namespace=?",
            "source.issuer_id=?",
            "source.recorded_at_ms<=?",
            "source.public_at_ms IS NOT NULL AND source.public_at_ms<=?",
            "q.status='quarantined'",
        ]
        quarantine_params: list[Any] = [namespace, issuer_id, acquired_cutoff, public_cutoff]
        for column, value in (
            ("taxonomy", taxonomy),
            ("concept", concept),
            ("filing_form", filing_form),
        ):
            if value is not None:
                quarantine_clauses.append(f"source.{column}=?")
                quarantine_params.append(value)
        quarantine_params.extend([acquired_cutoff, public_cutoff])
        quarantine_rows = self.conn.execute(
            """SELECT source.fact_observation_id,source.revision_id,source.record_hash,source.payload_json
               FROM market_financial_fact_revisions source
               JOIN market_quality_quarantine q
                 ON q.namespace=source.namespace AND q.object_kind='financial_fact'
                AND q.revision_id=source.revision_id
               WHERE """
            + " AND ".join(quarantine_clauses)
            + """ AND NOT EXISTS (
                 SELECT 1 FROM market_financial_fact_revisions newer
                 WHERE newer.namespace=source.namespace
                   AND newer.fact_observation_id=source.fact_observation_id
                   AND newer.revision>source.revision AND newer.recorded_at_ms<=?
                   AND newer.public_at_ms IS NOT NULL AND newer.public_at_ms<=?
               )""",
            quarantine_params,
        ).fetchall()
        quarantined_facts = []
        for fact_id, revision_id, record_hash, encoded in quarantine_rows:
            payload = json.loads(encoded)
            if fact_store._record_readable(
                namespace, payload, principal_id, scopes, public_cutoff
            ):
                quarantined_facts.append(
                    {
                        "kind": "financial_fact",
                        "object_id": fact_id,
                        "revision_id": revision_id,
                        "record_hash": record_hash,
                        "source_entitlements": self._source_entitlements(payload),
                    }
                )
        findings = []
        if quarantined_facts:
            findings.append(
                _finding(
                    "quarantined_source_input",
                    "warning",
                    "One or more latest filing fact revisions are quarantined and omitted from this query.",
                    [item["revision_id"] for item in quarantined_facts],
                    quarantined_count=len(quarantined_facts),
                )
            )
        if not facts:
            findings.append(_finding("financial_facts_missing", "error", "No retained filing facts match the selected query."))
        elif len(facts) == MAX_FINANCIAL_FACT_ROWS:
            findings.append(
                _finding(
                    "financial_fact_query_row_limit_reached",
                    "warning",
                    "Selected filing facts reached the store row limit and may be truncated.",
                    [fact["revision_id"] for fact in facts],
                )
            )
        unmapped = [
            fact for fact in facts if fact.get("mapping_status") != "mapped"
        ]
        if unmapped:
            findings.append(
                _finding(
                    "financial_fact_concept_unmapped",
                    "warning",
                    "One or more filing facts have no accepted canonical concept mapping.",
                    [fact["revision_id"] for fact in unmapped],
                    statuses=sorted({str(fact.get("mapping_status")) for fact in unmapped}),
                )
            )
        by_concept_period: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        for fact in facts:
            canonical_concept = str(fact.get("canonical_concept") or fact["concept"])
            period_key = _canonical(fact["period"])
            by_concept_period[(canonical_concept, period_key, str(fact.get("statement")))].append(fact)
        for (canonical_concept, period_key, statement), group in by_concept_period.items():
            units = {str(item["unit"]) for item in group}
            if len(units) > 1:
                findings.append(
                    _finding(
                        "financial_fact_unit_mismatch",
                        "warning",
                        "Comparable facts use different units and were not combined.",
                        [item["revision_id"] for item in group],
                        canonical_concept=canonical_concept,
                        period_hash=_digest(period_key),
                        statement=statement,
                        units=sorted(units),
                    )
                )
                continue
            amounts = []
            for item in group:
                amounts.append(
                    (
                        _decimal(item["value_lexical"])
                        * (Decimal(10) ** int(item.get("scale") or 0)),
                        item,
                    )
                )
            if len({value for value, _ in amounts}) > 1 and len(
                {item["filing_accession"] for _, item in amounts}
            ) > 1:
                findings.append(
                    _finding(
                        "filing_revision_difference",
                        "info",
                        "Later or separate filings report different values for the same concept and period; review amendment/restatement lineage.",
                        [item["revision_id"] for _, item in amounts],
                        canonical_concept=canonical_concept,
                        period_hash=_digest(period_key),
                        accessions=sorted({item["filing_accession"] for _, item in amounts}),
                    )
                )
        latest_public = max((int(item["public_at_ms"]) for item in facts), default=None)
        latest_retrieved = max((int(item["retrieved_at_ms"]) for item in facts), default=None)
        now_ms = _ms(self.now(), "now_ms")
        age = None if latest_retrieved is None else now_ms - latest_retrieved
        freshness_status = "unknown" if age is None else "stale" if age > stale_after_ms else "fresh"
        if age is not None and age < 0:
            findings.append(_finding("future_retrieval_time", "error", "A selected fact has a retrieval clock later than the assessment clock."))
        elif freshness_status == "stale":
            findings.append(
                _finding(
                    "stale_financial_facts",
                    "warning",
                    "Latest retained filing fact exceeds the configured freshness threshold.",
                    [max(facts, key=lambda item: item["retrieved_at_ms"])["revision_id"]],
                    age_ms=age,
                    stale_after_ms=stale_after_ms,
                )
            )
        refs = [
            {
                "kind": "financial_fact",
                "object_id": item["fact_observation_id"],
                "revision_id": item["revision_id"],
                "record_hash": item["record_hash"],
                "filing_accession": item["filing_accession"],
                "public_at_ms": item.get("public_at_ms"),
                "retrieved_at_ms": item.get("retrieved_at_ms"),
                "source_entitlements": self._source_entitlements(item),
            }
            for item in facts
        ]
        refs.extend(quarantined_facts)
        self._require_source_operation(
            namespace,
            [
                source
                for item in refs
                for source in item.get("source_entitlements", [])
            ],
            "derive",
            principal_id=principal_id,
            scopes=scopes,
        )
        request = {
            "object_kind": "financial_facts",
            "issuer_id": issuer_id,
            "publicly_available_by_ms": public_cutoff,
            "acquired_by_ms": acquired_cutoff,
            "stale_after_ms": stale_after_ms,
            **filters,
        }
        return self._save_assessment(
            namespace,
            request_key,
            request,
            refs,
            principal_id=principal_id,
            findings=findings,
            coverage={
                "status": "observed" if facts else "missing",
                "matched_rows": len(facts),
                "quarantined_rows": len(quarantined_facts),
            },
            freshness={
                "status": freshness_status,
                "latest_public_at_ms": latest_public,
                "latest_retrieved_at_ms": latest_retrieved,
                "age_ms": age,
                "stale_after_ms": stale_after_ms,
            },
        )

    def inspect_assessment(
        self,
        namespace: str,
        assessment_id: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _quality_auth(namespace, principal_id, scopes, READ_SCOPE, write=False)
        row = self.conn.execute(
            "SELECT owner,report_json FROM market_quality_assessments "
            "WHERE namespace=? AND assessment_id=?",
            [namespace, _text(assessment_id, "assessment_id", 200)],
        ).fetchone()
        if not row:
            raise MarketQualityError("assessment_unavailable", "assessment is unavailable")
        if row[0] != principal_id and "operator" not in scopes:
            raise MarketQualityError("unauthorized", "assessment ownership is required")
        report = json.loads(row[1])
        if _digest({key: value for key, value in report.items() if key != "assessment_hash"}) != report.get("assessment_hash"):
            raise MarketQualityError("assessment_integrity_error", "assessment hash is invalid")
        self._require_source_operation(
            namespace,
            [
                source
                for item in report.get("input_refs", [])
                for source in item.get("source_entitlements", [])
            ],
            "derive",
            principal_id=principal_id,
            scopes=scopes,
        )
        return report

    def _record_invalid_candidate(
        self,
        namespace: str,
        request_key: str,
        provider: str,
        listing_id: str,
        interval: str,
        observation: dict[str, Any],
        error_code: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Record sanitized identity for a rejected row; never retain its payload."""

        _quality_auth(
            namespace,
            principal_id,
            scopes,
            "market:prices:write",
            write=True,
        )
        record_hash = _digest(observation)
        candidate_id = "market-quality-candidate:" + _digest(
            [namespace, request_key, provider, listing_id, interval, record_hash]
        )[:32]
        created_at_ms = _ms(self.now(), "created_at_ms")
        provider_record_id = observation.get("provider_record_id")
        if (
            not isinstance(provider_record_id, str)
            or not _SAFE_PROVIDER_RECORD_ID.fullmatch(provider_record_id)
        ):
            provider_record_id = None
        self.conn.execute(
            "INSERT OR IGNORE INTO market_quality_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                candidate_id,
                namespace,
                request_key,
                provider,
                listing_id,
                interval,
                provider_record_id,
                record_hash,
                _text(error_code, "error_code", 100),
                "quarantined",
                created_at_ms,
            ],
        )
        return {
            "contract": CANDIDATE_CONTRACT,
            "candidate_id": candidate_id,
            "namespace": namespace,
            "request_key": request_key,
            "provider": provider,
            "listing_id": listing_id,
            "interval": interval,
            "provider_record_id": provider_record_id,
            "record_hash": record_hash,
            "error_code": error_code,
            "status": "quarantined",
            "created_at_ms": created_at_ms,
        }

    def list_invalid_candidates(
        self,
        namespace: str,
        request_key: str,
        *,
        principal_id: str,
        scopes: set[str],
        limit: int = 100,
    ) -> dict[str, Any]:
        _quality_auth(namespace, principal_id, scopes, REVIEW_SCOPE, write=False)
        request_key = _text(request_key, "request_key", 250)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise MarketQualityError("invalid_request", "limit must be between one and 500")
        rows = self.conn.execute(
            "SELECT candidate_id,provider,listing_id,interval,provider_record_id,record_hash,error_code,status,created_at_ms "
            "FROM market_quality_candidates WHERE namespace=? AND request_key=? "
            "ORDER BY created_at_ms,candidate_id LIMIT ?",
            [namespace, request_key, limit],
        ).fetchall()
        return {
            "contract": CANDIDATE_LIST_CONTRACT,
            "namespace": namespace,
            "request_key": request_key,
            "items": [
                {
                    "candidate_id": row[0],
                    "provider": row[1],
                    "listing_id": row[2],
                    "interval": row[3],
                    "provider_record_id": row[4],
                    "record_hash": row[5],
                    "error_code": row[6],
                    "status": row[7],
                    "created_at_ms": row[8],
                }
                for row in rows
            ],
            "limit": limit,
        }

    def quarantine_revision(
        self,
        namespace: str,
        object_kind: str,
        revision_id: str,
        reason: str,
        *,
        principal_id: str,
        scopes: set[str],
        finding_ids: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        _quality_auth(namespace, principal_id, scopes, REVIEW_SCOPE, write=True)
        if object_kind not in _QUARANTINABLE:
            raise MarketQualityError("invalid_object_kind", "unsupported market revision kind")
        revision_id = _text(revision_id, "revision_id", 300)
        reason = _text(reason, "reason", 1000)
        if not isinstance(finding_ids, (list, tuple)) or len(finding_ids) > 50:
            raise MarketQualityError("invalid_request", "finding_ids must be a bounded list")
        table = _QUARANTINABLE[object_kind]
        row = self.conn.execute(
            f"SELECT payload_json FROM {table} WHERE namespace=? AND revision_id=?",
            [namespace, revision_id],
        ).fetchone()
        if not row:
            raise MarketQualityError("revision_unavailable", "market revision is unavailable")
        payload = json.loads(row[0])
        if "operator" not in scopes:
            if payload.get("owner") not in (None, principal_id):
                raise MarketQualityError("unauthorized", "market revision access is required")
            if _SOURCE_SCOPES[object_kind] not in scopes:
                raise MarketQualityError("unauthorized", "market source read scope is required")
        from src.domains.market.actions import MarketCorporateActionStore
        from src.domains.market.financial_facts import MarketFinancialFactStore
        from src.domains.market.prices import MarketPriceStore

        if object_kind == "price_bar":
            readable = MarketPriceStore(
                self.conn, initialize=False, now=self.now
            )._record_readable(namespace, payload, principal_id, scopes, None)
        elif object_kind == "corporate_action":
            readable = MarketCorporateActionStore(
                self.conn, initialize=False, now=self.now
            )._record_readable(
                namespace,
                payload,
                principal_id,
                scopes,
                int(payload["public_at_ms"]),
            )
        else:
            readable = MarketFinancialFactStore(
                self.conn, initialize=False, now=self.now
            )._record_readable(
                namespace,
                payload,
                principal_id,
                scopes,
                int(payload["public_at_ms"]),
            )
        if not readable:
            raise MarketQualityError("unauthorized", "current source entitlement is required")
        now_ms = _ms(self.now(), "created_at_ms")
        finding_ids = sorted({_text(item, "finding_id", 200) for item in finding_ids})
        prior = self.conn.execute(
            "SELECT status,reason,finding_ids_json,reviewer_id,created_at_ms,updated_at_ms "
            "FROM market_quality_quarantine WHERE namespace=? AND object_kind=? AND revision_id=?",
            [namespace, object_kind, revision_id],
        ).fetchone()
        if (
            prior
            and prior[0] == "quarantined"
            and prior[1] == reason
            and json.loads(prior[2]) == finding_ids
            and prior[3] == principal_id
        ):
            return {
                "contract": QUARANTINE_CONTRACT,
                "namespace": namespace,
                "object_kind": object_kind,
                "revision_id": revision_id,
                "status": "quarantined",
                "reason": reason,
                "finding_ids": finding_ids,
                "reviewer_id": principal_id,
                "created_at_ms": prior[4],
                "updated_at_ms": prior[5],
                "idempotent": True,
            }
        event_body = [namespace, object_kind, revision_id, "quarantined", reason, principal_id, now_ms]
        event_id = "market-quality-event:" + _digest(event_body)[:32]
        self.conn.execute(
            "INSERT INTO market_quality_quarantine VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(namespace,object_kind,revision_id) DO UPDATE SET "
            "status='quarantined',reason=excluded.reason,finding_ids_json=excluded.finding_ids_json,"
            "reviewer_id=excluded.reviewer_id,updated_at_ms=excluded.updated_at_ms",
            [namespace, object_kind, revision_id, "quarantined", reason, _canonical(finding_ids), principal_id, now_ms, now_ms],
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO market_quality_quarantine_events VALUES (?,?,?,?,?,?,?,?)",
            [event_id, namespace, object_kind, revision_id, "quarantined", reason, principal_id, now_ms],
        )
        return {
            "contract": QUARANTINE_CONTRACT,
            "namespace": namespace,
            "object_kind": object_kind,
            "revision_id": revision_id,
            "status": "quarantined",
            "reason": reason,
            "finding_ids": finding_ids,
            "reviewer_id": principal_id,
            "created_at_ms": prior[4] if prior else now_ms,
            "updated_at_ms": now_ms,
        }

    def release_quarantine(
        self,
        namespace: str,
        object_kind: str,
        revision_id: str,
        reason: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _quality_auth(namespace, principal_id, scopes, REVIEW_SCOPE, write=True)
        if object_kind not in _QUARANTINABLE:
            raise MarketQualityError("invalid_object_kind", "unsupported market revision kind")
        revision_id, reason = _text(revision_id, "revision_id", 300), _text(reason, "reason", 1000)
        row = self.conn.execute(
            "SELECT status,reason,reviewer_id,created_at_ms,updated_at_ms,finding_ids_json "
            "FROM market_quality_quarantine WHERE namespace=? AND object_kind=? AND revision_id=?",
            [namespace, object_kind, revision_id],
        ).fetchone()
        if not row:
            raise MarketQualityError("quarantine_not_found", "quarantine record is unavailable")
        if row[0] == "released" and row[1] == reason and row[2] == principal_id:
            return {
                "contract": QUARANTINE_CONTRACT,
                "namespace": namespace,
                "object_kind": object_kind,
                "revision_id": revision_id,
                "status": "released",
                "reason": reason,
                "finding_ids": json.loads(row[5]),
                "reviewer_id": principal_id,
                "created_at_ms": row[3],
                "updated_at_ms": row[4],
                "idempotent": True,
            }
        now_ms = _ms(self.now(), "created_at_ms")
        event_id = "market-quality-event:" + _digest(
            [namespace, object_kind, revision_id, "released", reason, principal_id, now_ms]
        )[:32]
        self.conn.execute(
            "UPDATE market_quality_quarantine SET status='released',reason=?,reviewer_id=?,updated_at_ms=? "
            "WHERE namespace=? AND object_kind=? AND revision_id=?",
            [reason, principal_id, now_ms, namespace, object_kind, revision_id],
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO market_quality_quarantine_events VALUES (?,?,?,?,?,?,?,?)",
            [event_id, namespace, object_kind, revision_id, "released", reason, principal_id, now_ms],
        )
        return {
            "contract": QUARANTINE_CONTRACT,
            "namespace": namespace,
            "object_kind": object_kind,
            "revision_id": revision_id,
            "status": "released",
            "reason": reason,
            "finding_ids": json.loads(row[5]),
            "reviewer_id": principal_id,
            "created_at_ms": row[3],
            "updated_at_ms": now_ms,
        }

    def list_quarantine(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: set[str],
        status: str = "quarantined",
        limit: int = 100,
    ) -> dict[str, Any]:
        _quality_auth(namespace, principal_id, scopes, REVIEW_SCOPE, write=False)
        if status not in {"quarantined", "released", "all"}:
            raise MarketQualityError("invalid_request", "status must be quarantined, released, or all")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise MarketQualityError("invalid_request", "limit must be between one and 500")
        clause = "" if status == "all" else " AND status=?"
        params: list[Any] = [namespace]
        if status != "all":
            params.append(status)
        params.append(limit)
        rows = self.conn.execute(
            "SELECT object_kind,revision_id,status,reason,finding_ids_json,reviewer_id,created_at_ms,updated_at_ms "
            "FROM market_quality_quarantine WHERE namespace=?" + clause +
            " ORDER BY updated_at_ms DESC,revision_id LIMIT ?",
            params,
        ).fetchall()
        return {
            "contract": QUARANTINE_CONTRACT,
            "namespace": namespace,
            "items": [
                {
                    "object_kind": row[0],
                    "revision_id": row[1],
                    "status": row[2],
                    "reason": row[3],
                    "finding_ids": json.loads(row[4]),
                    "reviewer_id": row[5],
                    "created_at_ms": row[6],
                    "updated_at_ms": row[7],
                }
                for row in rows
            ],
            "limit": limit,
        }

    def create_price_repair_plan(
        self,
        namespace: str,
        request_key: str,
        *,
        provider: str,
        listing_id: str,
        interval: str,
        start_date: str,
        end_date: str,
        principal_id: str,
        scopes: set[str],
        budget: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Create an idempotent plan that executes through MarketPriceIngestor."""

        _quality_auth(namespace, principal_id, scopes, WRITE_SCOPE, write=True)
        request_key = _text(request_key, "request_key", 200)
        try:
            start = date.fromisoformat(_text(start_date, "start_date", 10))
            end = date.fromisoformat(_text(end_date, "end_date", 10))
        except ValueError as exc:
            raise MarketQualityError("invalid_request", "repair dates must use ISO format") from exc
        provider = _text(provider, "provider", 100)
        listing_id = _text(listing_id, "listing_id", 200)
        interval = _text(interval, "interval", 20)
        budget = dict(budget or {})
        if set(budget) - {
            "requests_per_minute",
            "max_requests_per_run",
            "max_records_per_run",
            "max_records_per_page",
            "max_backfill_days",
        }:
            raise MarketQualityError("invalid_budget", "repair budget has unsupported fields")
        from src.domains.market.prices import MarketIngestBudget, MarketPriceStore

        try:
            ingest_budget = MarketIngestBudget(**budget)
            ingest_budget.validate()
        except (TypeError, ValueError) as exc:
            raise MarketQualityError("invalid_budget", "repair budget is invalid") from exc
        span = (end - start).days
        if span <= 0 or span > ingest_budget.max_backfill_days:
            raise MarketQualityError("range_too_large", "repair range exceeds its bounded budget")
        MarketPriceStore(self.conn)._require_listing(
            namespace, listing_id, principal_id=principal_id, scopes=scopes
        )
        budget = {
            "requests_per_minute": ingest_budget.requests_per_minute,
            "max_requests_per_run": ingest_budget.max_requests_per_run,
            "max_records_per_run": ingest_budget.max_records_per_run,
            "max_records_per_page": ingest_budget.max_records_per_page,
            "max_backfill_days": ingest_budget.max_backfill_days,
        }
        plan = {
            "namespace": namespace,
            "owner": principal_id,
            "request_key": request_key,
            "provider": provider,
            "listing_id": listing_id,
            "interval": interval,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "budget": budget,
        }
        request_hash = _digest(plan)
        prior = self.conn.execute(
            "SELECT request_hash,plan_json,result_json,status FROM market_quality_repair_plans "
            "WHERE namespace=? AND owner=? AND request_key=?",
            [namespace, principal_id, request_key],
        ).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise MarketQualityError("idempotency_conflict", "request_key identifies another repair plan")
            return {
                **json.loads(prior[1]),
                "status": prior[3],
                "result": json.loads(prior[2]) if prior[2] else None,
                "idempotent": True,
            }
        plan_id = "market-repair:" + _digest([namespace, principal_id, request_key])[:32]
        now_ms = _ms(self.now(), "created_at_ms")
        response = {
            "contract": REPAIR_CONTRACT,
            "plan_id": plan_id,
            **plan,
            "status": "planned",
            "checkpoint_request_key": "market-quality:" + plan_id,
            "created_at_ms": now_ms,
            "request_hash": request_hash,
            "result": None,
        }
        encoded = _canonical(response)
        self.conn.execute(
            "INSERT INTO market_quality_repair_plans VALUES (?,?,?,?,?,?,?,?,?,?)",
            [plan_id, namespace, principal_id, request_key, request_hash, "planned", encoded, None, now_ms, now_ms],
        )
        return response

    def run_price_repair_plan(
        self,
        namespace: str,
        plan_id: str,
        *,
        fetch_page,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Run/resume a plan through the existing bounded price backfill runner."""

        _quality_auth(namespace, principal_id, scopes, WRITE_SCOPE, write=True)
        row = self.conn.execute(
            "SELECT owner,status,plan_json,result_json FROM market_quality_repair_plans "
            "WHERE namespace=? AND plan_id=?",
            [namespace, _text(plan_id, "plan_id", 200)],
        ).fetchone()
        if not row:
            raise MarketQualityError("repair_plan_unavailable", "repair plan is unavailable")
        if row[0] != principal_id and "operator" not in scopes:
            raise MarketQualityError("unauthorized", "repair plan ownership is required")
        plan = json.loads(row[2])
        if row[1] == "complete" and row[3]:
            return {**plan, "status": "complete", "result": json.loads(row[3]), "idempotent": True}
        from src.domains.market.prices import MarketIngestBudget, MarketPriceError, MarketPriceIngestor, MarketPriceStore

        budget = MarketIngestBudget(**plan["budget"])
        self.conn.execute(
            "UPDATE market_quality_repair_plans SET status='running',updated_at_ms=? WHERE namespace=? AND plan_id=?",
            [_ms(self.now(), "updated_at_ms"), namespace, plan_id],
        )
        try:
            result = MarketPriceIngestor(
                MarketPriceStore(self.conn), budget=budget
            ).backfill(
                namespace,
                provider=plan["provider"],
                listing_id=plan["listing_id"],
                interval=plan["interval"],
                start_date=plan["start_date"],
                end_date=plan["end_date"],
                request_key=plan["checkpoint_request_key"],
                fetch_page=fetch_page,
                principal_id=principal_id,
                scopes=scopes,
            )
            status = result["status"]
        except MarketPriceError as exc:
            result = {"status": "failed", "error_code": exc.code}
            status = "failed"
        self.conn.execute(
            "UPDATE market_quality_repair_plans SET status=?,result_json=?,updated_at_ms=? "
            "WHERE namespace=? AND plan_id=?",
            [status, _canonical(result), _ms(self.now(), "updated_at_ms"), namespace, plan_id],
        )
        return {**plan, "status": status, "result": result}


__all__ = [
    "ASSESSMENT_CONTRACT",
    "CANDIDATE_CONTRACT",
    "CANDIDATE_LIST_CONTRACT",
    "CALCULATE_SCOPE",
    "MarketQualityError",
    "MarketQualityStore",
    "summarize_input_quality",
    "QUARANTINE_CONTRACT",
    "READ_SCOPE",
    "REPAIR_CONTRACT",
    "REVIEW_SCOPE",
    "WRITE_SCOPE",
    "ensure_market_quality_schema",
]
