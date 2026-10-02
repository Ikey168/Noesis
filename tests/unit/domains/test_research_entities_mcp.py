"""Research-entities MCP entry points: catalog registration, declared scopes, exclusions, minimised answers, identity
review and evidence bundles (#2639)."""

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
    RESEARCH_ENTITIES_SCOPES,
    RESEARCH_ENTITIES_TOOLS,
    RESEARCH_ENTITIES_WRITES,
)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "research-entities-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn)
    h.load_second(conn)
    h.load_ownership(conn)
    h.seed_papers(conn)
    h.seed_funding(conn)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.REVIEW_SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_registered_with_every_scope_they_read_and_write_and_declare_exclusions(mcp_env):
    tools, _ = mcp_env
    assert RESEARCH_ENTITIES_TOOLS <= set(tools)
    assert set(RESEARCH_ENTITIES_SCOPES) == RESEARCH_ENTITIES_TOOLS
    for name in RESEARCH_ENTITIES_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in RESEARCH_ENTITIES_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == RESEARCH_ENTITIES_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    assert RESEARCH_ENTITIES_TOOLS <= {t["name"] for t in catalog["tools"]}
    descriptor = json.loads((h.ROOT / "packs/science/providers/science.research-entities.json").read_text())
    assert {op["tool"].split(".", 1)[1] for op in descriptor["operations"]} <= RESEARCH_ENTITIES_TOOLS
    for op in descriptor["operations"]:
        assert op["required_scopes"] == RESEARCH_ENTITIES_SCOPES[op["tool"].split(".", 1)[1]]
    assert "never verified authorship" in tools["researcher_asserted_works_as_of"].description
    assert "no ranking or metric" in tools["researcher_asserted_works_as_of"].description.lower()
    assert "never summed" in tools["organisation_lineage_projects_datasets"].description
    assert "researchers are never proposed" in tools["propose_research_entity_identity_matches"].description
    contracts = tools["research_entities_source_contracts"].fn()
    assert contracts["minimisation"]["policy"] == "research-entities-minimisation-v1"


def test_identity_links_answers_and_bundles_through_mcp_honour_minimisation(mcp_env):
    tools, state = mcp_env
    proposed = tools["propose_research_entity_identity_matches"].fn(namespace=h.NS, ownership_namespace=h.OWN_NS)
    domain = next(c for c in proposed["candidates"] if c["method"] == "website-domain"
                  and c["target_key"] == "research-entities:ror:0zzexa101")
    state["principal"] = "bob"
    reviewed = tools["review_research_entity_identity_match"].fn(namespace=h.NS, candidate_id=domain["candidate_id"],
                                                                 decision="accept", reason="same website")
    assert reviewed["state"] == "accepted" and reviewed["reviewer"] == "bob"
    linked = tools["link_research_entities"].fn(namespace=h.NS, ownership_namespace=h.OWN_NS)
    assert linked["providers"]["literature"] == "present"
    organisation = tools["organisation_lineage_projects_datasets"].fn(namespace=h.NS, ror=h.EXAMPLA,
                                                                      as_of="2099-07-01")
    assert [p["project_id"] for p in organisation["projects"]] == [h.EXAMPLAR, h.NORTHWAVE]
    assert forbidden_keys(organisation) == []
    researcher = tools["researcher_asserted_works_as_of"].fn(namespace=h.NS, orcid=h.ADA, as_of="2099-03-01")
    assert researcher["status"] == "answered" and len(researcher["works"]) == 2
    assert not [p for p in h.PERSONAL if p in json.dumps(researcher)]
    bundle = tools["export_research_entities_evidence_bundle"].fn(namespace=h.NS, query="organisation", key=h.EXAMPLA,
                                                                  as_of="2099-07-01")
    assert bundle["evidence_bundle"]["bibliography"] and bundle["exclusions"]
    assert all("as of" in b["text"] for b in bundle["evidence_bundle"]["bibliography"])
    record = tools["research_entity_as_of"].fn(namespace=h.NS, record_key="research-entities:ror:0zznwd303",
                                               as_of="2099-04-01")
    assert record["revision"]["status"] == "active"
    papers = tools["datasets_for_paper"].fn(namespace=h.NS, doi=h.PAPER1)
    assert [d["doi"] for d in papers["datasets"]] == [h.DS1]
    state["scopes"] = h.REVIEW_SCOPES - {h.RESEARCHERS}
    hidden = tools["researcher_asserted_works_as_of"].fn(namespace=h.NS, orcid=h.ADA)
    assert hidden.get("ok") is False or hidden.get("status") != "answered"
    withheld = tools["research_entity_record"].fn(namespace=h.NS, record_key=f"research-entities:orcid:{h.ADA}")
    assert withheld["status"] == "withheld" and "Exampla\"" not in json.dumps(withheld)
    monitor = tools["create_research_entities_monitor"].fn(namespace=h.NS, request_key="mcp", watch="organisation",
                                                           key=h.EXAMPLA)
    assert monitor["subscription_id"]
