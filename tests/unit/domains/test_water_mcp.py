"""environment.water MCP tools on noesis-knowledge-engine: catalog, scopes, bundle gate, exclusions (#2582, WA11)."""

from __future__ import annotations

import asyncio
import json

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.water import harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.water import WATER_TOOLS, WATER_WRITES

NS = h.NS


def _tools(monkeypatch, path, scopes=None, principal="alice"):
    state = {"principal": principal, "scopes": set(h.ALL if scopes is None else scopes) | {"knowledge:schema:register"}}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_in_the_catalog_with_environment_scopes_and_write_classification():
    for name in WATER_TOOLS:
        assert _mutability(name) == ("write" if name in WATER_WRITES else "read"), name
    assert _required_scopes("knowledge_engine_mcp", "read", "water_source_contracts") == []
    assert _required_scopes("knowledge_engine_mcp", "write", "review_water_identity_match")[0] == \
        "knowledge:environment:review"
    assert _required_scopes("knowledge_engine_mcp", "read", "water_value_at") == ["knowledge:environment:read"]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    assert {t["name"] for t in catalog["tools"] if t["name"] in WATER_TOOLS} == WATER_TOOLS


def test_descriptions_declare_the_exclusions_and_contracts_the_decisions(tmp_path, monkeypatch):
    path = str(tmp_path / "w.duckdb")
    h.Env(duckdb.connect(path)).loaded().conn.close()
    tools, _ = _tools(monkeypatch, path)
    def text(name):
        return " ".join(tools[name].description.split())

    assert "interpolated" in text("water_value_at") and "forecast" in text("water_value_at")
    assert "never merged" in text("water_body_status_history") and "no status assessment" in text(
        "water_body_status_history")
    assert "no causal links" in text("link_water_records")
    assert "notices are record changes" in text("create_water_monitor")
    contracts = tools["water_source_contracts"].fn()
    assert contracts["not_implemented"]["grdc"]["decision"] == "not implemented"
    assert contracts["minimisation"]["decision"] == "no personal data"
    assert {v["status"] for k, v in contracts["live_verification"].items() if k != "grdc"} == {"unverified-live"}
    ready = tools["water_readiness"].fn(namespace=NS)
    assert {f["selected"] for f in ready["features"].values()} == {False}
    assert {p["state"]["last_evidence_origin"] for p in ready["providers"].values()} == {"fixture"}
    assert ready["linked_providers"]["weather"]["status"] == "unavailable"


def test_scopes_and_namespace_access_are_enforced(tmp_path, monkeypatch):
    path = str(tmp_path / "w.duckdb")
    h.Env(duckdb.connect(path)).loaded().conn.close()
    tools, state = _tools(monkeypatch, path, scopes={"knowledge:environment:read"})
    refused = tools["water_value_at"].fn(namespace=NS, station="990001", parameter="W",
                                         time="2026-09-20T00:00:00+02:00")
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    state["scopes"] = set(h.READ)
    assert tools["propose_water_identity_matches"].fn(namespace=NS)["ok"] is False
    answer = tools["water_value_at"].fn(namespace=NS, station="990001", parameter="W",
                                        time="2026-09-20T00:00:00+02:00")
    assert answer["status"] == "value on record"
    assert tools["water_body_status_history"].fn(namespace=NS, water_body="nope")["error"]["code"] == "not_found"


def test_outputs_carry_no_personal_or_derived_fields(tmp_path, monkeypatch):
    path = str(tmp_path / "w.duckdb")
    env = h.Env(duckdb.connect(path)).loaded()
    places = env.places()
    env.conn.close()
    tools, _ = _tools(monkeypatch, path)
    answers = [tools["water_for_place"].fn(namespace=NS, place_id=places["dresden"]),
               tools["water_station_history"].fn(namespace=NS, station="USGS-01646500"),
               tools["export_water_bundle"].fn(namespace=NS, place_id=places["dresden"]),
               tools["water_series"].fn(namespace=NS, station="990001", parameter="W",
                                        start="2026-09-19T00:00:00Z", end="2026-09-21T00:00:00Z")]
    for answer in answers:
        assert "error" not in answer, answer
        assert not h.forbidden_keys(json.loads(json.dumps(answer)))
    assert answers[2]["every_item_cited"] is True
