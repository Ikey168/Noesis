"""Legislation MCP entry points: catalog registration, declared scopes, read-only answers and reviews (#2447)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.legislation import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import legislation_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.legislation import LEGISLATION_SCOPES, LEGISLATION_TOOLS, LEGISLATION_WRITES
from src.mcp_host.introspection import tool_map


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "legislation-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn)
    h.apply_lda(conn)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.REVIEW_SCOPES | {"knowledge:subscriptions:write"})}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return tool_map(server.mcp), state


def test_tools_are_registered_with_every_scope_they_read_and_write(mcp_env):
    tools, _ = mcp_env
    assert LEGISLATION_TOOLS <= set(tools)
    assert set(LEGISLATION_SCOPES) == LEGISLATION_TOOLS
    for name in LEGISLATION_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in LEGISLATION_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == LEGISLATION_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    assert LEGISLATION_TOOLS <= {t["name"] for t in catalog["tools"]}
    descriptor = json.loads((h.ROOT / "packs/political/providers/political.legislation.json").read_text())
    assert {op["tool"].split(".", 1)[1] for op in descriptor["operations"]} <= LEGISLATION_TOOLS
    description = tools["bill_dossier_as_of"].description.lower()
    assert "no passage prediction" in description and "legal-effect" in description


def test_dossier_answers_votes_links_and_reviews_through_mcp(mcp_env):
    tools, state = mcp_env
    built = tools["build_bill_dossier"].fn(namespace=h.NS, bill_key=h.US_BILL, dossier_namespace=h.DOSSIER_NS)
    assert built["jurisdiction"] == "US" and built["revision"] == 1
    linked = tools["link_bill_lobbying"].fn(namespace=h.NS, bill_key=h.US_BILL, dossier_namespace=h.DOSSIER_NS,
                                            lobbying_namespace=h.NS)
    assert linked["status"] == "linked"
    answer = tools["bill_dossier_as_of"].fn(namespace=h.NS, bill_key=h.US_BILL, dossier_namespace=h.DOSSIER_NS,
                                            as_of="2099-06-01", lobbying_namespace=h.NS)
    assert answer["status"] == "answered" and answer["lobbying"]["status"] == "linked"
    assert forbidden_keys(answer) == []
    votes = tools["list_bill_votes"].fn(namespace=h.NS, bill_key=h.US_BILL, dossier_namespace=h.DOSSIER_NS,
                                        as_of="2099-06-01")
    assert len(votes["votes"]["held"]) == 2
    bundle = tools["export_bill_evidence_bundle"].fn(namespace=h.NS, bill_key=h.US_BILL,
                                                     dossier_namespace=h.DOSSIER_NS, as_of="2099-06-01")
    assert bundle["evidence_bundle"]["bibliography"]
    sponsor = tools["lookup_sponsor_bills"].fn(namespace=h.NS, member_key="legislation:member:us-bioguide:S009901")
    assert sponsor["status"] == "answered"
    tools["build_bill_dossier"].fn(namespace=h.NS, bill_key=h.UK_BILL, dossier_namespace=h.DOSSIER_NS)
    (candidate, *_) = tools["list_bill_link_candidates"].fn(namespace=h.NS, bill_key=h.UK_BILL)["candidates"]
    state["principal"] = "bob"
    reviewed = tools["review_bill_record_link"].fn(namespace=h.NS, record_key=candidate["record_key"],
                                                   source_id=candidate["source_id"], bill_key=h.UK_BILL,
                                                   decision="accept", reason="the title names the bill")
    assert reviewed["state"] == "accepted" and reviewed["history"][-1]["by"] == "bob"
    proposed = tools["propose_legislation_identity_matches"].fn(namespace=h.NS)
    assert "candidates" in proposed
    monitor = tools["create_legislation_monitor"].fn(namespace=h.NS, request_key="mcp", watch="bill",
                                                     key=h.UK_BILL)
    assert monitor["subscription_id"]
    state["scopes"] = set(h.READ_ONLY)
    refused = tools["revert_bill_record_link"].fn(namespace=h.NS, review_id=reviewed["review_id"], reason="x")
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    contracts = tools["legislation_source_contracts"].fn()
    assert contracts["live_verification"]["congress-gov"]["status"] == "unverified-live"
