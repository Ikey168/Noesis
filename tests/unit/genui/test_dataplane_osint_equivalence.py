"""M1.1 equivalence: every OSINT panel type is served by a data-mode tool whose
payload matches the existing read path (the src.osint composition function it
wraps), and whose data-mode annotation mirrors its panel annotation.

Loads the OSINT server by path, calls each data-mode tool over an in-memory
FastMCP client on a seeded temp warehouse, and asserts the structured payload is
equivalent to calling the underlying src.osint function directly. Skips where
fastmcp/duckdb are unavailable.
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

# (tool name, panel type, kwargs passed to the tool). The seed below makes each
# call return a non-trivial payload.
CASES = [
    ("corroborate", "corroboration", {"claim_id": "k1"}),
    ("source_reliability", "reliability_card", {"source": "Alpha Wire"}),
    ("contradiction_scan", "contradiction_ledger", {"topic": "energy"}),
    ("entity_dossier", "entity_dossier", {"entity": "Jordan Rivera"}),
    ("relationship_path", "relationship_path", {"a": "Jordan Rivera", "b": "Alex Poll"}),
    ("timeline_reconstruct", "evidence_timeline", {"topic": "flood"}),
    ("trace_artifact", "provenance_trace", {"claim_id": "k1"}),
]

# How each tool maps its kwargs onto the underlying src.osint function, so the
# direct read-path call is identical to what the tool makes.
READ_PATH = {
    "corroborate": lambda con, k: __import__("src.osint", fromlist=["corroborate"]).corroborate(con, k["claim_id"]),
    "source_reliability": lambda con, k: __import__("src.osint", fromlist=["source_reliability"]).source_reliability(con, k["source"]),
    "contradiction_scan": lambda con, k: __import__("src.osint", fromlist=["contradiction_scan"]).contradiction_scan(con, topic=k.get("topic"), entity=k.get("entity")),
    "entity_dossier": lambda con, k: __import__("src.osint", fromlist=["entity_dossier"]).entity_dossier(con, k["entity"], entity_type=k.get("entity_type")),
    "relationship_path": lambda con, k: __import__("src.osint", fromlist=["relationship_path"]).relationship_path(con, k["a"], k["b"]),
    "timeline_reconstruct": lambda con, k: __import__("src.osint", fromlist=["timeline_reconstruct"]).timeline_reconstruct(con, topic=k.get("topic"), entity=k.get("entity")),
    "trace_artifact": lambda con, k: __import__("src.osint", fromlist=["trace_artifact"]).trace_artifact(con, claim_id=k.get("claim_id"), document_id=k.get("document_id")),
}


def _normalize(obj):
    """JSON-normalize both sides so the comparison is about content, not
    serialization artifacts (tuples vs lists, etc.)."""
    return json.loads(json.dumps(obj, default=str, sort_keys=True))


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    db = tmp_path / "wh.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE news_articles (id VARCHAR, title VARCHAR, url VARCHAR, "
        "content VARCHAR, publish_date TIMESTAMP, source VARCHAR, category VARCHAR, "
        "sentiment_score DOUBLE, sentiment_label VARCHAR)"
    )
    con.executemany(
        "INSERT INTO news_articles (id, title, url, source, publish_date, category) VALUES (?,?,?,?,?,?)",
        [
            ("d1", "Severe flooding hits the delta", "http://a/1", "Alpha Wire", "2026-06-01", "energy"),
            ("d2", "Flood response under review", "http://b/1", "Beta Journal", "2026-06-02", "energy"),
            ("d3", "Summit statement", "http://c/1", "Gamma Review", "2026-06-03", "policy"),
        ],
    )
    con.execute(
        "CREATE TABLE argument_claims (claim_id VARCHAR, claim_text VARCHAR, "
        "document_id VARCHAR, source_type VARCHAR, confidence DOUBLE, factcheck_verdict VARCHAR)"
    )
    con.executemany(
        "INSERT INTO argument_claims VALUES (?,?,?,?,?,?)",
        [
            ("k1", "Severe flooding struck the delta in June.", "d1", "news", 0.9, None),
            ("k2", "The flood response was adequate, officials said.", "d2", "news", 0.8, None),
            ("k3", "Rivera addressed the summit.", "d3", "news", 0.7, None),
        ],
    )
    con.execute(
        "CREATE TABLE claim_evidence (evidence_id VARCHAR, claim_id VARCHAR, "
        "evidence_text VARCHAR, evidence_document_id VARCHAR, evidence_source_type VARCHAR, "
        "relation VARCHAR, similarity_score DOUBLE, found_at VARCHAR)"
    )
    con.executemany(
        "INSERT INTO claim_evidence (evidence_id, claim_id, evidence_document_id, "
        "evidence_source_type, relation, similarity_score) VALUES (?,?,?,?,?,?)",
        [("ev1", "k1", "d2", "news", "supports", 0.88)],
    )
    con.execute(
        "CREATE TABLE claim_conflicts (claim_id_a VARCHAR, claim_id_b VARCHAR, "
        "conflict_type VARCHAR, similarity_score DOUBLE, source_type_a VARCHAR, "
        "source_type_b VARCHAR, topic VARCHAR, computed_at VARCHAR)"
    )
    con.executemany(
        "INSERT INTO claim_conflicts (claim_id_a, claim_id_b, conflict_type, similarity_score, topic) VALUES (?,?,?,?,?)",
        [("k1", "k2", "contradicts", 0.7, "energy")],
    )
    con.execute(
        "CREATE TABLE outlet_scores (source VARCHAR, source_type VARCHAR, score_date VARCHAR, "
        "frame_diversity DOUBLE, attribution_rate DOUBLE, stance_neutrality DOUBLE, "
        "composite_score DOUBLE, doc_count INTEGER, claim_count INTEGER, computed_at VARCHAR)"
    )
    con.executemany(
        "INSERT INTO outlet_scores (source, source_type, score_date, frame_diversity, "
        "attribution_rate, stance_neutrality, composite_score) VALUES (?,?,?,?,?,?,?)",
        [("Alpha Wire", "outlet", "2026-06-01", 0.6, 0.7, 0.5, 0.62)],
    )
    con.execute(
        "CREATE TABLE document_actors (document_id VARCHAR, source_type VARCHAR, "
        "actor_name VARCHAR, entity_id VARCHAR, role VARCHAR, confidence DOUBLE, extracted_at VARCHAR)"
    )
    con.executemany(
        "INSERT INTO document_actors (document_id, actor_name, entity_id, role) VALUES (?,?,?,?)",
        [
            ("d3", "Jordan Rivera", "person:jr", "speaker"),
            ("d3", "Alex Poll", "person:ap", "attendee"),
        ],
    )
    con.close()
    monkeypatch.setenv("NEURONEWS_DB_PATH", str(db))
    monkeypatch.setenv("NOESIS_DB_PATH", str(db))
    return db


def _load_osint():
    path = REPO / "tools/osint_mcp/server.py"
    spec = importlib.util.spec_from_file_location("osint_equiv_srv", path)
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


def test_every_osint_panel_has_a_mirrored_data_mode_tool(warehouse):
    module = _load_osint()
    metas = _tool_meta(module)
    for tool, panel_type, _ in CASES:
        assert tool in metas, f"{tool} not served"
        meta = metas[tool]
        assert isinstance(meta.get("data"), dict), f"{tool} missing meta.data block"
        # The data-mode annotation names the same panel it is discovered as.
        assert meta["data"]["panel"] == panel_type
        assert meta.get("panel", {}).get("type") == panel_type


@pytest.mark.parametrize("tool,panel_type,kwargs", CASES, ids=[c[1] for c in CASES])
def test_data_mode_payload_matches_read_path(warehouse, tool, panel_type, kwargs):
    module = _load_osint()
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
