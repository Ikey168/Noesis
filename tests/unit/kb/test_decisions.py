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
