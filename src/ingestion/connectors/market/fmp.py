"""Financial Modeling Prep adapter for unadjusted end-of-day equity bars.

The adapter deliberately exposes the callback shape consumed by
``MarketPriceIngestor``.  It does not persist raw responses, infer contractual
rights from a working API key, or include the key in source locators/errors.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time as daytime, timedelta, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_API_BASE = "https://financialmodelingprep.com/stable"
_UTC = timezone.utc
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class FmpMarketDataError(ValueError):
    """Credential-safe FMP adapter failure."""

    def __init__(self, code: str, message: str, *, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True)
class FmpListing:
    """Provider symbol plus the venue facts needed for daily-bar timestamps."""

    listing_id: str
    symbol: str
    mic: str
    currency: str
    timezone: str
    session_open: str = "09:30"
    session_close: str = "16:00"

    def validate(self) -> None:
        for name in ("listing_id", "symbol", "mic", "currency", "timezone"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise FmpMarketDataError("invalid_configuration", f"{name} is required")
        if len(self.mic) != 4 or len(self.currency) != 3:
            raise FmpMarketDataError(
                "invalid_configuration", "MIC and currency must use canonical codes"
            )
        try:
            ZoneInfo(self.timezone)
            _clock_time(self.session_open)
            _clock_time(self.session_close)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise FmpMarketDataError(
                "invalid_configuration", "listing timezone/session times are invalid"
            ) from exc


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _clock_time(value: str) -> daytime:
    parsed = daytime.fromisoformat(value)
    if parsed.second or parsed.microsecond or parsed.tzinfo is not None:
        raise ValueError("session time must be HH:MM")
    return parsed


def _iso_date(value: str, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise FmpMarketDataError(
            "invalid_request", f"{field} must be YYYY-MM-DD"
        ) from exc
    if parsed.isoformat() != value:
        raise FmpMarketDataError("invalid_request", f"{field} must be YYYY-MM-DD")
    return parsed


def _epoch_ms(value: datetime) -> int:
    return int(value.astimezone(_UTC).timestamp() * 1000)


def _number(
    row: Mapping[str, Any], *names: str, nullable: bool = False
) -> float | None:
    for name in names:
        value = row.get(name)
        if value is not None and type(value) in {int, float}:
            number = float(value)
            if number >= 0:
                return number
            break
    if nullable:
        return None
    raise FmpMarketDataError(
        "provider_contract_invalid", f"missing nonnegative {names[0]}"
    )


def _default_http_get(url: str, timeout: float) -> Any:
    request = Request(
        url, headers={"Accept": "application/json", "User-Agent": "Noesis/market"}
    )
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed HTTPS host
        return json.loads(response.read().decode("utf-8"))


class FmpEodAdapter:
    """Normalize FMP non-split-adjusted EOD rows into Noesis market bars."""

    provider = "fmp"

    def __init__(
        self,
        *,
        namespace: str,
        listings: Mapping[str, FmpListing],
        entitlement_id: str,
        license_id: str,
        api_key: str | None = None,
        http_get: Callable[[str, float], Any] | None = None,
        now: Callable[[], int] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 30.0,
        max_attempts: int = 3,
        session_close_overrides: Mapping[tuple[str, str], str] | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.getenv("FMP_API_KEY")
        if not self._api_key:
            raise FmpMarketDataError("not_configured", "FMP_API_KEY is required")
        if not namespace or not entitlement_id or not license_id:
            raise FmpMarketDataError(
                "invalid_configuration",
                "namespace, entitlement_id and license_id are required",
            )
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
            raise FmpMarketDataError(
                "invalid_configuration", "max_attempts must be 1..5"
            )
        if not 0 < timeout <= 120:
            raise FmpMarketDataError("invalid_configuration", "timeout must be 0..120")
        self.namespace = namespace
        self.entitlement_id = entitlement_id
        self.license_id = license_id
        self.listings = dict(listings)
        if not self.listings:
            raise FmpMarketDataError(
                "invalid_configuration", "at least one listing is required"
            )
        for listing_id, listing in self.listings.items():
            listing.validate()
            if listing_id != listing.listing_id:
                raise FmpMarketDataError(
                    "invalid_configuration", "listing map keys must match listing_id"
                )
        self._http_get = http_get or _default_http_get
        self._now = now or (lambda: int(time.time() * 1000))
        self._sleep = sleep
        self._timeout = float(timeout)
        self._max_attempts = max_attempts
        self._session_close_overrides = dict(session_close_overrides or {})

    def _request(self, endpoint: str, params: Mapping[str, str]) -> tuple[Any, str]:
        public_query = urlencode(params)
        source_url = f"{_API_BASE}/{endpoint}?{public_query}"
        request_url = f"{source_url}&{urlencode({'apikey': self._api_key})}"
        for attempt in range(1, self._max_attempts + 1):
            try:
                payload = self._http_get(request_url, self._timeout)
                return payload, source_url
            except HTTPError as exc:
                status = int(exc.code)
                if status not in _RETRYABLE_STATUS or attempt == self._max_attempts:
                    raise FmpMarketDataError(
                        "provider_http_error",
                        "FMP request failed",
                        http_status=status,
                    ) from None
            except FmpMarketDataError:
                raise
            except Exception:
                if attempt == self._max_attempts:
                    raise FmpMarketDataError(
                        "provider_unavailable", "FMP request failed"
                    ) from None
            self._sleep(min(2 ** (attempt - 1), 8))
        raise AssertionError("bounded retry loop exhausted")

    def fetch_page(
        self,
        *,
        listing_id: str,
        interval: str,
        start_date: str,
        end_date: str,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        """Fetch one bounded calendar-day window for ``MarketPriceIngestor``."""

        if interval != "1d":
            raise FmpMarketDataError(
                "unsupported_interval", "FMP adapter supports only 1d"
            )
        listing = self.listings.get(listing_id)
        if listing is None:
            raise FmpMarketDataError("unknown_listing", "listing is not configured")
        start, end = (
            _iso_date(start_date, "start_date"),
            _iso_date(end_date, "end_date"),
        )
        window_start = _iso_date(cursor, "cursor") if cursor is not None else start
        if start >= end or not start <= window_start < end:
            raise FmpMarketDataError(
                "invalid_request", "date range or cursor is invalid"
            )
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise FmpMarketDataError("invalid_request", "limit is outside 1..10000")
        window_end = min(end, window_start + timedelta(days=limit))
        params = {
            "symbol": listing.symbol,
            "from": window_start.isoformat(),
            "to": (window_end - timedelta(days=1)).isoformat(),
        }
        payload, source_url = self._request(
            "historical-price-eod/non-split-adjusted", params
        )
        if not isinstance(payload, list):
            raise FmpMarketDataError(
                "provider_contract_invalid", "FMP EOD response must be an array"
            )
        if any(not isinstance(row, Mapping) for row in payload):
            raise FmpMarketDataError(
                "provider_contract_invalid", "FMP EOD rows must be objects"
            )
        if len(payload) > limit:
            raise FmpMarketDataError(
                "provider_contract_invalid", "FMP returned more rows than requested"
            )
        retrieved_at_ms = self._now()
        snapshot_hash = _digest(payload)
        rows = [
            self._normalize_bar(
                listing,
                row,
                window_start=window_start,
                window_end=window_end,
                retrieved_at_ms=retrieved_at_ms,
                snapshot_hash=snapshot_hash,
                source_url=source_url,
            )
            for row in payload
        ]
        rows.sort(key=lambda row: row["bar_start_ms"])
        identities = [row["provider_record_id"] for row in rows]
        if len(identities) != len(set(identities)):
            raise FmpMarketDataError(
                "provider_contract_invalid", "FMP returned duplicate daily bars"
            )
        done = window_end >= end
        return {
            "bars": rows,
            "next_cursor": None if done else window_end.isoformat(),
            "done": done,
        }

    def _normalize_bar(
        self,
        listing: FmpListing,
        row: Mapping[str, Any],
        *,
        window_start: date,
        window_end: date,
        retrieved_at_ms: int,
        snapshot_hash: str,
        source_url: str,
    ) -> dict[str, Any]:
        session_date = _iso_date(str(row.get("date") or ""), "provider date")
        if not window_start <= session_date < window_end:
            raise FmpMarketDataError(
                "provider_contract_invalid", "FMP row is outside the requested window"
            )
        venue_tz = ZoneInfo(listing.timezone)
        opened = datetime.combine(
            session_date, _clock_time(listing.session_open), venue_tz
        )
        close_text = self._session_close_overrides.get(
            (listing.mic, session_date.isoformat()), listing.session_close
        )
        closed = datetime.combine(session_date, _clock_time(close_text), venue_tz)
        if closed <= opened:
            raise FmpMarketDataError(
                "invalid_configuration", "session close must follow session open"
            )
        row_hash = _digest(row)
        provider_record_id = f"fmp:{listing.symbol}:1d:{session_date.isoformat()}"
        source_ref = {
            "source_ref_id": f"source:fmp:{listing.symbol}:{session_date.isoformat()}:{row_hash[:16]}",
            "provider": self.provider,
            "provider_object_id": provider_record_id,
            "source_revision_id": f"fmp-row:{row_hash}",
            "public_at_ms": None,
            "source_snapshot_id": f"fmp-response:{snapshot_hash}",
            "source_url": source_url,
            "retrieved_at_ms": retrieved_at_ms,
            "content_hash": row_hash,
            "license_id": self.license_id,
            "entitlement_id": self.entitlement_id,
        }
        return {
            "contract": "noesis-market-bar-v1",
            "namespace": self.namespace,
            "owner": None,
            "bar_id": "adapter-assigned-by-store",
            "listing_id": listing.listing_id,
            "provider": self.provider,
            "interval": "1d",
            "bar_start_ms": _epoch_ms(opened),
            "bar_end_ms": _epoch_ms(closed),
            "public_at_ms": None,
            "retrieved_at_ms": retrieved_at_ms,
            "revision_id": "adapter-assigned-by-store",
            "revision": 1,
            "provider_record_id": provider_record_id,
            "provider_revision_id": f"fmp-row:{row_hash}",
            "open": _number(row, "adjOpen", "open"),
            "high": _number(row, "adjHigh", "high"),
            "low": _number(row, "adjLow", "low"),
            "close": _number(row, "adjClose", "close"),
            "volume": _number(row, "volume", nullable=True),
            "trade_count": None,
            "vwap": None,
            "currency": listing.currency,
            "price_basis": "unadjusted",
            "adjustment_method": None,
            "adjustment_cutoff_ms": None,
            "adjustment_action_revision_ids": [],
            "adjustment_calculation_id": None,
            "prior_revision_id": None,
            "source_refs": [source_ref],
            "recorded_at_ms": retrieved_at_ms,
            "record_hash": "0" * 64,
        }

    def _holiday_response(self, exchange: str) -> tuple[list[Any], str, str, int]:
        if not exchange or len(exchange) > 30:
            raise FmpMarketDataError("invalid_request", "exchange is required")
        payload, source_url = self._request(
            "holidays-by-exchange", {"exchange": exchange}
        )
        if not isinstance(payload, list):
            raise FmpMarketDataError(
                "provider_contract_invalid", "FMP holiday response must be an array"
            )
        return payload, source_url, _digest(payload), self._now()

    def fetch_holidays(self, exchange: str) -> list[dict[str, Any]]:
        """Return FMP holiday/early-close declarations without inferring coverage."""

        payload, _, _, _ = self._holiday_response(exchange)
        holidays = []
        for row in payload:
            if not isinstance(row, Mapping):
                continue
            holiday_date = _iso_date(str(row.get("date") or ""), "holiday date")
            holidays.append(
                {
                    "date": holiday_date.isoformat(),
                    "exchange": str(row.get("exchange") or exchange),
                    "name": str(row.get("name") or ""),
                    "is_closed": bool(row.get("isClosed")),
                    "adjusted_open": row.get("adjOpen"),
                    "adjusted_close": row.get("adjClose"),
                    "content_hash": _digest(row),
                }
            )
        return sorted(holidays, key=lambda row: row["date"])

    def fetch_sessions(
        self,
        *,
        listing_id: str,
        exchange: str,
        start_date: str,
        end_date: str,
        observed_price_dates: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Build a bounded venue calendar from FMP's declared holiday years.

        FMP's holiday response has no explicit coverage envelope.  To avoid
        treating absence as proof outside the response, every requested year
        must have at least one returned holiday declaration.
        """

        listing = self.listings.get(listing_id)
        if listing is None:
            raise FmpMarketDataError("unknown_listing", "listing is not configured")
        start, end = (
            _iso_date(start_date, "start_date"),
            _iso_date(end_date, "end_date"),
        )
        if start >= end or (end - start).days > 3660:
            raise FmpMarketDataError("invalid_request", "calendar range is invalid")
        payload, source_url, snapshot_hash, retrieved_at_ms = self._holiday_response(
            exchange
        )
        by_date: dict[str, Mapping[str, Any]] = {}
        covered_years: set[int] = set()
        for row in payload:
            if not isinstance(row, Mapping):
                continue
            holiday_date = _iso_date(str(row.get("date") or ""), "holiday date")
            covered_years.add(holiday_date.year)
            by_date[holiday_date.isoformat()] = row
        requested_years = set(range(start.year, (end - timedelta(days=1)).year + 1))
        if not requested_years.issubset(covered_years):
            raise FmpMarketDataError(
                "calendar_coverage_unknown",
                "FMP holiday response does not establish every requested year",
            )
        observed = observed_price_dates
        sessions = []
        cursor = start
        while cursor < end:
            date_text = cursor.isoformat()
            holiday = by_date.get(date_text)
            weekend = cursor.weekday() >= 5
            is_closed = weekend or bool(holiday and holiday.get("isClosed"))
            session_status = (
                "weekend" if weekend else ("holiday" if is_closed else "trading")
            )
            opened: int | None = None
            closed: int | None = None
            if session_status == "trading":
                open_text = str((holiday or {}).get("adjOpen") or listing.session_open)
                close_text = str(
                    (holiday or {}).get("adjClose") or listing.session_close
                )
                venue_tz = ZoneInfo(listing.timezone)
                opened = _epoch_ms(
                    datetime.combine(cursor, _clock_time(open_text), venue_tz)
                )
                closed = _epoch_ms(
                    datetime.combine(cursor, _clock_time(close_text), venue_tz)
                )
                if closed <= opened:
                    raise FmpMarketDataError(
                        "provider_contract_invalid", "calendar session close is invalid"
                    )
                if holiday and close_text != listing.session_close:
                    self._session_close_overrides[(listing.mic, date_text)] = close_text
            if session_status != "trading":
                data_status = "unknown"
            elif observed is None:
                data_status = "not_requested"
            elif date_text in observed:
                data_status = "present"
            else:
                data_status = "missing"
            row_hash = _digest(holiday) if holiday else snapshot_hash
            source_ref = {
                "source_ref_id": f"source:fmp:{exchange}:calendar:{date_text}:{row_hash[:16]}",
                "provider": self.provider,
                "provider_object_id": f"fmp:{exchange}:calendar:{date_text}",
                "source_revision_id": f"fmp-calendar:{row_hash}",
                "public_at_ms": None,
                "source_snapshot_id": f"fmp-response:{snapshot_hash}",
                "source_url": source_url,
                "retrieved_at_ms": retrieved_at_ms,
                "content_hash": row_hash,
                "license_id": self.license_id,
                "entitlement_id": self.entitlement_id,
            }
            sessions.append(
                {
                    "contract": "noesis-market-trading-session-v1",
                    "namespace": self.namespace,
                    "owner": None,
                    "calendar_id": f"calendar:{listing.mic}:fmp",
                    "mic": listing.mic,
                    "venue_timezone": listing.timezone,
                    "session_date": date_text,
                    "session_status": session_status,
                    "data_status": data_status,
                    "session_open_ms": opened,
                    "session_close_ms": closed,
                    "public_at_ms": None,
                    "retrieved_at_ms": retrieved_at_ms,
                    "revision_id": "adapter-assigned-by-store",
                    "revision": 1,
                    "source_refs": [source_ref],
                    "recorded_at_ms": retrieved_at_ms,
                    "record_hash": "0" * 64,
                }
            )
            cursor += timedelta(days=1)
        return sessions
