"""estimate_dividend_flows through the knowledge-engine MCP server and the ownership.core descriptor (#2891)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit import dividend_flow_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.ownership import OWNERSHIP_SCOPES, OWNERSHIP_TOOLS

ROOT = Path(__file__).resolve().parents[3]
TOOL = "estimate_dividend_flows"


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "dividend-flows.duckdb")
    conn, acquired = h.build(path=path)
    conn.close()
    state = {"principal": h.PRINCIPAL, "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    # Reads run on a read-only connection: the query must not write anything.
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return tool_map(server.mcp), state, acquired


def test_tool_is_declared_with_its_scopes_and_bound_by_the_descriptor():
    assert TOOL in OWNERSHIP_TOOLS and _mutability(TOOL) == "read"
    assert _required_scopes("knowledge_engine_mcp", "read", TOOL) == OWNERSHIP_SCOPES[TOOL]
    descriptor = json.loads((ROOT / "packs/corporate-ownership/providers/ownership.core.json").read_text())
    operation = next(o for o in descriptor["operations"] if o["id"] == "estimate-dividend-flows")
    assert operation["tool"] == f"noesis-knowledge-engine.{TOOL}" and operation["side_effect"] == "read-only"
    assert operation["required_scopes"] == OWNERSHIP_SCOPES[TOOL]
    graph = next(c for c in descriptor["capabilities"] if c["id"] == "ownership.graph")
    assert "estimate-dividend-flows" in graph["operations"]
    assert "derived" in graph["semantic_constraints"]["dividend_flows"]


def test_estimates_verify_and_export_through_mcp(mcp_env):
    tools, _, acquired = mcp_env
    assert TOOL in tools
    result = tools[TOOL].fn(namespace=h.NS, market_namespace=h.MARKET_NS, as_of=h.AS_OF, issuer=h.A, max_depth=3,
                            acquired_by_ms=acquired, verify=True, export_bundle=True)
    answer = result["answer"]
    assert answer["contract"] == "noesis-dividend-flow-estimate-v1" and answer["label"] == "derived"
    assert {e["recipient"] for e in answer["estimates"]} >= {h.B, h.C, h.D}
    assert result["verification"]["ok"] is True
    assert result["bundle"]["contract"] == "noesis-evidence-bundle-v1"
    plain = tools[TOOL].fn(namespace=h.NS, market_namespace=h.MARKET_NS, as_of=h.AS_OF, holder=h.D,
                           acquired_by_ms=acquired)
    assert plain["mode"] == "holder" and set(plain["by_currency"]) == {"EUR", "USD"}
    refused = tools[TOOL].fn(namespace=h.NS, market_namespace=h.MARKET_NS, as_of=h.AS_OF, holder=h.D, issuer=h.A)
    assert refused["ok"] is False and refused["error"]["code"] == "invalid_request"


def test_scopes_are_enforced(mcp_env):
    tools, state, acquired = mcp_env
    state["scopes"] = {f"namespace:{h.NS}:read", "market:actions:read"}
    denied = tools[TOOL].fn(namespace=h.NS, market_namespace=h.MARKET_NS, as_of=h.AS_OF, issuer=h.A,
                            acquired_by_ms=acquired)
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    state["scopes"] = {"knowledge:ownership:read", f"namespace:{h.NS}:read"}  # no Market access
    no_market = tools[TOOL].fn(namespace=h.NS, market_namespace=h.MARKET_NS, as_of=h.AS_OF, issuer=h.A,
                               acquired_by_ms=acquired)
    assert no_market["ok"] is False and no_market["error"]["code"] == "unauthorized"
