"""Unit tests for R3 tool-sourced adaptivity (src/genui/adaptivity.py).

resolve_availability / resolve_ui_flags prefer the MCP servers' stats
tools and fall back to the DuckDB probe / pack registry. All host access
goes through fakes; the fallback probe is stubbed so nothing touches a
real warehouse.
"""

from typing import Any, Dict

import pytest

import src.genui.adaptivity as adaptivity
from src.genui.adaptivity import (
    DOCUMENT_CORPUS_TABLES,
    reset_tool_cache,
    resolve_availability,
    resolve_ui_flags,
)


class FakeHost:
    """Scripted host: connected servers + canned call_tool results."""

    def __init__(self, connected, results: Dict[str, Any]):
        self.connected = set(connected)
        self.results = results
        self.calls = []

    def status(self):
        return {
            "servers": {
                name: {"state": "connected"} for name in self.connected
            }
        }

    def call_tool(self, server, tool, arguments=None, timeout=10.0):
        self.calls.append((server, tool))
        result = self.results[tool]
        if isinstance(result, Exception):
            raise result
        return result


ALL_STATS_SERVERS = ("neuronews-arguments", "neuronews-pipeline", "neuronews-domain-packs")

GOOD_RESULTS = {
    "am_stats": {
        "argument_claims": 5,
        "claim_conflicts": 0,
        "source_stances": 3,
        "stance_drift_events": "table_missing",
        "policy_positions": 1,
        "document_frames": 0,
        "total_claims": 5,
    },
    "article_stats": {"total_articles": 64, "sources": 9},
    "document_stats": {"total_documents": 0, "by_source_type": []},
    "list_outlet_scores": {"count": 1, "outlets": [{}]},
    "list_outlet_clusters": {"count": 0, "outlets": []},
    "actor_summary": {"count": 1, "actors": [{}]},
    "get_ui_flags": {"flags": {"trending": True, "watchlists": False}, "contributing": ["news"]},
}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    reset_tool_cache()
    # The fallback must never hit a real warehouse in these tests.
    monkeypatch.setattr(adaptivity, "data_availability", lambda probe=None: None)
    yield
    reset_tool_cache()


def install(monkeypatch, host):
    monkeypatch.setattr(adaptivity, "_get_host", lambda: host)
    return host


# ---------------------------------------------------------------------------
# resolve_availability
# ---------------------------------------------------------------------------


def test_tools_path_maps_counts_to_availability(monkeypatch):
    host = install(monkeypatch, FakeHost(ALL_STATS_SERVERS, GOOD_RESULTS))
    availability, source = resolve_availability()
    assert source == "tools"
    assert availability["argument_claims"] is True
    assert availability["claim_conflicts"] is False
    assert availability["source_stances"] is True
    assert availability["stance_drift_events"] is False  # table_missing -> 0
    assert availability["news_articles"] is True
    assert availability["outlet_scores"] is True
    assert availability["outlet_clusters"] is False
    assert availability["document_actors"] is True
    # Corpus union: zero documents rows, but news_articles has rows.
    assert availability["documents"] is True


def test_corpus_union_from_documents_only(monkeypatch):
    results = dict(GOOD_RESULTS)
    results["article_stats"] = {"total_articles": 0}
    results["document_stats"] = {"total_documents": 12}
    install(monkeypatch, FakeHost(ALL_STATS_SERVERS, results))
    availability, source = resolve_availability()
    assert source == "tools"
    assert availability["news_articles"] is False
    assert availability["documents"] is True


def test_results_are_cached_within_ttl(monkeypatch):
    host = install(monkeypatch, FakeHost(ALL_STATS_SERVERS, GOOD_RESULTS))
    resolve_availability()
    first_calls = len(host.calls)
    availability, source = resolve_availability()
    assert source == "tools"
    assert len(host.calls) == first_calls  # served from cache

    reset_tool_cache()
    resolve_availability()
    assert len(host.calls) == 2 * first_calls


def test_no_host_falls_back(monkeypatch):
    install(monkeypatch, None)
    monkeypatch.setattr(
        adaptivity, "data_availability", lambda probe=None: {"news_articles": True}
    )
    availability, source = resolve_availability()
    assert source == "warehouse"
    assert availability == {"news_articles": True}


def test_stats_server_down_falls_back(monkeypatch):
    install(monkeypatch, FakeHost(("neuronews-arguments",), GOOD_RESULTS))
    availability, source = resolve_availability()
    assert source == "unknown"
    assert availability is None


def test_tool_error_falls_back(monkeypatch):
    results = dict(GOOD_RESULTS)
    results["am_stats"] = {"error": "warehouse locked"}
    install(monkeypatch, FakeHost(ALL_STATS_SERVERS, results))
    availability, source = resolve_availability()
    assert source == "unknown"
    assert availability is None


def test_tool_exception_falls_back(monkeypatch):
    results = dict(GOOD_RESULTS)
    results["article_stats"] = RuntimeError("session died")
    install(monkeypatch, FakeHost(ALL_STATS_SERVERS, results))
    availability, source = resolve_availability()
    assert source == "unknown"


@pytest.mark.parametrize("tool", ["article_stats", "document_stats", "actor_summary"])
def test_any_stats_tool_error_falls_back(monkeypatch, tool):
    results = dict(GOOD_RESULTS)
    results[tool] = {"error": "warehouse locked"}
    install(monkeypatch, FakeHost(ALL_STATS_SERVERS, results))
    availability, source = resolve_availability()
    assert source == "unknown"
    assert availability is None


def test_get_host_reads_the_singleton(monkeypatch):
    """The real _get_host goes through src.mcp_host.get_host."""
    host = FakeHost(ALL_STATS_SERVERS, GOOD_RESULTS)
    monkeypatch.setattr("src.mcp_host.get_host", lambda: host)
    availability, source = resolve_availability()
    assert source == "tools"
    assert availability is not None


def test_availability_reads_through_shared_cache(monkeypatch):
    """R4 #593: when the host exposes call_tool_cached, adaptivity uses it
    (the shared stats cache) rather than call_tool, so results are shared
    with the planning loop."""

    class CachingHost(FakeHost):
        def __init__(self):
            super().__init__(ALL_STATS_SERVERS, GOOD_RESULTS)
            self.cached_calls = []

        def call_tool_cached(self, server, tool, arguments=None, timeout=10.0, ttl=None):
            self.cached_calls.append((server, tool))
            return self.call_tool(server, tool, arguments)

    host = CachingHost()
    install(monkeypatch, host)
    availability, source = resolve_availability()
    assert source == "tools"
    # Every stats read went through the shared cache, not the raw path.
    assert host.cached_calls
    assert len(host.cached_calls) == len(host.calls)


# ---------------------------------------------------------------------------
# resolve_ui_flags
# ---------------------------------------------------------------------------


def test_ui_flags_from_domain_packs_server(monkeypatch):
    install(monkeypatch, FakeHost(ALL_STATS_SERVERS, GOOD_RESULTS))
    flags, source = resolve_ui_flags()
    assert source == "tools"
    assert flags == {"trending": True, "watchlists": False}


def test_ui_flags_fall_back_to_registry(monkeypatch):
    install(monkeypatch, None)
    monkeypatch.setattr(adaptivity, "merged_ui_flags", lambda: {"clusters": True})
    flags, source = resolve_ui_flags()
    assert source == "packs"
    assert flags == {"clusters": True}


def test_ui_flags_bad_tool_payload_falls_back(monkeypatch):
    results = dict(GOOD_RESULTS)
    results["get_ui_flags"] = {"error": "no packs"}
    install(monkeypatch, FakeHost(ALL_STATS_SERVERS, results))
    monkeypatch.setattr(adaptivity, "merged_ui_flags", lambda: {})
    flags, source = resolve_ui_flags()
    assert source == "packs"
    assert flags == {}


def test_ui_flags_cached(monkeypatch):
    host = install(monkeypatch, FakeHost(ALL_STATS_SERVERS, GOOD_RESULTS))
    resolve_ui_flags()
    n = len(host.calls)
    resolve_ui_flags()
    assert len(host.calls) == n


# ---------------------------------------------------------------------------
# Corpus union in the warehouse fallback path
# ---------------------------------------------------------------------------


def test_probe_availability_applies_corpus_union():
    counts = {"news_articles": 4, "documents": 0, "argument_claims": 1}
    availability = adaptivity._availability_from_counts(counts)
    assert availability["documents"] is True
    assert availability["news_articles"] is True

    empty = adaptivity._availability_from_counts({t: 0 for t in DOCUMENT_CORPUS_TABLES})
    assert empty["documents"] is False
