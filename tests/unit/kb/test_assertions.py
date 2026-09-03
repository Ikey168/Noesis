import duckdb
import pytest

from src.kb.assertions import compare_assertions, record_assertions


def test_versioned_assertions_detect_only_newer_public_differences():
    conn = duckdb.connect(":memory:")
    record_assertions(
        conn,
        "rule-1",
        {"threshold": 10, "start": "2028-01"},
        effective_at_ms=1,
        document_id="public-v1",
    )
    record_assertions(
        conn,
        "rule-1",
        {"threshold": 10, "start": "2028-01"},
        effective_at_ms=2,
        document_id="memo",
        visibility="private",
    )
    assert compare_assertions(conn, "rule-1")["stale"] is False

    record_assertions(
        conn,
        "rule-1",
        {"threshold": 15, "start": "2027-01"},
        effective_at_ms=3,
        document_id="public-v2",
    )
    comparison = compare_assertions(conn, "rule-1")
    assert comparison["stale"] is True
    assert comparison["n"] == 2
    assert {item["assertion_key"] for item in comparison["differences"]} == {
        "start",
        "threshold",
    }


def test_versioned_assertions_are_idempotent_and_validate_visibility():
    conn = duckdb.connect(":memory:")
    kwargs = {
        "effective_at_ms": 1,
        "document_id": "document-1",
    }
    assert record_assertions(conn, "subject", {"value": 1}, **kwargs) == 1
    assert record_assertions(conn, "subject", {"value": 1}, **kwargs) == 1
    assert conn.execute("SELECT COUNT(*) FROM versioned_assertions").fetchone()[0] == 1
    with pytest.raises(ValueError, match="visibility"):
        record_assertions(
            conn,
            "subject",
            {"value": 1},
            visibility="secret",
            **kwargs,
        )
