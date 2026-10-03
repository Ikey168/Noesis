"""WX12 (#2175): weather MCP tools — catalog, declared scopes that suffice, not_ready and answers."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit.weather import fixture_builder as fb
from tests.unit.weather import harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.weather import (
    WEATHER_SCOPES,
    WEATHER_TOOLS,
    WEATHER_WRITES,
)

NS_SCOPES = {f"namespace:{h.NS}:read", f"namespace:{h.NS}:write"}
REQUIRED = {
    "weather_observations",
    "forecast_as_issued",
    "forecast_evolution",
    "warnings_in_force",
    "verify_published_forecasts",
    "propose_weather_station_matches",
    "create_weather_monitor",
}
CALLS = {
    "weather_observations": {
        "namespace": h.NS,
        "station": f"dwd:{fb.DWD}",
        "window_from": "2026-06-10T12:00:00Z",
        "window_to": "2026-06-10T13:00:00Z",
    },
    "forecast_as_issued": {
        "namespace": h.NS,
        "station": f"dwd-mosmix:{fb.MOSMIX}",
        "valid_time": "2026-06-10T13:00:00Z",
        "issued_before": "2026-06-10T11:00:00Z",
    },
    "forecast_evolution": {
        "namespace": h.NS,
        "station": f"dwd-mosmix:{fb.MOSMIX}",
        "valid_time": "2026-06-10T13:00:00Z",
    },
    "warnings_in_force": {
        "namespace": h.NS,
        "point": [13.53, 52.38],
        "as_of": "2026-06-10T21:00:00Z",
    },
    "verify_published_forecasts": {
        "namespace": h.NS,
        "station": f"dwd-mosmix:{fb.MOSMIX}",
        "parameter": "air_temperature",
        "period_from": "2026-06-10T00:00:00Z",
        "period_to": "2026-06-10T23:00:00Z",
    },
    "list_weather_station_matches": {"namespace": h.NS},
    "propose_weather_station_matches": {"namespace": h.NS},
    "weather_source_contracts": {"namespace": h.NS},
}


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "weather-mcp.duckdb")
    state = {"principal": "alice", "scopes": set(h.SCOPES), "path": path}
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
    h.acquire_all(conn, until="2026-06-10T22:30:00Z")
    conn.close()


def test_tools_are_registered_with_scopes_mutability_and_in_the_catalog(mcp_env):
    tools, _ = mcp_env
    assert REQUIRED <= WEATHER_TOOLS <= set(tools)
    for name in WEATHER_TOOLS:
        assert _mutability(name) == ("write" if name in WEATHER_WRITES else "read"), (
            name
        )
        assert (
            _required_scopes("knowledge_engine_mcp", _mutability(name), name)
            == WEATHER_SCOPES[name]
        )
    catalog = json.loads(
        (h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    assert {
        t["name"] for t in catalog["tools"] if t["name"] in WEATHER_TOOLS
    } == WEATHER_TOOLS
    descriptions = " ".join(tools[n].description.lower() for n in WEATHER_TOOLS)
    assert "no advice" in descriptions and "noesis forecasts nothing" in descriptions


def test_every_public_entry_point_is_not_ready_before_any_source_ran(mcp_env):
    tools, state = mcp_env
    duckdb.connect(state["path"]).close()
    for name, arguments in CALLS.items():
        if name == "weather_source_contracts":
            continue
        result = tools[name].fn(**arguments)
        assert result["ok"] is False and result["error"]["code"] == "not_ready", (
            name,
            result,
        )
    for name, arguments in (
        ("run_weather_monitor", {"subscription_id": "subscription:none"}),
        ("poll_weather_monitor", {"subscription_id": "subscription:none"}),
        ("replay_weather_verification", {"namespace": h.NS, "run_id": "x"}),
    ):
        result = tools[name].fn(**arguments)
        assert result["error"]["code"] == "not_ready", (name, result)


def test_each_tool_works_with_exactly_its_declared_scopes_and_refuses_without_any_of_them(
    mcp_env,
):
    tools, state = mcp_env
    load(state["path"])
    for name, arguments in CALLS.items():
        declared = set(WEATHER_SCOPES[name])
        state["scopes"] = declared | NS_SCOPES
        result = tools[name].fn(**arguments)
        assert not (
            isinstance(result, dict)
            and result.get("ok") is False
            and result["error"]["code"] == "unauthorized"
        ), (name, result)
        assert not (isinstance(result, dict) and result.get("ok") is False), (
            name,
            result,
        )
        for missing in declared:
            state["scopes"] = (declared - {missing}) | NS_SCOPES
            refused = tools[name].fn(**arguments)
            assert (
                refused["ok"] is False and refused["error"]["code"] == "unauthorized"
            ), (name, missing)


def test_optional_place_argument_needs_its_scope_at_call_time(mcp_env):
    tools, state = mcp_env
    load(state["path"])
    state["scopes"] = set(WEATHER_SCOPES["weather_observations"]) | NS_SCOPES
    arguments = {
        "namespace": h.NS,
        "point": [13.53, 52.38],
        "window_from": "2026-06-10T12:00:00Z",
        "window_to": "2026-06-10T13:00:00Z",
    }
    refused = tools["weather_observations"].fn(**arguments)
    assert (
        refused["error"]["code"] == "unauthorized"
        and "geospatial:calculate" in refused["error"]["message"]
    )
    state["scopes"] |= {"knowledge:geospatial:calculate"}
    answer = tools["weather_observations"].fn(**arguments)
    assert answer["n"] >= 1 and answer["knowledge_cutoff"] is None


def test_answers_cite_sources_and_warnings_are_quoted_as_issued(mcp_env):
    tools, state = mcp_env
    load(state["path"])
    answer = tools["warnings_in_force"].fn(**CALLS["warnings_in_force"])
    message = answer["in_force"][0]["message"]
    assert message["issuer"] == "Deutscher Wetterdienst" and answer["notice"].endswith(
        "adds no advice"
    )
    forecast = tools["forecast_as_issued"].fn(**CALLS["forecast_as_issued"])
    assert forecast["forecasts"][0]["issuer"] == "Deutscher Wetterdienst"
    assert forecast["forecasts"][0]["notice"].endswith("not made by Noesis")
