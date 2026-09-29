import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.kb.intake_decisions import accept_awareness_suggestion, suggest_awareness_item
from src.kb.intake_inbox import IntakeInboxStore
from src.kb.intake_modes import IntakeError
from src.kb.decision_runtime import DecisionRuntime
from src.evaluation.jev_awareness import evaluate_awareness_triage

ROOT = Path(__file__).resolve().parents[3]


def _validate_awareness_contract(value):
    schema = json.loads(
        (
            ROOT / "contracts/schemas/jsonschema/"
            "noesis-awareness-decision-suggestion-v1.json"
        ).read_text()
    )
    Draft7Validator(schema).validate(value)


SCOPES = {
    "knowledge:intake:read",
    "knowledge:intake:write",
    "namespace:research:read",
    "namespace:research:write",
}
POLICY = {
    "hosted_allowed": True,
    "model": "jev-1.13.0",
    "rubric_id": "awareness-v1",
    "policy_id": "pilot-v1",
}


class FakeRuntime:
    def __init__(self, *, available=True):
        self.calls = []
        self.available = available

    def run(self, namespace, run_id, task, **kwargs):
        self.calls.append((namespace, run_id, task, kwargs))
        if not self.available:
            return {
                "status": "unavailable",
                "receipt": {"status": "unavailable", "answers": {}},
            }
        answers = {
            "relevance": {
                "status": "answered",
                "value": True,
                "selected_probability": 0.93,
            },
            "urgency": {
                "status": "answered",
                "value": 2.5,
                "selected_probability": 0.8,
            },
            "advice": {
                "status": "answered",
                "value": "escalate",
                "selected_probability": 0.86,
            },
        }
        if "novelty" in kwargs["questions"]:
            answers["novelty"] = {
                "status": "answered",
                "value": True,
                "selected_probability": 0.7,
            }
        return {
            "status": "completed",
            "receipt": {"status": "answered", "answers": answers},
        }


def _inbox():
    inbox = IntakeInboxStore(duckdb.connect(":memory:"), now=lambda: 1000)
    subscription = inbox.subscribe(
        "research",
        "https://example.org/feed",
        "Test",
        "rss_atom",
        principal_id="alice",
        scopes=SCOPES,
    )
    inbox.ingest(
        "research",
        subscription["subscription_id"],
        [
            {
                "url": "https://example.org/one",
                "title": "Trial replicated",
                "content": "A second lab reproduced the measured result.",
            },
            {
                "url": "https://example.org/two",
                "title": "Earlier trial",
                "content": "The first lab reported the result.",
            },
        ],
        principal_id="alice",
        scopes=SCOPES,
    )
    session = inbox.start_awareness(
        "research",
        "session",
        intent="Track independent replication of the trial",
        principal_id="alice",
        scopes=SCOPES,
    )
    items = inbox.list("research", principal_id="alice", scopes=SCOPES)["items"]
    return inbox, session, items


def test_semantic_preview_is_source_bound_and_accepts_through_existing_command():
    inbox, session, items = _inbox()
    runtime = FakeRuntime()
    first, prior = items
    assert (
        inbox.signal_preview(
            "research",
            ["independent replication"],
            principal_id="alice",
            scopes=SCOPES,
        )["matches"]
        == []
    )
    suggestion = suggest_awareness_item(
        runtime,
        inbox,
        "research",
        session["session_id"],
        first["item_id"],
        "run-one",
        principal_id="alice",
        scopes=SCOPES,
        allow_remote=True,
        policy=POLICY,
        comparison_items=[
            {"item_id": prior["item_id"], "source_version": prior["source_version"]}
        ],
    )
    _validate_awareness_contract(suggestion)
    assert suggestion["suggested_decision"] == "escalate"
    assert suggestion["novelty"]["value"] is True
    assert suggestion["source_excerpt"]["text"] == first["content"]
    call = runtime.calls[0][3]
    assert call["source_refs"] == [
        {"item_id": first["item_id"], "source_version": 1},
        {"item_id": prior["item_id"], "source_version": 1},
    ]
    assert "novelty" in call["questions"]
    result = accept_awareness_suggestion(
        inbox,
        "research",
        suggestion,
        "accept-one",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert result["revision"] == session["revision"] + 1
    assert (
        inbox.inspect(
            "research", first["item_id"], principal_id="alice", scopes=SCOPES
        )["decision"]
        == "escalate"
    )


def test_unavailable_cannot_become_discard_and_changed_item_invalidates_advice():
    inbox, session, items = _inbox()
    item = items[0]
    unavailable = suggest_awareness_item(
        FakeRuntime(available=False),
        inbox,
        "research",
        session["session_id"],
        item["item_id"],
        "run-unavailable",
        principal_id="alice",
        scopes=SCOPES,
        allow_remote=True,
        policy=POLICY,
    )
    _validate_awareness_contract(unavailable)
    assert unavailable["status"] == "unavailable"
    assert unavailable["suggested_decision"] is None
    assert unavailable["novelty"]["status"] == "unknown"
    with pytest.raises(IntakeError, match="completed suggestion"):
        accept_awareness_suggestion(
            inbox,
            "research",
            unavailable,
            "bad",
            principal_id="alice",
            scopes=SCOPES,
        )
    suggestion = suggest_awareness_item(
        FakeRuntime(),
        inbox,
        "research",
        session["session_id"],
        item["item_id"],
        "run-current",
        principal_id="alice",
        scopes=SCOPES,
        allow_remote=True,
        policy=POLICY,
    )
    source_id = inbox.subscriptions("research", principal_id="alice", scopes=SCOPES)[
        "subscriptions"
    ][0]["subscription_id"]
    inbox.ingest(
        "research",
        source_id,
        [
            {
                "url": item["original_url"],
                "title": item["title"],
                "content": "Corrected content",
            }
        ],
        principal_id="alice",
        scopes=SCOPES,
    )
    with pytest.raises(IntakeError) as stale:
        accept_awareness_suggestion(
            inbox,
            "research",
            suggestion,
            "stale",
            principal_id="alice",
            scopes=SCOPES,
        )
    assert stale.value.code == "source_changed"


def test_deleted_item_invalidates_an_existing_awareness_suggestion():
    inbox, session, items = _inbox()
    item = items[0]
    suggestion = suggest_awareness_item(
        FakeRuntime(),
        inbox,
        "research",
        session["session_id"],
        item["item_id"],
        "run-before-delete",
        principal_id="alice",
        scopes=SCOPES,
        allow_remote=True,
        policy=POLICY,
    )
    inbox.conn.execute(
        "DELETE FROM intake_inbox_items WHERE namespace=? AND owner=? AND item_id=?",
        ["research", "alice", item["item_id"]],
    )
    with pytest.raises(IntakeError, match="unavailable"):
        accept_awareness_suggestion(
            inbox,
            "research",
            suggestion,
            "accept-deleted",
            principal_id="alice",
            scopes=SCOPES,
        )


def test_shadow_mode_does_not_offer_an_action():
    inbox, session, items = _inbox()

    class ShadowRuntime(FakeRuntime):
        def run(self, namespace, run_id, task, **kwargs):
            return {
                **super().run(namespace, run_id, task, **kwargs),
                "rollout_mode": "shadow",
            }

    suggestion = suggest_awareness_item(
        ShadowRuntime(),
        inbox,
        "research",
        session["session_id"],
        items[0]["item_id"],
        "shadow-run",
        principal_id="alice",
        scopes=SCOPES,
        allow_remote=True,
        policy=POLICY,
    )
    assert suggestion["status"] == "unavailable"
    assert suggestion["suggested_decision"] is None
    assert "shadow_mode" in suggestion["reason_codes"]


def test_discard_requires_a_retained_calibration_gate():
    inbox, session, items = _inbox()

    class DiscardRuntime(FakeRuntime):
        def run(self, namespace, run_id, task, **kwargs):
            result = super().run(namespace, run_id, task, **kwargs)
            result["receipt"]["answers"]["advice"]["value"] = "discard"
            result["receipt"]["answers"]["relevance"]["value"] = 0.02
            result["decision_policy"] = {
                "calibration_id": "human-validation-v1",
                "discard_max_relevance_p": 0.05,
            }
            return result

    suggestion = suggest_awareness_item(
        DiscardRuntime(),
        inbox,
        "research",
        session["session_id"],
        items[0]["item_id"],
        "discard-run",
        principal_id="alice",
        scopes=SCOPES,
        allow_remote=True,
        policy=POLICY,
    )
    assert suggestion["suggested_decision"] == "discard"
    forged = {
        **suggestion,
        "decision_run": {
            **suggestion["decision_run"],
            "decision_policy": {
                "calibration_id": None,
                "discard_max_relevance_p": 0.05,
            },
        },
    }
    with pytest.raises(IntakeError, match="calibrated relevance"):
        accept_awareness_suggestion(
            inbox,
            "research",
            forged,
            "forged",
            principal_id="alice",
            scopes=SCOPES,
        )
    accepted = accept_awareness_suggestion(
        inbox,
        "research",
        suggestion,
        "accepted",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert accepted["revision"] == session["revision"] + 1


def test_awareness_preview_runs_through_authorized_durable_runtime():
    inbox, session, items = _inbox()

    class Client:
        def decide(self, request, **kwargs):
            kwargs["reserve_attempt"](1, kwargs["max_cost_micros_per_attempt"])
            assert (
                request.state["sources"][0]["binding"]["item_id"] == items[0]["item_id"]
            )
            return {
                "status": "answered",
                "model_requested": "jev-1.13.0",
                "model_returned": "jev-1.13.0",
                "answers": {
                    "relevance": {"status": "answered", "value": True},
                    "urgency": {"status": "answered", "value": 2.0},
                    "advice": {"status": "answered", "value": "flag"},
                },
            }

    runtime = DecisionRuntime(
        inbox.conn, client=Client(), credential_resolver=lambda _: "fixture-secret"
    )
    full_policy = {
        **POLICY,
        "credential_ref": "fixture",
        "budget_id": "pilot",
        "max_total_cost_usd_micros": 10_000,
    }
    suggestion = suggest_awareness_item(
        runtime,
        inbox,
        "research",
        session["session_id"],
        items[0]["item_id"],
        "real-runtime",
        principal_id="alice",
        scopes=SCOPES | {"knowledge:decision:execute"},
        allow_remote=True,
        policy=full_policy,
    )
    assert suggestion["status"] == "suggested"
    assert suggestion["suggested_decision"] == "flag"
    assert suggestion["decision_run"]["source_binding"][0]["source_version"] == 1


def test_awareness_evaluator_compares_keyword_recall_and_false_discard():
    from jsonschema import Draft202012Validator

    rows = [
        {
            "item_id": "i1",
            "group_id": "feed-a",
            "split": "test",
            "label_origin": "fixture",
            "truth": "escalate",
            "suggestion": "discard",
            "keyword_preview_match": False,
        },
        {
            "item_id": "i2",
            "group_id": "feed-b",
            "split": "test",
            "label_origin": "fixture",
            "truth": "watch",
            "suggestion": "watch",
            "keyword_preview_match": True,
        },
    ]
    report = evaluate_awareness_triage(rows)
    assert report["keyword_preview_important_recall"] == 0
    assert report["semantic_important_recall"] == 0
    assert report["false_discard_count"] == 1
    assert report["task_ready"] is False
    schema = json.loads(
        (
            ROOT / "contracts/schemas/jsonschema/"
            "noesis-jev-awareness-evaluation-v1.json"
        ).read_text()
    )
    Draft202012Validator(schema).validate(report)
