"""Bundle declaration, readiness, enablement and MCP entry points (P13)."""

import asyncio

import duckdb
import pytest

from src.kb.funding_profiles import FundingProfileStore
from src.kb.procurement_bundle import BUNDLE, BundleError, readiness, require_enabled, set_enabled
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.procurement.harness import NS, SCOPES, Env, supplier_profile
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.procurement import PROCUREMENT_TOOLS, PROCUREMENT_WRITES


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "procurement-mcp.duckdb")
    env = Env(path=path)
    env.acquire()
    supplier_profile(env)
    env.conn.close()
    state = {"principal": "alice", "scopes": set(SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state, path


def profile_id(path):
    conn = duckdb.connect(path, read_only=True)
    value = conn.execute("SELECT profile_id FROM procurement_profiles").fetchone()[0]
    conn.close()
    return value


def test_declaration_reuses_existing_owners_and_never_submits():
    assert BUNDLE["architecture"]["status"].startswith("composed")
    workflows = BUNDLE["contributions"]["workflows"]
    assert set(workflows) == {"discovery", "profile_to_shortlist", "award_history", "bid_preparation", "monitoring"}
    assert {tool for w in workflows.values() for tool in w["tools"]} <= PROCUREMENT_TOOLS
    assert "ResearchProjectStore" in workflows["bid_preparation"]["reuses"]
    assert set(BUNDLE["contributions"]["reuses_funding"]) == {"profiles", "eligibility", "ranking", "workspaces", "monitoring"}
    assert "submit bids" in BUNDLE["never"] and "contact buyers or procurement portals" in BUNDLE["never"]
    sources = {s["provider"]: s for s in BUNDLE["contributions"]["sources"]}
    assert sources["berlin-vergabe"]["status"] == "not-implemented" and sources["ted"]["live_verification"] == "unverified-live"


def test_tools_are_discoverable_with_scopes_and_mutability(mcp_env):
    tools, _, _ = mcp_env
    assert PROCUREMENT_TOOLS <= set(tools)
    for name in PROCUREMENT_TOOLS:
        assert _mutability(name) == ("write" if name in PROCUREMENT_WRITES else "read")
    assert _required_scopes("knowledge_engine_mcp", "read", "procurement_provider_contracts") == []
    assert _required_scopes("knowledge_engine_mcp", "write", "set_procurement_bundle_enabled") == ["operator"]
    assert _required_scopes("knowledge_engine_mcp", "write", "create_procurement_workspace") == [
        "knowledge:procurement:write", "knowledge:projects:write"]
    assert _required_scopes("knowledge_engine_mcp", "read", "list_procurement_notices") == ["knowledge:procurement:read"]


def test_profile_to_shortlist_to_workspace_through_mcp_reports_fixture_only(mcp_env):
    tools, _, path = mcp_env
    status = tools["procurement_bundle_status"].fn(namespace=NS)
    assert {p["status"] for name, p in status["providers"].items() if name in {"ted", "uk-fts", "uk-cf", "sam-gov"}} == {"fixture-only"}
    assert status["providers"]["service-bund"]["status"] == "not-implemented"
    assert status["evidence"]["live"]["result"] == "blocked"
    listed = tools["list_procurement_notices"].fn(namespace=NS)
    assert len(listed["procedures"]) == 8
    shortlist = tools["build_procurement_shortlist"].fn(namespace=NS, profile_id=profile_id(path))
    assert shortlist["buckets"]["apply_now"]
    workspace = tools["create_procurement_workspace"].fn(namespace=NS, request_key="w", shortlist_id=shortlist["shortlist_id"],
                                                         item_id=shortlist["buckets"]["apply_now"][0])
    assert workspace["contract"] == "noesis-procurement-workspace-v1"
    draft = tools["draft_procurement_bid"].fn(namespace=NS, workspace_id=workspace["workspace_id"], request_key="d")
    assert tools["export_procurement_bid_draft"].fn(namespace=NS, draft_id=draft["draft_id"])["export"]["contract"] == "noesis-report-export-v1"
    history = tools["procurement_award_history"].fn(namespace=NS, buyer="Bezirksamt Fixture-Mitte von Berlin")
    assert history["awards"][0]["semantics"].startswith("award history")
    assert tools["replay_procurement_shortlist"].fn(namespace=NS, shortlist_id=shortlist["shortlist_id"])["identical"]


def test_private_state_is_not_reachable_by_other_principals(mcp_env):
    tools, state, path = mcp_env
    pid = profile_id(path)
    state["principal"] = "mallory"
    denied = tools["inspect_procurement_profile"].fn(namespace=NS, profile_id=pid)
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    state["principal"], state["scopes"] = "root", SCOPES | {"operator"}
    assert tools["inspect_procurement_profile"].fn(namespace=NS, profile_id=pid)["error"]["code"] == "unauthorized"


def test_disabling_procurement_leaves_funding_and_shared_providers_usable(mcp_env):
    tools, state, path = mcp_env
    state["scopes"] = SCOPES | {"operator"}
    assert tools["set_procurement_bundle_enabled"].fn(namespace=NS, enabled=False)["enabled"] is False
    blocked = tools["list_procurement_notices"].fn(namespace=NS)
    assert blocked["ok"] is False and blocked["error"]["code"] == "bundle_disabled"
    state["scopes"] = SCOPES | {"knowledge:funding:read", "knowledge:funding:write", "namespace:grants:write"}
    funding = tools["create_funding_profile"].fn(namespace="grants", request_key="k", label="Applicant")
    assert funding["profile_id"].startswith("funding-profile:")
    project = tools["create_research_project"].fn(namespace=NS, request_key="unrelated", questions=["Q?"], success_criteria=["A"],
                                                  scope={"domains": [], "namespaces": [NS]}, budget={})
    assert project["status"] == "active"
    conn = duckdb.connect(path)
    with pytest.raises(BundleError):
        require_enabled(conn, NS)
    require_enabled(conn, "other-namespace")
    with pytest.raises(BundleError):
        set_enabled(conn, NS, True, principal_id="alice", scopes=SCOPES)
    assert readiness(conn, NS, scopes=SCOPES)["enabled"] is False
    FundingProfileStore(conn)  # the funding store still initialises and serves
    conn.close()
