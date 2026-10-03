"""Business statistics MCP entry points: catalog registration, declared scopes, exclusions and answers (#2738)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.business_statistics_records import forbidden_paths, personal_data_paths
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit import business_statistics_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.business import (
    BUSINESS_SCOPES,
    BUSINESS_TOOLS,
    BUSINESS_WRITES,
)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "business-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn, revisions=True)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return tool_map(server.mcp), state


def test_tools_are_registered_with_every_scope_they_always_read_and_write(mcp_env):
    tools, _ = mcp_env
    assert BUSINESS_TOOLS <= set(tools) and set(BUSINESS_SCOPES) == BUSINESS_TOOLS
    for name in BUSINESS_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in BUSINESS_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == BUSINESS_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in BUSINESS_TOOLS:
        assert by_name[name]["required_scopes"] == BUSINESS_SCOPES[name]
    descriptor = json.loads((h.ROOT / "packs/economics/providers/economics.business.json").read_text())
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        assert name in BUSINESS_TOOLS and name not in BUSINESS_WRITES
        assert op["required_scopes"] == BUSINESS_SCOPES[name]
    for name in ("business_indicator_for_place", "compare_business_places", "business_series_history",
                 "business_source_contracts", "link_business_series"):
        description = " ".join(tools[name].description.lower().split())
        assert "no nowcasting" in description and "no blending of eurostat and census" in description, name


def test_reads_work_with_exactly_the_declared_scopes_and_writes_are_scoped(mcp_env):
    tools, state = mcp_env
    state["scopes"] = set(h.READ_ONLY)
    answer = tools["business_indicator_for_place"].fn(namespace="global",
                                                      place={"scheme": "eurostat-geo", "code": "DE"},
                                                      concept="production_index")
    assert answer["status"] == "reported" and answer["side_by_side"] is True
    assert forbidden_paths(answer) == [] and personal_data_paths(answer) == []
    assert "blending Eurostat and Census figures" in answer["exclusions"]
    side = tools["compare_business_places"].fn(namespace="global", places=[
        {"scheme": "eurostat-geo", "code": "DE"}, {"scheme": "us-fips-state", "code": "06"}], concept="employment")
    assert {r["provider"] for a in side["places"] for r in a["answer"]["results"]} == {"us-census-cbp"}
    series = tools["list_business_series"].fn(namespace="global", provider="eurostat-sts")["series"]
    history = tools["business_series_history"].fn(namespace="global", series_id=series[0]["series_id"])
    assert len(history["vintages"]) == 2
    assert tools["business_comparability_notes"].fn(namespace="global", series_id=series[0]["series_id"])["notes"]
    assert tools["list_business_identity_assertions"].fn(namespace="global")["assertions"] == []
    refused = tools["propose_business_classification_links"].fn(namespace="global")
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    state["scopes"] = {"knowledge:business:write", "namespace:global:write"}
    proposed = tools["propose_business_classification_links"].fn(namespace="global")
    assert proposed["assertions"] == [] and proposed["unlinked_codes"]
    denied = tools["link_business_series"].fn(namespace="global")
    assert denied["ok"] is False  # Labour and Trade read scopes are declared and required
    state["scopes"] = set()
    contracts = tools["business_source_contracts"].fn()
    assert contracts["live_verification"]["us-census-cbp"]["status"] == "unverified-live"
    assert contracts["minimisation"]["decision"].startswith("published aggregates only")
