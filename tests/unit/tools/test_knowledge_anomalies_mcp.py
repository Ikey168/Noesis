from __future__ import annotations

import asyncio
import inspect

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def test_anomaly_mcp_flow_auth_and_delivery(tmp_path, monkeypatch):
    db = tmp_path / "anomalies.duckdb"
    scopes = {"knowledge:anomalies:read"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(db))
    )
    tools = asyncio.run(server.mcp.get_tools())
    names = {
        "register_anomaly_watch",
        "preview_anomaly_baseline",
        "simulate_anomaly_detector",
        "run_anomaly_detector",
        "get_knowledge_anomaly",
        "correlate_knowledge_anomaly",
        "deliver_anomaly_alert",
        "transition_anomaly_alert",
        "anomaly_alert_history",
        "inspect_anomaly_health",
    }
    assert names <= tools.keys()
    denied = call(
        tools["register_anomaly_watch"],
        namespace="economic",
        watch_key="cpi",
        version=1,
        signal_type="metric",
        scope={},
        baseline={"window": 3},
        detector={"threshold": 2},
        notification={},
    )
    assert denied["error"]["code"] == "unauthorized"
    scopes.add("knowledge:anomalies:write")
    watch = call(
        tools["register_anomaly_watch"],
        namespace="economic",
        watch_key="cpi",
        version=1,
        signal_type="metric",
        scope={},
        baseline={"window": 3, "minimum_points": 2},
        detector={"threshold": 2},
        notification={},
    )
    values = [
        {"value": 10},
        {"value": 11},
        {"value": 9},
        {"value": 30, "signal_key": "cpi"},
    ]
    assert call(
        tools["simulate_anomaly_detector"],
        namespace="economic",
        watch_id=watch["watch_id"],
        observations=values,
    )["detected"]
    scopes.add("knowledge:anomalies:execute")
    run = call(
        tools["run_anomaly_detector"],
        namespace="economic",
        watch_id=watch["watch_id"],
        observations=values,
        generation=1,
    )
    anomaly_id = run["anomaly_ids"][0]
    call(
        tools["correlate_knowledge_anomaly"],
        namespace="economic",
        anomaly_id=anomaly_id,
        candidates=[{"object_id": "event:1", "relevance": 0.8}],
    )
    scopes.add("knowledge:anomalies:deliver")
    alert = call(
        tools["deliver_anomaly_alert"],
        namespace="economic",
        anomaly_id=anomaly_id,
        subscriber_id="desk",
    )
    assert (
        call(
            tools["transition_anomaly_alert"],
            namespace="economic",
            alert_id=alert["alert_id"],
            action="acknowledged",
            actor_id="analyst",
        )["status"]
        == "acknowledged"
    )
    assert call(tools["anomaly_alert_history"], namespace="economic")["alerts"]


def test_anomaly_catalog():
    assert _mutability("correlate_knowledge_anomaly") == "write"
    assert _required_scopes(
        "knowledge_engine_mcp", "write", "run_anomaly_detector"
    ) == ["knowledge:anomalies:execute"]
    assert _required_scopes(
        "knowledge_engine_mcp", "read", "inspect_anomaly_health"
    ) == ["knowledge:anomalies:read"]
    assert (
        "noesis-knowledge-anomaly-v1"
        in server.knowledge_engine_capabilities.fn()["contracts"]
    )
