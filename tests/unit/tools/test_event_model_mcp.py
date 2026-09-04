from __future__ import annotations

import asyncio
import inspect

import duckdb

from tools.knowledge_engine_mcp import server


def _call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def test_event_model_mcp_lifecycle_accounts_search_diff_and_replay(
    tmp_path, monkeypatch
):
    database = tmp_path / "events.duckdb"
    scopes = {"knowledge:event:read"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(database))
    )
    tools = asyncio.run(server.mcp.get_tools())
    event_input = {
        "event_type": "policy-decision",
        "participants": ["agency:1"],
        "location": {"country": "DE"},
        "time": {"start_ms": 10, "end_ms": 10},
        "evidence": [{"source_revision_id": "document-revision:1"}],
    }
    denied = _call(
        tools["create_event_record"], namespace="political", event=event_input
    )
    assert denied["error"]["code"] == "unauthorized"
    scopes.update({"knowledge:event:write", "knowledge:event:review"})
    event = _call(
        tools["create_event_record"],
        namespace="political",
        event=event_input,
        event_key="decision-1",
        generation=5,
    )
    account = _call(
        tools["attach_event_account"],
        namespace="political",
        event_id=event["event_id"],
        attribute_type="quantity",
        value={"value": 5, "unit": "percent"},
        role="rate-change",
        evidence=[{"citation": "decision:1"}],
    )
    assert account["value"]["normalized_value"] == 0.05
    revised = _call(
        tools["revise_event_record"],
        namespace="political",
        event_id=event["event_id"],
        expected_revision=1,
        patch={"time": {"start_ms": 9, "end_ms": 10}},
        reason="The official publication clarified the start time.",
        lifecycle="completed",
    )
    assert revised["revision"] == 2
    found = _call(
        tools["search_event_records"],
        namespace="political",
        lifecycles=["completed"],
        snapshot_generation=5,
    )
    assert found["items"][0]["event_id"] == event["event_id"]
    diff = _call(
        tools["diff_event_revisions"],
        namespace="political",
        event_id=event["event_id"],
        from_revision=1,
        to_revision=2,
    )
    assert "time" in diff["changes"]
    replay = _call(
        tools["replay_event_record"],
        namespace="political",
        event_id=event["event_id"],
    )
    assert replay["deterministic"] is True
    assert (
        _call(
            tools["get_event_record"],
            namespace="scientific",
            event_id=event["event_id"],
        )
        is None
    )
