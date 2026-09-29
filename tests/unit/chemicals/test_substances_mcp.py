"""CH11: the Chemicals and Substances MCP entry points, declaration, readiness and enablement (#2313)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.kb.substances_bundle import BUNDLE
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.chemicals import harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.substances import SUBSTANCE_READS, SUBSTANCE_TOOLS, SUBSTANCE_WRITES

NS = h.NS


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    directory = tmp_path_factory.mktemp("chemicals-mcp")
    env = h.loaded_env(directory)
    env.seed_legal_act("32016R2235", "Commission Regulation (EU) 2016/2235",
                       [("Annex XVII/entry 66", "66. Bisphenol A ... thermal paper.")])
    env.seed_notices()
    env.conn.close()
    return str(directory / "chemicals.duckdb")


@pytest.fixture
def mcp_env(database, monkeypatch):
    state = {"principal": "alice", "scopes": set(h.ALL)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(database, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_declared_contributions_and_catalog_scopes():
    declared = {tool for workflow in BUNDLE["contributions"]["workflows"].values() for tool in workflow["tools"]}
    assert declared <= SUBSTANCE_TOOLS
    assert BUNDLE["contributions"]["source_packs"][0]["pack_id"] == "chemicals-substances"
    assert any("safe or unregulated" in item for item in BUNDLE["never"])
    for name in SUBSTANCE_TOOLS:
        assert _mutability(name) == ("write" if name in SUBSTANCE_WRITES else "read"), name
    assert _required_scopes("knowledge_engine_mcp", "read", "substance_source_contracts") == []
    assert _required_scopes("knowledge_engine_mcp", "write", "review_substance_identity") == [
        "knowledge:substances:review"]
    assert _required_scopes("knowledge_engine_mcp", "read", "substance_dossier") == ["knowledge:substances:read"]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    listed = {t["name"] for t in catalog["tools"] if t["name"] in SUBSTANCE_TOOLS}
    assert listed == SUBSTANCE_TOOLS


def test_resolution_review_status_and_dossier_through_mcp(mcp_env):
    tools, state = mcp_env
    status = tools["chemicals_bundle_status"].fn(namespace=NS)
    assert status["entry_points"]["acquisition"] == "fixture-only"
    assert {p["status"] for p in status["providers"].values()} == {"fixture-only"}
    contracts = tools["substance_source_contracts"].fn()
    assert set(contracts["contracts"]) == {"pubchem", "echa-clp", "echa-reach", "comptox"}
    proposed = tools["propose_substance_identities"].fn(namespace=NS)
    bpa = [c for c in proposed["candidates"] if "pubchem:cid:6623" in (c["left_key"], c["right_key"])
           or "comptox:dtxsid:DTXSID7020182" in (c["left_key"], c["right_key"])]
    for candidate in bpa:
        reviewed = tools["review_substance_identity"].fn(namespace=NS, candidate_id=candidate["candidate_id"],
                                                         decision="accept", reason="identifiers agree")
        assert reviewed["state"] == "accepted", reviewed
    resolved = tools["resolve_substance"].fn(namespace=NS, query="80-05-7")
    assert resolved["status"] == "resolved" and len(resolved["substances"][0]["members"]) == 3
    linked = tools["link_substance_citations"].fn(namespace=NS)
    assert linked["legal"]["linked"] and linked["products"]["linked"]
    as_of = tools["substance_status_as_of"].fn(namespace=NS, query="80-05-7", as_of="2017-06-01")
    assert as_of["harmonised_classification"][0]["as_published"]["hazard_classes"][0]["hazard_class_category"] \
        == "Repr. 2"
    dossier = tools["substance_dossier"].fn(namespace=NS, query="bisphenol A", as_of="2026-09-01")
    assert dossier["status"] == "assembled" and dossier["linked_notices"] and dossier["regulations"]
    assert not h.forbidden_keys(dossier)
    none = tools["substance_status_as_of"].fn(namespace=NS, subject_key="pubchem:cid:5988", as_of="2026-09-01")
    assert "not a statement that the substance is safe" in json.dumps(none)
    state["scopes"] = {"knowledge:read"}
    denied = tools["substance_dossier"].fn(namespace=NS, query="80-05-7")
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"


def test_monitor_and_disable_through_mcp(mcp_env):
    tools, state = mcp_env
    created = tools["create_substance_monitor"].fn(namespace=NS, request_key="mcp-1",
                                                   substances=["echa:substance:100.003.829"])
    assert created["subscription_id"].startswith("subscription:"), created
    ran = tools["run_substance_monitor"].fn(namespace=NS, subscription_id=created["subscription_id"])
    assert ran["baseline"] and ran["notifications"]
    polled = tools["poll_substance_monitor"].fn(namespace=NS, subscription_id=created["subscription_id"])
    assert polled["events"]
    state["scopes"] = set(h.ALL) | {"operator"}
    off = tools["set_chemicals_bundle_enabled"].fn(namespace=NS, enabled=False)
    assert off["enabled"] is False
    blocked = tools["resolve_substance"].fn(namespace=NS, query="80-05-7")
    assert blocked["error"]["code"] == "bundle_disabled"
    assert "contracts" in tools["substance_source_contracts"].fn()  # contracts stay readable
    assert tools["set_chemicals_bundle_enabled"].fn(namespace=NS, enabled=True)["enabled"] is True


def test_every_read_tool_is_read_only_against_an_empty_warehouse(tmp_path, monkeypatch):
    path = str(tmp_path / "empty.duckdb")
    duckdb.connect(path).close()
    monkeypatch.setattr(server, "_context", lambda: ("alice", set(h.ALL)))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    for name in sorted(SUBSTANCE_READS - {"substance_source_contracts"}):
        properties = tools[name].parameters.get("properties", {})
        kwargs = {key: "missing" for key in tools[name].parameters.get("required", [])}
        kwargs.update({k: NS for k in ("namespace",) if k in properties})
        result = tools[name].fn(**kwargs)
        assert isinstance(result, dict), name
