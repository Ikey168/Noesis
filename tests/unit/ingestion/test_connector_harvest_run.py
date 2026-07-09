"""Unit tests for adaptive, observable Connector.harvest_run (#896, #897).

Offline: a fake connector drives discover -> fetch -> parse with scripted
outcomes, a real in-memory ``DocumentStore`` and ``SourceHealthTracker`` verify
the store/health integration, and ``sleep`` is a no-op so retries don't wait.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Callable, Dict, List, Optional

import duckdb
import pytest

from services.ingest.common.document_model import Document
from src.ingestion.connectors.base import (
    Connector,
    HarvestSummary,
    PermanentFetchError,
    RawDocument,
    SourceRef,
)
from src.ingestion.document_store import DocumentStore
from src.ingestion.source_health import SourceHealthTracker


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


def _doc(doc_id: str, *, content: str = "Body text.", title: str = "T") -> Document:
    return Document(
        document_id=doc_id,
        source_type="news",
        language="en",
        ingested_at=1_700_000_000_000,
        url=f"https://ex.com/{doc_id}",
        title=title,
        content=content,
    )


class FakeConnector(Connector):
    """A connector with scripted fetch/parse outcomes for one or more sources."""

    source_type = "news"

    def __init__(
        self,
        refs: List[SourceRef],
        docs: Dict[str, List[Document]],
        fetch_behaviors: Optional[Dict[str, Callable[[int], None]]] = None,
        parse_raises: Optional[set] = None,
    ):
        self._refs = refs
        self.docs = docs
        self._fetch_behaviors = fetch_behaviors or {}
        self._parse_raises = parse_raises or set()
        self.fetch_calls: Dict[str, int] = defaultdict(int)

    def discover(self, query=None):
        return self._refs

    def fetch(self, ref: SourceRef) -> RawDocument:
        self.fetch_calls[ref.locator] += 1
        behavior = self._fetch_behaviors.get(ref.locator)
        if behavior is not None:
            behavior(self.fetch_calls[ref.locator])  # may raise
        return RawDocument(ref=ref, content="raw")

    def parse(self, raw: RawDocument) -> List[Document]:
        if raw.ref.locator in self._parse_raises:
            raise ValueError("unparseable")
        return list(self.docs.get(raw.ref.locator, []))


def _noop_sleep(_seconds: float) -> None:
    pass


# --------------------------------------------------------------------------- #
# Retry semantics (#896)
# --------------------------------------------------------------------------- #


def test_transient_fetch_error_is_retried_then_succeeds():
    ref = SourceRef("feed-a")

    def flaky(attempt: int):
        if attempt < 3:  # fail attempts 1 and 2, succeed on 3
            raise RuntimeError("transient network blip")

    conn = FakeConnector([ref], {"feed-a": [_doc("a1")]}, {"feed-a": flaky})
    summary = conn.harvest_run(retries=2, sleep=_noop_sleep)

    assert conn.fetch_calls["feed-a"] == 3
    assert summary.fetched == 1
    assert summary.fetch_errors == 0
    assert summary.documents == 1


def test_permanent_fetch_error_fast_fails_without_retry():
    ref = SourceRef("feed-a")

    def gone(_attempt: int):
        raise PermanentFetchError("404 gone")

    conn = FakeConnector([ref], {"feed-a": [_doc("a1")]}, {"feed-a": gone})
    summary = conn.harvest_run(retries=5, sleep=_noop_sleep)

    assert conn.fetch_calls["feed-a"] == 1  # no retries burned
    assert summary.fetched == 0
    assert summary.fetch_errors == 1
    assert summary.documents == 0


def test_transient_error_exhausts_retries():
    ref = SourceRef("feed-a")

    def always(_attempt: int):
        raise RuntimeError("still down")

    conn = FakeConnector([ref], {"feed-a": [_doc("a1")]}, {"feed-a": always})
    summary = conn.harvest_run(retries=2, sleep=_noop_sleep)

    assert conn.fetch_calls["feed-a"] == 3  # initial + 2 retries
    assert summary.fetch_errors == 1
    assert summary.fetched == 0


def test_parse_error_is_isolated_per_source():
    refs = [SourceRef("good"), SourceRef("bad")]
    conn = FakeConnector(
        refs,
        {"good": [_doc("g1")], "bad": [_doc("b1")]},
        parse_raises={"bad"},
    )
    summary = conn.harvest_run(sleep=_noop_sleep)

    assert summary.fetched == 2       # both fetched fine
    assert summary.parsed == 1        # only "good" parsed
    assert summary.parse_errors == 1
    assert summary.documents == 1


# --------------------------------------------------------------------------- #
# Health integration + drift detection (#896)
# --------------------------------------------------------------------------- #


def test_source_that_stops_yielding_is_flagged_degraded():
    ref = SourceRef("feed", metadata={"source_id": "ex"})
    conn = FakeConnector([ref], {"feed": [_doc("d1"), _doc("d2")]})
    health = SourceHealthTracker()

    # Five productive runs establish a healthy baseline (yield 2).
    for _ in range(5):
        conn.harvest_run(health=health, respect_schedule=False, sleep=_noop_sleep)
    assert health.status("ex") in ("unknown", "healthy")

    # The source goes dark: 200 OK but zero documents extracted.
    conn.docs["feed"] = []
    for _ in range(3):
        conn.harvest_run(health=health, respect_schedule=False, sleep=_noop_sleep)

    assert health.status("ex") == "degraded"


def test_healthy_source_is_not_flagged():
    ref = SourceRef("feed", metadata={"source_id": "ex"})
    conn = FakeConnector([ref], {"feed": [_doc("d1"), _doc("d2")]})
    health = SourceHealthTracker()
    for _ in range(8):
        conn.harvest_run(health=health, respect_schedule=False, sleep=_noop_sleep)
    assert health.status("ex") == "healthy"


# --------------------------------------------------------------------------- #
# Adaptive scheduling / quarantine back-off (#896)
# --------------------------------------------------------------------------- #


def test_quarantined_source_is_skipped_when_respecting_schedule():
    # Drive a source into quarantine directly, then prove harvest_run backs off.
    health = SourceHealthTracker()
    for _ in range(5):
        health.record_run("ex", articles=3, now_ms=1000)
    for _ in range(7):
        health.record_run("ex", articles=0, now_ms=1000)
    assert health.is_quarantined("ex")

    ref = SourceRef("feed", metadata={"source_id": "ex"})
    conn = FakeConnector([ref], {"feed": [_doc("d1")]})
    summary = conn.harvest_run(
        health=health, respect_schedule=True, now_ms=1000, sleep=_noop_sleep
    )

    assert summary.skipped == 1
    assert summary.fetched == 0
    assert conn.fetch_calls["feed"] == 0  # never touched the source


def test_schedule_ignored_when_respect_schedule_false():
    health = SourceHealthTracker()
    for _ in range(12):
        health.record_run("ex", articles=0, now_ms=1000)  # quarantined

    ref = SourceRef("feed", metadata={"source_id": "ex"})
    conn = FakeConnector([ref], {"feed": [_doc("d1")]})
    summary = conn.harvest_run(
        health=health, respect_schedule=False, now_ms=1000, sleep=_noop_sleep
    )
    assert summary.skipped == 0
    assert summary.fetched == 1  # forced fetch despite quarantine


# --------------------------------------------------------------------------- #
# Store integration + summary reconciliation (#897)
# --------------------------------------------------------------------------- #


def test_store_integration_folds_upsert_outcome_into_summary():
    ref = SourceRef("feed")
    docs = [
        _doc("d1", content="Unique story."),
        _doc("d2", content="Shared body."),
        _doc("d3", content="Shared body."),  # content duplicate of d2
    ]
    conn = FakeConnector([ref], {"feed": docs})
    store = DocumentStore(duckdb.connect(":memory:"))

    summary = conn.harvest_run(store=store, sleep=_noop_sleep)

    assert summary.documents == 3
    assert summary.inserted == 2
    assert summary.duplicate == 1
    assert summary.invalid == 0
    # Every produced document lands in exactly one store outcome.
    assert summary.inserted + summary.duplicate + summary.invalid == summary.documents
    assert store.count() == 2


def test_summary_source_stage_counts_reconcile():
    refs = [SourceRef("a"), SourceRef("b"), SourceRef("c")]
    conn = FakeConnector(
        refs,
        {"a": [_doc("a1")], "b": [_doc("b1")], "c": [_doc("c1")]},
        fetch_behaviors={"b": lambda _a: (_ for _ in ()).throw(PermanentFetchError("x"))},
        parse_raises={"c"},
    )
    summary = conn.harvest_run(sleep=_noop_sleep)

    # a: fetched+parsed; b: fetch error; c: fetched but parse error.
    assert summary.discovered == 3
    assert summary.fetched == 2      # a, c
    assert summary.parsed == 1       # a
    assert summary.fetch_errors == 1
    assert summary.parse_errors == 1
    # Stage counts are monotonically non-increasing.
    assert summary.discovered >= summary.fetched >= summary.parsed


def test_per_source_breakdown_isolates_a_degraded_source():
    refs = [SourceRef("healthy"), SourceRef("broken")]
    conn = FakeConnector(
        refs,
        {"healthy": [_doc("h1"), _doc("h2")], "broken": []},
        fetch_behaviors={},
    )
    summary = conn.harvest_run(sleep=_noop_sleep)

    assert summary.per_source["healthy"]["documents"] == 2
    assert summary.per_source["broken"]["documents"] == 0
    assert summary.per_source["broken"]["fetched"] == 1


def test_harvest_run_without_health_or_store_just_counts():
    ref = SourceRef("feed")
    conn = FakeConnector([ref], {"feed": [_doc("d1"), _doc("d2")]})
    summary = conn.harvest_run(sleep=_noop_sleep)
    assert summary.as_dict()["documents"] == 2
    assert summary.inserted == 0  # no store attached


# --------------------------------------------------------------------------- #
# Back-compat: harvest() generator unchanged
# --------------------------------------------------------------------------- #


def test_harvest_generator_still_yields_documents():
    ref = SourceRef("feed")
    conn = FakeConnector([ref], {"feed": [_doc("d1"), _doc("d2")]})
    out = list(conn.harvest())
    assert [d.document_id for d in out] == ["d1", "d2"]


def test_harvest_summary_dataclass_defaults():
    s = HarvestSummary()
    d = s.as_dict()
    assert d["discovered"] == 0 and d["documents"] == 0
    assert d["per_source"] == {}
