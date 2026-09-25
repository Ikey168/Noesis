from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.domains.market.specialized import (
    MarketSpecializedError,
    MarketSpecializedStore,
    SPECIALIZED_READ_SCOPE,
    SPECIALIZED_WRITE_SCOPE,
)
from src.domains.market.entitlements import MarketEntitlementStore

ROOT = Path(__file__).resolve().parents[3]
NS = "market:specialized-fixture"
SCOPES = {SPECIALIZED_READ_SCOPE, SPECIALIZED_WRITE_SCOPE}


def test_fixed_income_and_derivatives_are_reproducible_and_rights_explicit():
    conn = duckdb.connect(":memory:")
    store = MarketSpecializedStore(conn, now=lambda: 10_000)
    bond = store.fixed_income(
        NS,
        instrument={
            "instrument_id": "bond:fixture",
            "face_value": 100,
            "coupon_rate": 0.04,
            "coupon_frequency": 2,
            "maturity_years": 2,
            "day_count": "30/360",
            "issuer_id": "issuer:fixture",
            "currency": "USD",
            "settlement_calendar": "calendar:us-government",
            "business_day_convention": "following",
        },
        quote={"clean_price": 99.5, "source_revision_id": "bond:quote:1"},
        scenarios=[{"name": "+100bp", "yield_shift_bps": 100}],
        credit_evidence=[{"kind": "rating", "value": "A", "source_revision_id": "rating:1"}],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert bond["status"] == "complete"
    assert bond["analytics"]["modified_duration"] > 0
    assert bond["model_scope"]["status"] == "supported_plain_bond"
    assert set(bond["source_revision_ids"]) >= {"bond:quote:1", "rating:1"}
    assert bond["source_rights"]["status"] == "rights_unverified"
    stale = store.fixed_income(
        NS,
        instrument={**bond["instrument"], "valuation_at_ms": 10_000},
        quote={"clean_price": 99.5, "quote_at_ms": 1, "as_of_ms": 10_000, "stale_after_ms": 100},
        cashflows=[{"time_years": 1, "amount": 102}],
        credit_evidence=[{"kind": "rating", "value": "A"}],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert stale["quote_quality"]["status"] == "stale"

    unsupported = store.fixed_income(
        NS,
        instrument={
            **bond["instrument"],
            "maturity_years": 2,
            "callable": True,
        },
        quote={"yield": 0.04},
        credit_evidence=[{"kind": "prospectus", "source_revision_id": "prospectus:1"}],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert unsupported["status"] == "unsupported_instrument"
    assert unsupported["model_scope"]["option_adjusted"] is False
    assert store.inspect_run(NS, bond["run_id"], principal_id="alice", scopes=SCOPES)["run_id"] == bond["run_id"]
    assert store.export_run(NS, bond["run_id"], principal_id="alice", scopes=SCOPES)["replay"]["deterministic"] is True

    option = store.derivatives(
        NS,
        contract={
            "underlying": "listing:fixture",
            "underlying_price": 100,
            "strike": 100,
            "time_to_expiry_years": 1,
            "rate": 0.02,
            "dividend_yield": 0,
            "volatility": 0.2,
            "option_type": "call",
            "exercise_style": "european",
            "multiplier": 100,
        },
        quote={"price": 8.916},
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert option["quote_status"] == "valid"
    assert option["implied_volatility"] == pytest.approx(0.2, abs=0.01)
    for filename in (
        "noesis-market-fixed-income-analytics-v1.json",
        "noesis-market-derivatives-analytics-v1.json",
    ):
        Draft7Validator(json.loads((ROOT / "contracts/schemas/jsonschema" / filename).read_text())).validate(
            bond if "fixed-income" in filename else option
        )
    conn.close()


def test_fx_negative_commodity_and_digital_reorg_replay():
    conn = duckdb.connect(":memory:")
    store = MarketSpecializedStore(conn)
    fx = store.fx_commodity(
        NS,
        observations=[
            {"at_ms": 1, "asset_type": "commodity_future", "price": -37, "base": "CL", "quote": "USD", "contract_id": "CL:1", "source_revision_id": "c:1"},
            {"at_ms": 2, "asset_type": "commodity_future", "price": -35, "base": "CL", "quote": "USD", "contract_id": "CL:1", "source_revision_id": "c:2"},
        ],
        trades=[{"entry_price": -37, "exit_price": -35, "quantity": 1, "multiplier": 1000}],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert fx["status"] == "complete"
    assert fx["tradable_pnl"][0]["pnl"] == 2000

    digital = store.digital_asset(
        NS,
        asset={"chain_id": "ethereum", "contract_address": "0xABC", "symbol": "FIX", "initial_supply": 100},
        observations=[
            {"at_ms": 1, "price": 10, "venue": "a"},
            {"at_ms": 1, "price": 12, "venue": "b"},
        ],
        chain_events=[
            {"block_number": 10, "block_hash": "old", "canonical": True},
            {"block_number": 10, "block_hash": "new", "canonical": True},
        ],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert digital["identity"]["contract_address"] == "0xabc"
    assert digital["identity"]["asset_kind"] == "contract"
    assert digital["chain_reorganizations"][0]["recovered"] is True
    assert digital["price_disagreements"][0]["status"] == "disputed"

    native = store.digital_asset(
        NS,
        asset={"chain_id": "bitcoin", "native_asset_id": "btc", "symbol": "BTC"},
        observations=[{"at_ms": 1, "price": 50_000, "venue": "fixture"}],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert native["identity"]["asset_kind"] == "native"
    assert native["identity"]["contract_address"] is None
    assert native["identity"]["asset_id"] == "bitcoin:native:btc"

    with pytest.raises(MarketSpecializedError, match="wrapped asset"):
        store.digital_asset(
            NS,
            asset={"chain_id": "bitcoin", "native_asset_id": "btc", "wrapped_asset_of": "bitcoin:native:btc"},
            observations=[],
            owner="alice",
            principal_id="alice",
            scopes=SCOPES,
        )
    conn.close()


def test_fx_quote_direction_preserves_pair_identity_and_normalizes_reciprocals():
    conn = duckdb.connect(":memory:")
    store = MarketSpecializedStore(conn)
    result = store.fx_commodity(
        NS,
        observations=[
            {
                "at_ms": 1,
                "asset_type": "fx_spot",
                "rate": 1.1,
                "base": "EUR",
                "quote": "USD",
                "quote_direction": "quote_per_base",
            },
            {
                "at_ms": 2,
                "asset_type": "fx_spot",
                "rate": 1 / 1.1,
                "base": "EUR",
                "quote": "USD",
                "quote_direction": "base_per_quote",
            },
        ],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )

    first, second = result["observations"]
    assert (first["base"], first["quote"]) == ("EUR", "USD")
    assert (second["base"], second["quote"]) == ("EUR", "USD")
    assert first["value"] == pytest.approx(1.1)
    assert second["value"] == pytest.approx(first["value"])
    assert first["quote_direction"] == second["quote_direction"] == "quote_per_base"
    assert second["original_quote_direction"] == "base_per_quote"
    conn.close()


def test_intraday_recovery_and_invalid_option_bounds_are_visible():
    conn = duckdb.connect(":memory:")
    store = MarketSpecializedStore(conn)
    intraday = store.intraday_replay(
        NS,
        events=[
            {"sequence": 1, "exchange_at_ms": 1000, "received_at_ms": 1002, "revision_id": "i:1"},
            {"sequence": 3, "exchange_at_ms": 1001, "received_at_ms": 1006, "revision_id": "i:3"},
            {"sequence": 3, "exchange_at_ms": 1001, "received_at_ms": 1007, "revision_id": "i:3-corrected", "correction_of": "i:3"},
        ],
        recovered_events=[{"sequence": 2, "exchange_at_ms": 1000, "received_at_ms": 1003, "revision_id": "i:2"}],
        entitlement_tier="delayed",
        target_events_per_second=10,
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert intraday["status"] == "complete"
    assert intraday["replay"]["gaps"] == [2]
    assert intraday["replay"]["recovered_sequences"] == [2]
    assert intraday["replay"]["unrecovered_sequences"] == []
    assert intraday["quality"]["out_of_order_count"] == 1
    assert intraday["quality"]["duplicate_sequence_count"] == 1
    assert intraday["quality"]["correction_count"] == 1

    degraded = store.intraday_replay(
        NS,
        events=[
            {"sequence": 10, "exchange_at_ms": 2000, "received_at_ms": 2001},
            {"sequence": 12, "exchange_at_ms": 2002, "received_at_ms": 2003},
        ],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert degraded["status"] == "degraded"
    assert degraded["replay"]["gaps"] == [11]
    assert degraded["replay"]["recovered_sequences"] == []
    assert degraded["replay"]["unrecovered_sequences"] == [11]

    with pytest.raises(MarketSpecializedError, match="contract.underlying_price"):
        store.derivatives(
            NS,
            contract={"underlying": "x", "strike": 100, "time_to_expiry_years": 1},
            owner="alice",
            principal_id="alice",
            scopes=SCOPES,
        )
    conn.close()


def test_international_matrix_marks_missing_live_inputs():
    conn = duckdb.connect(":memory:")
    store = MarketSpecializedStore(conn)
    result = store.international_coverage(
        NS,
        markets=[{"market_id": "xetra", "provider": "fixture", "currency": "EUR"}],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert result["status"] == "partial"
    assert "calendar" in result["coverage_matrix"][0]["missing"]
    conn.close()


def test_specialized_derivation_rechecks_current_entitlement():
    conn = duckdb.connect(":memory:")
    entitlement = MarketEntitlementStore(conn, now=lambda: 100)
    entitlement.put_entitlement(
        NS,
        "entitlement:fixture",
        provider="fixture-provider",
        license_id="fixture-license",
        capabilities=["read", "retain", "derive"],
        evidence_ref="evidence:fixture",
        decision_ref="decision:fixture",
        principal_id="operator",
        scopes={"operator"},
        effective_at_ms=0,
    )
    store = MarketSpecializedStore(conn, now=lambda: 100)
    result = store.fx_commodity(
        NS,
        observations=[{"at_ms": 1, "asset_type": "fx_spot", "rate": 1.1, "base": "EUR", "quote": "USD"}],
        source_entitlements=[{"source_ref_id": "fx:1", "entitlement_id": "entitlement:fixture", "provider": "fixture-provider", "license_id": "fixture-license"}],
        owner="alice",
        principal_id="alice",
        scopes=SCOPES | {"market:entitlement:entitlement:fixture:derive"},
    )
    assert result["source_rights"]["status"] == "authorized"
    conn.close()
