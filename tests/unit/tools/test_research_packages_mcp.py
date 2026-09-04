from __future__ import annotations

import asyncio
import inspect

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


MANIFEST = {
    "format_version": "1.0",
    "question": "q",
    "plan": {},
    "snapshot": {"generation": 1},
    "evidence": ["c1"],
    "transformations": [],
    "findings": [],
    "limitations": [],
    "policies": {},
    "compatibility": {},
}


def test_research_package_mcp_exchange_auth_and_isolation(tmp_path, monkeypatch):
    db = tmp_path / "packages.duckdb"
    scopes = {"knowledge:packages:read"}
    monkeypatch.setattr(server, "_context", lambda: ("researcher", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(db))
    )
    tools = asyncio.run(server.mcp.get_tools())
    names = {
        "validate_research_package_manifest",
        "create_research_package_manifest",
        "register_research_package_component",
        "resolve_research_package_closure",
        "build_research_package",
        "sign_research_package",
        "encrypt_research_package",
        "decrypt_research_package",
        "inspect_research_package",
        "verify_research_package",
        "import_research_package",
        "replay_research_package",
        "rollback_research_package_import",
    }
    assert names <= tools.keys()
    assert call(tools["validate_research_package_manifest"], manifest=MANIFEST)["valid"]
    assert (
        call(
            tools["create_research_package_manifest"],
            namespace="research",
            manifest=MANIFEST,
        )["error"]["code"]
        == "unauthorized"
    )
    scopes.add("knowledge:packages:write")
    created = call(
        tools["create_research_package_manifest"],
        namespace="research",
        manifest=MANIFEST,
    )
    call(
        tools["register_research_package_component"],
        namespace="research",
        component_type="claim",
        component_id="c1",
        content={"text": "finding"},
    )
    package = call(
        tools["build_research_package"],
        namespace="research",
        package_id=created["package_id"],
        root_ids=["c1"],
    )
    assert call(tools["verify_research_package"], package=package)["valid"]
    assert call(tools["inspect_research_package"], package=package)["member_count"] == 1
    scopes.add("knowledge:packages:import")
    receipt = call(
        tools["import_research_package"],
        package=package,
        target_namespace="import:peer",
    )
    assert (
        call(
            tools["replay_research_package"],
            target_namespace="import:peer",
            import_id=receipt["import_id"],
        )["status"]
        == "replayed"
    )
    assert (
        call(
            tools["rollback_research_package_import"],
            target_namespace="import:peer",
            import_id=receipt["import_id"],
        )["status"]
        == "rolled_back"
    )


def test_research_package_catalog():
    assert _mutability("build_research_package") == "write"
    assert _mutability("verify_research_package") == "read"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "import_research_package"
    ) == ["knowledge:packages:import"]
    assert _required_scopes(
        "knowledge_engine_mcp", "read", "verify_research_package"
    ) == ["knowledge:packages:read"]
    assert (
        "noesis-research-package-v1"
        in server.knowledge_engine_capabilities.fn()["contracts"]
    )
