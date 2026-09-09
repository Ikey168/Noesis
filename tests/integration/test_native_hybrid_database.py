"""Opt-in real pgvector checks; every test database is ephemeral and loopback-only."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("NOESIS_TEST_PGVECTOR") != "1",
    reason="requires explicit local Docker/pgvector execution",
)


def test_real_migrations_vector_parameters_filters_and_lexical_search():
    import psycopg2

    from scripts.evaluate_hybrid_retrieval import database
    from services.rag.lexical import LexicalSearchService, SearchFilters
    from services.rag.vector import VectorSearchFilters, VectorSearchService

    with database() as (connection, _):
        db = psycopg2.connect(**connection)
        try:
            with db.cursor() as cur:
                # Reapplying both migration copies must preserve the schema.
                root = Path(__file__).resolve().parents[2]
                for filename in (
                    "0001_init_pgvector.sql",
                    "0002_schema_chunks.sql",
                    "0003_add_indexer_columns.sql",
                    "0004_fts.sql",
                ):
                    assert (root / "migrations/pg" / filename).read_bytes() == (
                        root / "db/migrations/pg" / filename
                    ).read_bytes()
                    cur.execute((root / "migrations/pg" / filename).read_text())
                ids = []
                for i, source in enumerate(("de", "en")):
                    cur.execute(
                        "INSERT INTO documents(article_id,title,content,source,url,published_at) VALUES(%s,'','science evidence',%s,'',%s) RETURNING id",
                        (f"document-{i}", source, datetime(2026, i + 1, 1, tzinfo=UTC)),
                    )
                    did = cur.fetchone()[0]
                    cur.execute(
                        "INSERT INTO chunks(document_id,chunk_index,content,word_count,char_count,doc_id,chunk_id,title,source) VALUES(%s,0,'science evidence',2,16,%s,%s,'',%s) RETURNING id",
                        (did, f"document-{i}", f"chunk-{i}", source),
                    )
                    cid = cur.fetchone()[0]
                    ids.append(str(cid))
                    cur.execute(
                        "INSERT INTO embeddings(chunk_id,embedding) VALUES(%s,%s::vector)",
                        (cid, [1.0, float(i)] + [0.0] * 382),
                    )
            db.commit()
            with (
                VectorSearchService(connection) as vector,
                LexicalSearchService(connection) as lexical,
            ):
                query = [1.0] + [0.0] * 383
                rows = vector.search(query, 1)
                assert rows[0].id == ids[0]
                assert rows[0].doc_id == "document-0"
                filtered = vector.search(
                    query,
                    10,
                    VectorSearchFilters(
                        source="en",
                        date_from=datetime(2026, 2, 1, tzinfo=UTC),
                        date_to=datetime(2026, 2, 2, tzinfo=UTC),
                        min_similarity=0.5,
                    ),
                )
                assert [r.id for r in filtered] == [ids[1]]
                assert (
                    vector.search(query, 10, VectorSearchFilters(source="' OR true --"))
                    == []
                )
                words = lexical.search("science", 30, SearchFilters(source="de"))
                assert [r.id for r in words] == [ids[0]]
                assert lexical.search("zzzmissingword", 30) == []
        finally:
            db.close()
