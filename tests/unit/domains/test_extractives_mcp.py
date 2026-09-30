"""Extractives MCP entry points: catalog registration, declared scopes, exclusions and minimisation (#2709)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.ingestion.extractives_sources import personal_keys
from src.kb.extractives_records import ExtractivesError, forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import extractives_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.extractives import (
    EXTRACTIVES_SCOPES,
    EXTRACTIVES_TOOLS,
    EXTRACTIVES_WRITES,
    _declared,
)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "extractives-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_registered_with_every_scope_they_read_and_write(mcp_env):
    tools, _ = mcp_env
    assert EXTRACTIVES_TOOLS <= set(tools) and set(EXTRACTIVES_SCOPES) == EXTRACTIVES_TOOLS
    for name in EXTRACTIVES_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in EXTRACTIVES_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == EXTRACTIVES_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in EXTRACTIVES_TOOLS:
        assert by_name[name]["required_scopes"] == EXTRACTIVES_SCOPES[name]
    descriptor = json.loads((h.ROOT / "packs/economics/providers/economics.extractives.json").read_text())
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        assert name in EXTRACTIVES_TOOLS and name not in EXTRACTIVES_WRITES
        assert op["required_scopes"] == EXTRACTIVES_SCOPES[name]
    for name in ("extractive_payments_for_company", "extractive_payments_for_country",
                 "commodity_production_side_by_side", "extractives_source_contracts",
                 "export_extractives_evidence_bundle", "link_extractives_records"):
        description = tools[name].description.lower()
        assert "no own reserve estimates" in description and "risk scoring" in description
        assert "forecasts" in description and "never converted or summed" in description


def test_reads_work_with_exactly_the_declared_scopes_and_answers_are_minimised(mcp_env):
    tools, state = mcp_env
    state["scopes"] = set(h.READ_ONLY)
    answer = tools["commodity_production_side_by_side"].fn(namespace="global", commodity="Copper", country="Peru")
    assert answer["status"] == "reported" and forbidden_keys(answer) == [] and personal_keys(answer) == []
    assert "own reserve or resource estimates" in answer["exclusions"] and answer["minimisation"]
    country = tools["extractive_payments_for_country"].fn(namespace="global", country="PER")
    assert country["status"] == "reported" and "Juan" not in json.dumps(country)
    assert "contact@example.invalid" not in json.dumps(country)
    series = tools["list_extractives_series"].fn(namespace="global", provider="bgs-wms")["series"]
    assert tools["extractives_series_history"].fn(namespace="global", series_id=series[0]["series_id"])["vintages"]
    bundle = tools["export_extractives_evidence_bundle"].fn(namespace="global", question="payments-for-country",
                                                            country="PER")
    assert bundle["bundle"]["contract"] == "noesis-evidence-bundle-v1"
    refused = tools["propose_extractives_identity"].fn(namespace="global", kind="commodity")
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    matches = tools["list_extractives_company_matches"].fn(namespace="global")
    assert matches["ok"] is False
    state["scopes"] = {"knowledge:extractives:write", "namespace:global:write"}
    proposed = tools["propose_extractives_identity"].fn(namespace="global", kind="commodity")
    assert proposed["assertions"]
    state["scopes"] = set()
    contracts = tools["extractives_source_contracts"].fn()
    assert contracts["live_verification"]["eiti"]["status"] == "unverified-live" and contracts["minimisation"]


def test_the_output_guard_refuses_personal_or_derived_fields():
    with pytest.raises(ExtractivesError):
        _declared({"reports": [{"contact": {"email": "x@example.invalid"}}]})
    with pytest.raises(ExtractivesError):
        _declared({"results": [{"risk_score": 1}]})
    assert _declared({"status": "ok"})["exclusions"]
