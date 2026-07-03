"""M1.3 equivalence: the remaining analytics panels are served by data-mode
tools whose payloads match the read path they wrap, and whose data-mode
annotation mirrors their panel annotation.

Covers the five pipeline analytics panels (anomaly_timeline, lead_lag,
narrative_thread, drift_trajectory, forecast) with full read-path equivalence,
and the community-colored entity graph (kg_communities data-mode annotation).
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastmcp")
duckdb = pytest.importorskip("duckdb")

REPO = Path(__file__).resolve().parents[3]

# (tool, panel type, kwargs, read-path callable(con, kwargs))
PIPELINE_CASES = [
    ("detect_anomalies", "anomaly_timeline", {},
     lambda con, k: __import__("src.analytics.anomalies", fromlist=["detect_anomalies_payload"]).detect_anomalies_payload(con, topic=k.get("topic"), metric=k.get("metric"))),
    ("lead_lag", "lead_lag", {"topic": "energy"},
     lambda con, k: __import__("src.analytics.lead_lag", fromlist=["lead_lag_payload"]).lead_lag_payload(con, k["topic"], None)),
    ("cluster_narratives", "narrative_thread", {},
     lambda con, k: __import__("src.analytics.narratives", fromlist=["cluster_narratives_payload"]).cluster_narratives_payload(con, k.get("topic"), k.get("days"))),
    ("semantic_drift", "drift_trajectory", {"term": "grid"},
     lambda con, k: __import__("src.analytics.drift", fromlist=["semantic_drift_payload"]).semantic_drift_payload(con, k["term"], 90)),
    ("forecast_topic", "forecast", {"topic": "energy"},
     lambda con, k: __import__("src.analytics.drift", fromlist=["forecast_topic_payload"]).forecast_topic_payload(con, k["topic"], 7)),
]


def _normalize(obj):
    return json.loads(json.dumps(obj, default=str, sort_keys=True))


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE news_articles (id VARCHAR, title VARCHAR, url VARCHAR, content VARCHAR, "
        "publish_date TIMESTAMP, source VARCHAR, category VARCHAR, sentiment_score DOUBLE, "
        "sentiment_label VARCHAR)"
    )
    rows = []
    for d in range(1, 21):
        rows.append((f"n{d}", f"Energy grid update {d}", f"http://x/{d}", None,
                     f"2026-05-{d:02d}", "Alpha Wire", "energy",
                     0.1 * (d % 5) - 0.2, "neutral"))
    con.executemany(
        "INSERT INTO news_articles (id, title, url, content, publish_date, source, category, "
        "sentiment_score, sentiment_label) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.close()
    monkeypatch.setenv("NEURONEWS_DB_PATH", str(db))
    monkeypatch.setenv("NOESIS_DB_PATH", str(db))
    return db


def _load(name, rel):
    path = REPO / rel
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tool_meta(module):
    from fastmcp.client import Client

    async def run():
        async with Client(module.mcp) as c:
            return {t.name: (t.meta if isinstance(t.meta, dict) else {}) for t in await c.list_tools()}

    return asyncio.run(run())


def _call(module, tool, kwargs):
    from fastmcp.client import Client

    async def run():
        async with Client(module.mcp) as c:
            return (await c.call_tool(tool, kwargs)).structured_content

    return asyncio.run(run())


def test_pipeline_analytics_panels_have_mirrored_data_mode_tools(warehouse):
    module = _load("pl_analytics_equiv", "tools/pipeline_mcp/server.py")
    metas = _tool_meta(module)
    for tool, panel_type, _, _ in PIPELINE_CASES:
        assert tool in metas, f"{tool} not served"
        meta = metas[tool]
        assert isinstance(meta.get("data"), dict), f"{tool} missing meta.data block"
        assert meta["data"]["panel"] == panel_type
        assert meta.get("panel", {}).get("type") == panel_type


@pytest.mark.parametrize("tool,panel_type,kwargs,read_path", PIPELINE_CASES, ids=[c[1] for c in PIPELINE_CASES])
def test_pipeline_data_mode_payload_matches_read_path(warehouse, tool, panel_type, kwargs, read_path):
    module = _load("pl_analytics_equiv", "tools/pipeline_mcp/server.py")
    via_tool = _call(module, tool, kwargs)
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        via_read_path = read_path(con, kwargs)
    except Exception as exc:  # mirror the tool's own error handling
        via_read_path = {"error": str(exc)}
    finally:
        con.close()
    assert _normalize(via_tool) == _normalize(via_read_path), (
        f"{tool} data-mode payload diverges from the read path"
    )


def test_detect_anomalies_returns_a_real_payload(warehouse):
    module = _load("pl_analytics_equiv", "tools/pipeline_mcp/server.py")
    out = _call(module, "detect_anomalies", {"topic": "energy", "metric": "volume"})
    assert "error" not in out
    assert "windows" in out and "flagged" in out


def test_entity_graph_community_coloring_is_data_mode():
    module = _load("kg_analytics_equiv", "tools/kg_mcp/server.py")
    metas = _tool_meta(module)
    assert "kg_communities" in metas
    data = metas["kg_communities"].get("data")
    assert isinstance(data, dict) and data["panel"] == "entity_graph"
    # The tool still returns its read-path object shape.
    out = _call(module, "kg_communities", {})
    assert "error" not in out
    assert {"community_count", "communities", "assignments"} <= set(out)
