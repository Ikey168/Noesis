"""Life-sciences MCP entry points: catalog registration, declared scopes, exclusions and minimisation (#2711)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.kb.lifesci_records import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import lifesci_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.lifesci import LIFESCI_READS, LIFESCI_SCOPES, LIFESCI_TOOLS, LIFESCI_WRITES


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "lifesci-mcp.duckdb")
    h.Env(duckdb.connect(path)).loaded().conn.close()
    state = {"principal": "alice", "scopes": set(h.ALL)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_registered_with_every_scope_they_declare(mcp_env):
    tools, _ = mcp_env
    assert LIFESCI_TOOLS <= set(tools) and set(LIFESCI_SCOPES) == LIFESCI_TOOLS
    assert not LIFESCI_READS & LIFESCI_WRITES
    for name in LIFESCI_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in LIFESCI_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == LIFESCI_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in LIFESCI_TOOLS:
        assert by_name[name]["required_scopes"] == LIFESCI_SCOPES[name]
    descriptor = json.loads((h.ROOT / "packs/science/providers/science.life-sciences.json").read_text())
    assert {op["tool"].split(".", 1)[1] for op in descriptor["operations"]} == LIFESCI_TOOLS
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        assert op["required_scopes"] == LIFESCI_SCOPES[name]
        assert (op["side_effect"] == "local-mutation") == (name in LIFESCI_WRITES)
    for name in ("lifesci_entry_as_of", "lifesci_target_activities", "lifesci_source_contracts",
                 "export_lifesci_evidence_bundle"):
        description = tools[name].description.lower()
        assert "no activity prediction" in description and "no person names" in description, name


def test_answers_declare_exclusions_and_carry_no_person_fields(mcp_env):
    tools, state = mcp_env
    state["scopes"] = set(h.READ)
    entry = tools["lifesci_entry_as_of"].fn(namespace="global", identifier="Q9ZZZ1")
    assert entry["status"] == "answered" and "activity prediction" in entry["exclusions"]
    assert entry["minimisation"] == "no personal data stored" and forbidden_keys(entry) == []
    activities = tools["lifesci_target_activities"].fn(namespace="global", target="CHEMBL9990201")
    assert activities["aggregation"] == "none" and forbidden_keys(activities) == []
    bundle = tools["export_lifesci_evidence_bundle"].fn(namespace="global", target="CHEMBL9990201")
    assert bundle["bundle"]["contract"].startswith("noesis-evidence-bundle")
    ready = tools["lifesci_readiness"].fn(namespace="global")
    assert {f["selected"] for f in ready["features"].values()} == {False}
    assert {p["state"]["last_evidence_origin"] for p in ready["providers"].values()} == {"fixture"}
    assert ready["linked_packs"] == {"chemicals.substances": False, "environment.biodiversity": False,
                                     "clinical.medicines": False}
    refused = tools["propose_lifesci_identity_matches"].fn(namespace="global")
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    state["scopes"] = set()
    contracts = tools["lifesci_source_contracts"].fn()
    assert {v["status"] for v in contracts["live_verification"].values()} == {"unverified-live"}
    assert contracts["minimisation"]["decision"] == "no personal data stored"


def test_writes_review_link_and_monitor_with_declared_scopes(mcp_env):
    tools, state = mcp_env
    state["scopes"] = set(h.WRITE)
    proposed = tools["propose_lifesci_identity_matches"].fn(namespace="global")
    match = next(m for m in proposed["matches"] if {m["left_key"], m["right_key"]} == {"uniprot:P0DZZ1",
                                                                                         "pdb:9ZZ2"})
    state["scopes"] = set(h.ALL)
    state["principal"] = "bob"
    reviewed = tools["review_lifesci_identity_match"].fn(namespace="global", match_id=match["match_id"],
                                                         decision="accept", reason="both sources assert it")
    assert reviewed["state"] == "accepted"
    linked = tools["link_lifesci_records"].fn(namespace="global")
    assert {m["target"] for m in linked["missing"]} >= {"chemicals.substances", "clinical.medicines"}
    monitor = tools["create_lifesci_monitor"].fn(namespace="global", request_key="mcp", accessions=["P0DZZ1"])
    heard = tools["run_lifesci_monitor"].fn(subscription_id=monitor["subscription_id"])
    assert heard["baseline"] and heard["notifications"]
    assert tools["poll_lifesci_monitor"].fn(subscription_id=monitor["subscription_id"])["events"]
