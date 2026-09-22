"""Versioned native Ragas metrics over frozen Noesis retrieval records.

The selected metrics use no hosted judge. Text-similarity/ID metrics are not
entailment. Independent human labels remain a separate acceptance requirement.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import math
import os
import time

from src.evaluation.runtime_errors import BackendError

METRICS = frozenset(
    {"id_precision", "id_recall", "context_precision", "context_recall"}
)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def validate_cases(cases):
    if not isinstance(cases, list) or not 1 <= len(cases) <= 500:
        raise ValueError("one to 500 frozen evaluation cases required")
    ids = set()
    for case in cases:
        required = {
            "id",
            "question",
            "answer",
            "contexts",
            "citations",
            "reference_context_ids",
            "reference_contexts",
            "label_origin",
        }
        if (
            not isinstance(case, dict)
            or not required <= case.keys()
            or not case["id"]
            or case["id"] in ids
        ):
            raise ValueError(
                "unique complete question/retrieval/answer records required"
            )
        if case["label_origin"] not in {
            "independent-human",
            "assisted-human",
            "model",
            "fixture",
        }:
            raise ValueError("explicit label provenance required")
        for key in ("question", "answer"):
            if not isinstance(case[key], str) or len(case[key]) > 32000:
                raise ValueError("evaluation text exceeds its bound")
        contexts = case["contexts"]
        if not isinstance(contexts, list) or len(contexts) > 100:
            raise ValueError("retrieval context budget exceeded")
        for row in contexts:
            if (
                not isinstance(row, dict)
                or any(
                    not isinstance(row.get(key), str) or not row[key]
                    for key in ("id", "revision", "text")
                )
                or len(row["text"]) > 32000
            ):
                raise ValueError("each context needs a frozen source revision and text")
        context_ids = [row["id"] for row in contexts]
        if len(set(context_ids)) != len(context_ids):
            raise ValueError("duplicate retrieved contexts")
        for key in ("citations", "reference_context_ids", "reference_contexts"):
            if (
                not isinstance(case[key], list)
                or len(case[key]) > 100
                or any(not isinstance(v, str) or len(v) > 32000 for v in case[key])
            ):
                raise ValueError("invalid bounded reference list")
        if not set(case["citations"]) <= set(context_ids):
            raise ValueError("citation references unavailable captured context")
        ids.add(case["id"])
    if len(json.dumps(cases).encode()) > 16 * 1024**2:
        raise ValueError("evaluation corpus byte budget exceeded")


async def evaluate_ragas(cases, *, metrics=("id_precision", "id_recall"), timeout_s=30):
    """Execute Ragas 0.4 native single-turn metrics without LLM/model defaults."""
    validate_cases(cases)
    if not metrics or not set(metrics) <= METRICS or len(set(metrics)) != len(metrics):
        raise ValueError("explicit supported unique Ragas metrics required")
    if (
        type(timeout_s) not in {int, float}
        or not math.isfinite(timeout_s)
        or not 0.01 <= timeout_s <= 600
    ):
        raise ValueError("invalid evaluation deadline")
    # Ragas can otherwise emit analytics during imports/evaluation. Only this
    # explicitly selected offline execution path sets its documented opt-out.
    os.environ["RAGAS_DO_NOT_TRACK"] = "true"
    try:
        from ragas import SingleTurnSample
        from ragas.metrics import (
            IDBasedContextPrecision,
            IDBasedContextRecall,
            NonLLMContextPrecisionWithReference,
            NonLLMContextRecall,
        )
    except ImportError:
        return {
            "status": "unavailable",
            "failure_code": "ragas_dependency_unavailable",
            "input_sha256": _digest(cases),
        }
    version = importlib.metadata.version("ragas")
    if not version.startswith("0.4."):
        raise BackendError(
            "unsupported_version", "this adapter is verified against Ragas 0.4.x"
        )
    classes = {
        "id_precision": IDBasedContextPrecision,
        "id_recall": IDBasedContextRecall,
        "context_precision": NonLLMContextPrecisionWithReference,
        "context_recall": NonLLMContextRecall,
    }
    scorers = {name: classes[name]() for name in metrics}
    started = time.monotonic()
    output = []
    for case in cases:
        sample = SingleTurnSample(
            user_input=case["question"],
            response=case["answer"],
            retrieved_contexts=[row["text"] for row in case["contexts"]],
            retrieved_context_ids=[row["id"] for row in case["contexts"]],
            reference_context_ids=case["reference_context_ids"],
            reference_contexts=case["reference_contexts"],
        )
        result = {
            "id": case["id"],
            "label_origin": case["label_origin"],
            "source_revisions": {
                row["id"]: row["revision"] for row in case["contexts"]
            },
            "metrics": {},
        }
        for name, scorer in scorers.items():
            remaining = timeout_s - (time.monotonic() - started)
            try:
                if remaining <= 0:
                    raise TimeoutError
                value = float(
                    await asyncio.wait_for(scorer.single_turn_ascore(sample), remaining)
                )
                if not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError("undefined metric")
                result["metrics"][name] = {"status": "measured", "value": value}
            except TimeoutError:
                result["metrics"][name] = {
                    "status": "unavailable",
                    "reason": "deadline_exceeded",
                    "value": None,
                }
            except Exception as exc:  # noqa: BLE001 - retain failed cases, not falsely improved means
                result["metrics"][name] = {
                    "status": "unavailable",
                    "reason": type(exc).__name__,
                    "value": None,
                }
        output.append(result)
    complete = all(
        row["status"] == "measured"
        for case in output
        for row in case["metrics"].values()
    )
    return {
        "contract": "noesis-ragas-result-v1",
        "status": "completed" if complete else "partial",
        "cases": output,
        "configuration": {
            "ragas": version,
            "metrics": list(metrics),
            "llm": None,
            "prompt_version": None,
        },
        "input_sha256": _digest(cases),
        "elapsed_seconds": time.monotonic() - started,
        "hosted_calls": 0,
        "provider_cost_usd_micros": 0,
        "scores_are_independent_human_labels": False,
        "support_verified": False,
        "production_default_changed": False,
        "limitations": [
            "ID/text overlap is not evidence entailment.",
            "Human-origin metadata is not independently certified.",
            "Empty/undefined cases remain unavailable; no mean silently drops failures.",
        ],
    }
