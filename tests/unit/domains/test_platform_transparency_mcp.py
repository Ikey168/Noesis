"""Platform-transparency MCP entry points: catalog registration, declared scopes, exclusions and minimised answers
(#2641)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import platform_transparency_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.platform_transparency import (
    PLATFORM_TRANSPARENCY_SCOPES,
    PLATFORM_TRANSPARENCY_TOOLS,
    PLATFORM_TRANSPARENCY_WRITES,
    guard,
)

PERSONAL = ("PLACEHOLDER", "placeholder_user", "Placeholder notice body", "exampla.example/post", "access_token")


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "platform-transparency-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn)
    h.load_other_packs(conn)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.REVIEW_SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_registered_with_every_scope_they_read_and_write(mcp_env):
    tools, _ = mcp_env
    assert PLATFORM_TRANSPARENCY_TOOLS <= set(tools)
    assert set(PLATFORM_TRANSPARENCY_SCOPES) == PLATFORM_TRANSPARENCY_TOOLS
    for name in PLATFORM_TRANSPARENCY_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in PLATFORM_TRANSPARENCY_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == PLATFORM_TRANSPARENCY_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    assert PLATFORM_TRANSPARENCY_TOOLS <= {t["name"] for t in catalog["tools"]}
    for name in ("political_ads_for_advertiser", "political_ads_for_election", "moderation_statement_counts"):
        description = tools[name].description.lower()
        assert "point estimate" in description or "never converted" in description or "stored records" in \
            description, name
    assert "coordination" in tools["political_ads_for_advertiser"].description.lower()


def test_identity_links_answers_and_bundles_through_mcp_honour_exclusions_and_minimisation(mcp_env):
    tools, state = mcp_env
    proposed = tools["propose_platform_transparency_identity_matches"].fn(namespace=h.NS,
                                                                          ownership_namespace=h.OWN_NS)
    exact = next(c for c in proposed["candidates"] if c["method"] == "published-id")
    reviewed = tools["review_platform_transparency_identity_match"].fn(
        namespace=h.NS, candidate_id=exact["candidate_id"], decision="accept", reason="published FEC id")
    assert reviewed["state"] == "accepted"
    linked = tools["link_platform_transparency_records"].fn(namespace=h.NS, ownership_namespace=h.OWN_NS)
    assert linked["kinds"]["campaign-finance"]["status"] == "linked"
    answer = tools["political_ads_for_advertiser"].fn(namespace=h.NS, advertiser="C00999901")
    assert answer["status"] == "answered" and len(answer["ads"]) == 3 and answer["exclusions_note"]
    counts = tools["moderation_statement_counts"].fn(namespace=h.NS, platform="exampla-social",
                                                     start="2099-05-01", end="2099-05-02")
    assert counts["total"] == 6 and counts["dump_versions"]
    bundle = tools["export_platform_transparency_evidence_bundle"].fn(namespace=h.NS, query="moderation",
                                                                      key="exampla-social", start="2099-05-01",
                                                                      end="2099-05-02")
    assert bundle["evidence_bundle"]["bibliography"]
    contracts = tools["platform_transparency_source_contracts"].fn()
    assert contracts["live_verification"]["lumen"]["status"] == "gated-not-granted"
    notices = tools["platform_takedown_notices"].fn(namespace=h.NS, recipient="Exampla Social")
    assert notices["status"] == "answered"
    state["scopes"] = set(h.SCOPES)  # without the notices scope
    assert tools["platform_takedown_notices"].fn(namespace=h.NS, recipient="Exampla Social")["status"] == "counted"
    refused = tools["platform_transparency_record_history"].fn(
        namespace=h.NS, record_key="platform-transparency:lumen:notice:99000001")
    assert refused["ok"] is False
    text = json.dumps([answer, counts, bundle, notices, proposed])
    assert not [p for p in PERSONAL if p in text]


def test_guard_refuses_point_estimates_and_minimised_fields():
    assert guard({"ads": [{"midpoint": 5}]})["error"]["code"] == "minimisation_violation"
    assert guard({"fields": {"decision_facts": "x"}})["error"]["code"] == "minimisation_violation"
    assert guard({"ads": []})["exclusions_note"]
