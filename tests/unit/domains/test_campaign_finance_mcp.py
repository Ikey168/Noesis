"""Campaign-finance MCP entry points: catalog registration, declared scopes, minimised answers and reviews (#2523)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.campaign_finance_records import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit import campaign_finance_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.campaign_finance import (
    CAMPAIGN_FINANCE_SCOPES,
    CAMPAIGN_FINANCE_TOOLS,
    CAMPAIGN_FINANCE_WRITES,
)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "campaign-finance-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn)
    h.load_ownership(conn)
    h.load_lobbying(conn)
    h.load_elections(conn)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.REVIEW_SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return tool_map(server.mcp), state


def test_tools_are_registered_with_every_scope_they_read_and_write(mcp_env):
    tools, _ = mcp_env
    assert CAMPAIGN_FINANCE_TOOLS <= set(tools)
    assert set(CAMPAIGN_FINANCE_SCOPES) == CAMPAIGN_FINANCE_TOOLS
    for name in CAMPAIGN_FINANCE_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in CAMPAIGN_FINANCE_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == CAMPAIGN_FINANCE_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    assert CAMPAIGN_FINANCE_TOOLS <= {t["name"] for t in catalog["tools"]}
    descriptor = json.loads((h.ROOT / "packs/political/providers/political.campaign-finance.json").read_text())
    assert {op["tool"].split(".", 1)[1] for op in descriptor["operations"]} <= CAMPAIGN_FINANCE_TOOLS
    description = tools["campaign_finance_totals_as_of"].description.lower()
    assert "no influence score" in description
    assert "individual" in tools["campaign_finance_filing_items"].description.lower()


def test_totals_items_identity_links_and_bundles_through_mcp_honour_minimisation(mcp_env):
    tools, state = mcp_env
    totals = tools["campaign_finance_totals_as_of"].fn(namespace=h.NS, committee=h.CAMPAIGN, as_of="2099-07-31")
    assert totals["status"] == "answered" and totals["reports"][0]["version_used"]["file_number"] == 1500150
    assert forbidden_keys(totals) == []
    chain = tools["campaign_finance_amendment_chain"].fn(namespace=h.NS, filing="1500150")
    assert [v["file_number"] for v in chain["versions"]] == [1500101, 1500150]
    items = tools["campaign_finance_filing_items"].fn(namespace=h.NS, filing="1500101")
    assert items["individual_items_withheld"] == 1 and "PLACEHOLDER" not in json.dumps(items)
    proposed = tools["propose_campaign_finance_identity_matches"].fn(namespace=h.NS, ownership_namespace=h.OWN_NS)
    (energy,) = [c for c in proposed["candidates"] if "lei:5299EXAMPLEENERGY001" in c["records"]]
    state["principal"] = "bob"
    reviewed = tools["review_campaign_finance_identity_match"].fn(namespace=h.NS, candidate_id=energy["candidate_id"],
                                                                  decision="accept", reason="LEI record agrees")
    assert reviewed["state"] == "accepted" and reviewed["reviewer"] == "bob"
    linked = tools["link_campaign_finance_ownership"].fn(namespace=h.NS, ownership_namespace=h.OWN_NS)
    assert linked["status"] == "linked"
    affiliates = tools["campaign_finance_affiliate_donations"].fn(namespace=h.NS,
                                                                  organisation="lei:5299EXAMPLEINDUSTR01",
                                                                  ownership_namespace=h.OWN_NS, as_of="2099-12-31")
    assert [c["record_key"] for c in affiliates["contributions"]] == ["campaign-finance:fec:sa:1500310:4000101"]
    bundle = tools["export_campaign_finance_evidence_bundle"].fn(namespace=h.NS, query="totals", key=h.CAMPAIGN)
    assert bundle["evidence_bundle"]["bibliography"]
    listed = tools["list_campaign_finance_links"].fn(namespace=h.NS, kind="ownership")
    assert listed["links"] and all(link["filing_revision_id"] for link in listed["links"])
    monitor = tools["create_campaign_finance_monitor"].fn(namespace=h.NS, request_key="mcp", watch="committee",
                                                          key=h.CAMPAIGN)
    assert monitor["subscription_id"]
    state["scopes"] = set(h.READ_ONLY)
    refused = tools["revert_campaign_finance_identity_match"].fn(namespace=h.NS, candidate_id=energy["candidate_id"],
                                                                 reason="x")
    assert refused["ok"] is False
    contracts = tools["campaign_finance_source_contracts"].fn()
    assert contracts["live_verification"]["openfec"]["status"] == "unverified-live"
    assert contracts["minimisation"]["policy"] == "campaign-finance-minimisation-v1"
