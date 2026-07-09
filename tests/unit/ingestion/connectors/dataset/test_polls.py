"""Unit tests for polls-as-evidence (#787)."""

from __future__ import annotations

import pytest

from src.analytics.honesty import validate_analytic_output
from src.ingestion.connectors.dataset.polls import (
    PollMethodology,
    PollReading,
    check_opinion,
    parse_opinion_claim,
    poll_to_series,
)
from src.ingestion.connectors.dataset.store import ObservationStore


def test_parse_majority():
    c = parse_opinion_claim("A majority support the plan.")
    assert c.threshold == 50.0 and c.direction == "exceeds" and c.polarity == "support"


def test_parse_percentage_with_support_cue():
    c = parse_opinion_claim("More than 60% back the proposal.")
    assert c.threshold == 60.0 and c.direction == "exceeds"


def test_parse_oppose_polarity():
    c = parse_opinion_claim("Most voters oppose the measure.")
    assert c.polarity == "oppose"


def test_statistical_claim_is_not_an_opinion_claim():
    # A bare percentage with no opinion cue must not parse as opinion.
    assert parse_opinion_claim("GDP rose 3% last year.") is None
    assert parse_opinion_claim("Unemployment exceeded 5 percent.") is None


@pytest.fixture()
def conn_with_poll():
    duckdb = pytest.importorskip("duckdb")
    conn = duckdb.connect(":memory:")
    store = ObservationStore(conn)
    reading = PollReading(
        topic="carbon tax", option="support", support_pct=57.0, period="2024-03",
        methodology=PollMethodology(sample_n=1200, mode="online", margin_of_error=3.0, house="ACME", question="Do you support a carbon tax?"),
    )
    store.upsert(poll_to_series(reading, poll_id="acme-2024-03"))
    return conn


def test_poll_to_series_carries_methodology(conn_with_poll):
    store = ObservationStore(conn_with_poll)
    series = store.list_series(provider="poll")
    assert len(series) == 1
    assert series[0]["unit"] == "percent"


def test_majority_supported_with_moe(conn_with_poll):
    env = check_opinion(conn_with_poll, "A majority support the carbon tax.", topic="carbon tax")
    assert env["verdict"] == "supported"  # 57 > 50 + 3
    assert validate_analytic_output(env, interval_fields=["observed"]) == []
    # Methodology + question wording are declared assumptions.
    joined = " ".join(env["assumptions"])
    assert "carbon tax" in joined and "margin of error" in joined and "sample n=1200" in joined


def test_high_threshold_contradicted(conn_with_poll):
    env = check_opinion(conn_with_poll, "More than 70% support the carbon tax.", topic="carbon tax")
    assert env["verdict"] == "contradicted"  # 57 < 70 - 3


def test_within_moe_is_unverifiable(conn_with_poll):
    # Claim "more than 55%" vs observed 57 with ±3 -> 55 is within [54,60], unverifiable.
    env = check_opinion(conn_with_poll, "More than 55% support the carbon tax.", topic="carbon tax")
    assert env["verdict"] == "unverifiable"


def test_no_matching_poll(conn_with_poll):
    env = check_opinion(conn_with_poll, "A majority support the wealth tax.", topic="wealth tax")
    assert env["verdict"] == "unverifiable"


def test_non_opinion_claim(conn_with_poll):
    env = check_opinion(conn_with_poll, "GDP rose 3%.", topic="carbon tax")
    assert env["verdict"] == "unverifiable"
