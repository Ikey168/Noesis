import asyncio
import time

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def test_forecast_public_contract(tmp_path, monkeypatch):
    path = str(tmp_path / "forecasts.duckdb")
    monkeypatch.setattr(server, "_context", lambda: ("alice", {"operator"}))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    now = int(time.time() * 1000)
    forecast = tools["create_binary_forecast"].fn(namespace="r", request_key="f", question="Will it occur?",
        outcome_rule="Reviewed official event", resolution_at_ms=now+100000, probability=0.7, evidence=[])
    identity = {"namespace": "r", "forecast_id": forecast["forecast_id"]}
    assert tools["inspect_binary_forecast"].fn(**identity)["probability"] == 0.7
    assert tools["revise_binary_forecast"].fn(**identity, expected_revision=1, probability=0.6, evidence=[], rationale="Updated information")["revision"] == 2
    assert tools["propose_forecast_resolution"].fn(**identity)["requires_review"]
    outcome = tools["resolve_binary_forecast"].fn(**identity, expected_outcome_revision=0, status="cancelled",
        outcome=None, evidence=[], rationale="Event definition cannot be resolved", forecast_revision=2)
    assert outcome["status"] == "cancelled"
    scores = tools["score_binary_forecasts"].fn(namespace="r", forecast_ids=[forecast["forecast_id"]], cutoff_ms=now+10000)
    assert scores["excluded"][0]["reason"] == "cancelled"
    for name in ("create_binary_forecast", "revise_binary_forecast", "resolve_binary_forecast"):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == ["knowledge:forecasts:write"]
