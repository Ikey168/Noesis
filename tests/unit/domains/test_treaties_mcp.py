"""Treaties MCP entry points: catalog registration, scopes, exclusions, answers and reviews (#2636)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.treaties_records import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit import treaties_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.legal import LEGAL_TOOLS
from tools.knowledge_engine_mcp.treaties import (
    TREATIES_SCOPES,
    TREATIES_TOOLS,
    TREATIES_WRITES,
    guard,
)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "treaties-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn)
    h.seed_places(conn)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.REVIEW_SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return tool_map(server.mcp), state


def test_tools_are_registered_in_the_legal_module_with_their_scopes_and_exclusions(mcp_env):
    tools, _ = mcp_env
    assert TREATIES_TOOLS <= set(tools) and TREATIES_TOOLS <= LEGAL_TOOLS
    assert set(TREATIES_SCOPES) == TREATIES_TOOLS
    for name in TREATIES_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in TREATIES_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == TREATIES_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    assert TREATIES_TOOLS <= {t["name"] for t in catalog["tools"]}
    for name in ("treaty_status_as_of", "participant_treaty_actions", "treaty_reservations_and_objections"):
        description = tools[name].description
        assert "legal advice" in description or "legal effect" in description or "compliance" in description


def test_answers_evidence_links_reviews_and_monitors_through_mcp(mcp_env):
    tools, state = mcp_env
    found = tools["lookup_treaty"].fn(namespace=h.NS, treaty="22090A0510(01)")
    assert found["status"] == "answered" and {r["kind"] for r in found["records"]} == {"treaty", "treaty-expression"}
    status = tools["treaty_status_as_of"].fn(namespace=h.NS, treaty="XXVII-99", participant="Exampland",
                                             as_of="2095-01-01")
    assert status["status"] == "actions_on_record" and forbidden_keys(status) == []
    actions = tools["participant_treaty_actions"].fn(namespace=h.NS, participant="Exampland",
                                                     providers=["coe-treaty-office"])
    assert {a["provider"] for a in actions["actions"]} == {"coe-treaty-office"}
    statements = tools["treaty_reservations_and_objections"].fn(namespace=h.NS, treaty="CETS 999")
    assert "example.org" not in json.dumps(statements)
    bundle = tools["export_treaty_evidence_bundle"].fn(namespace=h.NS, query="status", treaty="XXVII-99",
                                                       participant="Exampland", as_of="2095-01-01")
    assert bundle["evidence_bundle"]["bibliography"] and all(
        "depositary revision" in item["text"] for item in bundle["evidence_bundle"]["bibliography"])
    history = tools["treaty_record_history"].fn(namespace=h.NS, record_key=h.UNTC)
    assert history["revisions"][0]["citation"]["as_of_ms"]
    linked = tools["link_treaty_records"].fn(namespace=h.NS)
    assert linked["counts"]["provider-missing"] >= 1
    proposed = tools["propose_treaty_matches"].fn(namespace=h.NS, geo_namespace=h.NS)
    candidate = next(c for c in proposed["candidates"] if c["method"] == "iso3166-code")
    state["principal"] = "bob"
    reviewed = tools["review_treaty_match"].fn(namespace=h.NS, candidate_id=candidate["candidate_id"],
                                               decision="accept", reason="published ISO code")
    assert reviewed["state"] == "accepted"
    listed = tools["list_treaty_identity_candidates"].fn(namespace=h.NS)
    assert listed["unmatched"]["participants"]
    monitor = tools["create_treaties_monitor"].fn(namespace=h.NS, request_key="mcp", watch="treaty", key="999")
    assert monitor["subscription_id"]
    contracts = tools["treaties_source_contracts"].fn()
    assert contracts["live_verification"]["untc"]["status"] == "unverified-live"
    state["scopes"] = set(h.READ_ONLY)
    denied = tools["revert_treaty_match"].fn(namespace=h.NS, candidate_id=candidate["candidate_id"], reason="x")
    assert denied["ok"] is False


def test_the_output_guard_enforces_exclusions_and_minimisation():
    from src.kb.treaties_records import TreatiesError

    assert guard({"statements": [{"text_verbatim": "as published"}]})
    for bad in ({"legal_effect": "binding"}, {"items": [{"signatory_name": "A. Person"}]}, {"compliance": True}):
        with pytest.raises(TreatiesError):
            guard(bad)
