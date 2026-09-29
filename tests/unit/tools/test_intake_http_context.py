from types import SimpleNamespace

from tools.knowledge_engine_mcp import server


def test_http_intake_context_uses_authenticated_caller(monkeypatch):
    from fastmcp.server import dependencies

    monkeypatch.setenv("NOESIS_MCP_TRANSPORT", "http")
    monkeypatch.setattr(server, "_context", lambda: ("env-operator", {"operator"}))
    monkeypatch.setattr(dependencies, "get_access_token", lambda: None)
    assert server._intake_context() == ("", set())
    token = SimpleNamespace(
        client_id="alice", scopes=["knowledge:intake:read", "namespace:research:read"]
    )
    monkeypatch.setattr(dependencies, "get_access_token", lambda: token)
    assert server._intake_context() == (
        "alice",
        {"knowledge:intake:read", "namespace:research:read"},
    )
    token.scopes = []
    assert server._intake_context() == ("alice", set())
