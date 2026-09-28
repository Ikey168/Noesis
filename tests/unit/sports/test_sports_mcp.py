"""Sports MCP entry points: catalog registration, exact declared scopes, not_ready and answers (#2146, SP11)."""

from __future__ import annotations

import asyncio
import inspect
import json

import duckdb
import pytest

from src.kb.sports_records import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.sports import harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.sports import SPORTS_SCOPES, SPORTS_TOOLS, SPORTS_WRITES

NAMESPACE_SCOPES = {
    "namespace:global:read",
    "namespace:global:write",
    "namespace:f:read",
    "namespace:f:write",
}
# Arguments that reach each entry point's own logic (unknown keys answer not_found, never a database error).
ARGS = {
    "sports_standings_as_of": {
        "namespace": "global",
        "season_key": h.SEASON,
        "date": "2099-08-25",
    },
    "sports_match_history": {"namespace": "global", "fixture_key": h.fixture_key(103)},
    "sports_fixture_history": {
        "namespace": "global",
        "fixture_key": h.fixture_key(104),
    },
    "sports_team_schedule": {
        "namespace": "global",
        "team_key": h.team_key(9001),
        "date_from": "2099-08-01",
        "date_to": "2099-08-31",
    },
    "sports_player_appearances": {
        "namespace": "global",
        "player_key": "sports:football-data:player:50001",
    },
    "sports_transfers": {
        "namespace": "global",
        "player_key": "sports:football-data:player:50001",
    },
    "sports_tennis_matches": {"namespace": "global"},
    "sports_olympic_event_results": {
        "namespace": "global",
        "fixture_key": h.fixture_key(101),
    },
    "export_sports_records": {
        "namespace": "global",
        "purpose": "non-commercial",
        "refs": [{"record_type": "fixture", "record_key": h.fixture_key(101)}],
    },
    "list_sports_identity_candidates": {"namespace": "global"},
    "sports_venue_place": {"namespace": "global", "venue_key": "sports:x"},
    "sports_news_links": {"namespace": "global", "target_key": h.fixture_key(101)},
    "score_sports_forecasts": {
        "forecast_namespace": "f",
        "forecast_ids": ["forecast:x"],
    },
    "poll_sports_monitor": {"subscription_id": "sub:x"},
    "propose_sports_identity_matches": {"namespace": "global"},
    "review_sports_identity_match": {
        "namespace": "global",
        "candidate_id": "own-idc:x",
        "decision": "accept",
        "reason": "r",
    },
    "revert_sports_identity_match": {
        "namespace": "global",
        "candidate_id": "own-idc:x",
        "reason": "r",
    },
    "record_sports_club_lineage": {
        "namespace": "global",
        "kind": "renamed",
        "subject_key": h.team_key(9001),
        "object_key": h.team_key(9002),
        "valid_on": "2099-09-01",
        "source": {"url": "https://fa.example.org/x", "title": "t"},
        "reason": "r",
    },
    "link_sports_venue_place": {
        "namespace": "global",
        "venue_key": "sports:x",
        "geo_namespace": "global",
    },
    "record_sports_decision": {
        "namespace": "global",
        "fixture_key": h.fixture_key(105),
        "status": "annulled",
        "deciding_body": h.FA,
        "decision": {"url": h.FORFEIT_URL},
        "published_at": "2099-09-12",
    },
    "record_sports_table_rule": {
        "namespace": "global",
        "season_key": h.SEASON,
        "rule_id": "scoring",
        "published_at": "2099-08-01",
        "citation_url": "https://fa.example.org/rules",
        "attribution": h.FA,
        "rule": {
            "kind": "scoring",
            "points": {"win": 3, "draw": 1, "loss": 0},
            "tiebreakers": ["points"],
        },
    },
    "record_sports_transfer": {
        "namespace": "global",
        "player_name": "Jan Example",
        "to_team_key": h.team_key(9002),
        "date": "2099-09-01",
        "source": {"kind": "club", "url": "https://c.example.org/x", "title": "t"},
    },
    "import_sports_olympic_results": {
        "namespace": "global",
        "source_url": "https://o.example.org/r",
        "csv_text": (h.FIXTURES / "olympic_2096_results.csv").read_text(),
    },
    "refresh_sports_news_links": {
        "namespace": "global",
        "target_type": "fixture",
        "target_key": h.fixture_key(103),
    },
    "review_sports_news_link": {
        "namespace": "global",
        "link_id": "x",
        "decision": "accept",
        "reason": "r",
    },
    "revert_sports_news_link": {"namespace": "global", "link_id": "x", "reason": "r"},
    "register_sports_forecast": {
        "namespace": "global",
        "forecast_namespace": "f",
        "request_key": "k",
        "rule": {
            "kind": "match_outcome",
            "fixture_key": h.fixture_key(106),
            "outcome": "draw",
        },
        "probability": 0.3,
        "resolution_at_ms": 2**62,
    },
    "create_sports_monitor": {
        "namespace": "global",
        "request_key": "m",
        "watch": "match",
        "key": h.fixture_key(103),
    },
    "run_sports_monitor": {"subscription_id": "sub:x"},
}


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "sports-mcp.duckdb")
    state = {"principal": "alice", "scopes": {"operator"}}
    monkeypatch.setattr(
        server, "_context", lambda: (state["principal"], state["scopes"])
    )
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    duckdb.connect(path).close()
    return asyncio.run(server.mcp.get_tools()), state, path


def load(path):
    conn = duckdb.connect(path)
    h.load_league(conn)
    h.record_decisions(conn)
    conn.close()


def test_tools_are_registered_with_every_scope_they_always_read_and_write(mcp_env):
    tools, _, _ = mcp_env
    assert (
        SPORTS_TOOLS <= set(tools)
        and set(ARGS) | {"sports_source_contracts", "sports_readiness"} == SPORTS_TOOLS
    )
    catalog = json.loads(
        (h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in SPORTS_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in SPORTS_WRITES else "read"), name
        assert (
            _required_scopes("knowledge_engine_mcp", mutability, name)
            == SPORTS_SCOPES[name]
        )
        assert by_name[name]["required_scopes"] == SPORTS_SCOPES[name]
    for provider in (
        "sports.football",
        "sports.tennis",
        "sports.olympics",
        "sports.forecasts",
    ):
        descriptor = json.loads(
            (h.ROOT / f"packs/sports/providers/{provider}.json").read_text()
        )
        for op in descriptor["operations"]:
            assert op["required_scopes"] == SPORTS_SCOPES[op["tool"].split(".", 1)[1]]


def test_every_entry_point_answers_not_ready_before_any_source_ran(mcp_env):
    tools, state, _ = mcp_env
    assert tools["sports_readiness"].fn()["status"] == "not_ready"
    for name, args in ARGS.items():
        answer = tools[name].fn(**args)
        code = answer.get("error", {}).get("code") if isinstance(answer, dict) else None
        assert code != "knowledge_engine_unavailable", (name, answer)
        if name.startswith("sports_") and name not in {
            "sports_venue_place",
            "sports_news_links",
        }:
            assert code == "not_ready", (name, answer)


def test_each_tool_works_with_exactly_its_declared_scopes_and_refuses_without_them(
    mcp_env,
):
    tools, state, path = mcp_env
    load(path)
    for name in sorted(ARGS):
        declared = set(SPORTS_SCOPES[name])
        state["scopes"] = declared | NAMESPACE_SCOPES
        answer = tools[name].fn(**ARGS[name])
        code = answer.get("error", {}).get("code") if isinstance(answer, dict) else None
        assert code not in {
            "unauthorized",
            "knowledge_engine_unavailable",
            "not_ready",
        }, (name, answer)
        for missing in declared:
            state["scopes"] = (declared - {missing}) | NAMESPACE_SCOPES
            refused = tools[name].fn(**ARGS[name])
            assert (
                refused.get("ok") is False
                and refused["error"]["code"] == "unauthorized"
            ), (name, missing)


def test_answers_carry_citations_and_never_odds_or_predictions(mcp_env):
    tools, state, path = mcp_env
    load(path)
    state["scopes"] = set(h.REVIEW_SCOPES) | NAMESPACE_SCOPES
    table = tools["sports_standings_as_of"].fn(
        **{**ARGS["sports_standings_as_of"], "date": "2099-09-12"}
    )
    assert table["comparison"]["status"] == "disagrees" and table["corrections"]
    assert forbidden_keys(table) == [] and "None" not in json.dumps(table)
    history = tools["sports_match_history"].fn(
        namespace="global", fixture_key=h.fixture_key(105)
    )
    assert history["status"] == "forfeit_awarded"
    refused = tools["register_sports_forecast"].fn(
        **{**ARGS["register_sports_forecast"], "probability": None}
    )
    assert refused["ok"] is False
    parameters = inspect.signature(tools["register_sports_forecast"].fn).parameters
    assert "odds" not in parameters and not any("odds" in name for name in SPORTS_TOOLS)
    assert (
        tools["sports_source_contracts"].fn()["contracts"]["bookmakers"][
            "access_decision"
        ]
        == "not implemented"
    )
