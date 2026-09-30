"""Space-object registration MCP entry points: scopes, read-only answers, not_ready and writes (#2224, SO12)."""

from __future__ import annotations

import asyncio

import duckdb
import pytest

from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.astronomy import harness as ah
from tests.unit.astronomy import registration_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.astronomy_registration import QUERY_EXAMPLES, REGISTRATION_TOOLS, REGISTRATION_WRITES

NAMESPACE_SCOPES = {f"namespace:{ah.NS}:read", f"namespace:{ah.NS}:write"}
READS = {
    "object_registration_as_of": {"namespace": ah.NS, "identifier": "2099-001A", "as_of": "2099-07-10"},
    "reentry_record": {"namespace": ah.NS, "identifier": "99901"},
    "export_space_registration_evidence": {"namespace": ah.NS, "identifier": "2099-001A", "as_of": "2099-07-10"},
    "list_space_registration_candidates": {"namespace": ah.NS},
    "space_registration_citations": {"namespace": ah.NS},
}
COMPUTED = {"predicted_by_noesis", "collision_probability", "footprint", "military_operator", "true_operator"}


def declared(name):
    return set(_required_scopes("knowledge_engine_mcp", _mutability(name), name))


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    state = {"principal": "alice", "scopes": set(ah.SCOPES) | NAMESPACE_SCOPES,
             "path": str(tmp_path / "registration-mcp.duckdb")}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection",
                        lambda *, read_only: duckdb.connect(state["path"], read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def load(path):
    conn = duckdb.connect(path)
    ah.acquire(conn, "satcat", "2099-07-01")
    h.acquire_all(conn)
    conn.close()


def keys(value):
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys(v)}
    return set()


def test_tools_are_registered_with_declared_mutability_and_examples(mcp_env):
    tools, _ = mcp_env
    assert REGISTRATION_TOOLS <= set(tools)
    for name in REGISTRATION_TOOLS:
        assert _mutability(name) == ("write" if name in REGISTRATION_WRITES else "read"), name
    assert set(QUERY_EXAMPLES) <= REGISTRATION_TOOLS
    assert "none on record" in " ".join(tools["object_registration_as_of"].description.split())


def test_entry_points_are_not_ready_before_any_registration_source_ran(mcp_env):
    tools, state = mcp_env
    duckdb.connect(state["path"]).close()
    for name, arguments in {**READS, "match_space_registration_objects": {"namespace": ah.NS}}.items():
        result = tools[name].fn(**arguments)
        assert result["ok"] is False and result["error"]["code"] == "not_ready", (name, result)
    status = tools["space_object_registration_status"].fn()
    assert status.get("ok", True) is not False and status["features"]


def test_reads_answer_on_a_read_only_connection_with_exactly_the_declared_scopes(mcp_env):
    tools, state = mcp_env
    load(state["path"])
    for name, arguments in READS.items():
        state["scopes"] = declared(name) | NAMESPACE_SCOPES
        result = tools[name].fn(**arguments)
        assert result.get("ok", True) is not False, (name, result)
        assert not keys(result) & COMPUTED, name
        for missing in sorted(declared(name)):
            state["scopes"] = (declared(name) - {missing}) | NAMESPACE_SCOPES
            refused = tools[name].fn(**arguments)
            assert refused["ok"] is False and refused["error"]["code"] == "unauthorized", (name, missing)


def test_writes_work_with_the_declared_scopes_and_optional_scopes_at_call_time(mcp_env):
    tools, state = mcp_env
    load(state["path"])
    for name, arguments in {
        "match_space_registration_objects": {"namespace": ah.NS},
        "match_space_registration_parties": {"namespace": ah.NS},
        "link_space_registration_citations": {"namespace": ah.NS},
        "create_space_registration_monitor": {"namespace": ah.NS, "request_key": "k", "watch": "object",
                                              "target": "2099-001A"},
    }.items():
        state["scopes"] = declared(name) | NAMESPACE_SCOPES
        result = tools[name].fn(**arguments)
        assert result.get("ok", True) is not False, (name, result)
    state["scopes"] = declared("link_space_registration_citations") | NAMESPACE_SCOPES
    refused = tools["link_space_registration_citations"].fn(namespace=ah.NS, legal_namespace="global")
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
