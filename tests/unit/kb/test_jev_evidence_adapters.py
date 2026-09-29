from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from src.evaluation.jev_evidence import (
    evaluate_jev_answer_support,
    evaluate_jev_claim_relations,
    evaluate_jev_reranking,
)
from src.kb.jev_evidence_adapters import (
    suggest_answer_support,
    suggest_claim_relation,
    suggest_shortlist_rerank,
)
from services.rag.rerank import CrossEncoderReranker


class FakeRuntime:
    def __init__(self, texts, *, labels=None, unavailable=False):
        self.texts = texts
        self.labels = labels or {}
        self.unavailable = unavailable
        self.calls = []

    def capture_sources(self, namespace, principal_id, refs, scopes):
        return (
            [
                {"document_id": ref["document_id"], "revision_id": ref["revision_id"]}
                for ref in refs
            ],
            [{"content": self.texts[ref["document_id"]]} for ref in refs],
        )

    def run(
        self,
        namespace,
        run_id,
        task,
        *,
        source_refs,
        source_slices,
        state,
        questions,
        **kwargs,
    ):
        self.calls.append(
            {"task": task, "refs": source_refs, "slices": source_slices, "state": state}
        )
        if self.unavailable:
            return {
                "status": "unavailable",
                "rollout_mode": "manual",
                "receipt": None,
                "source_binding": source_refs,
            }
        answers = {}
        for key, question in questions.items():
            if question["type"] == "noul":
                answers[key] = {
                    "kind": "noul",
                    "status": "answered",
                    "value": self.labels.get(key, 0.2),
                }
            else:
                value = self.labels.get(key, "neutral")
                answers[key] = {
                    "kind": "choice",
                    "status": "answered",
                    "value": value,
                    "probabilities": {value: 1.0},
                    "vendor_confidence": 0.8,
                }
        return {
            "status": "completed",
            "rollout_mode": "manual",
            "receipt": {"answers": answers},
            "source_binding": source_refs,
        }


def ref(name):
    return {"document_id": name, "revision_id": "rev"}


def candidate(name, text, score, source):
    return {
        "source_ref": ref(name),
        "locator": {"start": 0, "end": len(text)},
        "content": text,
        "original_score": score,
        "source": source,
        "citation": {"document_id": name},
    }


def test_shortlist_rerank_is_bounded_and_preserves_original_metadata():
    runtime = FakeRuntime(
        {"a": "Some unrelated text.", "b": "The requested answer is here."},
        labels={
            "relevant_0": 0.1,
            "answer_bearing_0": 0.1,
            "relevant_1": 0.9,
            "answer_bearing_1": 0.9,
        },
    )
    rows = [
        candidate("a", runtime.texts["a"], 0.8, "one"),
        candidate("b", runtime.texts["b"], 0.2, "two"),
    ]
    result = suggest_shortlist_rerank(
        runtime,
        "ns",
        "rerank-1",
        "requested answer",
        rows,
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
    )
    assert [row["original_index"] for row in result["ranking"]] == [1, 0]
    assert [row["original_score"] for row in result["ranking"]] == [0.2, 0.8]
    assert {row["source"] for row in result["ranking"]} == {"one", "two"}
    assert runtime.calls[0]["slices"] == [
        {"start": 0, "end": len(row["content"])} for row in rows
    ]
    assert runtime.calls[0]["state"]["candidate_trace"] == [
        {"original_index": 0, "original_score": 0.8},
        {"original_index": 1, "original_score": 0.2},
    ]
    assert result["accepted"] is False and result["selection_policy_applied"] is False
    with pytest.raises(ValueError, match="differs"):
        suggest_shortlist_rerank(
            runtime,
            "ns",
            "rerank-2",
            "query",
            [{**rows[0], "content": "wrong"}],
            principal_id="alice",
            scopes=set(),
            allow_remote=True,
            policy={},
        )


def test_unavailable_reranking_keeps_whole_shortlist_in_original_order():
    runtime = FakeRuntime(
        {"a": "First source passage.", "b": "Second source passage."}, unavailable=True
    )
    rows = [
        candidate(name, runtime.texts[name], score, name)
        for name, score in (("a", 0.8), ("b", 0.2))
    ]
    result = suggest_shortlist_rerank(
        runtime,
        "ns",
        "rerank-fallback",
        "query",
        rows,
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
    )
    assert result["status"] == "fallback_original_order"
    assert [row["original_index"] for row in result["ranking"]] == [0, 1]
    assert len(result["ranking"]) == 2


def test_existing_rerank_interface_has_explicit_jev_path_and_fallback():
    runtime = FakeRuntime(
        {"a": "First source passage.", "b": "Second source passage."},
        labels={
            "relevant_0": 0.1,
            "answer_bearing_0": 0.1,
            "relevant_1": 0.9,
            "answer_bearing_1": 0.9,
        },
    )
    candidates = [
        {
            **candidate(name, runtime.texts[name], score, name),
            "score": score,
            "title": name,
            "url": f"https://example.org/{name}",
        }
        for name, score in (("a", 0.8), ("b", 0.2))
    ]
    reranker = CrossEncoderReranker(scorer=object())
    results, suggestion = reranker.suggest_with_jev(
        "query",
        candidates,
        runtime=runtime,
        namespace="ns",
        run_id="interface-1",
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
    )
    assert [result.original_index for result in results] == [1, 0]
    assert results[0].title == "b" and results[0].url.endswith("/b")
    assert suggestion["accepted"] is False
    runtime.unavailable = True
    fallback, receipt = reranker.suggest_with_jev(
        "query",
        candidates,
        runtime=runtime,
        namespace="ns",
        run_id="interface-2",
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
    )
    assert [result.original_index for result in fallback] == [0, 1]
    assert receipt["status"] == "fallback_original_order"


def test_answer_support_uses_exact_locator_and_never_adds_source():
    text = "The report says emissions fell by five percent."
    runtime = FakeRuntime({"a": text}, labels={"relation": "entailment"})
    citation = {
        "source_ref": ref("a"),
        "locator": {"start": 0, "end": len(text), "quote": text},
    }
    result = suggest_answer_support(
        runtime,
        "ns",
        "answer-1",
        "Emissions fell five percent.",
        citation,
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
    )
    assert result["relation"] == "entailment"
    assert (
        result["counts_as_independent_source"] is False and result["accepted"] is False
    )
    assert runtime.calls[0]["state"]["passage_text"] == text
    assert runtime.calls[0]["slices"] == [{"start": 0, "end": len(text)}]
    with pytest.raises(ValueError, match="differs"):
        suggest_answer_support(
            runtime,
            "ns",
            "answer-2",
            "claim",
            {**citation, "locator": {**citation["locator"], "quote": "old text"}},
            principal_id="alice",
            scopes=set(),
            allow_remote=True,
            policy={},
        )
    assert len(runtime.calls) == 1


def test_claim_directions_duplicate_gate_and_conflict_abstention():
    text_a, text_b = "The levy was withdrawn.", "The levy was withdrawn."
    claims = [
        {"source_ref": ref(name), "locator": {"start": 0, "end": len(text)}}
        for name, text in (("a", text_a), ("b", text_b))
    ]
    runtime = FakeRuntime(
        {"a": text_a, "b": text_b},
        labels={"a_to_b": "entailment", "b_to_a": "entailment"},
    )
    duplicate = suggest_claim_relation(
        runtime,
        "ns",
        "pair-1",
        *claims,
        similarity=0.95,
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
    )
    assert duplicate["relation"] == "duplicate"
    assert (
        duplicate["graph_write"] is False and duplicate["temporal_transition"] is None
    )
    weak = suggest_claim_relation(
        runtime,
        "ns",
        "pair-2",
        *claims,
        similarity=0.4,
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
    )
    assert weak["relation"] != "duplicate"
    runtime.labels["b_to_a"] = "contradiction"
    conflict = suggest_claim_relation(
        runtime,
        "ns",
        "pair-3",
        *claims,
        similarity=0.95,
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
    )
    assert conflict["status"] == "abstained" and conflict["relation"] is None
    runtime.labels["a_to_b"] = "entailment"
    runtime.labels["b_to_a"] = "neutral"
    forward = suggest_claim_relation(
        runtime,
        "ns",
        "pair-forward",
        *claims,
        similarity=0.6,
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
    )
    runtime.labels["a_to_b"] = "neutral"
    runtime.labels["b_to_a"] = "entailment"
    reverse = suggest_claim_relation(
        runtime,
        "ns",
        "pair-reverse",
        *claims,
        similarity=0.6,
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
    )
    assert forward["relation"] == "a_supports_b"
    assert reverse["relation"] == "b_supports_a"
    before = len(runtime.calls)
    windows = suggest_claim_relation(
        runtime,
        "ns",
        "pair-windows",
        *claims,
        similarity=0.95,
        window_relations=["entailment", "contradiction"],
        principal_id="alice",
        scopes=set(),
        allow_remote=True,
        policy={},
    )
    assert windows["reason"] == "conflicting_windows"
    assert len(runtime.calls) == before


def test_long_claims_need_complete_window_coverage_and_self_pairs_abstain():
    long_text = "claim " * 700
    short_text = "A distinct claim."
    runtime = FakeRuntime({"a": long_text, "b": short_text})
    claim_a = {"source_ref": ref("a"), "locator": {"start": 0, "end": len(long_text)}}
    claim_b = {"source_ref": ref("b"), "locator": {"start": 0, "end": len(short_text)}}
    common = {
        "principal_id": "alice",
        "scopes": set(),
        "allow_remote": True,
        "policy": {},
    }
    unverified = suggest_claim_relation(
        runtime, "ns", "long-unverified", claim_a, claim_b, similarity=0.2,
        window_relations=["neutral"], **common,
    )
    assert unverified["reason"] == "window_coverage_unverified"
    assert not runtime.calls
    verified = suggest_claim_relation(
        runtime, "ns", "long-verified", claim_a, claim_b, similarity=0.2,
        window_relations=["neutral"], window_coverage_complete=True, **common,
    )
    assert verified["status"] == "suggested" and len(runtime.calls) == 1
    same = {"source_ref": ref("a"), "locator": {"start": 0, "end": len(long_text)}}
    self_pair = suggest_claim_relation(
        runtime, "ns", "self-pair", same, same, similarity=1.0, **common,
    )
    assert self_pair["reason"] == "same_claim_span"
    assert len(runtime.calls) == 1


def test_evaluation_hooks_compare_same_candidates_and_false_edge_rates():
    report = evaluate_jev_reranking(
        [
            {
                "split": "test",
                "query_id": "query-1",
                "label_origin": "fixture",
                "relevance": {"a": 0, "b": 3},
                "answer_bearing": ["b"],
                "rankings": {
                    "fusion": ["a", "b"],
                    "minilm": ["b", "a"],
                    "qwen": ["b", "a"],
                    "jev": ["b", "a"],
                },
                "jev_status": "suggested",
                "latency_ms": 12,
                "cost_usd_micros": 7,
            }
        ],
        k=1,
        allow_fixture=True,
    )
    schema = json.loads(
        (
            Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema/"
            "noesis-jev-rerank-evaluation-v1.json"
        ).read_text()
    )
    Draft202012Validator(schema).validate(report)
    assert report["methods"]["jev"]["answer_bearing_recall"] == 1
    assert report["methods"]["fusion"]["answer_bearing_recall"] == 0
    assert report["p95_latency_ms"] == 12 and report["task_ready"] is False
    relations = evaluate_jev_claim_relations(
        [
            {
                "split": "test",
                "pair_id": "pair-1",
                "label_origin": "fixture",
                "truth": "neutral",
                "jev": "a_supports_b",
                "local_nli": "neutral",
            },
            {
                "split": "test",
                "pair_id": "pair-2",
                "label_origin": "fixture",
                "truth": "duplicate",
                "jev": "duplicate",
                "local_nli": "a_supports_b",
            },
            {
                "split": "test",
                "pair_id": "pair-3",
                "label_origin": "fixture",
                "truth": "a_supports_b",
                "jev": "b_supports_a",
                "local_nli": "a_supports_b",
            },
        ],
        allow_fixture=True,
    )
    schema = json.loads(
        (
            Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema/"
            "noesis-jev-claim-relation-evaluation-v1.json"
        ).read_text()
    )
    Draft202012Validator(schema).validate(relations)
    assert relations["methods"]["jev"]["supports"]["false_rate"] == 1
    assert relations["methods"]["jev"]["duplicate"]["false_rate"] == 0
    assert relations["task_ready"] is False


def test_answer_support_evaluator_counts_abstention_and_false_support():
    base = {
        "split": "test",
        "label_origin": "fixture",
        "citation_state": "current",
        "source_revision": "rev1",
        "locator": {"start": 0, "end": 10},
        "coverage_complete": True,
        "relevant": True,
        "should_refuse": False,
        "jev_relevant": True,
        "local_nli_relevant": True,
        "jev_refused": False,
        "local_nli_refused": False,
    }
    cases = [
        {**base, "case_id": "unsupported", "case_kind": "unsupported_citation",
         "truth": "neutral", "jev": "entailment", "local_nli": "abstained"},
        {**base, "case_id": "contradiction", "case_kind": "contradiction",
         "truth": "contradiction", "jev": "contradiction", "local_nli": "contradiction",
         "should_refuse": True, "jev_refused": True, "local_nli_refused": True},
        {**base, "case_id": "correction", "case_kind": "correction",
         "citation_state": "stale", "source_revision": "old-rev", "truth": "unavailable",
         "jev": "entailment", "local_nli": "unavailable", "should_refuse": True,
         "jev_refused": False, "local_nli_refused": True},
        {**base, "case_id": "incomplete", "case_kind": "incomplete_coverage",
         "coverage_complete": False, "truth": "unavailable", "jev": "unavailable",
         "local_nli": "neutral", "relevant": False, "should_refuse": True,
         "jev_relevant": False, "local_nli_relevant": False, "jev_refused": True},
        {**base, "case_id": "refusal", "case_kind": "appropriate_refusal",
         "citation_state": "missing", "source_revision": None, "locator": None,
         "coverage_complete": False, "truth": "unavailable", "jev": "unavailable",
         "local_nli": "abstained", "relevant": False, "should_refuse": True,
         "jev_relevant": False, "local_nli_relevant": False, "jev_refused": True,
         "local_nli_refused": True},
    ]
    with pytest.raises(ValueError, match="allow_fixture"):
        evaluate_jev_answer_support(cases)
    report = evaluate_jev_answer_support(cases, allow_fixture=True)
    assert report["methods"]["jev"]["per_relation"]["entailment"]["false_rate"] == 1
    assert report["methods"]["local_nli"]["coverage"] == 0.6
    assert report["coverage_incomplete_cases"] == 2
    assert report["missing_citations"] == 1 and report["stale_citations"] == 1
    assert report["methods"]["jev"]["missed_refusals"] == 1
    assert not report["task_ready"]
    schema = json.loads(
        (
            Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema/"
            "noesis-jev-answer-support-evaluation-v1.json"
        ).read_text()
    )
    Draft202012Validator(schema).validate(report)
