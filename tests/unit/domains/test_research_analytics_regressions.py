"""Research analytics edge cases observed in the MCP venue panel."""

import duckdb

from src.analytics.honesty import is_interval
from src.domains.research.analytics import _entropy, venue_credibility


def test_single_topic_diversity_is_zero():
    assert _entropy([1]) == 0.0
    assert _entropy([2, 0]) == 0.0
    assert _entropy([1, 1]) == 1.0


def test_one_uncited_paper_has_valid_venue_interval():
    conn = duckdb.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE documents (document_id VARCHAR, source_type VARCHAR, "
            "title VARCHAR, metadata VARCHAR)"
        )
        conn.execute(
            "INSERT INTO documents VALUES ('p1', 'paper', 'Example', "
            "'{\"venue\":\"Example venue\",\"primary_category\":\"audio\"}')"
        )
        result = venue_credibility(conn)
        assert result["n"] == 1
        venue = result["venues"][0]
        assert venue["components"]["concept_diversity"] == 0.0
        assert is_interval(venue["credibility"])
    finally:
        conn.close()
