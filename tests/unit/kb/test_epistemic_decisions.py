import duckdb
import pytest

from src.kb.epistemic import EpistemicError, EpistemicStore, classify_statement
from src.kb.epistemic_decisions import assess_with_decision, classify_with_decision

POLICY = {
    "hosted_allowed": True,
    "model": "jev-1.13.0",
    "rubric_id": "epistemic-v1",
    "policy_id": "pilot-v1",
}
SCOPES = {
    "knowledge:epistemic:write",
    "knowledge:epistemic:read",
    "knowledge:epistemic:review",
    "knowledge:decision:execute",
    "namespace:research:read",
    "namespace:research:write",
}
REFS = [{"document_id": "doc-1", "revision_id": "rev-1"}]


class FakeRuntime:
    def __init__(self, status="completed", kind="report"):
        self.status, self.kind, self.calls = status, kind, []
        self.rollout_mode = "suggestion"
        self.texts = {"doc-1": "The filing reportedly confirms a result. A statement"}

    def capture_sources(self, namespace, principal_id, refs, scopes):
        bindings = [
            {"document_id": ref["document_id"], "revision_id": ref["revision_id"]}
            for ref in refs
        ]
        return bindings, [{"content": self.texts[ref["document_id"]]} for ref in refs]

    def run(self, namespace, run_id, task, **kwargs):
        self.calls.append((namespace, run_id, task, kwargs))
        answer = {
            "status": "answered",
            "value": self.kind,
            "probabilities": {
                label: 0.82 if label == self.kind else 0.0225
                for label in (
                    "fact",
                    "report",
                    "allegation",
                    "estimate",
                    "forecast",
                    "opinion",
                    "hypothesis",
                    "normative",
                    "unknown",
                )
            },
            "selected_probability": 0.82,
            "vendor_confidence": 0.63,
        }
        return {
            "status": self.status,
            "rollout_mode": self.rollout_mode,
            "receipt": {
                "status": "answered" if self.status == "completed" else self.status,
                "answers": {"kind": answer} if self.status == "completed" else {},
            },
        }


def test_kind_pilot_preserves_probability_meaning_and_evidence_separation():
    runtime = FakeRuntime()
    model = classify_with_decision(
        runtime,
        "research",
        "claim-1",
        "The filing reportedly confirms a result.",
        "run-1",
        source_refs=REFS,
        principal_id="alice",
        scopes=SCOPES,
        allow_remote=True,
        policy=POLICY,
    )
    classified = classify_statement(
        "The filing reportedly confirms a result.",
        classifier=lambda _: model,
        classifier_pin={
            "name": "typesafe-jev",
            "version": "jev-1.13.0",
            "revision": "epistemic-v1",
        },
    )
    assert classified["status"] == "report"
    assert classified["truth_verified"] is False
    assert classified["selected_probability"] == classified["confidence"] == 0.82
    assert classified["vendor_confidence"] == 0.63
    assert runtime.calls[0][3]["source_refs"] == REFS
    assert runtime.calls[0][3]["source_slices"] == [{"start": 0, "end": 40}]
    assert runtime.calls[0][3]["state"]["statement_locator"] == {"start": 0, "end": 40}
    assert "truth_not_verified" in classified["signals"]

    store = EpistemicStore(duckdb.connect(":memory:"), now=lambda: 100)
    assessed = assess_with_decision(
        store,
        runtime,
        "research",
        "claim-1",
        "The filing reportedly confirms a result. A statement",
        [],
        "run-2",
        source_refs=REFS,
        principal_id="alice",
        scopes=SCOPES,
        allow_remote=True,
        policy=POLICY,
        source_revision_id="rev-1",
    )
    assert assessed["machine_status"] == "report"
    assert assessed["assessment_state"] == "insufficient"
    assert assessed["classifier"]["vendor_confidence"] == 0.63
    assert assessed["classifier"]["classifier"]["revision"] == "epistemic-v1"
    override = store.override(
        "research",
        "claim-1",
        "allegation",
        "The reviewer treats the quoted assertion as an allegation.",
        reviewer_id="reviewer",
        scopes=SCOPES,
    )
    assert override["machine_status"] == "report"
    assert override["effective_status"] == "allegation"
    reassessed = assess_with_decision(
        store,
        runtime,
        "research",
        "claim-1",
        "The filing reportedly confirms a result.",
        [],
        "run-3",
        source_refs=REFS,
        principal_id="alice",
        scopes=SCOPES,
        allow_remote=True,
        policy=POLICY,
        source_revision_id="rev-1",
    )
    history = store.get(
        "research", "claim-1", scopes=SCOPES, include_history=True
    )
    assert reassessed["revision"] == 2
    assert history["revisions"][0]["transitions"][0]["status"] == "allegation"


def test_unavailable_is_unknown_and_invalid_probability_is_rejected():
    unknown = classify_with_decision(
        FakeRuntime(status="unavailable"),
        "research",
        "claim-1",
        "A statement",
        "run-1",
        source_refs=REFS,
        principal_id="alice",
        scopes=SCOPES,
        allow_remote=True,
        policy=POLICY,
    )
    assert unknown["status"] == "unknown" and unknown["confidence"] == 0.0
    assert unknown["availability"] == "unavailable"
    assert unknown["truth_verified"] is False
    with pytest.raises(EpistemicError, match="finite numbers"):
        classify_statement(
            "A statement",
            classifier=lambda _: {"status": "fact", "confidence": float("nan")},
            classifier_pin={"name": "fixture", "version": "1", "revision": "r"},
        )
    with pytest.raises(EpistemicError, match="versioned source"):
        classify_with_decision(
            FakeRuntime(),
            "research",
            "claim-1",
            "A statement",
            "run-2",
            source_refs=[],
            principal_id="alice",
            scopes=SCOPES,
            allow_remote=True,
            policy=POLICY,
        )


def test_caller_text_must_be_present_in_authorized_source_before_disclosure():
    runtime = FakeRuntime()
    with pytest.raises(EpistemicError, match="verbatim") as error:
        classify_with_decision(
            runtime,
            "research",
            "claim-1",
            "An unrelated caller statement",
            "run-3",
            source_refs=REFS,
            principal_id="alice",
            scopes=SCOPES,
            allow_remote=True,
            policy=POLICY,
        )
    assert error.value.code == "unbound_statement"
    assert not runtime.calls


def test_assessment_revision_must_match_the_bound_classification_source():
    runtime = FakeRuntime()
    store = EpistemicStore(duckdb.connect(":memory:"), now=lambda: 100)
    with pytest.raises(EpistemicError, match="revision must match"):
        assess_with_decision(
            store,
            runtime,
            "research",
            "claim-1",
            "The filing reportedly confirms a result.",
            [],
            "run-wrong-revision",
            source_refs=REFS,
            principal_id="alice",
            scopes=SCOPES,
            allow_remote=True,
            policy=POLICY,
            source_revision_id="another-revision",
        )
    assert not runtime.calls


def test_classifier_rejects_selected_probability_that_disagrees_with_distribution():
    class MismatchedRuntime(FakeRuntime):
        def run(self, namespace, run_id, task, **kwargs):
            result = super().run(namespace, run_id, task, **kwargs)
            result["receipt"]["answers"]["kind"]["selected_probability"] = 0.9
            return result

    with pytest.raises(EpistemicError, match="valid selected-label probability"):
        classify_with_decision(
            MismatchedRuntime(),
            "research",
            "claim-1",
            "The filing reportedly confirms a result.",
            "run-mismatched-probability",
            source_refs=REFS,
            principal_id="alice",
            scopes=SCOPES,
            allow_remote=True,
            policy=POLICY,
        )


def test_shadow_rollout_never_returns_hosted_kind_as_a_suggestion():
    runtime = FakeRuntime()
    runtime.rollout_mode = "shadow"
    result = classify_with_decision(
        runtime,
        "research",
        "claim-1",
        "The filing reportedly confirms a result.",
        "shadow-run",
        source_refs=REFS,
        principal_id="alice",
        scopes=SCOPES,
        allow_remote=True,
        policy=POLICY,
    )
    assert result["status"] == "unknown"
    assert result["confidence"] == 0.0
    assert result.get("selected_probability") is None
    assert "shadow_mode" in result["signals"]
