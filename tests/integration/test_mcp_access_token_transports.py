"""Authenticated, unauthenticated and under-scoped callers on both MCP transports.

MC02 (#2753): the knowledge-engine server resolves its caller through
fastmcp's ``get_access_token`` on Streamable HTTP and through
``NOESIS_MCP_PRINCIPAL`` / ``NOESIS_MCP_SCOPES`` on stdio, which carries no
bearer credential. Both paths must keep the same outcomes across fastmcp
majors.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[2]
SERVER = "tools/knowledge_engine_mcp/server.py"
INTAKE_SCOPES = [
    "knowledge:intake:read",
    "knowledge:intake:write",
    "namespace:research:read",
    "namespace:research:write",
]
ALICE = "a" * 32
CAROL = "c" * 32
ARGS = {"namespace": "research"}
START = {
    "namespace": "research",
    "mode": "Exploration",
    "request_key": "access-token-trail",
    "intent": "Browse",
}


def _structured(result):
    assert not result.isError, result
    return result.structuredContent


async def _http_call(url, token, name, arguments):
    async with (
        httpx.AsyncClient(headers={"Authorization": "Bearer " + token}) as http_client,
        streamable_http_client(url, http_client=http_client) as (reader, writer, _),
        ClientSession(reader, writer) as client,
    ):
        await client.initialize()
        return _structured(await client.call_tool(name, arguments))


def test_http_bearer_tokens_authenticate_and_scope_callers(tmp_path):
    tokens = tmp_path / "tokens.json"
    tokens.write_text(
        json.dumps(
            {
                ALICE: {"client_id": "alice", "scopes": INTAKE_SCOPES},
                CAROL: {"client_id": "carol", "scopes": ["knowledge:read"]},
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
        [sys.executable, SERVER],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    async def journey():
        for _ in range(150):
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

        # Authenticated with the required scope: served as the token's client.
        created = await _http_call(url, ALICE, "start_intake_mode", START)
        assert created["owner"] == "alice"
        listed = await _http_call(url, ALICE, "list_intake_modes", ARGS)
        assert [item["owner"] for item in listed["sessions"]] == ["alice"]

        # Authenticated but under-scoped: the tool refuses; the env operator
        # scopes are never substituted for the token's.
        for name, arguments in (("list_intake_modes", ARGS), ("start_intake_mode", START)):
            denied = await _http_call(url, CAROL, name, arguments)
            assert denied["error"]["code"] == "unauthorized", denied

        # Unauthenticated and wrong-token requests never reach a tool.
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "anonymous", "version": "0"},
            },
        }
        headers = {"Accept": "application/json, text/event-stream"}
        async with httpx.AsyncClient() as http_client:
            anonymous = await http_client.post(url, json=initialize, headers=headers)
            assert anonymous.status_code == 401
            forged = await http_client.post(
                url,
                json=initialize,
                headers={**headers, "Authorization": "Bearer " + "f" * 32},
            )
            assert forged.status_code == 401

    try:
        asyncio.run(journey())
    finally:
        process.terminate()
        process.wait(timeout=5)


def _stdio(db_path, scopes):
    env = dict(os.environ)
    env.pop("NOESIS_MCP_TRANSPORT", None)
    env.update(
        {
            "NOESIS_DB_PATH": str(db_path),
            "NOESIS_MCP_PRINCIPAL": "alice",
            "NOESIS_MCP_SCOPES": scopes,
        }
    )
    return StdioServerParameters(
        command=sys.executable, args=[SERVER], cwd=ROOT, env=env
    )


def test_stdio_callers_are_scoped_by_the_configured_principal(tmp_path):
    """stdio carries no bearer token: the operator-configured principal applies."""

    async def call(scopes, name, arguments):
        async with (
            stdio_client(_stdio(tmp_path / "intake.duckdb", scopes)) as (reader, writer),
            ClientSession(reader, writer) as client,
        ):
            await client.initialize()
            return _structured(await client.call_tool(name, arguments))

    async def journey():
        granted = ",".join(INTAKE_SCOPES)
        created = await call(granted, "start_intake_mode", START)
        assert created["owner"] == "alice"
        listed = await call(granted, "list_intake_modes", ARGS)
        assert [item["owner"] for item in listed["sessions"]] == ["alice"]
        for name, arguments in (("list_intake_modes", ARGS), ("start_intake_mode", START)):
            denied = await call("knowledge:read", name, arguments)
            assert denied["error"]["code"] == "unauthorized", denied

    asyncio.run(journey())
