"""Fact-checks MCP entry points: catalog registration, declared scopes, exclusions and minimised answers (#2712)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.kb.fact_checks_records import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import fact_checks_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.fact_checks import (
    FACT_CHECK_SCOPES,
    FACT_CHECK_TOOLS,
    FACT_CHECK_WRITES,
)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "fact-checks-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn)
    h.load_news(conn)
    h.load_source_identity(conn)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.REVIEW_SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_registered_with_every_scope_they_read_and_write_and_declare_exclusions(mcp_env):
    tools, _ = mcp_env
    assert FACT_CHECK_TOOLS <= set(tools)
    assert set(FACT_CHECK_SCOPES) == FACT_CHECK_TOOLS
    for name in FACT_CHECK_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in FACT_CHECK_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == FACT_CHECK_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    assert FACT_CHECK_TOOLS <= {t["name"] for t in catalog["tools"]}
    descriptor = json.loads((h.ROOT / "packs/news/providers/news.fact-checks.json").read_text())
    assert {op["tool"].split(".", 1)[1] for op in descriptor["operations"]} <= FACT_CHECK_TOOLS
    description = tools["fact_checks_for_claim"].description.lower()
    assert "no truth verdict" in description and "no rating normalisation" in description
    assert "claimant scope" in tools["fact_checks_for_claimant"].description.lower()


def test_claims_matches_links_bundles_and_monitors_through_mcp_honour_minimisation(mcp_env):
    tools, state = mcp_env
    proposed = tools["propose_fact_check_matches"].fn(namespace=h.NS)
    seals = [m for m in proposed["matches"] if m["right_key"] == "argument-claim:claim-seals"]
    assert len(seals) == 2
    state["principal"] = "bob"
    for match in seals:
        reviewed = tools["review_fact_check_match"].fn(namespace=h.NS, match_id=match["match_id"], decision="accept",
                                                       reason="the claim appeared in this article")
        assert reviewed["state"] == "accepted" and reviewed["reviewer"] == "bob"
    answer = tools["fact_checks_for_claim"].fn(namespace=h.NS, claim_id="claim-seals", as_of="2025-12-31")
    assert answer["status"] == "answered" and forbidden_keys(answer) == []
    text = json.dumps(answer)
    for withheld in ("Mayor of Example Bay", "Reviewer Placeholder", "Sam Placeholder", "mayor.jpg"):
        assert withheld not in text
    linked = tools["link_fact_checks"].fn(namespace=h.NS)
    assert linked["status"] == "linked"
    assert tools["list_fact_check_links"].fn(namespace=h.NS, kind="news-article")["links"]
    citing = tools["fact_checks_citing_url"].fn(namespace=h.NS, url=h.APPEARANCE)
    assert len(citing["fact_checks"]) == 2
    bundle = tools["export_fact_checks_evidence_bundle"].fn(namespace=h.NS, query="claim", key="claim-seals")
    assert bundle["evidence_bundle"]["bibliography"] and bundle["exclusions"]
    history = tools["fact_check_history"].fn(namespace=h.NS, record_key="fact-checks:ifcn:verifica-example")
    assert history["revisions"][0]["record"]["fields"]["status_as_published"] == "Under renewal"
    status = tools["fact_checks_publisher_status"].fn(namespace=h.NS, site="https://verifica.example.net",
                                                      as_of="2025-03-12")
    assert status["signatories"][0]["status_as_published"] == "Under renewal"
    monitor = tools["create_fact_checks_monitor"].fn(namespace=h.NS, request_key="mcp", watch="query", key="seals")
    assert monitor["subscription_id"]
    state["scopes"] = set(h.READ_ONLY)
    refused = tools["fact_checks_for_claimant"].fn(namespace=h.NS, claimant="Mayor Alex Example")
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    listed = tools["list_fact_check_matches"].fn(namespace=h.NS)
    assert not [m for m in listed["matches"] if m["match_kind"] == "claimant-entity"]
    assert "withheld" in listed["unmatched"]["claimants"]
    reverted = tools["revert_fact_check_match"].fn(namespace=h.NS, match_id=seals[0]["match_id"], reason="x")
    assert reverted["ok"] is False
    contracts = tools["fact_checks_source_contracts"].fn()
    assert contracts["live_verification"]["ifcn"]["status"] == "unverified-live"
    assert contracts["minimisation"]["policy"] == "fact-checks-minimisation-v1"
    assert "truth verdicts by Noesis" in contracts["exclusions"]
