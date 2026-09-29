"""Research analytics edge cases observed in the MCP venue panel."""

import duckdb

from src.analytics.honesty import validate_analytic_output
from src.domains.research.analytics import _entropy, venue_credibility


def test_single_topic_diversity_is_zero():
    assert _entropy([1]) == 0.0
    assert _entropy([2, 0]) == 0.0
    assert _entropy([1, 1]) == 1.0


def test_one_uncited_paper_reports_insufficient_venue_data():
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
        # Missing citation counts and claim attribution are not zeros: the
        # venue is reported as unscored rather than given a credibility.
        assert venue["status"] == "insufficient_data"
        assert venue["missing"] == ["citation_counts", "claim_attribution"]
        assert "credibility" not in venue
        assert validate_analytic_output(result) == []
    finally:
        conn.close()
