import copy

import duckdb
import pytest

from src.kb.decisions import DecisionStore, DecisionError
from src.kb.research_projects import ResearchProjectStore, ResearchProjectError

AUTH = {"principal_id": "alice", "scopes": {"knowledge:decisions:read", "knowledge:decisions:write", "knowledge:projects:read", "knowledge:projects:write", "namespace:r:write"}}


def setup():
    conn = duckdb.connect()
    project = ResearchProjectStore(conn).create("r", "p", questions=["Which option?"], success_criteria=["Review evidence"],
        scope={"namespaces": ["r"], "domains": []}, budget={}, **AUTH)
    content = {"project": {"id": project["project_id"], "namespace": "r", "revision": 1},
        "options": [{"id": "a", "description": "Low cost"}, {"id": "b", "description": "High quality"}],
        "constraints": ["Within budget"], "assumptions": ["Prices stable"], "observations": [],
        "preferences": ["Prefer lower cost"], "selected_action": "a", "rationale": "Fits current preferences",
        "review_conditions": ["Review if prices change"]}
    store = DecisionStore(conn)
    decision = store.create("r", "d", content, **AUTH)
    return store, content, decision


def test_revision_preserves_what_was_known_and_rechecks_project_access():
    store, content, decision = setup()
    revised = copy.deepcopy(content)
    revised.update(selected_action="b", rationale="Quality is now preferred", preferences=["Prefer quality"])
    store.revise("r", decision["decision_id"], 1, revised, **AUTH)
    assert store.inspect("r", decision["decision_id"], revision=1, **AUTH)["content"] == content
    assert store.create("r", "d", content, **AUTH)["idempotent"]
    with pytest.raises(DecisionError, match="changed"):
        store.revise("r", decision["decision_id"], 1, content, **AUTH)
    with pytest.raises(ResearchProjectError, match="required"):
        store.inspect("r", decision["decision_id"], revision=1, **{**AUTH, "scopes": AUTH["scopes"] - {"knowledge:projects:read"}})


def test_sensitivity_changes_order_preserves_ties_and_records_provenance():
    store, content, decision = setup()
    args = dict(weights={"cost": 1, "quality": 1}, inputs={"a": {"cost": 1, "quality": 0}, "b": {"cost": 0, "quality": 1}},
        scenarios=[{"assumption": "Cost matters twice as much", "weights": {"cost": 2}}], provenance="Explicit normalized utilities from the decision author")
    receipt = store.sensitivity("r", decision["decision_id"], 1, **args, **AUTH)
    assert receipt["baseline"]["ordering_with_ties"] == [["a", "b"]]
    assert receipt["scenarios"][0]["ordering_with_ties"] == [["a"], ["b"]]
    assert receipt["scenarios"][0]["ordering_changed"]
    assert receipt["receipt_id"] == store.sensitivity("r", decision["decision_id"], 1, **args, **AUTH)["receipt_id"]
    assert store.conn.execute("SELECT count(*) FROM decision_sensitivity_receipts").fetchone()[0] == 1
    assert store.inspect("r", decision["decision_id"], **AUTH)["content"]["selected_action"] == "a"
    args["inputs"]["a"]["quality"] = None
    missing = store.sensitivity("r", decision["decision_id"], 1, **args, **AUTH)
    assert missing["baseline"]["scores"]["a"] is None
    assert missing["baseline"]["missing_inputs"] == {"a": ["quality"]}


def test_invalid_baseline_and_input_bounds_reject_without_receipts():
    store, content, decision = setup()
    content["project"]["revision"] = 999
    with pytest.raises(ResearchProjectError):
        store.create("r", "bad", content, **AUTH)
    for weights in [{"x": float("nan")}, {"x": -1}, {"x": 0}, {"x": "1e1000"}]:
        with pytest.raises(DecisionError):
            store.sensitivity("r", decision["decision_id"], 1, weights=weights, inputs={"a": {"x": 1}, "b": {"x": 2}}, scenarios=[], provenance="Test inputs", **AUTH)
    assert store.conn.execute("SELECT count(*) FROM decision_sensitivity_receipts").fetchone()[0] == 0


def test_standalone_choice_is_versioned_and_requires_current_decision_access():
    conn = duckdb.connect()
    store = DecisionStore(conn)
    auth = {"principal_id": "alice", "scopes": {
        "knowledge:decisions:read", "knowledge:decisions:write", "namespace:r:write",
    }}
    content = {
        "project": None,
        "decision_context": {
            "question": "Renew the service?", "stakes": "One month of cost",
            "required_confidence": "Moderate", "stop_condition": "Current usage is known",
            "uncertainty": "Next month's usage is unknown", "missing_inputs": ["Future usage"],
            "deadline_at_ms": 1_800_000_000_000,
        },
        "options": [{"id": "yes", "description": "Renew"}, {"id": "no", "description": "Cancel"}],
        "constraints": ["Within budget"], "assumptions": [], "observations": [],
        "preferences": ["Avoid unused subscriptions"], "selected_action": "no",
        "rationale": "There is no current use", "review_conditions": ["Reconsider if use resumes"],
    }
    decision = store.create("r", "renewal", content, **auth)
    assert decision["contract"] == "noesis-decision-v2"
    assert store.create("r", "renewal", content, **auth)["idempotent"]
    revised = copy.deepcopy(content)
    revised["selected_action"] = "yes"
    revised["rationale"] = "Usage resumed"
    assert store.revise("r", decision["decision_id"], 1, revised, **auth)["revision"] == 2
    assert store.inspect("r", decision["decision_id"], revision=1, **auth)["content"]["selected_action"] == "no"
    assert store.inspect("r", decision["decision_id"], **auth)["content"]["selected_action"] == "yes"
    receipt = store.sensitivity("r", decision["decision_id"], 1,
        weights={"cost": 1}, inputs={"yes": {"cost": 0}, "no": {"cost": 1}},
        scenarios=[], provenance="Author-declared preference", **auth)
    assert receipt["decision_revision"] == 1
    assert receipt["baseline"]["ordering_with_ties"] == [["no"], ["yes"]]
    with pytest.raises(DecisionError, match="namespace access"):
        store.inspect("r", decision["decision_id"], revision=1,
                      principal_id="alice", scopes={"knowledge:decisions:read"})
    with pytest.raises(DecisionError, match="decision scope"):
        store.inspect("r", decision["decision_id"], principal_id="bob", scopes=auth["scopes"])


def test_standalone_choice_requires_explicit_bounded_context():
    conn = duckdb.connect()
    store = DecisionStore(conn)
    content = {"project": None, "options": [{"id": "yes", "description": "Yes"},
        {"id": "no", "description": "No"}], "constraints": [], "assumptions": [],
        "observations": [], "preferences": [], "selected_action": "yes",
        "rationale": "Chosen", "review_conditions": []}
    with pytest.raises(DecisionError, match="decision_context"):
        store.create("r", "d", content, **AUTH)
    content["decision_context"] = {"question": "Choose?", "stakes": "Low",
        "required_confidence": "Moderate", "stop_condition": "Enough information",
        "uncertainty": "Some unknowns", "missing_inputs": [], "deadline_at_ms": -1}
    with pytest.raises(DecisionError, match="deadline_at_ms"):
        store.create("r", "d", content, **AUTH)
    content["decision_context"]["deadline_at_ms"] = None
    content["observations"] = [{"kind": "evidence", "id": "other", "namespace": "other", "revision": 1}]
    with pytest.raises(DecisionError, match="namespace scope"):
        store.create("r", "d", content, **AUTH)
