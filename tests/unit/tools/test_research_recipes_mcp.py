from __future__ import annotations

import asyncio
import inspect

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def call(t, **k):
    v = t.fn(**k)
    return asyncio.run(v) if inspect.isawaitable(v) else v


def definition():
    return {
        "recipe_id": "dataset-summary",
        "version": "1",
        "namespace": "scientific",
        "inputs": {
            "query": {"type": "string", "required": True},
            "token": {"type": "string", "secret": True, "required": True},
        },
        "steps": [
            {
                "id": "search",
                "tool": "search_datasets",
                "depends_on": [],
                "input_schema": "query-v1",
                "output_schema": "dataset-search-v1",
                "required_scopes": ["knowledge:dataset:read"],
            }
        ],
        "outputs": {"datasets": "search"},
        "compatibility": {"engine": ">=1"},
    }


def test_recipe_mcp_registry_preview_run_status_replay_export_auth(
    tmp_path, monkeypatch
):
    db = tmp_path / "recipes.duckdb"
    scopes = {"knowledge:recipes:read"}
    monkeypatch.setattr(server, "_context", lambda: ("researcher", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(db))
    )
    tools = asyncio.run(server.mcp.get_tools())
    names = {
        "validate_research_recipe",
        "register_research_recipe",
        "list_research_recipes",
        "preview_research_recipe",
        "run_research_recipe",
        "get_research_recipe_run",
        "cancel_research_recipe_run",
        "replay_research_recipe_run",
        "export_research_recipe_run",
    }
    assert names <= tools.keys()
    assert call(tools["validate_research_recipe"], recipe=definition())["recipe_hash"]
    assert (
        call(tools["register_research_recipe"], recipe=definition())["error"]["code"]
        == "unauthorized"
    )
    scopes.add("knowledge:recipes:write")
    r = call(tools["register_research_recipe"], recipe=definition())
    assert call(tools["list_research_recipes"], namespace="scientific")["items"]
    params = {"query": "climate", "token": {"secret_ref": "vault:t"}}
    p = call(
        tools["preview_research_recipe"],
        namespace="scientific",
        recipe_revision_id=r["recipe_revision_id"],
        parameters=params,
        granted_scopes=["knowledge:dataset:read"],
        available_tool_versions={"search_datasets": "1"},
    )
    assert p["valid"]
    denied = call(
        tools["run_research_recipe"],
        namespace="scientific",
        recipe_revision_id=r["recipe_revision_id"],
        parameters=params,
        run_key="k",
        step_outputs={"search_datasets": {"items": []}},
        granted_scopes=["knowledge:dataset:read"],
        tool_versions={"search_datasets": "1"},
    )
    assert denied["error"]["code"] == "unauthorized"
    scopes.add("knowledge:recipes:execute")
    out = call(
        tools["run_research_recipe"],
        namespace="scientific",
        recipe_revision_id=r["recipe_revision_id"],
        parameters=params,
        run_key="k",
        step_outputs={"search_datasets": {"items": []}},
        granted_scopes=["knowledge:dataset:read"],
        tool_versions={"search_datasets": "1"},
        secrets={"vault:t": "SECRET"},
    )
    assert "SECRET" not in str(out)
    assert (
        call(
            tools["get_research_recipe_run"],
            namespace="scientific",
            run_id=out["run_id"],
        )["status"]
        == "completed"
    )
    assert call(
        tools["replay_research_recipe_run"],
        namespace="scientific",
        run_id=out["run_id"],
        current_tool_versions={"search_datasets": "1"},
    )["deterministic"]
    assert call(
        tools["export_research_recipe_run"],
        namespace="scientific",
        run_id=out["run_id"],
    )["dependency_complete"]


def test_recipe_catalog_scopes():
    assert _mutability("run_research_recipe") == "write"
    assert _required_scopes("knowledge_engine_mcp", "write", "run_research_recipe") == [
        "knowledge:recipes:execute"
    ]
    assert _required_scopes(
        "knowledge_engine_mcp", "read", "preview_research_recipe"
    ) == ["knowledge:recipes:read"]
    assert (
        "noesis-research-recipe-v1"
        in server.knowledge_engine_capabilities.fn()["contracts"]
    )
