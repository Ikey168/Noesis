"""Pinned multilingual NLI and validation-only calibration for stance and frames.

Calibration policies are reproducible artifacts, not live-domain readiness
claims. A frozen test set never participates in temperature/threshold selection.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping

from src.argument_mining.model_diagnostics import prf
from src.evaluation.model_backends import bounded_texts, model_path
from src.evaluation.runtime_errors import BackendError
from src.kb.nli import NLIResult, TransformersNLI


def _hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


class MultilingualNLI(TransformersNLI):
    """mDeBERTa execution compatible with Noesis stance/frame/evidence wrappers."""

    def __init__(self, *, model=None, tokenizer=None, device="cpu"):
        from src.argument_mining.model_registry import optional_model_spec

        self.spec = optional_model_spec("mdeberta")
        if model is None or tokenizer is None:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            path, _ = model_path("mdeberta")
            tokenizer = AutoTokenizer.from_pretrained(
                path, local_files_only=True, trust_remote_code=False
            )
            model = (
                AutoModelForSequenceClassification.from_pretrained(
                    path, local_files_only=True, trust_remote_code=False,
                    dtype=torch.float32 if str(device) == "cpu" else "auto",
                )
                .to(device)
                .eval()
            )
        self._tokenizer, self._model = tokenizer, model
        self.spec = {**self.spec, "runtime_dtype": str(getattr(model, "dtype", "unknown")), "device": str(device)}
        self._id2label = {
            int(key): str(value).lower() for key, value in model.config.id2label.items()
        }
        if set(self._id2label) != {0, 1, 2} or set(self._id2label.values()) != {
            "entailment",
            "neutral",
            "contradiction",
        }:
            raise BackendError(
                "label_mapping", "NLI must expose all three explicit semantic labels"
            )
        self._entailment_index = next(
            i for i, label in self._id2label.items() if label == "entailment"
        )
        self._contradiction_index = next(
            i for i, label in self._id2label.items() if label == "contradiction"
        )
        self.model_name = self.spec["model"]
        self.name = "nli:" + self.model_name
        self.model_version = self.model_name + "@" + self.spec["revision"]
        self.prediction_mode = "zero-shot:" + self.model_version

    def _bounded_inputs(self, premises, hypotheses):
        bounded_texts(premises)
        bounded_texts(hypotheses)
        if len(premises) != len(hypotheses):
            raise ValueError("aligned premise/hypothesis pairs required")
        values = super()._bounded_inputs(premises, hypotheses)
        return {key: value.to(self._model.device) for key, value in values.items()}

    def probabilities(self, pairs, *, batch_size=8):
        import torch

        if type(batch_size) is not int or not 1 <= batch_size <= 32 or len(pairs) > 256:
            raise BackendError("input_limit", "NLI batch or pair limit exceeded")
        output = []
        for start in range(0, len(pairs), batch_size):
            selected = pairs[start : start + batch_size]
            values = self._bounded_inputs(
                [p for p, _ in selected], [h for _, h in selected]
            )
            with torch.inference_mode():
                logits = self._model(**values).logits
                probabilities = torch.softmax(logits, dim=-1).tolist()
            if len(probabilities) != len(selected):
                raise BackendError("invalid_model_output", "NLI lost pair alignment")
            for row in probabilities:
                if len(row) != 3 or any(not math.isfinite(value) for value in row):
                    raise BackendError("invalid_model_output", "non-finite NLI output")
                output.append(
                    {self._id2label[i]: float(value) for i, value in enumerate(row)}
                )
        return output

    def classify(self, premise, hypothesis):
        probabilities = self.probabilities([(premise, hypothesis)])[0]
        label = max(probabilities, key=probabilities.get)
        return NLIResult(label, probabilities[label], self.prediction_mode)


def _checked_rows(rows, labels, task, split):
    if task not in {"stance", "frames", "nli"} or not 1 <= len(rows) <= 10000:
        raise ValueError("bounded rows and supported calibration task required")
    if not 2 <= len(labels) <= 64 or len(set(labels)) != len(labels):
        raise ValueError("distinct bounded class labels required")
    ids, groups = set(), set()
    clean = []
    for row in rows:
        required = {
            "id",
            "group_id",
            "split",
            "scores",
            "labels",
            "label_origin",
            "source",
            "domain",
            "language",
        }
        if (
            not isinstance(row, Mapping)
            or not required <= row.keys()
            or row["split"] != split
        ):
            raise ValueError(
                f"each row must declare {split} split, group, provenance and scores"
            )
        if not row["id"] or row["id"] in ids or not row["group_id"]:
            raise ValueError(
                "unique row identities and related-document groups required"
            )
        scores = row["scores"]
        if len(scores) != len(labels) or any(
            type(v) not in {int, float} or not math.isfinite(v) or not 0 <= v <= 1
            for v in scores
        ):
            raise ValueError("finite class scores in [0,1] required")
        if (
            not set(row["labels"]) <= set(labels)
            or task != "frames"
            and len(row["labels"]) != 1
        ):
            raise ValueError("invalid task labels")
        if row["label_origin"] not in {
            "independent-human",
            "assisted-human",
            "model",
            "fixture",
        }:
            raise ValueError("explicit label origin required")
        clean.append(dict(row))
        ids.add(row["id"])
        groups.add(row["group_id"])
    return clean, ids, groups


def calibrated_scores(scores, temperature, task):
    if not math.isfinite(temperature) or not 0.1 <= temperature <= 10:
        raise ValueError("invalid temperature")
    clipped = [min(1 - 1e-9, max(1e-9, v)) for v in scores]
    if task == "frames":
        return [
            1 / (1 + math.exp(-math.log(v / (1 - v)) / temperature)) for v in clipped
        ]
    logs = [math.log(v) / temperature for v in clipped]
    exp = [math.exp(v - max(logs)) for v in logs]
    return [v / sum(exp) for v in exp]


def apply_policy(scores, policy):
    values = calibrated_scores(scores, policy["temperature"], policy["task"])
    labels = policy["labels"]
    if len(values) != len(labels):
        raise ValueError("model class schema changed after calibration")
    if policy["task"] == "frames":
        selected = [
            label
            for label, value in zip(labels, values, strict=True)
            if value >= policy["thresholds"][label]
        ]
    else:
        best = max(range(len(values)), key=values.__getitem__)
        selected = [labels[best]] if values[best] >= policy["threshold"] else []
    return {
        "labels": selected,
        "scores": dict(zip(labels, values, strict=True)),
        "status": "classified" if selected else "unsupported",
        "confidence_is_accuracy": False,
    }


def fit_policy(
    validation_rows,
    labels,
    *,
    task,
    model_version,
    template_version,
    minimum_coverage=0.8,
):
    """Fit temperature by log loss, then thresholds, on validation rows only."""
    rows, ids, groups = _checked_rows(validation_rows, labels, task, "validation")
    if not model_version or not template_version or not 0 <= minimum_coverage <= 1:
        raise ValueError("model/template versions and coverage target required")
    labels = list(labels)
    temperatures = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0]

    def loss(temperature):
        total = 0.0
        for row in rows:
            values = calibrated_scores(row["scores"], temperature, task)
            for i, label in enumerate(labels):
                p = min(1 - 1e-9, max(1e-9, values[i]))
                if task == "frames":
                    total -= math.log(p if label in row["labels"] else 1 - p) / len(
                        labels
                    )
                elif label in row["labels"]:
                    total -= math.log(p)
        return total / len(rows)

    temperature = min(temperatures, key=lambda value: (loss(value), abs(value - 1.0)))
    policy = {
        "contract": "noesis-mining-calibration-v1",
        "task": task,
        "labels": labels,
        "model_version": model_version,
        "template_version": template_version,
        "temperature": temperature,
        "validation_ids": sorted(ids),
        "validation_groups": sorted(groups),
        "validation_sha256": _hash(rows),
        "label_origins": sorted({row["label_origin"] for row in rows}),
        "minimum_coverage": minimum_coverage,
        "selection_split": "validation",
        "task_ready": False,
    }
    thresholds = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95]
    truth = [set(row["labels"]) for row in rows]
    if task == "frames":
        policy["thresholds"] = {}
        for index, label in enumerate(labels):

            def quality(threshold, label=label, index=index):
                predictions = [
                    {label}
                    if calibrated_scores(row["scores"], temperature, task)[index]
                    >= threshold
                    else set()
                    for row in rows
                ]
                metric = prf([value & {label} for value in truth], predictions, [label])
                return metric["macro_f1"], threshold

            policy["thresholds"][label] = max(thresholds, key=quality)
    else:
        candidates = []
        for threshold in thresholds:
            candidate = {**policy, "threshold": threshold}
            predicted = [
                set(apply_policy(row["scores"], candidate)["labels"]) for row in rows
            ]
            coverage = sum(bool(value) for value in predicted) / len(rows)
            candidates.append(
                (
                    coverage >= minimum_coverage,
                    prf(truth, predicted, labels)["macro_f1"],
                    threshold,
                )
            )
        policy["threshold"] = max(candidates)[2]
    policy["validation_log_loss"] = loss(temperature)
    policy["policy_sha256"] = _hash(policy)
    return policy


def validate_policy(policy, *, task=None, model_version=None, template_version=None):
    core = {key: value for key, value in policy.items() if key != "policy_sha256"}
    if policy.get("contract") != "noesis-mining-calibration-v1" or _hash(
        core
    ) != policy.get("policy_sha256"):
        raise ValueError("calibration policy hash or contract mismatch")
    for key, expected in (
        ("task", task),
        ("model_version", model_version),
        ("template_version", template_version),
    ):
        if expected is not None and policy.get(key) != expected:
            raise ValueError("calibration model, task or hypothesis template changed")


def evaluate_policy(test_rows, policy):
    validate_policy(policy)
    rows, ids, groups = _checked_rows(
        test_rows, policy["labels"], policy["task"], "test"
    )
    if ids & set(policy["validation_ids"]) or groups & set(policy["validation_groups"]):
        raise ValueError(
            "related-document or row leakage across validation/test splits"
        )
    outcomes = [apply_policy(row["scores"], policy) for row in rows]
    truth = [set(row["labels"]) for row in rows]
    predictions = [set(row["labels"]) for row in outcomes]
    metric = prf(truth, predictions, policy["labels"])
    metric["coverage"] = sum(bool(v) for v in predictions) / len(rows)
    metric["confusions"] = [
        {
            "expected": list(row["labels"]),
            "predicted": outcome["labels"],
            "id": row["id"],
        }
        for row, outcome in zip(rows, outcomes, strict=True)
        if set(row["labels"]) != set(outcome["labels"])
    ]
    probabilities = [outcome["scores"] for outcome in outcomes]
    metric["brier"] = sum(
        sum((p[label] - (label in t)) ** 2 for label in policy["labels"])
        for p, t in zip(probabilities, truth, strict=True)
    ) / len(rows)
    metric["per_class_calibration"] = {}
    for label in policy["labels"]:
        bins = []
        for index in range(10):
            selected = [
                i
                for i, p in enumerate(probabilities)
                if min(9, int(p[label] * 10)) == index
            ]
            if selected:
                bins.append(
                    {
                        "n": len(selected),
                        "mean_probability": sum(
                            probabilities[i][label] for i in selected
                        )
                        / len(selected),
                        "positive_fraction": sum(label in truth[i] for i in selected)
                        / len(selected),
                    }
                )
        metric["per_class_calibration"][label] = {
            "bins": bins,
            "ece": sum(
                v["n"] * abs(v["mean_probability"] - v["positive_fraction"])
                for v in bins
            )
            / len(rows),
        }
    metric["breakdowns"] = {}
    for field in ("source", "domain", "language"):
        metric["breakdowns"][field] = {}
        for group in sorted({row[field] for row in rows}):
            selected = [i for i, row in enumerate(rows) if row[field] == group]
            metric["breakdowns"][field][group] = prf(
                [truth[i] for i in selected],
                [predictions[i] for i in selected],
                policy["labels"],
            )
    return {
        "contract": "noesis-mining-calibration-result-v1",
        "policy_sha256": policy["policy_sha256"],
        "test_sha256": _hash(rows),
        "metrics": metric,
        "outcomes": outcomes,
        "label_origins": sorted({row["label_origin"] for row in rows}),
        "task_ready": False,
        "readiness_requires": "independent human dataset, absolute per-class criteria and explicit domain review",
        "cost_usd_micros": None,
        "cost_semantics": "not supplied; metrics alone do not measure runtime cost",
    }


class CalibratedMiningBackend:
    """Optional inference using frozen task-specific hypothesis and policy artifacts."""

    def __init__(self, nli, policy, templates):
        if not isinstance(templates, Mapping) or set(templates) != set(
            policy["labels"]
        ):
            raise ValueError("hypothesis schema must match calibrated labels")
        validate_policy(
            policy,
            model_version=nli.model_version,
            template_version=_hash(dict(templates)),
        )
        self.nli, self.policy, self.templates = nli, dict(policy), dict(templates)

    def predict(self, text, *, topic=""):
        bounded_texts([text, topic])
        labels, values, evidence = self.policy["labels"], [], []
        for label in labels:
            hypothesis = self.templates[label].format(topic=topic)
            # Existing windowed path preserves all source spans and abstains when
            # windows support opposing readings. No text[:1500] shortcut here.
            result = self.nli.classify_evidence(text, hypothesis)
            if not result.get("coverage_complete"):
                raise BackendError(
                    "incomplete_evidence",
                    "mining requires complete source-window coverage",
                )
            values.append(
                result["confidence"] if result["label"] == "entailment" else 0.0
            )
            evidence.append({"label": label, "assessment": result})
        return {
            **apply_policy(values, self.policy),
            "raw_scores": values,
            "evidence": evidence,
            "policy_sha256": self.policy["policy_sha256"],
            "model_version": self.nli.model_version,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "topic": topic,
        }
