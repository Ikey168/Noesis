"""Holding-structure classification and dossier through the knowledge-engine MCP tools (#2890)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit.ownership import structure_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.ownership import OWNERSHIP_TOOLS
from tools.knowledge_engine_mcp.ownership_structures import (
    STRUCTURE_SCOPES,
    STRUCTURE_TOOLS,
    STRUCTURE_WRITES,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "structures.duckdb")
    conn = duckdb.connect(path)
    h.load_ownership(conn)
    h.load_donations(conn)
    h.load_lobbying(conn)
    h.load_payments(conn)
    conn.close()
    state = {"principal": h.ANALYST, "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    h.enable(monkeypatch)
    return tool_map(server.mcp), state


def test_tools_are_registered_with_scopes_mutability_and_the_provider_descriptor():
    assert STRUCTURE_TOOLS <= OWNERSHIP_TOOLS
    for name in STRUCTURE_TOOLS:
        assert _mutability(name) == ("write" if name in STRUCTURE_WRITES else "read")
        assert _required_scopes("knowledge_engine_mcp", _mutability(name), name) == STRUCTURE_SCOPES[name]
    descriptor = json.loads((ROOT / "packs/corporate-ownership/providers/ownership.core.json").read_text())
    bound = {op["tool"].split(".", 1)[1] for op in descriptor["operations"]}
    assert STRUCTURE_TOOLS <= bound
    dossier = next(c for c in descriptor["capabilities"] if c["id"] == "ownership.dossier")
    assert {"structure_classification", "structure_dossier"} <= set(dossier["semantic_constraints"])
    from src.composition.contracts import validate_provider_descriptor

    assert validate_provider_descriptor(descriptor) == []


def test_classify_build_and_export_through_mcp(mcp_env):
    tools, state = mcp_env
    proposed = tools["propose_ownership_structure_classification"].fn(
        namespace=h.OWN_NS, entity_key=h.STRUCTURE, structure_type="holding_company", basis="self-description",
        stated_value="holding company", citation=h.SELF_DESCRIPTION)
    assert proposed["state"] == "proposed"
    refused = tools["propose_ownership_structure_classification"].fn(
        namespace=h.OWN_NS, entity_key=h.FAMILY_OFFICE, structure_type="family_office", basis="self-description",
        stated_value="Family Office", citation={"kind": "ownership-record", "record_id": "x", "revision": 1,
                                                "field": "name"})
    assert refused["error"]["code"] == "name_inference_refused"
    state["principal"] = h.REVIEWER
    tools["review_ownership_structure_classification"].fn(namespace=h.OWN_NS,
                                                           classification_id=proposed["classification_id"],
                                                           decision="accept", reason="annual report states it")
    offered = tools["propose_ownership_structure_classifications_from_records"].fn(namespace=h.OWN_NS)
    assert len(offered["proposed"]) == 1
    listed = tools["list_ownership_structure_classifications"].fn(namespace=h.OWN_NS, entity_key=h.FAMILY_OFFICE)
    assert listed["entity"]["status"] == "unclassified" and listed["classifications"] == []
    dossier = tools["build_ownership_structure_dossier"].fn(namespace=h.OWN_NS, scheme="gb-coh", value="09990002",
                                                            as_of="2099-06-01", sections=h.SECTIONS)
    assert dossier["structure"]["structure_types"] == ["holding_company"]
    assert {dossier["coverage"][n] for n in ("lobbying", "political_donations", "public_payments")} == {"assembled"}
    assert dossier["coverage"]["offshore_leaks"] == "not_requested"
    bundle = tools["export_ownership_structure_dossier"].fn(namespace=h.OWN_NS, scheme="gb-coh", value="09990002",
                                                            as_of="2099-06-01", sections=h.SECTIONS)
    assert verify_bundle(bundle).errors == []
