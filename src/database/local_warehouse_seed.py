"""
Schema + seed data for the local DuckDB analytics warehouse.

Seeds the ``news_articles`` table with realistic sample articles so the API's
news, feed and sentiment endpoints return real rows on a fresh local setup
(no Snowflake, no external ingestion required).

Seeding is idempotent: it only runs when the table is empty.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Tuple

import duckdb

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS document_frames (
    document_id   VARCHAR,
    source_type   VARCHAR,
    frame         VARCHAR,
    score         DOUBLE,
    classified_at VARCHAR,
    PRIMARY KEY (document_id, frame)
);

CREATE TABLE IF NOT EXISTS argument_claims (
    claim_id              VARCHAR PRIMARY KEY,
    claim_text            VARCHAR NOT NULL,
    document_id           VARCHAR NOT NULL,
    source_type           VARCHAR NOT NULL,
    confidence            DOUBLE,
    extracted_at          VARCHAR,
    factcheck_verdict     VARCHAR,
    factcheck_url         VARCHAR,
    factcheck_publisher   VARCHAR,
    factcheck_checked_at  VARCHAR
);

CREATE TABLE IF NOT EXISTS claim_evidence (
    evidence_id          VARCHAR PRIMARY KEY,
    claim_id             VARCHAR NOT NULL,
    evidence_text        VARCHAR,
    evidence_document_id VARCHAR NOT NULL,
    evidence_source_type VARCHAR NOT NULL,
    relation             VARCHAR NOT NULL,
    similarity_score     DOUBLE,
    found_at             VARCHAR
);

CREATE TABLE IF NOT EXISTS source_stances (
    source         VARCHAR NOT NULL,
    source_type    VARCHAR NOT NULL,
    topic          VARCHAR NOT NULL,
    stance         VARCHAR NOT NULL,
    confidence     DOUBLE,
    document_count INTEGER,
    window_start   VARCHAR,
    window_end     VARCHAR,
    computed_at    VARCHAR
);

CREATE TABLE IF NOT EXISTS stance_drift_events (
    source           VARCHAR NOT NULL,
    source_type      VARCHAR NOT NULL,
    topic            VARCHAR NOT NULL,
    from_stance      VARCHAR NOT NULL,
    to_stance        VARCHAR NOT NULL,
    confidence_delta DOUBLE,
    detected_at      VARCHAR,
    window_pair      VARCHAR
);

CREATE TABLE IF NOT EXISTS policy_positions (
    position_id    VARCHAR PRIMARY KEY,
    document_id    VARCHAR NOT NULL,
    source_type    VARCHAR NOT NULL,
    actor          VARCHAR NOT NULL,
    topic          VARCHAR NOT NULL,
    position_text  VARCHAR NOT NULL,
    position_date  VARCHAR,
    confidence     DOUBLE,
    extracted_at   VARCHAR
);

CREATE TABLE IF NOT EXISTS position_updates (
    update_id      VARCHAR PRIMARY KEY,
    position_id    VARCHAR NOT NULL,
    article_id     VARCHAR NOT NULL,
    update_type    VARCHAR NOT NULL,
    evidence_text  VARCHAR,
    confidence     DOUBLE,
    detected_at    VARCHAR
);

CREATE TABLE IF NOT EXISTS claim_conflicts (
    claim_id_a      VARCHAR NOT NULL,
    claim_id_b      VARCHAR NOT NULL,
    conflict_type   VARCHAR NOT NULL,
    similarity_score DOUBLE,
    source_type_a   VARCHAR,
    source_type_b   VARCHAR,
    topic           VARCHAR,
    computed_at     VARCHAR,
    PRIMARY KEY (claim_id_a, claim_id_b)
);

CREATE TABLE IF NOT EXISTS document_actors (
    document_id  VARCHAR NOT NULL,
    source_type  VARCHAR NOT NULL,
    actor_name   VARCHAR NOT NULL,
    entity_id    VARCHAR,
    role         VARCHAR NOT NULL,
    confidence   DOUBLE,
    extracted_at VARCHAR,
    PRIMARY KEY (document_id, actor_name, role)
);

CREATE TABLE IF NOT EXISTS outlet_clusters (
    source         VARCHAR NOT NULL,
    source_type    VARCHAR NOT NULL,
    cluster_id     INTEGER NOT NULL,
    cluster_label  VARCHAR NOT NULL,
    pca_x          DOUBLE,
    pca_y          DOUBLE,
    dominant_frame VARCHAR,
    doc_count      INTEGER,
    computed_at    VARCHAR,
    PRIMARY KEY (source, source_type)
);

CREATE TABLE IF NOT EXISTS outlet_scores (
    source            VARCHAR NOT NULL,
    source_type       VARCHAR NOT NULL,
    score_date        VARCHAR NOT NULL,
    frame_diversity   DOUBLE,
    attribution_rate  DOUBLE,
    stance_neutrality DOUBLE,
    composite_score   DOUBLE,
    doc_count         INTEGER,
    claim_count       INTEGER,
    computed_at       VARCHAR,
    PRIMARY KEY (source, source_type, score_date)
);

CREATE TABLE IF NOT EXISTS resource_metrics (
    id           VARCHAR PRIMARY KEY,
    sampled_at   TIMESTAMP NOT NULL,
    metric_name  VARCHAR NOT NULL,
    value        DOUBLE NOT NULL,
    unit         VARCHAR NOT NULL,
    pid          INTEGER,
    process_name VARCHAR
);

CREATE TABLE IF NOT EXISTS user_privacy_prefs (
    pref_key   VARCHAR PRIMARY KEY,
    pref_value VARCHAR NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS local_api_keys (
    key_id      VARCHAR PRIMARY KEY,
    key_hash    VARCHAR NOT NULL,
    key_prefix  VARCHAR NOT NULL,
    name        VARCHAR NOT NULL,
    role        VARCHAR NOT NULL DEFAULT 'viewer',
    status      VARCHAR NOT NULL DEFAULT 'active',
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at  TIMESTAMP,
    last_used_at TIMESTAMP,
    usage_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS admin_mfa_secrets (
    user_id    VARCHAR PRIMARY KEY,
    totp_secret VARCHAR NOT NULL,
    enabled    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# Each topic seeds a cluster of articles sharing a leading title word (the
# sentiment-topics endpoint groups by the first word of the title). The
# dominant sentiment is repeated enough times to clear the endpoint's
# "minimum articles per (topic, label)" threshold, giving each topic a clear
# positive / negative / neutral reading.
#
# (lead phrase, category, source, dominant label, dominant base score)
_TOPICS: List[Tuple[str, str, str, str, float]] = [
    ("Federal Reserve", "Economy", "Reuters", "neutral", 0.03),
    ("Markets", "Economy", "Bloomberg", "positive", 0.38),
    ("Inflation", "Economy", "Financial Times", "negative", -0.34),
    ("Nvidia", "Technology", "The Verge", "positive", 0.52),
    ("Quantum", "Technology", "Wired", "positive", 0.44),
    ("Energy", "Energy", "Reuters", "negative", -0.29),
    ("Climate", "Policy", "The Guardian", "negative", -0.41),
    ("Healthcare", "Health", "STAT News", "positive", 0.49),
]

# Short descriptive fragments appended after the lead phrase to vary titles.
_FRAGMENTS = [
    "outlook shifts as new data lands",
    "draws sharp reaction from analysts",
    "enters a pivotal week",
    "signals a turning point",
    "faces fresh scrutiny",
    "beats expectations in latest read",
    "weighs on the broader sector",
    "sets the tone for the quarter",
    "prompts a strategic rethink",
    "gains momentum heading into Q3",
]


def _label_for(index: int, dominant: str, dominant_count: int) -> str:
    """First `dominant_count` articles carry the dominant label; rest neutral."""
    return dominant if index < dominant_count else "neutral"


def _score_for(label: str, base: float, index: int) -> float:
    if label == "neutral":
        return round(-0.04 + (index % 3) * 0.04, 3)
    # Jitter around the base score so a topic isn't perfectly flat.
    jitter = ((index % 4) - 1.5) * 0.05
    return round(base + jitter, 3)


def _build_rows(now: datetime) -> List[tuple]:
    rows: List[tuple] = []
    per_topic = 8
    dominant_count = 6  # clears the default min-articles threshold of 5
    article_no = 0

    for t_idx, (lead, category, source, dominant, base) in enumerate(_TOPICS):
        for i in range(per_topic):
            label = _label_for(i, dominant, dominant_count)
            score = _score_for(label, base, i)
            fragment = _FRAGMENTS[(t_idx + i) % len(_FRAGMENTS)]
            title = f"{lead} {fragment}"
            # Spread timestamps across the last ~5 days (inside the 7-day
            # sentiment window), newest first.
            published = now - timedelta(hours=article_no * 2 + 1)
            slug = title.lower().replace(" ", "-")[:60]
            rows.append(
                (
                    f"art-{article_no:04d}",
                    title,
                    f"https://news.example/{slug}",
                    f"{title}. {fragment.capitalize()}.",
                    published,
                    source,
                    category,
                    score,
                    label,
                )
            )
            article_no += 1

    return rows


def _migrate_factcheck_columns(conn: duckdb.DuckDBPyConnection) -> None:
    """Add factcheck columns to argument_claims if they were added after initial creation."""
    for col, dtype in [
        ("factcheck_verdict",    "VARCHAR"),
        ("factcheck_url",        "VARCHAR"),
        ("factcheck_publisher",  "VARCHAR"),
        ("factcheck_checked_at", "VARCHAR"),
    ]:
        try:
            conn.execute(f"ALTER TABLE argument_claims ADD COLUMN {col} {dtype}")
        except Exception:
            pass  # column already exists


def _migrate_attribution_columns(conn: duckdb.DuckDBPyConnection) -> None:
    """Add attribution columns to argument_claims (#113)."""
    for col, dtype in [
        ("attributed",       "BOOLEAN"),
        ("attribution_text", "VARCHAR"),
    ]:
        try:
            conn.execute(f"ALTER TABLE argument_claims ADD COLUMN {col} {dtype}")
        except Exception:
            pass  # column already exists


#: prediction tables that carry model-vs-heuristic provenance (#958)
_PREDICTION_TABLES = (
    "argument_claims",
    "source_stances",
    "document_frames",
    "policy_positions",
    "claim_conflicts",
    "stance_drift_events",
)


def _migrate_prediction_mode_columns(conn: duckdb.DuckDBPyConnection) -> None:
    """Add prediction_mode (+ confidence where absent) to prediction tables.

    Pre-existing rows are backfilled as ``heuristic`` — everything written
    before #958 came from the heuristic fallbacks, and marking them keeps
    the evidence-quality summary honest instead of unknown.
    """
    for table in _PREDICTION_TABLES:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN prediction_mode VARCHAR")
        except Exception:
            pass  # column already exists
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN confidence DOUBLE")
        except Exception:
            pass  # column already exists (several tables ship with it)
        try:
            conn.execute(
                f"UPDATE {table} SET prediction_mode = 'heuristic'"
                " WHERE prediction_mode IS NULL"
            )
        except Exception:
            pass


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the analytics tables and the ``news_articles`` compatibility view.

    ``news_articles`` is now a view over the unified ``documents`` sink (#909);
    the analysis tables (frames, claims, stances, …) remain base tables.
    """
    conn.execute(_SCHEMA)
    _migrate_factcheck_columns(conn)
    _migrate_attribution_columns(conn)
    _migrate_prediction_mode_columns(conn)
    # Base tables (documents, document_enrichments) + the news_articles view.
    from src.database.news_articles_compat import ensure_news_articles_view
    ensure_news_articles_view(conn)


def seed_if_empty(conn: duckdb.DuckDBPyConnection) -> None:
    """Seed sample news documents only when the corpus is empty."""
    from src.database.news_articles_compat import (
        NEWS_ARTICLES_COLUMNS,
        write_news_articles,
    )

    count = conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]
    if count and count > 0:
        return

    rows = _build_rows(datetime.now())
    dict_rows = [dict(zip(NEWS_ARTICLES_COLUMNS, row)) for row in rows]
    write_news_articles(conn, dict_rows)
    logger.info("Seeded local warehouse with %d sample articles", len(rows))


def ensure_schema_and_seed(conn: duckdb.DuckDBPyConnection) -> None:
    """Create the news_articles table and seed it if empty."""
    ensure_schema(conn)
    seed_if_empty(conn)
