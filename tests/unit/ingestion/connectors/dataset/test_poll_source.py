"""Unit tests for the CSV polling-aggregate source (#822) — offline."""

from __future__ import annotations

import pytest

from src.ingestion.connectors.dataset.poll_source import (
    PollColumnMap,
    harvest_polls,
    parse_poll_csv,
)
from src.ingestion.connectors.dataset.store import ObservationStore

CSV = """topic,pollster,end_date,sample_size,methodology,moe,population,question,support_pct,oppose_pct
carbon tax,ACME Polling,2024-03-15,1200,online,3.0,adults,Do you support a carbon tax?,57,38
carbon tax,Beta Research,2024-03-20,900,phone,3.5,likely voters,Do you support a carbon tax?,52,41
wealth tax,ACME Polling,2024-03-15,1200,online,3.0,adults,Do you support a wealth tax?,61,
bad row,,not-a-date,,,,,,,
"""


def test_parse_readings_with_methodology():
    readings = parse_poll_csv(CSV)
    # 2 options x 2 carbon-tax rows + 1 wealth-tax support (oppose empty, skipped).
    assert len(readings) == 5
    first = next(r for r in readings if r.methodology.house == "ACME Polling" and r.topic == "carbon tax" and r.option == "support")
    assert first.support_pct == 57.0
    assert first.period == "2024-03"
    assert first.methodology.sample_n == 1200
    assert first.methodology.margin_of_error == 3.0
    assert first.methodology.question == "Do you support a carbon tax?"
    # Distinct houses keep distinct methodology — never averaged together.
    beta = next(r for r in readings if r.methodology.house == "Beta Research")
    assert beta.methodology.mode == "phone" and beta.methodology.sample_n == 900


def test_bad_rows_skipped_not_guessed():
    readings = parse_poll_csv(CSV)
    assert not any(r.topic == "bad row" for r in readings)


def test_topic_override_for_single_question_export():
    csv_text = "pollster,end_date,sample_size,methodology,moe,population,question,support_pct,oppose_pct\nACME,2024-04-01,1000,online,3.0,adults,Q?,55,40\n"
    readings = parse_poll_csv(csv_text, topic="carbon tax")
    assert len(readings) == 2
    assert all(r.topic == "carbon tax" for r in readings)


def test_custom_column_map():
    csv_text = "subject,firm,fieldwork_end,n,how,err,pop,wording,yes,no\ncarbon tax,ACME,2024-05-02,800,online,4.0,adults,Q?,58,35\n"
    cmap = PollColumnMap(
        topic="subject", pollster="firm", end_date="fieldwork_end", sample_size="n",
        mode="how", margin_of_error="err", population="pop", question="wording",
        options={"support": "yes", "oppose": "no"},
    )
    readings = parse_poll_csv(csv_text, column_map=cmap)
    assert len(readings) == 2
    assert readings[0].methodology.margin_of_error == 4.0


def test_harvest_end_to_end_check_opinion():
    duckdb = pytest.importorskip("duckdb")
    from src.ingestion.connectors.dataset.polls import check_opinion

    conn = duckdb.connect(":memory:")
    store = ObservationStore(conn)
    stored = harvest_polls("https://aggregate.example/polls.csv", store, fetch=lambda url: CSV, as_of=1)
    assert stored == 5

    env = check_opinion(conn, "A majority support the carbon tax.", topic="carbon tax")
    # Latest reading resolves with its own MOE as tolerance and question wording declared.
    assert env["verdict"] in ("supported", "unverifiable")
    assert any("question" in a for a in env["assumptions"])
    # The 70% claim contradicts every harvested reading.
    env_high = check_opinion(conn, "More than 70% support the carbon tax.", topic="carbon tax")
    assert env_high["verdict"] == "contradicted"
