"""Courts and justice-statistics MCP entry points: catalog registration, scopes, answers and reviews (#2427)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.kb.courts_justice import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import courts_justice_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.courts_justice import (
    COURTS_JUSTICE_SCOPES,
    COURTS_JUSTICE_TOOLS,
    COURTS_JUSTICE_WRITES,
)
from tools.knowledge_engine_mcp.legal import LEGAL_TOOLS


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "courts-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn)
    h.seed_us_code(conn)
    h.seed_ownership(conn)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.REVIEW_SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_registered_in_the_legal_module_with_their_scopes(mcp_env):
    tools, _ = mcp_env
    assert COURTS_JUSTICE_TOOLS <= set(tools) and COURTS_JUSTICE_TOOLS <= LEGAL_TOOLS
    assert set(COURTS_JUSTICE_SCOPES) == COURTS_JUSTICE_TOOLS
    for name in COURTS_JUSTICE_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in COURTS_JUSTICE_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == COURTS_JUSTICE_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    assert COURTS_JUSTICE_TOOLS <= {t["name"] for t in catalog["tools"]}
    for provider in ("legal.courts", "legal.justice-statistics"):
        descriptor = json.loads((h.ROOT / f"packs/legal/providers/{provider}.json").read_text())
        assert {op["tool"].split(".", 1)[1] for op in descriptor["operations"]} <= COURTS_JUSTICE_TOOLS
    assert "legal advice" in tools["lookup_dockets"].description
    assert "safety ratings" in tools["justice_statistics_for_place"].description


def test_answers_links_reviews_and_monitors_through_mcp(mcp_env):
    tools, state = mcp_env
    linked = tools["link_court_citations"].fn(namespace=h.NS)
    assert linked["links"]
    answer = tools["lookup_dockets"].fn(namespace=h.NS, provision="42 U.S.C. § 1983", as_of="2099-08-01")
    assert answer.get("status") == "answered", answer
    assert forbidden_keys(answer) == []
    refused = tools["lookup_dockets"].fn(namespace=h.NS, entity_id="Jane Roe")
    assert refused["ok"] is False and refused["error"]["code"] == "natural_person_not_a_query_key"
    stats = tools["justice_statistics_for_place"].fn(namespace=h.NS, place="eurostat-geo:DE")
    assert stats["status"] == "answered"
    compared = tools["compare_justice_statistics"].fn(namespace=h.NS, places=["us-state:EX", "eurostat-geo:DE"])
    assert compared["comparison"]["status"] == "refused_no_comparability_note"
    proposed = tools["propose_court_party_matches"].fn(namespace=h.NS, ownership_namespace=h.NS)
    (candidate,) = proposed["candidates"]
    state["principal"] = "bob"
    reviewed = tools["review_court_party_match"].fn(namespace=h.NS, candidate_id=candidate["candidate_id"],
                                                    decision="accept", reason="filing checked")
    assert reviewed["state"] == "accepted"
    monitor = tools["create_courts_justice_monitor"].fn(namespace=h.NS, request_key="mcp", watch="court", key="dcd")
    assert monitor["subscription_id"]
    contracts = tools["courts_justice_source_contracts"].fn()
    assert contracts["live_verification"]["courtlistener"]["status"] == "unverified-live"
    state["scopes"] = set(h.READ_ONLY)
    denied = tools["revert_court_party_match"].fn(namespace=h.NS, candidate_id=candidate["candidate_id"], reason="x")
    assert denied["ok"] is False
