"""Supported MCP discovery and calls expose the same practice state machine."""

import asyncio

import duckdb

from src.mcp_host.catalog import _required_scopes
from tools.knowledge_engine_mcp import server


def test_practice_public_mcp_round_trip_and_revocation(tmp_path, monkeypatch):
    path = str(tmp_path / "practice-mcp.duckdb")
    scopes = {
        "knowledge:intake:read", "knowledge:intake:write",
        "namespace:research:read", "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server, "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    tools = asyncio.run(server.mcp.get_tools())
    cards = [{
        "kind": "recall", "prompt": "What stopped?", "answer": "The worker",
        "mastery_criterion": "Recall without notes on three reviews",
        "references": [{"kind": "concept", "id": "concept:worker",
                        "namespace": "research", "version": 1}],
    }]
    pack = tools["create_practice_pack"].fn(
        namespace="research", request_key="pack", title="Worker", cards=cards,
    )
    assert pack["pack_id"].startswith("practice-pack:")
    assert tools["list_due_practice"].fn(namespace="research")["cards"][0][
        "prompt"] == "What stopped?"
    review = tools["start_practice_review"].fn(
        namespace="research", pack_id=pack["pack_id"],
        card_id="card-1", request_key="review",
    )
    assert "answer" not in review
    attempt = tools["command_practice_review"].fn(
        namespace="research", review_id=review["review_id"],
        command_key="attempt", expected_revision=1, action="attempt",
        payload={"answer": "The worker", "assisted": False},
    )
    assert "answer" not in attempt
    revealed = tools["command_practice_review"].fn(
        namespace="research", review_id=review["review_id"],
        command_key="reveal", expected_revision=2, action="reveal",
    )
    assert revealed["answer"] == "The worker"
    exported = tools["export_practice_pack"].fn(
        namespace="research", pack_id=pack["pack_id"],
    )
    assert tools["verify_practice_export"].fn(bundle=exported)["valid"]
    assert _required_scopes("knowledge_engine_mcp", "write",
                            "command_practice_review") == ["knowledge:intake:write"]
    scopes.remove("namespace:research:read")
    denied = tools["inspect_practice_review"].fn(
        namespace="research", review_id=review["review_id"],
    )
    assert denied["error"]["code"] == "unauthorized"
