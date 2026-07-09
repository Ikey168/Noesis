"""Unit tests for cross-modal contradiction detection (#785)."""

from __future__ import annotations

import json

import pytest

from src.analytics.cross_modal import contradictions_in, find_intra_document_contradictions
from src.analytics.honesty import validate_analytic_output

PROSE = "Unemployment in Germany rose to 3.4% in 2024."


def test_direction_conflict():
    findings = contradictions_in(PROSE, ["Figure 1: unemployment in Germany fell to 2.1% in 2024."])
    assert len(findings) == 1
    assert "direction" in findings[0]["conflict"]


def test_value_conflict():
    findings = contradictions_in(PROSE, ["Figure 1: unemployment in Germany rose to 8.0% in 2024."])
    assert len(findings) == 1
    assert "value" in findings[0]["conflict"]


def test_agreement_within_tolerance_not_flagged():
    # Figure says 3.5% vs prose 3.4% — within the approximate-figure tolerance.
    assert contradictions_in(PROSE, ["Figure 1: unemployment in Germany rose to 3.5% in 2024."]) == []


def test_unrelated_subject_not_matched():
    assert contradictions_in(PROSE, ["Figure 1: GDP grew 2% in 2024."]) == []


def test_figure_label_number_not_read_as_value():
    # 'Figure 5' must not be read as a value of 5.
    findings = contradictions_in("Sales rose to 100 units in 2023.", ["Figure 5: sales rose to 101 units in 2023."])
    assert findings == []  # 100 vs 101, agreement (label 5 ignored)


def test_different_period_not_compared():
    assert contradictions_in(PROSE, ["Figure 1: unemployment in Germany rose to 9.0% in 2019."]) == []


@pytest.fixture()
def conn():
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE documents (document_id TEXT, content TEXT, metadata JSON)")
    return c


def _add(conn, doc_id, content, metadata):
    conn.execute("INSERT INTO documents VALUES (?, ?, ?)", [doc_id, content, json.dumps(metadata)])


def test_find_intra_document_contradictions(conn):
    _add(conn, "paper:1", PROSE, {})
    _add(conn, "paper:1#figure-1", "Figure 1: unemployment in Germany fell to 2.1% in 2024.",
         {"modality": "image", "parent_document_id": "paper:1"})
    env = find_intra_document_contradictions(conn)
    assert validate_analytic_output(env) == []
    assert env["finding_count"] == 1
    finding = env["findings"][0]
    assert finding["parent_document_id"] == "paper:1"
    assert finding["cited"] is True


def test_no_contradiction_when_figures_agree(conn):
    _add(conn, "paper:2", PROSE, {})
    _add(conn, "paper:2#figure-1", "Figure 1: unemployment in Germany rose to 3.4% in 2024.",
         {"modality": "image", "parent_document_id": "paper:2"})
    assert find_intra_document_contradictions(conn)["finding_count"] == 0


def test_empty_corpus(conn):
    assert find_intra_document_contradictions(conn)["findings"] == []
