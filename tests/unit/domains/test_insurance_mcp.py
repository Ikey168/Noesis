"""Market insurance MCP tools on the noesis-market server (#2230, IN11)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import duckdb
import pytest

import tests.unit.insurance_harness as h
from tools.market_mcp.insurance import INSURANCE_SCOPES, INSURANCE_TOOLS, INSURANCE_WRITES
from src.mcp_host.introspection import tool_map

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def tools():
    spec = importlib.util.spec_from_file_location("insurance_market_server", ROOT / "tools/market_mcp/server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tool_map(module.mcp)


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    path = tmp_path / "insurance.duckdb"
    conn = duckdb.connect(str(path))
    h.acquire_all(conn)
    conn.close()
    monkeypatch.setenv("NOESIS_DB_PATH", str(path))
    return path


def test_every_tool_is_registered_with_declared_scopes(tools):
    assert INSURANCE_TOOLS <= set(tools)
    assert set(INSURANCE_SCOPES) == INSURANCE_TOOLS


def test_tools_refuse_without_scopes_before_touching_the_warehouse(tools, warehouse, monkeypatch):
    monkeypatch.setenv("NOESIS_MCP_SCOPES", "")
    before = warehouse.read_bytes()
    result = tools["insurance_event_estimates"].fn(namespace=h.NS, event="Hurricane Fiktiva", as_of="2025-08-01")
    assert result["ok"] is False and result["error"]["code"] == "unauthorized"
    for name in INSURANCE_WRITES:
        assert tools[name].fn.__name__ == name
    assert warehouse.read_bytes() == before


def test_event_and_market_answers_through_the_tools(tools, warehouse, monkeypatch):
    monkeypatch.setenv("NOESIS_MCP_SCOPES", ",".join(sorted(h.SCOPES)))
    monkeypatch.setenv("NOESIS_MCP_PRINCIPAL", h.PRINCIPAL)
    event = tools["insurance_event_estimates"].fn(namespace=h.NS, event="Hurricane Fiktiva", as_of="2025-08-01")
    assert event.get("ok"), event
    assert len(event["result"]["series"]) == 2 and event["result"]["merged"] is False
    market = tools["insurance_market_as_of"].fn(namespace=h.NS, country="DE", as_of="2026-01-10")
    assert market["ok"] and market["result"]["status"] == "on_record"
    coverage = tools["insurance_coverage"].fn()
    assert {d["decision"] for d in coverage["result"]["decisions"]} == {"in-scope", "metadata-only", "excluded"}
    missing = tools["insurance_insurer_as_of"].fn(namespace=h.NS, insurer="529900MUSTERVERSAG57",
                                                  as_of="2025-06-30")
    assert missing["ok"] and missing["result"]["status"] == "none_on_record"
