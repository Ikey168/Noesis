"""Bundle declaration, enablement, readiness and MCP entry points (#1860)."""

import asyncio
import json
from pathlib import Path

import duckdb
import pytest

from src.kb.ownership_bundle import BUNDLE, BundleError, readiness, require_enabled, set_enabled
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.ownership import harness
from tests.unit.ownership.harness import NS, SCOPES, UK_KEYS
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.ownership import OWNERSHIP_SCOPES, OWNERSHIP_TOOLS, OWNERSHIP_WRITES

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "ownership-mcp.duckdb")
    env = harness.Env(path)
    env.ready()
    env.conn.close()
    state = {"principal": harness.PRINCIPAL, "scopes": set(harness.REVIEW_SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state, path


def test_declaration_reuses_existing_owners_and_never_determines_ownership():
    assert BUNDLE["architecture"]["status"].startswith("composed")
    workflows = BUNDLE["contributions"]["workflows"]
    assert {t for w in workflows.values() for t in w["tools"]} <= OWNERSHIP_TOOLS
    assert "EntityHistoryStore" in workflows["identity"]["reuses"]
    assert "LeiStore" in workflows["acquisition"]["reuses"]
    assert "AuthoredReportStore" in workflows["dossier"]["reuses"]
    assert {"infer beneficial ownership", "make sanctions or AML determinations", "merge entities automatically"} <= set(BUNDLE["never"])


def test_tools_are_in_the_catalog_with_preserved_ids_scopes_and_mutability(mcp_env):
    tools, _, _ = mcp_env
    assert OWNERSHIP_TOOLS <= set(tools)
    for name in OWNERSHIP_TOOLS:
        assert _mutability(name) == ("write" if name in OWNERSHIP_WRITES else "read")
        expected = OWNERSHIP_SCOPES.get(name, ["knowledge:ownership:write" if name in OWNERSHIP_WRITES else "knowledge:ownership:read"])
        assert _required_scopes("knowledge_engine_mcp", _mutability(name), name) == expected
    catalog = json.loads((ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    listed = {t["name"]: t for t in catalog["tools"] if t["name"] in OWNERSHIP_TOOLS}
    assert set(listed) == OWNERSHIP_TOOLS
    assert listed["acquire_ownership_sources"]["required_scopes"] == ["knowledge:ownership:write", "knowledge:ingestion:execute"]
    descriptor = json.loads((ROOT / "packs/corporate-ownership/providers/ownership.core.json").read_text())
    bound = {op["tool"].split(".", 1)[1] for op in descriptor["operations"]}
    # The optional competition feature's tools (#2217) are bound by its own ownership.competition provider.
    competition = json.loads((ROOT / "packs/corporate-ownership/providers/ownership.competition.json").read_text())
    bound |= {op["tool"].split(".", 1)[1] for op in competition["operations"]}
    assert bound == OWNERSHIP_TOOLS - {"ownership_bundle_status", "set_ownership_bundle_enabled"}


def test_lookup_graph_timeline_and_dossier_through_mcp(mcp_env):
    tools, _, _ = mcp_env
    status = tools["ownership_bundle_status"].fn(namespace=NS)
    assert status["providers"]["handelsregister"]["status"] == "not-implemented"
    assert status["providers"]["gleif"]["live_verification"]["status"] == "unverified-live"
    found = tools["lookup_ownership_entity"].fn(namespace=NS, scheme="lei", value=harness.UK)
    assert found["status"] == "found"
    proposed = tools["propose_ownership_identity_matches"].fn(namespace=NS, lei_namespace=NS)
    for item in proposed["candidates"]:
        if harness.DECOY not in (item["left_key"], item["right_key"]):
            reviewed = tools["review_ownership_identity_match"].fn(namespace=NS, candidate_id=item["candidate_id"],
                                                                   decision="accept", reason="fixture")
            assert reviewed["state"] == "accepted"
    graph = tools["ownership_graph"].fn(namespace=NS, query="direct_parents", entity=UK_KEYS["gleif"], as_of="2025-06-01")
    assert graph["conflicts"]
    line = tools["ownership_timeline"].fn(namespace=NS, entity=UK_KEYS["gleif"])
    assert line["entries"] and line["undated"]
    dossier = tools["build_ownership_dossier"].fn(namespace=NS, scheme="lei", value=harness.UK, as_of="2025-06-01",
                                                  evidence_kind="offline-fixture")
    assert dossier["status"] == "assembled" and dossier["evidence"]["kind"] == "offline-fixture"
    exported = tools["export_ownership_dossier"].fn(namespace=NS, scheme="lei", value=harness.UK, request_key="mcp",
                                                    as_of="2025-06-01", evidence_kind="offline-fixture")
    assert exported["report_id"].startswith("report:") and exported["citations"] > 10
    contracts = tools["ownership_provider_contracts"].fn()
    assert contracts["contracts"]["bris"]["status"] == "not-implemented"


def test_scopes_are_enforced_and_disabling_leaves_shared_providers(mcp_env):
    tools, state, path = mcp_env
    state["scopes"] = {"knowledge:ownership:read", f"namespace:{NS}:read"}
    denied = tools["propose_ownership_identity_matches"].fn(namespace=NS)
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    state["scopes"] = SCOPES | {"operator"}
    assert tools["set_ownership_bundle_enabled"].fn(namespace=NS, enabled=False)["enabled"] is False
    blocked = tools["lookup_ownership_entity"].fn(namespace=NS, scheme="lei", value=harness.UK)
    assert blocked["ok"] is False and blocked["error"]["code"] == "bundle_disabled"
    conn = duckdb.connect(path)
    from src.kb.lei import LeiStore

    lei = LeiStore(conn).entity(NS, harness.UK, scopes={"knowledge:companies:read", f"namespace:{NS}:read"})
    assert lei["lei"] == harness.UK  # the shared market.lei owner is not gated by this bundle
    with pytest.raises(BundleError):
        require_enabled(conn, NS)
    require_enabled(conn, "other-namespace")
    with pytest.raises(BundleError):
        set_enabled(conn, NS, True, principal_id=harness.PRINCIPAL, scopes=SCOPES)
    assert readiness(conn, NS, scopes=SCOPES)["enabled"] is False
    assert set_enabled(conn, NS, True, principal_id="operator", scopes={"operator"})["enabled"] is True
