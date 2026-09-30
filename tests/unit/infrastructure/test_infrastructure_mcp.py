"""CI12 (#2393): the infrastructure MCP tools are registered, scoped and cataloged."""

from __future__ import annotations

import json
from pathlib import Path

from tools.knowledge_engine_mcp.infrastructure import (
    INFRASTRUCTURE_READS,
    INFRASTRUCTURE_SCOPES,
    INFRASTRUCTURE_TOOLS,
    INFRASTRUCTURE_WRITES,
    register,
)

ROOT = Path(__file__).resolve().parents[3]


class _MCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def wrap(fn):
            self.tools[fn.__name__] = fn
            return fn

        return wrap


def test_every_tool_is_registered_scoped_and_in_the_generated_catalog():
    mcp = _MCP()
    register(mcp, lambda op, **_: op, lambda: ("p", set()))
    assert set(mcp.tools) == INFRASTRUCTURE_TOOLS == set(INFRASTRUCTURE_SCOPES)
    assert not INFRASTRUCTURE_READS & INFRASTRUCTURE_WRITES
    catalog = json.loads((ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    entries = {t["name"]: t for t in catalog["tools"] if t["server"] == "noesis-knowledge-engine"}
    for name in INFRASTRUCTURE_TOOLS:
        assert entries[name]["mutability"] == ("write" if name in INFRASTRUCTURE_WRITES else "read"), name
        assert entries[name]["required_scopes"] == INFRASTRUCTURE_SCOPES[name], name


def test_a_tool_refuses_missing_scopes_before_touching_data():
    mcp = _MCP()

    def safe(operation, **_):
        try:
            return operation(None)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": {"code": getattr(exc, "code", "error")}}

    register(mcp, safe, lambda: ("p", {"knowledge:infrastructure:read"}))
    result = mcp.tools["infrastructure_assets_in_place"]("infrastructure", bbox=[14.2, 51.3, 14.8, 51.8])
    assert result["error"]["code"] == "unauthorized"
    linked = mcp.tools["link_infrastructure_records"]("infrastructure", energy_namespace="energy")
    assert linked["error"]["code"] == "unauthorized"
    contracts = mcp.tools["infrastructure_source_contracts"]()
    assert set(contracts["contracts"]) == {"gppd", "gem", "osm", "eia", "entsog"}
