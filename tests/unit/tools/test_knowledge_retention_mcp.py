from __future__ import annotations

import asyncio
import inspect

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def test_archive_mcp_restores_into_a_fresh_database(tmp_path, monkeypatch):
    current = [tmp_path / "original.duckdb"]
    monkeypatch.setattr(server, "_context", lambda: ("admin", {"knowledge:retention:read", "knowledge:retention:execute"}))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(str(current[0])))
    tools = asyncio.run(server.mcp.get_tools())
    checkpoint = call(tools["create_retention_checkpoint"], namespace="research", generation_start=1,
                      generation_end=1, records=[{"id": "source-1"}], tombstones=["source-0"], schema_version="1")
    archive = call(tools["archive_knowledge_checkpoint"], namespace="research", checkpoint_id=checkpoint["checkpoint_id"],
                   storage={"driver": "filesystem", "uri": str(tmp_path / "archive.json")})
    current[0].unlink()
    current[0] = tmp_path / "fresh.duckdb"
    restored = call(tools["restore_knowledge_archive"], namespace="research", archive_id=archive["archive_id"], manifest=archive)
    assert restored["record_count"] == restored["tombstone_count"] == 1
    assert call(tools["verify_retention_checkpoint"], namespace="research", checkpoint_id=checkpoint["checkpoint_id"])["verified"]


def test_retention_mcp_flow_auth_checkpoint_archive_gc(tmp_path, monkeypatch):
    db = tmp_path / "retention.duckdb"
    scopes = {"knowledge:retention:read"}
    monkeypatch.setattr(server, "_context", lambda: ("admin", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(db))
    )
    tools = asyncio.run(server.mcp.get_tools())
    names = {
        "register_retention_policy",
        "register_retention_object",
        "place_retention_legal_hold",
        "release_retention_legal_hold",
        "simulate_retention_eligibility",
        "create_retention_checkpoint",
        "verify_retention_checkpoint",
        "archive_knowledge_checkpoint",
        "restore_knowledge_archive",
        "plan_retention_gc",
        "execute_retention_gc",
        "get_retention_job",
        "cancel_retention_job",
        "inspect_retention_health",
    }
    assert names <= tools.keys()
    assert (
        call(
            tools["register_retention_policy"],
            namespace="research",
            policy_id="p",
            version=1,
            rules={"minimum_age_ms": 0},
        )["error"]["code"]
        == "unauthorized"
    )
    scopes.add("knowledge:retention:admin")
    call(
        tools["register_retention_policy"],
        namespace="research",
        policy_id="p",
        version=1,
        rules={"minimum_age_ms": 0},
    )
    call(
        tools["register_retention_object"],
        namespace="research",
        object_id="d1",
        object_class="document",
        policy_id="p",
        policy_version=1,
        payload={"id": "d1"},
        created_at_ms=1,
    )
    plan = call(tools["plan_retention_gc"], namespace="research", object_ids=["d1"])
    assert plan["eligible"] == ["d1"]
    scopes.add("knowledge:retention:execute")
    checkpoint = call(
        tools["create_retention_checkpoint"],
        namespace="research",
        generation_start=1,
        generation_end=1,
        records=[{"id": "d1"}],
        schema_version="1",
    )
    assert call(
        tools["verify_retention_checkpoint"],
        namespace="research",
        checkpoint_id=checkpoint["checkpoint_id"],
    )["verified"]
    archived = call(
        tools["archive_knowledge_checkpoint"],
        namespace="research",
        checkpoint_id=checkpoint["checkpoint_id"],
        storage={"driver": "filesystem", "uri": str(tmp_path / "archive.json")},
    )
    assert call(
        tools["restore_knowledge_archive"],
        namespace="research",
        archive_id=archived["archive_id"],
    )["restored_atomically"]
    job = call(tools["execute_retention_gc"], namespace="research", plan=plan)
    assert (
        call(tools["get_retention_job"], namespace="research", job_id=job["job_id"])[
            "status"
        ]
        == "completed"
    )


def test_retention_catalog():
    assert _mutability("plan_retention_gc") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "archive_knowledge_checkpoint"
    ) == ["knowledge:retention:execute"]
    assert _required_scopes(
        "knowledge_engine_mcp", "read", "simulate_retention_eligibility"
    ) == ["knowledge:retention:read"]
    assert (
        "noesis-retention-gc-plan-v1"
        in server.knowledge_engine_capabilities.fn()["contracts"]
    )
