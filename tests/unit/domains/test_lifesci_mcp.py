"""Life-sciences MCP entry points: catalog registration, declared scopes, exclusions and minimisation (#2652, LS12
#2711)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.lifesci_records import personal_fields
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit import lifesci_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.life_sciences import (
    LIFESCI_SCOPES,
    LIFESCI_TOOLS,
    LIFESCI_WRITES,
)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "lifesci-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn, revisions=True)
    h.seed_all(conn)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return tool_map(server.mcp), state


def test_tools_are_registered_with_every_scope_they_always_read_and_write(mcp_env):
    tools, _ = mcp_env
    assert LIFESCI_TOOLS <= set(tools) and set(LIFESCI_SCOPES) == LIFESCI_TOOLS
    for name in LIFESCI_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in LIFESCI_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == LIFESCI_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in LIFESCI_TOOLS:
        assert by_name[name]["required_scopes"] == LIFESCI_SCOPES[name]
    descriptor = json.loads((h.ROOT / "packs/science/providers/science.life-sciences.json").read_text())
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        assert name in LIFESCI_TOOLS and name not in LIFESCI_WRITES
        assert op["required_scopes"] == LIFESCI_SCOPES[name]
    for name in ("lifesci_entry_as_of", "lifesci_compounds_for_target", "lifesci_source_contracts",
                 "export_lifesci_evidence_bundle"):
        description = tools[name].description.lower()
        assert "no biological or clinical inference" in description and "no personal names" in description, name


def test_reads_with_exactly_the_declared_scopes_writes_are_scoped_and_outputs_are_minimised(mcp_env):
    tools, state = mcp_env
    state["scopes"] = set(h.READ_ONLY)
    entry = tools["lifesci_entry_as_of"].fn(namespace="global", accession="X9EXA1", release="2099_01")
    assert entry["status"] == "answered" and personal_fields(entry) == []
    assert "personal names of authors, depositors or submitters" in entry["exclusions"]
    target = tools["lifesci_compounds_for_target"].fn(namespace="global", target="CHEMBL9900001",
                                                      release="CHEMBL_100")
    assert target["status"] == "answered"
    bundle = tools["export_lifesci_evidence_bundle"].fn(namespace="global", target="CHEMBL9900001",
                                                        release="CHEMBL_99")
    assert bundle["items"] and all(i["record_revision"] and i["as_of"] for i in bundle["items"])
    refused = tools["propose_lifesci_matches"].fn(namespace="global")
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    state["scopes"] = {"knowledge:lifesci:write", "namespace:global:write"}
    proposed = tools["propose_lifesci_matches"].fn(namespace="global")
    assert proposed["matches"] and {m["state"] for m in proposed["matches"]} == {"proposed"}
    state["scopes"] = set(h.READ_ONLY)
    listed = tools["list_lifesci_identity_matches"].fn(namespace="global", state="proposed")
    assert len(listed["matches"]) == len(proposed["matches"])
    unmatched = tools["list_lifesci_unmatched"].fn(namespace="global")
    assert unmatched["count"] > 0
    readiness = tools["lifesci_readiness"].fn(namespace="global")
    assert readiness["stores_ready"] is True and readiness["linked_packs"]["chemicals"] == "present"
    state["scopes"] = set()
    contracts = tools["lifesci_source_contracts"].fn()
    assert contracts["live_verification"]["chembl"]["status"] == "unverified-live"
    assert "excluded" in contracts["personal_data"]
