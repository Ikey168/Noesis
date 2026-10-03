"""Elections MCP entry points: catalog registration, declared scopes, conditional scopes and answers (#2000)."""

from __future__ import annotations

import json

import duckdb
import pytest

from src.kb.elections import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from src.mcp_host.introspection import tool_map
from tests.unit import elections_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.elections import (
    ELECTION_SCOPES,
    ELECTION_TOOLS,
    ELECTION_WRITES,
)

POLL = "poll_beispiel_institut_2099-02-21.csv"


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "elections-mcp.duckdb")
    conn = duckdb.connect(path)
    h.apply(conn, "de-btw", h.DE_PRELIMINARY)
    h.apply(conn, "de-btw", h.DE_FINAL)
    contest = h.contest_id(conn, h.DE_ELECTION, "de-bt-wahlkreis", "001", "first-vote")
    conn.close()
    state = {"principal": "alice", "scopes": set(h.REVIEW_SCOPES)}
    monkeypatch.setattr(
        server, "_context", lambda: (state["principal"], state["scopes"])
    )
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    return tool_map(server.mcp), state, contest


def test_tools_are_registered_with_every_scope_they_always_read_and_write(mcp_env):
    tools, _, _ = mcp_env
    assert ELECTION_TOOLS <= set(tools) and set(ELECTION_SCOPES) == ELECTION_TOOLS
    for name in ELECTION_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in ELECTION_WRITES else "read"), name
        assert (
            _required_scopes("knowledge_engine_mcp", mutability, name)
            == ELECTION_SCOPES[name]
        )
    catalog = json.loads(
        (h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    by_name = {t["name"]: t for t in catalog["tools"]}
    assert ELECTION_TOOLS <= set(by_name)
    # Existing tools keep their static scopes; the elections case is conditional and documented.
    assert by_name["propose_forecast_resolution"]["required_scopes"] == [
        "knowledge:forecasts:read"
    ]
    assert (
        "knowledge:political:elections:read"
        in tools["propose_forecast_resolution"].description
    )
    assert by_name["query_geospatial_features_within"]["required_scopes"] == [
        "knowledge:geospatial:calculate"
    ]
    assert "conditional scope" in tools["election_contest_dossier"].description.lower()
    descriptor = json.loads(
        (h.ROOT / "packs/political/providers/political.elections.json").read_text()
    )
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        assert name in ELECTION_TOOLS and name not in ELECTION_WRITES
        assert op["required_scopes"] == ELECTION_SCOPES[name]


def test_results_polls_and_dossier_through_mcp(mcp_env):
    tools, state, contest = mcp_env
    results = tools["election_contest_results"].fn(
        namespace="global", contest_id=contest, as_of="2099-03-10"
    )
    assert (
        results["current"]["kind"] == "preliminary"
        and results["status"] == "preliminary-only"
    )
    imported = tools["import_election_poll_release"].fn(
        namespace="global",
        publisher="Beispiel Institut",
        election_id=h.DE_ELECTION,
        source_url="https://www.beispiel-institut.example/sonntagsfrage.csv",
        csv_text=(h.FIXTURES / POLL).read_text(),
        redistribution="allowed",
    )
    assert imported["readings"] == 6
    dossier = tools["election_contest_dossier"].fn(
        namespace="global", contest_id=contest
    )
    assert (
        dossier["results"]["status"] == "certified" and len(dossier["poll_series"]) == 3
    )
    assert (
        "Beispielpartei: no reviewed identity (shown as the source string)"
        in dossier["unknowns"]
    )
    assert forbidden_keys(dossier) == []
    refused = tools["election_contest_dossier"].fn(
        namespace="global", contest_id=contest, forecast_namespace="f"
    )
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    state["scopes"] = set(h.READ_ONLY)
    denied = tools["import_election_poll_release"].fn(
        namespace="global",
        publisher="Beispiel Institut",
        election_id=h.DE_ELECTION,
        source_url="https://www.beispiel-institut.example/x.csv",
        csv_text="x",
        redistribution="allowed",
    )
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    assert (
        tools["election_source_contracts"].fn()["contracts"]["wahlrecht-de"][
            "access_decision"
        ]
        == "not-implemented"
    )
