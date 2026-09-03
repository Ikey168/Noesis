"""
Coverage tests for src/argument_mining/positions.py — policy position
extraction pipeline (issue #110).

Exercises the real public API (extract_positions, store_positions,
run_position_pipeline) plus the internal helpers (_is_position_bearing,
_extract_actor, _infer_topic, _document_date, _position_id) against real
Document instances. A model double keeps the tests independent of downloaded
weights.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure repo root is on path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.argument_mining.positions import (
    PositionRecord,
    _document_date,
    _extract_actor,
    _infer_topic,
    _is_position_bearing,
    _MIN_CONFIDENCE,
    _normalise_actor,
    _position_id,
    extract_positions,
    run_position_pipeline,
    store_positions,
)
from services.ingest.common.document_model import Document
from src.argument_mining.models import ClaimPrediction


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doc(document_id="d1", source_type="news", content="", **kw):
    return Document(
        document_id=document_id,
        source_type=source_type,
        language="en",
        ingested_at=kw.pop("ingested_at", 1_704_067_200_000),  # 2024-01-01
        content=content,
        **kw,
    )


@pytest.fixture(autouse=True)
def _claim_model(monkeypatch):
    class Detector:
        @staticmethod
        def predict_text(text):
            nonclaim = any(word in text.lower() for word in ("perhaps", "vague", "uncertain"))
            return ClaimPrediction(text, 0, not nonclaim, 0.9 if nonclaim else 0.8)

    monkeypatch.setattr(
        "src.argument_mining.models.get_claim_detector", lambda: Detector()
    )


# ---------------------------------------------------------------------------
# _is_position_bearing
# ---------------------------------------------------------------------------

def test_is_position_bearing_rejects_questions():
    is_pos, conf = _is_position_bearing("Will the government act on this?", _MIN_CONFIDENCE)
    assert is_pos is False
    assert conf == 0.0


def test_is_position_bearing_true_for_commitment_claim():
    sent = "The government will invest ten billion dollars in renewable energy."
    is_pos, conf = _is_position_bearing(sent, _MIN_CONFIDENCE)
    assert is_pos is True
    assert conf >= _MIN_CONFIDENCE


def test_is_position_bearing_commitment_boost_applied():
    # A sentence with a commitment verb gets a +0.15 boost (capped at 0.95).
    sent = "The minister pledged to reform the tax system next year."
    _is_pos, conf = _is_position_bearing(sent, _MIN_CONFIDENCE)
    assert conf <= 0.95


def test_is_position_bearing_false_for_hedged_opinion():
    sent = "In my view, we might perhaps consider some vague possibility."
    is_pos, _conf = _is_position_bearing(sent, _MIN_CONFIDENCE)
    assert is_pos is False


# ---------------------------------------------------------------------------
# _extract_actor
# ---------------------------------------------------------------------------

def test_extract_actor_transcript_allcaps_label():
    doc = _doc(source_type="transcript")
    actor = _extract_actor("SENATOR SMITH: We will pass the bill.", doc)
    assert "SENATOR SMITH" in actor


def test_extract_actor_titlecase_label():
    doc = _doc(source_type="transcript")
    actor = _extract_actor("Jane Doe: We will act on climate.", doc)
    assert actor == "Jane Doe"


def test_extract_actor_title_prefix():
    doc = _doc()
    actor = _extract_actor("President Biden pledged to cut emissions.", doc)
    assert actor == "Biden"


def test_extract_actor_name_said_pattern():
    doc = _doc()
    actor = _extract_actor("Angela Merkel announced a new energy programme.", doc)
    assert actor == "Angela Merkel"


def test_extract_actor_institution_pattern():
    doc = _doc()
    actor = _extract_actor("The government will raise the minimum wage.", doc)
    assert actor.lower().startswith("the government") or actor == "The government"


def test_extract_actor_fallback_to_author():
    doc = _doc(authors=["Reporter Name"], source_id="Outlet")
    actor = _extract_actor("Nothing matches any pattern here at all today.", doc)
    assert actor == "Reporter Name"


def test_extract_actor_fallback_to_source_id():
    doc = _doc(authors=[], source_id="The Times")
    actor = _extract_actor("Nothing matches any pattern here at all today.", doc)
    assert actor == "The Times"


def test_extract_actor_fallback_to_source_type():
    doc = _doc(authors=[], source_id=None, source_type="book")
    actor = _extract_actor("Nothing matches any pattern here at all today.", doc)
    assert actor == "book"


def test_normalise_actor_collapses_whitespace():
    assert _normalise_actor("  Jane   Doe  ") == "Jane Doe"


# ---------------------------------------------------------------------------
# _infer_topic
# ---------------------------------------------------------------------------

def test_infer_topic_from_metadata_topics_matching_label():
    doc = _doc(metadata={"topics": ["environment and climate"]})
    assert _infer_topic(doc, "some sentence") == "environment"


def test_infer_topic_from_metadata_topics_unknown_raw():
    doc = _doc(metadata={"topics": ["weirdlabel"]})
    assert _infer_topic(doc, "some sentence") == "weirdlabel"


def test_infer_topic_from_category():
    doc = _doc(metadata={"category": "economy weekly digest"})
    assert _infer_topic(doc, "no keywords here") == "economy"


def test_infer_topic_keyword_scan():
    doc = _doc(title="Health update", metadata={})
    topic = _infer_topic(doc, "The hospital vaccine and drug for the patient.")
    assert topic == "healthcare"


def test_infer_topic_defaults_to_general():
    doc = _doc(title="", metadata={})
    assert _infer_topic(doc, "A neutral sentence about morning coffee and quiet gardens.") == "general"


# ---------------------------------------------------------------------------
# _document_date
# ---------------------------------------------------------------------------

def test_document_date_from_created_at():
    doc = _doc(created_at=1_704_067_200_000)  # 2024-01-01 UTC
    assert _document_date(doc) == "2024-01-01"


def test_document_date_falls_back_to_ingested_at():
    doc = _doc(created_at=None, ingested_at=1_704_067_200_000)
    assert _document_date(doc) == "2024-01-01"


def test_document_date_returns_none_when_no_timestamp():
    doc = _doc(created_at=None, ingested_at=0)
    assert _document_date(doc) is None


def test_document_date_returns_none_on_invalid_timestamp():
    doc = _doc(created_at=10**24)  # overflows fromtimestamp
    assert _document_date(doc) is None


# ---------------------------------------------------------------------------
# _position_id
# ---------------------------------------------------------------------------

def test_position_id_deterministic_and_prefixed():
    a = _position_id("doc", "sentence", "actor")
    b = _position_id("doc", "sentence", "actor")
    assert a == b
    assert a.startswith("pos-")


def test_position_id_differs_by_actor():
    assert _position_id("doc", "s", "A") != _position_id("doc", "s", "B")


# ---------------------------------------------------------------------------
# extract_positions — end-to-end
# ---------------------------------------------------------------------------

def test_extract_positions_empty_document():
    doc = _doc(content="")
    assert extract_positions(doc) == []


def test_extract_positions_finds_commitments():
    doc = _doc(
        document_id="d-clim",
        title="Climate policy announcement",
        content=(
            "President Biden pledged to cut carbon emissions in half. "
            "The government will invest heavily in renewable energy and solar power. "
            "Will this really work out for everyone involved in the plan?"
        ),
        metadata={"topics": ["environment"]},
    )
    recs = extract_positions(doc)
    assert recs, "expected at least one position"
    actors = {r.actor for r in recs}
    assert "Biden" in actors
    for r in recs:
        assert r.topic == "environment"
        assert r.document_id == "d-clim"
        assert r.source_type == "news"
        assert r.position_date == "2024-01-01"
        assert 0.0 <= r.confidence <= 1.0
        assert not r.position_text.strip().endswith("?")  # questions excluded


def test_extract_positions_dedupes_identical_sentences():
    sent = "The government will invest heavily in renewable energy programmes. "
    doc = _doc(document_id="dup", content=sent + sent, metadata={"topics": ["environment"]})
    recs = extract_positions(doc)
    ids = [r.position_id for r in recs]
    assert len(ids) == len(set(ids))  # no duplicate position_id


def test_extract_positions_returns_empty_for_non_position_text():
    doc = _doc(content=(
        "It remains to be seen whether this vague situation improves at all. "
        "Perhaps things could possibly change in some uncertain way eventually."
    ))
    assert extract_positions(doc) == []


# ---------------------------------------------------------------------------
# store_positions / run_position_pipeline
# ---------------------------------------------------------------------------

class _FakeConn:
    def __init__(self):
        self.executemany_calls = []

    def executemany(self, sql, seq):
        self.executemany_calls.append((sql, list(seq)))


def test_store_positions_noop_on_empty():
    conn = _FakeConn()
    store_positions([], conn)
    assert conn.executemany_calls == []


def test_store_positions_persists_records():
    rec = PositionRecord(
        position_id="pos-x",
        document_id="d",
        source_type="news",
        actor="Biden",
        topic="environment",
        position_text="The government will act.",
        position_date="2024-01-01",
        confidence=0.7,
    )
    conn = _FakeConn()
    store_positions([rec], conn)
    assert len(conn.executemany_calls) == 1
    _sql, rows = conn.executemany_calls[0]
    assert rows[0][0] == "pos-x"      # position_id
    assert rows[0][3] == "Biden"      # actor
    assert rows[0][4] == "environment"


def test_run_position_pipeline_extracts_and_stores():
    doc = _doc(
        document_id="pipe",
        content="The government will invest heavily in renewable energy this year.",
        metadata={"topics": ["environment"]},
    )
    conn = _FakeConn()
    records = run_position_pipeline(doc, conn)
    assert records, "expected positions from pipeline"
    # store_positions was invoked once with the same records.
    assert len(conn.executemany_calls) == 1
    _sql, rows = conn.executemany_calls[0]
    assert len(rows) == len(records)


def test_run_position_pipeline_no_positions_skips_store():
    doc = _doc(content="Perhaps something might possibly happen at some point maybe.")
    conn = _FakeConn()
    records = run_position_pipeline(doc, conn)
    assert records == []
    # store_positions returns early on empty -> no executemany call.
    assert conn.executemany_calls == []
