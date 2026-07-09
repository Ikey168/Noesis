"""Unit tests for the document summarization pass. Offline, in-memory DuckDB;
the extractive default needs no ML stack."""

from __future__ import annotations

import duckdb
import pytest

from services.ingest.common.document_model import Document
from src.database.news_articles_compat import ensure_corpus_documents_view
from src.ingestion.document_store import DocumentStore
from src.ingestion.summarize import (
    document_summary,
    extractive_summary,
    summarize_documents,
    summarize_topic,
)
from src.ingestion.summary_store import SummaryStore

_LEDE = "The central bank raised interest rates by half a point on Wednesday."
_BODY = (
    "Officials cited persistent inflation as the main driver of the decision. "
    "Markets had largely expected the move after recent economic data. "
    "The bank signalled further tightening could follow if prices stay high. "
    "Analysts debated whether the economy could avoid a recession."
)


def _doc(doc_id, content, category="Economy", source_type="news"):
    return Document(document_id=doc_id, source_type=source_type, language="en",
                    ingested_at=1_700_000_000_000, created_at=1_700_000_000_000,
                    url=f"https://ex.com/{doc_id}", title=f"Rates decision {doc_id}",
                    content=content, source_id="Wire",
                    metadata={"source": "Wire", "category": category})


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    DocumentStore(c).upsert([
        _doc("d1", f"{_LEDE} {_BODY}"),
        _doc("p1", "A paper on measured warming trends over the last decade. "
                   "It reports steady increases across all monitored regions.",
             category="Science", source_type="paper"),
    ])
    ensure_corpus_documents_view(c)   # the corpus view is part of the warehouse
    return c


# --- extractive summarizer -------------------------------------------------- #


def test_extractive_is_deterministic_and_bounded():
    a = extractive_summary(f"{_LEDE} {_BODY}", max_sentences=2)
    b = extractive_summary(f"{_LEDE} {_BODY}", max_sentences=2)
    assert a == b                                   # deterministic
    assert a.count(".") <= 2                         # at most 2 sentences
    assert _LEDE in a                                # lead sentence retained


def test_extractive_short_text_returned_whole():
    assert extractive_summary("One short sentence.") == "One short sentence."


# --- batch pass ------------------------------------------------------------- #


def test_summarizes_documents_and_persists(conn):
    n = summarize_documents(conn)
    assert n == 2
    store = SummaryStore(conn)
    assert store.count() == 2
    rec = store.get("d1")
    assert rec["method"] == "extractive"
    assert rec["summary"]


def test_is_idempotent_only_fills_gaps(conn):
    assert summarize_documents(conn) == 2
    assert summarize_documents(conn) == 0
    DocumentStore(conn).upsert([_doc("d3", "New coverage about the budget vote.")])
    assert summarize_documents(conn) == 1


def test_covers_non_news(conn):
    summarize_documents(conn)
    assert SummaryStore(conn).get("p1") is not None


# --- read-only accessors ---------------------------------------------------- #


def test_document_summary_prefers_stored(conn):
    summarize_documents(conn)
    out = document_summary(conn, "d1")
    assert out["stored"] is True and out["summary"]


def test_document_summary_computes_on_the_fly_when_absent(conn):
    # No batch run: document_summaries table does not exist yet.
    out = document_summary(conn, "d1")
    assert out["stored"] is False and out["method"] == "extractive"
    assert out["summary"]


def test_document_summary_unknown_is_flagged(conn):
    out = document_summary(conn, "ghost")
    assert out.get("code") == "not_found"


def test_summarize_topic_draws_on_topic_docs(conn):
    out = summarize_topic(conn, "Economy")
    assert out["document_count"] == 1
    assert "d1" in out["document_ids"]
    assert out["summary"]


def test_summarize_topic_empty(conn):
    out = summarize_topic(conn, "Sports")
    assert out["document_count"] == 0 and "note" in out
