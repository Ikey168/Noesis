"""Income MCP entry points: catalog registration, declared scopes, exclusions and minimisation (#2583, IP11)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.kb.income_distribution_records import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import income_distribution_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.income_distribution import (
    INCOME_SCOPES,
    INCOME_TOOLS,
    INCOME_WRITES,
)

DEU = {"scheme": "iso3166-1-alpha3", "code": "DEU"}


@pytest.fixture(scope="module")
def db_path(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("income") / "income-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn, revisions=True)
    conn.close()
    return path


@pytest.fixture()
def mcp_env(db_path, monkeypatch):
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(db_path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_registered_with_their_scopes_and_declare_exclusions(mcp_env):
    tools, _ = mcp_env
    assert INCOME_TOOLS <= set(tools) and set(INCOME_SCOPES) == INCOME_TOOLS
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in INCOME_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in INCOME_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == INCOME_SCOPES[name]
        assert by_name[name]["required_scopes"] == INCOME_SCOPES[name]
    descriptor = json.loads((h.ROOT / "packs/society/providers/society.income.json").read_text())
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        assert name in INCOME_TOOLS and name not in INCOME_WRITES
        assert op["required_scopes"] == INCOME_SCOPES[name]
    for name in ("income_indicator_for_place", "income_series_history", "income_comparability_notes",
                 "income_source_contracts", "link_income_records", "export_income_evidence_bundle"):
        description = tools[name].description.lower()
        assert "no nowcasting" in description and "no own poverty lines" in description
        assert "no blending of pip, eu-silc and oecd" in description and "no person-level data" in description


def test_reads_work_with_the_declared_scopes_and_outputs_are_minimised(mcp_env):
    tools, state = mcp_env
    state["scopes"] = set(h.READ_ONLY)
    answer = tools["income_indicator_for_place"].fn(namespace="global", place=DEU, concept="gini_index")
    assert answer["status"] == "reported" and forbidden_keys(answer) == []
    assert "blending PIP, EU-SILC and OECD figures into one series" in answer["exclusions"]
    assert answer["minimisation"].startswith("store aggregate published statistics only")
    series = tools["list_income_series"].fn(namespace="global", provider="pip")["series"]
    assert tools["income_series_history"].fn(namespace="global", series_id=series[0]["series_id"])["vintages"]
    assert tools["income_comparability_notes"].fn(namespace="global")["notes"]
    assert tools["list_income_identity_assertions"].fn(namespace="global")["assertions"] == []
    bundle = tools["export_income_evidence_bundle"].fn(namespace="global", place=DEU, concept="gini_index")
    assert bundle["status"] == "reported" and bundle["bundle"]["objects"]
    refused = tools["propose_income_related_indicators"].fn(namespace="global")
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    state["scopes"] = set()
    denied = tools["income_indicator_for_place"].fn(namespace="global", place=DEU)
    assert denied["ok"] is False
    contracts = tools["income_source_contracts"].fn()
    assert contracts["live_verification"]["pip"]["status"] == "unverified-live"
    assert contracts["minimisation_decision"]["who_may_query"]
