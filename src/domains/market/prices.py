"""Local-first, revisioned end-of-day bars and exchange-session coverage.

Provider-specific HTTP, credentials and licensing plans are injected by a later
connector. This module accepts normalized contract payloads, retains source
revisions, and provides a bounded/resumable page runner without assuming vendor
request limits.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time as daytime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PRICE_READ_SCOPE = "market:prices:read"
PRICE_WRITE_SCOPE = "market:prices:write"
MAX_HISTORY_ROWS = 20_000
MAX_BACKFILL_DAYS = 36_600
MAX_REQUESTS_PER_RUN = 2_000
MAX_RECORDS_PER_RUN = 200_000
MAX_RECORDS_PER_PAGE = 10_000
MAX_REQUESTS_PER_MINUTE = 60_000
_WRITE_LOCK = threading.RLock()
_UTC = timezone.utc
_DDL = """
CREATE TABLE IF NOT EXISTS market_price_bar_revisions (
 namespace TEXT NOT NULL,
 bar_id TEXT NOT NULL,
 revision INTEGER NOT NULL,
 revision_id TEXT NOT NULL,
 listing_id TEXT NOT NULL,
 provider TEXT NOT NULL,
 interval TEXT NOT NULL,
 bar_start_ms BIGINT NOT NULL,
 public_at_ms BIGINT,
 retrieved_at_ms BIGINT NOT NULL,
 owner TEXT,
 payload_json TEXT NOT NULL,
 record_hash TEXT NOT NULL,
 recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, bar_id, revision),
 UNIQUE(namespace, revision_id)
);
CREATE INDEX IF NOT EXISTS idx_market_price_history
 ON market_price_bar_revisions(namespace, listing_id, interval, bar_start_ms,
                                public_at_ms, recorded_at_ms);
CREATE TABLE IF NOT EXISTS market_trading_session_revisions (
 namespace TEXT NOT NULL,
 calendar_id TEXT NOT NULL,
 mic TEXT NOT NULL,
 session_date TEXT NOT NULL,
 revision INTEGER NOT NULL,
 revision_id TEXT NOT NULL,
 owner TEXT,
 public_at_ms BIGINT,
 retrieved_at_ms BIGINT NOT NULL,
 payload_json TEXT NOT NULL,
 record_hash TEXT NOT NULL,
 recorded_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, calendar_id, mic, session_date, revision),
 UNIQUE(namespace, revision_id)
);
CREATE INDEX IF NOT EXISTS idx_market_trading_sessions
 ON market_trading_session_revisions(namespace, calendar_id, mic, session_date,
                                      public_at_ms, recorded_at_ms);
CREATE TABLE IF NOT EXISTS market_price_ingest_checkpoints (
 namespace TEXT NOT NULL,
 owner TEXT NOT NULL,
 provider TEXT NOT NULL,
 listing_id TEXT NOT NULL,
 interval TEXT NOT NULL,
 request_key TEXT NOT NULL,
 request_hash TEXT NOT NULL,
 start_date TEXT NOT NULL,
 end_date TEXT NOT NULL,
 cursor TEXT,
 status TEXT NOT NULL,
 records_processed BIGINT NOT NULL,
 requests_made BIGINT NOT NULL,
 last_error_code TEXT,
 updated_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace, owner, provider, listing_id, interval, request_key)
);
"""


class MarketPriceError(ValueError):
    """Typed price ingest/history error without provider credential details."""

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
        raise MarketPriceError("invalid_request", "payload must be JSON-safe") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: Any, field: str, *, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise MarketPriceError(
            "invalid_request", f"{field} must be bounded nonempty text"
        )
    return value.strip()


def _millis(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise MarketPriceError("invalid_request", f"{field} must be epoch milliseconds")
    return value


def _date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise MarketPriceError("invalid_request", f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise MarketPriceError(
            "invalid_request", f"{field} must be an ISO date"
        ) from exc
    if parsed.isoformat() != value:
        raise MarketPriceError("invalid_request", f"{field} must be YYYY-MM-DD")
    return parsed


@lru_cache(maxsize=8)
def _validator(schema_name: str):
    from jsonschema import Draft7Validator

    path = (
        Path(__file__).resolve().parents[3]
        / "contracts/schemas/jsonschema"
        / schema_name
    )
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)


def _validate(payload: Mapping[str, Any], schema_name: str) -> None:
    errors = sorted(
        _validator(schema_name).iter_errors(payload),
        key=lambda error: (tuple(str(part) for part in error.path), error.message),
    )
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.path) or "$"
        raise MarketPriceError(
            "contract_invalid",
            f"{schema_name} rejected {location}: {first.message}",
            schema=schema_name,
            path=location,
        )


def ensure_market_price_schema(conn: Any) -> None:
    """Create additive OHLCV, session-calendar and checkpoint tables."""

    conn.execute(_DDL)
    from src.domains.market.quality import ensure_market_quality_schema

    ensure_market_quality_schema(conn)


class MarketPriceStore:
    """Immutable provider observations and separately versioned session calendars."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            ensure_market_price_schema(conn)

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
        required = PRICE_WRITE_SCOPE if write else PRICE_READ_SCOPE
        ns_scopes = (
            {f"namespace:{namespace}:write"}
            if write
            else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
        )
        if required not in scopes or not (ns_scopes & scopes):
            raise MarketPriceError(
                "unauthorized", "current market and namespace access is required"
            )

    @staticmethod
    def _owner_for_write(
        owner: str | None, principal_id: str, scopes: set[str]
    ) -> str | None:
        if owner is None and "operator" not in scopes:
            return principal_id
        if owner not in (None, principal_id) and "operator" not in scopes:
            raise MarketPriceError(
                "unauthorized", "market price data belongs to another principal"
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
            raise MarketPriceError(exc.code, exc.message, **exc.details) from exc

    def _record_readable(
        self,
        namespace: str,
        payload: Mapping[str, Any],
        principal_id: str,
        scopes: set[str],
        publicly_available_by_ms: int | None,
    ) -> bool:
        if (
            payload.get("owner") not in (None, principal_id)
            and "operator" not in scopes
        ):
            return False
        refs = payload.get("source_refs", [])
        try:
            self._check_source_refs(namespace, refs, principal_id, scopes, write=False)
        except MarketPriceError:
            return False
        if publicly_available_by_ms is not None and not refs:
            return False
        if publicly_available_by_ms is not None and not all(
            ref.get("public_at_ms") is not None
            and int(ref["public_at_ms"]) <= publicly_available_by_ms
            for ref in refs
        ):
            return False
        return True

    def _require_listing(
        self,
        namespace: str,
        listing_id: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        from src.domains.market.instruments import (
            MarketInstrumentError,
            MarketInstrumentStore,
        )

        try:
            return MarketInstrumentStore(self.conn, initialize=False).get_instrument(
                namespace,
                "listing",
                listing_id,
                acquired_by_ms=_millis(self.now(), "now_ms"),
                principal_id=principal_id,
                scopes=scopes,
            )
        except MarketInstrumentError as exc:
            raise MarketPriceError(
                "listing_unavailable", "listing identity is unavailable"
            ) from exc

    @staticmethod
    def bar_id(provider: str, listing_id: str, interval: str, bar_start_ms: int) -> str:
        key = f"{provider}\n{listing_id}\n{interval}\n{bar_start_ms}"
        return "bar:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

    def put_bar(
        self,
        namespace: str,
        observation: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
        owner: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Validate and append one original (unadjusted or provider-adjusted) bar."""

        self._authorize(namespace, principal_id, scopes, write=True)
        listing_id = _text(observation.get("listing_id"), "listing_id", limit=200)
        listing = self._require_listing(
            namespace, listing_id, principal_id=principal_id, scopes=scopes
        )
        if observation.get("contract") != "noesis-market-bar-v1":
            raise MarketPriceError(
                "contract_invalid", "bar payload must use noesis-market-bar-v1"
            )
        owner = self._owner_for_write(owner, principal_id, scopes)
        refs = observation.get("source_refs")
        if not isinstance(refs, list) or not refs:
            raise MarketPriceError("invalid_request", "source_refs are required")
        self._check_source_refs(namespace, refs, principal_id, scopes, write=True)
        provider = _text(observation.get("provider"), "provider", limit=100)
        interval = _text(observation.get("interval"), "interval", limit=20)
        bar_start_ms = _millis(observation.get("bar_start_ms"), "bar_start_ms")
        bar_id = self.bar_id(provider, listing_id, interval, bar_start_ms)
        latest = self.conn.execute(
            """SELECT revision,revision_id,payload_json FROM market_price_bar_revisions
               WHERE namespace=? AND bar_id=? ORDER BY revision DESC LIMIT 1""",
            [namespace, bar_id],
        ).fetchone()
        current_revision = int(latest[0]) if latest else 0
        if expected_revision is not None and expected_revision != current_revision:
            raise MarketPriceError(
                "revision_conflict",
                "bar changed since it was read",
                expected_revision=expected_revision,
                current_revision=current_revision,
            )
        revision = current_revision + 1
        recorded_at_ms = _millis(self.now(), "recorded_at_ms")
        payload = dict(observation)
        payload.update(
            {
                "namespace": namespace,
                "owner": owner,
                "bar_id": bar_id,
                "revision_id": f"market-bar:{bar_id}@{revision}",
                "revision": revision,
                "prior_revision_id": latest[1] if latest else None,
                "recorded_at_ms": recorded_at_ms,
            }
        )
        payload.pop("record_hash", None)
        payload["record_hash"] = _digest(payload)
        _validate(payload, "noesis-market-bar-v1.json")
        if payload["currency"] != listing["currency"]:
            raise MarketPriceError(
                "currency_mismatch", "bar currency must match the listing currency"
            )
        if latest:
            prior_payload = json.loads(latest[2])
            if (
                payload.get("provider_revision_id") is not None
                and payload.get("provider_revision_id")
                == prior_payload.get("provider_revision_id")
                and {
                    ref.get("content_hash") for ref in payload.get("source_refs", [])
                }
                == {
                    ref.get("content_hash")
                    for ref in prior_payload.get("source_refs", [])
                }
            ):
                return prior_payload
            if self._semantic_bar(prior_payload) == self._semantic_bar(payload):
                return prior_payload
        encoded = _canonical(payload)
        with _WRITE_LOCK:
            self.conn.execute("BEGIN TRANSACTION")
            try:
                self.conn.execute(
                    """INSERT INTO market_price_bar_revisions
                       (namespace,bar_id,revision,revision_id,listing_id,provider,
                        interval,bar_start_ms,public_at_ms,retrieved_at_ms,owner,
                        payload_json,record_hash,recorded_at_ms)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [
                        namespace,
                        bar_id,
                        revision,
                        payload["revision_id"],
                        listing_id,
                        provider,
                        interval,
                        bar_start_ms,
                        payload["public_at_ms"],
                        payload["retrieved_at_ms"],
                        owner,
                        encoded,
                        payload["record_hash"],
                        recorded_at_ms,
                    ],
                )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise
        return payload

    @staticmethod
    def _semantic_bar(payload: Mapping[str, Any]) -> dict[str, Any]:
        ignored = {
            "revision_id",
            "revision",
            "prior_revision_id",
            "recorded_at_ms",
            "record_hash",
        }
        return {key: value for key, value in payload.items() if key not in ignored}

    def get_bars(
        self,
        namespace: str,
        listing_id: str,
        *,
        start_ms: int,
        end_ms: int,
        acquired_by_ms: int,
        publicly_available_by_ms: int | None = None,
        principal_id: str,
        scopes: set[str],
        limit: int = MAX_HISTORY_ROWS,
        offset: int = 0,
        include_page: bool = False,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """Return the latest retained revision per bar under explicit cutoffs."""

        self._authorize(namespace, principal_id, scopes, write=False)
        listing_id = _text(listing_id, "listing_id", limit=200)
        start_ms, end_ms = _millis(start_ms, "start_ms"), _millis(end_ms, "end_ms")
        acquired_by_ms = _millis(acquired_by_ms, "acquired_by_ms")
        if start_ms >= end_ms:
            raise MarketPriceError("invalid_request", "start_ms must precede end_ms")
        if end_ms - start_ms > MAX_BACKFILL_DAYS * 86_400_000:
            raise MarketPriceError(
                "range_too_large", "history range exceeds the bounded maximum"
            )
        if type(limit) is not int or not 1 <= limit <= MAX_HISTORY_ROWS:
            raise MarketPriceError(
                "invalid_request", "history limit is outside the bounded maximum"
            )
        if type(offset) is not int or not 0 <= offset <= MAX_HISTORY_ROWS:
            raise MarketPriceError(
                "invalid_request", "history offset is outside the bounded maximum"
            )
        if publicly_available_by_ms is not None:
            publicly_available_by_ms = _millis(
                publicly_available_by_ms, "publicly_available_by_ms"
            )
            if publicly_available_by_ms > acquired_by_ms:
                raise MarketPriceError(
                    "invalid_request",
                    "public cutoff cannot be later than acquired cutoff",
                )
        clauses = [
            "namespace=?",
            "listing_id=?",
            "bar_start_ms>=?",
            "bar_start_ms<?",
            "recorded_at_ms<=?",
        ]
        params: list[Any] = [namespace, listing_id, start_ms, end_ms, acquired_by_ms]
        if publicly_available_by_ms is not None:
            clauses.append("public_at_ms IS NOT NULL AND public_at_ms<=?")
            params.append(publicly_available_by_ms)
        rows = self.conn.execute(
            """WITH ranked AS (
                   SELECT bar_id,bar_start_ms,revision,revision_id,payload_json,
                          ROW_NUMBER() OVER (PARTITION BY bar_id ORDER BY revision DESC) AS row_number
                   FROM market_price_bar_revisions WHERE """
            + " AND ".join(clauses)
            + """
               )
               SELECT bar_id,revision,payload_json FROM ranked
               WHERE row_number=1 AND NOT EXISTS (
                   SELECT 1 FROM market_quality_quarantine q
                   WHERE q.namespace=? AND q.object_kind='price_bar'
                     AND q.revision_id=ranked.revision_id
                     AND q.status='quarantined'
               )
               ORDER BY bar_start_ms,bar_id LIMIT ? OFFSET ?""",
            params + [namespace, min(limit + 1, MAX_HISTORY_ROWS), offset],
        ).fetchall()
        selected: dict[str, dict[str, Any]] = {}
        for bar_id, _, encoded in rows[:limit]:
            if bar_id in selected:
                continue
            payload = json.loads(encoded)
            if self._record_readable(
                namespace, payload, principal_id, scopes, publicly_available_by_ms
            ):
                selected[bar_id] = payload
            else:
                selected[bar_id] = {}
        results = [payload for payload in selected.values() if payload]
        ordered = sorted(
            results, key=lambda item: (item["bar_start_ms"], item["bar_id"])
        )
        items = ordered[:limit]
        if include_page:
            quarantined_rows = self.conn.execute(
                """WITH ranked AS (
                       SELECT bar_id,revision_id,payload_json,
                              ROW_NUMBER() OVER (PARTITION BY bar_id ORDER BY revision DESC) AS row_number
                       FROM market_price_bar_revisions WHERE """
                + " AND ".join(clauses)
                + """
                   )
                   SELECT ranked.payload_json,q.revision_id,q.reason
                   FROM ranked JOIN market_quality_quarantine q
                     ON q.namespace=? AND q.object_kind='price_bar'
                    AND q.revision_id=ranked.revision_id AND q.status='quarantined'
                   WHERE ranked.row_number=1 ORDER BY ranked.bar_id LIMIT 1001""",
                params + [namespace],
            ).fetchall()
            quality_exclusions = []
            for encoded, revision_id, reason in quarantined_rows[:1000]:
                payload = json.loads(encoded)
                if self._record_readable(
                    namespace,
                    payload,
                    principal_id,
                    scopes,
                    publicly_available_by_ms,
                ):
                    quality_exclusions.append(
                        {
                            "object_kind": "price_bar",
                            "revision_id": str(revision_id),
                            "reason": str(reason),
                        }
                    )
            has_more = len(rows) > limit
            scanned = min(len(rows), limit)
            return {
                "items": items,
                "next_offset": offset + scanned if has_more else None,
                "scanned": scanned,
                "quality_exclusions": quality_exclusions,
                "quality_exclusions_truncated": len(quarantined_rows) > 1000,
            }
        return items

    def put_session(
        self,
        namespace: str,
        session: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
        owner: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Append a sourced exchange session with independent calendar/price coverage."""

        self._authorize(namespace, principal_id, scopes, write=True)
        if session.get("contract") != "noesis-market-trading-session-v1":
            raise MarketPriceError(
                "contract_invalid",
                "session payload must use noesis-market-trading-session-v1",
            )
        owner = self._owner_for_write(owner, principal_id, scopes)
        refs = session.get("source_refs")
        if not isinstance(refs, list) or not refs:
            raise MarketPriceError("invalid_request", "source_refs are required")
        self._check_source_refs(namespace, refs, principal_id, scopes, write=True)
        calendar_id = _text(session.get("calendar_id"), "calendar_id", limit=200)
        mic = _text(session.get("mic"), "mic", limit=4).upper()
        session_date = _date(session.get("session_date"), "session_date").isoformat()
        if session.get("session_status") == "trading":
            opened = _millis(session.get("session_open_ms"), "session_open_ms")
            closed = _millis(session.get("session_close_ms"), "session_close_ms")
            if closed <= opened:
                raise MarketPriceError(
                    "invalid_request", "session close must follow session open"
                )
        elif (
            session.get("session_open_ms") is not None
            or session.get("session_close_ms") is not None
        ):
            raise MarketPriceError(
                "invalid_request", "closed sessions cannot have open/close times"
            )
        prior = self.conn.execute(
            """SELECT revision,revision_id,payload_json
               FROM market_trading_session_revisions
               WHERE namespace=? AND calendar_id=? AND mic=? AND session_date=?
               ORDER BY revision DESC LIMIT 1""",
            [namespace, calendar_id, mic, session_date],
        ).fetchone()
        current_revision = int(prior[0]) if prior else 0
        if expected_revision is not None and expected_revision != current_revision:
            raise MarketPriceError(
                "revision_conflict",
                "trading session changed since it was read",
                expected_revision=expected_revision,
                current_revision=current_revision,
            )
        revision = current_revision + 1
        recorded_at_ms = _millis(self.now(), "recorded_at_ms")
        payload = dict(session)
        payload.update(
            {
                "namespace": namespace,
                "owner": owner,
                "mic": mic,
                "session_date": session_date,
                "revision_id": f"market-session:{calendar_id}:{mic}:{session_date}@{revision}",
                "revision": revision,
                "recorded_at_ms": recorded_at_ms,
            }
        )
        payload.pop("record_hash", None)
        payload["record_hash"] = _digest(payload)
        _validate(payload, "noesis-market-trading-session-v1.json")
        if prior and self._semantic_session(
            json.loads(prior[2])
        ) == self._semantic_session(payload):
            return json.loads(prior[2])
        encoded = _canonical(payload)
        self.conn.execute(
            """INSERT INTO market_trading_session_revisions
               (namespace,calendar_id,mic,session_date,revision,revision_id,owner,
                public_at_ms,retrieved_at_ms,payload_json,record_hash,recorded_at_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                namespace,
                calendar_id,
                mic,
                session_date,
                revision,
                payload["revision_id"],
                owner,
                payload["public_at_ms"],
                payload["retrieved_at_ms"],
                encoded,
                payload["record_hash"],
                recorded_at_ms,
            ],
        )
        return payload

    @staticmethod
    def _semantic_session(payload: Mapping[str, Any]) -> dict[str, Any]:
        ignored = {"revision_id", "revision", "recorded_at_ms", "record_hash"}
        return {key: value for key, value in payload.items() if key not in ignored}

    def get_sessions(
        self,
        namespace: str,
        calendar_id: str,
        mic: str,
        *,
        start_date: str,
        end_date: str,
        acquired_by_ms: int,
        publicly_available_by_ms: int | None = None,
        principal_id: str,
        scopes: set[str],
        limit: int = 5_000,
    ) -> list[dict[str, Any]]:
        self._authorize(namespace, principal_id, scopes, write=False)
        calendar_id = _text(calendar_id, "calendar_id", limit=200)
        mic = _text(mic, "mic", limit=4).upper()
        start, end = _date(start_date, "start_date"), _date(end_date, "end_date")
        if start >= end:
            raise MarketPriceError(
                "invalid_request", "start_date must precede end_date"
            )
        if (end - start).days > 3660:
            raise MarketPriceError(
                "range_too_large", "calendar range exceeds the bounded maximum"
            )
        acquired_by_ms = _millis(acquired_by_ms, "acquired_by_ms")
        if publicly_available_by_ms is not None:
            publicly_available_by_ms = _millis(
                publicly_available_by_ms, "publicly_available_by_ms"
            )
            if publicly_available_by_ms > acquired_by_ms:
                raise MarketPriceError(
                    "invalid_request",
                    "public cutoff cannot be later than acquired cutoff",
                )
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise MarketPriceError(
                "invalid_request", "calendar limit is outside the bounded maximum"
            )
        clauses = [
            "namespace=?",
            "calendar_id=?",
            "mic=?",
            "session_date>=?",
            "session_date<?",
            "recorded_at_ms<=?",
        ]
        params: list[Any] = [
            namespace,
            calendar_id,
            mic,
            start.isoformat(),
            end.isoformat(),
            acquired_by_ms,
        ]
        if publicly_available_by_ms is not None:
            clauses.append("public_at_ms IS NOT NULL AND public_at_ms<=?")
            params.append(publicly_available_by_ms)
        params.append(limit)
        rows = self.conn.execute(
            """WITH ranked AS (
                   SELECT session_date,revision,payload_json,
                          ROW_NUMBER() OVER (PARTITION BY session_date ORDER BY revision DESC) AS row_number
                   FROM market_trading_session_revisions WHERE """
            + " AND ".join(clauses)
            + """
               )
               SELECT session_date,revision,payload_json FROM ranked WHERE row_number=1
               ORDER BY session_date LIMIT ?""",
            params,
        ).fetchall()
        selected: dict[str, dict[str, Any]] = {}
        for session_date, _, encoded in rows:
            if session_date in selected:
                continue
            payload = json.loads(encoded)
            if self._record_readable(
                namespace, payload, principal_id, scopes, publicly_available_by_ms
            ):
                selected[session_date] = payload
            else:
                selected[session_date] = {}
        return [selected[key] for key in sorted(selected) if selected[key]][:limit]

    def coverage(
        self,
        namespace: str,
        listing_id: str,
        *,
        calendar_id: str,
        start_date: str,
        end_date: str,
        acquired_by_ms: int,
        publicly_available_by_ms: int | None = None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        """Join EOD bars with venue sessions and report why a date has no bar."""

        from src.domains.market.instruments import (
            MarketInstrumentError,
            MarketInstrumentStore,
        )

        self._authorize(namespace, principal_id, scopes, write=False)
        listing_id = _text(listing_id, "listing_id", limit=200)
        start, end = _date(start_date, "start_date"), _date(end_date, "end_date")
        if start >= end:
            raise MarketPriceError(
                "invalid_request", "start_date must precede end_date"
            )
        if (end - start).days > 3660:
            raise MarketPriceError(
                "range_too_large", "coverage range exceeds the bounded maximum"
            )
        try:
            listing = MarketInstrumentStore(self.conn, initialize=False).get_instrument(
                namespace,
                "listing",
                listing_id,
                acquired_by_ms=_millis(acquired_by_ms, "acquired_by_ms"),
                publicly_available_by_ms=publicly_available_by_ms,
                principal_id=principal_id,
                scopes=scopes,
            )
        except MarketInstrumentError as exc:
            raise MarketPriceError(
                "listing_unavailable", "listing identity is unavailable"
            ) from exc
        timezone_name = listing.get("timezone") or "UTC"
        try:
            venue_tz = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise MarketPriceError(
                "unknown_timezone", "listing has no usable venue timezone"
            ) from exc
        start_local = datetime.combine(start, daytime.min, tzinfo=venue_tz)
        end_local = datetime.combine(end, daytime.min, tzinfo=venue_tz)
        start_ms = int(start_local.astimezone(_UTC).timestamp() * 1000)
        end_ms = int(end_local.astimezone(_UTC).timestamp() * 1000)
        bars = self.get_bars(
            namespace,
            listing_id,
            start_ms=start_ms,
            end_ms=end_ms,
            acquired_by_ms=acquired_by_ms,
            publicly_available_by_ms=publicly_available_by_ms,
            principal_id=principal_id,
            scopes=scopes,
        )
        sessions = self.get_sessions(
            namespace,
            calendar_id,
            listing["mic"],
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            acquired_by_ms=acquired_by_ms,
            publicly_available_by_ms=publicly_available_by_ms,
            principal_id=principal_id,
            scopes=scopes,
            limit=min(10_000, max(1, (end - start).days)),
        )
        bars_by_date: dict[str, list[dict[str, Any]]] = {}
        for bar in bars:
            local_date = (
                datetime.fromtimestamp(bar["bar_start_ms"] / 1000, tz=_UTC)
                .astimezone(venue_tz)
                .date()
                .isoformat()
            )
            bars_by_date.setdefault(local_date, []).append(bar)
        by_date = {session["session_date"]: session for session in sessions}
        coverage_rows = []
        cursor = start
        while cursor < end:
            day = cursor.isoformat()
            session = by_date.get(day)
            session_bars = bars_by_date.get(day, [])
            if session is None:
                status = "unknown_session"
            elif session["session_status"] != "trading":
                status = session["session_status"]
            elif session_bars:
                status = "present"
            elif session["data_status"] == "no_trade":
                status = "no_trade"
            elif session["data_status"] in {"missing", "present"}:
                status = "missing_bar"
            elif session["data_status"] == "not_requested":
                status = "not_requested"
            else:
                status = "unknown_coverage"
            coverage_rows.append(
                {
                    "session_date": day,
                    "status": status,
                    "bar_ids": [bar["bar_id"] for bar in session_bars],
                    "calendar_revision_id": session.get("revision_id")
                    if session
                    else None,
                }
            )
            cursor += timedelta(days=1)
        counts: dict[str, int] = {}
        for item in coverage_rows:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return {
            "contract": "noesis-market-price-coverage-v1",
            "namespace": namespace,
            "listing_id": listing_id,
            "mic": listing["mic"],
            "timezone": timezone_name,
            "start_date": start.isoformat(),
            "end_date_exclusive": end.isoformat(),
            "acquired_by_ms": acquired_by_ms,
            "publicly_available_by_ms": publicly_available_by_ms,
            "bars": bars,
            "sessions": sessions,
            "coverage": coverage_rows,
            "counts": counts,
        }

    def _get_checkpoint(
        self,
        namespace: str,
        owner: str,
        provider: str,
        listing_id: str,
        interval: str,
        request_key: str,
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT request_hash,start_date,end_date,cursor,status,
                      records_processed,requests_made,last_error_code,updated_at_ms
               FROM market_price_ingest_checkpoints
               WHERE namespace=? AND owner=? AND provider=? AND listing_id=?
                 AND interval=? AND request_key=?""",
            [namespace, owner, provider, listing_id, interval, request_key],
        ).fetchone()
        if not row:
            return None
        return {
            "request_hash": row[0],
            "start_date": row[1],
            "end_date": row[2],
            "cursor": row[3],
            "status": row[4],
            "records_processed": int(row[5]),
            "requests_made": int(row[6]),
            "last_error_code": row[7],
            "updated_at_ms": int(row[8]),
        }

    def _save_checkpoint(
        self,
        namespace: str,
        owner: str,
        provider: str,
        listing_id: str,
        interval: str,
        request_key: str,
        checkpoint: Mapping[str, Any],
    ) -> None:
        self.conn.execute(
            """INSERT INTO market_price_ingest_checkpoints
               (namespace,owner,provider,listing_id,interval,request_key,
                request_hash,start_date,end_date,cursor,status,records_processed,
                requests_made,last_error_code,updated_at_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(namespace,owner,provider,listing_id,interval,request_key)
               DO UPDATE SET request_hash=excluded.request_hash,
                 start_date=excluded.start_date,end_date=excluded.end_date,
                 cursor=excluded.cursor,status=excluded.status,
                 records_processed=excluded.records_processed,
                 requests_made=excluded.requests_made,
                 last_error_code=excluded.last_error_code,
                 updated_at_ms=excluded.updated_at_ms""",
            [
                namespace,
                owner,
                provider,
                listing_id,
                interval,
                request_key,
                checkpoint["request_hash"],
                checkpoint["start_date"],
                checkpoint["end_date"],
                checkpoint.get("cursor"),
                checkpoint["status"],
                checkpoint["records_processed"],
                checkpoint["requests_made"],
                checkpoint.get("last_error_code"),
                checkpoint["updated_at_ms"],
            ],
        )


@dataclass(frozen=True)
class MarketIngestBudget:
    """Provider-specific request limits plus hard local run bounds."""

    requests_per_minute: int = 60
    max_requests_per_run: int = 50
    max_records_per_run: int = 10_000
    max_records_per_page: int = 500
    max_backfill_days: int = 3_650

    def validate(self) -> None:
        if (
            type(self.requests_per_minute) is not int
            or not 1 <= self.requests_per_minute <= MAX_REQUESTS_PER_MINUTE
        ):
            raise MarketPriceError(
                "invalid_budget", "requests_per_minute is outside the hard limit"
            )
        if (
            type(self.max_requests_per_run) is not int
            or not 1 <= self.max_requests_per_run <= MAX_REQUESTS_PER_RUN
        ):
            raise MarketPriceError(
                "invalid_budget", "max_requests_per_run is outside the hard limit"
            )
        if (
            type(self.max_records_per_run) is not int
            or not 1 <= self.max_records_per_run <= MAX_RECORDS_PER_RUN
        ):
            raise MarketPriceError(
                "invalid_budget", "max_records_per_run is outside the hard limit"
            )
        if (
            type(self.max_records_per_page) is not int
            or not 1 <= self.max_records_per_page <= MAX_RECORDS_PER_PAGE
        ):
            raise MarketPriceError(
                "invalid_budget", "max_records_per_page is outside the hard limit"
            )
        if (
            type(self.max_backfill_days) is not int
            or not 1 <= self.max_backfill_days <= MAX_BACKFILL_DAYS
        ):
            raise MarketPriceError(
                "invalid_budget", "max_backfill_days is outside the hard limit"
            )


class _RateGate:
    def __init__(self, requests_per_minute: int, clock, sleep) -> None:
        self.interval = 60.0 / requests_per_minute
        self.clock = clock
        self.sleep = sleep
        self.last_call: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self.last_call is not None:
            delay = self.interval - (now - self.last_call)
            if delay > 0:
                self.sleep(delay)
                now = self.clock()
        self.last_call = now


class MarketPriceIngestor:
    """Run a provider callback in bounded, rate-limited, resumable pages.

    ``fetch_page`` is an adapter boundary; it receives a listing, interval,
    half-open ``[start_date,end_date)`` bounds, cursor and maximum rows, then returns
    ``{bars, next_cursor, done}`` using normalized ``noesis-market-bar-v1``
    observations. Vendor-specific request shapes and terms stay outside here.
    """

    def __init__(
        self,
        store: MarketPriceStore,
        *,
        budget: MarketIngestBudget | None = None,
        monotonic=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.store = store
        self.budget = budget or MarketIngestBudget()
        self.budget.validate()
        self.monotonic = monotonic
        self.sleep = sleep

    def backfill(
        self,
        namespace: str,
        *,
        provider: str,
        listing_id: str,
        interval: str,
        start_date: str,
        end_date: str,
        request_key: str,
        fetch_page: Callable[..., Mapping[str, Any]],
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        MarketPriceStore._authorize(namespace, principal_id, scopes, write=True)
        provider = _text(provider, "provider", limit=100)
        listing_id = _text(listing_id, "listing_id", limit=200)
        interval = _text(interval, "interval", limit=20)
        request_key = _text(request_key, "request_key", limit=200)
        start, end = _date(start_date, "start_date"), _date(end_date, "end_date")
        span_days = (end - start).days
        if span_days <= 0:
            raise MarketPriceError("invalid_request", "end_date must follow start_date")
        if span_days > self.budget.max_backfill_days:
            raise MarketPriceError(
                "range_too_large", "backfill exceeds the configured date bound"
            )
        self.store._require_listing(
            namespace, listing_id, principal_id=principal_id, scopes=scopes
        )
        request = {
            "namespace": namespace,
            "provider": provider,
            "listing_id": listing_id,
            "interval": interval,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        request_hash = _digest(request)
        checkpoint = self.store._get_checkpoint(
            namespace, principal_id, provider, listing_id, interval, request_key
        )
        if checkpoint and checkpoint["request_hash"] != request_hash:
            raise MarketPriceError(
                "idempotency_conflict", "request_key identifies another backfill"
            )
        if checkpoint and checkpoint["status"] == "complete":
            return {"status": "complete", "resumed": False, **checkpoint}
        checkpoint = checkpoint or {
            "request_hash": request_hash,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "cursor": None,
            "status": "running",
            "records_processed": 0,
            "requests_made": 0,
            "last_error_code": None,
            "updated_at_ms": _millis(self.store.now(), "updated_at_ms"),
        }
        resumed = checkpoint["requests_made"] > 0
        checkpoint["status"] = "running"
        self.store._save_checkpoint(
            namespace,
            principal_id,
            provider,
            listing_id,
            interval,
            request_key,
            checkpoint,
        )
        gate = _RateGate(self.budget.requests_per_minute, self.monotonic, self.sleep)
        requests_this_run = 0
        records_this_run = 0
        status = "paused"
        while requests_this_run < self.budget.max_requests_per_run:
            if records_this_run >= self.budget.max_records_per_run:
                break
            gate.wait()
            try:
                page = fetch_page(
                    listing_id=listing_id,
                    interval=interval,
                    start_date=start.isoformat(),
                    end_date=end.isoformat(),
                    cursor=checkpoint.get("cursor"),
                    limit=min(
                        self.budget.max_records_per_page,
                        self.budget.max_records_per_run - records_this_run,
                    ),
                )
            except Exception:
                checkpoint.update(
                    {
                        "status": "failed",
                        "last_error_code": "provider_error",
                        "updated_at_ms": _millis(self.store.now(), "updated_at_ms"),
                    }
                )
                self.store._save_checkpoint(
                    namespace,
                    principal_id,
                    provider,
                    listing_id,
                    interval,
                    request_key,
                    checkpoint,
                )
                raise MarketPriceError(
                    "provider_error", "market provider page request failed"
                ) from None
            requests_this_run += 1
            checkpoint["requests_made"] += 1
            if not isinstance(page, Mapping) or not isinstance(page.get("bars"), list):
                raise MarketPriceError(
                    "provider_contract_invalid", "adapter page must contain a bars list"
                )
            bars = page["bars"]
            if len(bars) > self.budget.max_records_per_page:
                raise MarketPriceError(
                    "provider_contract_invalid",
                    "adapter exceeded the page record bound",
                )
            for observation in bars:
                if not isinstance(observation, Mapping):
                    raise MarketPriceError(
                        "provider_contract_invalid",
                        "bar page contains a non-object item",
                    )
                normalized = dict(observation)
                normalized["provider"] = provider
                normalized["listing_id"] = listing_id
                normalized["interval"] = interval
                try:
                    self.store.put_bar(
                        namespace, normalized, principal_id=principal_id, scopes=scopes
                    )
                except MarketPriceError as exc:
                    if exc.code not in {
                        "contract_invalid",
                        "invalid_request",
                        "currency_mismatch",
                    }:
                        raise
                    from src.domains.market.quality import MarketQualityStore

                    MarketQualityStore(self.store.conn)._record_invalid_candidate(
                        namespace,
                        request_key,
                        provider,
                        listing_id,
                        interval,
                        normalized,
                        exc.code,
                        principal_id=principal_id,
                        scopes=scopes,
                    )
                    continue
                checkpoint["records_processed"] += 1
                records_this_run += 1
            done = page.get("done")
            next_cursor = page.get("next_cursor")
            if type(done) is not bool:
                raise MarketPriceError(
                    "provider_contract_invalid",
                    "adapter page needs a boolean done flag",
                )
            if done:
                checkpoint["cursor"] = None
                checkpoint["status"] = "complete"
                status = "complete"
            else:
                if (
                    not isinstance(next_cursor, str)
                    or not next_cursor
                    or next_cursor == checkpoint.get("cursor")
                ):
                    raise MarketPriceError(
                        "provider_contract_invalid",
                        "unfinished page needs a new cursor",
                    )
                checkpoint["cursor"] = next_cursor
                checkpoint["status"] = "paused"
                status = "paused"
            checkpoint["updated_at_ms"] = _millis(self.store.now(), "updated_at_ms")
            self.store._save_checkpoint(
                namespace,
                principal_id,
                provider,
                listing_id,
                interval,
                request_key,
                checkpoint,
            )
            if done:
                break
            if records_this_run >= self.budget.max_records_per_run:
                break
        quarantined = self.store.conn.execute(
            "SELECT COUNT(*) FROM market_quality_candidates "
            "WHERE namespace=? AND request_key=? AND status='quarantined'",
            [namespace, request_key],
        ).fetchone()[0]
        return {
            "status": status,
            "resumed": resumed,
            "quarantined_records": int(quarantined),
            **checkpoint,
        }
