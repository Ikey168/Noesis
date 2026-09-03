"""
Tests for the knowledge-graph live-update service (Issue #42).

Covers:
- Heuristic NER entity extraction
- KG store population from document dicts
- Emerging-connections query
- Evolving-topics query
- Stats endpoint
- Thread-safety: multiple concurrent background-task calls
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _doc(
    document_id: str = "doc-1",
    title: str = "",
    content: str = "",
) -> Dict[str, Any]:
    return {"document_id": document_id, "title": title, "content": content}


# ---------------------------------------------------------------------------
# Isolate each test from shared singleton state
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_kg_state(tmp_path, monkeypatch):
    """Reset the process-level KG store and event log before every test."""
    import src.knowledge_graph.kg_updater as mod
    monkeypatch.setenv("NOESIS_DB_PATH", str(tmp_path / "kg.duckdb"))
    mod._store = None
    mod._resolver = None
    mod._events.clear()
    yield
    if mod._store is not None and hasattr(mod._store, "close"):
        mod._store.close()
    mod._store = None
    mod._resolver = None
    mod._events.clear()


# ---------------------------------------------------------------------------
# Heuristic NER
# ---------------------------------------------------------------------------

class TestExtractMentions:
    def _extract(self, text: str):
        from src.knowledge_graph.kg_updater import _extract_mentions
        return _extract_mentions(text)

    def test_person_two_tokens(self):
        results = self._extract("Angela Merkel spoke at the summit.")
        names = [name for name, _ in results]
        assert "Angela Merkel" in names

    def test_organisation_suffix(self):
        results = self._extract("Microsoft Corp released a new model.")
        names = [name for name, _ in results]
        assert any("Microsoft" in n for n in names)

    def test_stop_word_filtered(self):
        results = self._extract("The President signed the bill.")
        names = [name for name, _ in results]
        assert "The" not in names

    def test_empty_text(self):
        assert self._extract("") == []

    def test_deduplication(self):
        results = self._extract("Elon Musk said Elon Musk again.")
        names = [name for name, _ in results]
        assert names.count("Elon Musk") == 1


# ---------------------------------------------------------------------------
# update_from_document
# ---------------------------------------------------------------------------

class TestUpdateFromDocument:
    def test_nodes_added_to_store(self):
        from src.knowledge_graph.kg_updater import update_from_document, _shared_store
        update_from_document(_doc(title="Tim Cook unveiled Apple Inc products."))
        store = _shared_store()
        assert store.node_count >= 1  # at least the document anchor

    def test_triple_count_grows(self):
        from src.knowledge_graph.kg_updater import update_from_document, _shared_store
        update_from_document(_doc(title="Angela Merkel met Emmanuel Macron in Paris."))
        store = _shared_store()
        assert store.triple_count >= 1

    def test_events_recorded(self):
        import src.knowledge_graph.kg_updater as mod
        mod.update_from_document(_doc(title="Satya Nadella runs Microsoft Corp."))
        assert len(mod._events) > 0
        kinds = {e.kind for e in mod._events}
        assert "node" in kinds
        assert "triple" in kinds

    def test_empty_document_is_no_op(self):
        import src.knowledge_graph.kg_updater as mod
        mod.update_from_document(_doc())
        assert len(mod._events) == 0

    def test_duplicate_ingest_does_not_error(self):
        from src.knowledge_graph.kg_updater import update_from_document
        doc = _doc(title="Jeff Bezos founded Amazon Inc.")
        update_from_document(doc)
        update_from_document(doc)  # should not raise

    def test_multiple_documents_accumulate(self):
        from src.knowledge_graph.kg_updater import update_from_document, _shared_store
        update_from_document(_doc("d1", title="Apple Inc announced iPhone."))
        update_from_document(_doc("d2", title="Tim Cook presented at Apple Inc event."))
        store = _shared_store()
        assert store.triple_count >= 2

    def test_concurrent_updates_safe(self):
        from src.knowledge_graph.kg_updater import update_from_document, _shared_store
        errors = []

        def _run(i):
            try:
                update_from_document(_doc(f"doc-{i}", title=f"Entity{i} Inc released report{i}."))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_run, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent updates raised: {errors}"
        store = _shared_store()
        assert store.node_count >= 10


# ---------------------------------------------------------------------------
# get_emerging_connections
# ---------------------------------------------------------------------------

class TestGetEmergingConnections:
    def test_returns_connections_after_since(self):
        from src.knowledge_graph.kg_updater import update_from_document, get_emerging_connections
        before = datetime.now(timezone.utc) - timedelta(seconds=1)
        update_from_document(_doc(title="Elon Musk leads Tesla Inc."))
        results = get_emerging_connections(since=before)
        assert len(results) > 0

    def test_returns_empty_when_since_is_future(self):
        from src.knowledge_graph.kg_updater import update_from_document, get_emerging_connections
        update_from_document(_doc(title="Elon Musk leads Tesla Inc."))
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        assert get_emerging_connections(since=future) == []

    def test_result_structure(self):
        from src.knowledge_graph.kg_updater import update_from_document, get_emerging_connections
        before = datetime.now(timezone.utc) - timedelta(seconds=1)
        update_from_document(_doc("d1", title="Angela Merkel led Germany."))
        results = get_emerging_connections(since=before)
        assert len(results) > 0
        r = results[0]
        assert "subject_id" in r
        assert "predicate" in r
        assert "object_id" in r
        assert "source_doc" in r
        assert "added_at" in r

    def test_limit_respected(self):
        from src.knowledge_graph.kg_updater import update_from_document, get_emerging_connections
        before = datetime.now(timezone.utc) - timedelta(seconds=1)
        for i in range(5):
            update_from_document(_doc(f"d{i}", title=f"Entity{i} Corp signed deal{i}."))
        results = get_emerging_connections(since=before, limit=3)
        assert len(results) <= 3


# ---------------------------------------------------------------------------
# get_evolving_topics
# ---------------------------------------------------------------------------

class TestGetEvolvingTopics:
    def test_returns_entities_in_window(self):
        from src.knowledge_graph.kg_updater import update_from_document, get_evolving_topics
        update_from_document(_doc("d1", title="Microsoft Corp released Windows."))
        update_from_document(_doc("d2", title="Microsoft Corp keynote event."))
        results = get_evolving_topics(window_seconds=60)
        names = [r["name"] for r in results]
        assert any("Microsoft" in n for n in names)

    def test_result_structure(self):
        from src.knowledge_graph.kg_updater import update_from_document, get_evolving_topics
        update_from_document(_doc(title="Google Inc announced Gemini."))
        results = get_evolving_topics(window_seconds=60)
        if results:
            r = results[0]
            assert "entity_id" in r
            assert "name" in r
            assert "type" in r
            assert "new_connections" in r
            assert "source_docs" in r

    def test_top_n_respected(self):
        from src.knowledge_graph.kg_updater import update_from_document, get_evolving_topics
        for i in range(10):
            update_from_document(_doc(f"d{i}", title=f"Company{i} Corp deal{i}."))
        results = get_evolving_topics(window_seconds=60, top_n=3)
        assert len(results) <= 3

    def test_empty_when_window_excludes_all(self):
        from src.knowledge_graph.kg_updater import update_from_document, get_evolving_topics
        import src.knowledge_graph.kg_updater as mod
        update_from_document(_doc(title="Apple Inc released iPhone."))
        # Backdate all events so they fall outside the 1-second window
        old_ts = datetime.now(timezone.utc) - timedelta(seconds=120)
        for e in mod._events:
            e.ts = old_ts
        # Mutation events are durable now; backdate the persisted source of
        # truth as well as the compatibility in-process event view.
        mod._store.connection.execute(
            "UPDATE kg_mutation_events SET created_at = ?", [old_ts]
        )
        results = get_evolving_topics(window_seconds=1)
        assert results == []


# ---------------------------------------------------------------------------
# get_store_stats
# ---------------------------------------------------------------------------

class TestGetStoreStats:
    def test_initial_stats(self):
        from src.knowledge_graph.kg_updater import get_store_stats
        stats = get_store_stats()
        assert stats["node_count"] == 0
        assert stats["triple_count"] == 0
        assert stats["status"] == "persisted"

    def test_stats_after_update(self):
        from src.knowledge_graph.kg_updater import update_from_document, get_store_stats
        update_from_document(_doc(title="Tim Cook runs Apple Inc globally."))
        stats = get_store_stats()
        assert stats["node_count"] >= 1
        assert stats["triple_count"] >= 1
        assert stats["total_update_events"] > 0
