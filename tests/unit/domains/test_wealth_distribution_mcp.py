"""#2882: wealth MCP entry points: declared scopes, exclusions, minimisation, cited export and bundle gating."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.kb.wealth_distribution_records import forbidden_paths, personal_data_paths
from src.mcp_host.introspection import tool_map
from tests.unit import wealth_distribution_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.wealth import WEALTH_SCOPES, WEALTH_TOOLS


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("wealth-mcp") / "wealth.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn, revisions=True)
    conn.close()
    return path


@pytest.fixture
def mcp_env(database, monkeypatch):
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(database, read_only=read_only))
    return tool_map(server.mcp), state


def test_tools_are_registered_and_answering_tools_declare_the_exclusions(mcp_env):
    tools, _ = mcp_env
    assert WEALTH_TOOLS <= set(tools) and set(WEALTH_SCOPES) == WEALTH_TOOLS
    for name in ("wealth_indicator_for_place", "wealth_series_history", "wealth_source_contracts", "wealth_profile"):
        description = " ".join(tools[name].description.lower().split())
        assert "no nowcasting" in description and "no blending of wid, dwa and dfa figures" in description, name


def test_answers_history_notes_and_cited_export_through_mcp(mcp_env):
    tools, state = mcp_env
    status = tools["wealth_readiness"].fn(namespace=h.NS)
    assert {status["providers"][p]["status"] for p in h.SOURCES.values()} == {"fixture-only"}
    assert status["providers"]["bundesbank-phf-aggregates"]["status"] == "not-implemented"
    answer = tools["wealth_indicator_for_place"].fn(namespace=h.NS, area=h.US, indicator="wealth_share",
                                                    as_of="2099-06-30")
    assert {r["provider"] for r in answer["results"]} == {"wid-wealth-distribution",
                                                          "fed-distributional-financial-accounts"}
    assert {p["comparability"] for p in answer["pairs"]} == {"comparability_unknown"}
    assert answer["exclusions"] and answer["minimisation"].startswith("published aggregates only")
    assert forbidden_paths(answer) == [] and personal_data_paths(answer) == []
    series_id = answer["results"][0]["series_id"]
    other_id = next(r["series_id"] for r in answer["results"] if r["provider"] != answer["results"][0]["provider"])
    history = tools["wealth_series_history"].fn(namespace=h.NS, series_id=series_id)
    assert len(history["vintages"]) == 2 and history["exclusions"]
    note = tools["record_wealth_comparability"].fn(namespace=h.NS, series_id=series_id, other_series_id=other_id,
                                                   relation="different_population", statement="adults vs households",
                                                   cited=[{"kind": "methodology", "url": "https://example.org"}])
    state.update(principal="bob")
    assert tools["review_wealth_comparability"].fn(namespace=h.NS, note_id=note["note_id"], decision="accept",
                                                   reason="cited")["state"] == "accepted"
    assert tools["wealth_comparability_notes"].fn(namespace=h.NS, series_id=series_id)["notes"]
    bundle = tools["export_wealth_profile"].fn(namespace=h.NS, area=h.DE, as_of="2099-06-30")
    assert not verify_bundle(bundle).errors
    cited = [o["payload"]["citation"] for o in bundle["objects"] if o["payload"].get("kind") == "wealth-series-vintage"]
    assert cited and all(c["source"] and c["record_revision"] and c["as_of"] for c in cited)
    root = next(o for o in bundle["objects"] if o["payload"].get("kind") == "wealth-profile")
    assert root["payload"]["exclusions"] and root["payload"]["minimisation"].startswith("published aggregates")
    contracts = tools["wealth_source_contracts"].fn()
    assert contracts["live_verification"]["ecb-hfcs-statistical-tables"]["status"] == "not-implemented"
    assert json.dumps(contracts)


def test_reads_need_their_declared_scopes_and_disabling_the_bundle_is_enforced(mcp_env):
    tools, state = mcp_env
    state["scopes"] = {"knowledge:wealth:read", f"namespace:{h.NS}:read"}
    assert tools["list_wealth_series"].fn(namespace=h.NS, provider="wid-wealth-distribution")["series"]
    denied = tools["record_wealth_comparability"].fn(namespace=h.NS, series_id="x", relation="comparable",
                                                     statement="s", cited=[{"kind": "x"}])
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    state["scopes"] = set(h.SCOPES) | {"operator"}
    assert tools["set_society_bundle_enabled"].fn(namespace=h.NS, enabled=False)["enabled"] is False
    try:
        blocked = tools["wealth_profile"].fn(namespace=h.NS, area=h.DE)
        assert blocked["ok"] is False and blocked["error"]["code"] == "bundle_disabled"
    finally:
        assert tools["set_society_bundle_enabled"].fn(namespace=h.NS, enabled=True)["enabled"] is True
