"""Caller identity from fastmcp's ``get_access_token`` (MC02, #2753).

The knowledge-engine, OSINT and on-chain servers read the caller through
``fastmcp.server.dependencies.get_access_token``. The function keeps its
import path, signature and ``AccessToken`` fields (``client_id``, ``scopes``)
on fastmcp 3, so the servers call it directly. These tests drive the real
function through the MCP SDK's auth context (as the Streamable HTTP bearer
middleware does) instead of monkeypatching it, so a fastmcp upgrade that
changes how the token is resolved fails here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from tools.knowledge_engine_mcp import server as knowledge_engine

ROOT = Path(__file__).resolve().parents[3]


def _load(relative: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def osint():
    return _load("tools/osint_mcp/server.py", "osint_mcp_access_token_under_test")


@pytest.fixture(scope="module")
def onchain():
    return _load("tools/onchain_mcp/server.py", "onchain_mcp_access_token_under_test")


@pytest.fixture()
def stdio_env(monkeypatch):
    for name in ("MCP_TRANSPORT", "MCP_PRINCIPAL", "MCP_SCOPES"):
        monkeypatch.delenv(f"NOESIS_{name}", raising=False)
        monkeypatch.delenv(f"NEURONEWS_{name}", raising=False)
    monkeypatch.setenv("NOESIS_MCP_PRINCIPAL", "env-reader")
    monkeypatch.setenv("NOESIS_MCP_SCOPES", "knowledge:read")


@pytest.fixture()
def bearer():
    """Install an authenticated caller the way the HTTP bearer middleware does."""
    tokens = []

    def install(client_id: str, scopes: list[str]):
        user = AuthenticatedUser(
            AccessToken(token="t" * 32, client_id=client_id, scopes=scopes)
        )
        tokens.append(auth_context_var.set(user))

    yield install
    for token in reversed(tokens):
        auth_context_var.reset(token)


def _contexts(osint, onchain):
    return {
        "knowledge_engine": knowledge_engine._context,
        "osint": osint._context,
        "onchain": onchain._context,
    }


def test_fastmcp_get_access_token_keeps_its_import_path_and_fields(bearer):
    from fastmcp.server.dependencies import get_access_token

    assert get_access_token() is None
    bearer("alice", ["knowledge:read"])
    token = get_access_token()
    assert token.client_id == "alice"
    assert list(token.scopes) == ["knowledge:read"]


@pytest.mark.parametrize("server", ["knowledge_engine", "osint", "onchain"])
def test_authenticated_caller_uses_token_identity(server, osint, onchain, stdio_env, bearer):
    bearer("alice", ["knowledge:read", "knowledge:onchain:read"])
    principal, scopes = _contexts(osint, onchain)[server]()
    assert principal == "alice"
    assert scopes == {"knowledge:read", "knowledge:onchain:read"}


@pytest.mark.parametrize("server", ["knowledge_engine", "osint", "onchain"])
def test_unauthenticated_stdio_caller_uses_configured_principal(server, osint, onchain, stdio_env):
    assert _contexts(osint, onchain)[server]() == ("env-reader", {"knowledge:read"})


def test_intake_context_on_http_denies_unauthenticated_callers(stdio_env, monkeypatch):
    monkeypatch.setenv("NOESIS_MCP_TRANSPORT", "http")
    monkeypatch.setenv("NOESIS_MCP_SCOPES", "operator")
    assert knowledge_engine._intake_context() == ("", set())


def test_intake_context_uses_authenticated_token_even_without_scopes(stdio_env, monkeypatch, bearer):
    monkeypatch.setenv("NOESIS_MCP_TRANSPORT", "http")
    bearer("bob", [])
    assert knowledge_engine._intake_context() == ("bob", set())


def test_insufficient_scope_is_unauthorized(stdio_env, bearer):
    bearer("alice", ["knowledge:read"])
    result = knowledge_engine._intake_safe(
        lambda conn: pytest.fail("operation must not run"),
        required_scope="knowledge:intake:read",
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "unauthorized"
