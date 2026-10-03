"""Fact-checks MCP entry points: catalog registration, declared scopes, minimised answers and reviews (#2712)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.fact_checks_records import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit import fact_checks_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.fact_checks import (
    FACT_CHECK_SCOPES,
    FACT_CHECK_TOOLS,
    FACT_CHECK_WRITES,
)

WITHHELD = ("Chief Spokesperson", "images.example", "robinsample_fake", "Pat Reviewer")


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "fact-checks-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn)
    h.load_all(conn, version="v2")
    h.load_news(conn)
    h.load_source_identity(conn)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.REVIEW_SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return tool_map(server.mcp), state


def test_tools_are_registered_with_every_scope_they_read_and_write(mcp_env):
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
    assert {op["tool"].split(".", 1)[1] for op in descriptor["operations"]} == FACT_CHECK_TOOLS
    for name in ("fact_checks_of_claim_or_claimant", "fact_checks_citing_article"):
        description = tools[name].description.lower()
        assert "verbatim" in description and "cited" in description
    assert "no truth verdict" in tools["fact_checks_of_claim_or_claimant"].description.lower()


def test_answers_identity_links_bundles_and_monitors_through_mcp_honour_minimisation(mcp_env):
    tools, state = mcp_env
    proposed = tools["propose_fact_check_identity_matches"].fn(namespace=h.NS)
    assert proposed["candidates"] and {c["state"] for c in proposed["candidates"]} == {"proposed"}
    (claim,) = [c for c in proposed["candidates"] if c["right_key"] == "claim-widgets"
                and c["method"] == "shared-appearance-url"]
    state["principal"] = "bob"
    reviewed = tools["review_fact_check_identity_match"].fn(namespace=h.NS, candidate_id=claim["candidate_id"],
                                                            decision="accept", reason="same claim, same article")
    assert reviewed["state"] == "accepted" and reviewed["reviewer"] == "bob"
    linked = tools["link_fact_checks"].fn(namespace=h.NS)
    assert linked["status"] == "linked"
    answer = tools["fact_checks_of_claim_or_claimant"].fn(namespace=h.NS, claim_id="claim-widgets",
                                                          as_of="2099-08-31")
    assert answer["status"] == "answered" and forbidden_keys(answer) == []
    citing = tools["fact_checks_citing_article"].fn(namespace=h.NS, document_id="doc-widgets")
    assert citing["fact_checks"] and citing["subject"]["url_rule"]["rules"] == "wa-canon-v1"
    history = tools["fact_check_revision_history"].fn(namespace=h.NS,
                                                     record_key="fact-check:publisher:northwind-verify.example")
    assert [r["record"]["fields"]["ifcn_status"] for r in history["revisions"]] == ["verified", "expired"]
    bundle = tools["export_fact_check_evidence_bundle"].fn(namespace=h.NS, query="claim", key="claim-widgets")
    assert bundle["evidence_bundle"]["bibliography"]
    listed = tools["list_fact_check_links"].fn(namespace=h.NS, kind="news-article")
    assert listed["links"] and all(link["record_revision_id"] for link in listed["links"])
    monitor = tools["create_fact_check_monitor"].fn(namespace=h.NS, request_key="mcp", watch="publisher",
                                                    key="factdesk.example")
    assert monitor["subscription_id"]
    everything = json.dumps([proposed, answer, citing, history, bundle, listed])
    assert not [p for p in WITHHELD if p in everything]
    state["scopes"] = set(h.READ_ONLY)
    refused = tools["revert_fact_check_identity_match"].fn(namespace=h.NS, candidate_id=claim["candidate_id"],
                                                           reason="x")
    assert refused["ok"] is False
    contracts = tools["fact_check_source_contracts"].fn()
    assert contracts["live_verification"]["google-fact-check-tools"]["status"] == "unverified-live"
    assert contracts["minimisation"]["policy"] == "fact-checks-minimisation-v1"
    assert "no truth verdicts by Noesis" in contracts["exclusions"]
