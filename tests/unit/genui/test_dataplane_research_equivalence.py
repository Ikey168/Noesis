"""M1.2 equivalence: every research panel type is served by a data-mode tool
whose payload matches the existing read path (the src.domains.research.analytics
function it wraps), and whose data-mode annotation mirrors its panel annotation.

Loads the research server by path, calls each data-mode tool over an in-memory
FastMCP client on a seeded temp warehouse, and asserts the structured payload is
equivalent to calling the underlying analytics function directly.
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

CASES = [
    ("venues", "venues", {}),
    ("citation_graph", "citation_graph", {"topic": "climate"}),
    ("literature_claims", "literature_claims", {"topic": "grid"}),
]

READ_PATH = {
    "venues": lambda con, k: __import__("src.domains.research.analytics", fromlist=["venue_credibility"]).venue_credibility(con),
    "citation_graph": lambda con, k: __import__("src.domains.research.analytics", fromlist=["citation_graph"]).citation_graph(con, k.get("topic")),
    "literature_claims": lambda con, k: __import__("src.domains.research.analytics", fromlist=["literature_claims"]).literature_claims(con, k.get("topic")),
}


def _normalize(obj):
    return json.loads(json.dumps(obj, default=str, sort_keys=True))


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE documents (id VARCHAR, title VARCHAR, source_type VARCHAR, "
        "venue VARCHAR, concept VARCHAR, citations INTEGER, refs VARCHAR, created_at TIMESTAMP)"
    )
    con.executemany(
        "INSERT INTO documents (id, title, source_type, venue, concept, citations, refs, created_at) VALUES (?,?,?,?,?,?,?,?)",
        [
            ("a", "Climate grid resilience", "paper", "Nature", "climate", 100, "b,c", "2025-06-01"),
            ("b", "Renewable energy policy", "paper", "Nature", "energy", 80, "c", "2025-06-02"),
            ("c", "Grid stability under load", "paper", "arXiv", "grid", 12, "", "2025-06-03"),
        ],
    )
    con.execute(
        "CREATE TABLE argument_claims (claim_id VARCHAR, claim_text VARCHAR, document_id VARCHAR, "
        "source_type VARCHAR, confidence DOUBLE, factcheck_verdict VARCHAR, attributed BOOLEAN)"
    )
    con.executemany(
        "INSERT INTO argument_claims VALUES (?,?,?,?,?,?,?)",
        [
            ("k1", "The grid handled peak load without failure.", "c", "paper", 0.9, None, True),
            ("k2", "Renewable subsidies raised deployment.", "b", "paper", 0.8, None, False),
        ],
    )
    con.close()
    monkeypatch.setenv("NEURONEWS_DB_PATH", str(db))
    monkeypatch.setenv("NOESIS_DB_PATH", str(db))
    return db


def _load_research():
    path = REPO / "tools/research_mcp/server.py"
    spec = importlib.util.spec_from_file_location("research_equiv_srv", path)
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


def test_every_research_panel_has_a_mirrored_data_mode_tool(warehouse):
    module = _load_research()
    metas = _tool_meta(module)
    for tool, panel_type, _ in CASES:
        assert tool in metas, f"{tool} not served"
        meta = metas[tool]
        assert isinstance(meta.get("data"), dict), f"{tool} missing meta.data block"
        assert meta["data"]["panel"] == panel_type
        assert meta.get("panel", {}).get("type") == panel_type


@pytest.mark.parametrize("tool,panel_type,kwargs", CASES, ids=[c[1] for c in CASES])
def test_data_mode_payload_matches_read_path(warehouse, tool, panel_type, kwargs):
    module = _load_research()
    via_tool = _call(module, tool, kwargs)
    con = duckdb.connect(str(warehouse), read_only=True)
    try:
        via_read_path = READ_PATH[tool](con, kwargs)
    finally:
        con.close()
    assert "error" not in via_tool, via_tool
    assert _normalize(via_tool) == _normalize(via_read_path), (
        f"{tool} data-mode payload diverges from the read path"
    )
