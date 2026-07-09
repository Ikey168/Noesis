"""Unit tests for speaker-turn analytics (#789)."""

from __future__ import annotations

from src.analytics.honesty import validate_analytic_output
from src.analytics.speaker_turns import analyze_segments


def _segs():
    return [
        {"speaker": "SPEAKER_A", "start_s": 0, "end_s": 10, "text": "hello there everyone welcome to the show"},
        {"speaker": "SPEAKER_B", "start_s": 9, "end_s": 15, "text": "thanks"},           # interrupts A
        {"speaker": "SPEAKER_A", "start_s": 15, "end_s": 40, "text": "as I was saying " * 5},
        {"speaker": "SPEAKER_B", "start_s": 40, "end_s": 45, "text": "understood"},
    ]


def test_airtime_and_floor_share():
    env = analyze_segments(_segs())
    assert validate_analytic_output(env) == []
    a = next(s for s in env["speakers"] if s["speaker"] == "SPEAKER_A")
    b = next(s for s in env["speakers"] if s["speaker"] == "SPEAKER_B")
    assert a["airtime_s"] == 35.0
    assert b["airtime_s"] == 11.0
    assert round(a["floor_share"] + b["floor_share"], 3) == 1.0
    assert a["floor_share"] > b["floor_share"]
    assert a["turns"] == 2 and b["turns"] == 2


def test_interruption_detected():
    env = analyze_segments(_segs())
    assert env["interruptions"] == [{"interrupter": "SPEAKER_B", "interrupted": "SPEAKER_A", "count": 1}]


def test_no_interruption_when_no_overlap():
    segs = [
        {"speaker": "A", "start_s": 0, "end_s": 10, "text": "x"},
        {"speaker": "B", "start_s": 10, "end_s": 20, "text": "y"},
    ]
    assert analyze_segments(segs)["interruptions"] == []


def test_same_speaker_overlap_not_interruption():
    segs = [
        {"speaker": "A", "start_s": 0, "end_s": 10, "text": "x"},
        {"speaker": "A", "start_s": 9, "end_s": 15, "text": "y"},
    ]
    assert analyze_segments(segs)["interruptions"] == []
    assert analyze_segments(segs)["speaker_count"] == 1


def test_empty_and_malformed_segments():
    assert analyze_segments([])["speaker_count"] == 0
    # Segments missing fields are dropped.
    env = analyze_segments([{"speaker": None, "start_s": 0, "end_s": 1}, {"speaker": "A"}])
    assert env["speaker_count"] == 0


def test_diarization_assumption_declared():
    env = analyze_segments(_segs())
    assert any("diarization" in a for a in env["assumptions"])


def test_speaker_balance_reads_documents():
    import pytest

    duckdb = pytest.importorskip("duckdb")
    import json

    from src.analytics.speaker_turns import speaker_balance

    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE documents (document_id TEXT, source_type TEXT, url TEXT, content_ref TEXT, content TEXT, metadata JSON)")
    for i, (spk, a, b) in enumerate([("A", 0, 12), ("B", 12, 18), ("A", 18, 30)]):
        conn.execute(
            "INSERT INTO documents VALUES (?, 'transcript', 'http://x/ep1', ?, ?, ?)",
            [f"m#{i}", f"file://ep1#t={a}", "word " * 3, json.dumps({"speaker": spk, "start_s": a, "end_s": b})],
        )
    env = speaker_balance(conn, media="ep1")
    assert env["speaker_count"] == 2
    assert validate_analytic_output(env) == []
