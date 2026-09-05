import threading
import time

import duckdb
import pytest

from src.kb.unified_query import QueryCatalog, UnifiedQueryEngine, UnifiedQueryError, capability_definition


def request(**extra):
    return {"query": "q", "scope": {"namespaces": ["research"]}, "surfaces": ["lexical"],
            "budgets": {"timeout_ms": 40, "max_retries": 3}, **extra}


class BlockingSource:
    def __init__(self, release, *, surface="lexical", conn=None):
        self.release = release
        self.entered = threading.Event()
        self.finished = threading.Event()
        self.sql_results = []
        self.definition = capability_definition("slow", "memory" if surface == "memory" else "test",
                                                namespaces=["research"], surfaces=[surface], object_types=["document"])
        if conn is not None:
            self.conn = conn
    def describe(self):
        return self.definition
    def query(self, child, *, scopes):
        self.entered.set()
        try:
            self.release.wait(timeout=2)
            if hasattr(self, "conn"):
                self.sql_results.append(self.conn.execute("SELECT 1").fetchone())
            return {"items": []}
        finally:
            self.finished.set()


@pytest.mark.parametrize("surface", ["lexical", "memory"])
def test_deadline_does_not_wait_for_slow_source_or_memory(surface):
    release = threading.Event()
    source = BlockingSource(release, surface=surface)
    engine = UnifiedQueryEngine(QueryCatalog([source]))
    extra = {"surfaces": [surface], "memory": {"mode": "query-expansion"}} if surface == "memory" else {}
    try:
        start = time.monotonic()
        result = engine.execute(request(**extra), scopes={"operator"})
        assert time.monotonic() - start < .25
        assert source.entered.is_set() and not source.finished.is_set()
        assert result["failures"][0]["error"]["code"] == "source_timeout"
    finally:
        release.set()
        assert source.finished.wait(timeout=2)


def test_retries_receive_only_remaining_time():
    calls = []
    class RetryingSource(BlockingSource):
        def query(self, child, *, scopes):
            calls.append(child["timeout_ms"])
            time.sleep(.025)
            raise UnifiedQueryError("source_timeout", "receiver timed out")
    engine = UnifiedQueryEngine(QueryCatalog([RetryingSource(threading.Event())]))
    start = time.monotonic()
    engine.execute(request(), scopes={"operator"})
    assert time.monotonic() - start < .25
    time.sleep(.04)  # allow the bounded in-flight call to finish
    assert len(calls) == 2 and calls[1] < calls[0]


def test_repeated_timeouts_have_bounded_admission():
    release = threading.Event()
    sources = []
    try:
        for _ in range(10):
            source = BlockingSource(release)
            sources.append(source)
            result = UnifiedQueryEngine(QueryCatalog([source])).execute(request(), scopes={"operator"})
        assert sum(source.entered.is_set() for source in sources) <= 8
        assert result["failures"][0]["error"]["code"] == "source_busy"
        assert len([thread for thread in threading.enumerate() if thread.name.startswith("noesis-query")]) <= 8
    finally:
        release.set()
        for source in sources:
            if source.entered.is_set():
                assert source.finished.wait(timeout=2)


def test_timed_out_file_reader_keeps_independent_connection(tmp_path):
    conn = duckdb.connect(str(tmp_path / "query.duckdb"))
    release = threading.Event()
    source = BlockingSource(release, conn=conn)
    try:
        result = UnifiedQueryEngine(QueryCatalog([source])).execute(request(), scopes={"operator"})
        conn.close()
        assert result["failures"][0]["error"]["code"] == "source_timeout"
    finally:
        release.set()
        assert source.finished.wait(timeout=2)
    assert source.sql_results == [(1,)]
