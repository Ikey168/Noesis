"""Shared in-memory warehouse builders for the analytics tests."""

import threading
from types import SimpleNamespace

import pytest

duckdb = pytest.importorskip("duckdb")


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def lock():
    return threading.Lock()


@pytest.fixture
def seed():
    """Warehouse seeders, exposed as a fixture so tests don't import conftest
    directly (pytest's import mode makes relative imports of it unreliable)."""
    return SimpleNamespace(
        news=seed_news,
        outlet_scores=seed_outlet_scores,
        source_stances=seed_source_stances,
    )


def seed_news(conn, rows):
    """rows: list of (category, 'YYYY-MM-DD', sentiment_score)."""
    conn.execute(
        "CREATE TABLE news_articles ("
        "category VARCHAR, publish_date DATE, sentiment_score DOUBLE)"
    )
    if rows:
        conn.executemany(
            "INSERT INTO news_articles VALUES (?, ?, ?)",
            [(cat, day, s) for cat, day, s in rows],
        )


def seed_outlet_scores(conn, rows):
    """rows: list of (source, score_date, composite, frame, attr, neutral)."""
    conn.execute(
        "CREATE TABLE outlet_scores ("
        "source VARCHAR, source_type VARCHAR, score_date VARCHAR, "
        "composite_score DOUBLE, frame_diversity DOUBLE, "
        "attribution_rate DOUBLE, stance_neutrality DOUBLE)"
    )
    if rows:
        conn.executemany(
            "INSERT INTO outlet_scores "
            "(source, source_type, score_date, composite_score, frame_diversity, "
            "attribution_rate, stance_neutrality) VALUES (?, 'news', ?, ?, ?, ?, ?)",
            rows,
        )


def seed_source_stances(conn, rows):
    """rows: list of (source, topic, stance, document_count)."""
    conn.execute(
        "CREATE TABLE source_stances ("
        "source VARCHAR, topic VARCHAR, stance VARCHAR, document_count INTEGER)"
    )
    if rows:
        conn.executemany(
            "INSERT INTO source_stances (source, topic, stance, document_count) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
