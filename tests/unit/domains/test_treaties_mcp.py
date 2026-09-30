"""Treaties MCP entry points: catalog registration, scopes, exclusions, answers and reviews (#2636)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.kb.treaties_records import (
    TreatiesError,
    forbidden_keys,
    minimisation_violations,
)
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import treaties_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.legal import LEGAL_TOOLS
from tools.knowledge_engine_mcp.treaties import (
    TREATIES_SCOPES,
    TREATIES_TOOLS,
    TREATIES_WRITES,
    guarded,
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
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_registered_in_the_legal_module_with_their_scopes(mcp_env):
    tools, _ = mcp_env
    assert TREATIES_TOOLS <= set(tools) and TREATIES_TOOLS <= LEGAL_TOOLS
    assert set(TREATIES_SCOPES) == TREATIES_TOOLS
    for name in TREATIES_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in TREATIES_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == TREATIES_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    assert TREATIES_TOOLS <= {t["name"] for t in catalog["tools"]}
    descriptor = json.loads((h.ROOT / "packs/legal/providers/legal.treaties.json").read_text())
    assert {op["tool"].split(".", 1)[1] for op in descriptor["operations"]} == TREATIES_TOOLS
    for name in ("treaty_status_as_of", "treaty_reservations_and_objections", "lookup_treaties"):
        assert "legal advice" in tools[name].description


def test_answers_reviews_links_and_monitors_through_mcp(mcp_env):
    tools, state = mcp_env
    answer = tools["treaty_status_as_of"].fn(namespace=h.NS, treaty="cets:990", participant="France",
                                             as_of="2099-06-01")
    assert answer["status"] == "answered", answer
    assert forbidden_keys(answer) == [] and minimisation_violations(answer) == []
    statements = tools["treaty_reservations_and_objections"].fn(namespace=h.NS, treaty="untc:XXIX-99")
    assert any(s["action_type"] == "objection" for s in statements["statements"])
    bundle = tools["export_treaty_evidence_bundle"].fn(namespace=h.NS, treaty="cets:990", participant="France",
                                                       as_of="2099-06-01")
    assert bundle["bundle"]["contract"].startswith("noesis-evidence-bundle")
    proposed = tools["propose_treaty_matches"].fn(namespace=h.NS)
    treaty = next(c for c in proposed["candidates"] if c["kind"] == "treaty")
    state["principal"] = "bob"
    reviewed = tools["review_treaty_match"].fn(namespace=h.NS, candidate_id=treaty["candidate_id"],
                                               decision="accept", reason="CETS citation in the title")
    assert reviewed["state"] == "accepted"
    links = tools["link_treaty_records"].fn(namespace=h.NS)
    assert {p["pack"] for p in links["provider_unavailable"]} == {"legal.core", "legal.sanctions", "economics.trade"}
    monitor = tools["create_treaties_monitor"].fn(namespace=h.NS, request_key="mcp", watch="treaty", key="cets:990")
    assert monitor["subscription_id"]
    contracts = tools["treaties_source_contracts"].fn()
    assert contracts["licence_decisions"]["untc"]["status"] == "declined"
    readiness = tools["treaties_readiness"].fn()
    assert readiness["providers"]["untc"]["live"]["status"] == "declined"
    state["scopes"] = set(h.READ_ONLY)
    denied = tools["revert_treaty_match"].fn(namespace=h.NS, candidate_id=treaty["candidate_id"], reason="x")
    assert denied["ok"] is False


def test_outputs_refuse_person_fields_and_obligation_keys():
    with pytest.raises(TreatiesError):
        guarded({"sources": [{"chain": [{"signatory_name": "A. Person"}]}]})
    with pytest.raises(TreatiesError):
        guarded({"obligations": ["x"]})
    assert guarded({"status": "answered"}) == {"status": "answered"}
