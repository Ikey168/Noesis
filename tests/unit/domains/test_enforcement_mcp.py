"""Legal regulatory enforcement MCP tools: registration, scopes, exclusions and the minimisation guard (#2710)."""

from __future__ import annotations

import asyncio
import json

import pytest

from src.kb.enforcement import EnforcementError
from tests.unit import enforcement_harness as h
from tools.knowledge_engine_mcp.enforcement import (
    ENFORCEMENT_SCOPES,
    ENFORCEMENT_TOOLS,
    ENFORCEMENT_WRITES,
    guarded,
)


def test_tools_are_registered_through_the_legal_pack_with_scopes():
    from tools.knowledge_engine_mcp.legal import (
        LEGAL_SCOPES,
        LEGAL_TOOLS,
        LEGAL_WRITES,
        required_scopes,
    )

    assert ENFORCEMENT_TOOLS <= LEGAL_TOOLS and ENFORCEMENT_WRITES <= LEGAL_WRITES
    assert set(ENFORCEMENT_SCOPES) == ENFORCEMENT_TOOLS
    for tool in ENFORCEMENT_TOOLS:
        assert LEGAL_SCOPES[tool] == ENFORCEMENT_SCOPES[tool]
        assert required_scopes(tool, "write" if tool in ENFORCEMENT_WRITES else "read") == ENFORCEMENT_SCOPES[tool]


def test_the_catalog_lists_every_tool():
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    names = {t["name"] for t in catalog["tools"] if t.get("server") == "noesis-knowledge-engine"}
    assert ENFORCEMENT_TOOLS <= names


def test_tool_descriptions_declare_the_exclusions():
    from mcp.server.fastmcp import FastMCP

    from tools.knowledge_engine_mcp.enforcement import register

    mcp = FastMCP("test")
    register(mcp, lambda fn, **_: fn(None), lambda: ("alice", set()))
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    assert set(tools) == ENFORCEMENT_TOOLS
    for name in ("enforcement_actions_for_entity", "enforcement_actions_by_authority"):
        text = tools[name].description.lower()
        assert "no risk or compliance scoring" in text or "no ranking, scoring" in text
    assert "individuals are never proposed" in " ".join(tools["propose_enforcement_identity_matches"].description.split())


def test_outputs_pass_the_minimisation_guard_and_carry_the_exclusions():
    clean = guarded({"actions": [{"title": "Exampla Holdings plc and [individual]"}]})
    assert "no profiling of named individuals" in clean["exclusions"]
    for bad in ({"risk_score": 3}, {"actions": [{"respondent": {"date_of_birth": "1970-01-01"}}]},
                {"finding_of_wrongdoing": True}):
        with pytest.raises(EnforcementError) as exc:
            guarded(bad)
        assert exc.value.code == "minimisation_violation"


def test_tools_answer_over_the_fixtures():
    from mcp.server.fastmcp import FastMCP

    from tools.knowledge_engine_mcp.enforcement import register

    conn = h.connection()
    h.reviewed(conn)

    def safe(fn, **_):
        return fn(conn)

    mcp = FastMCP("test")
    register(mcp, safe, lambda: ("alice", h.SCOPES))

    def call(name, **arguments):
        result = asyncio.run(mcp.call_tool(name, arguments))
        payload = result[1] if isinstance(result, tuple) else result
        return payload.get("result", payload) if isinstance(payload, dict) else json.loads(payload[0].text)

    answer = call("enforcement_actions_for_entity", namespace=h.NS, entity=h.HOLD_ENTITY,
                  ownership_namespace=h.OWN_NS, group=True)
    assert {a["action_key"] for a in answer["actions"]} == {h.SEC_LR, h.FCA_EX, h.EDPB_EX}
    assert answer["exclusions"]
    bundle = call("export_enforcement_evidence_bundle", namespace=h.NS, ownership_namespace=h.OWN_NS,
                  entity=h.HOLD_ENTITY, group=True)["bundle"]
    cited = {b["id"] for b in bundle["bibliography"]}
    assert all(set(a["citations"]) <= cited for a in bundle["sections"][0]["assertions"])
    assert all(b["source"]["provider"] and b["record_revision"]["revision_id"] and b["as_of"]["record_time_ms"]
               for b in bundle["bibliography"])
    contracts = call("enforcement_source_contracts")
    assert set(contracts["contracts"]) == {"us-sec", "uk-fca", "us-epa-echo", "edpb-art60"}
