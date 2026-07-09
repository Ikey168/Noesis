"""Unit tests for the health-aware harvest scheduler (orchestration). Offline."""

from __future__ import annotations

from typing import List

import duckdb
import pytest

from services.ingest.common.document_model import Document
from src.ingestion.connectors.base import Connector, RawDocument, SourceRef
from src.ingestion.document_store import DocumentStore
from src.ingestion.scheduler import HarvestScheduler
from src.ingestion.source_health import SourceHealthTracker


class _FakeConnector(Connector):
    source_type = "news"

    def __init__(self, source_id: str, docs: List[Document], raises: bool = False):
        self._source_id = source_id
        self._docs = docs
        self._raises = raises

    def discover(self, query=None):
        return [SourceRef(self._source_id, metadata={"source_id": self._source_id})]

    def fetch(self, ref):
        if self._raises:
            raise RuntimeError("boom")
        return RawDocument(ref=ref, content="raw")

    def parse(self, raw):
        return list(self._docs)


def _doc(doc_id, content="Body.", source_type="news"):
    return Document(document_id=doc_id, source_type=source_type, language="en",
                    ingested_at=1_700_000_000_000, url=f"https://ex.com/{doc_id}",
                    content=content)


@pytest.fixture
def store():
    return DocumentStore(duckdb.connect(":memory:"))


def test_run_once_harvests_all_connectors_and_aggregates(store):
    connectors = [
        ("news", _FakeConnector("s1", [_doc("a1"), _doc("a2", content="Two.")])),
        ("blog", _FakeConnector("s2", [_doc("b1", content="Blog.", source_type="blog")])),
    ]
    sched = HarvestScheduler(store, SourceHealthTracker(), connectors=connectors)
    result = sched.run_once(now_ms=1000)

    assert result["totals"]["documents"] == 3
    assert result["totals"]["inserted"] == 3
    assert store.count() == 3
    # per-connector detail is present
    assert result["connectors"]["news"]["inserted"] == 2
    assert result["connectors"]["blog"]["inserted"] == 1


def test_totals_reconcile_with_per_connector(store):
    connectors = [
        ("news", _FakeConnector("s1", [_doc("a1")])),
        ("blog", _FakeConnector("s2", [_doc("b1", source_type="blog")])),
    ]
    result = HarvestScheduler(store, SourceHealthTracker(), connectors=connectors).run_once(now_ms=1)
    for key in ("inserted", "documents", "duplicate", "invalid"):
        assert result["totals"][key] == sum(
            c[key] for c in result["connectors"].values() if "error" not in c
        )


def test_health_aware_second_pass_skips_not_due_sources(store):
    health = SourceHealthTracker()
    conn = [("news", _FakeConnector("s1", [_doc("a1")]))]
    sched = HarvestScheduler(store, health, connectors=conn)

    first = sched.run_once(now_ms=1000)
    assert first["totals"]["inserted"] == 1

    # Same instant: the source was just harvested, so it is not due -> skipped.
    second = sched.run_once(now_ms=1000)
    assert second["totals"]["skipped"] == 1
    assert second["totals"]["inserted"] == 0
    assert store.count() == 1


def test_a_failing_connector_does_not_abort_the_run(store):
    connectors = [
        ("news", _FakeConnector("s1", [_doc("a1")])),
        ("broken", _FakeConnector("s2", [], raises=True)),
    ]
    result = HarvestScheduler(store, SourceHealthTracker(), connectors=connectors).run_once(now_ms=1)
    # The healthy connector still harvested; the broken one is recorded, not raised.
    assert result["connectors"]["news"]["inserted"] == 1
    # broken's fetch error is caught inside harvest_run -> a summary with fetch_errors,
    # not a scheduler-level error entry.
    assert result["connectors"]["broken"]["fetch_errors"] == 1
    assert store.count() == 1


def test_no_connectors_yields_zero_totals(store):
    result = HarvestScheduler(store, SourceHealthTracker(), connectors=[]).run_once()
    assert result["connectors"] == {}
    assert result["totals"]["inserted"] == 0
