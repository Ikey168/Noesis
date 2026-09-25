"""Deterministic checks for the credential-safe FMP EOD adapter."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.error import HTTPError

import pytest
from jsonschema import Draft7Validator

from src.ingestion.connectors.market.fmp import (
    FmpEodAdapter,
    FmpListing,
    FmpMarketDataError,
)

NAMESPACE = "market:fmp-test"
LISTING = FmpListing(
    listing_id="listing:msft:xnas",
    symbol="MSFT",
    mic="XNAS",
    currency="USD",
    timezone="America/New_York",
)
NOW = 1_800_000_000_000
SECRET = "test-secret-never-log"


def millis(value: str) -> int:
    return int(
        datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000
    )


def adapter(http_get, **kwargs) -> FmpEodAdapter:
    return FmpEodAdapter(
        namespace=NAMESPACE,
        listings={LISTING.listing_id: LISTING},
        entitlement_id="entitlement:fmp-fixture",
        license_id="fmp-fixture-only",
        api_key=SECRET,
        http_get=http_get,
        now=lambda: NOW,
        sleep=lambda _: None,
        **kwargs,
    )


def eod_row(day: str, close: float = 401.0) -> dict:
    return {
        "symbol": "MSFT",
        "date": day,
        "adjOpen": close - 2,
        "adjHigh": close + 1,
        "adjLow": close - 3,
        "adjClose": close,
        "volume": 20_000_000,
    }


def test_eod_page_is_bounded_sorted_dst_aware_and_credential_safe():
    calls = []

    def get(url, timeout):
        calls.append((url, timeout))
        return [eod_row("2026-03-07", 402), eod_row("2026-03-06", 401)]

    page = adapter(get).fetch_page(
        listing_id=LISTING.listing_id,
        interval="1d",
        start_date="2026-03-06",
        end_date="2026-03-12",
        cursor=None,
        limit=2,
    )

    assert page["done"] is False
    assert page["next_cursor"] == "2026-03-08"
    assert [row["close"] for row in page["bars"]] == [401.0, 402.0]
    assert page["bars"][0]["bar_start_ms"] == millis("2026-03-06T14:30:00")
    assert page["bars"][0]["bar_end_ms"] == millis("2026-03-06T21:00:00")
    assert page["bars"][0]["price_basis"] == "unadjusted"
    assert page["bars"][0]["public_at_ms"] is None
    assert SECRET in calls[0][0]
    assert SECRET not in page["bars"][0]["source_refs"][0]["source_url"]
    assert "2026-03-06" in calls[0][0] and "2026-03-07" in calls[0][0]

    schema = json.loads(
        open(
            "contracts/schemas/jsonschema/noesis-market-bar-v1.json",
            encoding="utf-8",
        ).read()
    )
    Draft7Validator(schema).validate(page["bars"][0])


def test_eod_rejects_duplicate_dates_and_rows_outside_requested_window():
    duplicate = adapter(lambda *_: [eod_row("2026-01-02"), eod_row("2026-01-02")])
    with pytest.raises(FmpMarketDataError, match="duplicate daily bars"):
        duplicate.fetch_page(
            listing_id=LISTING.listing_id,
            interval="1d",
            start_date="2026-01-01",
            end_date="2026-01-04",
            cursor=None,
            limit=3,
        )

    outside = adapter(lambda *_: [eod_row("2025-12-31")])
    with pytest.raises(FmpMarketDataError, match="outside the requested window"):
        outside.fetch_page(
            listing_id=LISTING.listing_id,
            interval="1d",
            start_date="2026-01-01",
            end_date="2026-01-04",
            cursor=None,
            limit=3,
        )


def test_http_retry_is_bounded_and_errors_never_disclose_key():
    attempts = []

    def unavailable(url, _timeout):
        attempts.append(url)
        raise HTTPError(url, 429, "limited", {}, None)

    with pytest.raises(FmpMarketDataError) as caught:
        adapter(unavailable, max_attempts=2).fetch_page(
            listing_id=LISTING.listing_id,
            interval="1d",
            start_date="2026-01-01",
            end_date="2026-01-02",
            cursor=None,
            limit=1,
        )

    assert len(attempts) == 2
    assert caught.value.http_status == 429
    assert SECRET not in str(caught.value)


def test_calendar_distinguishes_trading_holiday_weekend_and_early_close():
    payload = [
        {
            "date": "2026-01-01",
            "exchange": "NASDAQ",
            "name": "New Year",
            "isClosed": True,
            "adjOpen": None,
            "adjClose": None,
        },
        {
            "date": "2026-07-03",
            "exchange": "NASDAQ",
            "name": "Independence Day observed",
            "isClosed": True,
            "adjOpen": None,
            "adjClose": None,
        },
        {
            "date": "2026-11-27",
            "exchange": "NASDAQ",
            "name": "Early close",
            "isClosed": False,
            "adjOpen": "09:30",
            "adjClose": "13:00",
        },
    ]
    subject = adapter(lambda *_: payload)
    sessions = subject.fetch_sessions(
        listing_id=LISTING.listing_id,
        exchange="NASDAQ",
        start_date="2026-07-02",
        end_date="2026-07-06",
        observed_price_dates={"2026-07-02"},
    )
    by_date = {row["session_date"]: row for row in sessions}

    assert by_date["2026-07-02"]["session_status"] == "trading"
    assert by_date["2026-07-02"]["data_status"] == "present"
    assert by_date["2026-07-03"]["session_status"] == "holiday"
    assert by_date["2026-07-04"]["session_status"] == "weekend"
    assert by_date["2026-07-05"]["session_status"] == "weekend"

    early = subject.fetch_sessions(
        listing_id=LISTING.listing_id,
        exchange="NASDAQ",
        start_date="2026-11-27",
        end_date="2026-11-28",
    )[0]
    assert early["session_status"] == "trading"
    assert early["session_close_ms"] == millis("2026-11-27T18:00:00")


def test_calendar_fails_closed_when_provider_does_not_cover_requested_year():
    subject = adapter(
        lambda *_: [
            {
                "date": "2026-01-01",
                "exchange": "NASDAQ",
                "name": "New Year",
                "isClosed": True,
            }
        ]
    )
    with pytest.raises(FmpMarketDataError) as caught:
        subject.fetch_sessions(
            listing_id=LISTING.listing_id,
            exchange="NASDAQ",
            start_date="2025-12-31",
            end_date="2026-01-02",
        )
    assert caught.value.code == "calendar_coverage_unknown"
