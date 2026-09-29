"""Revisioned corporate actions and reproducible adjustment calculations.

Only confirmed/corrected splits and cash dividends enter the current formula.
Spin-offs, mergers, stock dividends and delistings are retained as sourced
events, but calculations crossing one fail closed until an asset-specific
valuation rule exists.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ACTION_READ_SCOPE = "market:actions:read"
ACTION_WRITE_SCOPE = "market:actions:write"
ADJUSTMENT_READ_SCOPE = "market:adjustments:read"
ADJUSTMENT_WRITE_SCOPE = "market:adjustments:write"
MAX_ACTION_ROWS = 20_000
MAX_CALCULATION_BARS = 20_000
FORMULA_VERSION = "noesis-market-adjustment-v1"
_WRITE_LOCK = threading.RLock()
_DDL = """
CREATE TABLE IF NOT EXISTS market_corporate_action_revisions (
 namespace TEXT NOT NULL,
 action_id TEXT NOT NULL,
 revision INTEGER NOT NULL,
 revision_id TEXT NOT NULL,
 security_id TEXT NOT NULL,
 listing_id TEXT,
 public_at_ms BIGINT,
 recorded_at_ms BIGINT NOT NULL,
 payload_json TEXT NOT NULL,
 record_hash TEXT NOT NULL,
 PRIMARY KEY(namespace, action_id, revision),
 UNIQUE(namespace, revision_id)
);
CREATE INDEX IF NOT EXISTS idx_market_actions_asof
 ON market_corporate_action_revisions(namespace, security_id, public_at_ms,
                                       recorded_at_ms, action_id);
CREATE TABLE IF NOT EXISTS market_price_adjustment_calculations (
 namespace TEXT NOT NULL,
 calculation_id TEXT NOT NULL,
 listing_id TEXT NOT NULL,
 public_cutoff_ms BIGINT NOT NULL,
 acquired_cutoff_ms BIGINT NOT NULL,
 recorded_at_ms BIGINT NOT NULL,
 payload_json TEXT NOT NULL,
 record_hash TEXT NOT NULL,
 PRIMARY KEY(namespace, calculation_id)
);
"""
_SUPPORTED = {"split", "reverse_split", "cash_dividend"}
_ACTION_TYPES = {
    "split",
    "reverse_split",
    "cash_dividend",
    "stock_dividend",
    "spin_off",
    "merger",
    "delisting",
    "symbol_change",
    "unknown",
}


class MarketActionError(ValueError):
    """Typed corporate-action failure safe to pass through an API adapter."""

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
        raise MarketActionError("invalid_request", "payload must be JSON-safe") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, *, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MarketActionError(
            "invalid_request", f"{field} must be bounded nonempty text"
        )
    return value.strip()


def _millis(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise MarketActionError(
            "invalid_request", f"{field} must be epoch milliseconds"
        )
    return value


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MarketActionError("invalid_request", f"{field} must be decimal") from exc
    if not number.is_finite() or (positive and number <= 0):
        raise MarketActionError(
            "invalid_request",
            f"{field} must be {'positive and ' if positive else ''}finite",
        )
    return number


def _decimal_text(value: Decimal) -> str:
    # Financial outputs are deterministic to 12 decimal places; the exact raw
    # price and event values remain available in their immutable source rows.
    text = format(value.quantize(Decimal("0.000000000001")), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _event_date(action: Mapping[str, Any]) -> date | None:
    raw = action.get("ex_date") or action.get("effective_date")
    if raw is None:
        return None
    try:
        parsed = date.fromisoformat(str(raw))
    except ValueError as exc:
        raise MarketActionError(
            "contract_invalid", "action event date is invalid"
        ) from exc
    if parsed.isoformat() != raw:
        raise MarketActionError(
            "contract_invalid", "action event date must be YYYY-MM-DD"
        )
    return parsed


@lru_cache(maxsize=8)
def _validator(schema_name: str):
    from jsonschema import Draft7Validator, FormatChecker

    path = (
        Path(__file__).resolve().parents[3]
        / "contracts/schemas/jsonschema"
        / schema_name
    )
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema, format_checker=FormatChecker())


def _validate(payload: Mapping[str, Any], schema_name: str) -> None:
    errors = sorted(
        _validator(schema_name).iter_errors(payload),
        key=lambda error: (tuple(str(part) for part in error.path), error.message),
    )
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.path) or "$"
        raise MarketActionError(
            "contract_invalid",
            f"{schema_name} rejected {location}: {first.message}",
            schema=schema_name,
            path=location,
        )


def ensure_market_action_schema(conn: Any) -> None:
    """Create the additive, append-only action and calculation tables."""

    conn.execute(_DDL)
    from src.domains.market.quality import ensure_market_quality_schema

    ensure_market_quality_schema(conn)


class MarketCorporateActionStore:
    """Revisioned action observations and persisted adjustment-factor series."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            ensure_market_action_schema(conn)

    @staticmethod
    def _authorize(
        namespace: str,
        principal_id: str,
        scopes: set[str],
        *,
        write: bool,
    ) -> None:
        _text(namespace, "namespace", limit=100)
        _text(principal_id, "principal_id", limit=200)
        if "operator" in scopes:
            return
        required = ACTION_WRITE_SCOPE if write else ACTION_READ_SCOPE
        ns_scopes = (
            {f"namespace:{namespace}:write"}
            if write
            else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
        )
        if required not in scopes or not (ns_scopes & scopes):
            raise MarketActionError(
                "unauthorized", "current market action and namespace access is required"
            )

    @staticmethod
    def _owner_for_write(
        owner: str | None, principal_id: str, scopes: set[str]
    ) -> str | None:
        if owner is None and "operator" not in scopes:
            return principal_id
        if owner not in (None, principal_id) and "operator" not in scopes:
            raise MarketActionError(
                "unauthorized", "market action data belongs to another principal"
            )
        return owner

    def _check_source_refs(
        self,
        namespace: str,
        refs: Sequence[Mapping[str, Any]],
        principal_id: str,
        scopes: set[str],
        *,
        write: bool,
    ) -> None:
        from src.domains.market.entitlements import (
            MarketEntitlementError,
            authorize_market_sources,
        )

        try:
            authorize_market_sources(
                self.conn,
                namespace,
                [dict(ref) for ref in refs],
                operation="ingest" if write else "read",
                principal_id=principal_id,
                scopes=scopes,
                now_ms=_millis(self.now(), "now_ms"),
            )
        except MarketEntitlementError as exc:
            raise MarketActionError(exc.code, exc.message, **exc.details) from exc

    def _record_readable(
        self,
        namespace: str,
        payload: Mapping[str, Any],
        principal_id: str,
        scopes: set[str],
        public_cutoff_ms: int,
    ) -> bool:
        if (
            payload.get("owner") not in (None, principal_id)
            and "operator" not in scopes
        ):
            return False
        if (
            payload.get("public_at_ms") is None
            or int(payload["public_at_ms"]) > public_cutoff_ms
        ):
            return False
        try:
            self._check_source_refs(
                namespace,
                payload.get("source_refs", []),
                principal_id,
                scopes,
                write=False,
            )
        except MarketActionError:
            return False
        return all(
            ref.get("public_at_ms") is not None
            and int(ref["public_at_ms"]) <= public_cutoff_ms
            for ref in payload.get("source_refs", [])
        )

    @staticmethod
    def _validate_action_semantics(payload: Mapping[str, Any]) -> None:
        action_type = payload["action_type"]
        _event_date(payload)
        if action_type in {"split", "reverse_split", "stock_dividend"}:
            if (
                payload.get("ratio_numerator") is None
                or payload.get("ratio_denominator") is None
            ):
                raise MarketActionError(
                    "contract_invalid",
                    "share-ratio action requires numerator and denominator",
                )
            numerator = _decimal(
                payload["ratio_numerator"], "ratio_numerator", positive=True
            )
            denominator = _decimal(
                payload["ratio_denominator"], "ratio_denominator", positive=True
            )
            if action_type == "reverse_split" and numerator >= denominator:
                raise MarketActionError(
                    "contract_invalid",
                    "reverse split ratio must represent fewer new shares",
                )
            if action_type == "split" and numerator <= denominator:
                raise MarketActionError(
                    "contract_invalid", "split ratio must represent more new shares"
                )
        if action_type == "cash_dividend":
            if payload.get("cash_amount") is None or payload.get("currency") is None:
                raise MarketActionError(
                    "contract_invalid", "cash dividend requires amount and currency"
                )
            if _decimal(payload["cash_amount"], "cash_amount") < 0:
                raise MarketActionError(
                    "contract_invalid", "cash dividend amount cannot be negative"
                )
            if payload.get("ex_date") is None:
                raise MarketActionError(
                    "contract_invalid", "cash dividend requires an ex-date"
                )
            if payload.get("distribution_type") == "not_applicable":
                raise MarketActionError(
                    "contract_invalid", "cash dividend requires a distribution type"
                )
        if (
            action_type in {"merger", "spin_off"}
            and payload.get("related_security_id") is None
        ):
            # Events remain retainable without a resolved successor, but the
            # missing identity is recorded as an explicit unsupported case.
            return

    def put_action(
        self,
        namespace: str,
        observation: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
        owner: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Validate and append one provider action observation or correction."""

        self._authorize(namespace, principal_id, scopes, write=True)
        if observation.get("contract") != "noesis-market-corporate-action-v1":
            raise MarketActionError(
                "contract_invalid",
                "action payload must use noesis-market-corporate-action-v1",
            )
        owner = self._owner_for_write(owner, principal_id, scopes)
        refs = observation.get("source_refs")
        if not isinstance(refs, list) or not refs:
            raise MarketActionError("invalid_request", "source_refs are required")
        self._check_source_refs(namespace, refs, principal_id, scopes, write=True)
        action_id = _text(observation.get("action_id"), "action_id", limit=200)
        security_id = _text(observation.get("security_id"), "security_id", limit=200)
        _text(observation.get("provider"), "provider", limit=100)
        payload = dict(observation)
        payload.setdefault("listing_id", None)
        payload.setdefault("related_security_id", None)
        payload.setdefault("related_listing_id", None)
        payload.setdefault("cash_amount_basis", "not_applicable")
        payload.setdefault("distribution_type", "not_applicable")
        payload.update({"namespace": namespace, "owner": owner})
        payload.pop("revision_id", None)
        payload.pop("revision", None)
        payload.pop("prior_revision_id", None)
        payload.pop("recorded_at_ms", None)
        payload.pop("record_hash", None)
        latest = self.conn.execute(
            """SELECT revision,revision_id,payload_json
               FROM market_corporate_action_revisions
               WHERE namespace=? AND action_id=? ORDER BY revision DESC LIMIT 1""",
            [namespace, action_id],
        ).fetchone()
        current_revision = int(latest[0]) if latest else 0
        if expected_revision is not None and expected_revision != current_revision:
            raise MarketActionError(
                "revision_conflict",
                "corporate action changed since it was read",
                expected_revision=expected_revision,
                current_revision=current_revision,
            )
        revision = current_revision + 1
        payload.update(
            {
                "revision_id": f"market-action:{action_id}@{revision}",
                "revision": revision,
                "prior_revision_id": latest[1] if latest else None,
                "recorded_at_ms": _millis(self.now(), "recorded_at_ms"),
                "record_hash": "0" * 64,
            }
        )
        _validate(payload, "noesis-market-corporate-action-v1.json")
        if payload["action_type"] not in _ACTION_TYPES:
            raise MarketActionError(
                "contract_invalid", "unsupported action_type vocabulary"
            )
        self._validate_action_semantics(payload)
        payload.update(
            {
                "recorded_at_ms": _millis(self.now(), "recorded_at_ms"),
            }
        )
        payload["record_hash"] = _digest(payload)
        _validate(payload, "noesis-market-corporate-action-v1.json")
        if latest:
            prior = json.loads(latest[2])
            ignored = {
                "revision_id",
                "revision",
                "prior_revision_id",
                "recorded_at_ms",
                "record_hash",
            }
            if {k: v for k, v in prior.items() if k not in ignored} == {
                k: v for k, v in payload.items() if k not in ignored
            }:
                return prior
        encoded = _canonical(payload)
        with _WRITE_LOCK:
            self.conn.execute("BEGIN TRANSACTION")
            try:
                self.conn.execute(
                    """INSERT INTO market_corporate_action_revisions
                       (namespace,action_id,revision,revision_id,security_id,listing_id,
                        public_at_ms,recorded_at_ms,payload_json,record_hash)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    [
                        namespace,
                        action_id,
                        revision,
                        payload["revision_id"],
                        security_id,
                        payload.get("listing_id"),
                        payload["public_at_ms"],
                        payload["recorded_at_ms"],
                        encoded,
                        payload["record_hash"],
                    ],
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return payload

    def get_actions(
        self,
        namespace: str,
        security_id: str,
        *,
        acquired_by_ms: int,
        publicly_available_by_ms: int,
        principal_id: str,
        scopes: set[str],
        limit: int = MAX_ACTION_ROWS,
        require_complete: bool = False,
    ) -> list[dict[str, Any]]:
        """Select each action's latest revision known at both requested cutoffs."""

        self._authorize(namespace, principal_id, scopes, write=False)
        security_id = _text(security_id, "security_id", limit=200)
        acquired_by_ms = _millis(acquired_by_ms, "acquired_by_ms")
        publicly_available_by_ms = _millis(
            publicly_available_by_ms, "publicly_available_by_ms"
        )
        if publicly_available_by_ms > acquired_by_ms:
            raise MarketActionError(
                "invalid_request", "public cutoff cannot be later than acquired cutoff"
            )
        if type(limit) is not int or not 1 <= limit <= MAX_ACTION_ROWS:
            raise MarketActionError(
                "invalid_request", "action limit is outside the bound"
            )
        rows = self.conn.execute(
            """WITH ranked AS (
                   SELECT action_id,revision,revision_id,recorded_at_ms,payload_json,
                     ROW_NUMBER() OVER (PARTITION BY action_id ORDER BY revision DESC) AS rn
                   FROM market_corporate_action_revisions
                   WHERE namespace=? AND security_id=? AND recorded_at_ms<=?
                     AND public_at_ms IS NOT NULL AND public_at_ms<=?
               )
               SELECT payload_json FROM ranked WHERE rn=1 AND NOT EXISTS (
                   SELECT 1 FROM market_quality_quarantine q
                   WHERE q.namespace=? AND q.object_kind='corporate_action'
                     AND q.revision_id=ranked.revision_id
                     AND q.status='quarantined'
               ) ORDER BY action_id LIMIT ?""",
            [namespace, security_id, acquired_by_ms, publicly_available_by_ms, namespace, limit],
        ).fetchall()
        result = []
        for (encoded,) in rows:
            payload = json.loads(encoded)
            if self._record_readable(
                namespace, payload, principal_id, scopes, publicly_available_by_ms
            ):
                result.append(payload)
            elif require_complete:
                raise MarketActionError(
                    "action_history_unavailable",
                    "current entitlement or ownership does not allow a complete action history",
                )
        return result

    def calculate_adjusted_series(
        self,
        namespace: str,
        security_id: str,
        listing_id: str,
        bars: Sequence[Mapping[str, Any]],
        *,
        listing_timezone: str,
        acquired_by_ms: int,
        publicly_available_by_ms: int,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Persist point-in-time split-adjusted and reinvested total-return closes.

        Daily bars must be raw. Cash distributions are reinvested at the ex-date
        close, before taxes, fees or FX. The returned factors are anchored so the
        final adjusted close equals the final raw close.
        """

        self._authorize(namespace, principal_id, scopes, write=False)
        if "operator" not in scopes:
            required = {
                "market:prices:read",
                ADJUSTMENT_WRITE_SCOPE,
                "knowledge:quantitative:calculate",
            }
            ns_scopes = {
                f"namespace:{namespace}:read",
                f"namespace:{namespace}:write",
            }
            if not required.issubset(scopes) or not (ns_scopes & scopes):
                raise MarketActionError(
                    "unauthorized", "price and adjustment access is required"
                )
        acquired_by_ms = _millis(acquired_by_ms, "acquired_by_ms")
        public_cutoff = _millis(publicly_available_by_ms, "publicly_available_by_ms")
        if public_cutoff > acquired_by_ms:
            raise MarketActionError(
                "invalid_request", "public cutoff cannot be later than acquired cutoff"
            )
        listing_id = _text(listing_id, "listing_id", limit=200)
        security_id = _text(security_id, "security_id", limit=200)
        if not isinstance(bars, Sequence) or isinstance(bars, (str, bytes)):
            raise MarketActionError(
                "invalid_request", "bars must be a bounded sequence"
            )
        if not 1 <= len(bars) <= MAX_CALCULATION_BARS:
            raise MarketActionError("invalid_request", "bar count is outside the bound")
        try:
            venue_tz = ZoneInfo(_text(listing_timezone, "listing_timezone", limit=100))
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise MarketActionError(
                "invalid_request", "listing timezone is not a known IANA zone"
            ) from exc

        from src.domains.market.prices import MarketPriceStore

        MarketPriceStore._authorize(namespace, principal_id, scopes, write=False)
        price_store = MarketPriceStore(self.conn, initialize=False, now=self.now)
        normalized_bars = []
        for raw in bars:
            bar = dict(raw)
            if bar.get("listing_id") != listing_id:
                raise MarketActionError(
                    "listing_mismatch", "all bars must use the requested listing"
                )
            if bar.get("interval") != "1d":
                raise MarketActionError(
                    "interval_unsupported", "adjustments require daily bars"
                )
            if bar.get("price_basis") != "unadjusted":
                raise MarketActionError(
                    "double_adjustment",
                    "provider-adjusted bars cannot be adjusted again",
                )
            if int(bar.get("recorded_at_ms", acquired_by_ms)) > acquired_by_ms:
                raise MarketActionError(
                    "bar_not_acquired",
                    "bar revision is later than the acquisition cutoff",
                )
            if (
                bar.get("public_at_ms") is None
                or int(bar["public_at_ms"]) > public_cutoff
            ):
                raise MarketActionError(
                    "bar_not_public",
                    "bar revision was not public by the requested cutoff",
                )
            if not price_store._record_readable(
                namespace, bar, principal_id, scopes, public_cutoff
            ):
                raise MarketActionError(
                    "bar_unavailable",
                    "current ownership or source entitlement blocks a required bar",
                )
            start_ms = _millis(bar.get("bar_start_ms"), "bar_start_ms")
            session_date = datetime.fromtimestamp(start_ms / 1000, tz=venue_tz).date()
            close = _decimal(bar.get("close"), "close", positive=True)
            currency = _text(bar.get("currency"), "currency", limit=3)
            if len(currency) != 3 or currency.upper() != currency:
                raise MarketActionError(
                    "invalid_request", "bar currency must be ISO uppercase"
                )
            normalized_bars.append(
                {
                    "session_date": session_date,
                    "bar_revision_id": _text(
                        bar.get("revision_id"), "bar.revision_id", limit=300
                    ),
                    "close": close,
                    "currency": currency,
                    "public_at_ms": int(bar["public_at_ms"]),
                    "recorded_at_ms": int(bar.get("recorded_at_ms", 0)),
                    "source_refs": bar.get("source_refs", []),
                }
            )
        normalized_bars.sort(
            key=lambda item: (item["session_date"], item["bar_revision_id"])
        )
        if not normalized_bars:
            raise MarketActionError(
                "no_eligible_bars",
                "no raw daily bars are public and acquired by the requested cutoffs",
            )
        if len({row["session_date"] for row in normalized_bars}) != len(
            normalized_bars
        ):
            raise MarketActionError(
                "duplicate_session",
                "more than one eligible bar maps to a venue session",
            )
        currencies = {row["currency"] for row in normalized_bars}
        if len(currencies) != 1:
            raise MarketActionError(
                "currency_mismatch", "bar currency changed within the series"
            )

        actions = self.get_actions(
            namespace,
            security_id,
            acquired_by_ms=acquired_by_ms,
            publicly_available_by_ms=public_cutoff,
            principal_id=principal_id,
            scopes=scopes,
            require_complete=True,
        )
        first_date, last_date = (
            normalized_bars[0]["session_date"],
            normalized_bars[-1]["session_date"],
        )
        relevant_actions: list[tuple[dict[str, Any], date]] = []
        active_status = {"confirmed", "corrected"}
        unsupported = []
        for action in actions:
            if action.get("listing_id") not in (None, listing_id):
                continue
            event_date = _event_date(action)
            if action.get("status") not in active_status:
                continue
            if event_date is None:
                unsupported.append(action)
                continue
            if not first_date < event_date <= last_date:
                continue
            if action.get("action_type") not in _SUPPORTED:
                unsupported.append(action)
                continue
            if action.get("action_type") in {"split", "reverse_split"}:
                numerator = _decimal(
                    action.get("ratio_numerator"), "ratio_numerator", positive=True
                )
                denominator = _decimal(
                    action.get("ratio_denominator"), "ratio_denominator", positive=True
                )
                if action["action_type"] == "split" and numerator <= denominator:
                    raise MarketActionError(
                        "invalid_split_ratio", "split ratio must exceed one"
                    )
                if (
                    action["action_type"] == "reverse_split"
                    and numerator >= denominator
                ):
                    raise MarketActionError(
                        "invalid_split_ratio", "reverse split ratio must be below one"
                    )
            if action.get("action_type") == "cash_dividend":
                if action.get("currency") != next(iter(currencies)):
                    raise MarketActionError(
                        "currency_mismatch",
                        "dividend currency conversion is unsupported",
                    )
                if action.get("cash_amount_basis") not in {
                    "per_share_before_action",
                    "per_share_after_action",
                }:
                    raise MarketActionError(
                        "dividend_basis_unknown",
                        "cash dividend share basis is not explicit",
                    )
            relevant_actions.append((action, event_date))

        source_refs = [
            dict(ref)
            for bar in normalized_bars
            for ref in bar.get("source_refs", [])
        ] + [
            dict(ref)
            for action, _ in relevant_actions
            for ref in action.get("source_refs", [])
        ]
        from src.domains.market.entitlements import (
            MarketEntitlementError,
            authorize_market_sources,
        )

        try:
            entitlement_decisions = authorize_market_sources(
                self.conn,
                namespace,
                source_refs,
                operation="derive",
                principal_id=principal_id,
                scopes=scopes,
                now_ms=_millis(self.now(), "now_ms"),
            )
        except MarketEntitlementError as exc:
            raise MarketActionError(exc.code, exc.message, **exc.details) from exc
        entitlement_by_ref = {
            item["source_ref_id"]: item for item in entitlement_decisions
        }
        for ref in source_refs:
            decision = entitlement_by_ref.get(str(ref.get("source_ref_id") or ""))
            if decision:
                ref["policy_revision_id"] = decision["policy_revision_id"]
        if unsupported:
            raise MarketActionError(
                "unsupported_action",
                "series crosses one or more retained actions without an approved valuation rule",
                action_revision_ids=sorted(a["revision_id"] for a in unsupported),
                action_types=sorted({a["action_type"] for a in unsupported}),
            )
        observed_sessions = {bar["session_date"] for bar in normalized_bars}
        missing_action_sessions = sorted(
            event_date.isoformat()
            for _, event_date in relevant_actions
            if event_date not in observed_sessions
        )
        if missing_action_sessions:
            raise MarketActionError(
                "action_session_missing",
                "a supported action ex-date has no eligible daily bar; refusing to reinvest at a later close",
                session_dates=missing_action_sessions,
            )

        # Conflicting providers for the same economic event must not be added
        # together. Different dividend classes on one date remain distinct.
        share_ratios: dict[date, set[tuple[str, str]]] = defaultdict(set)
        dividend_amounts: dict[tuple[date, str, str, str], set[str]] = defaultdict(set)
        for action, event_date in relevant_actions:
            if action["action_type"] in {"split", "reverse_split"}:
                share_ratios[event_date].add(
                    (
                        str(
                            _decimal(
                                action["ratio_numerator"],
                                "ratio_numerator",
                                positive=True,
                            )
                        ),
                        str(
                            _decimal(
                                action["ratio_denominator"],
                                "ratio_denominator",
                                positive=True,
                            )
                        ),
                    )
                )
            else:
                key = (
                    event_date,
                    action["currency"],
                    action["distribution_type"],
                    action["cash_amount_basis"],
                )
                dividend_amounts[key].add(
                    str(_decimal(action["cash_amount"], "cash_amount"))
                )
        if any(len(values) > 1 for values in share_ratios.values()) or any(
            len(values) > 1 for values in dividend_amounts.values()
        ):
            raise MarketActionError(
                "conflicting_actions",
                "providers disagree on a split ratio or same-class cash distribution",
            )

        # Exact duplicate observations from multiple vendors represent one
        # economic event. Keep every source revision in provenance, apply once.
        grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for action, event_date in relevant_actions:
            if action["action_type"] in {"split", "reverse_split"}:
                signature = (
                    action["action_type"],
                    event_date.isoformat(),
                    str(
                        _decimal(
                            action["ratio_numerator"], "ratio_numerator", positive=True
                        )
                    ),
                    str(
                        _decimal(
                            action["ratio_denominator"],
                            "ratio_denominator",
                            positive=True,
                        )
                    ),
                )
            else:
                signature = (
                    action["action_type"],
                    event_date.isoformat(),
                    action["currency"],
                    action["distribution_type"],
                    str(_decimal(action["cash_amount"], "cash_amount")),
                    action["cash_amount_basis"],
                )
            grouped[signature].append(action)
        unique_events = []
        for signature, observations in sorted(
            grouped.items(), key=lambda item: item[0]
        ):
            event = min(
                observations, key=lambda row: (row["provider"], row["revision_id"])
            )
            unique_events.append((event, date.fromisoformat(signature[1])))
        unique_events.sort(
            key=lambda pair: (pair[1], pair[0]["action_type"], pair[0]["action_id"])
        )

        ratios_by_date: dict[date, Decimal] = defaultdict(lambda: Decimal(1))
        dividends_by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
        for action, event_date in unique_events:
            if action["action_type"] in {"split", "reverse_split"}:
                with localcontext() as context:
                    context.prec = 28
                    ratios_by_date[event_date] *= _decimal(
                        action["ratio_numerator"], "ratio_numerator", positive=True
                    ) / _decimal(
                        action["ratio_denominator"], "ratio_denominator", positive=True
                    )
            else:
                dividends_by_date[event_date].append(action)

        with localcontext() as context:
            context.prec = 28
            rows = []
            price_index = Decimal(100)
            total_index = Decimal(100)
            units = Decimal(1)
            previous = None
            for bar in normalized_bars:
                day = bar["session_date"]
                if previous is not None:
                    period_days = sorted(
                        event_day
                        for event_day in set(ratios_by_date) | set(dividends_by_date)
                        if previous["session_date"] < event_day <= day
                    )
                    period_ratio = Decimal(1)
                    period_dividends = []
                    for event_day in period_days:
                        period_ratio *= ratios_by_date[event_day]
                        period_dividends.extend(dividends_by_date[event_day])
                    previous_units = units
                    split_units = previous_units * period_ratio
                    price_gross = (split_units * bar["close"]) / (
                        previous_units * previous["close"]
                    )
                    price_index *= price_gross
                    dividend_cash = Decimal(0)
                    for dividend in period_dividends:
                        amount = _decimal(dividend["cash_amount"], "cash_amount")
                        shares = (
                            previous_units
                            if dividend["cash_amount_basis"]
                            == "per_share_before_action"
                            else split_units
                        )
                        dividend_cash += shares * amount
                    units = split_units + (dividend_cash / bar["close"])
                    total_index *= (units * bar["close"]) / (
                        previous_units * previous["close"]
                    )
                rows.append(
                    {
                        "session_date": day.isoformat(),
                        "bar_revision_id": bar["bar_revision_id"],
                        "raw_close": _decimal_text(bar["close"]),
                        "split_factor": "1",  # filled after the terminal date is known
                        "split_adjusted_close": "1",
                        "price_return_index": _decimal_text(price_index),
                        "total_return_index": _decimal_text(total_index),
                        "total_return_adjustment_factor": "1",
                        "total_return_adjusted_close": "1",
                    }
                )
                previous = bar
            terminal_close = normalized_bars[-1]["close"]
            terminal_total_index = Decimal(rows[-1]["total_return_index"])
            split_events = [
                (action, event_date)
                for action, event_date in unique_events
                if action["action_type"] in {"split", "reverse_split"}
            ]
            for row in rows:
                day = date.fromisoformat(row["session_date"])
                split_factor = Decimal(1)
                for action, event_date in split_events:
                    if day < event_date <= last_date:
                        split_factor *= _decimal(
                            action["ratio_denominator"],
                            "ratio_denominator",
                            positive=True,
                        ) / _decimal(
                            action["ratio_numerator"], "ratio_numerator", positive=True
                        )
                raw_close = Decimal(row["raw_close"])
                tr_index = Decimal(row["total_return_index"])
                tr_factor = (tr_index / terminal_total_index) * (
                    terminal_close / raw_close
                )
                row["split_factor"] = _decimal_text(split_factor)
                row["split_adjusted_close"] = _decimal_text(raw_close * split_factor)
                row["total_return_adjustment_factor"] = _decimal_text(tr_factor)
                row["total_return_adjusted_close"] = _decimal_text(
                    raw_close * tr_factor
                )

        action_revision_ids = sorted(
            action["revision_id"] for action, _ in relevant_actions
        )
        source_entitlement_ids = sorted(
            {
                str(ref["entitlement_id"])
                for bar in normalized_bars
                for ref in bar["source_refs"]
                if ref.get("entitlement_id")
            }
            | {
                str(ref["entitlement_id"])
                for action, _ in relevant_actions
                for ref in action.get("source_refs", [])
                if ref.get("entitlement_id")
            }
        )
        source_entitlements = {
            str(ref["source_ref_id"]): {
                "source_ref_id": str(ref["source_ref_id"]),
                "provider": str(ref["provider"]),
                "license_id": str(ref["license_id"]),
                "entitlement_id": str(ref["entitlement_id"]),
                "retrieved_at_ms": int(ref["retrieved_at_ms"]),
                "policy_revision_id": str(ref["policy_revision_id"]),
                "authorized_operation": "derive",
            }
            for ref in source_refs
        }
        input_manifest = {
            "formula_version": FORMULA_VERSION,
            "namespace": namespace,
            "owner": None if "operator" in scopes else principal_id,
            "security_id": security_id,
            "listing_id": listing_id,
            "listing_timezone": listing_timezone,
            "public_cutoff_ms": public_cutoff,
            "acquired_cutoff_ms": acquired_by_ms,
            "bar_revision_ids": [bar["bar_revision_id"] for bar in normalized_bars],
            "action_revision_ids": action_revision_ids,
            "currency": next(iter(currencies)),
            "reinvestment_policy": "cash_dividends_reinvested_at_ex_date_close",
            "source_entitlement_ids": source_entitlement_ids,
            "source_entitlements": [
                source_entitlements[key] for key in sorted(source_entitlements)
            ],
        }
        input_hash = _digest(input_manifest)
        calculation_id = "adjustment:" + input_hash[:40]
        existing = self.conn.execute(
            "SELECT payload_json FROM market_price_adjustment_calculations WHERE namespace=? AND calculation_id=?",
            [namespace, calculation_id],
        ).fetchone()
        if existing:
            return json.loads(existing[0])

        from src.kb.quantitative import QuantitativeStore

        quantitative = QuantitativeStore(self.conn, initialize=True, now=self.now)
        receipt = quantitative.record_domain_calculation(
            namespace,
            "market-adjustment",
            input_manifest,
            {
                "input_hash": input_hash,
                "price_return_index": [row["price_return_index"] for row in rows],
                "split_factors": [row["split_factor"] for row in rows],
                "total_return_index": [row["total_return_index"] for row in rows],
                "total_return_adjustment_factors": [
                    row["total_return_adjustment_factor"] for row in rows
                ],
            },
            input_ids=[
                *input_manifest["bar_revision_ids"],
                *action_revision_ids,
            ],
            principal_id=principal_id,
            scopes=scopes,
            formula_revision_id=FORMULA_VERSION,
        )
        output = {
            "contract": "noesis-market-adjustment-calculation-v1",
            "namespace": namespace,
            "owner": None if "operator" in scopes else principal_id,
            "calculation_id": calculation_id,
            "quantitative_calculation_id": receipt["calculation_id"],
            "formula_version": FORMULA_VERSION,
            "security_id": security_id,
            "listing_id": listing_id,
            "currency": next(iter(currencies)),
            "public_cutoff_ms": public_cutoff,
            "acquired_cutoff_ms": acquired_by_ms,
            "recorded_at_ms": int(receipt["created_at_ms"]),
            "reinvestment_policy": "cash_dividends_reinvested_at_ex_date_close",
            "bar_revision_ids": input_manifest["bar_revision_ids"],
            "action_revision_ids": action_revision_ids,
            "source_entitlement_ids": source_entitlement_ids,
            "source_entitlements": input_manifest["source_entitlements"],
            "input_hash": input_hash,
            "series": rows,
        }
        output["record_hash"] = _digest(output)
        _validate(output, "noesis-market-adjustment-calculation-v1.json")
        encoded = _canonical(output)
        with _WRITE_LOCK:
            self.conn.execute(
                """INSERT INTO market_price_adjustment_calculations
                   (namespace,calculation_id,listing_id,public_cutoff_ms,
                    acquired_cutoff_ms,recorded_at_ms,payload_json,record_hash)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [
                    namespace,
                    calculation_id,
                    listing_id,
                    public_cutoff,
                    acquired_by_ms,
                    output["recorded_at_ms"],
                    encoded,
                    output["record_hash"],
                ],
            )
        return output

    def get_calculation(
        self,
        namespace: str,
        calculation_id: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any] | None:
        """Read a previously persisted calculation under namespace ownership."""

        self._authorize(namespace, principal_id, scopes, write=False)
        if "operator" not in scopes and not {
            ADJUSTMENT_READ_SCOPE,
            "market:prices:read",
        }.issubset(scopes):
            raise MarketActionError(
                "unauthorized", "price and market adjustment read access is required"
            )
        row = self.conn.execute(
            "SELECT payload_json FROM market_price_adjustment_calculations WHERE namespace=? AND calculation_id=?",
            [namespace, _text(calculation_id, "calculation_id", limit=200)],
        ).fetchone()
        if not row:
            return None
        payload = json.loads(row[0])
        if (
            payload.get("owner") not in (None, principal_id)
            and "operator" not in scopes
        ):
            return None
        try:
            from src.domains.market.entitlements import (
                MarketEntitlementError,
                authorize_market_sources,
            )

            authorize_market_sources(
                self.conn,
                namespace,
                payload.get("source_entitlements", []),
                operation="derive",
                principal_id=principal_id,
                scopes=scopes,
                now_ms=_millis(self.now(), "now_ms"),
            )
        except MarketEntitlementError:
            return None
        return payload
