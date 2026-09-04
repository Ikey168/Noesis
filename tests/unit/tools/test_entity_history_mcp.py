from __future__ import annotations

import asyncio
import inspect

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def call(t, **k):
    v = t.fn(**k)
    return asyncio.run(v) if inspect.isawaitable(v) else v


def test_entity_history_mcp_flow_auth_dual_control_audit(tmp_path, monkeypatch):
    db = tmp_path / "entities.duckdb"
    scopes = {"knowledge:entity-history:read"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(db))
    )
    tools = asyncio.run(server.mcp.get_tools())
    names = {
        "register_entity_history_identity",
        "record_entity_identity_decision",
        "resolve_entity_history",
        "preview_entity_merge",
        "execute_entity_merge",
        "preview_entity_split",
        "execute_entity_split",
        "undo_entity_identity_change",
        "register_entity_dependency",
        "inspect_entity_change_impact",
        "publish_entity_change_rebuild",
        "get_entity_identity_history",
        "export_entity_identity_history",
    }
    assert names <= tools.keys()
    assert (
        call(
            tools["register_entity_history_identity"], namespace="osint", entity_id="a"
        )["error"]["code"]
        == "unauthorized"
    )
    scopes.add("knowledge:entity-history:write")
    for e in ("a", "b", "c"):
        call(
            tools["register_entity_history_identity"],
            namespace="osint",
            entity_id=e,
            aliases=[e],
        )
    call(
        tools["register_entity_dependency"],
        namespace="osint",
        entity_id="a",
        dependent_type="graph",
        dependent_id="g",
    )
    p = call(
        tools["preview_entity_merge"],
        namespace="osint",
        source_ids=["a"],
        target_id="b",
        dual_control=True,
        approvals=["r1", "r2"],
    )
    assert p["eligible"]
    assert (
        call(
            tools["execute_entity_merge"],
            namespace="osint",
            preview=p,
            reviewer_id="r2",
        )["error"]["code"]
        == "unauthorized"
    )
    scopes.add("knowledge:entity-history:execute")
    m = call(
        tools["execute_entity_merge"], namespace="osint", preview=p, reviewer_id="r2"
    )
    assert (
        call(tools["resolve_entity_history"], namespace="osint", entity_id="a")[
            "canonical_id"
        ]
        == "b"
    )
    assert call(
        tools["inspect_entity_change_impact"], namespace="osint", entity_ids=["a"]
    )["affected"]
    assert call(
        tools["publish_entity_change_rebuild"],
        namespace="osint",
        decision_id=m["decision_id"],
        generation=1,
        results=[{"status": "completed"}],
    )["published"]
    assert call(
        tools["undo_entity_identity_change"],
        namespace="osint",
        decision_id=m["decision_id"],
        reviewer_id="r2",
    )["reversible"]
    assert call(
        tools["export_entity_identity_history"], namespace="osint", entity_ids=["a"]
    )["audit_complete"]


def test_entity_history_catalog():
    assert _mutability("undo_entity_identity_change") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "execute_entity_merge"
    ) == ["knowledge:entity-history:execute"]
    assert _required_scopes("knowledge_engine_mcp", "read", "preview_entity_split") == [
        "knowledge:entity-history:read"
    ]
    assert (
        "noesis-entity-merge-v1"
        in server.knowledge_engine_capabilities.fn()["contracts"]
    )
