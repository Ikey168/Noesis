"""The read-only noesis-onchain MCP server: scopes, exclusions, readiness, audit (#2058)."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path

import duckdb
import pytest

from tests.unit.onchain import fixture_builder as fb

ROOT = Path(__file__).resolve().parents[3]
SERVER = ROOT / "tools/onchain_mcp/server.py"
READ = "knowledge:onchain:read"


def _load():
    spec = importlib.util.spec_from_file_location("onchain_mcp_under_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tools(module):
    return asyncio.run(module.mcp.get_tools())


@pytest.fixture()
def warehouse(tmp_path, monkeypatch):
    path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    for source in fb.RAW_FILES:
        fb.run_journey(conn, source, namespace="onchain")
    conn.close()
    monkeypatch.setenv("NOESIS_DB_PATH", str(path))
    monkeypatch.setenv("NOESIS_MCP_SCOPES", READ)
    monkeypatch.delenv("NOESIS_ONCHAIN_AUDIT_PATH", raising=False)
    return path


def test_exactly_the_three_read_only_tools_are_served_and_exclusions_are_absent():
    module = _load()
    tools = _tools(module)
    assert (
        set(tools)
        == {"address_observations", "contract_origin", "address_cluster"}
        == set(module.TOOL_SCOPES)
    )
    for name in module.EXCLUDED_TOOLS:
        assert name not in tools
    for tool in tools.values():
        schema = tool.output_schema or {}
        assert {"n", "method", "assumptions"} <= set(schema.get("properties", {}))
    catalog = json.loads(
        (ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    served = {t["name"] for t in catalog["tools"] if t["server"] == "noesis-onchain"}
    assert served == set(tools)
    names = {t["name"] for t in catalog["tools"]}
    for excluded in module.EXCLUDED_TOOLS:
        assert excluded not in names


def test_catalog_descriptor_and_tool_declare_exactly_the_scopes_the_tool_uses(
    warehouse, monkeypatch
):
    module = _load()
    catalog = json.loads(
        (ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    descriptor = json.loads(
        (ROOT / "packs/onchain/providers/onchain.core.json").read_text()
    )
    by_tool = {op["tool"]: op for op in descriptor["operations"]}
    tools = _tools(module)
    for tool in catalog["tools"]:
        if tool["server"] != "noesis-onchain":
            continue
        assert tool["mutability"] == "read"
        assert tool["required_scopes"] == module.TOOL_SCOPES[tool["name"]]
        assert (
            by_tool[tool["id"]]["required_scopes"] == module.TOOL_SCOPES[tool["name"]]
        )
        assert by_tool[tool["id"]]["side_effect"] == "read-only"
    args = {
        "address_observations": {"address": fb.ETH["journey-user"]},
        "contract_origin": {"contract": fb.ETH["exampla-token-contract"]},
        "address_cluster": {"address": fb.ETH["journey-user"]},
    }
    for name, kwargs in args.items():
        monkeypatch.setenv("NOESIS_MCP_SCOPES", ",".join(module.TOOL_SCOPES[name]))
        assert tools[name].fn(**kwargs)["status"] in {"observed", "probable"}, name
        monkeypatch.setenv("NOESIS_MCP_SCOPES", "knowledge:read")
        denied = tools[name].fn(**kwargs)
        assert (
            denied["status"] == "unauthorized"
            and denied["required_scopes"] == module.TOOL_SCOPES[name]
        )


def test_tools_are_not_ready_before_any_acquisition_and_never_mutate_the_warehouse(
    tmp_path, monkeypatch
):
    path = tmp_path / "empty.duckdb"
    duckdb.connect(str(path)).close()
    monkeypatch.setenv("NOESIS_DB_PATH", str(path))
    monkeypatch.setenv("NOESIS_MCP_SCOPES", READ)
    before = hashlib.sha256(path.read_bytes()).digest()
    tools = _tools(_load())
    assert (
        tools["address_observations"].fn(address=fb.ETH["journey-user"])["status"]
        == "not_ready"
    )
    assert (
        tools["contract_origin"].fn(contract=fb.ETH["exampla-token-contract"])["status"]
        == "not_ready"
    )
    assert (
        tools["address_cluster"].fn(address=fb.ETH["journey-user"])["status"]
        == "not_ready"
    )
    assert hashlib.sha256(path.read_bytes()).digest() == before
    missing = tmp_path / "missing.duckdb"
    monkeypatch.setenv("NOESIS_DB_PATH", str(missing))
    assert (
        tools["address_observations"].fn(address=fb.ETH["journey-user"])["status"]
        == "not_ready"
    )


def test_every_invocation_is_logged_to_the_separate_audit_store(
    warehouse, tmp_path, monkeypatch
):
    audit = tmp_path / "audit.duckdb"
    monkeypatch.setenv("NOESIS_ONCHAIN_AUDIT_PATH", str(audit))
    tools = _tools(_load())
    before = hashlib.sha256(warehouse.read_bytes()).digest()
    tools["address_cluster"].fn(address=fb.ETH["journey-user"], investigation="case-7")
    tools["address_observations"].fn(address=fb.ETH["first-buyer"])
    assert hashlib.sha256(warehouse.read_bytes()).digest() == before
    conn = duckdb.connect(str(audit))
    try:
        rows = conn.execute(
            "SELECT kg_name, event, detail FROM provisioning_events ORDER BY seq"
        ).fetchall()
    finally:
        conn.close()
    assert [(r[0], r[1]) for r in rows] == [
        ("case-7", "onchain.address_cluster"),
        ("onchain-observations", "onchain.address_observations"),
    ]
    assert json.loads(rows[1][2])["status"] == "unknown"


def test_unknown_addresses_are_explicit_through_the_tools(warehouse):
    tools = _tools(_load())
    result = tools["address_observations"].fn(address=fb.ETH["first-buyer"])
    assert result["status"] == "unknown" and result["items"] == []
    origin = tools["contract_origin"].fn(contract=fb.ETH["journey-user"])
    assert origin["status"] == "unknown" and origin["deployment"] is None
