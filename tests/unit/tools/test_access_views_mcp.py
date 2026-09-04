from __future__ import annotations

import asyncio
import inspect

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


RULES = {
    "allowed_principals": ["analyst"],
    "allowed_purposes": ["research"],
    "allowed_classifications": ["public"],
    "allowed_transformations": ["read", "query", "summary"],
}


def test_access_view_mcp_flow_and_admin_separation(tmp_path, monkeypatch):
    db = tmp_path / "views.duckdb"
    scopes = {"knowledge:views:read"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(db))
    )
    tools = asyncio.run(server.mcp.get_tools())
    names = {
        "register_access_view_policy",
        "register_access_bound_object",
        "inspect_effective_access_view",
        "simulate_access_view",
        "filter_access_bound_query",
        "derive_redacted_projection",
        "create_access_share_grant",
        "authorize_access_export",
        "revoke_access_share_grant",
        "get_access_view_audit",
        "inspect_access_view_health",
    }
    assert names <= tools.keys()
    assert (
        call(
            tools["register_access_view_policy"],
            namespace="research",
            policy_id="p",
            version=1,
            rules=RULES,
        )["error"]["code"]
        == "unauthorized"
    )
    scopes.add("knowledge:views:admin")
    call(
        tools["register_access_view_policy"],
        namespace="research",
        policy_id="p",
        version=1,
        rules=RULES,
    )
    scopes.add("knowledge:views:write")
    call(
        tools["register_access_bound_object"],
        namespace="research",
        object_type="claim",
        object_id="c1",
        classification="public",
        policy_id="p",
        policy_version=1,
        payload={"text": "safe"},
    )
    filtered = call(
        tools["filter_access_bound_query"],
        namespace="research",
        candidates=[{"object_type": "claim", "object_id": "c1"}],
        purpose="research",
    )
    assert filtered["visible_count"] == 1
    simulated = call(
        tools["simulate_access_view"],
        namespace="research",
        object_type="claim",
        object_id="c1",
        subject_principal_id="analyst",
        purpose="research",
    )
    assert simulated["allowed"]
    scopes.add("knowledge:views:export")
    grant = call(
        tools["create_access_share_grant"],
        namespace="research",
        recipient_id="recipient",
        purpose="research",
        expires_at_ms=9_999_999_999_999,
        policy_id="p",
        policy_version=1,
        object_ids=["c1"],
    )
    authorized = call(
        tools["authorize_access_export"],
        namespace="research",
        grant_id=grant["grant_id"],
        recipient_id="recipient",
        purpose="research",
        object_ids=["c1"],
        watermark="recipient",
    )
    assert authorized["authorized"]
    assert call(tools["get_access_view_audit"], namespace="research")["events"]


def test_access_view_catalog():
    assert _mutability("filter_access_bound_query") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "derive_redacted_projection"
    ) == ["knowledge:views:write"]
    assert _required_scopes("knowledge_engine_mcp", "read", "simulate_access_view") == [
        "knowledge:views:admin"
    ]
    assert (
        "noesis-access-view-policy-v1"
        in server.knowledge_engine_capabilities.fn()["contracts"]
    )
