"""Fixture-only checks for point-in-time actions and adjustment calculations."""

from __future__ import annotations

from datetime import datetime
import hashlib

import duckdb
import pytest

from src.domains.market.actions import (
    ACTION_WRITE_SCOPE,
    MarketActionError,
    MarketCorporateActionStore,
)
from src.domains.market.instruments import MarketInstrumentStore
from src.domains.market.prices import MarketPriceStore
from src.kb.quantitative import QuantitativeStore
from tests.unit.domains.market_entitlement_fixtures import register_market_entitlement

NAMESPACE = "market:actions-test"
PRINCIPAL = "analyst:actions-test"
OPERATOR = {"operator"}
T0 = 1_788_273_000_000
T1 = 1_788_500_000_000
T_CUTOFF = 1_790_208_000_000


def millis(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def source_ref(name: str, at_ms: int) -> dict:
    return {
        "source_ref_id": f"src:{name}",
        "provider": "fixture-only",
        "provider_object_id": name,
        "source_revision_id": f"fixture:{name}:1",
        "public_at_ms": at_ms,
        "source_snapshot_id": f"snapshot:{name}",
        "source_url": None,
        "retrieved_at_ms": at_ms,
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
    actions = MarketCorporateActionStore(conn, now=lambda: clock["now"])
    issuer_ref = source_ref("issuer", T0)
    instruments.put_issuer(
        NAMESPACE,
        issuer_id="issuer:actions",
        kg_entity_id="kg:issuer:actions",
        display_name="Actions Test Inc.",
        source_refs=[issuer_ref],
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )
    security_ref = source_ref("security", T0)
    instruments.put_security(
        NAMESPACE,
        issuer_id="issuer:actions",
        security_id="security:actions",
        security_type="common_equity",
        share_class="Common",
        denomination_currency="USD",
        source_refs=[security_ref],
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )
    listing_ref = source_ref("listing", T0)
    instruments.put_listing(
        NAMESPACE,
        listing_id="listing:actions",
        security_id="security:actions",
        mic="XNAS",
        currency="USD",
        timezone="America/New_York",
        valid_from_ms=T0,
        valid_to_ms=None,
        ticker_assertions=[
            {
                "value": "ACTN",
                "valid_from_ms": T0,
                "valid_to_ms": None,
                "source_ref_id": listing_ref["source_ref_id"],
            }
        ],
        source_refs=[listing_ref],
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )
    return conn, clock, prices, actions


def bar_payload(start_ms: int, close: float, *, price_basis="unadjusted") -> dict:
    retrieved = start_ms + 7 * 60 * 60 * 1000
    ref = source_ref(f"bar:{start_ms}", retrieved)
    return {
        "contract": "noesis-market-bar-v1",
        "namespace": NAMESPACE,
        "owner": None,
        "bar_id": "provider supplied id is ignored",
        "listing_id": "listing:actions",
        "provider": "fixture-provider",
        "interval": "1d",
        "bar_start_ms": start_ms,
        "bar_end_ms": retrieved,
        "public_at_ms": retrieved,
        "retrieved_at_ms": retrieved,
        "revision_id": "provider supplied revision is ignored",
        "revision": 1,
        "provider_record_id": f"bar:{start_ms}",
        "provider_revision_id": "fixture:bar:1",
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1000,
        "trade_count": None,
        "vwap": close,
        "currency": "USD",
        "price_basis": price_basis,
        "adjustment_method": None,
        "adjustment_cutoff_ms": None,
        "adjustment_action_revision_ids": [],
        "adjustment_calculation_id": None,
        "prior_revision_id": None,
        "source_refs": [ref],
        "recorded_at_ms": retrieved,
        "record_hash": "provider supplied hash is ignored",
    }


def put_bars(
    market,
    closes: list[float],
    dates: list[str] | None = None,
):
    _, clock, prices, _ = market
    dates = dates or ["2026-09-01", "2026-09-02", "2026-09-03"]
    result = []
    for session, close in zip(dates, closes):
        start = millis(f"{session}T13:30:00Z")
        clock["now"] = start + 7 * 60 * 60 * 1000
        result.append(
            prices.put_bar(
                NAMESPACE,
                bar_payload(start, close),
                principal_id=PRINCIPAL,
                scopes=OPERATOR,
            )
        )
    return result


def action_payload(
    action_id: str,
    action_type: str,
    ex_date: str,
    public_at_ms: int,
    *,
    ratio_numerator=None,
    ratio_denominator=None,
    cash_amount=None,
    cash_amount_basis="not_applicable",
    currency=None,
    distribution_type=None,
    listing_id="listing:actions",
    related_security_id=None,
    status="confirmed",
    provider="fixture-provider",
) -> dict:
    ref = source_ref(f"action:{action_id}:{public_at_ms}", public_at_ms)
    return {
        "contract": "noesis-market-corporate-action-v1",
        "namespace": NAMESPACE,
        "owner": None,
        "action_id": action_id,
        "issuer_id": "issuer:actions",
        "security_id": "security:actions",
        "listing_id": listing_id,
        "related_security_id": related_security_id,
        "related_listing_id": None,
        "revision_id": "provider supplied revision is ignored",
        "revision": 1,
        "provider": provider,
        "provider_record_id": f"record:{action_id}",
        "action_type": action_type,
        "status": status,
        "announced_at_ms": public_at_ms,
        "public_at_ms": public_at_ms,
        "effective_date": ex_date,
        "ex_date": ex_date,
        "record_date": None,
        "payable_date": None,
        "ratio_numerator": ratio_numerator,
        "ratio_denominator": ratio_denominator,
        "cash_amount": cash_amount,
        "cash_amount_basis": cash_amount_basis,
        "distribution_type": distribution_type
        or ("regular" if action_type == "cash_dividend" else "not_applicable"),
        "currency": currency,
        "prior_revision_id": None,
        "source_refs": [ref],
        "recorded_at_ms": public_at_ms,
        "record_hash": "provider supplied hash is ignored",
    }


def add_action(market, payload):
    _, clock, _, actions = market
    clock["now"] = max(clock["now"], payload["public_at_ms"])
    return actions.put_action(
        NAMESPACE, payload, principal_id=PRINCIPAL, scopes=OPERATOR
    )


def calculate(market, bars, *, public_cutoff=T_CUTOFF, acquired_cutoff=T_CUTOFF):
    _, _, _, actions = market
    return actions.calculate_adjusted_series(
        NAMESPACE,
        "security:actions",
        "listing:actions",
        bars,
        listing_timezone="America/New_York",
        acquired_by_ms=acquired_cutoff,
        publicly_available_by_ms=public_cutoff,
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
    )


def test_two_for_one_split_preserves_price_return_and_is_not_double_adjusted(market):
    bars = put_bars(market, [100.0, 50.0])
    action = add_action(
        market,
        action_payload(
            "split-2-for-1",
            "split",
            "2026-09-02",
            T0,
            ratio_numerator=2,
            ratio_denominator=1,
        ),
    )

    result = calculate(market, bars)
    replay = calculate(market, bars)

    assert result["calculation_id"] == replay["calculation_id"]
    assert result["series"][0]["split_factor"] == "0.5"
    assert result["series"][0]["split_adjusted_close"] == "50"
    assert result["series"][0]["price_return_index"] == "100"
    assert result["series"][1]["split_adjusted_close"] == "50"
    assert result["action_revision_ids"] == [action["revision_id"]]
    assert result["quantitative_calculation_id"]
    assert (
        market[3].get_calculation(
            NAMESPACE,
            result["calculation_id"],
            principal_id=PRINCIPAL,
            scopes=OPERATOR,
        )
        == result
    )
    assert (
        market[0]
        .execute("SELECT COUNT(*) FROM market_price_adjustment_calculations")
        .fetchone()[0]
        == 1
    )
    quantitative_replay = QuantitativeStore(
        market[0], initialize=False
    ).replay_calculation(
        NAMESPACE,
        result["quantitative_calculation_id"],
        scopes=OPERATOR,
    )
    assert quantitative_replay["deterministic"] is True
    assert (
        market[0]
        .execute(
            "SELECT COUNT(*) FROM quantitative_calculations WHERE operation='market-adjustment'"
        )
        .fetchone()[0]
        == 1
    )
    with pytest.raises(MarketActionError, match="cannot be adjusted again"):
        calculate(
            market,
            [
                bar_payload(
                    millis("2026-09-01T13:30:00Z"), 100, price_basis="split_adjusted"
                )
            ],
        )


def test_reverse_split_uses_ratio_below_one_and_keeps_return_continuous(market):
    bars = put_bars(market, [1.0, 10.0])
    add_action(
        market,
        action_payload(
            "reverse-1-for-10",
            "reverse_split",
            "2026-09-02",
            T0,
            ratio_numerator=1,
            ratio_denominator=10,
        ),
    )

    series = calculate(market, bars)["series"]

    assert series[0]["split_factor"] == "10"
    assert series[0]["split_adjusted_close"] == "10"
    assert series[1]["price_return_index"] == "100"


def test_missing_action_session_fails_instead_of_reinvesting_at_later_close(market):
    bars = put_bars(
        market,
        [100.0, 50.0],
        dates=["2026-09-01", "2026-09-03"],
    )
    add_action(
        market,
        action_payload(
            "missing-split-session",
            "split",
            "2026-09-02",
            T0,
            ratio_numerator=2,
            ratio_denominator=1,
        ),
    )

    with pytest.raises(MarketActionError, match="has no eligible daily bar") as exc:
        calculate(market, bars)
    assert exc.value.code == "action_session_missing"


def test_cash_dividend_separates_price_return_from_reinvested_total_return(market):
    bars = put_bars(market, [100.0, 99.0])
    add_action(
        market,
        action_payload(
            "dividend-1",
            "cash_dividend",
            "2026-09-02",
            T0,
            cash_amount="1",
            cash_amount_basis="per_share_after_action",
            currency="USD",
        ),
    )

    series = calculate(market, bars)["series"]

    assert series[1]["price_return_index"] == "99"
    assert series[1]["total_return_index"] == "100"
    assert series[0]["total_return_adjusted_close"] == "99"
    assert series[1]["total_return_adjusted_close"] == "99"


def test_regular_and_special_dividends_are_distinct_same_day_components(market):
    bars = put_bars(market, [100.0, 97.0])
    add_action(
        market,
        action_payload(
            "regular-dividend",
            "cash_dividend",
            "2026-09-02",
            T0,
            cash_amount="1",
            cash_amount_basis="per_share_after_action",
            currency="USD",
            distribution_type="regular",
        ),
    )
    add_action(
        market,
        action_payload(
            "special-dividend",
            "cash_dividend",
            "2026-09-02",
            T0,
            cash_amount="2",
            cash_amount_basis="per_share_after_action",
            currency="USD",
            distribution_type="special",
        ),
    )

    assert calculate(market, bars)["series"][1]["total_return_index"] == "100"


def test_public_cutoff_selects_prior_action_revision_and_prevents_lookahead(market):
    bars = put_bars(market, [100.0, 98.0])
    first = add_action(
        market,
        action_payload(
            "dividend-corrected",
            "cash_dividend",
            "2026-09-02",
            T0,
            cash_amount="1",
            cash_amount_basis="per_share_after_action",
            currency="USD",
        ),
    )
    clock = market[1]
    clock["now"] = T1
    correction = action_payload(
        "dividend-corrected",
        "cash_dividend",
        "2026-09-02",
        T1,
        cash_amount="2",
        cash_amount_basis="per_share_after_action",
        currency="USD",
    )
    correction["provider_record_id"] = "record:dividend-corrected:correction"
    correction["source_refs"] = [source_ref("dividend-correction", T1)]
    corrected = market[3].put_action(
        NAMESPACE,
        correction,
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
        expected_revision=1,
    )

    before_correction = calculate(
        market, bars, public_cutoff=T1 - 1, acquired_cutoff=T_CUTOFF
    )
    after_correction = calculate(
        market, bars, public_cutoff=T1 + 1, acquired_cutoff=T_CUTOFF
    )

    assert before_correction["action_revision_ids"] == [first["revision_id"]]
    assert before_correction["series"][1]["total_return_index"] == "99"
    assert after_correction["action_revision_ids"] == [corrected["revision_id"]]
    assert after_correction["series"][1]["total_return_index"] == "100"


def test_cancellation_revision_removes_action_effect_only_after_publication(market):
    bars = put_bars(market, [100.0, 50.0])
    original = add_action(
        market,
        action_payload(
            "cancelled-split",
            "split",
            "2026-09-02",
            T0,
            ratio_numerator=2,
            ratio_denominator=1,
        ),
    )
    cancellation = action_payload(
        "cancelled-split",
        "split",
        "2026-09-02",
        T1,
        ratio_numerator=2,
        ratio_denominator=1,
        status="cancelled",
    )
    cancellation["provider_record_id"] = "record:cancelled-split:cancelled"
    cancellation["source_refs"] = [source_ref("cancelled-split", T1)]
    revised = market[3].put_action(
        NAMESPACE,
        cancellation,
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
        expected_revision=1,
    )

    before_cancel = calculate(
        market, bars, public_cutoff=T1 - 1, acquired_cutoff=T_CUTOFF
    )
    after_cancel = calculate(
        market, bars, public_cutoff=T1 + 1, acquired_cutoff=T_CUTOFF
    )

    assert before_cancel["action_revision_ids"] == [original["revision_id"]]
    assert before_cancel["series"][1]["price_return_index"] == "100"
    assert after_cancel["action_revision_ids"] == []
    assert after_cancel["series"][1]["price_return_index"] == "50"
    assert revised["status"] == "cancelled"


def test_identical_vendor_actions_apply_once_but_keep_both_sources(market):
    bars = put_bars(market, [100.0, 50.0])
    first = add_action(
        market,
        action_payload(
            "same-split-a",
            "split",
            "2026-09-02",
            T0,
            ratio_numerator=2,
            ratio_denominator=1,
            provider="vendor-a",
        ),
    )
    second = add_action(
        market,
        action_payload(
            "same-split-b",
            "split",
            "2026-09-02",
            T0,
            ratio_numerator=2,
            ratio_denominator=1,
            provider="vendor-b",
        ),
    )

    result = calculate(market, bars)

    assert result["series"][1]["price_return_index"] == "100"
    assert result["action_revision_ids"] == sorted(
        [first["revision_id"], second["revision_id"]]
    )


def test_conflicting_vendor_action_amounts_fail_closed(market):
    bars = put_bars(market, [100.0, 98.0])
    add_action(
        market,
        action_payload(
            "dividend-vendor-a",
            "cash_dividend",
            "2026-09-02",
            T0,
            cash_amount="1",
            cash_amount_basis="per_share_after_action",
            currency="USD",
            provider="vendor-a",
        ),
    )
    add_action(
        market,
        action_payload(
            "dividend-vendor-b",
            "cash_dividend",
            "2026-09-02",
            T0,
            cash_amount="2",
            cash_amount_basis="per_share_after_action",
            currency="USD",
            provider="vendor-b",
        ),
    )

    with pytest.raises(MarketActionError, match="providers disagree") as exc:
        calculate(market, bars)
    assert exc.value.code == "conflicting_actions"


@pytest.mark.parametrize(
    "action_type", ["spin_off", "merger", "delisting", "stock_dividend"]
)
def test_unimplemented_event_types_fail_closed_when_series_crosses_event(
    market, action_type
):
    bars = put_bars(market, [100.0, 50.0])
    add_action(
        market,
        action_payload(
            f"unsupported-{action_type}",
            action_type,
            "2026-09-02",
            T0,
            ratio_numerator=1 if action_type == "stock_dividend" else None,
            ratio_denominator=10 if action_type == "stock_dividend" else None,
            related_security_id="security:related"
            if action_type in {"spin_off", "merger"}
            else None,
        ),
    )

    with pytest.raises(
        MarketActionError, match="without an approved valuation rule"
    ) as exc:
        calculate(market, bars)
    assert exc.value.code == "unsupported_action"


def test_action_replay_is_idempotent_and_correction_is_append_only(market):
    _, _, _, actions = market
    payload = action_payload(
        "same-action",
        "split",
        "2026-09-02",
        T0,
        ratio_numerator=2,
        ratio_denominator=1,
    )
    first = add_action(market, payload)
    replay = add_action(market, payload)
    correction = dict(payload)
    correction["ratio_numerator"] = 3
    market[1]["now"] = T1
    corrected = actions.put_action(
        NAMESPACE,
        correction,
        principal_id=PRINCIPAL,
        scopes=OPERATOR,
        expected_revision=1,
    )

    assert replay["revision_id"] == first["revision_id"]
    assert corrected["revision"] == 2
    assert corrected["prior_revision_id"] == first["revision_id"]
    assert (
        market[0]
        .execute("SELECT COUNT(*) FROM market_corporate_action_revisions")
        .fetchone()[0]
        == 2
    )


def test_action_ingest_requires_source_entitlement(market):
    action = action_payload(
        "licensed-action",
        "split",
        "2026-09-02",
        T0,
        ratio_numerator=2,
        ratio_denominator=1,
    )
    with pytest.raises(MarketActionError) as exc:
        market[3].put_action(
            NAMESPACE,
            action,
            principal_id=PRINCIPAL,
            scopes={
                ACTION_WRITE_SCOPE,
                f"namespace:{NAMESPACE}:write",
            },
        )
    assert exc.value.code == "entitlement_unavailable"
