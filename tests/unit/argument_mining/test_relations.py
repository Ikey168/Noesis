"""Unit tests for relation extraction. Offline; the regex NER fallback needs no
ML stack (spaCy improves recall when installed but is not required)."""

from __future__ import annotations

import duckdb
import pytest

from services.ingest.common.document_model import Document
from src.argument_mining.relations import (
    document_relations,
    entity_relations,
    extract_document_relations,
    extract_relations,
)
from src.database.news_articles_compat import ensure_corpus_documents_view
from src.ingestion.document_store import DocumentStore


def _doc(doc_id, content, source_type="news"):
    return Document(document_id=doc_id, source_type=source_type, language="en",
                    ingested_at=1_700_000_000_000, created_at=1_700_000_000_000,
                    url=f"https://ex.com/{doc_id}", title="Politics", content=content,
                    source_id="Wire", metadata={"source": "Wire", "category": "Politics"})


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    DocumentStore(c).upsert([
        _doc("d1", "Angela Merkel met Emmanuel Macron in Berlin on Tuesday. "
                   "Later, Joe Biden praised Angela Merkel for the agreement."),
        _doc("d2", "The weather was mild and nothing of note occurred today."),
    ])
    ensure_corpus_documents_view(c)
    return c


# --- extractor -------------------------------------------------------------- #


def test_extracts_subject_verb_object():
    rels = extract_relations("d0", "news", "Angela Merkel met Emmanuel Macron in Paris.")
    assert any(r.subject == "Angela Merkel" and r.relation == "met"
               and r.object == "Emmanuel Macron" for r in rels)


def test_no_relation_without_a_relation_verb():
    rels = extract_relations("d0", "news", "Angela Merkel and Emmanuel Macron were present.")
    assert rels == []


def test_relation_ids_are_stable_entity_ids():
    from src.argument_mining.metadata import _entity_id

    rels = extract_relations("d0", "news", "Joe Biden praised Angela Merkel.")
    assert rels
    r = rels[0]
    assert r.subject_id == _entity_id(r.subject)
    assert r.object_id == _entity_id(r.object)


# --- batch + persistence ---------------------------------------------------- #


def test_batch_extracts_and_persists(conn):
    result = extract_document_relations(conn)
    assert result["documents_processed"] == 2
    assert result["relations_found"] >= 2       # d1 has met + praised
    got = document_relations(conn, "d1")
    verbs = {r["relation"] for r in got["relations"]}
    assert {"met", "praised"} <= verbs


def test_batch_is_idempotent(conn):
    assert extract_document_relations(conn)["documents_processed"] == 2
    # Re-running processes nothing (both documents already in the table).
    assert extract_document_relations(conn)["documents_processed"] == 0


def test_entity_relations_by_name(conn):
    extract_document_relations(conn)
    out = entity_relations(conn, "Angela Merkel")
    assert out["count"] >= 2   # subject of "met", object of "praised"
    assert all("Angela Merkel" in (r["subject"], r["object"]) for r in out["relations"])


def test_read_only_accessors_before_extraction(conn):
    # No batch run yet -> document_relations table may be absent -> graceful note.
    out = document_relations(conn, "d1")
    assert out["count"] == 0 and "note" in out
    out2 = entity_relations(conn, "Angela Merkel")
    assert out2["count"] == 0 and "note" in out2
