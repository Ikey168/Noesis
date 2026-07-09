"""Unit tests for the geospatial evidence plane (#783)."""

from __future__ import annotations

import pytest

from src.analytics.geospatial import geocode, place_coverage


def test_geocode_finds_known_places():
    refs = geocode("Protests erupted in Berlin and Paris today.")
    names = {r.name for r in refs}
    assert "berlin" in names and "paris" in names
    berlin = next(r for r in refs if r.name == "berlin")
    assert berlin.country == "DE"


def test_geocode_longest_match_first():
    refs = geocode("Reporting from New York.")
    assert refs[0].name == "new york"  # not "york"


def test_geocode_unknown_place_not_guessed():
    assert geocode("Something happened in Atlantis.") == []
    assert geocode("") == []


@pytest.fixture()
def conn():
    duckdb = pytest.importorskip("duckdb")
    c = duckdb.connect(":memory:")
    c.execute("CREATE TABLE documents (document_id TEXT, title TEXT, content TEXT)")
    return c


def _add(conn, doc_id, content):
    conn.execute("INSERT INTO documents VALUES (?, ?, ?)", [doc_id, "", content])


def test_place_coverage_counts_and_corroboration(conn):
    _add(conn, "d1", "Floods in Berlin overwhelmed the city.")
    _add(conn, "d2", "Berlin authorities responded to the flooding.")
    _add(conn, "d3", "Meanwhile Paris saw lighter rain.")
    cov = place_coverage(conn, topic=None)
    berlin = next(p for p in cov["places"] if p["place"] == "berlin")
    paris = next(p for p in cov["places"] if p["place"] == "paris")
    assert berlin["document_count"] == 2
    assert berlin["corroborated"] is True   # independent sources from same place
    assert paris["document_count"] == 1
    assert paris["corroborated"] is False


def test_place_coverage_topic_filter(conn):
    _add(conn, "d1", "A story about Berlin transit.")
    _add(conn, "d2", "A story about Paris fashion week.")
    cov = place_coverage(conn, topic="transit")
    assert cov["count"] == 1 and cov["places"][0]["place"] == "berlin"


def test_place_coverage_empty_corpus(conn):
    assert place_coverage(conn)["places"] == []
