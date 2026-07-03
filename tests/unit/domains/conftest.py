"""In-memory warehouse builders for the research-pack tests."""

from types import SimpleNamespace

import pytest

duckdb = pytest.importorskip("duckdb")


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    yield c
    c.close()


def seed_documents(conn, rows):
    """rows: list of dicts (id/title/source_type/venue/concept/citations/refs/created_at)."""
    conn.execute(
        "CREATE TABLE documents ("
        "id VARCHAR, title VARCHAR, source_type VARCHAR, venue VARCHAR, "
        "concept VARCHAR, citations INTEGER, refs VARCHAR, created_at TIMESTAMP)"
    )
    if rows:
        conn.executemany(
            "INSERT INTO documents (id, title, source_type, venue, concept, citations, refs, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    r.get("id"),
                    r.get("title", ""),
                    r.get("source_type", "paper"),
                    r.get("venue"),
                    r.get("concept"),
                    r.get("citations", 0),
                    r.get("refs", ""),
                    r.get("created_at", "2025-06-01"),
                )
                for r in rows
            ],
        )


def seed_claims(conn, rows):
    """rows: list of dicts (claim_id/claim_text/document_id/source_type/attributed/verdict/confidence)."""
    conn.execute(
        "CREATE TABLE argument_claims ("
        "claim_id VARCHAR, claim_text VARCHAR, document_id VARCHAR, source_type VARCHAR, "
        "confidence DOUBLE, factcheck_verdict VARCHAR, attributed BOOLEAN)"
    )
    if rows:
        conn.executemany(
            "INSERT INTO argument_claims (claim_id, claim_text, document_id, source_type, "
            "confidence, factcheck_verdict, attributed) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    r.get("claim_id"),
                    r.get("claim_text", ""),
                    r.get("document_id"),
                    r.get("source_type", "paper"),
                    r.get("confidence", 0.5),
                    r.get("factcheck_verdict"),
                    r.get("attributed", False),
                )
                for r in rows
            ],
        )


@pytest.fixture
def seed():
    return SimpleNamespace(documents=seed_documents, claims=seed_claims)
