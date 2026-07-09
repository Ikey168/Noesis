"""Unit tests for news_articles as a compatibility view over documents (#909).

Offline, in-memory DuckDB. Covers the view projection, the inverse writer, and
migrating a legacy news_articles base table into the view.
"""

from __future__ import annotations

import duckdb
import pytest

from src.database.news_articles_compat import (
    ensure_news_articles_view,
    migrate_news_articles_to_view,
    write_news_articles,
)


@pytest.fixture
def conn():
    return duckdb.connect(":memory:")


def _is_view(conn) -> bool:
    row = conn.execute(
        "SELECT table_type FROM information_schema.tables WHERE table_name = 'news_articles'"
    ).fetchone()
    return bool(row) and str(row[0]).upper() == "VIEW"


# --------------------------------------------------------------------------- #
# View + writer
# --------------------------------------------------------------------------- #


def test_ensure_view_creates_a_view_and_is_idempotent(conn):
    ensure_news_articles_view(conn)
    ensure_news_articles_view(conn)  # second call is a no-op
    assert _is_view(conn)
    assert conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0] == 0


def test_write_maps_legacy_columns_through_the_view(conn):
    ensure_news_articles_view(conn)
    written = write_news_articles(conn, [{
        "id": "a1", "title": "Fed holds", "url": "http://x/1", "content": "Body.",
        "publish_date": "2026-05-20", "source": "Reuters", "category": "Economy",
        "sentiment_score": 0.3, "sentiment_label": "positive",
    }])
    assert written == 1
    row = conn.execute(
        "SELECT id, title, url, content, publish_date, source, category, "
        "sentiment_score, sentiment_label FROM news_articles"
    ).fetchone()
    assert row[0] == "a1"
    assert row[1] == "Fed holds"
    assert str(row[4]).startswith("2026-05-20")  # publish_date round-trips
    assert row[5] == "Reuters"
    assert row[6] == "Economy"
    assert row[7] == 0.3
    assert row[8] == "positive"


def test_write_handles_partial_rows(conn):
    ensure_news_articles_view(conn)
    write_news_articles(conn, [{"id": "a1", "title": "Just a title", "source": "BBC"}])
    row = conn.execute(
        "SELECT id, title, source, category, publish_date, sentiment_score FROM news_articles"
    ).fetchone()
    assert row == ("a1", "Just a title", "BBC", None, None, None)


def test_write_is_idempotent_by_id(conn):
    ensure_news_articles_view(conn)
    write_news_articles(conn, [{"id": "a1", "title": "First", "source": "s"}])
    write_news_articles(conn, [{"id": "a1", "title": "Second", "source": "s"}])
    assert conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0] == 1


def test_view_only_exposes_news_source_type(conn):
    ensure_news_articles_view(conn)
    write_news_articles(conn, [{"id": "n1", "title": "News", "source": "s"}])
    # A non-news document must not surface through the news_articles view.
    conn.execute(
        "INSERT INTO documents (document_id, source_type, language, ingested_at, title) "
        "VALUES ('p1', 'paper', 'en', 0, 'A paper')"
    )
    ids = {r[0] for r in conn.execute("SELECT id FROM news_articles").fetchall()}
    assert ids == {"n1"}
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2


def test_analytics_style_aggregation_works_over_the_view(conn):
    ensure_news_articles_view(conn)
    write_news_articles(conn, [
        {"id": "a1", "title": "T", "source": "Reuters", "category": "Economy",
         "publish_date": "2026-05-20", "sentiment_score": 0.2},
        {"id": "a2", "title": "T", "source": "Reuters", "category": "Economy",
         "publish_date": "2026-05-21", "sentiment_score": 0.4},
    ])
    row = conn.execute(
        "SELECT source, DATE_TRUNC('month', publish_date), AVG(sentiment_score) "
        "FROM news_articles GROUP BY 1, 2"
    ).fetchone()
    assert row[0] == "Reuters"
    assert abs(row[2] - 0.3) < 1e-9


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #


def test_migrate_legacy_table_to_view(conn):
    # A legacy warehouse with news_articles as a base table.
    conn.execute(
        "CREATE TABLE news_articles (id VARCHAR, title VARCHAR, url VARCHAR, "
        "content VARCHAR, publish_date TIMESTAMP, source VARCHAR, category VARCHAR, "
        "sentiment_score DOUBLE, sentiment_label VARCHAR)"
    )
    conn.execute(
        "INSERT INTO news_articles VALUES "
        "('art-1', 'Legacy', 'http://x', 'Body', TIMESTAMP '2026-05-20', 'Reuters', "
        "'Economy', 0.5, 'positive')"
    )
    migrated = migrate_news_articles_to_view(conn)
    assert migrated == 1
    assert _is_view(conn)
    row = conn.execute(
        "SELECT id, source, sentiment_label FROM news_articles"
    ).fetchone()
    assert row == ("art-1", "Reuters", "positive")
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


def test_migrate_is_idempotent(conn):
    ensure_news_articles_view(conn)  # already a view
    assert migrate_news_articles_to_view(conn) == 0
    assert _is_view(conn)
