"""Unit tests for the legislative/court record connector (#788)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.analytics.honesty import validate_analytic_output
from src.ingestion.connectors.legislative import (
    ABSTAIN,
    AGAINST,
    FOR,
    LegislativeConnector,
    VoteRecord,
    check_position,
    check_position_claim,
    normalize_position,
    parse_position_claim,
    record_vote,
    voting_record,
)
from src.ingestion.connectors.registry import get_connector, is_registered


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


def test_registered_connector_parses_fixture_and_feeds_position_check(tmp_path):
    fixture = Path(__file__).resolve().parents[3] / "fixtures/legislative/votes.json"
    connector = LegislativeConnector(sources=[str(fixture)])
    documents = list(connector.harvest())
    assert len(documents) == 2
    assert documents[0].source_type == "note"
    assert documents[0].metadata["record_type"] == "legislative_vote"
    assert documents[0].url.startswith("https://")

    duckdb = pytest.importorskip("duckdb")
    database = duckdb.connect(":memory:")
    for document in documents:
        metadata = document.metadata
        record_vote(database, VoteRecord(
            actor=metadata["actor"], topic=metadata["topic"],
            bill=metadata["bill"], position=metadata["position"],
            date=metadata["date"], source=metadata["source"],
            document_id=document.document_id,
        ))
    result = check_position(database, "Alex Example", "Clean Energy", "for")
    assert result["verdict"] == "supported"
    assert validate_analytic_output(result) == []


def test_legislative_document_id_does_not_depend_on_checkout_path(tmp_path):
    fixture = Path(__file__).resolve().parents[3] / "fixtures/legislative/votes.json"
    left = tmp_path / "left" / "votes.json"
    right = tmp_path / "right" / "renamed.json"
    left.parent.mkdir()
    right.parent.mkdir()
    left.write_bytes(fixture.read_bytes())
    right.write_bytes(fixture.read_bytes())

    left_ids = [item.document_id for item in LegislativeConnector([str(left)]).harvest()]
    right_ids = [item.document_id for item in LegislativeConnector([str(right)]).harvest()]
    assert left_ids == right_ids


def test_legislative_connector_is_in_builtin_registry():
    import src.ingestion.connectors  # noqa: F401
    assert is_registered("legislative")
    assert isinstance(get_connector("legislative"), LegislativeConnector)
