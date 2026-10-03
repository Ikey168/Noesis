"""Health-capacity MCP entry points: catalog registration, exact scopes and the boundary sentence (#2215, HS10)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.health_capacity import NEVER_SENTENCE
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit.clinical import health_capacity_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.clinical import (
    CLINICAL_TOOLS,
    HEALTH_CAPACITY_SCOPES,
    HEALTH_CAPACITY_TOOLS,
    HEALTH_CAPACITY_WRITES,
)

NS_READ = f"namespace:{h.NS}:read"
NS_WRITE = f"namespace:{h.NS}:write"


@pytest.fixture(scope="module")
def mcp_env(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("capacity") / "health-capacity-mcp.duckdb")
    env = h.Env(path)
    env.acquire("r1")
    places = h.register_places(env.conn)
    env.conn.close()
    patch = pytest.MonkeyPatch()
    state = {"principal": "alice", "scopes": set()}
    patch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    patch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    yield tool_map(server.mcp), state, places
    patch.undo()


def call(tools, state, name, scopes, **kwargs):
    state["scopes"] = set(scopes)
    return tools[name].fn(**kwargs)


def test_tools_are_registered_in_the_catalog_with_every_scope_they_always_use(mcp_env):
    tools, _, _ = mcp_env
    assert HEALTH_CAPACITY_TOOLS <= set(tools) and set(HEALTH_CAPACITY_SCOPES) == HEALTH_CAPACITY_TOOLS
    assert HEALTH_CAPACITY_TOOLS <= CLINICAL_TOOLS
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in HEALTH_CAPACITY_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in HEALTH_CAPACITY_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == HEALTH_CAPACITY_SCOPES[name]
        assert by_name[name]["required_scopes"] == HEALTH_CAPACITY_SCOPES[name]


def test_the_journey_runs_through_the_tools_with_exactly_their_declared_scopes(mcp_env):
    tools, state, places = mcp_env

    def run(name, extra=(), **kwargs):
        if name in HEALTH_CAPACITY_WRITES:
            denied = call(tools, state, name, {NS_READ, NS_WRITE}, namespace=h.NS, **kwargs)
            assert denied["ok"] is False and denied["error"]["code"] == "unauthorized", name
        result = call(tools, state, name, {*HEALTH_CAPACITY_SCOPES[name], NS_READ, NS_WRITE, *extra},
                      namespace=h.NS, **kwargs)
        assert result.get("ok") is not False, (name, result)
        assert result["boundary"] == NEVER_SENTENCE, name
        return result

    readiness = run("health_capacity_readiness")
    assert readiness["selected"] is False and readiness["providers"]["oecd-health"]["series"] == 4
    assert readiness["providers"]["who-gho"]["evidence_origins"] == ["fixture"]
    indicators = run("list_health_capacity_indicators", domain="beds")["indicators"]
    assert {i["provider"] for i in indicators} == {"who-gho", "oecd-health", "eurostat-health"}
    resolved = run("resolve_health_capacity_places")
    assert len(resolved["matched"]) == 4 and len(resolved["aggregate"]) == 3
    fra = next(r for r in run("health_capacity_comparability")["places"] if r["geography_code"] == "FRA")
    assert run("review_health_capacity_place", resolution_id=fra["resolution_id"], decision="accept",
               reason="ISO code")["review_state"] == "accepted"
    gho = {"provider": "who-gho", "source_code": "WHS6_102"}
    eurostat = {"provider": "eurostat-health", "source_code": "hlth_rs_bds1:HBEDT"}
    mapping = run("propose_health_capacity_mapping", left=gho, right=eurostat, kind="equivalent",
                  evidence="both count staffed hospital beds")
    assert run("review_health_capacity_mapping", mapping_id=mapping["mapping_id"], decision="reject",
               reason="denominators differ")["state"] == "rejected"
    note = run("record_health_capacity_note", left=gho, right=eurostat, relation="different_unit_or_denominator",
               statement="per 10 000 against per 100 000 inhabitants")
    assert run("review_health_capacity_note", note_id=note["note_id"], decision="accept",
               reason="cited")["state"] == "accepted"
    answer = run("health_capacity_as_of", place_id=places["DEU"], as_of="2098-12-31")
    assert answer["status"] == "answered" and set(answer["domains"]["beds"]) == {"who-gho", "oecd-health",
                                                                                   "eurostat-health"}
    (beds,) = answer["domains"]["beds"]["who-gho"]
    assert beds["comparability_notes"][0]["note_id"] == note["note_id"]
    history = run("health_capacity_definition_history", series_id=beds["series_id"])
    assert history["revisions"][0]["version"] == "GHO IMR 2090"
    beside = run("health_capacity_beside_surveillance", place_id=places["DEU"])
    assert beside["status"] == "resolved" and beside["capacity"]
    linked = run("link_health_capacity_economics")
    assert len(linked["cited_not_held"]) == 3
    germany = next(e for e in answer["domains"]["beds"]["eurostat-health"])
    assert run("health_capacity_series_links", series_id=germany["series_id"])["status"] == "cited-not-held"
    monitor = run("create_health_capacity_monitor", request_key="mcp-watch",
                  watch={"place_id": places["DEU"], "domains": ["beds"]}, extra={"knowledge:subscriptions:read"})
    ran = run("run_health_capacity_monitor", subscription_id=monitor["subscription_id"], watermark=1)
    assert {n["kind"] for n in ran["notifications"]} == {"new-release"}
    assert run("poll_health_capacity_monitor", subscription_id=monitor["subscription_id"])
