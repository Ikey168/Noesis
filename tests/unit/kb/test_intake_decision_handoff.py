"""Deterministic Decision Support handoff fixture, separate from user outcome evidence."""

import duckdb
import pytest

from src.kb.decisions import DecisionStore
from src.kb.intake_modes import IntakeError, IntakeStore

AUTH = {"principal_id": "alice", "scopes": {
    "knowledge:decisions:read", "knowledge:decisions:write",
    "knowledge:intake:read", "knowledge:intake:write", "namespace:r:read", "namespace:r:write",
}}


def test_standalone_choice_completes_linked_mode_once_and_preserves_origin():
    conn = duckdb.connect()
    intake = IntakeStore(conn, now=lambda: 1_800_000_000_000)
    parent = intake.create("r", "Exploration", "explore", intent="Consider renewal", **AUTH)
    content = {"project": None, "decision_context": {
        "question": "Renew?", "stakes": "One month", "required_confidence": "Moderate",
        "stop_condition": "Usage known", "uncertainty": "Next month unknown",
        "missing_inputs": [], "deadline_at_ms": None},
        "options": [{"id": "yes", "description": "Renew"}, {"id": "no", "description": "Cancel"}],
        "constraints": [], "assumptions": [], "observations": [], "preferences": [],
        "selected_action": "no", "rationale": "No current use", "review_conditions": []}
    decision = DecisionStore(conn).create("r", "choice", content, **AUTH)
    reference = {"kind": "decision", "id": decision["decision_id"],
        "namespace": "r", "version": decision["revision"]}
    forged = intake.create("r", "Decision Support", "forged-decision", intent="Renew?", **AUTH)
    intake.command("r", forged["session_id"], "forged-record", expected_revision=1,
        action="record", payload={"data": {"selected_option": "no", "rationale": "No current use"},
                                  "references": [{"kind": "decision", "id": "decision:missing",
                                                  "namespace": "r", "version": 1}]}, **AUTH)
    with pytest.raises(IntakeError, match="current owned Decision Record"):
        intake.command("r", forged["session_id"], "forged-complete", expected_revision=2,
            action="complete", payload=None, **AUTH)
    mismatched = intake.create("r", "Decision Support", "mismatched-decision", intent="Renew?", **AUTH)
    intake.command("r", mismatched["session_id"], "mismatched-record", expected_revision=1,
        action="record", payload={"data": {"selected_option": "yes", "rationale": "No current use"},
                                  "references": [reference]}, **AUTH)
    with pytest.raises(IntakeError, match="current owned Decision Record"):
        intake.command("r", mismatched["session_id"], "mismatched-complete", expected_revision=2,
            action="complete", payload=None, **AUTH)
    link = {"system": "modulo", "workspace_id": "personal", "kind": "artifact",
        "id": "decision.abc", "version": 1}
    args = {"intent": "Renew?", "origin": {"session_id": parent["session_id"],
        "reason": "Decision recorded from the linked research context"},
        "references": [reference], "workspace_links": [link], **AUTH}
    session = intake.create("r", "Decision Support", "decision-session", **args)
    assert session["origin"]["session_id"] == parent["session_id"]
    assert session["references"] == [reference]
    assert session["workspace_links"] == [link]
    recorded = intake.command("r", session["session_id"], "decision-record",
        expected_revision=1, action="record", payload={"data": {
            "selected_option": "no", "rationale": "No current use"}}, **AUTH)
    completed = intake.command("r", session["session_id"], "decision-complete",
        expected_revision=2, action="complete", payload=None, **AUTH)
    assert recorded["revision"] == 2
    assert completed["revision"] == 3 and completed["status"] == "completed"
    assert intake.create("r", "Decision Support", "decision-session", **args)["idempotent"]
    assert intake.command("r", session["session_id"], "decision-record",
        expected_revision=1, action="record", payload={"data": {
            "selected_option": "no", "rationale": "No current use"}}, **AUTH)["idempotent"]
    assert intake.command("r", session["session_id"], "decision-complete",
        expected_revision=2, action="complete", payload=None, **AUTH)["idempotent"]
    handoff = intake.modulo_handoff("r", session["session_id"], **AUTH)
    assert handoff["noesis_references"] == [reference]
    assert handoff["modulo_links"] == [link]
    with pytest.raises(IntakeError, match="current owner"):
        intake.modulo_handoff("r", session["session_id"], principal_id="bob", scopes=AUTH["scopes"])
    DecisionStore(conn).revise("r", decision["decision_id"], 1, content, **AUTH)
    stale = intake.create("r", "Decision Support", "stale-decision", intent="Renew?", **AUTH)
    intake.command("r", stale["session_id"], "stale-record", expected_revision=1,
        action="record", payload={"data": {"selected_option": "no", "rationale": "No current use"},
                                  "references": [reference]}, **AUTH)
    with pytest.raises(IntakeError, match="current owned Decision Record"):
        intake.command("r", stale["session_id"], "stale-complete", expected_revision=2,
            action="complete", payload=None, **AUTH)
