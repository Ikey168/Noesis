"""Fixture-only checks for revisioned bars, sessions and bounded backfills."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.domains.market.instruments import MarketInstrumentStore
from src.domains.market.entitlements import MarketEntitlementStore
from tests.unit.domains.market_entitlement_fixtures import register_market_entitlement
from src.domains.market.prices import (
    PRICE_READ_SCOPE,
    MarketIngestBudget,
    MarketPriceError,
    MarketPriceIngestor,
    MarketPriceStore,
)

T0 = 1_790_000_000_000
T1 = T0 + 10_000
NAMESPACE = "market:prices-test"
PRINCIPAL = "analyst:prices-test"
OPERATOR = {"operator"}


def millis(iso: str) -> int:
    return int(
        datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000
    )


def source_ref(
    name: str, retrieved_at_ms: int = T0, public_at_ms: int | None = None
) -> dict:
    import hashlib

    return {
        "source_ref_id": f"src:{name}",
        "provider": "fixture-only",
        "provider_object_id": name,
        "source_revision_id": f"fixture:{name}:1",
        "public_at_ms": retrieved_at_ms if public_at_ms is None else public_at_ms,
        "source_snapshot_id": f"snapshot:{name}",
        "source_url": None,
        "retrieved_at_ms": retrieved_at_ms,
        "content_hash": hashlib.sha256(name.encode()).hexdigest(),
        "license_id": "fixture-only",
        "entitlement_id": "entitlement:fixture",
    }


@pytest.fixture
def market():
    clock = {"now": T0}
    conn = duckdb.connect(":memory:")
    register_market_entitlement(
        conn, NAMESPACE, "entitlement:fixture", "fixture-only", "fixture-only", now_ms=T0
    )
    instruments = MarketInstrumentStore(conn, now=lambda: clock["now"])
    prices = MarketPriceStore(conn, now=lambda: clock["now"])
    issuer_ref = source_ref("issuer", T0)
    instruments.put_issuer(
        NAMESPACE,
        issuer_id="issuer:prices",
        kg_entity_id="kg:issuer:prices",
        display_name="Prices Test Inc.",
        source_refs=[issuer_ref],
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )
    security_ref = source_ref("security", T0)
    instruments.put_security(
        NAMESPACE,
        issuer_id="issuer:prices",
        security_id="security:prices",
        security_type="common_equity",
        share_class="A",
        denomination_currency="USD",
        source_refs=[security_ref],
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )
    listing_ref = source_ref("listing", T0)
    instruments.put_listing(
        NAMESPACE,
        listing_id="listing:prices",
        security_id="security:prices",
        mic="XNAS",
        currency="USD",
        timezone="America/New_York",
        valid_from_ms=T0,
        valid_to_ms=None,
        ticker_assertions=[
            {
                "value": "PRCE",
                "valid_from_ms": T0,
                "valid_to_ms": None,
                "source_ref_id": listing_ref["source_ref_id"],
            }
        ],
        source_refs=[listing_ref],
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )
    return conn, clock, instruments, prices


def bar_observation(
    start_ms: int,
    *,
    close: float = 101.0,
    retrieved_at_ms: int = T0,
    public_at_ms: int | None = None,
    provider_revision_id: str | None = "fixture:bar:1",
    ref_name: str = "bar-v1",
) -> dict:
    ref = source_ref(ref_name, retrieved_at_ms, public_at_ms)
    return {
        "contract": "noesis-market-bar-v1",
        "namespace": NAMESPACE,
        "owner": None,
        "bar_id": "ignored-provider-bar-id",
        "listing_id": "listing:prices",
        "provider": "fixture-provider",
        "interval": "1d",
        "bar_start_ms": start_ms,
        "bar_end_ms": start_ms + 6 * 60 * 60 * 1000 + 30 * 60 * 1000,
        "public_at_ms": ref["public_at_ms"],
        "retrieved_at_ms": retrieved_at_ms,
        "revision_id": "ignored-provider-revision",
        "revision": 1,
        "provider_record_id": f"fixture-record:{start_ms}",
        "provider_revision_id": provider_revision_id,
        "open": close - 1.0,
        "high": close + 1.0,
        "low": close - 2.0,
        "close": close,
        "volume": 1000,
        "trade_count": None,
        "vwap": close - 0.1,
        "currency": "USD",
        "price_basis": "unadjusted",
        "adjustment_method": None,
        "adjustment_cutoff_ms": None,
        "adjustment_action_revision_ids": [],
        "adjustment_calculation_id": None,
        "prior_revision_id": None,
        "source_refs": [ref],
        "recorded_at_ms": retrieved_at_ms,
        "record_hash": "ignored-provider-hash",
    }


def session_payload(
    session_date: str,
    *,
    session_status: str,
    data_status: str,
    retrieved_at_ms: int = T0,
    opens: bool = True,
) -> dict:
    ref = source_ref(f"calendar-{session_date}-{data_status}", retrieved_at_ms)
    opened = millis(f"{session_date}T13:30:00") if opens else None
    closed = millis(f"{session_date}T20:00:00") if opens else None
    return {
        "contract": "noesis-market-trading-session-v1",
        "namespace": NAMESPACE,
        "owner": None,
        "calendar_id": "calendar:XNAS:fixture",
        "mic": "XNAS",
        "venue_timezone": "America/New_York",
        "session_date": session_date,
        "session_status": session_status,
        "data_status": data_status,
        "session_open_ms": opened,
        "session_close_ms": closed,
        "public_at_ms": ref["public_at_ms"],
        "retrieved_at_ms": retrieved_at_ms,
        "revision_id": "ignored-session-revision",
        "revision": 1,
        "source_refs": [ref],
        "recorded_at_ms": retrieved_at_ms,
        "record_hash": "ignored-session-hash",
    }


def test_provider_correction_is_append_only_and_selected_by_bitemporal_cutoffs(market):
    conn, clock, _, prices = market
    first = prices.put_bar(
        NAMESPACE,
        bar_observation(millis("2026-09-21T13:30:00")),
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )
    duplicate = prices.put_bar(
        NAMESPACE,
        bar_observation(millis("2026-09-21T13:30:00")),
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )
    replayed_later = prices.put_bar(
        NAMESPACE,
        bar_observation(
            millis("2026-09-21T13:30:00"),
            retrieved_at_ms=T1,
            public_at_ms=T0,
        ),
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )

    clock["now"] = T1
    corrected = prices.put_bar(
        NAMESPACE,
        bar_observation(
            millis("2026-09-21T13:30:00"),
            close=103.0,
            retrieved_at_ms=T1,
            public_at_ms=T1,
            provider_revision_id="fixture:bar:2",
            ref_name="bar-v2",
        ),
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
        expected_revision=1,
    )
    before_publication = prices.get_bars(
        NAMESPACE,
        "listing:prices",
        start_ms=millis("2026-09-21T00:00:00"),
        end_ms=millis("2026-09-22T00:00:00"),
        acquired_by_ms=T1 + 1,
        publicly_available_by_ms=T0 + 1,
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )
    after_publication = prices.get_bars(
        NAMESPACE,
        "listing:prices",
        start_ms=millis("2026-09-21T00:00:00"),
        end_ms=millis("2026-09-22T00:00:00"),
        acquired_by_ms=T1 + 1,
        publicly_available_by_ms=T1 + 1,
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )

    assert first["bar_id"] == duplicate["bar_id"] == replayed_later["bar_id"] == corrected["bar_id"]
    assert first["revision_id"] == duplicate["revision_id"]
    assert first["revision_id"] == replayed_later["revision_id"]
    assert corrected["revision"] == 2
    assert before_publication[0]["close"] == 101.0
    assert after_publication[0]["close"] == 103.0
    assert after_publication[0]["prior_revision_id"] == first["revision_id"]
    assert (
        conn.execute("SELECT COUNT(*) FROM market_price_bar_revisions").fetchone()[0]
        == 2
    )


def test_calendar_coverage_distinguishes_holiday_no_trade_and_missing_bar(market):
    _, _, _, prices = market
    day1 = millis("2026-09-21T13:30:00")
    prices.put_bar(
        NAMESPACE, bar_observation(day1), principal_id=PRINCIPAL, scopes=OPERATOR
    )
    sessions = [
        session_payload("2026-09-21", session_status="trading", data_status="present"),
        session_payload(
            "2026-09-22", session_status="holiday", data_status="unknown", opens=False
        ),
        session_payload("2026-09-23", session_status="trading", data_status="no_trade"),
        session_payload("2026-09-24", session_status="trading", data_status="missing"),
    ]
    for session in sessions:
        prices.put_session(NAMESPACE, session, principal_id=PRINCIPAL, scopes=OPERATOR)

    report = prices.coverage(
        NAMESPACE,
        "listing:prices",
        calendar_id="calendar:XNAS:fixture",
        start_date="2026-09-21",
        end_date="2026-09-25",
        acquired_by_ms=T0 + 10,
        publicly_available_by_ms=T0 + 10,
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )
    statuses = {item["session_date"]: item["status"] for item in report["coverage"]}

    assert report["timezone"] == "America/New_York"
    assert statuses == {
        "2026-09-21": "present",
        "2026-09-22": "holiday",
        "2026-09-23": "no_trade",
        "2026-09-24": "missing_bar",
    }


def test_price_entitlements_gate_readback(market):
    _, _, _, prices = market
    prices.put_bar(
        NAMESPACE,
        bar_observation(millis("2026-09-21T13:30:00")),
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )
    scopes = {PRICE_READ_SCOPE, f"namespace:{NAMESPACE}:read"}
    denied = prices.get_bars(
        NAMESPACE,
        "listing:prices",
        start_ms=millis("2026-09-21T00:00:00"),
        end_ms=millis("2026-09-22T00:00:00"),
        acquired_by_ms=T0 + 1,
        publicly_available_by_ms=T0 + 1,
        principal_id="reader",
        scopes=scopes,
    )
    allowed = prices.get_bars(
        NAMESPACE,
        "listing:prices",
        start_ms=millis("2026-09-21T00:00:00"),
        end_ms=millis("2026-09-22T00:00:00"),
        acquired_by_ms=T0 + 1,
        publicly_available_by_ms=T0 + 1,
        principal_id="reader",
        scopes=scopes | {"market:entitlement:entitlement:fixture:read"},
    )

    assert denied == []
    assert len(allowed) == 1


def test_current_revocation_and_retention_policy_hide_previously_stored_bars(market):
    conn, clock, _, prices = market
    prices.put_bar(
        NAMESPACE,
        bar_observation(millis("2026-09-21T13:30:00")),
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )
    entitlements = MarketEntitlementStore(conn, now=lambda: clock["now"])
    revised = entitlements.put_entitlement(
        NAMESPACE,
        "entitlement:fixture",
        provider="fixture-only",
        license_id="fixture-only",
        capabilities=[
            "ingest",
            "read",
            "display",
            "retain",
            "derive",
            "cache",
            "evidence",
            "export",
            "redistribute",
        ],
        evidence_ref="synthetic-fixture:rights-review:v1",
        decision_ref="synthetic-fixture:retention-update:v1",
        principal_id="operator:rights-reviewer",
        scopes=OPERATOR,
        effective_at_ms=T0,
        max_retention_ms=10,
        expected_revision=1,
    )
    assert revised["revision"] == 2
    clock["now"] = T0 + 10
    assert (
        prices.get_bars(
            NAMESPACE,
            "listing:prices",
            start_ms=millis("2026-09-21T00:00:00"),
            end_ms=millis("2026-09-22T00:00:00"),
            acquired_by_ms=T0 + 10,
            publicly_available_by_ms=T0 + 10,
            principal_id=PRINCIPAL,
            scopes=OPERATOR,
        )
        == []
    )
    purge = entitlements.purge_expired_source_revisions(
        NAMESPACE,
        principal_id="operator:rights-reviewer",
        scopes=OPERATOR,
    )
    Draft7Validator(
        json.loads(
            (
                Path(__file__).resolve().parents[3]
                / "contracts/schemas/jsonschema/noesis-market-entitlement-purge-report-v1.json"
            ).read_text()
        )
    ).validate(purge)
    assert any(item["object_kind"] == "price_bar" for item in purge["purged"])
    assert conn.execute(
        "SELECT COUNT(*) FROM market_price_bar_revisions"
    ).fetchone()[0] == 0
    purge_columns = {
        row[0]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='market_entitlement_purge_events'"
        ).fetchall()
    }
    assert "payload_json" not in purge_columns


def test_revocation_blocks_previously_readable_history_for_operator(market):
    conn, clock, _, prices = market
    prices.put_bar(
        NAMESPACE,
        bar_observation(millis("2026-09-21T13:30:00")),
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )
    entitlements = MarketEntitlementStore(conn, now=lambda: clock["now"])
    entitlements.put_entitlement(
        NAMESPACE,
        "entitlement:fixture",
        provider="fixture-only",
        license_id="fixture-only",
        capabilities=[
            "ingest",
            "read",
            "display",
            "retain",
            "derive",
            "cache",
            "evidence",
            "export",
            "redistribute",
        ],
        evidence_ref="synthetic-fixture:rights-review:v1",
        decision_ref="synthetic-fixture:revocation:v1",
        principal_id="operator:rights-reviewer",
        scopes=OPERATOR,
        status="revoked",
        effective_at_ms=T0,
        expected_revision=1,
    )
    assert (
        prices.get_bars(
            NAMESPACE,
            "listing:prices",
            start_ms=millis("2026-09-21T00:00:00"),
            end_ms=millis("2026-09-22T00:00:00"),
            acquired_by_ms=T0 + 1,
            publicly_available_by_ms=T0 + 1,
            principal_id=PRINCIPAL,
            scopes=OPERATOR,
        )
        == []
    )


def test_bounded_backfill_resumes_and_idempotent_completion_skips_provider(market):
    conn, _, _, prices = market
    ingestor = MarketPriceIngestor(
        prices,
        budget=MarketIngestBudget(
            requests_per_minute=60_000,
            max_requests_per_run=1,
            max_records_per_run=10,
            max_records_per_page=2,
            max_backfill_days=30,
        ),
    )
    pages = {
        None: {
            "bars": [bar_observation(millis("2026-09-21T13:30:00"))],
            "next_cursor": "page:2",
            "done": False,
        },
        "page:2": {
            "bars": [bar_observation(millis("2026-09-22T13:30:00"))],
            "next_cursor": None,
            "done": True,
        },
    }
    calls = []

    def fetch_page(**kwargs):
        calls.append(kwargs["cursor"])
        return pages[kwargs["cursor"]]

    first = ingestor.backfill(
        NAMESPACE,
        provider="fixture-provider",
        listing_id="listing:prices",
        interval="1d",
        start_date="2026-09-21",
        end_date="2026-09-23",
        request_key="fixture-run-1",
        fetch_page=fetch_page,
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )
    second = ingestor.backfill(
        NAMESPACE,
        provider="fixture-provider",
        listing_id="listing:prices",
        interval="1d",
        start_date="2026-09-21",
        end_date="2026-09-23",
        request_key="fixture-run-1",
        fetch_page=fetch_page,
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )
    complete_again = ingestor.backfill(
        NAMESPACE,
        provider="fixture-provider",
        listing_id="listing:prices",
        interval="1d",
        start_date="2026-09-21",
        end_date="2026-09-23",
        request_key="fixture-run-1",
        fetch_page=fetch_page,
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )

    assert first["status"] == "paused"
    assert second["status"] == "complete" and second["resumed"] is True
    assert second["records_processed"] == 2
    assert calls == [None, "page:2"]
    assert complete_again["status"] == "complete"
    assert calls == [None, "page:2"]
    assert (
        conn.execute("SELECT COUNT(*) FROM market_price_bar_revisions").fetchone()[0]
        == 2
    )


def test_backfill_obeys_configured_provider_rate_limit(market):
    _, _, _, prices = market
    simulated = {"time": 0.0}
    sleeps = []

    def monotonic():
        return simulated["time"]

    def sleep(seconds):
        sleeps.append(seconds)
        simulated["time"] += seconds

    ingestor = MarketPriceIngestor(
        prices,
        budget=MarketIngestBudget(
            requests_per_minute=60,
            max_requests_per_run=2,
            max_records_per_run=5,
            max_records_per_page=2,
            max_backfill_days=30,
        ),
        monotonic=monotonic,
        sleep=sleep,
    )
    pages = {
        None: {"bars": [], "next_cursor": "page:2", "done": False},
        "page:2": {"bars": [], "next_cursor": None, "done": True},
    }
    ingestor.backfill(
        NAMESPACE,
        provider="fixture-provider",
        listing_id="listing:prices",
        interval="1d",
        start_date="2026-09-21",
        end_date="2026-09-22",
        request_key="rate-limited-run",
        fetch_page=lambda **kw: pages[kw["cursor"]],
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )

    assert sleeps == [1.0]


def test_backfill_rejects_unbounded_date_windows(market):
    _, _, _, prices = market
    ingestor = MarketPriceIngestor(
        prices, budget=MarketIngestBudget(max_backfill_days=30)
    )
    with pytest.raises(MarketPriceError, match="configured date bound"):
        ingestor.backfill(
            NAMESPACE,
            provider="fixture-provider",
            listing_id="listing:prices",
            interval="1d",
            start_date="2000-01-01",
            end_date="2026-01-01",
            request_key="too-long",
            fetch_page=lambda **_: {},
            principal_id=PRINCIPAL,
            scopes=OPERATOR,
        )
