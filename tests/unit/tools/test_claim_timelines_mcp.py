from __future__ import annotations

import asyncio
import inspect

import duckdb

from src.database.local_warehouse_seed import ensure_schema
from tools.knowledge_engine_mcp import server


def _call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def test_claim_timeline_mcp_state_detection_lineage_diff_timeline_replay_and_auth(
    tmp_path, monkeypatch
):
    database = tmp_path / "claims.duckdb"
    conn = duckdb.connect(str(database))
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO argument_claims(claim_id,claim_text,document_id,source_type) VALUES "
        "('old','GDP grew 2 percent','doc:old','news'),"
        "('new','Economic output increased 3 percent','doc:new','news')"
    )
    conn.close()
    scopes = {"knowledge:claim-timeline:read"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(database))
    )
    tools = asyncio.run(server.mcp.get_tools())
    expected = {
        "capture_claim_timeline_state",
        "link_claim_evolution",
        "get_claim_timeline_state",
        "detect_claim_successors",
        "diff_claim_timeline_states",
        "get_claim_evolution_timeline",
        "compare_claim_sources",
        "replay_claim_evolution",
    }
    assert expected <= tools.keys()
    denied = _call(
        tools["capture_claim_timeline_state"],
        namespace="economic",
        claim_id="old",
        source_id="source:a",
        source_revision_id="revision:old",
        evidence=[{"citation": "old:1"}],
    )
    assert denied["error"]["code"] == "unauthorized"
    scopes.add("knowledge:claim-timeline:write")
    for claim_id, source in (("old", "source:a"), ("new", "source:b")):
        _call(
            tools["capture_claim_timeline_state"],
            namespace="economic",
            claim_id=claim_id,
            source_id=source,
            source_revision_id=f"revision:{claim_id}",
            evidence=[{"citation": f"{claim_id}:1"}],
            stance="supports",
            certainty=0.8,
            observed_at_ms=100 if claim_id == "old" else 200,
        )
    matches = _call(
        tools["detect_claim_successors"],
        namespace="economic",
        claim_id="old",
        candidate_claim_ids=["new"],
        threshold=0.4,
    )
    assert matches["matches"][0]["relation"] == "refinement"
    edge = _call(
        tools["link_claim_evolution"],
        namespace="economic",
        predecessor_claim_id="old",
        successor_claim_id="new",
        relation="refinement",
        confidence=matches["matches"][0]["score"],
        evidence=[{"citation": "new:1"}],
        explanation=matches["matches"][0]["explanation"],
        method={"kind": "deterministic"},
    )
    assert edge["successor_claim_id"] == "new"
    timeline = _call(
        tools["get_claim_evolution_timeline"],
        namespace="economic",
        claim_id="old",
    )
    assert [item["claim_id"] for item in timeline["items"]] == ["old", "new"]
    diff = _call(
        tools["diff_claim_timeline_states"],
        namespace="economic",
        left_claim_id="old",
        right_claim_id="new",
    )
    assert "wording" in diff["changes"]
    replay = _call(
        tools["replay_claim_evolution"],
        namespace="economic",
        claim_id="old",
    )
    assert replay["deterministic"] and replay["citation_closed"]


def test_claim_timeline_capabilities_advertise_contracts_and_features():
    capabilities = server.knowledge_engine_capabilities.fn()
    assert {
        "noesis-claim-state-v1",
        "noesis-claim-lineage-v1",
        "noesis-claim-successor-match-v1",
        "noesis-claim-timeline-v1",
        "noesis-claim-semantic-diff-v1",
    } <= set(capabilities["contracts"])
    assert {
        "claim-evolution-lineage",
        "explainable-claim-successor-matching",
        "semantic-claim-state-diffs",
        "snapshot-consistent-claim-timelines",
    } <= set(capabilities["features"])
