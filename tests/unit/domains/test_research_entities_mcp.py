"""Research-entities MCP entry points: catalog registration, declared scopes, exclusions and minimised output (#2639)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.kb.research_entities_records import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import research_entities_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.research_entities import (
    RESEARCH_ENTITY_READS,
    RESEARCH_ENTITY_SCOPES,
    RESEARCH_ENTITY_TOOLS,
    RESEARCH_ENTITY_WRITES,
)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "research-entities-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn, later=True)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_registered_with_every_scope_they_always_read_and_write(mcp_env):
    tools, _ = mcp_env
    assert RESEARCH_ENTITY_TOOLS <= set(tools) and set(RESEARCH_ENTITY_SCOPES) == RESEARCH_ENTITY_TOOLS
    for name in RESEARCH_ENTITY_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in RESEARCH_ENTITY_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == RESEARCH_ENTITY_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in RESEARCH_ENTITY_TOOLS:
        assert by_name[name]["required_scopes"] == RESEARCH_ENTITY_SCOPES[name]
    descriptor = json.loads((h.ROOT / "packs/science/providers/science.research-entities.json").read_text())
    names = set()
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        names.add(name)
        assert op["required_scopes"] == RESEARCH_ENTITY_SCOPES[name]
        assert (op["side_effect"] == "read-only") == (name in RESEARCH_ENTITY_READS)
    assert names == RESEARCH_ENTITY_TOOLS
    for name in ("research_organisation_as_of", "researcher_assertions_as_of", "research_datasets_for_paper",
                 "research_entities_source_contracts", "export_research_entities_evidence_bundle"):
        description = tools[name].description.lower()
        assert "no researcher rankings or metrics" in description and "author matching" in description, name


def test_outputs_declare_exclusions_and_enforce_minimisation(mcp_env):
    tools, state = mcp_env
    state["scopes"] = set(h.READ_ONLY)
    organisation = tools["research_organisation_as_of"].fn(namespace="global", ror=h.A1)
    assert organisation["status"] == "answered" and forbidden_keys(organisation) == []
    assert "author disambiguation or matching by name" in organisation["exclusions"]
    refused = tools["researcher_assertions_as_of"].fn(namespace="global", orcid=h.R1)
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    history = tools["research_record_history"].fn(namespace="global", kind="dataset", identifier="10.9999/rent.data1")
    creators = [c for r in history["revisions"] for c in r["record"]["creators"]]
    assert all(c["orcid"] is None for c in creators) and any(c.get("orcid_withheld") for c in creators)
    state["scopes"] = set(h.READ_ONLY) | {"knowledge:research-entities:researchers"}
    answer = tools["researcher_assertions_as_of"].fn(namespace="global", orcid=h.R1, as_of="2099-03-01")
    assert answer["status"] == "answered" and set(answer["researcher"]) == {
        "orcid", "display_name", "display_name_withheld", "employments", "works"}
    history = tools["research_record_history"].fn(namespace="global", kind="dataset", identifier="10.9999/rent.data1")
    assert {c["orcid"] for r in history["revisions"] for c in r["record"]["creators"]} == {h.R1, None}
    bundle = tools["export_research_entities_evidence_bundle"].fn(namespace="global", kind="ror", identifier=h.A1)
    assert bundle["bundle"]["contract"] == "noesis-evidence-bundle-v1"
    refused = tools["propose_research_entity_matches"].fn(namespace="global")
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    state["scopes"] = {"knowledge:research-entities:write", "namespace:global:write"}
    proposed = tools["propose_research_entity_matches"].fn(namespace="global")
    assert {m["state"] for m in proposed["matches"]} == {"proposed"}
    state["scopes"] = set()
    contracts = tools["research_entities_source_contracts"].fn()
    assert contracts["live_verification"]["orcid"]["status"] == "unverified-live"
    assert "emails" in contracts["minimisation"]["orcid_excluded"]
