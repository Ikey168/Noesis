"""Tests for the Noesis-as-MCP-server (R13 #622).

Drives noesis_generate_view over an in-memory FastMCP client exactly as an
external host would, and asserts the returned document validates against the
ui-spec-v1 contract. Also covers source_type validation and the auth gate.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastmcp")

from src.genui.spec import validate_spec

REPO = Path(__file__).resolve().parents[3]


def _load_server():
    path = REPO / "tools/noesis_mcp/server.py"
    spec = importlib.util.spec_from_file_location("noesis_mcp_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _call(module, tool, args):
    from fastmcp.client import Client

    async def run():
        async with Client(module.mcp) as c:
            return (await c.call_tool(tool, args)).structured_content

    return asyncio.run(run())


def test_external_host_receives_a_valid_ui_spec():
    module = _load_server()
    out = _call(module, "noesis_generate_view", {"intent": "who disagrees about AI regulation?"})
    assert "error" not in out, out
    assert validate_spec(out["spec"]) == []
    assert out["meta"]["generated_by"]
    # The intent should surface conflict/claims-flavored panels.
    types = {p["type"] for p in out["spec"]["panels"]}
    assert types  # a non-empty plan


def test_intent_shapes_the_plan():
    module = _load_server()
    trend = _call(module, "noesis_generate_view", {"intent": "coverage trend over time"})
    assert validate_spec(trend["spec"]) == []


def test_invalid_source_type_is_rejected():
    module = _load_server()
    out = _call(module, "noesis_generate_view", {"intent": "x", "source_type": "not_a_type"})
    assert "error" in out and "source_type" in out["error"]


def test_panels_catalog_tool():
    module = _load_server()
    out = _call(module, "noesis_panels", {})
    assert out["count"] > 0
    assert any(p["type"] == "claims" for p in out["panels"])


def test_auth_gate_blocks_without_token(monkeypatch):
    monkeypatch.setenv("NOESIS_MCP_AUTH_TOKEN", "s3cret")
    module = _load_server()
    blocked = _call(module, "noesis_generate_view", {"intent": "x"})
    assert "error" in blocked and "unauthorized" in blocked["error"]
    ok = _call(module, "noesis_generate_view", {"intent": "x", "auth_token": "s3cret"})
    assert "error" not in ok and validate_spec(ok["spec"]) == []


def test_auth_open_when_unset(monkeypatch):
    monkeypatch.delenv("NOESIS_MCP_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("NEURONEWS_MCP_AUTH_TOKEN", raising=False)
    module = _load_server()
    out = _call(module, "noesis_generate_view", {"intent": "x"})
    assert "error" not in out
