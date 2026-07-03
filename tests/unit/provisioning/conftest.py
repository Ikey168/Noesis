"""Fixtures for the provisioning-plane tests: a temp DuckDB warehouse seeded
with a shared corpus (news_articles), a claim layer (argument_claims) and
outlet transparency scores (outlet_scores) so criteria-attach and routing have
something to resolve against."""

from types import SimpleNamespace

import pytest

duckdb = pytest.importorskip("duckdb")


def _seed_articles(conn, rows):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS news_articles ("
        "id VARCHAR, title VARCHAR, url VARCHAR, content VARCHAR, "
        "publish_date TIMESTAMP, source VARCHAR, category VARCHAR, "
        "sentiment_score DOUBLE, sentiment_label VARCHAR)"
    )
    if rows:
        conn.executemany(
            "INSERT INTO news_articles (id, title, url, content, publish_date, "
            "source, category) VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def _seed_claims(conn, rows):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS argument_claims ("
        "claim_id VARCHAR, claim_text VARCHAR, document_id VARCHAR, "
        "source_type VARCHAR, confidence DOUBLE, factcheck_verdict VARCHAR)"
    )
    if rows:
        conn.executemany(
            "INSERT INTO argument_claims (claim_id, claim_text, document_id, "
            "source_type, confidence, factcheck_verdict) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )


def _seed_outlet_scores(conn, rows):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outlet_scores ("
        "source VARCHAR, source_type VARCHAR, score_date VARCHAR, "
        "frame_diversity DOUBLE, attribution_rate DOUBLE, "
        "stance_neutrality DOUBLE, composite_score DOUBLE, "
        "doc_count INTEGER, claim_count INTEGER, computed_at VARCHAR)"
    )
    if rows:
        conn.executemany(
            "INSERT INTO outlet_scores (source, source_type, score_date, "
            "frame_diversity, attribution_rate, stance_neutrality, "
            "composite_score, doc_count, claim_count, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "wh.duckdb"
    c = duckdb.connect(str(db))
    yield c
    c.close()


@pytest.fixture
def seed(conn):
    return SimpleNamespace(
        conn=conn,
        articles=lambda rows: _seed_articles(conn, rows),
        claims=lambda rows: _seed_claims(conn, rows),
        outlet_scores=lambda rows: _seed_outlet_scores(conn, rows),
    )
