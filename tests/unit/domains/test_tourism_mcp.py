"""Tourism statistics MCP entry points: catalog registration, declared scopes, exclusions and answers (#2739, TO10)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.kb.tourism_records import forbidden_paths, personal_data_paths
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import tourism_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.tourism import (
    TOURISM_SCOPES,
    TOURISM_TOOLS,
    TOURISM_WRITES,
)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "tourism-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn, revisions=True)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_registered_with_every_scope_they_always_read_and_write(mcp_env):
    tools, _ = mcp_env
    assert TOURISM_TOOLS <= set(tools) and set(TOURISM_SCOPES) == TOURISM_TOOLS
    for name in TOURISM_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in TOURISM_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == TOURISM_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in TOURISM_TOOLS:
        assert by_name[name]["required_scopes"] == TOURISM_SCOPES[name]
    descriptor = json.loads((h.ROOT / "packs/economics/providers/economics.tourism.json").read_text())
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        assert name in TOURISM_TOOLS and name not in TOURISM_WRITES
        assert op["required_scopes"] == TOURISM_SCOPES[name]
    for name in ("tourism_indicator_for_place", "tourism_series_history", "export_tourism_evidence_bundle",
                 "tourism_source_contracts", "link_tourism_series"):
        description = " ".join(tools[name].description.lower().split())
        assert "no nowcasting" in description and "no blending of eurostat and un tourism" in description, name


def test_reads_work_with_exactly_the_declared_scopes_and_writes_are_scoped(mcp_env):
    tools, state = mcp_env
    state["scopes"] = set(h.READ_ONLY)
    berlin = {"scheme": "eurostat-geo", "code": "DE30", "nuts_version": "2021"}
    answer = tools["tourism_indicator_for_place"].fn(namespace="global", place=berlin, concept="nights_spent")
    assert answer["status"] == "reported" and answer["by_frequency"].keys() == {"annual"}
    assert forbidden_paths(answer) == [] and personal_data_paths(answer) == []
    assert "blending Eurostat and UN Tourism figures" in answer["exclusions"]
    assert answer["not_implemented"][0]["provider"] == "un-tourism"
    untourism = tools["tourism_indicator_for_place"].fn(namespace="global", place=berlin, providers=["un-tourism"])
    assert untourism["status"] == "not-implemented"
    bundle = tools["export_tourism_evidence_bundle"].fn(namespace="global", place=berlin)["evidence_bundle"]
    assert all(a["dependencies"][0]["revision"] and a["dependencies"][0]["as_of"]
               for a in bundle["sections"][0]["assertions"])
    series = tools["list_tourism_series"].fn(namespace="global", frequency="monthly")["series"]
    assert {s["frequency"] for s in series} == {"monthly"}
    nights = next(s for s in series if s["dataset"] == "tour_occ_nim")
    history = tools["tourism_series_history"].fn(namespace="global", series_id=nights["series_id"])
    assert len(history["vintages"]) == 2
    assert tools["tourism_comparability_notes"].fn(namespace="global", series_id=nights["series_id"])["notes"]
    assert tools["list_tourism_identity_assertions"].fn(namespace="global")["assertions"] == []
    refused = tools["propose_tourism_nuts_links"].fn(namespace="global")
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    state["scopes"] = {"knowledge:tourism:write", "namespace:global:write"}
    proposed = tools["propose_tourism_nuts_links"].fn(namespace="global")
    assert proposed["assertions"] == [] and proposed["unlinked_place_keys"]
    denied = tools["link_tourism_series"].fn(namespace="global")
    assert denied["ok"] is False  # Geospatial and Labour read scopes are declared and required
    state["scopes"] = {"knowledge:tourism:write", "knowledge:geospatial:read", "knowledge:labour:read",
                       "namespace:global:write", "namespace:global:read"}
    linked = tools["link_tourism_series"].fn(namespace="global")
    assert linked["boundary"]["missing"] and linked["labour"]["missing"]  # provider_absent, reported
    state["scopes"] = set()
    contracts = tools["tourism_source_contracts"].fn()
    assert contracts["live_verification"]["eurostat-tourism-occupancy"]["status"] == "unverified-live"
    assert contracts["live_verification"]["un-tourism"]["status"] == "not-implemented"
    assert contracts["minimisation"]["decision"].startswith("published aggregates only")
