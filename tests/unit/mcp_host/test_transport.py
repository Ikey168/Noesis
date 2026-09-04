"""Unit tests for the shared MCP transport runner (#819)."""

from __future__ import annotations

import sys
import types

import pytest

from src.mcp_host.transport import (
    HOST_ENV,
    PORT_ENV,
    TOKEN_ENV,
    TRANSPORT_ENV,
    TransportConfigError,
    resolve_transport,
    run_server,
)


class _FakeMCP:
    """Records how run() was invoked and what auth was attached."""

    def __init__(self):
        self.run_calls = []
        self.auth = None

    def run(self, **kwargs):
        self.run_calls.append(kwargs)


class _FakeVerifier:
    def __init__(self, tokens):
        self.tokens = tokens


@pytest.fixture()
def clean_env(monkeypatch):
    for var in (TRANSPORT_ENV, HOST_ENV, PORT_ENV, TOKEN_ENV):
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(var.replace("NOESIS_", "NEURONEWS_"), raising=False)
    return monkeypatch


def _install_fake_auth_module(monkeypatch):
    """A fastmcp.server.auth module exposing StaticTokenVerifier."""
    fastmcp = types.ModuleType("fastmcp")
    server = types.ModuleType("fastmcp.server")
    auth = types.ModuleType("fastmcp.server.auth")
    auth.StaticTokenVerifier = _FakeVerifier
    fastmcp.server = server
    server.auth = auth
    monkeypatch.setitem(sys.modules, "fastmcp", fastmcp)
    monkeypatch.setitem(sys.modules, "fastmcp.server", server)
    monkeypatch.setitem(sys.modules, "fastmcp.server.auth", auth)


def test_default_is_stdio(clean_env):
    mcp = _FakeMCP()
    run_server(mcp)
    assert mcp.run_calls == [{}]  # plain mcp.run(), exactly as before
    assert mcp.auth is None


def test_http_transport_binds_configured_host_port(clean_env):
    clean_env.setenv(TRANSPORT_ENV, "http")
    clean_env.setenv(HOST_ENV, "0.0.0.0")
    clean_env.setenv(PORT_ENV, "8123")
    mcp = _FakeMCP()
    run_server(mcp)
    assert mcp.run_calls == [{"transport": "http", "host": "0.0.0.0", "port": 8123}]
    assert mcp.auth is None  # no token -> open (localhost posture is the operator's call)


def test_http_defaults_are_localhost_8100(clean_env):
    clean_env.setenv(TRANSPORT_ENV, "http")
    cfg = resolve_transport()
    assert cfg["host"] == "127.0.0.1"
    assert cfg["port"] == 8100


def test_legacy_transport_env_warns_and_resolves(clean_env):
    clean_env.setenv("NEURONEWS_MCP_TRANSPORT", "http")
    clean_env.setenv("NEURONEWS_MCP_HTTP_PORT", "8124")
    with pytest.warns(DeprecationWarning) as caught:
        cfg = resolve_transport()
    assert cfg["transport"] == "http"
    assert cfg["port"] == 8124
    assert {"NOESIS_MCP_TRANSPORT", "NOESIS_MCP_HTTP_PORT"} <= {
        name
        for warning in caught
        for name in (TRANSPORT_ENV, PORT_ENV)
        if name in str(warning.message)
    }


def test_http_with_token_attaches_verifier(clean_env):
    _install_fake_auth_module(clean_env)
    clean_env.setenv(TRANSPORT_ENV, "http")
    clean_env.setenv(TOKEN_ENV, "s3cret")
    mcp = _FakeMCP()
    run_server(mcp)
    assert isinstance(mcp.auth, _FakeVerifier)
    assert "s3cret" in mcp.auth.tokens
    assert mcp.run_calls[0]["transport"] == "http"


def test_token_without_verifier_fails_closed(clean_env):
    # No fastmcp auth module importable -> startup must raise, never serve open.
    for mod in ("fastmcp", "fastmcp.server", "fastmcp.server.auth",
                "fastmcp.server.auth.providers.jwt", "fastmcp.server.auth.verifiers"):
        clean_env.setitem(sys.modules, mod, None)  # forces ImportError
    clean_env.setenv(TRANSPORT_ENV, "http")
    clean_env.setenv(TOKEN_ENV, "s3cret")
    mcp = _FakeMCP()
    with pytest.raises(TransportConfigError):
        run_server(mcp)
    assert mcp.run_calls == []  # the server never started


def test_unknown_transport_rejected(clean_env):
    clean_env.setenv(TRANSPORT_ENV, "carrier-pigeon")
    with pytest.raises(TransportConfigError):
        resolve_transport()


def test_bad_port_rejected(clean_env):
    clean_env.setenv(TRANSPORT_ENV, "http")
    clean_env.setenv(PORT_ENV, "not-a-port")
    with pytest.raises(TransportConfigError):
        resolve_transport()


def test_every_tool_server_uses_run_server():
    """All 24 servers route their main guard through the shared runner."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    servers = sorted((repo_root / "tools").glob("*_mcp/server.py"))
    assert len(servers) == 24
    for server in servers:
        text = server.read_text()
        assert "from src.mcp_host.transport import run_server" in text, server
        assert "run_server(mcp)" in text, server
        # The old direct-run pattern is gone from the main guard.
        assert 'if __name__ == "__main__":\n    mcp.run()' not in text, server
