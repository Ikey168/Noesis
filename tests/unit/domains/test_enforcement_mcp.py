"""Enforcement MCP entry points: catalog registration, scopes, exclusions and minimised answers (#2651, EN12)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.kb.enforcement import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import enforcement_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.enforcement import (
    ENFORCEMENT_SCOPES,
    ENFORCEMENT_TOOLS,
    ENFORCEMENT_WRITES,
)
from tools.knowledge_engine_mcp.legal import LEGAL_TOOLS


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "enforcement-mcp.duckdb")
    conn = duckdb.connect(path)
    h.ownership(conn)
    h.load_all(conn)
    h.seed_legal(conn)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.REVIEW_SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_registered_in_the_legal_module_with_scopes_and_exclusions(mcp_env):
    tools, _ = mcp_env
    assert ENFORCEMENT_TOOLS <= set(tools) and ENFORCEMENT_TOOLS <= LEGAL_TOOLS
    assert set(ENFORCEMENT_SCOPES) == ENFORCEMENT_TOOLS
    for name in ENFORCEMENT_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in ENFORCEMENT_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == ENFORCEMENT_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    assert ENFORCEMENT_TOOLS <= {t["name"] for t in catalog["tools"]}
    descriptor = json.loads((h.ROOT / "packs/legal/providers/legal.enforcement.json").read_text())
    assert {op["tool"].split(".", 1)[1] for op in descriptor["operations"]} == ENFORCEMENT_TOOLS
    for name in ("enforcement_actions_for_entity", "enforcement_actions_by_authority",
                 "enforcement_source_contracts"):
        assert "compliance score" in tools[name].description or "compliance scoring" in tools[name].description
    assert "never summed" in tools["enforcement_actions_by_authority"].description
    assert "without admitting or denying" in tools["enforcement_actions_for_entity"].description


def test_review_link_answer_export_and_monitor_through_mcp(mcp_env):
    tools, state = mcp_env
    proposed = tools["propose_enforcement_respondent_matches"].fn(namespace=h.NS, ownership_namespace=h.OWN_NS)
    exact = next(c for c in proposed["candidates"] if c["method"] == "exact-identifier")
    state["principal"] = "bob"
    reviewed = tools["review_enforcement_respondent_match"].fn(namespace=h.NS, candidate_id=exact["candidate_id"],
                                                               decision="accept", reason="CIK stated")
    assert reviewed["state"] == "accepted"
    linked = tools["link_enforcement_records"].fn(namespace=h.NS, ownership_namespace=h.OWN_NS)
    assert linked["links"]
    answer = tools["enforcement_actions_for_entity"].fn(namespace=h.NS, entity=h.SEC_CIK_ENTITY,
                                                        ownership_namespace=h.OWN_NS, as_of="2099-12-31")
    assert answer["status"] == "answered" and forbidden_keys(answer) == []
    assert "Jordan" not in json.dumps(answer) and "natural person 2" in json.dumps(answer)
    refused = tools["enforcement_actions_for_entity"].fn(namespace=h.NS, entity=h.PERSON_ENTITY,
                                                         ownership_namespace=h.OWN_NS)
    assert refused["ok"] is False and refused["error"]["code"] == "natural_person_not_a_query_key"
    by_authority = tools["enforcement_actions_by_authority"].fn(namespace=h.NS, authority="uk-fca")
    assert by_authority["penalties_by_authority_and_currency"][0]["count"] == 3
    exported = tools["export_enforcement_evidence_bundle"].fn(namespace=h.NS, entity=h.SEC_CIK_ENTITY,
                                                              ownership_namespace=h.OWN_NS, as_of="2099-12-31")
    assert verify_bundle(exported["bundle"]).valid
    history = tools["enforcement_action_history"].fn(namespace=h.NS, action_key=h.SEC_LR)
    assert history["revisions"][h.SEC_LR]
    contracts = tools["enforcement_source_contracts"].fn()
    assert contracts["live_verification"]["uk-fca"]["status"] == "unverified-live"
    assert "natural_persons" in contracts["minimisation"]
    readiness = tools["enforcement_readiness"].fn(namespace=h.NS)
    assert set(readiness["features"]) == {"enforcement-sec", "enforcement-fca", "enforcement-epa",
                                          "enforcement-edpb"}
    monitor = tools["create_enforcement_monitor"].fn(namespace=h.NS, request_key="mcp", watch="authority",
                                                     key="uk-fca")
    assert monitor["subscription_id"]
    state["scopes"] = set(h.READ_ONLY)
    denied = tools["revert_enforcement_respondent_match"].fn(namespace=h.NS, candidate_id=exact["candidate_id"],
                                                             reason="x")
    assert denied["ok"] is False
