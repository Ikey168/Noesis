"""R7 #605 acceptance: with research on and news off, the canvas is fully
functional on research panels and shows research telemetry.

Exercises the registry + adaptivity + telemetry + planner together, the way
the running app wires them, so "research is a first-class domain" is proven
end-to-end (minus the live panel data path, which lands at R12)."""

import threading

import pytest

duckdb = pytest.importorskip("duckdb")

import src.domains.news  # noqa: F401  (registers the news pack)
import src.domains.research  # noqa: F401  (registers the research pack)
from src.domains import registry
from src.domains.news.pack import NewsDomainPack
from src.domains.research.pack import ResearchDomainPack
from src.genui.adaptivity import merged_ui_flags
from src.genui.planner import plan
from src.genui.telemetry import pack_telemetry


@pytest.fixture
def research_only():
    """Enable research, disable news (the acceptance scenario). reset() clears
    the registry, so both packs are re-registered before enabling research."""
    registry.reset()
    registry.register_pack(NewsDomainPack)
    registry.register_pack(ResearchDomainPack)
    registry.enable_pack("research")
    yield
    registry.reset()
    registry.register_pack(NewsDomainPack)
    registry.register_pack(ResearchDomainPack)
    registry.enable_pack("news")


def test_only_research_flags_are_active(research_only):
    flags = merged_ui_flags()
    # Research panel flags present...
    assert flags.get("research") is True
    assert flags.get("citation_graph") is True
    assert flags.get("venues") is True
    assert flags.get("literature_claims") is True
    # ...and no news flags leak in.
    assert "sentiment_dashboard" not in flags
    assert "trending" not in flags


def test_research_intents_plan_research_panels(research_only):
    flags = merged_ui_flags()
    types = {p.type for p in plan("citation graph and venues for papers", ui_flags=flags).panels}
    assert {"citation_graph", "venues"} & types
    # News-gated panels are hidden because their ui_flags are off.
    assert "trending" not in types
    assert "sentiment_heatmap" not in types


def test_overview_still_works_with_news_off(research_only):
    flags = merged_ui_flags()
    # A generic briefing still yields a live overview canvas (documents anchor).
    types = {p.type for p in plan("daily briefing", ui_flags=flags).panels}
    assert types & {"kpi_row", "articles", "documents"}
    assert len(types) > 1  # never an empty canvas


def test_research_telemetry_when_news_off(research_only, monkeypatch):
    # Point the shared warehouse at an in-memory paper corpus.
    conn = duckdb.connect(":memory:")
    conn.execute(
        "CREATE TABLE documents (id VARCHAR, title VARCHAR, source_type VARCHAR, "
        "venue VARCHAR, concept VARCHAR, created_at TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO documents VALUES "
        "('p1','Attention is all you need','paper','NeurIPS','ml','2025-06-02'),"
        "('p2','Diffusion models','paper','ICML','ml','2025-06-03'),"
        "('p3','Grid storage limits','paper','Energy Policy','energy','2025-06-01')"
    )
    import src.database.local_analytics_connector as connector

    monkeypatch.setattr(connector, "get_shared_connection", lambda: conn)
    monkeypatch.setattr(connector, "_LOCK", threading.Lock())

    telemetry = pack_telemetry()
    # Research pack supplied the ambient signal, not the news pack or library.
    assert telemetry["packs"] == ["research"]
    assert telemetry["ticker"]["label"] == "NEW PAPERS"
    assert any(m["intent"].startswith("literature on") for m in telemetry["movers"])
    labels = {s["label"] for s in telemetry["signals"]}
    assert "PAPERS" in labels and "VENUES" in labels
