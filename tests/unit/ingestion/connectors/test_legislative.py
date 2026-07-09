"""Unit tests for the legislative/court record connector (#788)."""

from __future__ import annotations

import pytest

from src.analytics.honesty import validate_analytic_output
from src.ingestion.connectors.legislative import (
    ABSTAIN,
    AGAINST,
    FOR,
    VoteRecord,
    check_position,
    check_position_claim,
    normalize_position,
    parse_position_claim,
    record_vote,
    voting_record,
)


def test_normalize_position():
    assert normalize_position("Yea") == FOR
    assert normalize_position("voted against") == AGAINST
    assert normalize_position("abstained") == ABSTAIN
    assert normalize_position("banana") is None


def test_parse_position_claim():
    p = parse_position_claim("Senator Smith supports the climate bill")
    assert p.actor == "Senator Smith" and p.topic == "climate bill" and p.claimed_position == FOR
    p2 = parse_position_claim("Rep. Jones opposed the tax cut")
    assert p2.claimed_position == AGAINST
    assert parse_position_claim("The weather is nice today") is None


@pytest.fixture()
def conn():
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect(":memory:")
    record_vote(c, VoteRecord(actor="Senator Smith", topic="climate bill", position="for", bill="HR-123", date=100, source="roll call"))
    return c


def test_supported(conn):
    env = check_position_claim(conn, "Senator Smith supports the climate bill")
    assert env["verdict"] == "supported"
    assert env["citation"]["cited"] is True
    assert validate_analytic_output(env) == []


def test_contradicted(conn):
    env = check_position_claim(conn, "Senator Smith opposes the climate bill")
    assert env["verdict"] == "contradicted"
    assert env["recorded_position"] == FOR


def test_unverifiable_no_record(conn):
    env = check_position_claim(conn, "Senator Jones supports the tax bill")
    assert env["verdict"] == "unverifiable"


def test_abstain_vs_for_is_unverifiable(conn):
    record_vote(conn, VoteRecord(actor="Rep Lee", topic="budget", position="abstain", date=50))
    env = check_position(conn, "Rep Lee", "budget", "for")
    # for vs abstain is not a clean for/against contradiction.
    assert env["verdict"] == "unverifiable"


def test_voting_record_and_idempotent(conn):
    # Re-recording the same vote does not duplicate.
    record_vote(conn, VoteRecord(actor="Senator Smith", topic="climate bill", position="for", bill="HR-123", date=100))
    assert len(voting_record(conn, "Senator Smith")) == 1
    assert len(voting_record(conn, "Senator Smith", topic="climate")) == 1
    assert voting_record(conn, "Nobody") == []


def test_uses_most_recent_vote(conn):
    # A later vote reverses position; the check uses the most recent.
    record_vote(conn, VoteRecord(actor="Senator Smith", topic="climate bill", position="against", bill="HR-999", date=200))
    env = check_position(conn, "Senator Smith", "climate bill", "against")
    assert env["verdict"] == "supported"
    assert env["recorded_position"] == AGAINST
