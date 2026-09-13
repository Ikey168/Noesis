"""MCP resource and prompt discovery retain intake owner checks."""

import asyncio
import json

import duckdb
import pytest
from fastmcp import Client, FastMCP

from src.kb.intake_modes import IntakeError, IntakeStore
from tools.knowledge_engine_mcp.intake import register


def test_discovered_session_resources_and_prompt_recheck_current_access(tmp_path):
    path = str(tmp_path / "intake.duckdb")
    scopes = {
        "knowledge:intake:read", "knowledge:intake:write",
        "namespace:research:read", "namespace:research:write",
    }
    conn = duckdb.connect(path)
    created = IntakeStore(conn, now=lambda: 1000).create(
        "research", "Exploration", "resource-1", intent="Find related sources",
        principal_id="alice", scopes=scopes,
    )
    conn.close()
    caller = {"id": "alice", "scopes": scopes}
    mcp = FastMCP("test-intake-resources")

    def context():
        return caller["id"], caller["scopes"]

    def safe(operation, *, write=False, required_scope=None):
        if required_scope not in caller["scopes"]:
            return {"ok": False, "error": {"code": "unauthorized",
                "message": f"{required_scope} scope is required"}}
        conn = duckdb.connect(path, read_only=not write)
        try:
            return operation(conn)
        except IntakeError as exc:
            return {"ok": False, "error": {"code": getattr(exc, "code", "error"),
                "message": str(exc)}}
        finally:
            conn.close()

    register(mcp, safe, context)

    async def exercise():
        async with Client(mcp) as client:
            templates = await client.list_resource_templates()
            uris = {str(value.uriTemplate) for value in templates}
            assert "noesis://intake/{namespace}/{session_id}" in uris
            assert "noesis://intake/{namespace}/{session_id}/revisions/{revision}" in uris
            uri = f"noesis://intake/research/{created['session_id']}"
            latest = await client.read_resource(uri)
            assert json.loads(latest[0].text)["session_id"] == created["session_id"]
            exact = await client.read_resource(uri + "/revisions/1")
            assert json.loads(exact[0].text)["revision"] == 1
            prompts = await client.list_prompts()
            assert "start-information-intake" in {item.name for item in prompts}
            prompt = await client.get_prompt("start-information-intake", {
                "mode": "Exploration", "namespace": "research", "intent": "Follow a lead",
            })
            assert "discover_intake_modes" in prompt.messages[0].content.text
            caller["id"] = "bob"
            with pytest.raises(Exception, match="current owner"):
                await client.read_resource(uri)
            caller["id"] = "alice"
            caller["scopes"] = {"namespace:research:read"}
            with pytest.raises(Exception, match="knowledge:intake:read"):
                await client.read_resource(uri)

    asyncio.run(exercise())
