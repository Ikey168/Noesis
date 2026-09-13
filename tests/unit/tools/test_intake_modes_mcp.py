import asyncio

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def test_intake_public_tools_and_access(tmp_path, monkeypatch):
    path = str(tmp_path / "intake.duckdb")
    scopes = {
        "knowledge:intake:read",
        "knowledge:intake:write",
        "namespace:research:read",
        "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    tools = asyncio.run(server.mcp.get_tools())
    assert len(tools["discover_intake_modes"].fn()["modes"]) == 10
    assert (
        tools["route_intake_mode"].fn(answers={"decision_needed": True})["mode"]
        == "Decision Support"
    )
    created = tools["start_intake_mode"].fn(
        namespace="research",
        mode="Exploration",
        request_key="curiosity",
        intent="Browse",
        inputs={},
    )
    identity = {"namespace": "research", "session_id": created["session_id"]}
    assert tools["inspect_intake_mode"].fn(**identity)["revision"] == 1
    assert (
        tools["list_intake_modes"].fn(namespace="research")["sessions"][0]["session_id"]
        == created["session_id"]
    )
    assert (
        tools["command_intake_mode"].fn(
            **identity,
            command_key="end",
            expected_revision=1,
            action="record",
            payload={"data": {"escalation_reason": "Research this"}},
        )["revision"]
        == 2
    )
    assert (
        tools["command_intake_mode"].fn(
            **identity, command_key="done", expected_revision=2, action="complete"
        )["status"]
        == "completed"
    )
    assert len(tools["export_intake_mode"].fn(**identity)["revisions"]) == 3
    assert tools["verify_intake_mode_export"].fn(
        bundle=tools["export_intake_mode"].fn(**identity)
    )["valid"]
    assert (
        tools["export_modulo_intake_handoff"].fn(**identity)["session"]["id"]
        == created["session_id"]
    )
    scopes.remove("namespace:research:read")
    assert (
        tools["inspect_intake_mode"].fn(**identity)["error"]["code"] == "unauthorized"
    )
    assert _mutability("command_intake_mode") == "write"
    assert _required_scopes("knowledge_engine_mcp", "write", "command_intake_mode") == [
        "knowledge:intake:write"
    ]
    assert _required_scopes("knowledge_engine_mcp", "read", "inspect_intake_mode") == [
        "knowledge:intake:read"
    ]
    assert _required_scopes(
        "knowledge_engine_mcp", "read", "export_modulo_intake_handoff"
    ) == ["knowledge:intake:read"]


def test_awareness_mcp_uses_persistent_feed_inbox(tmp_path, monkeypatch):
    from src.kb import intake_inbox

    path = str(tmp_path / "inbox.duckdb")
    scopes = {
        "knowledge:intake:read",
        "knowledge:intake:write",
        "knowledge:intake:fetch",
        "namespace:research:read",
        "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    monkeypatch.setattr(
        intake_inbox,
        "_fetch_public_feed",
        lambda _: (
            b'<rss version="2.0"><channel><item><title>One</title><link>https://example.org/one</link></item></channel></rss>'
        ),
    )
    tools = asyncio.run(server.mcp.get_tools())
    subscription = tools["subscribe_intake_feed"].fn(
        namespace="research", url="https://example.org/rss", name="Example"
    )
    assert subscription["subscription_id"].startswith("subscription:")
    assert (
        tools["refresh_intake_feed_inbox"].fn(namespace="research")["results"][0][
            "created"
        ]
        == 1
    )
    page = tools["list_intake_feed_inbox"].fn(namespace="research")
    assert page["remaining_unprocessed"] == 1
    item_id = page["items"][0]["item_id"]
    session = tools["start_awareness_from_inbox"].fn(
        namespace="research", request_key="today"
    )
    triaged = tools["triage_awareness_item"].fn(
        namespace="research",
        session_id=session["session_id"],
        item_id=item_id,
        command_key="discard-one",
        expected_revision=1,
        decision="discard",
    )
    assert triaged["data"]["decisions"][item_id] == "discard"
    assert (
        tools["list_intake_feed_inbox"].fn(namespace="research")[
            "remaining_unprocessed"
        ]
        == 0
    )
    assert (
        tools["command_intake_mode"].fn(
            namespace="research",
            session_id=session["session_id"],
            command_key="finish",
            expected_revision=2,
            action="complete",
        )["status"]
        == "completed"
    )
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "refresh_intake_feed_inbox"
    ) == ["knowledge:intake:write", "knowledge:intake:fetch"]


def test_exploration_capture_over_mcp(tmp_path, monkeypatch):
    path = str(tmp_path / "exploration.duckdb")
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
    started = tools["start_intake_mode"].fn(
        namespace="research", mode="Exploration", request_key="curious", intent="Explore",
    )
    captured = tools["capture_exploration_page"].fn(
        namespace="research", session_id=started["session_id"],
        command_key="page-one", expected_revision=1,
        url="https://example.org/article", title="Article", note="Interesting",
        saved=True, content="Caller-provided readable content",
    )
    assert captured["data"]["trail"][0]["saved"] is True
    source_id = captured["references"][0]["id"]
    source = tools["inspect_exploration_source"].fn(
        namespace="research", source_id=source_id,
    )
    assert source["acquisition"] == "caller_supplied"
    assert source["content"] == "Caller-provided readable content"
    denied = tools["capture_exploration_page"].fn(
        namespace="research", session_id=started["session_id"],
        command_key="fetch", expected_revision=2,
        url="https://example.org/other", title="Other", fetch_readable=True,
    )
    assert denied["error"]["code"] == "unauthorized"
