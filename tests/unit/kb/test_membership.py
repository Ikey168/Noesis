"""Unit tests for the document→domain membership pass and corpus-view backing."""

import json
import time

import duckdb
import pytest

from src.ingestion.document_store import DocumentStore
from src.ingestion.embedding_store import EmbeddingStore
from src.kb import load_registry
from src.kb.membership import (
    ensure_domain_views,
    run_membership_pass,
    view_name,
)

CONFIG = """
version: 1
domains:
  - name: web3
    backing: corpus-view
    embedding_model: hashing-model
    tags: [web3]
    keywords: [defi, staking, stablecoin]
    embedding_anchors:
      - decentralized finance protocols and staking on chain
  - name: economics
    backing: corpus-view
    embedding_model: hashing-model
    tags: [economics]
    keywords: [inflation, "interest rates"]
  - name: reference
    backing: namespace
    embedding_model: hashing-model
"""


class FakeProvider:
    """Deterministic embedding provider keyed on token overlap."""

    VOCAB = [
        "decentralized", "finance", "protocols", "staking", "chain",
        "inflation", "rates", "cats", "gardening", "weather",
    ]

    def embed_texts(self, texts):
        vectors = []
        for text in texts:
            tokens = set(text.lower().replace(",", " ").split())
            vectors.append(
                [1.0 if word in tokens else 0.0 for word in self.VOCAB]
            )
        return vectors


def _doc(document_id, title, content, tags=None, source_id="feed", ingested_at=None):
    return {
        "document_id": document_id,
        "source_type": "blog",
        "language": "en",
        "ingested_at": ingested_at or int(time.time() * 1000),
        "source_id": source_id,
        "url": f"https://example.com/{document_id}",
        "title": title,
        "content": content,
        "metadata": {"tags": tags or []},
    }


@pytest.fixture()
def conn():
    connection = duckdb.connect()
    DocumentStore(connection)
    EmbeddingStore(connection)
    return connection


@pytest.fixture()
def registry(tmp_path):
    path = tmp_path / "domains.yml"
    path.write_text(CONFIG)
    return load_registry(path)


def _seed(conn, docs):
    DocumentStore(conn).upsert(docs)


def _assignments(conn, domain):
    return {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT document_id, method, score FROM document_domains WHERE domain = ?",
            [domain],
        ).fetchall()
    }


class TestAssignment:
    def test_by_source_tag(self, conn, registry):
        _seed(conn, [_doc("d1", "Governance vote", "A protocol vote.", tags=["web3"])])
        run_membership_pass(conn, registry)
        assignments = _assignments(conn, "web3")
        assert assignments["d1"][0] == "source"
        assert assignments["d1"][1] == 1.0

    def test_by_keywords_needs_two_hits_at_default_threshold(self, conn, registry):
        _seed(
            conn,
            [
                _doc("one-hit", "Note", "Only defi is mentioned here."),
                _doc("two-hits", "Defi report", "Both defi and staking feature."),
            ],
        )
        run_membership_pass(conn, registry)
        assignments = _assignments(conn, "web3")
        assert "one-hit" not in assignments
        assert assignments["two-hits"][0] == "keyword"

    def test_keyword_matching_respects_word_boundaries(self, conn, registry):
        _seed(
            conn,
            [_doc("bound", "Ratesx inflationary", "ratesx and inflationary only.")],
        )
        run_membership_pass(conn, registry)
        assert "bound" not in _assignments(conn, "economics")

    def test_by_embedding_similarity(self, conn, registry):
        _seed(conn, [_doc("emb", "Chain analysis", "On chain staking data.")])
        EmbeddingStore(conn).upsert(
            "emb",
            model="hashing-model",
            vector=FakeProvider().embed_texts(
                ["decentralized finance staking chain"]
            )[0],
        )
        run_membership_pass(conn, registry, provider=FakeProvider())
        assignments = _assignments(conn, "web3")
        assert assignments["emb"][0] == "embedding"
        assert assignments["emb"][1] >= 0.35

    def test_embedding_skipped_on_model_mismatch(self, conn, registry):
        _seed(conn, [_doc("mismatch", "Chain analysis", "On chain staking data.")])
        EmbeddingStore(conn).upsert(
            "mismatch",
            model="other-model",
            vector=FakeProvider().embed_texts(["decentralized finance staking"])[0],
        )
        run_membership_pass(conn, registry, provider=FakeProvider())
        assert "mismatch" not in _assignments(conn, "web3")

    def test_overlapping_membership(self, conn, registry):
        _seed(
            conn,
            [
                _doc(
                    "both",
                    "Stablecoin rules",
                    "Regulators weigh stablecoin rules as inflation and"
                    " interest rates shift.",
                    tags=["web3"],
                )
            ],
        )
        run_membership_pass(conn, registry)
        assert "both" in _assignments(conn, "web3")
        assert "both" in _assignments(conn, "economics")


class TestIncrementalRuns:
    def test_idempotent_and_incremental(self, conn, registry):
        _seed(conn, [_doc("d1", "Defi staking", "defi staking news.", ingested_at=1000)])
        first = run_membership_pass(conn, registry)
        assert first["domains"]["web3"]["scanned"] == 1

        second = run_membership_pass(conn, registry)
        assert second["domains"]["web3"]["scanned"] == 0
        assert len(_assignments(conn, "web3")) == 1

        _seed(conn, [_doc("d2", "More defi staking", "defi staking again.", ingested_at=2000)])
        third = run_membership_pass(conn, registry)
        assert third["domains"]["web3"]["scanned"] == 1
        assert set(_assignments(conn, "web3")) == {"d1", "d2"}

    def test_out_of_order_arrival_is_not_skipped(self, conn, registry):
        # A document committed later with an *older* payload timestamp must
        # still be assessed — incrementality is set-based, not clock-based.
        _seed(conn, [_doc("late-clock", "Defi staking", "defi staking now.", ingested_at=5000)])
        run_membership_pass(conn, registry)
        _seed(conn, [_doc("early-clock", "Old defi staking", "defi staking then.", ingested_at=100)])
        run_membership_pass(conn, registry)
        assert set(_assignments(conn, "web3")) == {"late-clock", "early-clock"}

    def test_zero_ingested_at_documents_are_scanned(self, conn, registry):
        # Legacy compat writers (seed, migration) store ingested_at = 0.
        _seed(conn, [_doc("legacy", "Defi staking", "defi staking legacy.", ingested_at=None)])
        conn.execute("UPDATE documents SET ingested_at = 0 WHERE document_id = 'legacy'")
        run_membership_pass(conn, registry)
        assert "legacy" in _assignments(conn, "web3")

    def test_late_arriving_embedding_is_reassessed(self, conn, registry):
        _seed(conn, [_doc("late-vec", "Chain analysis", "on chain data only.")])
        run_membership_pass(conn, registry, provider=FakeProvider())
        assert "late-vec" not in _assignments(conn, "web3")

        EmbeddingStore(conn).upsert(
            "late-vec",
            model="hashing-model",
            vector=FakeProvider().embed_texts(
                ["decentralized finance staking chain"]
            )[0],
        )
        summary = run_membership_pass(conn, registry, provider=FakeProvider())
        assert summary["domains"]["web3"]["scanned"] >= 1
        assert _assignments(conn, "web3")["late-vec"][0] == "embedding"

    def test_malformed_vector_row_does_not_wedge_the_pass(self, conn, registry):
        _seed(
            conn,
            [
                _doc("corrupt", "Chain analysis", "on chain data only."),
                _doc("healthy", "Defi staking", "defi staking news."),
            ],
        )
        EmbeddingStore(conn).upsert("corrupt", model="hashing-model", vector=[0.1])
        conn.execute(
            "UPDATE document_embeddings SET vector = 'not-json'"
            " WHERE document_id = 'corrupt'"
        )
        summary = run_membership_pass(conn, registry, provider=FakeProvider())
        assert summary["domains"]["web3"]["scanned"] == 2
        assert "healthy" in _assignments(conn, "web3")
        # The corrupt row stays pending, ready for reassessment once repaired.
        pending = conn.execute(
            "SELECT embedding_pending FROM kb_membership_scans"
            " WHERE document_id = 'corrupt' AND domain = 'web3'"
        ).fetchone()[0]
        assert pending is True

    def test_config_change_rebuilds_domain(self, conn, registry, tmp_path):
        _seed(conn, [_doc("d1", "Defi staking", "defi staking news.")])
        run_membership_pass(conn, registry)
        assert "d1" in _assignments(conn, "web3")

        changed = CONFIG.replace("[defi, staking, stablecoin]", "[gardening, weather]")
        path = tmp_path / "changed.yml"
        path.write_text(changed)
        run_membership_pass(conn, load_registry(path))
        assert "d1" not in _assignments(conn, "web3")

    def test_failed_rebuild_keeps_previous_assignments(self, conn, registry, tmp_path):
        class ExplodingProvider:
            def embed_texts(self, texts):
                raise RuntimeError("provider outage")

        _seed(conn, [_doc("d1", "Defi staking", "defi staking news.")])
        run_membership_pass(conn, registry)
        assert "d1" in _assignments(conn, "web3")

        changed = CONFIG.replace("[defi, staking, stablecoin]", "[defi, staking]")
        path = tmp_path / "changed.yml"
        path.write_text(changed)
        with pytest.raises(RuntimeError, match="outage"):
            run_membership_pass(conn, load_registry(path), provider=ExplodingProvider())
        # The failed rebuild must not have emptied the domain.
        assert "d1" in _assignments(conn, "web3")

        # A later healthy pass self-heals onto the new definition.
        run_membership_pass(conn, load_registry(path))
        assert "d1" in _assignments(conn, "web3")


class TestViewsAndBacking:
    def test_views_contain_members_only(self, conn, registry):
        _seed(
            conn,
            [
                _doc("member", "Defi staking", "defi staking news.", tags=["web3"]),
                _doc("outsider", "Gardening", "tomatoes and weather."),
            ],
        )
        run_membership_pass(conn, registry)
        ensure_domain_views(conn, registry)
        rows = conn.execute(
            f"SELECT document_id FROM {view_name('web3')}"
        ).fetchall()
        assert [row[0] for row in rows] == ["member"]

    def test_backing_documents_search_coverage(self, conn, registry):
        _seed(
            conn,
            [
                _doc("a", "Stablecoin rules", "stablecoin and defi.", tags=["web3"]),
                _doc("b", "Staking yield", "defi staking yields.", tags=["web3"]),
            ],
        )
        run_membership_pass(conn, registry)
        backing = registry.resolve("web3", conn=conn)

        documents = backing.documents(limit=10)
        assert {doc["document_id"] for doc in documents} == {"a", "b"}
        assert documents[0]["domain_method"] == "source"

        hits = backing.search("stablecoin")
        assert [hit["document_id"] for hit in hits] == ["a"]

        coverage = backing.coverage()
        assert coverage["ready"] is True
        assert coverage["documents"] == 2
        assert coverage["assignment_methods"] == {"source": 2}
        assert coverage["sources"] == ["feed"]

    def test_backing_since_filters_on_ingestion_time(self, conn, registry):
        old_ms = 1_600_000_000_000
        new_ms = 1_900_000_000_000
        _seed(
            conn,
            [
                _doc("old", "Old defi staking", "defi staking, the early days.", tags=["web3"], ingested_at=old_ms),
                _doc("new", "New defi staking", "defi staking, the sequel.", tags=["web3"], ingested_at=new_ms),
            ],
        )
        # A backfilled document published long ago but ingested *now* is new
        # domain content — publication date must not hide it from `since`.
        conn.execute(
            "UPDATE documents SET created_at = 1_000_000_000_000"
            " WHERE document_id = 'new'"
        )
        run_membership_pass(conn, registry)
        backing = registry.resolve("web3", conn=conn)
        recent = backing.documents(since="2025-01-01")
        assert [doc["document_id"] for doc in recent] == ["new"]

        # UTC offsets are honoured, not silently dropped: new_ms is
        # 2030-03-17T17:46:40Z; a since 1s later in a +05:00 offset that is
        # still earlier in UTC must include the doc, 1s after in UTC must not.
        assert backing.documents(since="2030-03-17T22:46:39+05:00") != []
        assert backing.documents(since="2030-03-17T17:46:41+00:00") == []

    def test_search_escapes_like_wildcards(self, conn, registry):
        _seed(
            conn,
            [
                _doc("pct", "Markets", "stocks saw a 50% gain today, defi staking up.", tags=["web3"]),
                _doc("nopct", "Markets again", "a 50 point gain today, defi staking up.", tags=["web3"]),
            ],
        )
        run_membership_pass(conn, registry)
        backing = registry.resolve("web3", conn=conn)

        hits = backing.search("50% gain")
        assert [hit["document_id"] for hit in hits] == ["pct"]
        # Wildcards are literals now: "%" finds the doc containing a literal
        # percent sign; "_" (present nowhere) must not match everything.
        assert [hit["document_id"] for hit in backing.search("%")] == ["pct"]
        assert backing.search("_") == []
