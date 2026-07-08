"""Fixtures for the investigation engine tests: a temp DuckDB warehouse seeded
with the corpus layers the engine's leads compose (news_articles,
argument_claims, claim_evidence, claim_conflicts, outlet_scores,
document_actors), same shapes as the OSINT test fixtures."""

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


@pytest.fixture
def corpus(seed):
    """A small corpus where 'Severe flooding struck the delta region' is
    corroborated by two independent sources, contradicted by one, and the
    counter-claim ('flooding was minor') sits on a conflict edge."""
    seed.articles(
        [
            ("d1", "Delta flooding", "http://a/1", "Alpha Wire", "2026-06-01"),
            ("d2", "Support doc", "http://b/1", "Beta Journal", "2026-06-02"),
            ("d3", "Support two", "http://c/1", "Gamma Review", "2026-06-03"),
            ("d4", "Counter doc", "http://d/1", "Delta Post", "2026-06-04"),
        ]
    )
    seed.claims(
        [
            ("k1", "Severe flooding struck the delta region.", "d1", "news", 0.9, None),
            ("k2", "Flooding in the delta region was minor.", "d4", "news", 0.8, None),
        ]
    )
    seed.evidence(
        [
            ("e1", "k1", "d2", "news", "supports", 0.9),
            ("e2", "k1", "d3", "news", "supports", 0.8),
        ]
    )
    seed.conflicts([("k1", "k2", "contradicts", 0.8, "flooding")])
    seed.outlet_scores(
        [
            ("Alpha Wire", "news", "2026-06-01", 0.7, 0.8, 0.7, 0.78),
            ("Beta Journal", "news", "2026-06-01", 0.8, 0.85, 0.75, 0.81),
            ("Gamma Review", "news", "2026-06-01", 0.6, 0.6, 0.6, 0.66),
            ("Delta Post", "news", "2026-06-01", 0.4, 0.4, 0.5, 0.42),
        ]
    )
    return seed
