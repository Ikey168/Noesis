"""Development-finance MCP entry points: catalog registration, exact scopes and not-ready reads (#2035, #2037)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit.funding import development_finance_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.development_finance import (
    DEVELOPMENT_FINANCE_SCOPES,
    DEVELOPMENT_FINANCE_TOOLS,
    DEVELOPMENT_FINANCE_WRITES,
)

NS_READ = f"namespace:{h.NS}:read"
NS_WRITE = f"namespace:{h.NS}:write"


def _server(monkeypatch, path):
    state = {"principal": "alice", "scopes": set()}
    monkeypatch.setattr(
        server, "_context", lambda: (state["principal"], state["scopes"])
    )
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    return tool_map(server.mcp), state


@pytest.fixture()
def loaded(tmp_path, monkeypatch):
    path = str(tmp_path / "devfin.duckdb")
    env = h.Env(path).load()
    h.seed_ownership(env.conn)
    env.conn.close()
    return _server(monkeypatch, path)


def call(tools, state, name, scopes, **kwargs):
    state["scopes"] = set(scopes)
    return tools[name].fn(**kwargs)


def test_tools_are_registered_in_the_catalog_with_every_scope_they_always_use(loaded):
    tools, _ = loaded
    assert (
        DEVELOPMENT_FINANCE_TOOLS <= set(tools)
        and set(DEVELOPMENT_FINANCE_SCOPES) == DEVELOPMENT_FINANCE_TOOLS
    )
    catalog = json.loads(
        (h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in DEVELOPMENT_FINANCE_TOOLS:
        mutability = _mutability(name)
        assert mutability == (
            "write" if name in DEVELOPMENT_FINANCE_WRITES else "read"
        ), name
        assert (
            _required_scopes("knowledge_engine_mcp", mutability, name)
            == DEVELOPMENT_FINANCE_SCOPES[name]
        )
        assert by_name[name]["required_scopes"] == DEVELOPMENT_FINANCE_SCOPES[name]
    for name in (
        "propose_development_finance_identity_matches",
        "run_development_finance_monitor",
    ):
        assert "conditional scope" in tools[name].description.lower()


def test_reads_work_with_exactly_their_declared_scopes_and_fail_without_them(loaded):
    tools, state = loaded
    requests = {
        "development_finance_source_contracts": {},
        "development_finance_readiness": {"namespace": h.NS},
        "list_development_finance_activities": {"namespace": h.NS, "country": "KE"},
        "inspect_development_finance_activity": {
            "namespace": h.NS,
            "iati_identifier": "XM-DAC-99901-FICT-0001",
        },
        "search_development_finance_transactions": {
            "namespace": h.NS,
            "publisher": h.FDPA,
        },
        "development_finance_publisher_coverage": {"namespace": h.NS},
        "lookup_crs_aggregates": {"namespace": h.NS, "recipient": "KEN"},
        "list_world_bank_project_links": {"namespace": h.NS},
        "list_development_finance_identity_candidates": {"namespace": h.NS},
    }
    for name, kwargs in requests.items():
        result = call(
            tools, state, name, {*DEVELOPMENT_FINANCE_SCOPES[name], NS_READ}, **kwargs
        )
        assert result.get("ok") is not False, (name, result)
        if DEVELOPMENT_FINANCE_SCOPES[name]:
            for missing in DEVELOPMENT_FINANCE_SCOPES[name]:
                denied = call(
                    tools,
                    state,
                    name,
                    {*DEVELOPMENT_FINANCE_SCOPES[name], NS_READ} - {missing},
                    **kwargs,
                )
                assert (
                    denied.get("ok") is False
                    and denied["error"]["code"] == "unauthorized"
                ), (name, missing)
    listed = call(
        tools,
        state,
        "list_development_finance_activities",
        {*DEVELOPMENT_FINANCE_SCOPES["list_development_finance_activities"], NS_READ},
        namespace=h.NS,
        country="KE",
    )
    assert (
        listed["receipt"]["stored"] is False
    )  # a read-only connection computes the receipt, never writes it


def test_writes_work_with_exactly_their_declared_scopes(loaded):
    tools, state = loaded
    proposed = call(
        tools,
        state,
        "propose_development_finance_identity_matches",
        {
            *DEVELOPMENT_FINANCE_SCOPES["propose_development_finance_identity_matches"],
            NS_READ,
            NS_WRITE,
        },
        namespace=h.NS,
        ownership_namespace=h.NS,
    )
    assert proposed.get("ok") is not False, proposed
    candidate = next(
        c for c in proposed["candidates"] if "companies-house:99000001" in c["records"]
    )
    reviewed = call(
        tools,
        state,
        "review_development_finance_identity_match",
        {
            *DEVELOPMENT_FINANCE_SCOPES["review_development_finance_identity_match"],
            NS_READ,
            NS_WRITE,
        },
        namespace=h.NS,
        candidate_id=candidate["candidate_id"],
        decision="accept",
        reason="same Companies House number",
    )
    assert reviewed.get("ok") is not False and reviewed["state"] == "accepted", reviewed
    recorded = call(
        tools,
        state,
        "record_development_finance_answer",
        {
            *DEVELOPMENT_FINANCE_SCOPES["record_development_finance_answer"],
            NS_READ,
            NS_WRITE,
        },
        namespace=h.NS,
        kind="activities",
        request={"country": "KE"},
    )
    assert recorded.get("ok") is not False and recorded["receipt"]["stored"] is True, (
        recorded
    )
    cited = call(
        tools,
        state,
        "development_finance_report_citation",
        {*DEVELOPMENT_FINANCE_SCOPES["development_finance_report_citation"], NS_READ},
        namespace=h.NS,
        answer_id=recorded["receipt"]["answer_id"],
    )
    assert cited["bibliography"]["id"] == recorded["receipt"]["answer_id"]
    monitor = call(
        tools,
        state,
        "create_development_finance_monitor",
        {
            *DEVELOPMENT_FINANCE_SCOPES["create_development_finance_monitor"],
            NS_READ,
            NS_WRITE,
        },
        namespace=h.NS,
        request_key="m1",
        filters={"publishers": [h.FDPA]},
    )
    assert monitor.get("ok") is not False, monitor
    run = call(
        tools,
        state,
        "run_development_finance_monitor",
        {
            *DEVELOPMENT_FINANCE_SCOPES["run_development_finance_monitor"],
            NS_READ,
            NS_WRITE,
        },
        namespace=h.NS,
        subscription_id=monitor["subscription_id"],
    )
    assert run.get("ok") is not False and run["notifications"], run
    polled = call(
        tools,
        state,
        "poll_development_finance_monitor",
        {*DEVELOPMENT_FINANCE_SCOPES["poll_development_finance_monitor"], NS_READ},
        namespace=h.NS,
        subscription_id=monitor["subscription_id"],
    )
    assert polled.get("ok") is not False and polled["events"], polled


def test_every_read_before_any_source_ran_is_not_ready(tmp_path, monkeypatch):
    path = str(tmp_path / "empty.duckdb")
    duckdb.connect(path).close()
    tools, state = _server(monkeypatch, path)
    for name, kwargs in {
        "list_development_finance_activities": {"namespace": h.NS},
        "inspect_development_finance_activity": {
            "namespace": h.NS,
            "iati_identifier": "x",
        },
        "search_development_finance_transactions": {"namespace": h.NS},
        "development_finance_publisher_coverage": {"namespace": h.NS},
        "lookup_crs_aggregates": {"namespace": h.NS},
        "list_world_bank_project_links": {"namespace": h.NS},
    }.items():
        result = call(
            tools, state, name, {*DEVELOPMENT_FINANCE_SCOPES[name], NS_READ}, **kwargs
        )
        assert result.get("ok") is False and result["error"]["code"] == "not_ready", (
            name,
            result,
        )
    readiness = call(
        tools,
        state,
        "development_finance_readiness",
        {*DEVELOPMENT_FINANCE_SCOPES["development_finance_readiness"], NS_READ},
        namespace=h.NS,
    )
    assert readiness["stores_ready"] is False
