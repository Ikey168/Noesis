from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.hypotheses import HypothesisError, HypothesisStore

READ = {"knowledge:hypothesis:read"}
WRITE = {"knowledge:hypothesis:write"}
EXECUTE = {"knowledge:hypothesis:execute"}
SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def _validate(name, value):
    schema = json.loads((SCHEMAS / name).read_text())
    Draft202012Validator(schema).validate(value)


def _hypotheses():
    return [
        {
            "hypothesis_id": "h-weather",
            "label": "Weather",
            "statement": "Weather caused the outage.",
            "assumptions": ["Sensors are calibrated"],
            "predictions": [
                {
                    "prediction_id": "p-weather",
                    "statement": "Weather alerts overlap the outage.",
                    "discriminates_from": ["h-change"],
                    "test": {"cost": 2},
                }
            ],
            "alternative_to": ["h-change"],
        },
        {
            "hypothesis_id": "h-change",
            "label": "Change",
            "statement": "A deployment caused the outage.",
            "predictions": [
                {
                    "prediction_id": "p-change",
                    "statement": "A deployment precedes the outage.",
                    "discriminates_from": ["h-weather"],
                    "test": {"cost": 1},
                }
            ],
        },
    ]


def _workspace(store, namespace="technical"):
    return store.create(
        namespace,
        "Outage causes",
        _hypotheses(),
        principal_id="analyst",
        scopes=WRITE,
        idempotency_key="outage-1",
        generation=7,
        valid_from_ms=10,
        observed_at_ms=20,
        policy={"method": "ACH"},
    )


def test_draft_revision_retirement_branching_and_stable_identity():
    conn = duckdb.connect(":memory:")
    store = HypothesisStore(conn, now=lambda: 100)
    first = _workspace(store)
    assert first["lifecycle"] == "draft" and first["generation"] == 7
    _validate("noesis-hypothesis-workspace-v1.json", first)
    assert _workspace(store)["idempotent"] is True
    values = _hypotheses()
    values[0]["statement"] = "Severe weather caused the outage."
    revised = store.revise(
        "technical",
        first["workspace_id"],
        principal_id="editor",
        scopes=WRITE,
        expected_revision=1,
        hypotheses=values,
        lifecycle="active",
    )
    assert revised["revision"] == 2
    assert revised["hypotheses"][0]["hypothesis_id"] == "h-weather"
    branch = store.branch(
        "technical",
        first["workspace_id"],
        "Regional outage causes",
        principal_id="editor",
        scopes=WRITE,
    )
    assert branch["parent_workspace_id"] == first["workspace_id"]
    assert branch["hypotheses"][0]["hypothesis_id"] == "h-weather"
    retired = store.revise(
        "technical",
        first["workspace_id"],
        principal_id="editor",
        scopes=WRITE,
        expected_revision=2,
        lifecycle="retired",
    )
    history = store.get(
        "technical", first["workspace_id"], scopes=READ, include_history=True
    )
    assert retired["lifecycle"] == "retired"
    assert [item["revision"] for item in history["revisions"]] == [1, 2, 3]
    with pytest.raises(HypothesisError, match="revision changed"):
        store.revise(
            "technical",
            first["workspace_id"],
            principal_id="stale",
            scopes=WRITE,
            expected_revision=1,
            title="stale",
        )
    conn.close()


def test_evidence_deduplication_mixed_stance_access_and_retraction():
    conn = duckdb.connect(":memory:")
    store = HypothesisStore(conn, now=lambda: 100)
    workspace = _workspace(store)
    first = store.link_evidence(
        "technical",
        workspace["workspace_id"],
        "h-weather",
        "report-a",
        "support",
        principal_id="analyst",
        scopes=WRITE,
        relevance=0.9,
        independence_group="wire-report",
        source_revision_id="derived-revision:1",
        provenance={"url": "https://example.test/a"},
        annotations={"reviewer_note": "timing matches"},
    )
    duplicate = store.link_evidence(
        "technical",
        workspace["workspace_id"],
        "h-weather",
        "report-a",
        "support",
        principal_id="analyst",
        scopes=WRITE,
        relevance=0.9,
        independence_group="wire-report",
        source_revision_id="derived-revision:1",
        provenance={"url": "https://example.test/a"},
        annotations={"reviewer_note": "timing matches"},
    )
    assert duplicate["idempotent"] is True
    store.link_evidence(
        "technical",
        workspace["workspace_id"],
        "h-weather",
        "report-copy",
        "support",
        principal_id="analyst",
        scopes=WRITE,
        relevance=0.8,
        independence_group="wire-report",
    )
    store.link_evidence(
        "technical",
        workspace["workspace_id"],
        "h-weather",
        "private-sensor",
        "contradict",
        principal_id="analyst",
        scopes=WRITE | {"source:private"},
        required_scope="source:private",
    )
    comparison = store.compare(
        "technical", workspace["workspace_id"], scopes=READ, method="weighted"
    )
    weather = next(
        item for item in comparison["results"] if item["hypothesis_id"] == "h-weather"
    )
    assert weather["independent_groups"] == 1
    assert comparison["inaccessible_evidence_count"] == 1
    retracted = store.retract_evidence(
        "technical",
        workspace["workspace_id"],
        first["link_id"],
        "The source withdrew the underlying report.",
        principal_id="reviewer",
        scopes=WRITE,
    )
    assert retracted["lifecycle"] == "retracted" and retracted["revision"] == 2
    exported = store.export("technical", workspace["workspace_id"], scopes=READ)
    assert [
        item["revision"]
        for item in exported["evidence"]
        if item["link_id"] == first["link_id"]
    ] == [1, 2]
    assert exported["omissions"]["inaccessible_evidence_revisions"] == 1
    conn.close()


def test_comparison_sparse_priors_sensitivity_and_ties():
    conn = duckdb.connect(":memory:")
    store = HypothesisStore(conn)
    workspace = _workspace(store)
    sparse = store.compare("technical", workspace["workspace_id"], scopes=READ)
    assert {item["assessment"] for item in sparse["results"]} == {"insufficient"}
    assert sparse["tie"] is True
    with_priors = store.compare(
        "technical",
        workspace["workspace_id"],
        scopes=READ,
        method="weighted",
        priors={"h-weather": 0.8, "h-change": 0.2},
        sensitivity=0.1,
    )
    assert with_priors["ranking"][0] == "h-weather"
    assert "not posterior truth probabilities" in with_priors["limitations"][0]
    assert all(len(item["interval"]) == 2 for item in with_priors["results"])
    _validate("noesis-hypothesis-comparison-v1.json", with_priors)
    conn.close()


def test_bounded_plan_budget_partial_source_cancellation_and_resume():
    conn = duckdb.connect(":memory:")
    store = HypothesisStore(conn, now=lambda: 100)
    workspace = _workspace(store)
    plan = store.create_plan(
        "technical",
        workspace["workspace_id"],
        principal_id="planner",
        scopes=WRITE,
    )
    assert len(plan["steps"]) == 2
    _validate("noesis-hypothesis-research-plan-v1.json", plan)
    first_step, second_step = plan["steps"]
    exhausted = store.execute_plan(
        "technical",
        plan["plan_id"],
        [],
        principal_id="runner",
        scopes=EXECUTE,
        budget=1,
    )
    assert exhausted["status"] == "paused" and exhausted["cursor"] == 0
    partial = store.execute_plan(
        "technical",
        plan["plan_id"],
        [{"step_id": first_step["step_id"], "result": "matched"}],
        principal_id="runner",
        scopes=EXECUTE,
        budget=3,
    )
    assert partial["cursor"] == 1 and partial["status"] == "paused"
    cancelled = store.execute_plan(
        "technical",
        plan["plan_id"],
        [],
        principal_id="runner",
        scopes=EXECUTE,
        budget=3,
        cancel_requested=True,
    )
    assert cancelled["status"] == "cancelled" and cancelled["cursor"] == 1
    complete = store.execute_plan(
        "technical",
        plan["plan_id"],
        [{"step_id": second_step["step_id"], "result": "deployment found"}],
        principal_id="runner",
        scopes=EXECUTE,
        budget=3,
    )
    assert complete["status"] == "complete" and complete["cursor"] == 2
    conn.close()


@pytest.mark.parametrize("namespace", ["osint", "scientific"])
def test_deterministic_export_replay_and_namespace_isolation(namespace):
    conn = duckdb.connect(":memory:")
    store = HypothesisStore(conn, now=lambda: 100)
    workspace = _workspace(store, namespace=namespace)
    first = store.export(namespace, workspace["workspace_id"], scopes=READ)
    second = store.export(namespace, workspace["workspace_id"], scopes=READ)
    assert first == second
    _validate("noesis-hypothesis-export-v1.json", first)
    assert store.replay(namespace, workspace["workspace_id"], scopes=READ)[
        "deterministic"
    ]
    assert store.get("other", workspace["workspace_id"], scopes=READ) is None
    with pytest.raises(HypothesisError, match="required scope"):
        store.compare(namespace, workspace["workspace_id"], scopes=set())
    conn.close()
