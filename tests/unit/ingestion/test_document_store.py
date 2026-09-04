"""Unit tests for the unified DocumentStore sink (#894, #893, #897).

Offline: an in-memory DuckDB connection is injected, so nothing touches disk or
the network. Covers idempotency, exact- and content-dedup, partial-batch
resilience (one invalid doc never aborts the batch), and summary bookkeeping.
"""

from __future__ import annotations

import duckdb
import pytest

from services.ingest.common.document_model import Document
from src.ingestion.document_store import DocumentStore, UpsertSummary

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def store() -> DocumentStore:
    """A DocumentStore over a fresh in-memory DuckDB, using the real validator."""
    return DocumentStore(duckdb.connect(":memory:"))


def _doc(
    doc_id: str,
    *,
    content: str = "Body text.",
    url: str | None = None,
    source_type: str = "news",
    language: str = "en",
) -> Document:
    return Document(
        document_id=doc_id,
        source_type=source_type,
        language=language,
        ingested_at=1_700_000_000_000,
        url=url,
        content=content,
    )


# --------------------------------------------------------------------------- #
# Schema / lifecycle
# --------------------------------------------------------------------------- #


def test_ensure_schema_is_idempotent():
    conn = duckdb.connect(":memory:")
    DocumentStore(conn)
    DocumentStore(conn)  # second construction must not raise on existing table
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_empty_batch_returns_zeroed_summary(store: DocumentStore):
    summary = store.upsert([])
    assert summary.as_dict() == {
        "received": 0,
        "inserted": 0,
        "duplicate": 0,
        "invalid": 0,
    }
    assert store.count() == 0


# --------------------------------------------------------------------------- #
# Insertion + idempotency
# --------------------------------------------------------------------------- #


def test_inserts_distinct_documents(store: DocumentStore):
    summary = store.upsert(
        [
            _doc("d1", content="First story.", url="https://ex.com/1"),
            _doc("d2", content="Second story.", url="https://ex.com/2"),
        ]
    )
    assert summary.inserted == 2
    assert summary.duplicate == 0
    assert store.count() == 2


def test_rerunning_same_batch_inserts_nothing(store: DocumentStore):
    batch = [
        _doc("d1", content="First story.", url="https://ex.com/1"),
        _doc("d2", content="Second story.", url="https://ex.com/2"),
    ]
    store.upsert(batch)
    second = store.upsert(batch)
    assert second.received == 2
    assert second.inserted == 0
    assert second.duplicate == 2
    assert store.count() == 2


def test_same_document_id_appends_revision_and_updates_projection(store: DocumentStore):
    store.upsert([_doc("d1", content="Original.", url="https://ex.com/1")])
    # Same identity and changed body becomes revision 1, never a second row.
    summary = store.upsert([_doc("d1", content="Rewritten.", url="https://ex.com/1")])
    assert summary.inserted == 0
    assert summary.updated == 1
    assert summary.duplicate == 0
    assert store.count() == 1
    assert store.get("d1")["content"] == "Rewritten."
    assert store.conn.execute(
        "SELECT COUNT(*) FROM document_revision_records WHERE document_id='d1'"
    ).fetchone() == (2,)


# --------------------------------------------------------------------------- #
# Content dedup (URL-independent)
# --------------------------------------------------------------------------- #


def test_syndicated_content_collapses_across_urls(store: DocumentStore):
    body = "Parliament approved the budget after a marathon debate."
    summary = store.upsert(
        [
            _doc("bbc-1", content=body, url="https://bbc.com/news/budget"),
            _doc("guardian-1", content=body, url="https://guardian.com/uk/budget"),
        ]
    )
    # Same content, different ids/URLs -> second is a content duplicate.
    assert summary.inserted == 1
    assert summary.duplicate == 1
    assert store.count() == 1


def test_content_dedup_ignores_whitespace_and_case(store: DocumentStore):
    store.upsert([_doc("d1", content="The Budget Passed.", url="https://ex.com/1")])
    summary = store.upsert(
        [_doc("d2", content="the   budget\npassed.", url="https://ex.com/2")]
    )
    assert summary.inserted == 0
    assert summary.duplicate == 1


def test_same_body_different_source_type_are_both_kept(store: DocumentStore):
    body = "Identical body across a news article and a blog post."
    summary = store.upsert(
        [
            _doc("news-1", content=body, url="https://ex.com/n", source_type="news"),
            _doc("blog-1", content=body, url="https://ex.com/b", source_type="blog"),
        ]
    )
    # Content hash keyed by (hash, source_type), so these do not collide.
    assert summary.inserted == 2
    assert store.count() == 2


def test_within_batch_content_duplicates_collapse(store: DocumentStore):
    body = "One story submitted twice in a single batch."
    summary = store.upsert(
        [
            _doc("a", content=body, url="https://ex.com/a"),
            _doc("b", content=body, url="https://ex.com/b"),
            _doc("c", content=body, url="https://ex.com/c"),
        ]
    )
    assert summary.received == 3
    assert summary.inserted == 1
    assert summary.duplicate == 2


# --------------------------------------------------------------------------- #
# Validation / dead-lettering (partial-batch resilience)
# --------------------------------------------------------------------------- #


def test_invalid_document_is_dead_lettered_and_batch_survives(store: DocumentStore):
    good = _doc("good-1", content="Valid body.", url="https://ex.com/good")
    # Bypass Document.__post_init__ (which would reject a bad source_type) by
    # feeding a raw payload with an out-of-contract source_type.
    bad_payload = good.to_dict()
    bad_payload["document_id"] = "bad-1"
    bad_payload["source_type"] = "not-a-real-type"

    summary = store.upsert([bad_payload, good])
    assert summary.received == 2
    assert summary.invalid == 1
    assert summary.inserted == 1
    assert len(summary.dead_letter) == 1
    assert summary.dead_letter[0]["document_id"] == "bad-1"
    assert store.count() == 1
    assert store.get("good-1") is not None
    assert store.get("bad-1") is None


def test_missing_required_field_is_dead_lettered(store: DocumentStore):
    payload = _doc("d1", url="https://ex.com/1").to_dict()
    del payload["language"]  # required by document-ingest-v1
    summary = store.upsert([payload])
    assert summary.invalid == 1
    assert summary.inserted == 0
    assert store.count() == 0


def test_validate_false_skips_validation(store: DocumentStore):
    # An injected validator that would fail must not be consulted when off.
    def _boom(_payload):
        raise AssertionError("validator should not run when validate=False")

    s = DocumentStore(duckdb.connect(":memory:"), validator=_boom)
    summary = s.upsert([_doc("d1", url="https://ex.com/1")], validate=False)
    assert summary.inserted == 1
    assert summary.invalid == 0


def test_injected_validator_is_used(store: DocumentStore):
    seen = []

    def _rejecting(payload):
        seen.append(payload["document_id"])
        raise ValueError("nope")

    s = DocumentStore(duckdb.connect(":memory:"), validator=_rejecting)
    summary = s.upsert([_doc("d1", url="https://ex.com/1")])
    assert seen == ["d1"]
    assert summary.invalid == 1
    assert summary.inserted == 0


# --------------------------------------------------------------------------- #
# Persistence details
# --------------------------------------------------------------------------- #


def test_stored_url_is_canonicalized(store: DocumentStore):
    store.upsert([_doc("d1", url="https://ex.com/story?utm_source=nl#top")])
    rec = store.get("d1")
    assert rec["canonical_url"] == "https://ex.com/story"
    assert rec["url"] == "https://ex.com/story?utm_source=nl#top"  # raw URL preserved


def test_get_roundtrips_authors_and_metadata():
    store = DocumentStore(duckdb.connect(":memory:"))
    doc = Document(
        document_id="d1",
        source_type="paper",
        language="en",
        ingested_at=1_700_000_000_000,
        authors=["Ada Lovelace", "Alan Turing"],
        metadata={"venue": "arXiv", "year": 2026},
        content="Abstract.",
    )
    store.upsert([doc])
    rec = store.get("d1")
    assert rec["authors"] == ["Ada Lovelace", "Alan Turing"]
    assert rec["metadata"] == {"venue": "arXiv", "year": 2026}


def test_get_missing_returns_none(store: DocumentStore):
    assert store.get("nope") is None


def test_accepts_dict_payloads_not_only_documents(store: DocumentStore):
    payload = _doc("d1", url="https://ex.com/1").to_dict()
    summary = store.upsert([payload])
    assert summary.inserted == 1
    assert store.get("d1") is not None


# --------------------------------------------------------------------------- #
# Summary bookkeeping
# --------------------------------------------------------------------------- #


def test_summary_counts_reconcile(store: DocumentStore):
    body = "Repeated body."
    good = _doc("g1", content="Unique.", url="https://ex.com/g")
    dup_a = _doc("da", content=body, url="https://ex.com/a")
    dup_b = _doc("db", content=body, url="https://ex.com/b")  # content dup of dup_a
    bad = good.to_dict()
    bad["document_id"] = "bad"
    bad["source_type"] = "bogus"

    summary = store.upsert([good, dup_a, dup_b, bad])
    d = summary.as_dict()
    # received == inserted + duplicate + invalid, always.
    assert d["received"] == d["inserted"] + d["duplicate"] + d["invalid"]
    assert d == {"received": 4, "inserted": 2, "duplicate": 1, "invalid": 1}


def test_upsert_summary_dataclass_defaults():
    s = UpsertSummary()
    assert s.as_dict() == {"received": 0, "inserted": 0, "duplicate": 0, "invalid": 0}
    assert s.dead_letter == []


# --------------------------------------------------------------------------- #
# list_documents / delete
# --------------------------------------------------------------------------- #


def test_list_documents_returns_all(store: DocumentStore):
    store.upsert(
        [
            _doc("d1", content="One", url="https://ex.com/1"),
            _doc("d2", content="Two", url="https://ex.com/2"),
        ]
    )
    docs = store.list_documents()
    assert {d["document_id"] for d in docs} == {"d1", "d2"}
    # Rows are fully hydrated (authors/metadata decoded).
    assert all(
        isinstance(d["authors"], list) and isinstance(d["metadata"], dict) for d in docs
    )


def test_list_documents_filters_by_source_type(store: DocumentStore):
    store.upsert(
        [
            _doc("n1", content="News", url="https://ex.com/n", source_type="news"),
            _doc("b1", content="Blog", url="https://ex.com/b", source_type="blog"),
        ]
    )
    blogs = store.list_documents(source_type="blog")
    assert [d["document_id"] for d in blogs] == ["b1"]


def test_list_documents_pages(store: DocumentStore):
    store.upsert(
        [
            _doc(f"d{i}", content=f"body {i}", url=f"https://ex.com/{i}")
            for i in range(5)
        ]
    )
    page = store.list_documents(limit=2, offset=0)
    assert len(page) == 2
    assert len(store.list_documents(limit=2, offset=4)) == 1


def test_delete_removes_and_reports_existence(store: DocumentStore):
    store.upsert([_doc("d1", url="https://ex.com/1")])
    assert store.delete("d1") is True
    assert store.get("d1") is None
    assert store.count() == 0
    # Deleting a missing doc reports False, does not raise.
    assert store.delete("nope") is False
