"""Unit tests for pack-supplied empty-canvas telemetry (R3, #591).

Covers the collector (src/genui/telemetry.py), the library fallback over
an in-memory warehouse, and the news pack's provider. The exit-criterion
test: with the news pack disabled the canvas telemetry still carries
recently ingested documents, never an empty gap.
"""

from types import SimpleNamespace

import duckdb
import pytest

import src.genui.telemetry as telemetry
from src.genui.telemetry import (
    _clean_movers,
    _clean_signals,
    _clean_ticker,
    _library_telemetry,
    pack_telemetry,
)


def make_pack(name, provider):
    return SimpleNamespace(name=name, telemetry=provider)


@pytest.fixture
def packs(monkeypatch):
    def _install(pack_list):
        import src.domains.registry as registry

        monkeypatch.setattr(registry, "get_enabled_packs", lambda: pack_list)

    return _install


@pytest.fixture
def memory_warehouse(monkeypatch):
    """In-memory DuckDB standing in for the shared analytics connection."""
    import threading

    import src.database.local_analytics_connector as connector

    conn = duckdb.connect(":memory:")
    monkeypatch.setattr(connector, "get_shared_connection", lambda: conn)
    monkeypatch.setattr(connector, "_LOCK", threading.Lock())
    return conn


NEWS_SUPPLY = {
    "signals": [{"label": "ARTICLES", "value": 64}],
    "movers": [{"label": "Ai Policy", "intent": "coverage of ai policy", "change": 12}],
    "ticker": {"label": "BREAKING", "items": ["Headline one", "Headline two"]},
}


# ---------------------------------------------------------------------------
# Cleaning helpers
# ---------------------------------------------------------------------------


def test_clean_helpers_accept_valid_shapes():
    assert _clean_signals(NEWS_SUPPLY["signals"]) == NEWS_SUPPLY["signals"]
    assert _clean_movers(NEWS_SUPPLY["movers"]) == NEWS_SUPPLY["movers"]
    ticker = _clean_ticker(NEWS_SUPPLY["ticker"])
    assert ticker == {"label": "BREAKING", "items": ["Headline one", "Headline two"]}


def test_clean_helpers_reject_junk():
    assert _clean_signals("nope") == []
    assert _clean_signals([{"value": 3}, 7]) == []
    assert _clean_movers([{"label": "x"}, {"intent": "y"}, None]) == []
    assert _clean_ticker(None) is None
    assert _clean_ticker({"label": "X", "items": []}) is None
    assert _clean_ticker({"items": ["a"]}) is None
    # Non-string items are dropped; movers without numeric change lose it.
    assert _clean_ticker({"label": "X", "items": ["a", 3, " "]}) == {
        "label": "X",
        "items": ["a"],
    }
    assert _clean_movers([{"label": "a", "intent": "b", "change": "hot"}]) == [
        {"label": "a", "intent": "b"}
    ]


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


def test_enabled_pack_supplies_telemetry(packs):
    packs([make_pack("news", lambda: NEWS_SUPPLY)])
    result = pack_telemetry()
    assert result["packs"] == ["news"]
    assert result["signals"] == NEWS_SUPPLY["signals"]
    assert result["movers"] == NEWS_SUPPLY["movers"]
    assert result["ticker"]["label"] == "BREAKING"


def test_pack_without_provider_is_skipped(packs, monkeypatch):
    monkeypatch.setattr(telemetry, "_library_telemetry", lambda: {})
    packs([make_pack("quiet", None)])
    result = pack_telemetry()
    assert result == {"signals": [], "movers": [], "ticker": None, "packs": []}


def test_failing_provider_never_breaks_the_canvas(packs, monkeypatch):
    def boom():
        raise RuntimeError("pack exploded")

    monkeypatch.setattr(telemetry, "_library_telemetry", lambda: {})
    packs([make_pack("news", boom)])
    result = pack_telemetry()
    assert result["packs"] == []
    assert result["ticker"] is None


def test_first_ticker_wins_and_movers_are_capped(packs, monkeypatch):
    monkeypatch.setattr(telemetry, "_library_telemetry", lambda: {})
    many_movers = [
        {"label": f"m{i}", "intent": f"view {i}"} for i in range(10)
    ]
    packs(
        [
            make_pack("a", lambda: {"ticker": {"label": "FIRST", "items": ["x"]}, "movers": many_movers}),
            make_pack("b", lambda: {"ticker": {"label": "SECOND", "items": ["y"]}}),
        ]
    )
    result = pack_telemetry()
    assert result["ticker"]["label"] == "FIRST"
    assert len(result["movers"]) == telemetry.MAX_MOVERS
    assert result["packs"] == ["a", "b"]


def test_news_off_falls_back_to_library(packs, monkeypatch):
    """The R3 exit criterion: news disabled, telemetry still live."""
    packs([])  # no enabled packs advertise anything
    monkeypatch.setattr(
        telemetry,
        "_library_telemetry",
        lambda: {
            "signals": [{"label": "DOCS", "value": 12}],
            "movers": [{"label": "paper", "intent": "library documents about paper", "change": 7}],
            "ticker": {"label": "NEW IN LIBRARY", "items": ["Attention is all you need"]},
        },
    )
    result = pack_telemetry()
    assert result["packs"] == ["library"]
    assert result["ticker"]["label"] == "NEW IN LIBRARY"
    assert result["movers"][0]["intent"].startswith("library documents")


def test_library_failure_degrades_to_empty(packs, monkeypatch):
    packs([])

    def boom():
        raise RuntimeError("no warehouse")

    monkeypatch.setattr(telemetry, "_library_telemetry", boom)
    result = pack_telemetry()
    assert result == {"signals": [], "movers": [], "ticker": None, "packs": []}


def test_registry_failure_degrades_to_library(monkeypatch):
    import src.domains.registry as registry

    def boom():
        raise RuntimeError("registry broken")

    monkeypatch.setattr(registry, "get_enabled_packs", boom)
    monkeypatch.setattr(telemetry, "_library_telemetry", lambda: {})
    assert pack_telemetry()["packs"] == []


# ---------------------------------------------------------------------------
# Library fallback over a real (in-memory) warehouse
# ---------------------------------------------------------------------------


def test_library_telemetry_prefers_documents_table(memory_warehouse):
    memory_warehouse.execute(
        "CREATE TABLE documents (title VARCHAR, source_type VARCHAR, created_at TIMESTAMP)"
    )
    memory_warehouse.execute(
        "INSERT INTO documents VALUES "
        "('Paper A', 'paper', '2026-07-01'), ('Book B', 'book', '2026-07-02')"
    )
    result = _library_telemetry()
    assert result["signals"] == [{"label": "DOCS", "value": 2}]
    assert result["ticker"]["label"] == "NEW IN LIBRARY"
    assert "Book B" in result["ticker"]["items"]
    intents = {m["intent"] for m in result["movers"]}
    assert "library documents about paper" in intents


def test_library_telemetry_uses_news_articles_when_no_documents(memory_warehouse):
    memory_warehouse.execute(
        "CREATE TABLE news_articles (title VARCHAR, category VARCHAR, publish_date TIMESTAMP)"
    )
    memory_warehouse.execute(
        "INSERT INTO news_articles VALUES ('Headline', 'Politics', '2026-07-01')"
    )
    result = _library_telemetry()
    assert result["signals"] == [{"label": "DOCS", "value": 1}]
    assert result["ticker"]["items"] == ["Headline"]


def test_library_telemetry_empty_warehouse_returns_nothing(memory_warehouse):
    assert _library_telemetry() == {}


# ---------------------------------------------------------------------------
# News pack provider over a real (in-memory) warehouse
# ---------------------------------------------------------------------------


def test_news_telemetry_provider(memory_warehouse):
    from src.domains.news.telemetry import news_telemetry

    memory_warehouse.execute(
        "CREATE TABLE news_articles ("
        "title VARCHAR, source VARCHAR, category VARCHAR, publish_date TIMESTAMP)"
    )
    memory_warehouse.execute(
        "INSERT INTO news_articles VALUES "
        "('H1', 'Reuters', 'Politics', CURRENT_TIMESTAMP), "
        "('H2', 'AP', 'Politics', CURRENT_TIMESTAMP), "
        "('H3', 'AP', 'Tech', TIMESTAMP '2020-01-01')"
    )
    result = news_telemetry()
    labels = {s["label"]: s["value"] for s in result["signals"]}
    assert labels["ARTICLES"] == 3
    assert labels["SOURCES"] == 2
    assert result["ticker"]["label"] == "BREAKING"
    assert set(result["ticker"]["items"]) == {"H1", "H2", "H3"}
    politics = next(m for m in result["movers"] if m["label"] == "Politics")
    assert politics["intent"] == "coverage of politics"
    assert politics["change"] == 100  # both articles in the last 7 days
