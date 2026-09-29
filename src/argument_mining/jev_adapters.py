"""Optional Jev suggestions for stance and multilabel editorial frames.

These adapters read the exact authorized version through DecisionRuntime and
leave the existing local classifiers and stored enrichments untouched.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from src.evaluation.mining_runtime import apply_policy, validate_policy
from src.kb.jev_tasks import suggest_task

STANCE_LABELS = ("supportive", "critical", "neutral", "ambiguous")
FRAME_DESCRIPTIONS = {
    "economic": "costs, jobs, markets, or resources",
    "security": "safety, threats, defense, or conflict",
    "humanitarian": "human welfare, suffering, or aid",
    "legal": "laws, rights, courts, or regulation",
    "political": "political actors, elections, or governance",
    "scientific": "research, evidence, or scientific methods",
}
FRAME_WINDOW_CHARS = 4000
FRAME_WINDOW_OVERLAP = 200
MAX_FRAME_WINDOWS = 16


def _captured_text(
    runtime: Any, namespace: str, principal_id: str, source_ref: dict, scopes: set[str]
) -> tuple[str, list[dict]]:
    bindings, captures = runtime.capture_sources(
        namespace, principal_id, [source_ref], scopes
    )
    if len(bindings) != 1 or len(captures) != 1:
        raise ValueError("one exact captured source is required")
    capture = captures[0]
    content = capture.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("exact captured source text is unavailable")
    # All spans use offsets into the retained content, including inbox items
    # whose title is stored separately.
    return content, bindings


def _sentence_spans(text: str) -> list[tuple[str, int, int]]:
    # Match the existing sentence splitter while retaining original offsets.
    from services.ingest.common.document_model import Document
    from src.argument_mining.dataset import sentences_from_document

    document = Document(
        document_id="__captured__",
        source_type="news",
        language="en",
        ingested_at=0,
        content=text,
    )
    cursor = 0
    spans = []
    for sentence in sentences_from_document(document):
        start = text.find(sentence, cursor)
        if start < 0:
            raise ValueError("sentence split cannot be aligned to exact source")
        end = start + len(sentence)
        spans.append((sentence, start, end))
        cursor = end
    return spans


def _receipt_answer(
    run: Mapping[str, Any], key: str, kind: str
) -> Mapping[str, Any] | None:
    if run.get("status") != "suggested":
        return None
    answer = run.get("answers", {}).get(key)
    if (
        not isinstance(answer, Mapping)
        or answer.get("kind") != kind
        or answer.get("status") != "answered"
    ):
        return None
    return answer


def _receipt_versions(run: Mapping[str, Any]) -> tuple[str, str]:
    decision_run = run.get("decision_run")
    receipt = decision_run.get("receipt") if isinstance(decision_run, Mapping) else None
    if not isinstance(receipt, Mapping):
        raise ValueError("durable decision receipt required for calibrated suggestion")
    model, rubric = receipt.get("model_requested"), receipt.get("rubric_id")
    if not isinstance(model, str) or not model or not isinstance(rubric, str) or not rubric:
        raise ValueError("pinned model and rubric receipt required for calibrated suggestion")
    return model, rubric


def _probability(value: Any) -> bool:
    return (
        type(value) in {int, float}
        and math.isfinite(value)
        and 0 <= value <= 1
    )


def suggest_stance(
    runtime: Any,
    namespace: str,
    run_id: str,
    source_ref: dict,
    *,
    topic: str,
    sentence_index: int,
    principal_id: str,
    scopes: set[str],
    allow_remote: bool,
    policy: Mapping[str, Any],
    calibration_policy: Mapping[str, Any] | None = None,
    max_cost_usd_micros: int = 1000,
    deadline_s: float = 30,
) -> dict[str, Any]:
    """Suggest one contextual stance with the original sentence and span."""
    text, bindings = _captured_text(
        runtime, namespace, principal_id, source_ref, scopes
    )
    sentences = _sentence_spans(text)
    if type(sentence_index) is not int or not 0 <= sentence_index < len(sentences):
        raise ValueError("sentence index is outside captured source")
    sentence, start, end = sentences[sentence_index]
    context_start = max(0, start - 1000)
    context_end = min(len(text), end + 1000)
    context = text[context_start:context_end]
    parameters = {
        "topic": topic,
        "sentence_index": sentence_index,
        "sentence_text": sentence,
        "sentence_span": [start, end],
        "bounded_context": context,
    }
    run = suggest_task(
        runtime,
        namespace,
        run_id,
        "stance",
        parameters,
        [source_ref],
        principal_id=principal_id,
        scopes=scopes,
        allow_remote=allow_remote,
        policy=policy,
        max_cost_usd_micros=max_cost_usd_micros,
        deadline_s=deadline_s,
        source_slices=[{"start": context_start, "end": context_end}],
    )
    answer = _receipt_answer(run, "stance", "choice")
    distribution = answer.get("probabilities") if answer else None
    candidate = answer.get("value") if answer else None
    selected_probability = answer.get("selected_probability") if answer else None
    if (
        not isinstance(candidate, str)
        or candidate not in STANCE_LABELS
        or not isinstance(distribution, Mapping)
        or set(distribution) != set(STANCE_LABELS)
        or any(not _probability(value) for value in distribution.values())
        or not math.isclose(sum(distribution.values()), 1.0, abs_tol=1e-6)
        or not _probability(selected_probability)
        or not math.isclose(
            distribution[candidate], selected_probability, abs_tol=1e-9
        )
    ):
        distribution = None
        candidate = None
    model, rubric = (
        _receipt_versions(run) if run.get("status") == "suggested" else (None, None)
    )
    selected = None
    if distribution is not None and calibration_policy is not None:
        if model is None or rubric is None:
            raise ValueError("pinned model and rubric receipt required for calibration")
        validate_policy(
            calibration_policy,
            task="stance",
            model_version=model,
            template_version=rubric,
        )
        if calibration_policy.get("labels") != list(STANCE_LABELS):
            raise ValueError("stance calibration label order changed")
        selected = apply_policy(
            [distribution[label] for label in STANCE_LABELS], calibration_policy
        )
    return {
        "contract": "noesis-jev-stance-suggestion-v1",
        "status": run["status"],
        "accepted": False,
        "topic": topic,
        "sentence_index": sentence_index,
        "text": sentence,
        "span": {"start": start, "end": end},
        "context_span": {"start": context_start, "end": context_end},
        "source_binding": run.get("source_binding") or bindings,
        "raw_probabilities": dict(distribution) if distribution else None,
        "vendor_confidence": answer.get("vendor_confidence")
        if answer and _probability(answer.get("vendor_confidence"))
        else None,
        "candidate": candidate,
        "suggested_stance": selected["labels"][0]
        if selected and selected["labels"]
        else None,
        "prediction_status": "suggested"
        if selected and selected["labels"]
        else "abstained",
        "calibration_policy_sha256": calibration_policy.get("policy_sha256")
        if calibration_policy
        else None,
        "decision_run": run["decision_run"],
    }


def _windows(text: str) -> list[tuple[int, int, str]]:
    windows = []
    start = 0
    while start < len(text):
        end = min(start + FRAME_WINDOW_CHARS, len(text))
        windows.append((start, end, text[start:end]))
        if end == len(text):
            break
        start = end - FRAME_WINDOW_OVERLAP
    return windows


def suggest_frames(
    runtime: Any,
    namespace: str,
    run_id: str,
    source_ref: dict,
    *,
    document_kind: str,
    principal_id: str,
    scopes: set[str],
    allow_remote: bool,
    policy: Mapping[str, Any],
    calibration_policy: Mapping[str, Any] | None = None,
    max_cost_usd_micros: int = 1000,
    deadline_s: float = 30,
) -> dict[str, Any]:
    """Score independent frame questions across all bounded source windows."""
    text, bindings = _captured_text(
        runtime, namespace, principal_id, source_ref, scopes
    )
    windows = _windows(text)
    if len(windows) > MAX_FRAME_WINDOWS:
        return {
            "contract": "noesis-jev-frame-suggestion-v1",
            "status": "abstained",
            "reason": "source_exceeds_window_limit",
            "accepted": False,
            "coverage": {
                "covered_chars": 0,
                "total_chars": len(text),
                "complete": False,
                "windows": [],
            },
            "source_binding": bindings,
            "raw_scores": None,
            "suggested_frames": [],
            "dominant": None,
            "prediction_status": "abstained",
            "calibration_policy_sha256": calibration_policy.get("policy_sha256")
            if calibration_policy
            else None,
            "decision_runs": [],
        }
    labels = list(FRAME_DESCRIPTIONS)
    raw_scores = {label: 0.0 for label in labels}
    completed = []
    runs = []
    model, rubric = None, None
    for index, (start, end, window) in enumerate(windows):
        parameters = {
            "frames": FRAME_DESCRIPTIONS,
            "document_kind": document_kind,
            "window_text": window,
            "window_span": [start, end],
        }
        result = suggest_task(
            runtime,
            namespace,
            f"{run_id}:window:{index}",
            "frames",
            parameters,
            [source_ref],
            principal_id=principal_id,
            scopes=scopes,
            allow_remote=allow_remote,
            policy=policy,
            max_cost_usd_micros=max_cost_usd_micros,
            deadline_s=deadline_s,
            source_slices=[{"start": start, "end": end}],
        )
        runs.append(result["decision_run"])
        if result.get("status") != "suggested":
            break
        candidate_model, candidate_rubric = _receipt_versions(result)
        if model is None:
            model, rubric = candidate_model, candidate_rubric
        elif (candidate_model, candidate_rubric) != (model, rubric):
            break
        answers = [
            _receipt_answer(result, f"frame_{i}", "noul") for i in range(len(labels))
        ]
        if any(
            answer is None
            or type(answer.get("value")) not in {int, float}
            or not math.isfinite(answer["value"])
            or not 0 <= answer["value"] <= 1
            for answer in answers
        ):
            break
        for label, answer in zip(labels, answers, strict=True):
            raw_scores[label] = max(raw_scores[label], float(answer["value"]))
        completed.append(
            {"start": start, "end": end, "run_id": f"{run_id}:window:{index}"}
        )
    complete = len(completed) == len(windows)
    covered = completed[-1]["end"] if completed else 0
    selected = None
    if complete and calibration_policy is not None:
        validate_policy(
            calibration_policy,
            task="frames",
            model_version=model,
            template_version=rubric,
        )
        if calibration_policy.get("labels") != labels:
            raise ValueError("frame calibration label order changed")
        selected = apply_policy(
            [raw_scores[label] for label in labels], calibration_policy
        )
    frames = selected["labels"] if selected else []
    dominant = (
        max(frames, key=lambda label: raw_scores[label])
        if frames
        else "other"
        if selected
        else None
    )
    return {
        "contract": "noesis-jev-frame-suggestion-v1",
        "status": "suggested" if complete else "abstained",
        "accepted": False,
        "reason": None if complete else "incomplete_coverage_or_unavailable_answer",
        "coverage": {
            "covered_chars": covered,
            "total_chars": len(text),
            "complete": complete,
            "windows": completed,
        },
        "source_binding": bindings,
        "raw_scores": raw_scores if complete else None,
        "suggested_frames": frames,
        "dominant": dominant if complete else None,
        "prediction_status": "suggested" if selected and frames else "abstained",
        "calibration_policy_sha256": calibration_policy.get("policy_sha256")
        if calibration_policy
        else None,
        "decision_runs": runs,
    }
