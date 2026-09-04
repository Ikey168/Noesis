from __future__ import annotations

import asyncio
import inspect

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def call(t, **k):
    v = t.fn(**k)
    return asyncio.run(v) if inspect.isawaitable(v) else v


def test_change_brief_mcp_end_to_end(tmp_path, monkeypatch):
    db = tmp_path / "briefs.duckdb"
    scopes = {"knowledge:briefs:read"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(db))
    )
    tools = asyncio.run(server.mcp.get_tools())
    names = {
        "register_change_brief_policy",
        "preview_semantic_change",
        "generate_change_brief",
        "get_change_brief",
        "list_change_briefs",
        "compare_change_briefs",
        "replay_change_brief",
        "create_change_brief_subscription",
        "deliver_change_briefs",
        "acknowledge_change_brief_delivery",
        "review_change_brief",
        "export_change_briefs",
    }
    assert names <= tools.keys()
    assert (
        call(
            tools["register_change_brief_policy"],
            namespace="political",
            policy_id="p",
            version="1",
        )["error"]["code"]
        == "unauthorized"
    )
    scopes.add("knowledge:briefs:write")
    p = call(
        tools["register_change_brief_policy"],
        namespace="political",
        policy_id="p",
        version="1",
    )
    preview = call(
        tools["preview_semantic_change"],
        namespace="political",
        object_type="claim",
        object_id="c",
        before="old",
        after="new",
        from_generation=1,
        to_generation=2,
        evidence_before=[{"citation": "a"}],
        evidence_after=[{"citation": "b"}],
    )
    b = call(
        tools["generate_change_brief"],
        namespace="political",
        policy_revision_id=p["policy_revision_id"],
        preview=preview,
    )
    assert call(
        tools["replay_change_brief"], namespace="political", brief_id=b["brief_id"]
    )["deterministic"]
    sub = call(
        tools["create_change_brief_subscription"],
        namespace="political",
        subscriber_id="a",
        window_ms=1000,
        filters={},
    )
    assert (
        call(
            tools["deliver_change_briefs"],
            namespace="political",
            subscription_id=sub["subscription_id"],
            window_start_ms=0,
            window_end_ms=999999,
        )["error"]["code"]
        == "unauthorized"
    )
    scopes.add("knowledge:briefs:deliver")
    d = call(
        tools["deliver_change_briefs"],
        namespace="political",
        subscription_id=sub["subscription_id"],
        window_start_ms=0,
        window_end_ms=999999,
    )
    assert (
        call(
            tools["acknowledge_change_brief_delivery"],
            namespace="political",
            delivery_id=d["delivery_id"],
        )["status"]
        == "acknowledged"
    )
    scopes.add("knowledge:briefs:review")
    assert (
        call(
            tools["review_change_brief"],
            namespace="political",
            brief_id=b["brief_id"],
            rating="useful",
            reason="x",
        )["rating"]
        == "useful"
    )
    assert call(
        tools["export_change_briefs"], namespace="political", brief_ids=[b["brief_id"]]
    )["dependency_complete"]


def test_change_brief_catalog():
    assert _mutability("deliver_change_briefs") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "deliver_change_briefs"
    ) == ["knowledge:briefs:deliver"]
    assert _required_scopes(
        "knowledge_engine_mcp", "read", "preview_semantic_change"
    ) == ["knowledge:briefs:read"]
    assert (
        "noesis-change-brief-v1"
        in server.knowledge_engine_capabilities.fn()["contracts"]
    )
