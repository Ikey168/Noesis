"""Unit tests for claim-vs-data checking (A4)."""

from __future__ import annotations

import pytest

from services.ingest.common.series_model import Observation, SeriesRecord
from src.analytics.claim_check import (
    check_assertion,
    claim_vs_data,
    data_check_ledger,
    record_check,
    resolve_series,
)
from src.analytics.honesty import validate_analytic_output
from src.argument_mining.quantities import QuantityExtractor
from src.ingestion.connectors.dataset.store import ObservationStore


@pytest.fixture()
def conn():
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect(":memory:")
    store = ObservationStore(c)
    store.upsert(
        SeriesRecord(
            series_id="wb:SL.UEM.TOTL.ZS:DE",
            provider="worldbank",
            title="Unemployment, total (% of labor force) - Germany",
            frequency="annual",
            as_of=100,
            observations=[Observation("2022", 3.13), Observation("2023", 3.02), Observation("2024", 3.40)],
            unit="percent",
            geography="DE",
        )
    )
    return c


def _assert(text):
    return QuantityExtractor().extract(text)[0]


def test_resolver_ranks_and_thresholds(conn):
    cands = resolve_series(conn, _assert("Unemployment in Germany rose in 2024."))
    assert cands and cands[0].series_id == "wb:SL.UEM.TOTL.ZS:DE"
    assert 0.0 < cands[0].match_confidence <= 1.0
    # A geography mismatch excludes the series entirely.
    assert resolve_series(conn, _assert("Unemployment in France rose in 2024.")) == []


def test_movement_supported(conn):
    env = check_assertion(conn, _assert("Unemployment in Germany rose in 2024."))
    assert env["verdict"] == "supported"
    assert env["direction"] == "rose"
    assert env["n"] == 2
    assert validate_analytic_output(env, interval_fields=["observed"]) == []


def test_movement_contradicted(conn):
    env = check_assertion(conn, _assert("Unemployment in Germany fell in 2024."))
    assert env["verdict"] == "contradicted"
    assert validate_analytic_output(env, interval_fields=["observed"]) == []


def test_magnitude_supported(conn):
    env = check_assertion(conn, _assert("Unemployment in Germany exceeded 3 percent in 2024."))
    assert env["verdict"] == "supported"
    assert env["claimed_value"] == 3.0
    assert validate_analytic_output(env, interval_fields=["observed"]) == []


def test_magnitude_contradicted(conn):
    env = check_assertion(conn, _assert("Unemployment in Germany exceeded 10 percent in 2024."))
    assert env["verdict"] == "contradicted"


def test_unverifiable_no_series(conn):
    env = check_assertion(conn, _assert("Chocolate consumption tripled in 2024."))
    assert env["verdict"] == "unverifiable"
    assert env["n"] == 0
    # An unverifiable envelope is still a valid honesty output (no interval req).
    assert validate_analytic_output(env) == []


def test_unverifiable_missing_period(conn):
    env = check_assertion(conn, _assert("Unemployment in Germany exceeded 3 percent in 1990."))
    assert env["verdict"] == "unverifiable"


def test_verdict_is_three_valued_never_boolean(conn):
    for text in [
        "Unemployment in Germany rose in 2024.",
        "Unemployment in Germany fell in 2024.",
        "Chocolate consumption tripled in 2024.",
    ]:
        env = check_assertion(conn, _assert(text))
        assert env["verdict"] in ("supported", "contradicted", "unverifiable")
        assert not isinstance(env["verdict"], bool)


def test_record_and_ledger(conn):
    supported = check_assertion(conn, _assert("Unemployment in Germany rose in 2024."))
    contradicted = check_assertion(conn, _assert("Unemployment in Germany fell in 2024."))
    record_check(conn, supported, claim_id="c1", now_ms=1)
    record_check(conn, contradicted, claim_id="c2", now_ms=2)
    # Ledger defaults to contradicted checks.
    led = data_check_ledger(conn, verdict="contradicted")
    assert led["count"] == 1
    assert led["checks"][0]["series_id"] == "wb:SL.UEM.TOTL.ZS:DE"
    assert data_check_ledger(conn, verdict=None)["count"] == 2


def test_record_is_idempotent(conn):
    env = check_assertion(conn, _assert("Unemployment in Germany rose in 2024."))
    id1 = record_check(conn, env, claim_id="c1", now_ms=1)
    id2 = record_check(conn, env, claim_id="c1", now_ms=1)
    assert id1 == id2
    assert data_check_ledger(conn, verdict=None)["count"] == 1


def test_claim_vs_data_panel_payload(conn):
    env = check_assertion(conn, _assert("Unemployment in Germany rose in 2024."))
    record_check(conn, env, claim_id="c1", now_ms=1)
    payload = claim_vs_data(conn, topic="unemployment")
    assert payload["count"] == 1
    check = payload["checks"][0]
    assert check["verdict"] == "supported"
    assert check["observed"] is not None
    assert check["series_title"] and "Germany" in check["series_title"]


def test_check_pins_vintage_for_replay(conn):
    # A newer vintage revises 2024 downward so the movement reverses.
    ObservationStore(conn).upsert(
        SeriesRecord(
            series_id="wb:SL.UEM.TOTL.ZS:DE",
            provider="worldbank",
            title="Unemployment - Germany",
            frequency="annual",
            as_of=200,
            observations=[Observation("2023", 3.02), Observation("2024", 2.40)],
            unit="percent",
            geography="DE",
        )
    )
    latest = check_assertion(conn, _assert("Unemployment in Germany rose in 2024."))
    assert latest["verdict"] == "contradicted"  # 3.02 -> 2.40 under latest vintage
    pinned = check_assertion(conn, _assert("Unemployment in Germany rose in 2024."), as_of=100)
    assert pinned["verdict"] == "supported"  # 3.02 -> 3.40 under the original vintage
