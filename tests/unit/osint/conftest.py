"""Fixtures for the OSINT composition tests: a temp DuckDB warehouse seeded
with the layers the tools compose (news_articles, argument_claims,
claim_evidence, claim_conflicts, outlet_scores)."""

from types import SimpleNamespace

import pytest

duckdb = pytest.importorskip("duckdb")


def _articles(conn, rows):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS news_articles ("
        "id VARCHAR, title VARCHAR, url VARCHAR, content VARCHAR, "
        "publish_date TIMESTAMP, source VARCHAR, category VARCHAR, "
        "sentiment_score DOUBLE, sentiment_label VARCHAR)"
    )
    if rows:
        conn.executemany(
            "INSERT INTO news_articles (id, title, url, source, publish_date) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )


def _claims(conn, rows):
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


def _evidence(conn, rows):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS claim_evidence ("
        "evidence_id VARCHAR, claim_id VARCHAR, evidence_text VARCHAR, "
        "evidence_document_id VARCHAR, evidence_source_type VARCHAR, "
        "relation VARCHAR, similarity_score DOUBLE, found_at VARCHAR)"
    )
    if rows:
        conn.executemany(
            "INSERT INTO claim_evidence (evidence_id, claim_id, evidence_document_id, "
            "evidence_source_type, relation, similarity_score) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )


def _conflicts(conn, rows):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS claim_conflicts ("
        "claim_id_a VARCHAR, claim_id_b VARCHAR, conflict_type VARCHAR, "
        "similarity_score DOUBLE, source_type_a VARCHAR, source_type_b VARCHAR, "
        "topic VARCHAR, computed_at VARCHAR)"
    )
    if rows:
        conn.executemany(
            "INSERT INTO claim_conflicts (claim_id_a, claim_id_b, conflict_type, "
            "similarity_score, topic) VALUES (?, ?, ?, ?, ?)",
            rows,
        )


def _outlet_scores(conn, rows):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS outlet_scores ("
        "source VARCHAR, source_type VARCHAR, score_date VARCHAR, "
        "frame_diversity DOUBLE, attribution_rate DOUBLE, stance_neutrality DOUBLE, "
        "composite_score DOUBLE, doc_count INTEGER, claim_count INTEGER, computed_at VARCHAR)"
    )
    if rows:
        conn.executemany(
            "INSERT INTO outlet_scores (source, source_type, score_date, "
            "frame_diversity, attribution_rate, stance_neutrality, composite_score) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


def _actors(conn, rows):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS document_actors ("
        "document_id VARCHAR, source_type VARCHAR, actor_name VARCHAR, "
        "entity_id VARCHAR, role VARCHAR, confidence DOUBLE, extracted_at VARCHAR)"
    )
    if rows:
        conn.executemany(
            "INSERT INTO document_actors (document_id, actor_name, entity_id, role) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )


@pytest.fixture
def conn(tmp_path):
    c = duckdb.connect(str(tmp_path / "wh.duckdb"))
    yield c
    c.close()


@pytest.fixture
def seed(conn):
    return SimpleNamespace(
        conn=conn,
        articles=lambda rows: _articles(conn, rows),
        claims=lambda rows: _claims(conn, rows),
        evidence=lambda rows: _evidence(conn, rows),
        conflicts=lambda rows: _conflicts(conn, rows),
        outlet_scores=lambda rows: _outlet_scores(conn, rows),
        actors=lambda rows: _actors(conn, rows),
    )
