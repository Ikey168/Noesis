"""Metrics, calibration, abstention, and promotion gates for human gold data."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .human_eval import FRAME_LABELS, STANCE_LABELS, HumanEvalError, load_gold

PREDICTION_FIELDS = (
    "item_id",
    "model_id",
    "claim_label",
    "claim_confidence",
    "claim_abstained",
    "stance_label",
    "stance_confidence",
    "stance_abstained",
    "frames",
    "frame_confidence",
    "frame_abstained",
)
PROMOTION_IMPROVEMENT = 0.02
MAX_SLICE_REGRESSION = 0.05
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _ratio(numerator: float, denominator: float) -> float:
    return round(float(numerator) / float(denominator), 4) if denominator else 0.0


def _prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    f1 = _ratio(2 * precision * recall, precision + recall)
    return {"precision": precision, "recall": recall, "f1": f1}


def _binary_metrics(truth: Sequence[str], predicted: Sequence[str]) -> dict[str, Any]:
    tp = sum(a == "1" and b == "1" for a, b in zip(truth, predicted))
    fp = sum(a == "0" and b == "1" for a, b in zip(truth, predicted))
    fn = sum(a == "1" and b == "0" for a, b in zip(truth, predicted))
    tn = sum(a == "0" and b == "0" for a, b in zip(truth, predicted))
    return {
        **_prf(tp, fp, fn),
        "accuracy": _ratio(tp + tn, len(truth)),
        "n": len(truth),
        "confusion_matrix": {"labels": ["0", "1"], "values": [[tn, fp], [fn, tp]]},
    }


def _multiclass_metrics(
    truth: Sequence[str], predicted: Sequence[str], labels: Sequence[str]
) -> dict[str, Any]:
    per_label = {}
    matrix = []
    f1s = []
    for actual in labels:
        matrix.append(
            [
                sum(a == actual and b == guess for a, b in zip(truth, predicted))
                for guess in labels
            ]
        )
    for label in labels:
        tp = sum(a == label and b == label for a, b in zip(truth, predicted))
        fp = sum(a != label and b == label for a, b in zip(truth, predicted))
        fn = sum(a == label and b != label for a, b in zip(truth, predicted))
        metrics = {
            **_prf(tp, fp, fn),
            "support": sum(value == label for value in truth),
        }
        per_label[label] = metrics
        if metrics["support"]:
            f1s.append(metrics["f1"])
    return {
        "macro_f1": round(sum(f1s) / len(f1s), 4) if f1s else 0.0,
        "accuracy": _ratio(sum(a == b for a, b in zip(truth, predicted)), len(truth)),
        "n": len(truth),
        "per_label": per_label,
        "confusion_matrix": {"labels": list(labels), "values": matrix},
    }


def _multilabel_metrics(
    truth: Sequence[set[str]], predicted: Sequence[set[str]], labels: Sequence[str]
) -> dict[str, Any]:
    per_label = {}
    f1s = []
    for label in labels:
        tp = sum(label in a and label in b for a, b in zip(truth, predicted))
        fp = sum(label not in a and label in b for a, b in zip(truth, predicted))
        fn = sum(label in a and label not in b for a, b in zip(truth, predicted))
        metrics = {
            **_prf(tp, fp, fn),
            "support": sum(label in value for value in truth),
        }
        per_label[label] = metrics
        if metrics["support"]:
            f1s.append(metrics["f1"])
    return {
        "macro_f1": round(sum(f1s) / len(f1s), 4) if f1s else 0.0,
        "exact_match": _ratio(
            sum(a == b for a, b in zip(truth, predicted)), len(truth)
        ),
        "n": len(truth),
        "per_label": per_label,
    }


def _bootstrap_primary(
    truth: Sequence[Any],
    predicted: Sequence[Any],
    *,
    metric: Any,
    key: str,
    seed: int,
) -> dict[str, Any]:
    if len(truth) < 10:
        return {
            "status": "undersized",
            "n": len(truth),
            "minimum_n": 10,
            "lower": None,
            "upper": None,
        }
    rng = random.Random(seed)
    estimates = []
    for _ in range(500):
        positions = [rng.randrange(len(truth)) for _ in truth]
        sample_truth = [truth[index] for index in positions]
        sample_predicted = [predicted[index] for index in positions]
        estimates.append(float(metric(sample_truth, sample_predicted)[key]))
    estimates.sort()
    return {
        "status": "reported",
        "method": "percentile_bootstrap",
        "iterations": 500,
        "n": len(truth),
        "lower": round(estimates[int(0.025 * (len(estimates) - 1))], 4),
        "upper": round(estimates[int(0.975 * (len(estimates) - 1))], 4),
    }


def _calibration(
    confidences: Sequence[float], correct: Sequence[bool]
) -> dict[str, Any]:
    if not confidences:
        return {"n": 0, "brier": None, "ece": None, "bins": []}
    bins = []
    weighted_gap = 0.0
    for index in range(10):
        lower, upper = index / 10, (index + 1) / 10
        positions = [
            pos
            for pos, value in enumerate(confidences)
            if lower <= value <= upper and (index == 9 or value < upper)
        ]
        if not positions:
            bins.append(
                {
                    "lower": lower,
                    "upper": upper,
                    "n": 0,
                    "accuracy": None,
                    "mean_confidence": None,
                }
            )
            continue
        accuracy = sum(correct[pos] for pos in positions) / len(positions)
        mean_confidence = sum(confidences[pos] for pos in positions) / len(positions)
        weighted_gap += (
            abs(accuracy - mean_confidence) * len(positions) / len(confidences)
        )
        bins.append(
            {
                "lower": lower,
                "upper": upper,
                "n": len(positions),
                "accuracy": round(accuracy, 4),
                "mean_confidence": round(mean_confidence, 4),
            }
        )
    brier = sum(
        (confidence - float(ok)) ** 2 for confidence, ok in zip(confidences, correct)
    )
    return {
        "n": len(confidences),
        "brier": round(brier / len(confidences), 4),
        "ece": round(weighted_gap, 4),
        "bins": bins,
    }


def _selective(
    confidences: Sequence[float], correct: Sequence[bool], abstained: Sequence[bool]
) -> dict[str, Any]:
    retained = [index for index, value in enumerate(abstained) if not value]
    return {
        "n_total": len(abstained),
        "n_answered": len(retained),
        "coverage": _ratio(len(retained), len(abstained)),
        "selective_error": (
            _ratio(sum(not correct[index] for index in retained), len(retained))
            if retained
            else None
        ),
        "calibration": _calibration(
            [confidences[index] for index in retained],
            [correct[index] for index in retained],
        ),
    }


def _threshold_curve(
    confidences: Sequence[float], correct: Sequence[bool]
) -> list[dict[str, Any]]:
    curve = []
    for threshold in (0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        retained = [
            index
            for index, confidence in enumerate(confidences)
            if confidence >= threshold
        ]
        curve.append(
            {
                "threshold": threshold,
                "n_answered": len(retained),
                "coverage": _ratio(len(retained), len(confidences)),
                "selective_error": (
                    _ratio(sum(not correct[index] for index in retained), len(retained))
                    if retained
                    else None
                ),
            }
        )
    return curve


def _boolean(value: str, *, field: str, item_id: str) -> bool:
    normalized = value.strip().casefold()
    if normalized not in {"true", "false", "1", "0"}:
        raise HumanEvalError(f"prediction {item_id} has invalid {field}")
    return normalized in {"true", "1"}


def _confidence(value: str, *, field: str, item_id: str) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise HumanEvalError(f"prediction {item_id} has invalid {field}") from exc
    if not 0.0 <= number <= 1.0 or math.isnan(number):
        raise HumanEvalError(f"prediction {item_id} has out-of-range {field}")
    return number


def _validate_model_id(value: str) -> str:
    if not _MODEL_ID.fullmatch(value):
        raise HumanEvalError(
            "model_id must use only letters, numbers, dot, underscore, plus, and hyphen"
        )
    return value


def read_predictions(path: Path) -> tuple[str, dict[str, dict[str, Any]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != PREDICTION_FIELDS:
            raise HumanEvalError(
                f"{path}: prediction columns do not match the protocol"
            )
        raw_rows = list(reader)
    if not raw_rows:
        raise HumanEvalError(f"{path}: predictions are empty")
    model_ids = {row["model_id"].strip() for row in raw_rows}
    if len(model_ids) != 1 or not next(iter(model_ids), ""):
        raise HumanEvalError(f"{path}: predictions must contain exactly one model_id")
    model_id = _validate_model_id(next(iter(model_ids)))
    rows = {}
    for raw in raw_rows:
        item_id = raw["item_id"].strip()
        if not item_id or item_id in rows:
            raise HumanEvalError(f"{path}: duplicate or empty prediction item_id")
        claim_abstained = _boolean(
            raw["claim_abstained"], field="claim_abstained", item_id=item_id
        )
        stance_abstained = _boolean(
            raw["stance_abstained"], field="stance_abstained", item_id=item_id
        )
        frame_abstained = _boolean(
            raw["frame_abstained"], field="frame_abstained", item_id=item_id
        )
        claim = raw["claim_label"].strip()
        stance = raw["stance_label"].strip().casefold()
        frames = {
            value.strip().casefold()
            for value in raw["frames"].split("|")
            if value.strip()
        }
        if not claim_abstained and claim not in {"0", "1"}:
            raise HumanEvalError(f"prediction {item_id} has invalid claim_label")
        if not stance_abstained and stance not in STANCE_LABELS:
            raise HumanEvalError(f"prediction {item_id} has invalid stance_label")
        if not frame_abstained and (not frames or not frames.issubset(FRAME_LABELS)):
            raise HumanEvalError(f"prediction {item_id} has invalid frames")
        rows[item_id] = {
            "item_id": item_id,
            "model_id": raw["model_id"].strip(),
            "claim_label": claim,
            "claim_confidence": _confidence(
                raw["claim_confidence"], field="claim_confidence", item_id=item_id
            ),
            "claim_abstained": claim_abstained,
            "stance_label": stance,
            "stance_confidence": _confidence(
                raw["stance_confidence"], field="stance_confidence", item_id=item_id
            ),
            "stance_abstained": stance_abstained,
            "frames": frames,
            "frame_confidence": _confidence(
                raw["frame_confidence"], field="frame_confidence", item_id=item_id
            ),
            "frame_abstained": frame_abstained,
        }
    return model_id, rows


def _evaluate_rows(
    gold: list[Mapping[str, Any]], predictions: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    claim_answered = [
        row for row in gold if not predictions[row["item_id"]]["claim_abstained"]
    ]
    stance_answered = [
        row for row in gold if not predictions[row["item_id"]]["stance_abstained"]
    ]
    frame_answered = [
        row for row in gold if not predictions[row["item_id"]]["frame_abstained"]
    ]

    tasks: dict[str, Any] = {}
    for name, answered, truth_field, prediction_field, metric in (
        ("claim", claim_answered, "is_claim", "claim_label", _binary_metrics),
        ("stance", stance_answered, "stance", "stance_label", None),
    ):
        truth = [str(row[truth_field]) for row in answered]
        guessed = [
            str(predictions[row["item_id"]][prediction_field]) for row in answered
        ]
        metrics = (
            metric(truth, guessed)
            if metric
            else _multiclass_metrics(truth, guessed, STANCE_LABELS)
        )
        bootstrap_metric = metric or (
            lambda actual, predicted: _multiclass_metrics(
                actual, predicted, STANCE_LABELS
            )
        )
        metrics["confidence_interval_95"] = _bootstrap_primary(
            truth,
            guessed,
            metric=bootstrap_metric,
            key="f1" if name == "claim" else "macro_f1",
            seed=3101 if name == "claim" else 3102,
        )
        all_confidence = [
            float(predictions[row["item_id"]][f"{name}_confidence"]) for row in gold
        ]
        all_correct = [
            str(predictions[row["item_id"]][prediction_field]) == str(row[truth_field])
            for row in gold
        ]
        all_abstained = [
            bool(predictions[row["item_id"]][f"{name}_abstained"]) for row in gold
        ]
        tasks[name] = {
            "metrics": metrics,
            "selective": _selective(all_confidence, all_correct, all_abstained),
            "threshold_curve": _threshold_curve(all_confidence, all_correct),
        }

    frame_truth = [set(str(row["frames"]).split("|")) for row in frame_answered]
    frame_guessed = [
        set(predictions[row["item_id"]]["frames"]) for row in frame_answered
    ]
    frame_confidence = [
        float(predictions[row["item_id"]]["frame_confidence"]) for row in gold
    ]
    frame_correct = [
        set(predictions[row["item_id"]]["frames"]) == set(str(row["frames"]).split("|"))
        for row in gold
    ]
    frame_abstained = [
        bool(predictions[row["item_id"]]["frame_abstained"]) for row in gold
    ]
    frame_metrics = _multilabel_metrics(frame_truth, frame_guessed, FRAME_LABELS)
    frame_metrics["confidence_interval_95"] = _bootstrap_primary(
        frame_truth,
        frame_guessed,
        metric=lambda actual, predicted: _multilabel_metrics(
            actual, predicted, FRAME_LABELS
        ),
        key="macro_f1",
        seed=3103,
    )
    tasks["frame"] = {
        "metrics": frame_metrics,
        "selective": _selective(frame_confidence, frame_correct, frame_abstained),
        "threshold_curve": _threshold_curve(frame_confidence, frame_correct),
    }
    return tasks


def _slice_reports(
    gold: list[Mapping[str, Any]],
    predictions: Mapping[str, Mapping[str, Any]],
    *,
    minimum_n: int,
) -> dict[str, Any]:
    output = {}
    for field in ("source_type", "language", "length_bucket"):
        groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for row in gold:
            groups[str(row.get(field) or "unknown")].append(row)
        output[field] = {}
        for value, rows in sorted(groups.items()):
            if len(rows) < minimum_n:
                output[field][value] = {
                    "status": "undersized",
                    "n": len(rows),
                    "minimum_n": minimum_n,
                }
            else:
                output[field][value] = {
                    "status": "reported",
                    "n": len(rows),
                    "tasks": _evaluate_rows(rows, predictions),
                }
    return output


def _promotion_gate(
    candidate: Mapping[str, Any], baseline: Mapping[str, Any]
) -> dict[str, Any]:
    failures = []
    warnings = []
    for task in ("stance", "frame"):
        current = float(candidate["tasks"][task]["metrics"]["macro_f1"])
        previous = float(baseline["tasks"][task]["metrics"]["macro_f1"])
        delta = current - previous
        if delta < PROMOTION_IMPROVEMENT:
            failures.append(
                f"{task} macro-F1 improved {delta:+.4f}; +{PROMOTION_IMPROVEMENT:.2f} required"
            )
        current_ece = candidate["tasks"][task]["selective"]["calibration"]["ece"]
        previous_ece = baseline["tasks"][task]["selective"]["calibration"]["ece"]
        if (
            current_ece is not None
            and previous_ece is not None
            and current_ece > previous_ece
        ):
            warnings.append(
                f"{task} ECE regressed {previous_ece:.4f} -> {current_ece:.4f}; review coverage trade-off"
            )
    for dimension, values in candidate.get("slices", {}).items():
        baseline_values = baseline.get("slices", {}).get(dimension, {})
        for value, row in values.items():
            old = baseline_values.get(value)
            if (
                row.get("status") != "reported"
                or not old
                or old.get("status") != "reported"
            ):
                continue
            for task in ("stance", "frame"):
                current = float(row["tasks"][task]["metrics"]["macro_f1"])
                previous = float(old["tasks"][task]["metrics"]["macro_f1"])
                if current - previous < -MAX_SLICE_REGRESSION:
                    failures.append(
                        f"{task} {dimension}={value} regressed {current - previous:+.4f}; "
                        f"limit -{MAX_SLICE_REGRESSION:.2f}"
                    )
    return {
        "accepted": not failures,
        "required_improvement": PROMOTION_IMPROVEMENT,
        "maximum_slice_regression": MAX_SLICE_REGRESSION,
        "failures": failures,
        "warnings": warnings,
        "action": "pin candidate only after accepted=true"
        if not failures
        else "retain current pinned model",
    }


def _markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Human gold model evaluation",
        "",
        f"- Model: `{report['model_id']}`",
        f"- Gold examples (untouched test split): {report['n_test']} of {report['n_gold']}",
        f"- Evaluated: {report['evaluated_at']}",
        "",
        "| Task | Primary metric | 95% interval | Coverage | Selective error | ECE | N |",
        "| --- | ---: | --- | ---: | ---: | ---: | ---: |",
    ]
    for task in ("claim", "stance", "frame"):
        payload = report["tasks"][task]
        metric_name = "f1" if task == "claim" else "macro_f1"
        metric = payload["metrics"][metric_name]
        interval = payload["metrics"]["confidence_interval_95"]
        interval_text = (
            f"{interval['lower']:.4f}–{interval['upper']:.4f}"
            if interval["status"] == "reported"
            else interval["status"]
        )
        selective = payload["selective"]
        error = selective["selective_error"]
        ece = selective["calibration"]["ece"]
        lines.append(
            f"| {task} | {metric:.4f} | {interval_text} | {selective['coverage']:.4f} | "
            f"{error if error is not None else '—'} | {ece if ece is not None else '—'} | "
            f"{payload['metrics']['n']} |"
        )
    lines += ["", "## Failure slices", ""]
    for dimension, values in report["slices"].items():
        lines.append(f"### {dimension}")
        lines.append("")
        for value, row in values.items():
            if row["status"] == "undersized":
                lines.append(
                    f"- `{value}`: undersized (n={row['n']}, minimum={row['minimum_n']})"
                )
            else:
                stance = row["tasks"]["stance"]["metrics"]["macro_f1"]
                frame = row["tasks"]["frame"]["metrics"]["macro_f1"]
                lines.append(
                    f"- `{value}` (n={row['n']}): stance={stance:.4f}, frame={frame:.4f}"
                )
        lines.append("")
    if report.get("promotion_gate"):
        gate = report["promotion_gate"]
        lines += [
            "## Promotion gate",
            "",
            f"Status: **{'accepted' if gate['accepted'] else 'rejected'}**",
            "",
        ]
        lines.extend(f"- {message}" for message in gate["failures"] + gate["warnings"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def evaluate_predictions(
    gold_path: Path,
    predictions_path: Path,
    out: Path,
    *,
    baseline_path: Path | None = None,
    experiment_path: Path | None = None,
    minimum_slice_n: int = 10,
) -> dict[str, Any]:
    gold_all = load_gold(gold_path)
    gold = [row for row in gold_all if row.get("split") == "test"]
    if not gold:
        raise HumanEvalError("gold artifact has no untouched test split")
    model_id, predictions = read_predictions(predictions_path)
    expected = {row["item_id"] for row in gold}
    if set(predictions) != expected:
        missing = len(expected - set(predictions))
        extra = len(set(predictions) - expected)
        raise HumanEvalError(
            f"predictions must match the test split exactly (missing={missing}, extra={extra})"
        )
    report: dict[str, Any] = {
        "contract": "noesis-human-eval-results-v1",
        "evaluated_at": _utc_now(),
        "model_id": model_id,
        "gold_sha256": hashlib.sha256(gold_path.read_bytes()).hexdigest(),
        "predictions_sha256": hashlib.sha256(predictions_path.read_bytes()).hexdigest(),
        "n_gold": len(gold_all),
        "n_test": len(gold),
        "tasks": _evaluate_rows(gold, predictions),
        "slices": _slice_reports(gold, predictions, minimum_n=minimum_slice_n),
        "human_and_synthetic_separated": True,
        "experiment": {"status": "not_supplied"},
    }
    if experiment_path is not None:
        try:
            experiment = json.loads(experiment_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HumanEvalError(f"could not read experiment metadata: {exc}") from exc
        if not isinstance(experiment, dict):
            raise HumanEvalError("experiment metadata must be a JSON object")
        report["experiment"] = experiment
    if baseline_path is not None:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        if baseline.get("gold_sha256") != report["gold_sha256"]:
            raise HumanEvalError(
                "candidate and baseline must use the identical human test set"
            )
        report["promotion_gate"] = _promotion_gate(report, baseline)
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / f"{model_id}.human-eval.json", report)
    (out / f"{model_id}.human-eval.md").write_text(_markdown(report), encoding="utf-8")
    return report


def predict_gold(
    gold_path: Path,
    output_path: Path,
    *,
    model_id: str | None = None,
    claim_threshold: float = 0.0,
    stance_threshold: float = 0.0,
    frame_threshold: float = 0.0,
) -> dict[str, Any]:
    """Run the active pinned/fine-tuned models on the untouched test split."""
    thresholds = {
        "claim": claim_threshold,
        "stance": stance_threshold,
        "frame": frame_threshold,
    }
    if any(not 0.0 <= value <= 1.0 for value in thresholds.values()):
        raise HumanEvalError("abstention thresholds must be between 0 and 1")
    from src.argument_mining.frames import FrameClassifier
    from src.argument_mining.models import ClaimDetector, StanceClassifier

    gold = [row for row in load_gold(gold_path) if row.get("split") == "test"]
    if not gold:
        raise HumanEvalError("gold artifact has no untouched test split")
    claim_model = ClaimDetector()
    stance_model = StanceClassifier()
    frame_model = FrameClassifier()
    active_id = model_id or (
        f"{claim_model.prediction_mode}+{stance_model.prediction_mode}+"
        f"{frame_model.prediction_mode}"
    ).replace("/", "_")
    active_id = _validate_model_id(active_id)
    rows = []
    for row in gold:
        claim = claim_model.predict_text(row["text"])
        stance = stance_model.predict_text(row["text"], row["topic"])
        frame = frame_model.predict_text(row["text"], source_type=row["source_type"])
        predicted_frames = sorted(
            label for label, score in frame.frames.items() if score > 0
        )
        if not predicted_frames:
            predicted_frames = ["other"]
        frame_confidence = max(frame.frames.values(), default=0.0)
        rows.append(
            {
                "item_id": row["item_id"],
                "model_id": active_id,
                "claim_label": str(int(claim.is_claim)),
                "claim_confidence": f"{claim.confidence:.6f}",
                "claim_abstained": str(claim.confidence < claim_threshold).lower(),
                "stance_label": stance.stance,
                "stance_confidence": f"{stance.confidence:.6f}",
                "stance_abstained": str(stance.confidence < stance_threshold).lower(),
                "frames": "|".join(predicted_frames),
                "frame_confidence": f"{frame_confidence:.6f}",
                "frame_abstained": str(frame_confidence < frame_threshold).lower(),
            }
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PREDICTION_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    receipt = {
        "model_id": active_id,
        "n": len(rows),
        "output": str(output_path),
        "gold_sha256": hashlib.sha256(gold_path.read_bytes()).hexdigest(),
        "predictions_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
        "abstention_thresholds": thresholds,
        "prediction_modes": {
            "claim": claim_model.prediction_mode,
            "stance": stance_model.prediction_mode,
            "frame": frame_model.prediction_mode,
        },
    }
    _write_json(output_path.with_suffix(output_path.suffix + ".manifest.json"), receipt)
    return receipt


__all__ = [
    "MAX_SLICE_REGRESSION",
    "PREDICTION_FIELDS",
    "PROMOTION_IMPROVEMENT",
    "evaluate_predictions",
    "predict_gold",
    "read_predictions",
]
