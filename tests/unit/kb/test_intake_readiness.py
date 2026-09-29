"""Readiness is caller-scoped and never treats a listed tool as live acceptance."""

import duckdb
import pytest
import json
from pathlib import Path
from jsonschema import Draft202012Validator

from src.kb.intake_modes import IntakeError, IntakeStore
from src.kb.intake_readiness import discover_workflows, preflight


def test_readiness_distinguishes_scopes_subscriptions_and_unknown_live_state():
    conn = duckdb.connect(":memory:")
    scopes = {"knowledge:intake:read", "namespace:research:read"}
    result = preflight(conn, "research", principal_id="alice", scopes=scopes)
    assert len(result["modes"]) == 10
    assert result["transport_accessible"] is True
    assert result["source_mode"] == "unknown_live_or_fixture"
    assert result["ledger_initialized"] is False
    awareness = next(item for item in result["modes"] if item["mode"] == "Awareness")
    assert awareness["native_start_possible"] is False
    assert awareness["complete_journey_ready"] is False
    assert "knowledge:intake:write" in awareness["missing_scopes"]
    assert any("subscription" in blocker for blocker in awareness["blockers"])
    exploration = preflight(conn, "research", mode="Exploration",
                            principal_id="alice", scopes=scopes)["modes"][0]
    assert exploration["fetch_scope_available"] is False
    assert any("knowledge:intake:fetch" in blocker for blocker in exploration["blockers"])
    research = preflight(conn, "research", mode="Deep Research",
                         principal_id="alice", scopes=scopes)["modes"][0]
    assert "start_intake_research_topic" in research["native_tools"]
    assert "knowledge:projects:write" in research["missing_scopes"]
    assert research["native_start_possible"] is False
    with pytest.raises(IntakeError, match="namespace access"):
        preflight(conn, "archive", principal_id="alice", scopes=scopes)
    with pytest.raises(IntakeError, match="ten modes"):
        preflight(conn, "research", mode="Unknown", principal_id="alice", scopes=scopes)


def test_readiness_counts_only_enabled_owner_subscriptions():
    conn = duckdb.connect(":memory:")
    conn.execute("CREATE TABLE intake_inbox_subscriptions (subscription_id TEXT, namespace TEXT, "
                 "owner TEXT, enabled BOOLEAN)")
    conn.execute("INSERT INTO intake_inbox_subscriptions VALUES "
                 "('one','research','alice',true),('two','research','bob',true),"
                 "('three','research','alice',false)")
    scopes = {"knowledge:intake:read", "knowledge:intake:write",
              "namespace:research:read", "namespace:research:write"}
    result = preflight(conn, "research", mode="Awareness",
                       principal_id="alice", scopes=scopes)
    assert result["enabled_feed_subscription_count"] == 1
    assert result["modes"][0]["native_start_possible"] is True
    assert result["modes"][0]["live_source_verified"] is False


def test_discovery_lists_authorized_steps_for_durable_sessions():
    conn = duckdb.connect(":memory:")
    full = {"knowledge:intake:read", "knowledge:intake:write",
            "namespace:research:read", "namespace:research:write"}
    store = IntakeStore(conn)
    created = store.create("research", "Exploration", "discovery",
                           intent="Browse briefly", principal_id="alice", scopes=full)
    value = discover_workflows(conn, "research", principal_id="alice", scopes=full)
    schema = json.loads((
        Path(__file__).resolve().parents[3]
        / "contracts/schemas/jsonschema/noesis-intake-workflow-discovery-v1.json"
    ).read_text())
    Draft202012Validator(schema).validate(value)
    assert len(value["modes"]) == 10
    assert "record" in value["sessions"][0]["allowed_next_actions"]
    paused = store.command("research", created["session_id"], "pause", expected_revision=1,
                           action="pause", payload=None, principal_id="alice", scopes=full)
    assert paused["status"] == "paused"
    readonly = discover_workflows(conn, "research", principal_id="alice",
                                  scopes=full - {"knowledge:intake:write"})
    assert readonly["sessions"][0]["allowed_next_actions"] == ["inspect", "export"]
    assert readonly["modes"][1]["allowed_mutations"] == []
    assert readonly["live_source_verified"] is False
