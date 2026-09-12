"""A real MCP client journey through the registered stdio server."""

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[2]


def _server(db_path, scopes):
    env = dict(os.environ)
    env.update(
        {
            "NOESIS_DB_PATH": str(db_path),
            "NOESIS_MCP_PRINCIPAL": "alice",
            "NOESIS_MCP_SCOPES": scopes,
        }
    )
    return StdioServerParameters(
        command=sys.executable,
        args=["tools/knowledge_engine_mcp/server.py"],
        cwd=ROOT,
        env=env,
    )


async def _call(session, name, arguments):
    result = await session.call_tool(name, arguments)
    assert not result.isError, result
    assert isinstance(result.structuredContent, dict), result
    return result.structuredContent


def test_awareness_to_exploration_survives_server_restart(tmp_path):
    db_path = tmp_path / "intake.duckdb"
    scopes = "knowledge:intake:read,knowledge:intake:write,namespace:research:read,namespace:research:write"

    async def journey():
        async with (
            stdio_client(_server(db_path, scopes)) as (reader, writer),
            ClientSession(reader, writer) as client,
        ):
            await client.initialize()
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert {
                "start_intake_mode",
                "command_intake_mode",
                "export_intake_mode",
            } <= names
            awareness = await _call(
                client,
                "start_intake_mode",
                {
                    "namespace": "research",
                    "mode": "Awareness",
                    "request_key": "daily-queue",
                    "intent": "Scan the daily feed",
                    "inputs": {"feed_item_ids": ["feed:one"]},
                    "workspace_links": [
                        {
                            "system": "modulo",
                            "workspace_id": "home",
                            "kind": "intake_item",
                            "id": "item-1",
                            "version": 1,
                        }
                    ],
                },
            )
            assert awareness["workspace_links"][0]["id"] == "item-1"
            assert awareness["unmet_completion_checks"] == ["all_feed_items_decided"]
            triaged = await _call(
                client,
                "command_intake_mode",
                {
                    "namespace": "research",
                    "session_id": awareness["session_id"],
                    "command_key": "triage-one",
                    "expected_revision": 1,
                    "action": "record",
                    "payload": {"data": {"decisions": {"feed:one": "escalate"}}},
                },
            )
            assert triaged["unmet_completion_checks"] == []
            exploration = await _call(
                client,
                "start_intake_mode",
                {
                    "namespace": "research",
                    "mode": "Exploration",
                    "request_key": "explore-one",
                    "intent": "Follow the signal",
                    "origin": {
                        "session_id": awareness["session_id"],
                        "reason": "Escalated feed item",
                    },
                },
            )
            assert exploration["origin"]["session_id"] == awareness["session_id"]
            exploration_id = exploration["session_id"]

        async with (
            stdio_client(_server(db_path, scopes)) as (reader, writer),
            ClientSession(reader, writer) as client,
        ):
            await client.initialize()
            resumed = await _call(
                client,
                "inspect_intake_mode",
                {
                    "namespace": "research",
                    "session_id": exploration_id,
                },
            )
            assert resumed["revision"] == 1
            recorded = await _call(
                client,
                "command_intake_mode",
                {
                    "namespace": "research",
                    "session_id": exploration_id,
                    "command_key": "promote",
                    "expected_revision": 1,
                    "action": "record",
                    "payload": {"data": {"escalation_reason": "Research topic"}},
                },
            )
            assert recorded["revision"] == 2
            replay = await _call(
                client,
                "command_intake_mode",
                {
                    "namespace": "research",
                    "session_id": exploration_id,
                    "command_key": "promote",
                    "expected_revision": 1,
                    "action": "record",
                    "payload": {"data": {"escalation_reason": "Research topic"}},
                },
            )
            assert replay["idempotent"] is True
            completed = await _call(
                client,
                "command_intake_mode",
                {
                    "namespace": "research",
                    "session_id": exploration_id,
                    "command_key": "complete",
                    "expected_revision": 2,
                    "action": "complete",
                },
            )
            assert completed["status"] == "completed"
            exported = await _call(
                client,
                "export_intake_mode",
                {
                    "namespace": "research",
                    "session_id": exploration_id,
                },
            )
            assert len(exported["revisions"]) == 3

        async with (
            stdio_client(_server(db_path, "knowledge:intake:read")) as (reader, writer),
            ClientSession(reader, writer) as client,
        ):
            await client.initialize()
            denied = await _call(
                client,
                "inspect_intake_mode",
                {
                    "namespace": "research",
                    "session_id": exploration_id,
                },
            )
            assert denied["error"]["code"] == "unauthorized"

    asyncio.run(journey())
