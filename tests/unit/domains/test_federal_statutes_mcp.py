"""Federal statutes MCP entry points: catalog, declared scopes, read-only answers and not_ready (#2105, FL11)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import federal_statutes_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.federal_statutes import (
    FEDERAL_SCOPES,
    FEDERAL_TOOLS,
    FEDERAL_WRITES,
)

NAMESPACE_SCOPES = {f"namespace:{h.NS}:read", f"namespace:{h.NS}:write"}
REQUIRED = {
    "get_federal_provision",
    "compare_provision_versions",
    "list_amendment_acts",
    "decisions_citing_provision",
    "resolve_statutory_citation",
}


def declared(name):
    return set(_required_scopes("knowledge_engine_mcp", _mutability(name), name))


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "federal-mcp.duckdb")
    state = {"principal": "alice", "scopes": set(h.SCOPES), "path": path}
    monkeypatch.setattr(
        server, "_context", lambda: (state["principal"], state["scopes"])
    )
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(state["path"], read_only=read_only),
    )
    return asyncio.run(server.mcp.get_tools()), state


def load(path):
    conn = duckdb.connect(path)
    h.load_all(conn)
    h.dip_dossier(conn)
    conn.close()


def test_tools_are_registered_with_scopes_mutability_and_in_the_catalog(mcp_env):
    tools, _ = mcp_env
    assert REQUIRED <= FEDERAL_TOOLS <= set(tools)
    for name in FEDERAL_TOOLS:
        assert _mutability(name) == ("write" if name in FEDERAL_WRITES else "read"), (
            name
        )
        expected = FEDERAL_SCOPES.get(
            name,
            [
                "knowledge:legal:write"
                if name in FEDERAL_WRITES
                else "knowledge:legal:read"
            ],
        )
        assert (
            _required_scopes("knowledge_engine_mcp", _mutability(name), name)
            == expected
        )
    catalog = json.loads(
        (h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    assert {
        t["name"] for t in catalog["tools"] if t["name"] in FEDERAL_TOOLS
    } == FEDERAL_TOOLS
    descriptor = json.loads(
        (h.ROOT / "packs/legal/providers/legal.federal-statutes.json").read_text()
    )
    assert {
        op["tool"].split(".", 1)[1] for op in descriptor["operations"]
    } <= FEDERAL_TOOLS - FEDERAL_WRITES
    assert (
        "never states that the provision is in force"
        in tools["get_federal_provision"].description.lower()
    )


def test_every_public_read_is_not_ready_before_any_source_ran(mcp_env):
    tools, state = mcp_env
    duckdb.connect(state["path"]).close()
    calls = {
        "get_federal_provision": {
            "namespace": h.NS,
            "statute": "MPHG",
            "provision": "§5",
            "as_of": "2030-01-01",
        },
        "compare_provision_versions": {
            "namespace": h.NS,
            "statute": "MPHG",
            "provision": "§5",
            "left": "2030-01-01",
            "right": "2030-02-01",
        },
        "list_amendment_acts": {"namespace": h.NS},
        "decisions_citing_provision": {"namespace": h.NS, "statute": "MPHG"},
        "list_federal_statute_versions": {"namespace": h.NS, "statute": "MPHG"},
    }
    for name, arguments in calls.items():
        result = tools[name].fn(**arguments)
        assert result["ok"] is False and result["error"]["code"] == "not_ready", (
            name,
            result,
        )
    polled = tools["poll_statute_monitor"].fn(subscription_id="knowledge-subscription:none")
    assert polled["ok"] is False and polled["error"]["code"] == "not_ready"
    assert (
        tools["resolve_statutory_citation"].fn(namespace=h.NS, citation="§ 5 WpHG")[
            "status"
        ]
        == "resolved"
    )


def test_answers_run_on_a_read_only_connection_with_exactly_the_declared_scopes(
    mcp_env,
):
    tools, state = mcp_env
    load(state["path"])
    reads = {
        "get_federal_provision": {
            "namespace": h.NS,
            "statute": "MPHG",
            "provision": "§ 5 Abs. 2 MPHG",
            "as_of": "2031-02-01",
        },
        "compare_provision_versions": {
            "namespace": h.NS,
            "statute": "MPHG",
            "provision": "§5/abs2",
            "left": "2030-03-02",
            "right": "2031-02-01",
        },
        "list_amendment_acts": {"namespace": h.NS, "statute": "MPHG"},
        "decisions_citing_provision": {
            "namespace": h.NS,
            "statute": "MPHG",
            "provision": "§5/abs2",
        },
        "resolve_statutory_citation": {
            "namespace": h.NS,
            "citation": "§ 5 Abs. 2 MPHG a.F.",
        },
        "list_federal_statute_versions": {"namespace": h.NS, "statute": "MPHG"},
    }
    for name, arguments in reads.items():
        state["scopes"] = declared(name) | NAMESPACE_SCOPES
        result = tools[name].fn(**arguments)
        assert result.get("ok", True) is not False, (name, result)
        missing = sorted(declared(name))[0]
        state["scopes"] = (declared(name) - {missing}) | NAMESPACE_SCOPES
        assert tools[name].fn(**arguments)["ok"] is False, name
    state["scopes"] = declared("get_federal_provision") | NAMESPACE_SCOPES
    answer = tools["get_federal_provision"].fn(**reads["get_federal_provision"])
    assert (
        answer["status"] == "observed"
        and answer["label"] == "observed on 2030-06-01, validity not stated"
    )


def test_writes_work_with_exactly_the_declared_scopes(mcp_env):
    tools, state = mcp_env
    load(state["path"])
    from src.kb.subscriptions import SubscriptionStore

    conn = duckdb.connect(state["path"])
    SubscriptionStore(conn).commit_watermark(h.NS, 1, kind="ingestion")
    conn.close()
    writes = {
        "link_amendment_dossiers": {
            "namespace": h.NS,
            "dossier_namespace": h.DOSSIER_NS,
        },
        "create_statute_monitor": {
            "namespace": h.NS,
            "request_key": "k",
            "watch": "provision",
            "statute": "MPHG",
            "provision": "§5/abs2",
        },
    }
    for name, arguments in writes.items():
        for scope in sorted(declared(name)):
            state["scopes"] = (declared(name) - {scope}) | NAMESPACE_SCOPES
            refused = tools[name].fn(**arguments)
            assert (
                refused["ok"] is False and refused["error"]["code"] == "unauthorized"
            ), (name, scope)
        state["scopes"] = declared(name) | NAMESPACE_SCOPES
        result = tools[name].fn(**arguments)
        assert result.get("ok", True) is not False, (name, result)
        if name == "create_statute_monitor":
            subscription_id = result["subscription_id"]
    for scope in sorted(declared("run_statute_monitor")):
        state["scopes"] = (declared("run_statute_monitor") - {scope}) | NAMESPACE_SCOPES
        assert (
            tools["run_statute_monitor"].fn(subscription_id=subscription_id)["ok"]
            is False
        ), scope
    state["scopes"] = declared("run_statute_monitor") | NAMESPACE_SCOPES
    ran = tools["run_statute_monitor"].fn(subscription_id=subscription_id)
    assert ran["baseline"] is True and ran["notifications"]
    state["scopes"] = declared("poll_statute_monitor") | NAMESPACE_SCOPES
    assert tools["poll_statute_monitor"].fn(subscription_id=subscription_id)["events"]
