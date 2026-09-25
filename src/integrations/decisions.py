"""Provider-neutral typed judgments and source-bound decision receipts.

Vendor confidence is distribution concentration. It is deliberately separate
from the probability assigned to a selected label and from evidence strength.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping


CONTRACT = "noesis-typed-decision-v1"
KINDS = frozenset({"choice", "score", "noul"})
STATUSES = frozenset({"answered", "abstained", "unavailable"})


class DecisionError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _required_text(value: Any, name: str, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise DecisionError(
            "invalid_input", f"{name} must be nonempty text up to {limit} characters"
        )
    return value


def _json_value(value: Any, name: str) -> Any:
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False)
        decoded = json.loads(encoded)
    except (TypeError, ValueError, OverflowError) as exc:
        raise DecisionError("invalid_input", f"{name} must be finite JSON") from exc
    if len(encoded.encode("utf-8")) > 512_000:
        raise DecisionError("invalid_input", f"{name} exceeds the size limit")
    return decoded


def _probability(value: Any, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise DecisionError("schema_drift", f"{name} must be a finite probability")
    return float(value)


def _distribution(value: Any, expected: set[str]) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise DecisionError(
            "schema_drift", "answer distribution does not match question labels"
        )
    result = {
        key: _probability(prob, f"probability for {key}") for key, prob in value.items()
    }
    if abs(sum(result.values()) - 1.0) > 0.01:
        raise DecisionError("schema_drift", "answer probabilities do not sum to one")
    return result


@dataclass(frozen=True)
class DecisionQuestion:
    kind: str
    instructions: Any
    criteria: Any = None

    @classmethod
    def from_value(
        cls, value: DecisionQuestion | Mapping[str, Any]
    ) -> DecisionQuestion:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise DecisionError("invalid_input", "question must be an object")
        extra = set(value) - {"type", "kind", "instructions", "criteria"}
        if extra or (
            "type" in value and "kind" in value and value["type"] != value["kind"]
        ):
            raise DecisionError(
                "invalid_input", "question has unexpected or conflicting fields"
            )
        return cls(
            value.get("kind", value.get("type")),
            value.get("instructions"),
            value.get("criteria"),
        )

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise DecisionError(
                "invalid_input", "question kind must be choice, score or noul"
            )
        if not isinstance(self.instructions, (str, Mapping, list, tuple)):
            raise DecisionError(
                "invalid_input", "instructions must be text or structured text"
            )
        _json_value(self.instructions, "instructions")
        if self.instructions in (None, "", {}, []):
            raise DecisionError("invalid_input", "question instructions are required")
        if self.kind == "choice":
            if (
                not isinstance(self.criteria, Mapping)
                or not 2 <= len(self.criteria) <= 255
            ):
                raise DecisionError(
                    "invalid_input", "choice needs 2 to 255 labeled options"
                )
            for label, description in self.criteria.items():
                _required_text(label, "choice label", 200)
                if description is not None:
                    if not isinstance(description, (str, Mapping, list, tuple)):
                        raise DecisionError(
                            "invalid_input",
                            "choice description must be text or structured text",
                        )
                    _json_value(description, "choice description")
        elif self.kind == "score":
            if (
                not isinstance(self.criteria, (list, tuple))
                or not 2 <= len(self.criteria) <= 10
            ):
                raise DecisionError(
                    "invalid_input", "score needs 2 to 10 ordered levels"
                )
            for level in self.criteria:
                if level in (None, "", {}, []):
                    raise DecisionError(
                        "invalid_input", "score levels need descriptions"
                    )
                if not isinstance(level, (str, Mapping, list, tuple)):
                    raise DecisionError(
                        "invalid_input", "score levels must be text or structured text"
                    )
                _json_value(level, "score level")
        elif self.criteria is not None:
            if not isinstance(self.criteria, Mapping) or set(self.criteria) != {
                "true",
                "false",
            }:
                raise DecisionError(
                    "invalid_input", "noul criteria require true and false descriptions"
                )
            _json_value(self.criteria, "noul criteria")

    def as_wire(self) -> dict[str, Any]:
        result = {
            "type": self.kind,
            "instructions": _json_value(self.instructions, "instructions"),
        }
        if self.criteria is not None:
            result["criteria"] = _json_value(self.criteria, "criteria")
        return result


def _binding(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        if not value or len(value) > 100:
            raise DecisionError(
                "invalid_input", "source binding list must contain 1 to 100 entries"
            )
        return [_binding(entry) for entry in value]
    if not isinstance(value, Mapping):
        raise DecisionError("invalid_input", "source binding must be an object or list")
    data = _json_value(dict(value), "source binding")
    kind = data.get("kind")
    required = {
        "document_revision": ("document_id", "revision_id"),
        "inbox_item_version": ("item_id", "source_version"),
        "user_input_version": ("input_id", "version"),
    }.get(kind)
    if required is None or any(data.get(key) in (None, "") for key in required):
        raise DecisionError(
            "invalid_input", "source binding needs a recognized kind and exact version"
        )
    return data


@dataclass(frozen=True)
class DecisionRequest:
    task_id: str
    rubric_id: str
    state: Any
    questions: Mapping[str, DecisionQuestion | Mapping[str, Any]]
    source_binding: Any
    model: str = "jev-1.13.0"
    policy_id: str | None = None
    calibration_id: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.task_id, "task_id")
        _required_text(self.rubric_id, "rubric_id")
        _required_text(self.model, "model")
        for name in ("policy_id", "calibration_id"):
            if getattr(self, name) is not None:
                _required_text(getattr(self, name), name)
        if not isinstance(self.state, (str, Mapping, list, tuple)) or self.state in (
            "",
            {},
            [],
        ):
            raise DecisionError(
                "invalid_input", "state must be nonempty text or structured JSON"
            )
        _json_value(self.state, "state")
        if (
            not isinstance(self.questions, Mapping)
            or not 1 <= len(self.questions) <= 64
        ):
            raise DecisionError("invalid_input", "request needs 1 to 64 questions")
        for question_id, question in self.questions.items():
            _required_text(question_id, "question id", 200)
            DecisionQuestion.from_value(question)
        _binding(self.source_binding)

    def as_wire(self) -> dict[str, Any]:
        return {
            "state": _json_value(self.state, "state"),
            "model": self.model,
            "questions": {
                key: DecisionQuestion.from_value(value).as_wire()
                for key, value in self.questions.items()
            },
        }


@dataclass(frozen=True)
class DecisionAnswer:
    kind: str
    status: str
    value: str | float | None = None
    probabilities: Mapping[str, float] | None = None
    selected_probability: float | None = None
    vendor_confidence: float | None = None
    legend: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS or self.status not in STATUSES:
            raise DecisionError("invalid_input", "invalid answer kind or status")
        if self.status == "answered" and self.value is None:
            raise DecisionError("invalid_input", "answered result needs a value")
        if self.status != "answered" and any(
            x is not None
            for x in (
                self.value,
                self.probabilities,
                self.selected_probability,
                self.vendor_confidence,
                self.legend,
            )
        ):
            raise DecisionError(
                "invalid_input", "non-answer status cannot carry a model value"
            )
        for name in ("selected_probability", "vendor_confidence"):
            if getattr(self, name) is not None:
                _probability(getattr(self, name), name)
        if self.status != "answered":
            return
        if self.kind == "noul":
            _probability(self.value, "noul")
            if any(
                x is not None
                for x in (
                    self.probabilities,
                    self.selected_probability,
                    self.vendor_confidence,
                    self.legend,
                )
            ):
                raise DecisionError(
                    "invalid_input", "noul has no distribution or vendor confidence"
                )
        elif self.kind == "choice":
            if (
                not isinstance(self.value, str)
                or not isinstance(self.probabilities, Mapping)
                or not self.probabilities
            ):
                raise DecisionError(
                    "invalid_input", "choice needs a selected label and distribution"
                )
            distribution = _distribution(self.probabilities, set(self.probabilities))
            if (
                self.value not in distribution
                or self.selected_probability is None
                or abs(distribution[self.value] - self.selected_probability) > 1e-9
                or self.vendor_confidence is None
                or self.legend is not None
            ):
                raise DecisionError(
                    "invalid_input", "choice probability or confidence is inconsistent"
                )
        else:
            if (
                isinstance(self.value, bool)
                or not isinstance(self.value, (int, float))
                or not math.isfinite(self.value)
            ):
                raise DecisionError("invalid_input", "score needs a finite value")
            if (
                not isinstance(self.probabilities, Mapping)
                or not isinstance(self.legend, Mapping)
                or not self.legend
            ):
                raise DecisionError(
                    "invalid_input", "score needs a distribution and legend"
                )
            distribution = _distribution(self.probabilities, set(self.legend))
            if (
                set(self.legend) != {str(i) for i in range(len(self.legend))}
                or self.vendor_confidence is None
                or self.selected_probability is not None
            ):
                raise DecisionError(
                    "invalid_input", "score legend or confidence is invalid"
                )
            if (
                abs(self.value - sum(int(k) * v for k, v in distribution.items()))
                > 0.02
            ):
                raise DecisionError(
                    "invalid_input", "score is inconsistent with distribution"
                )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "status": self.status,
            "value": self.value,
            "probabilities": dict(self.probabilities)
            if self.probabilities is not None
            else None,
            "selected_probability": self.selected_probability,
            "vendor_confidence": self.vendor_confidence,
            "legend": dict(self.legend) if self.legend is not None else None,
        }


@dataclass(frozen=True)
class DecisionReceipt:
    status: str
    task_id: str
    rubric_id: str
    source_binding: Any
    model_requested: str
    model_returned: str | None
    answers: Mapping[str, DecisionAnswer]
    usage: Mapping[str, int | None]
    attempts: int
    execution: Mapping[str, Any]
    policy_id: str | None = None
    calibration_id: str | None = None
    contract: str = field(default=CONTRACT, init=False)

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise DecisionError("invalid_input", "invalid receipt status")
        if type(self.attempts) is not int or self.attempts < 0:
            raise DecisionError("invalid_input", "attempts must be nonnegative")
        if self.status == "answered" and (
            not self.answers
            or any(a.status != "answered" for a in self.answers.values())
            or not self.model_returned
        ):
            raise DecisionError(
                "invalid_input",
                "answered receipt needs complete answers and returned model",
            )
        if not self.answers or any(
            not isinstance(a, DecisionAnswer) for a in self.answers.values()
        ):
            raise DecisionError("invalid_input", "receipt needs typed answers")
        if self.status != "answered" and any(
            a.status != self.status for a in self.answers.values()
        ):
            raise DecisionError("invalid_input", "receipt and answer statuses differ")
        if not isinstance(self.usage, Mapping) or set(self.usage) != {
            "input_tokens",
            "output_tokens",
        }:
            raise DecisionError("invalid_input", "receipt needs token usage fields")
        for value in self.usage.values():
            if value is not None and (type(value) is not int or value < 0):
                raise DecisionError(
                    "invalid_input", "receipt token usage must be nonnegative"
                )
        _binding(self.source_binding)

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "status": self.status,
            "task_id": self.task_id,
            "rubric_id": self.rubric_id,
            "source_binding": _binding(self.source_binding),
            "model_requested": self.model_requested,
            "model_returned": self.model_returned,
            "answers": {key: value.as_dict() for key, value in self.answers.items()},
            "usage": dict(self.usage),
            "attempts": self.attempts,
            "execution": _json_value(dict(self.execution), "execution"),
            "policy_id": self.policy_id,
            "calibration_id": self.calibration_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> DecisionReceipt:
        if not isinstance(value, Mapping) or value.get("contract") != CONTRACT:
            raise DecisionError("invalid_input", "typed decision contract mismatch")
        data = dict(value)
        data.pop("contract")
        data["answers"] = {
            key: DecisionAnswer(**answer) for key, answer in data["answers"].items()
        }
        return cls(**data)


def parse_answers(
    payload: Any, request: DecisionRequest
) -> tuple[str, dict[str, DecisionAnswer], dict[str, int | None]]:
    """Validate a complete provider reply, preserving distinct probability semantics."""
    if (
        not isinstance(payload, Mapping)
        or not isinstance(payload.get("model"), str)
        or not payload["model"].strip()
    ):
        raise DecisionError("schema_drift", "provider response needs a returned model")
    raw = payload.get("answers")
    if not isinstance(raw, Mapping) or set(raw) != set(request.questions):
        raise DecisionError(
            "schema_drift", "provider response question IDs are missing or unexpected"
        )
    usage = payload.get("usage")
    if not isinstance(usage, Mapping) or not {"input_tokens", "output_tokens"} <= set(
        usage
    ):
        raise DecisionError("schema_drift", "provider response needs token usage")
    for key in ("input_tokens", "output_tokens"):
        value = usage[key]
        if value is not None and (type(value) is not int or value < 0):
            raise DecisionError(
                "schema_drift", "token usage must be nonnegative integers"
            )
    answers = {}
    for key, raw_answer in raw.items():
        question = DecisionQuestion.from_value(request.questions[key])
        if (
            not isinstance(raw_answer, Mapping)
            or raw_answer.get("type") != question.kind
        ):
            raise DecisionError("schema_drift", f"answer type mismatch for {key}")
        if question.kind == "noul":
            if "noul" not in raw_answer:
                raise DecisionError("schema_drift", f"missing noul answer for {key}")
            answers[key] = DecisionAnswer(
                "noul", "answered", _probability(raw_answer["noul"], "noul")
            )
        elif question.kind == "choice":
            expected = set(question.criteria)
            probabilities = _distribution(raw_answer.get("probabilities"), expected)
            choice = raw_answer.get("choice")
            if choice not in expected or probabilities[choice] + 1e-9 < max(
                probabilities.values()
            ):
                raise DecisionError(
                    "schema_drift", f"invalid selected choice for {key}"
                )
            confidence = _probability(raw_answer.get("confidence"), "vendor confidence")
            answers[key] = DecisionAnswer(
                "choice",
                "answered",
                choice,
                probabilities,
                probabilities[choice],
                confidence,
            )
        else:
            expected = {str(index) for index in range(len(question.criteria))}
            probabilities = _distribution(raw_answer.get("probabilities"), expected)
            legend = raw_answer.get("legend")
            if not isinstance(legend, Mapping) or set(legend) != expected:
                raise DecisionError("schema_drift", f"score legend mismatch for {key}")
            if list(
                legend[str(index)] for index in range(len(question.criteria))
            ) != list(question.criteria):
                raise DecisionError(
                    "schema_drift", f"score legend differs from rubric for {key}"
                )
            score = raw_answer.get("score")
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
                or not 0 <= score <= len(question.criteria) - 1
            ):
                raise DecisionError("schema_drift", f"invalid score for {key}")
            expected_score = sum(
                int(level) * probability for level, probability in probabilities.items()
            )
            if abs(score - expected_score) > 0.02:
                raise DecisionError(
                    "schema_drift", f"score is inconsistent with distribution for {key}"
                )
            confidence = _probability(raw_answer.get("confidence"), "vendor confidence")
            answers[key] = DecisionAnswer(
                "score",
                "answered",
                float(score),
                probabilities,
                None,
                confidence,
                dict(legend),
            )
    return (
        payload["model"],
        answers,
        {key: usage[key] for key in ("input_tokens", "output_tokens")},
    )
