"""Typed Iteration tools are discoverable and enforce intake/namespace scopes."""

import asyncio

import duckdb

from src.mcp_host.catalog import _mutability, _required_scopes
from src.kb.decisions import DecisionStore
from src.kb.authored_reports import AuthoredReportStore
from tools.knowledge_engine_mcp import server


def test_iteration_mcp_discovery_and_scope(tmp_path, monkeypatch):
    path = str(tmp_path / "iteration-mcp.duckdb")
    scopes = {"knowledge:intake:read", "knowledge:intake:write",
              "namespace:research:read", "namespace:research:write"}
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(server, "_connection",
                        lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    for name in ("start_intake_iteration", "record_intake_iteration_outcome",
                 "propose_intake_playbook_revision", "accept_intake_playbook_revision",
                 "review_intake_iteration_stability", "start_intake_report_iteration",
                 "propose_intake_report_revision", "accept_intake_report_revision",
                 "start_intake_concept_iteration", "propose_intake_concept_revision",
                 "accept_intake_concept_revision", "start_intake_modulo_note_iteration",
                 "propose_intake_modulo_note_revision", "accept_intake_modulo_note_revision"):
        assert name in tools
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:intake:write",
        ]
    scopes.remove("knowledge:intake:write")
    denied = tools["start_intake_iteration"].fn(
        namespace="research", request_key="cycle", playbook_id="playbook:missing",
        expected_revision=1, expected="Faster search",
        stability_criteria="Three clean runs", intent="Check the repair",
    )
    assert denied["error"]["code"] == "unauthorized"


def test_decision_iteration_mcp_discovery_and_authoritative_revision(tmp_path, monkeypatch):
    path = str(tmp_path / "decision-iteration-mcp.duckdb")
    scopes = {
        "knowledge:intake:read", "knowledge:intake:write",
        "knowledge:decisions:read", "knowledge:decisions:write",
        "namespace:research:read", "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(server, "_connection",
                        lambda *, read_only: duckdb.connect(path, read_only=read_only))
    content = {
        "project": None,
        "decision_context": {
            "question": "Move the job schedule?", "stakes": "Stale index results",
            "required_confidence": "Moderate", "stop_condition": "Latency measured",
            "uncertainty": "One run", "missing_inputs": [], "deadline_at_ms": None,
        },
        "options": [{"id": "daily", "description": "Refresh daily"},
                    {"id": "threshold", "description": "Refresh by threshold"}],
        "constraints": [], "assumptions": [], "observations": [], "preferences": [],
        "selected_action": "daily", "rationale": "Current schedule is predictable",
        "review_conditions": [],
    }
    conn = duckdb.connect(path)
    decision = DecisionStore(conn).create("research", "schedule", content,
                                          principal_id="alice", scopes=scopes)
    conn.close()
    tools = asyncio.run(server.mcp.get_tools())
    names = ("start_intake_decision_iteration", "record_intake_iteration_outcome",
             "propose_intake_decision_revision", "accept_intake_decision_revision")
    assert set(names) <= set(tools)
    for name in ("start_intake_decision_iteration", "propose_intake_decision_revision",
                 "accept_intake_decision_revision"):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:intake:write",
        ]
    cycle = tools["start_intake_decision_iteration"].fn(
        namespace="research", request_key="schedule-cycle",
        decision_id=decision["decision_id"], expected_revision=1,
        expected="Latency stays under 5 seconds", stability_criteria="Three checks",
        intent="Review the schedule",
    )
    assert cycle["inputs"]["iteration_contract"] == "noesis-intake-iteration-decision-v1"
    assert "noesis-intake-iteration-decision-v1" in server.knowledge_engine_capabilities.fn()["contracts"]
    measured = tools["record_intake_iteration_outcome"].fn(
        namespace="research", session_id=cycle["session_id"], command_key="measure",
        expected_revision=1, observed="Latency reached 7 seconds",
        learning="The daily refresh is inefficient",
        measurements=[{"metric": "latency", "expected": "5", "observed": "7", "unit": "seconds"}],
        uncertainty="Only one day measured", external_causes="Index growth",
        evidence=[],
    )
    changed = {**content, "selected_action": "threshold",
               "rationale": "Measured latency exceeded the limit"}
    proposed = tools["propose_intake_decision_revision"].fn(
        namespace="research", session_id=cycle["session_id"], command_key="propose",
        expected_revision=measured["revision"], content=changed,
        before_after_rationale="Replace fixed daily schedule with a threshold trigger",
    )
    accepted = tools["accept_intake_decision_revision"].fn(
        namespace="research", session_id=cycle["session_id"], command_key="accept",
        expected_revision=proposed["revision"],
    )
    assert accepted["data"]["accepted_revision"]["revision"] == 2
    conn = duckdb.connect(path)
    current = DecisionStore(conn).inspect("research", decision["decision_id"],
                                          principal_id="alice", scopes=scopes)
    assert current["content"]["selected_action"] == "threshold"
    assert current["iteration_history"][0]["outcome"]["measurements"][0]["observed"] == "7"
    conn.close()


def test_report_iteration_mcp_discovery_and_authoritative_revision(tmp_path, monkeypatch):
    path = str(tmp_path / "report-iteration-mcp.duckdb")
    scopes = {
        "knowledge:intake:read", "knowledge:intake:write",
        "knowledge:reports:read", "knowledge:reports:write",
        "namespace:research:read", "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(server, "_connection",
                        lambda *, read_only: duckdb.connect(path, read_only=read_only))
    content = {
        "title": "Index repair",
        "snapshot": {"id": "snapshot:repair", "generations": {"research": 1}},
        "sections": [{"id": "section:repair", "title": "Repair", "assertions": [{
            "id": "assertion:repair", "text": "Restart the worker", "kind": "commentary",
            "dependencies": [], "citations": [],
        }]}],
        "bibliography": [], "limitations": ["Caller-authored"],
    }
    conn = duckdb.connect(path)
    report = AuthoredReportStore(conn).create(
        "research", "report", content, principal_id="alice", scopes=scopes,
    )
    conn.close()
    tools = asyncio.run(server.mcp.get_tools())
    names = ("start_intake_report_iteration", "record_intake_iteration_outcome",
             "propose_intake_report_revision", "accept_intake_report_revision")
    assert set(names) <= set(tools)
    for name in ("start_intake_report_iteration", "propose_intake_report_revision",
                 "accept_intake_report_revision"):
        assert _mutability(name) == "write"
        assert _required_scopes("knowledge_engine_mcp", "write", name) == [
            "knowledge:intake:write",
        ]
    cycle = tools["start_intake_report_iteration"].fn(
        namespace="research", request_key="field-cycle", report_id=report["report_id"],
        expected_revision=1, expected="Records appear within 5 seconds",
        stability_criteria="Three checks pass", intent="Improve the repair guide",
    )
    measured = tools["record_intake_iteration_outcome"].fn(
        namespace="research", session_id=cycle["session_id"], command_key="measure",
        expected_revision=1, observed="Records appeared after 8 seconds",
        learning="A sync wait is missing",
        measurements=[{"metric": "search latency", "expected": "5", "observed": "8", "unit": "seconds"}],
        uncertainty="One field check", external_causes="Large index", evidence=[],
    )
    changed = {**content, "sections": [{**content["sections"][0], "assertions": [{
        **content["sections"][0]["assertions"][0],
        "text": "Restart, wait for sync, then search again",
    }]}]}
    proposed = tools["propose_intake_report_revision"].fn(
        namespace="research", session_id=cycle["session_id"], command_key="propose",
        expected_revision=measured["revision"], content=changed,
        before_after_rationale="Add the observed wait before checking search",
    )
    accepted = tools["accept_intake_report_revision"].fn(
        namespace="research", session_id=cycle["session_id"], command_key="accept",
        expected_revision=proposed["revision"],
    )
    assert accepted["data"]["accepted_revision"]["revision"] == 2
    assert "noesis-intake-iteration-report-v1" in server.knowledge_engine_capabilities.fn()["contracts"]
    current = AuthoredReportStore(duckdb.connect(path), initialize=False).inspect(
        "research", report["report_id"], principal_id="alice", scopes=scopes,
    )
    assert current["revision"] == 2
    assert current["iteration_history"][0]["outcome"]["measurements"][0]["observed"] == "8"
