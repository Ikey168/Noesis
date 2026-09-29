"""E12: bundle declaration, readiness, enablement and the MCP entry points."""

import asyncio

import duckdb
import pytest

from src.kb.environment_bundle import BUNDLE, BundleError, readiness, require_enabled, set_enabled
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.environment import harness
from tests.unit.environment.harness import NS, SCOPES
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.environment import ENVIRONMENT_TOOLS, ENVIRONMENT_WRITES


@pytest.fixture(scope="module")
def database(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("environment-mcp") / "env.duckdb")
    env = harness.world(duckdb.connect(path))
    place_id = env.alexanderplatz["place_id"]
    env.conn.close()
    return path, place_id


@pytest.fixture
def mcp_env(database, monkeypatch):
    path, place_id = database
    state = {"principal": "alice", "scopes": set(SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state, path, place_id


def test_declared_contributions_reuse_existing_owners():
    assert BUNDLE["architecture"]["status"].startswith("composed")
    assert set(BUNDLE["architecture"]["depends_on"]) == {"C02", "C03", "C04", "C05", "C06", "C07"}
    declared = {tool for workflow in BUNDLE["contributions"]["workflows"].values() for tool in workflow["tools"]}
    assert declared <= ENVIRONMENT_TOOLS
    assert "make compliance determinations" in BUNDLE["never"]
    assert "present forecasts or reanalysis as observations" in BUNDLE["never"]
    assert {s["pack_id"] for s in BUNDLE["contributions"]["source_packs"]} == {"climate-environment", "geospatial-berlin"}


def test_tools_are_discoverable_with_preserved_ids_scopes_and_mutability(mcp_env):
    tools = mcp_env[0]
    assert ENVIRONMENT_TOOLS <= set(tools)
    for name in ENVIRONMENT_TOOLS:
        assert _mutability(name) == ("write" if name in ENVIRONMENT_WRITES else "read"), name
    assert _required_scopes("knowledge_engine_mcp", "write", "acquire_environment_source") == [
        "knowledge:environment:write", "knowledge:ingestion:execute"]
    assert _required_scopes("knowledge_engine_mcp", "read", "environment_provider_contracts") == []
    assert _required_scopes("knowledge_engine_mcp", "write", "review_environment_operator_link") == ["knowledge:environment:review"]
    assert _required_scopes("knowledge_engine_mcp", "read", "lookup_environment_series") == ["knowledge:environment:read"]


def test_place_dossier_series_grid_facilities_and_vintages_through_mcp(mcp_env):
    tools, _, _, place_id = mcp_env
    status = tools["climate_environment_bundle_status"].fn(namespace=NS)
    assert status["entry_points"]["acquisition"] == "fixture-only"
    assert status["providers"]["copernicus-cams"]["status"] == "not implemented"
    assert {p["status"] for name, p in status["providers"].items() if name not in {"copernicus-cams", "umweltatlas"}} == {"fixture-only"}
    dossier = tools["build_environment_place_dossier"].fn(namespace=NS, request_key="mcp", place_id=place_id)
    assert dossier["status"] == "complete", dossier
    inspected = tools["inspect_environment_dossier"].fn(namespace=NS, dossier_id=dossier["dossier_id"])
    assert inspected["stale"] is False
    assert tools["replay_environment_dossier"].fn(namespace=NS, dossier_id=dossier["dossier_id"])["deterministic"]
    assert "[forecast]" in tools["export_environment_dossier"].fn(namespace=NS, dossier_id=dossier["dossier_id"])["markdown"]
    listed = tools["lookup_environment_series"].fn(namespace=NS, kind="forecast")
    assert listed["series"] and {s["kind"] for s in listed["series"]} == {"forecast"}
    one = tools["lookup_environment_series"].fn(namespace=NS, record_id=listed["series"][0]["record_id"])
    assert one["kind_notice"] == "forecast values; never an observation"
    events = tools["list_environment_grid_events"].fn(namespace=NS, event_type="unavailability")
    assert {e["unavailability"]["kind"] for e in events["events"]} == {"planned", "unplanned"}
    facilities = tools["list_environment_facilities"].fn(namespace=NS)
    assert {f["operator"]["state"] for f in facilities["facilities"]} == {"unmatched (source string)"}
    compared = tools["compare_environment_vintages"].fn(namespace=NS, record_id=one["record_id"])
    assert compared["status"] == "single_vintage"
    contracts = tools["environment_provider_contracts"].fn()
    assert contracts["live_verification"]["copernicus-cams"]["status"] == "not implemented"


def test_scopes_are_enforced_per_tool(mcp_env):
    tools, state, _, place_id = mcp_env
    state["scopes"] = {"knowledge:environment:read", f"namespace:{NS}:read"}
    denied = tools["build_environment_place_dossier"].fn(namespace=NS, request_key="nope", place_id=place_id)
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    state["scopes"] = SCOPES - {"knowledge:ingestion:execute"}
    blocked = tools["acquire_environment_source"].fn(namespace=NS, provider="dwd", selection={}, observation="o",
                                                      budget_id="b", reuse_notice="n")
    assert blocked["ok"] is False and blocked["error"]["code"] == "unauthorized"
    state["scopes"] = SCOPES
    review = tools["review_environment_operator_link"].fn(namespace=NS, link_id="x", decision="accepted", reason="r")
    assert review["ok"] is False and review["error"]["code"] == "unauthorized"


def test_disabling_blocks_only_environment_entry_points(mcp_env):
    tools, state, path, _ = mcp_env
    state["scopes"] = SCOPES | {"operator"}
    assert tools["set_climate_environment_bundle_enabled"].fn(namespace=NS, enabled=False)["enabled"] is False
    try:
        blocked = tools["list_environment_facilities"].fn(namespace=NS)
        assert blocked["ok"] is False and blocked["error"]["code"] == "bundle_disabled"
        within = tools["query_geospatial_features_within"].fn(namespace="global", collection="alkis_bezirke:bezirksgrenzen",
                                                              boundary_name="Mitte")
        assert within["status"] == "complete" and within["boundary"], within  # Geospatial keeps working
        conn = duckdb.connect(path)
        with pytest.raises(BundleError):
            require_enabled(conn, NS)
        require_enabled(conn, "other-namespace")
        with pytest.raises(BundleError):
            set_enabled(conn, NS, True, principal_id="alice", scopes=SCOPES)
        assert readiness(conn, NS, scopes=SCOPES)["enabled"] is False
        conn.close()
    finally:
        assert tools["set_climate_environment_bundle_enabled"].fn(namespace=NS, enabled=True)["enabled"] is True
