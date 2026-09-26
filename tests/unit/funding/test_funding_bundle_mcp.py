"""Bundle composition, readiness and MCP entry points (#1775)."""

import asyncio

import duckdb
import pytest

from src.ingestion.provider_execution import DurableHTTP
from src.kb.funding_bundle import BUNDLE, BundleError, readiness, require_enabled, set_enabled
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.funding import harness
from tests.unit.funding.harness import NS, SCOPES
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.funding import FUNDING_TOOLS, FUNDING_WRITES


@pytest.fixture
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "funding-mcp.duckdb")
    env = harness.Env()
    env.conn.close()
    env.conn = duckdb.connect(path)
    env.store = harness.FundingEvidenceStore(env.conn, now=env.now)
    env.acquire_all()
    harness.founder_profile(env)
    env.conn.close()
    state = {"principal": "alice", "scopes": set(SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state, path


def test_declared_contributions_reuse_existing_owners_and_are_composed():
    assert BUNDLE["architecture"]["status"].startswith("implemented")
    assert set(BUNDLE["architecture"]["depends_on"]) == {"C02", "C03", "C04", "C05", "C06", "C07"}
    assert not any("pending" in value for value in BUNDLE["architecture"]["depends_on"].values())
    workflows = BUNDLE["contributions"]["workflows"]
    assert set(workflows) == {"discovery", "profile_to_shortlist", "application_preparation", "monitoring"}
    assert "ResearchProjectStore" in workflows["application_preparation"]["reuses"]
    assert any("SubscriptionStore" in r for r in workflows["monitoring"]["reuses"])
    declared = {tool for w in workflows.values() for tool in w["tools"]}
    assert declared <= FUNDING_TOOLS
    assert "submit applications" in BUNDLE["never"]


def test_tools_are_discoverable_with_scopes_and_mutability(mcp_env):
    tools, _, _ = mcp_env
    assert FUNDING_TOOLS <= set(tools)
    for name in FUNDING_TOOLS:
        assert _mutability(name) == ("write" if name in FUNDING_WRITES else "read")
    assert _required_scopes("knowledge_engine_mcp", "write", "acquire_funding_source") == [
        "knowledge:funding:write", "knowledge:ingestion:execute"]
    assert _required_scopes("knowledge_engine_mcp", "read", "funding_provider_contracts") == []
    assert _required_scopes("knowledge_engine_mcp", "write", "set_funding_bundle_enabled") == ["operator"]


def test_profile_to_shortlist_to_workspace_through_mcp_reports_fixture_only(mcp_env):
    tools, _, _ = mcp_env
    status = tools["funding_bundle_status"].fn(namespace=NS)
    assert {p["status"] for p in status["providers"].values()} == {"fixture-only"}
    assert status["entry_points"]["discovery"] == "fixture-only"
    listed = tools["list_funding_opportunities"].fn(namespace=NS)
    assert len(listed["opportunities"]) == 9
    conn = duckdb.connect(mcp_env[2], read_only=True)
    profile_id = conn.execute("SELECT profile_id FROM funding_profiles").fetchone()[0]
    conn.close()
    shortlist = tools["build_funding_shortlist"].fn(namespace=NS, profile_id=profile_id)
    apply_now = shortlist["buckets"]["apply_now"]
    assert apply_now
    workspace = tools["create_funding_workspace"].fn(namespace=NS, request_key="w", shortlist_id=shortlist["shortlist_id"],
                                                     opportunity_id=apply_now[0])
    assert workspace["contract"] == "noesis-funding-workspace-v1"
    draft = tools["draft_funding_application"].fn(namespace=NS, workspace_id=workspace["workspace_id"], request_key="d")
    assert tools["export_funding_application_draft"].fn(namespace=NS, draft_id=draft["draft_id"])["export"]["contract"] == "noesis-report-export-v1"


def test_private_state_is_not_reachable_by_other_principals(mcp_env):
    tools, state, path = mcp_env
    conn = duckdb.connect(path, read_only=True)
    profile_id = conn.execute("SELECT profile_id FROM funding_profiles").fetchone()[0]
    conn.close()
    state["principal"] = "mallory"
    denied = tools["inspect_funding_profile"].fn(namespace=NS, profile_id=profile_id)
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    state["principal"], state["scopes"] = "root", SCOPES | {"operator"}
    assert tools["inspect_funding_profile"].fn(namespace=NS, profile_id=profile_id)["error"]["code"] == "unauthorized"


def test_disabling_funding_leaves_shared_providers_and_research_usable(mcp_env):
    tools, state, path = mcp_env
    state["scopes"] = SCOPES | {"operator"}
    assert tools["set_funding_bundle_enabled"].fn(namespace=NS, enabled=False)["enabled"] is False
    blocked = tools["list_funding_opportunities"].fn(namespace=NS)
    assert blocked["ok"] is False and blocked["error"]["code"] == "bundle_disabled"
    project = tools["create_research_project"].fn(namespace=NS, request_key="unrelated", questions=["Q?"], success_criteria=["A"],
                                                  scope={"domains": [], "namespaces": [NS]}, budget={})
    assert project["status"] == "active"
    conn = duckdb.connect(path)
    http = DurableHTTP(conn, budget_id="shared", provider="nlnet", principal_id="p", allowed_hosts={"nlnet.nl"},
                       reuse_notice="shared provider", transport=lambda **_: {"status": 200, "headers": {}, "content": b"ok"})
    assert http.request("k", "https://nlnet.nl/", principal_id="p").content == b"ok"
    with pytest.raises(BundleError):
        require_enabled(conn, NS)
    require_enabled(conn, "other-namespace")
    with pytest.raises(BundleError):
        set_enabled(conn, NS, True, principal_id="alice", scopes=SCOPES)
    assert readiness(conn, NS, scopes=SCOPES)["enabled"] is False
    conn.close()
