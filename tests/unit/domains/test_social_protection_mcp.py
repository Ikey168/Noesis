"""Social protection MCP entry points: declared scopes, exclusions, minimisation, cited export and gating (#2798)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.kb.social_protection_records import forbidden_paths, personal_data_paths
from src.mcp_host.introspection import tool_map
from tests.unit import social_protection_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.social_protection import (
    SOCIAL_PROTECTION_SCOPES,
    SOCIAL_PROTECTION_TOOLS,
)

DE = {"scheme": "eurostat-geo", "code": "DE"}


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("social-protection-mcp") / "social.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn, revisions=True)
    h.register_places(conn, keys=("de",))
    conn.close()
    return path


@pytest.fixture
def mcp_env(database, monkeypatch):
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(database, read_only=read_only))
    return tool_map(server.mcp), state, database


def test_tools_are_registered_and_answering_tools_declare_the_exclusions(mcp_env):
    tools, _, _ = mcp_env
    assert SOCIAL_PROTECTION_TOOLS <= set(tools) and set(SOCIAL_PROTECTION_SCOPES) == SOCIAL_PROTECTION_TOOLS
    for name in ("social_protection_indicator_for_place", "social_protection_series_history",
                 "social_protection_source_contracts", "social_protection_profile", "link_social_protection_series"):
        description = " ".join(tools[name].description.lower().split())
        assert "no nowcasting" in description and "no blending of esspros, socx and ilo figures" in description, name
        assert "no combination with cofog" in description, name


def test_answers_identity_links_and_cited_export_through_mcp(mcp_env):
    tools, state, _ = mcp_env
    status = tools["social_protection_readiness"].fn(namespace=h.NS)
    assert {status["providers"][p]["status"] for p in h.SOURCES.values()} == {"fixture-only"}
    assert status["optional_links"]["economics.public-finance"] == "provider_absent"
    proposed = tools["propose_social_protection_place_matches"].fn(namespace=h.NS)
    state.update(principal="bob")
    for assertion in proposed["proposed"]:
        assert tools["review_social_protection_identity"].fn(namespace=h.NS, assertion_id=assertion["assertion_id"],
                                                             decision="accept", reason="ISO code")["state"] == \
            "accepted"
    state.update(principal="alice")
    links = tools["link_social_protection_series"].fn(namespace=h.NS)
    assert links["denominators"]["missing"] and links["cofog"]["missing"]  # optional links degrade, reported
    answer = tools["social_protection_indicator_for_place"].fn(namespace=h.NS, area=DE, as_of="2099-07-01")
    assert {r["provider"] for r in answer["results"]} == {"eurostat-esspros", "oecd-socx",
                                                          "ilo-social-protection-coverage"}
    assert answer["exclusions"] and answer["minimisation"].startswith("published aggregates only")
    assert forbidden_paths(answer) == [] and personal_data_paths(answer) == []
    bundle = tools["export_social_protection_profile"].fn(namespace=h.NS, area=DE, as_of="2099-07-01")
    assert not verify_bundle(bundle).errors
    cited = [o["payload"]["citation"] for o in bundle["objects"]
             if o["payload"].get("kind") == "social-protection-series-vintage"]
    assert cited and all(c["source"] and c["record_revision"] and c["as_of"] for c in cited)
    root = next(o for o in bundle["objects"] if o["payload"].get("kind") == "social-protection-profile")
    assert root["payload"]["exclusions"] and root["payload"]["minimisation"].startswith("published aggregates")
    contracts = tools["social_protection_source_contracts"].fn()
    assert contracts["live_verification"]["ilo-world-social-protection-dashboards"]["status"] == "not-implemented"
    assert contracts["minimisation"]["excluded"].startswith("benefit-recipient registers")


def test_reads_need_their_declared_scopes_and_disabling_the_bundle_is_enforced(mcp_env):
    tools, state, _ = mcp_env
    state["scopes"] = {"knowledge:social-protection:read", f"namespace:{h.NS}:read"}
    series = tools["list_social_protection_series"].fn(namespace=h.NS, provider="oecd-socx")["series"]
    history = tools["social_protection_series_history"].fn(namespace=h.NS, series_id=series[0]["series_id"])
    assert len(history["vintages"]) == 2 and history["exclusions"]
    denied = tools["propose_social_protection_function_relations"].fn(namespace=h.NS)
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    state["scopes"] = {"knowledge:social-protection:write", f"namespace:{h.NS}:write"}
    linked = tools["link_social_protection_series"].fn(namespace=h.NS)
    assert linked["ok"] is False  # Demographics and Public finance read scopes are declared and required
    state["scopes"] = set(h.SCOPES) | {"operator"}
    assert tools["set_society_bundle_enabled"].fn(namespace=h.NS, enabled=False)["enabled"] is False
    try:
        blocked = tools["social_protection_profile"].fn(namespace=h.NS, area=DE)
        assert blocked["ok"] is False and blocked["error"]["code"] == "bundle_disabled"
    finally:
        assert tools["set_society_bundle_enabled"].fn(namespace=h.NS, enabled=True)["enabled"] is True
    assert json.dumps(tools["social_protection_source_contracts"].fn())
