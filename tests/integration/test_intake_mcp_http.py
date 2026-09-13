"""Per-caller isolation over the real Streamable HTTP MCP transport."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

ROOT = Path(__file__).resolve().parents[2]


async def _tool(url, token, name, arguments):
    async with (
        httpx.AsyncClient(headers={"Authorization": "Bearer " + token}) as http_client,
        streamable_http_client(url, http_client=http_client) as (reader, writer, _),
        ClientSession(reader, writer) as client,
    ):
        await client.initialize()
        result = await client.call_tool(name, arguments)
        assert not result.isError, result
        return result.structuredContent


def test_http_tokens_isolate_intake_sessions(tmp_path):
    tokens = tmp_path / "tokens.json"
    scopes = [
        "knowledge:intake:read",
        "knowledge:intake:write",
        "namespace:research:read",
        "namespace:research:write",
    ]
    tokens.write_text(
        json.dumps(
            {
                "a" * 32: {"client_id": "alice", "scopes": scopes},
                "b" * 32: {"client_id": "bob", "scopes": scopes},
            }
        )
    )
    tokens.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(os.environ)
    env.pop("NOESIS_MCP_AUTH_TOKEN", None)
    env.pop("NEURONEWS_MCP_AUTH_TOKEN", None)
    env.update(
        {
            "NOESIS_MCP_TRANSPORT": "http",
            "NOESIS_MCP_HTTP_HOST": "127.0.0.1",
            "NOESIS_MCP_HTTP_PORT": str(port),
            "NOESIS_MCP_AUTH_TOKENS_FILE": str(tokens),
            "NOESIS_DB_PATH": str(tmp_path / "intake.duckdb"),
            "NOESIS_MCP_PRINCIPAL": "unsafe-env-operator",
            "NOESIS_MCP_SCOPES": "operator",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "tools/knowledge_engine_mcp/server.py"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    async def journey():
        for _ in range(100):
            if process.poll() is not None:
                raise AssertionError("MCP HTTP server exited before readiness")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                await asyncio.sleep(0.1)
        else:
            raise AssertionError("MCP HTTP server did not become ready")
        url = f"http://127.0.0.1:{port}/mcp"
        created = await _tool(
            url,
            "a" * 32,
            "start_intake_mode",
            {
                "namespace": "research",
                "mode": "Exploration",
                "request_key": "alice-trail",
                "intent": "Browse",
            },
        )
        assert created["owner"] == "alice"
        readiness = await _tool(url, "a" * 32, "preflight_intake_mode", {
            "namespace": "research", "mode": "Exploration",
        })
        assert readiness["contract"] == "noesis-intake-readiness-v1"
        assert readiness["modes"][0]["native_start_possible"] is True
        assert readiness["modes"][0]["complete_journey_ready"] is False
        denied_readiness = await _tool(url, "a" * 32, "preflight_intake_mode", {
            "namespace": "archive",
        })
        assert denied_readiness["error"]["code"] == "unauthorized"
        uri = AnyUrl(f"noesis://intake/research/{created['session_id']}")
        pack = await _tool(url, "a" * 32, "create_practice_pack", {
            "namespace": "research", "request_key": "alice-pack", "title": "Recall index repair",
            "cards": [{"kind": "recall", "prompt": "What stopped indexing?",
                       "answer": "The index worker", "mastery_criterion": "Name the worker",
                       "references": [{"kind": "concept", "id": "concept:worker",
                                       "namespace": "research", "version": 1}]}],
        })
        review = await _tool(url, "a" * 32, "start_practice_review", {
            "namespace": "research", "pack_id": pack["pack_id"],
            "card_id": "card-1", "request_key": "alice-review",
        })
        pack_uri = AnyUrl(f"noesis://intake/practice/research/{pack['pack_id']}")
        review_uri = AnyUrl(f"noesis://intake/practice-reviews/research/{review['review_id']}")
        async with (
            httpx.AsyncClient(headers={"Authorization": "Bearer " + "a" * 32}) as http_client,
            streamable_http_client(url, http_client=http_client) as (reader, writer, _),
            ClientSession(reader, writer) as client,
        ):
            await client.initialize()
            templates = await client.list_resource_templates()
            assert any("{session_id}" in str(item.uriTemplate) for item in templates.resourceTemplates)
            assert any("playbooks/{namespace}/{playbook_id}" in str(item.uriTemplate)
                       for item in templates.resourceTemplates)
            assert any("practice-reviews/{namespace}/{review_id}" in str(item.uriTemplate)
                       for item in templates.resourceTemplates)
            resource = await client.read_resource(uri)
            assert json.loads(resource.contents[0].text)["owner"] == "alice"
            pack_resource = await client.read_resource(pack_uri)
            assert json.loads(pack_resource.contents[0].text)["cards"][0]["answer"] == "The index worker"
            review_resource = await client.read_resource(review_uri)
            assert "answer" not in json.loads(review_resource.contents[0].text)
            prompts = await client.list_prompts()
            assert "start-information-intake" in {item.name for item in prompts.prompts}
            prompt = await client.get_prompt("start-information-intake", {
                "mode": "Exploration", "namespace": "research", "intent": "Browse",
            })
            assert "discover_intake_modes" in prompt.messages[0].content.text
        async with (
            httpx.AsyncClient(headers={"Authorization": "Bearer " + "b" * 32}) as http_client,
            streamable_http_client(url, http_client=http_client) as (reader, writer, _),
            ClientSession(reader, writer) as client,
        ):
            await client.initialize()
            with pytest.raises(McpError, match="current owner"):
                await client.read_resource(uri)
            with pytest.raises(McpError, match="current owner"):
                await client.read_resource(pack_uri)
            with pytest.raises(McpError, match="current owner"):
                await client.read_resource(review_uri)
        attempted = await _tool(url, "a" * 32, "command_practice_review", {
            "namespace": "research", "review_id": review["review_id"],
            "command_key": "alice-attempt", "expected_revision": 1,
            "action": "attempt", "payload": {"answer": "A worker", "assisted": False},
        })
        assert "answer" not in attempted
        await _tool(url, "a" * 32, "command_practice_review", {
            "namespace": "research", "review_id": review["review_id"],
            "command_key": "alice-reveal", "expected_revision": 2,
            "action": "reveal", "payload": {},
        })
        async with (
            httpx.AsyncClient(headers={"Authorization": "Bearer " + "a" * 32}) as http_client,
            streamable_http_client(url, http_client=http_client) as (reader, writer, _),
            ClientSession(reader, writer) as client,
        ):
            await client.initialize()
            current_review = await client.read_resource(review_uri)
            assert json.loads(current_review.contents[0].text)["answer"] == "The index worker"
            before_reveal = await client.read_resource(AnyUrl(
                f"noesis://intake/practice-reviews/research/{review['review_id']}/revisions/2"
            ))
            assert "answer" not in json.loads(before_reveal.contents[0].text)
        subscribed = await _tool(
            url,
            "a" * 32,
            "subscribe_intake_feed",
            {
                "namespace": "research",
                "url": "https://example.org/rss",
                "name": "Example",
            },
        )
        assert subscribed["subscription_id"].startswith("subscription:")
        bob_subscriptions = await _tool(
            url, "b" * 32, "list_intake_feed_subscriptions", {"namespace": "research"}
        )
        assert bob_subscriptions["subscriptions"] == []
        denied = await _tool(
            url,
            "b" * 32,
            "inspect_intake_mode",
            {
                "namespace": "research",
                "session_id": created["session_id"],
            },
        )
        assert denied["error"]["code"] == "unauthorized"
        handoff = await _tool(
            url,
            "a" * 32,
            "export_modulo_intake_handoff",
            {"namespace": "research", "session_id": created["session_id"]},
        )
        assert handoff["scope"]["owner"] == "alice"
        denied_handoff = await _tool(
            url,
            "b" * 32,
            "export_modulo_intake_handoff",
            {"namespace": "research", "session_id": created["session_id"]},
        )
        assert denied_handoff["error"]["code"] == "unauthorized"
        bob = await _tool(
            url,
            "b" * 32,
            "start_intake_mode",
            {
                "namespace": "research",
                "mode": "Exploration",
                "request_key": "bob-trail",
                "intent": "Browse",
            },
        )
        assert bob["owner"] == "bob" and bob["session_id"] != created["session_id"]
        listed = await _tool(
            url, "a" * 32, "list_intake_modes", {"namespace": "research"}
        )
        assert [session["owner"] for session in listed["sessions"]] == ["alice"]
        unrelated = await _tool(
            url,
            "b" * 32,
            "create_research_project",
            {
                "namespace": "research",
                "request_key": "unauthorized",
                "questions": ["Why?"],
                "success_criteria": ["Evidence"],
                "scope": {"domains": [], "namespaces": ["research"]},
                "budget": {},
            },
        )
        assert unrelated["error"]["code"] == "unauthorized"

    try:
        asyncio.run(journey())
    finally:
        process.terminate()
        process.wait(timeout=5)
