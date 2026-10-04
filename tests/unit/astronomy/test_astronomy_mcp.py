"""Astronomy MCP entry points: catalog, declared scopes, read-only answers and not_ready (#2149, AS11)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit.astronomy import harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.astronomy import (
    ASTRONOMY_TOOLS,
    ASTRONOMY_WRITES,
    OPTIONAL_SCOPES,
    QUERY_EXAMPLES,
)

NAMESPACE_SCOPES = {f"namespace:{h.NS}:read", f"namespace:{h.NS}:write"}
REQUIRED = {
    "small_body_history",
    "orbit_solution_as_of",
    "exoplanet_status_as_of",
    "lookup_launches",
    "orbital_object_history",
    "space_weather_alerts",
    "propose_astronomy_identity_matches",
    "create_astronomy_monitor",
}
READS = {
    "small_body_history": {
        "namespace": h.NS,
        "designation": "2099 AB12",
        "as_of": "2099-05-01",
    },
    "orbit_solution_as_of": {
        "namespace": h.NS,
        "designation": "2099 AB12",
        "as_of": "2099-05-10",
    },
    "impact_risk_listing_as_of": {
        "namespace": h.NS,
        "designation": "2099 AB12",
        "as_of": "2099-03-01",
    },
    "exoplanet_status_as_of": {
        "namespace": h.NS,
        "planet": "TOI-99902.01",
        "as_of": "2099-03-01",
    },
    "lookup_launches": {"namespace": h.NS, "provider": "FICTSPACE"},
    "orbital_object_history": {"namespace": h.NS, "identifier": "99901"},
    "space_weather_alerts": {
        "namespace": h.NS,
        "window_from": "2099-09-01",
        "window_to": "2099-09-02",
    },
    "list_astronomy_identity_candidates": {"namespace": h.NS},
    "astronomy_citations": {"namespace": h.NS},
}
# Keys an answer would carry if Noesis computed something the tracker excludes.
COMPUTED = {
    "ephemeris",
    "state_vector",
    "propagated",
    "conjunction",
    "close_approach",
    "impact_probability_noesis",
    "risk_verdict",
    "hazard",
    "validated_disposition",
    "tle",
    "advice",
}


def declared(name):
    return set(_required_scopes("knowledge_engine_mcp", _mutability(name), name))


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    state = {
        "principal": "alice",
        "scopes": set(h.SCOPES) | NAMESPACE_SCOPES,
        "path": str(tmp_path / "astronomy-mcp.duckdb"),
    }
    monkeypatch.setattr(
        server, "_context", lambda: (state["principal"], state["scopes"])
    )
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(state["path"], read_only=read_only),
    )
    return tool_map(server.mcp), state


def load(path):
    conn = duckdb.connect(path)
    h.acquire_all(conn)
    conn.close()


def keys(value):
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys(v)}
    return set()


def test_tools_are_registered_with_scopes_mutability_examples_and_in_the_catalog(
    mcp_env,
):
    tools, _ = mcp_env
    assert REQUIRED <= ASTRONOMY_TOOLS <= set(tools)
    for name in ASTRONOMY_TOOLS:
        assert _mutability(name) == ("write" if name in ASTRONOMY_WRITES else "read"), (
            name
        )
    catalog = json.loads(
        (h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    assert {
        t["name"] for t in catalog["tools"] if t["name"] in ASTRONOMY_TOOLS
    } == ASTRONOMY_TOOLS
    for path in sorted((h.ROOT / "packs/astronomy/providers").glob("*.json")):
        for op in json.loads(path.read_text())["operations"]:
            assert set(op["required_scopes"]) == declared(
                op["tool"].split(".", 1)[1]
            ), op["id"]
    assert set(QUERY_EXAMPLES) <= ASTRONOMY_TOOLS and all(
        e["semantics"] for e in QUERY_EXAMPLES.values()
    )
    assert "never averaged" in " ".join(
        tools["orbit_solution_as_of"].description.split()
    )


def test_every_public_entry_point_is_not_ready_before_any_source_ran(mcp_env):
    tools, state = mcp_env
    duckdb.connect(state["path"]).close()
    calls = {
        **READS,
        "propose_astronomy_identity_matches": {"namespace": h.NS},
        "link_astronomy_citations": {"namespace": h.NS},
        "create_astronomy_monitor": {
            "namespace": h.NS,
            "request_key": "k",
            "watch": "small_body",
            "target": "2099 AB12",
        },
        "run_astronomy_monitor": {"subscription_id": "knowledge-subscription:none"},
        "poll_astronomy_monitor": {"subscription_id": "knowledge-subscription:none"},
    }
    for name, arguments in calls.items():
        result = tools[name].fn(**arguments)
        assert result["ok"] is False and result["error"]["code"] == "not_ready", (
            name,
            result,
        )


def test_reads_answer_on_a_read_only_connection_with_exactly_the_declared_scopes(
    mcp_env,
):
    tools, state = mcp_env
    load(state["path"])
    for name, arguments in READS.items():
        state["scopes"] = declared(name) | NAMESPACE_SCOPES
        result = tools[name].fn(**arguments)
        assert result.get("ok", True) is not False, (name, result)
        assert isinstance(result["n"], int) and not keys(result) & COMPUTED, name
        for missing in sorted(declared(name)):
            state["scopes"] = (declared(name) - {missing}) | NAMESPACE_SCOPES
            refused = tools[name].fn(**arguments)
            assert (
                refused["ok"] is False and refused["error"]["code"] == "unauthorized"
            ), (name, missing)


def test_writes_work_with_exactly_the_declared_scopes_and_optional_scopes_at_call_time(
    mcp_env,
):
    tools, state = mcp_env
    load(state["path"])
    from src.kb.subscriptions import SubscriptionStore

    conn = duckdb.connect(state["path"])
    SubscriptionStore(conn).commit_watermark(h.NS, 1, kind="ingestion")
    conn.close()
    writes = {
        "propose_astronomy_identity_matches": {"namespace": h.NS},
        "link_astronomy_citations": {"namespace": h.NS},
        "create_astronomy_monitor": {
            "namespace": h.NS,
            "request_key": "k",
            "watch": "exoplanet",
            "target": "TOI-99902.01",
        },
    }
    results = {}
    for name, arguments in writes.items():
        for scope in sorted(declared(name)):
            state["scopes"] = (declared(name) - {scope}) | NAMESPACE_SCOPES
            refused = tools[name].fn(**arguments)
            assert (
                refused["ok"] is False and refused["error"]["code"] == "unauthorized"
            ), (name, scope)
        state["scopes"] = declared(name) | NAMESPACE_SCOPES
        results[name] = tools[name].fn(**arguments)
        assert results[name].get("ok", True) is not False, (name, results[name])
    # A Geospatial namespace needs the Geospatial scopes, checked only when it is given.
    state["scopes"] = declared("propose_astronomy_identity_matches") | NAMESPACE_SCOPES
    refused = tools["propose_astronomy_identity_matches"].fn(
        namespace=h.NS, geo_namespace=h.GEO_NS
    )
    assert (
        refused["ok"] is False
        and OPTIONAL_SCOPES["geo_namespace"][0] in refused["error"]["message"]
    )
    state["scopes"] |= set(OPTIONAL_SCOPES["geo_namespace"])
    assert (
        tools["propose_astronomy_identity_matches"]
        .fn(namespace=h.NS, geo_namespace=h.GEO_NS)
        .get("ok", True)
        is not False
    )
    candidate = next(
        c
        for c in results["propose_astronomy_identity_matches"]["candidates"]
        if c["basis"] == "catalogue-conflict"
    )
    review = {
        "namespace": h.NS,
        "candidate_id": candidate["candidate_id"],
        "decision": "reject",
        "reason": "the catalogues disagree; kept apart",
    }
    state["scopes"] = declared("review_astronomy_identity_match") | NAMESPACE_SCOPES
    assert tools["review_astronomy_identity_match"].fn(**review)["state"] == "rejected"
    state["scopes"] = declared("revert_astronomy_identity_match") | NAMESPACE_SCOPES
    assert (
        tools["revert_astronomy_identity_match"].fn(
            namespace=h.NS, candidate_id=candidate["candidate_id"], reason="re-check"
        )["state"]
        == "reverted"
    )
    subscription_id = results["create_astronomy_monitor"]["subscription_id"]
    for scope in sorted(declared("run_astronomy_monitor")):
        state["scopes"] = (
            declared("run_astronomy_monitor") - {scope}
        ) | NAMESPACE_SCOPES
        assert (
            tools["run_astronomy_monitor"].fn(subscription_id=subscription_id)["ok"]
            is False
        ), scope
    state["scopes"] = declared("run_astronomy_monitor") | NAMESPACE_SCOPES
    assert (
        tools["run_astronomy_monitor"].fn(subscription_id=subscription_id)["baseline"]
        is True
    )
    state["scopes"] = declared("poll_astronomy_monitor") | NAMESPACE_SCOPES
    assert "events" in tools["poll_astronomy_monitor"].fn(
        subscription_id=subscription_id
    )
