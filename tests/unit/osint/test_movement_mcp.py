"""OSINT movement tools: absent while the flag is off, gated, scoped, purpose-logged and refusing (#2285, MV13)."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import duckdb
import pytest

from src.osint.investigations import MOVEMENT_GATED_TOOLS, MOVEMENT_TOOLS, is_gated
from src.osint.movements import request_log
from tests.unit.osint.movement_harness import Env

REPO = Path(__file__).resolve().parents[3]


def _server(monkeypatch, *, movements=None, gated=None, name="osint_movements"):
    for prefix in ("NOESIS", "NEURONEWS"):
        monkeypatch.delenv(f"{prefix}_OSINT_MOVEMENTS", raising=False)
        monkeypatch.delenv(f"{prefix}_OSINT_GATED_TOOLS", raising=False)
    if movements is not None:
        monkeypatch.setenv("NOESIS_OSINT_MOVEMENTS", movements)
    if gated is not None:
        monkeypatch.setenv("NOESIS_OSINT_GATED_TOOLS", gated)
    spec = importlib.util.spec_from_file_location(f"{name}_{movements}_{gated}", REPO / "tools/osint_mcp/server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _tools(module):
    return asyncio.run(module.mcp.get_tools())


def test_movement_tools_are_absent_while_the_flag_is_off(monkeypatch):
    off = _tools(_server(monkeypatch))
    assert "movement_source_contracts" in off
    assert not set(MOVEMENT_TOOLS) & set(off)
    contracts = off["movement_source_contracts"].fn()
    assert contracts["feature"]["enabled"] is False and contracts["providers"]["opensky"]["bounds"]
    # The review gate alone does not serve them either.
    assert not set(MOVEMENT_TOOLS) & set(_tools(_server(monkeypatch, gated="on")))


def test_registry_lookup_needs_the_flag_and_position_tools_also_need_the_review_gate(monkeypatch):
    flag_only = _tools(_server(monkeypatch, movements="on"))
    assert "movement_registry" in flag_only and not set(MOVEMENT_GATED_TOOLS) & set(flag_only)
    both = _tools(_server(monkeypatch, movements="on", gated="on"))
    assert set(MOVEMENT_TOOLS) <= set(both)
    assert all(is_gated(tool) for tool in MOVEMENT_GATED_TOOLS) and not is_gated("movement_registry")


@pytest.fixture()
def served(monkeypatch, tmp_path):
    path = tmp_path / "warehouse.duckdb"
    conn = duckdb.connect(str(path))
    Env(conn).loaded()
    conn.close()
    monkeypatch.setenv("NOESIS_DB_PATH", str(path))
    monkeypatch.setenv("NOESIS_OSINT_MOVEMENT_LOG_PATH", str(tmp_path / "requests.duckdb"))
    monkeypatch.setenv("NOESIS_MCP_PRINCIPAL", "analyst")
    monkeypatch.setenv("NOESIS_MCP_SCOPES", "knowledge:read,knowledge:osint:movements,namespace:osint:read")
    tools = _tools(_server(monkeypatch, movements="on", gated="on"))
    return tools, tmp_path


def _log(tmp_path):
    conn = duckdb.connect(str(tmp_path / "requests.duckdb"))
    try:
        return request_log(conn)
    finally:
        conn.close()


def test_person_keyed_over_bound_and_purposeless_requests_are_refused_and_logged(served):
    tools, tmp_path = served
    window = tools["movement_window"].fn
    person = window(identifier="Jane Q Example", start="2099-05-01", end="2099-05-02", purpose="test")
    assert person["status"] == "refused" and person["code"] == "person_identifier_refused"
    over = window(identifier="a0f1b2", start="2099-01-01", end="2099-05-01", purpose="test")
    assert over["code"] == "over_bound"
    opted = tools["movement_calls"].fn(identifier="a0f1f6", start="2099-05-01", end="2099-05-02", purpose="test")
    assert opted["code"] == "privacy_opt_out"
    silent = window(identifier="a0f1b2", start="2099-05-01", end="2099-05-02", purpose=" ")
    assert silent["code"] == "purpose_required"
    logged = _log(tmp_path)
    assert [r["outcome"] for r in logged] == ["refused:person_identifier_refused", "refused:over_bound",
                                              "refused:privacy_opt_out"]
    assert all(r["purpose"] == "test" and r["principal_id"] == "analyst" for r in logged)


def test_a_served_window_answers_with_coverage_and_is_logged_with_its_purpose(served):
    tools, tmp_path = served
    answer = tools["movement_window"].fn(identifier="a0f1b2", start="2099-05-01", end="2099-05-02",
                                         purpose="verify a reported charter flight", export_bundle=True)
    assert answer["contract"] == "noesis-osint-movement-answer-v1" and answer["sample_windows"]
    assert answer["evidence_bundle"]["bundle"]["objects"]
    calls = tools["movement_calls"].fn(identifier="a0f1e5", start="2099-05-01", end="2099-05-02",
                                       purpose="check coverage")
    assert calls["coverage"]["statement"] == "no coverage observed"
    registry = tools["movement_registry"].fn(identifier="N902EX", as_of="2099-05-01")
    assert registry["registry"][0]["as_published"]["registrant"]["name"] == "withheld: a natural person"
    assert [r["tool"] for r in _log(tmp_path)] == ["movement_window", "movement_calls"]


def test_position_tools_need_the_movement_scope(served, monkeypatch):
    tools, _ = served
    monkeypatch.setenv("NOESIS_MCP_SCOPES", "knowledge:read")
    refused = tools["movement_window"].fn(identifier="a0f1b2", start="2099-05-01", end="2099-05-02",
                                          purpose="test")
    assert refused["code"] == "unauthorized" and "knowledge:osint:movements" in refused["reason"]
