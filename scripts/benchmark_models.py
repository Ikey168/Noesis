"""
Benchmark & evaluate all argument mining models (issue #92).

Evaluates ClaimDetector, StanceClassifier, and FrameClassifier against the
held-out test split of the #109 dataset (data/argument_mining/*.parquet).

Cross-dataset generalisation against FEVER, LIAR, and AVeriTeC is attempted
when those files are found at configurable paths; otherwise skipped gracefully.

Usage:
    python scripts/benchmark_models.py
    python scripts/benchmark_models.py --data data/argument_mining --out docs/
    python scripts/benchmark_models.py --gate          # fail if F1 regressed ≥2%
    python scripts/benchmark_models.py --fever /path/to/fever/  # external datasets

Exit codes:
    0 = pass
    1 = gate failure (a model regressed ≥2% F1 vs. previous checkpoint)
    2 = evaluation error
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

FRAME_LABELS = ["economic", "security", "humanitarian", "legal", "political", "scientific", "other"]
STANCE_LABELS = ["supportive", "critical", "neutral", "ambiguous"]

# Length bucket boundaries (characters)
_LEN_BINS = [(0, 100, "short"), (100, 300, "medium"), (300, 10_000, "long")]

# ≥2 pp absolute F1 improvement required to replace a saved checkpoint
GATE_THRESHOLD = 0.02


# ─────────────────────────────────────────────────────────────────────────────
# Metric helpers
# ─────────────────────────────────────────────────────────────────────────────

def _prf1(tp: int, fp: int, fn: int) -> Tuple[float, float, float]:
    p = tp / (tp + fp) if (tp + fp) else 0.0
    r = tp / (tp + fn) if (tp + fn) else 0.0
    f = 2 * p * r / (p + r) if (p + r) else 0.0
    return round(p, 4), round(r, 4), round(f, 4)


def _binary_prf1(y_true: List[int], y_pred: List[int]) -> dict:
    tp = sum(1 for a, b in zip(y_true, y_pred) if a == 1 and b == 1)
    fp = sum(1 for a, b in zip(y_true, y_pred) if a == 0 and b == 1)
    fn = sum(1 for a, b in zip(y_true, y_pred) if a == 1 and b == 0)
    tn = sum(1 for a, b in zip(y_true, y_pred) if a == 0 and b == 0)
    p, r, f = _prf1(tp, fp, fn)
    acc = (tp + tn) / len(y_true) if y_true else 0.0
    return {"precision": p, "recall": r, "f1": f, "accuracy": round(acc, 4), "n": len(y_true)}


def _multiclass_prf1(y_true: List[str], y_pred: List[str], labels: List[str]) -> dict:
    per_class: dict = {}
    f1s = []
    for label in labels:
        tp = sum(1 for a, b in zip(y_true, y_pred) if a == label and b == label)
        fp = sum(1 for a, b in zip(y_true, y_pred) if a != label and b == label)
        fn = sum(1 for a, b in zip(y_true, y_pred) if a == label and b != label)
        p, r, f = _prf1(tp, fp, fn)
        per_class[label] = {"precision": p, "recall": r, "f1": f,
                             "support": sum(1 for x in y_true if x == label)}
        f1s.append(f)
    acc = sum(1 for a, b in zip(y_true, y_pred) if a == b) / len(y_true) if y_true else 0.0
    return {
        "per_class": per_class,
        "macro_f1": round(sum(f1s) / len(f1s), 4) if f1s else 0.0,
        "accuracy": round(acc, 4),
        "n": len(y_true),
    }


def _multilabel_prf1(y_true_sets: List[set], y_pred: List[str], labels: List[str]) -> dict:
    """Evaluate dominant-label prediction against a set of ground-truth labels."""
    per_label: dict = {}
    f1s = []
    for label in labels:
        tp = sum(1 for ts, p in zip(y_true_sets, y_pred) if p == label and label in ts)
        fp = sum(1 for ts, p in zip(y_true_sets, y_pred) if p == label and label not in ts)
        fn = sum(1 for ts, p in zip(y_true_sets, y_pred) if p != label and label in ts)
        p, r, f = _prf1(tp, fp, fn)
        support = sum(1 for ts in y_true_sets if label in ts)
        per_label[label] = {"precision": p, "recall": r, "f1": f, "support": support}
        if support > 0:
            f1s.append(f)
    in_set = sum(1 for ts, p in zip(y_true_sets, y_pred) if p in ts)
    subset_acc = round(in_set / len(y_pred), 4) if y_pred else 0.0
    return {
        "per_label": per_label,
        "macro_f1": round(sum(f1s) / len(f1s), 4) if f1s else 0.0,
        "subset_accuracy": subset_acc,
        "n": len(y_pred),
    }


def _len_bucket(text: str) -> str:
    n = len(text)
    for lo, hi, name in _LEN_BINS:
        if lo <= n < hi:
            return name
    return "long"


def _split_by(keys: List[str], y_true, y_pred) -> Dict[str, Tuple]:
    groups: dict = defaultdict(lambda: ([], []))
    for k, t, p in zip(keys, y_true, y_pred):
        groups[k][0].append(t)
        groups[k][1].append(p)
    return dict(groups)


# ─────────────────────────────────────────────────────────────────────────────
# Dataset loading
# ─────────────────────────────────────────────────────────────────────────────

def _load_test_claims(data_dir: Path):
    import pyarrow.parquet as pq
    t = pq.read_table(data_dir / "claims.parquet").to_pydict()
    rows = [(t["text"][i], int(t["is_claim"][i]), t["source_type"][i])
            for i in range(len(t["text"])) if t["split"][i] == "test"]
    log.info("Loaded %d test claim examples", len(rows))
    return rows


def _load_test_stance(data_dir: Path):
    import pyarrow.parquet as pq
    t = pq.read_table(data_dir / "stance.parquet").to_pydict()
    rows = [(t["text"][i], t["stance"][i], t["topic"][i], t["source_type"][i])
            for i in range(len(t["text"])) if t["split"][i] == "test"]
    log.info("Loaded %d test stance examples", len(rows))
    return rows


def _load_test_frames(data_dir: Path):
    import pyarrow.parquet as pq, json as _json
    t = pq.read_table(data_dir / "frames.parquet").to_pydict()
    rows = []
    for i in range(len(t["text"])):
        if t["split"][i] != "test":
            continue
        raw = t["frames"][i]
        frame_set = set(_json.loads(raw)) if isinstance(raw, str) else set(raw)
        dominant = next(iter(frame_set)) if frame_set else "other"
        rows.append((t["text"][i], frame_set, dominant, t["source_type"][i]))
    log.info("Loaded %d test frame examples", len(rows))
    return rows


def _load_annotation_status(data_dir: Path) -> dict:
    stats_path = data_dir / "stats.json"
    if not stats_path.exists():
        return {}
    with open(stats_path) as f:
        stats = json.load(f)
    return {
        "human_evaluation": stats.get("human_evaluation", {"status": "not_collected"}),
        "simulated_noise_robustness": stats.get("simulated_noise_robustness", {}),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Model evaluation
# ─────────────────────────────────────────────────────────────────────────────

def eval_claim_detector(rows) -> dict:
    from src.argument_mining.models import ClaimDetector
    detector = ClaimDetector()
    mode = detector.prediction_mode
    log.info("ClaimDetector mode: %s", mode)

    texts = [r[0] for r in rows]
    y_true = [r[1] for r in rows]
    source_types = [r[2] for r in rows]

    # Benchmark rows are already single sentences. Use the wrappers' batched
    # model path directly; calling ``predict_text`` would create 1,076 separate
    # DeBERTa forwards and make the scheduled comparison take hours on CPU.
    y_pred = _predict_claim_labels(detector, texts)

    overall = _binary_prf1(y_true, y_pred)
    overall["mode"] = mode

    # Per source-type
    per_type = {}
    groups = _split_by(source_types, y_true, y_pred)
    for stype, (yt, yp) in sorted(groups.items()):
        if len(set(yt)) >= 2:
            per_type[stype] = _binary_prf1(yt, yp)

    # Failure modes by article length
    length_buckets = [_len_bucket(t) for t in texts]
    per_length = {}
    groups_len = _split_by(length_buckets, y_true, y_pred)
    for bucket, (yt, yp) in sorted(groups_len.items()):
        if len(yt) >= 5:
            per_length[bucket] = _binary_prf1(yt, yp)

    return {"overall": overall, "per_source_type": per_type, "per_length": per_length}


def eval_stance_classifier(rows) -> dict:
    from src.argument_mining.models import StanceClassifier
    clf = StanceClassifier()
    mode = clf.prediction_mode
    log.info("StanceClassifier mode: %s", mode)

    texts = [r[0] for r in rows]
    y_true = [r[1] for r in rows]
    topics = [r[2] for r in rows]
    source_types = [r[3] for r in rows]

    if clf._nli is not None and hasattr(clf._nli, "entailment_scores"):
        labels = list(clf.NLI_TEMPLATES)
        pairs = [
            (text, clf.NLI_TEMPLATES[label].format(topic=topic))
            for text, topic in zip(texts, topics) for label in labels
        ]
        scores = clf._nli.entailment_scores(pairs, batch_size=32)
        y_pred = []
        for index in range(len(texts)):
            row_scores = scores[index * len(labels):(index + 1) * len(labels)]
            best_index = max(range(len(labels)), key=row_scores.__getitem__)
            total = sum(row_scores)
            best_share = row_scores[best_index] / total if total else 0.0
            y_pred.append(
                labels[best_index]
                if row_scores[best_index] >= clf.NLI_FLOOR and best_share >= 0.30
                else "neutral"
            )
    elif clf._pipeline is not None:
        predictions = clf._pipeline(
            [f"{topic} [SEP] {text}" for text, topic in zip(texts, topics)],
            truncation=True, max_length=128, batch_size=32,
        )
        from src.argument_mining.dataset import ID2STANCE
        y_pred = [
            ID2STANCE.get(int(prediction["label"].split("_")[1]), "neutral")
            for prediction in predictions
        ]
    else:
        y_pred = [
            clf.predict_text(text, topic).stance
            for text, topic in zip(texts, topics)
        ]

    overall = _multiclass_prf1(y_true, y_pred, STANCE_LABELS)
    overall["mode"] = mode

    # Per source-type
    per_type = {}
    groups = _split_by(source_types, y_true, y_pred)
    for stype, (yt, yp) in sorted(groups.items()):
        if len(yt) >= 5:
            m = _multiclass_prf1(yt, yp, STANCE_LABELS)
            per_type[stype] = {"macro_f1": m["macro_f1"], "accuracy": m["accuracy"], "n": m["n"]}

    # Failure modes by article length
    length_buckets = [_len_bucket(t) for t in texts]
    per_length = {}
    groups_len = _split_by(length_buckets, y_true, y_pred)
    for bucket, (yt, yp) in sorted(groups_len.items()):
        if len(yt) >= 5:
            m = _multiclass_prf1(yt, yp, STANCE_LABELS)
            per_length[bucket] = {"macro_f1": m["macro_f1"], "accuracy": m["accuracy"], "n": m["n"]}

    return {"overall": overall, "per_source_type": per_type, "per_length": per_length}


def eval_frame_classifier(rows) -> dict:
    from src.argument_mining.frames import FrameClassifier
    clf = FrameClassifier()
    mode = clf.prediction_mode
    log.info("FrameClassifier mode: %s", mode)

    texts = [r[0] for r in rows]
    y_true_sets = [r[1] for r in rows]
    y_true_dom = [r[2] for r in rows]
    source_types = [r[3] for r in rows]

    if clf._nli is not None and hasattr(clf._nli, "entailment_scores"):
        labels = list(clf.NLI_TEMPLATES)
        pairs = [
            (text[:1500], clf.NLI_TEMPLATES[label])
            for text in texts for label in labels
        ]
        raw_scores = clf._nli.entailment_scores(pairs, batch_size=32)
        y_pred = []
        for index in range(len(texts)):
            values = raw_scores[index * len(labels):(index + 1) * len(labels)]
            thresholded = [
                score if score >= clf.NLI_THRESHOLDS.get(
                    label, clf.NLI_DEFAULT_THRESHOLD
                ) else 0.0
                for label, score in zip(labels, values)
            ]
            best = max(range(len(labels)), key=thresholded.__getitem__)
            y_pred.append(labels[best] if thresholded[best] > 0 else "other")
    else:
        y_pred = [
            clf.predict_text(text, source_type=source_type).dominant
            for text, source_type in zip(texts, source_types)
        ]

    overall = _multilabel_prf1(y_true_sets, y_pred, FRAME_LABELS)
    overall["mode"] = mode
    # Dominant-only accuracy (predicted dominant == GT dominant)
    overall["dominant_accuracy"] = round(
        sum(1 for a, b in zip(y_true_dom, y_pred) if a == b) / len(y_pred), 4
    ) if y_pred else 0.0

    # Per source-type
    per_type = {}
    groups = _split_by(source_types, list(range(len(y_true_sets))), y_pred)
    for stype, (idxs, preds) in sorted(groups.items()):
        yt_s = [y_true_sets[i] for i in idxs]
        m = _multilabel_prf1(yt_s, preds, FRAME_LABELS)
        per_type[stype] = {"macro_f1": m["macro_f1"], "subset_accuracy": m["subset_accuracy"], "n": m["n"]}

    # Failure modes by article length
    length_buckets = [_len_bucket(t) for t in texts]
    per_length = {}
    idx_groups = _split_by(length_buckets, list(range(len(texts))), y_pred)
    for bucket, (idxs, preds) in sorted(idx_groups.items()):
        yt_l = [y_true_sets[i] for i in idxs]
        m = _multilabel_prf1(yt_l, preds, FRAME_LABELS)
        per_length[bucket] = {"macro_f1": m["macro_f1"], "subset_accuracy": m["subset_accuracy"], "n": m["n"]}

    return {"overall": overall, "per_source_type": per_type, "per_length": per_length}


# ─────────────────────────────────────────────────────────────────────────────
# Cross-dataset generalisation
# ─────────────────────────────────────────────────────────────────────────────

def _predict_claim_labels(detector, texts: List[str]) -> List[int]:
    if detector._pipeline is not None:
        predictions = detector._predict_model(texts)
    elif detector._pretrained is not None:
        predictions = detector._predict_pretrained(texts)
    else:
        predictions = [detector.predict_text(text) for text in texts]
    return [1 if prediction.is_claim else 0 for prediction in predictions]


def _try_fever(fever_dir: Optional[Path], detector=None) -> Optional[dict]:
    """Evaluate ClaimDetector on FEVER dev set (claim vs. non-claim binary proxy)."""
    if fever_dir is None or not fever_dir.exists():
        return None
    # FEVER paper_dev.jsonl: each line has {"label": "SUPPORTS"|"REFUTES"|"NOT ENOUGH INFO", "claim": "..."}
    lines = []
    for fname in ["paper_dev.jsonl", "dev.jsonl", "shared_task_dev.jsonl"]:
        p = fever_dir / fname
        if p.exists():
            lines = p.read_text().splitlines()
            break
    if not lines:
        return None

    from src.argument_mining.models import ClaimDetector
    detector = detector or ClaimDetector()
    y_true, texts = [], []
    for line in lines[:2000]:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        claim_text = obj.get("claim", "")
        label = obj.get("label", "")
        # SUPPORTS/REFUTES are verifiable claims; NOT ENOUGH INFO is not
        true_claim = 1 if label in ("SUPPORTS", "REFUTES") else 0
        y_true.append(true_claim)
        texts.append(claim_text)

    if not y_true:
        return None
    y_pred = _predict_claim_labels(detector, texts)
    m = _binary_prf1(y_true, y_pred)
    log.info("FEVER cross-dataset claim F1=%.4f  (n=%d)", m["f1"], m["n"])
    return m


def _try_liar(liar_dir: Optional[Path], detector=None) -> Optional[dict]:
    """Evaluate ClaimDetector on LIAR dataset (all are claims, so a trivial sanity check
    for precision/recall balance rather than classification accuracy)."""
    if liar_dir is None or not liar_dir.exists():
        return None
    # LIAR TSV format: id | label | statement | ...
    test_tsv = liar_dir / "test.tsv"
    if not test_tsv.exists():
        return None

    from src.argument_mining.models import ClaimDetector
    detector = detector or ClaimDetector()
    y_true, texts = [], []
    for line in test_tsv.read_text().splitlines()[:2000]:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        text = parts[2].strip()
        if not text:
            continue
        # All LIAR items are political claims — true label is always 1
        y_true.append(1)
        texts.append(text)

    if not y_true:
        return None
    y_pred = _predict_claim_labels(detector, texts)
    m = _binary_prf1(y_true, y_pred)
    log.info("LIAR cross-dataset claim F1=%.4f  (n=%d)", m["f1"], m["n"])
    return m


def _try_averitec(averitec_dir: Optional[Path], detector=None) -> Optional[dict]:
    """Evaluate ClaimDetector on AVeriTeC (claim detection proxy)."""
    if averitec_dir is None or not averitec_dir.exists():
        return None
    dev_json = averitec_dir / "dev.json"
    if not dev_json.exists():
        return None

    from src.argument_mining.models import ClaimDetector
    detector = detector or ClaimDetector()
    data = json.loads(dev_json.read_text())
    if isinstance(data, dict):
        data = list(data.values())

    y_true, texts = [], []
    for item in data[:2000]:
        claim_text = item.get("claim", "")
        if not claim_text:
            continue
        y_true.append(1)  # AVeriTeC items are all verifiable claims
        texts.append(claim_text)

    if not y_true:
        return None
    y_pred = _predict_claim_labels(detector, texts)
    m = _binary_prf1(y_true, y_pred)
    log.info("AVeriTeC cross-dataset claim F1=%.4f  (n=%d)", m["f1"], m["n"])
    return m


# ─────────────────────────────────────────────────────────────────────────────
# F1 gate
# ─────────────────────────────────────────────────────────────────────────────

def _check_gate(current: dict, previous: dict) -> List[str]:
    """Return list of failure messages (empty = gate passed)."""
    failures = []
    checks = [
        ("claim_detector",     current["claim_detector"]["overall"]["f1"],
                               previous["claim_detector"]["overall"]["f1"]),
        ("stance_classifier",  current["stance_classifier"]["overall"]["macro_f1"],
                               previous["stance_classifier"]["overall"]["macro_f1"]),
        ("frame_classifier",   current["frame_classifier"]["overall"]["macro_f1"],
                               previous["frame_classifier"]["overall"]["macro_f1"]),
    ]
    for model_name, curr_f1, prev_f1 in checks:
        if prev_f1 is None:
            continue
        delta = curr_f1 - prev_f1
        if delta < -GATE_THRESHOLD:
            failures.append(
                f"{model_name}: F1 regressed {delta:+.4f} "
                f"({prev_f1:.4f} → {curr_f1:.4f}), threshold ±{GATE_THRESHOLD:.2f}"
            )
    return failures


def _check_candidate_gate(current: dict, previous: dict) -> List[str]:
    """Reject a promoted model unless each task metric improves by two points."""
    failures = []
    for model_name, metric in (
        ("claim_detector", "f1"),
        ("stance_classifier", "macro_f1"),
        ("frame_classifier", "macro_f1"),
    ):
        current_f1 = current[model_name]["overall"][metric]
        previous_f1 = previous[model_name]["overall"][metric]
        delta = current_f1 - previous_f1
        if delta < GATE_THRESHOLD:
            failures.append(
                f"{model_name}: candidate improved {delta:+.4f}; at least "
                f"+{GATE_THRESHOLD:.2f} is required ({previous_f1:.4f} → {current_f1:.4f})"
            )
    return failures


@contextlib.contextmanager
def _backend(mode: str):
    """Select all argument-model families consistently for one evaluation."""
    keys = ("NOESIS_CLAIMS_BACKEND", "NOESIS_STANCE_BACKEND", "NOESIS_FRAMES_BACKEND")
    previous = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ[key] = "heuristic" if mode == "heuristic" else "auto"
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _evaluate_backend(mode: str, claim_rows, stance_rows, frame_rows) -> dict:
    with _backend(mode):
        return {
            "claim_detector": eval_claim_detector(claim_rows),
            "stance_classifier": eval_stance_classifier(stance_rows),
            "frame_classifier": eval_frame_classifier(frame_rows),
        }


def _evaluate_external(mode: str, paths: dict) -> dict:
    output = {}
    with _backend(mode):
        from src.argument_mining.models import ClaimDetector
        detector = ClaimDetector()
        for name, fn in (
            ("fever", _try_fever),
            ("liar", _try_liar),
            ("averitec", _try_averitec),
        ):
            result = fn(paths.get(name), detector=detector)
            if result:
                output[name] = result
    return output


# ─────────────────────────────────────────────────────────────────────────────
# Markdown report
# ─────────────────────────────────────────────────────────────────────────────

def _bar(value: float, width: int = 20) -> str:
    filled = round(value * width)
    return "█" * filled + "░" * (width - filled)


def _render_markdown(
    results: dict, gate_requested: bool, gate_failures: List[str]
) -> str:
    ts = results["evaluated_at"]
    claim = results["claim_detector"]
    stance = results["stance_classifier"]
    frame = results["frame_classifier"]
    annotation = results.get("annotation", {})
    cross = results.get("cross_dataset", {})

    lines = [
        "# Argument Mining Model Benchmarks",
        "",
        f"> Generated: {ts}",
        f"> Dataset: held-out test split (claim n={claim['overall']['n']}, "
        f"stance n={stance['overall']['n']}, frame n={frame['overall']['n']})",
        "",
    ]

    # Gate status
    if gate_failures:
        lines += ["## ⚠ Gate Status: FAILED", ""]
        for f in gate_failures:
            lines.append(f"- {f}")
        lines.append("")
    elif gate_requested:
        lines += ["## ✓ Gate Status: PASSED", ""]
    else:
        lines += ["## Gate Status: NOT RUN", "", "Run with `--gate` to enforce the saved baseline.", ""]

    comparison = results.get("backend_comparison") or {}
    if comparison:
        lines += [
            "## Backend comparison",
            "",
            "| Backend | Claims F1 | Stance macro F1 | Frames macro F1 | Active modes |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
        for backend, row in comparison.items():
            modes = ", ".join(sorted({
                row["claim_detector"]["overall"]["mode"],
                row["stance_classifier"]["overall"]["mode"],
                row["frame_classifier"]["overall"]["mode"],
            }))
            lines.append(
                f"| {backend} | {row['claim_detector']['overall']['f1']:.4f} | "
                f"{row['stance_classifier']['overall']['macro_f1']:.4f} | "
                f"{row['frame_classifier']['overall']['macro_f1']:.4f} | {modes} |"
            )
        lines.append("")

    # ── Claim Detector ──────────────────────────────────────────────────────
    co = claim["overall"]
    lines += [
        "## Claim Detector",
        "",
        f"| Mode | Precision | Recall | F1 | Accuracy | N |",
        f"|------|-----------|--------|----|----------|---|",
        f"| {co['mode']} | {co['precision']:.4f} | {co['recall']:.4f} | {co['f1']:.4f} | {co['accuracy']:.4f} | {co['n']} |",
        "",
        "### Per Source Type",
        "",
        "| Source Type | Precision | Recall | F1 | N |",
        "|-------------|-----------|--------|-----|---|",
    ]
    for stype, m in sorted(claim["per_source_type"].items()):
        lines.append(f"| {stype} | {m['precision']:.4f} | {m['recall']:.4f} | {m['f1']:.4f} | {m['n']} |")
    lines += [
        "",
        "### Failure Modes by Article Length",
        "",
        "| Length Bucket | F1 | Precision | Recall | N |",
        "|---------------|----|-----------|--------|---|",
    ]
    for bucket, m in sorted(claim["per_length"].items()):
        lines.append(f"| {bucket} | {m['f1']:.4f} | {m['precision']:.4f} | {m['recall']:.4f} | {m['n']} |")
    lines.append("")

    # ── Stance Classifier ───────────────────────────────────────────────────
    so = stance["overall"]
    lines += [
        "## Stance Classifier",
        "",
        f"| Mode | Macro F1 | Accuracy | N |",
        f"|------|----------|----------|---|",
        f"| {so['mode']} | {so['macro_f1']:.4f} | {so['accuracy']:.4f} | {so['n']} |",
        "",
        "### Per Class",
        "",
        "| Class | Precision | Recall | F1 | Support |",
        "|-------|-----------|--------|----|---------|",
    ]
    for cls, m in so["per_class"].items():
        lines.append(f"| {cls} | {m['precision']:.4f} | {m['recall']:.4f} | {m['f1']:.4f} | {m['support']} |")
    lines += [
        "",
        "### Per Source Type",
        "",
        "| Source Type | Macro F1 | Accuracy | N |",
        "|-------------|----------|----------|---|",
    ]
    for stype, m in sorted(stance["per_source_type"].items()):
        lines.append(f"| {stype} | {m['macro_f1']:.4f} | {m['accuracy']:.4f} | {m['n']} |")
    lines += [
        "",
        "### Failure Modes by Article Length",
        "",
        "| Length | Macro F1 | Accuracy | N |",
        "|--------|----------|----------|---|",
    ]
    for bucket, m in sorted(stance["per_length"].items()):
        lines.append(f"| {bucket} | {m['macro_f1']:.4f} | {m['accuracy']:.4f} | {m['n']} |")
    lines.append("")

    # ── Frame Classifier ─────────────────────────────────────────────────────
    fo = frame["overall"]
    lines += [
        "## Frame Classifier",
        "",
        f"| Mode | Macro F1 | Subset Accuracy | Dominant Accuracy | N |",
        f"|------|----------|-----------------|-------------------|---|",
        f"| {fo['mode']} | {fo['macro_f1']:.4f} | {fo['subset_accuracy']:.4f} | {fo['dominant_accuracy']:.4f} | {fo['n']} |",
        "",
        "### Per Frame Label",
        "",
        "| Frame | Precision | Recall | F1 | Support |",
        "|-------|-----------|--------|----|---------|",
    ]
    for lbl, m in fo["per_label"].items():
        lines.append(f"| {lbl} | {m['precision']:.4f} | {m['recall']:.4f} | {m['f1']:.4f} | {m['support']} |")
    lines += [
        "",
        "### Per Source Type",
        "",
        "| Source Type | Macro F1 | Subset Accuracy | N |",
        "|-------------|----------|-----------------|---|",
    ]
    for stype, m in sorted(frame["per_source_type"].items()):
        lines.append(f"| {stype} | {m['macro_f1']:.4f} | {m['subset_accuracy']:.4f} | {m['n']} |")
    lines += [
        "",
        "### Failure Modes by Article Length",
        "",
        "| Length | Macro F1 | Subset Accuracy | N |",
        "|--------|----------|-----------------|---|",
    ]
    for bucket, m in sorted(frame["per_length"].items()):
        lines.append(f"| {bucket} | {m['macro_f1']:.4f} | {m['subset_accuracy']:.4f} | {m['n']} |")
    lines.append("")

    # ── Annotation provenance ────────────────────────────────────────────────
    if annotation:
        human = annotation.get("human_evaluation", {})
        noise = annotation.get("simulated_noise_robustness", {})
        lines += [
            "## Annotation provenance",
            "",
            f"Human evaluation status: **{human.get('status', 'not_collected')}**.",
            "",
        ]
        if human.get("status") == "complete":
            lines += [
                "| Human metric | Cohen's κ |",
                "|--------------|-----------|",
                f"| Claim detection | {human.get('kappa_claim', 0):.4f} |",
                f"| Stance classification | {human.get('kappa_stance', 0):.4f} |",
            ]
        if noise:
            lines += [
                "",
                "> Synthetic labels are used only for pipeline smoke tests. The",
                "> random-perturbation similarity numbers are not IAA or a quality gate.",
            ]
        lines.append("")

    # ── Cross-dataset ─────────────────────────────────────────────────────────
    lines += [
        "## Cross-Dataset Generalisation",
        "",
        "Claim detector (binary) evaluated against external benchmarks.",
        "",
        "| Dataset | Precision | Recall | F1 | N | Notes |",
        "|---------|-----------|--------|----|---|-------|",
    ]
    if cross.get("fever"):
        m = cross["fever"]
        lines.append(f"| FEVER | {m['precision']:.4f} | {m['recall']:.4f} | {m['f1']:.4f} | {m['n']} | SUPPORTS/REFUTES=claim; NEI=non-claim |")
    else:
        lines.append("| FEVER | — | — | — | — | not available; set --fever to path |")
    if cross.get("liar"):
        m = cross["liar"]
        lines.append(f"| LIAR | {m['precision']:.4f} | {m['recall']:.4f} | {m['f1']:.4f} | {m['n']} | all political claims (sanity check) |")
    else:
        lines.append("| LIAR | — | — | — | — | not available; set --liar to path |")
    if cross.get("averitec"):
        m = cross["averitec"]
        lines.append(f"| AVeriTeC | {m['precision']:.4f} | {m['recall']:.4f} | {m['f1']:.4f} | {m['n']} | verifiable real-world claims |")
    else:
        lines.append("| AVeriTeC | — | — | — | — | not available; set --averitec to path |")
    lines.append("")

    external_comparison = results.get("cross_dataset_comparison") or {}
    if external_comparison:
        lines += [
            "### External backend comparison",
            "",
            "| Backend | Dataset | Precision | Recall | F1 | N |",
            "| --- | --- | ---: | ---: | ---: | ---: |",
        ]
        for backend, datasets in external_comparison.items():
            for dataset, metrics in datasets.items():
                lines.append(
                    f"| {backend} | {dataset.upper()} | "
                    f"{metrics['precision']:.4f} | {metrics['recall']:.4f} | "
                    f"{metrics['f1']:.4f} | {metrics['n']} |"
                )
        lines.append("")

    # ── Model update gate ────────────────────────────────────────────────────
    lines += [
        "## Model Update Gate",
        "",
        f"Any model update must show ≥{GATE_THRESHOLD*100:.0f}% absolute F1 improvement over the",
        "previous checkpoint stored in `docs/benchmark_results.json`.",
        "",
        "| Model | Condition | Threshold |",
        "|-------|-----------|-----------|",
        "| ClaimDetector | binary F1 | ≥+2 pp |",
        "| StanceClassifier | macro F1 | ≥+2 pp |",
        "| FrameClassifier | macro F1 | ≥+2 pp |",
        "",
        "Re-run `python scripts/benchmark_models.py --gate` after training to validate.",
        "",
        "---",
        "",
        f"*Benchmarks auto-generated by `scripts/benchmark_models.py`.*",
    ]

    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=REPO / "data" / "argument_mining")
    ap.add_argument("--out", type=Path, default=REPO / "docs")
    ap.add_argument("--gate", action="store_true",
                    help="Fail (exit 1) if any model F1 regressed ≥2 pp vs. previous checkpoint")
    ap.add_argument("--candidate-gate", action="store_true",
                    help="Fail unless every candidate task metric improves by at least 2 pp")
    ap.add_argument("--backend", choices=("auto", "heuristic"), default="auto",
                    help="Primary backend (auto uses cached pretrained weights when available)")
    ap.add_argument("--compare-backends", action="store_true",
                    help="Report explicit heuristic and cached-pretrained/default modes side by side")
    ap.add_argument("--fever", type=Path, default=None, help="Path to FEVER dataset directory")
    ap.add_argument("--liar", type=Path, default=None, help="Path to LIAR dataset directory")
    ap.add_argument("--averitec", type=Path, default=None, help="Path to AVeriTeC dataset directory")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)

    # Prepared external benchmarks are picked up by default when present
    # (scripts/prepare_external_benchmarks.py puts them here); an explicit
    # --fever/--liar/--averitec path always wins. (#957)
    external_root = REPO / "data" / "external_benchmarks"
    for attr in ("fever", "liar", "averitec"):
        if getattr(args, attr) is None:
            candidate = external_root / attr
            if candidate.exists():
                setattr(args, attr, candidate)
                log.info("Using prepared %s benchmark at %s", attr.upper(), candidate)

    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    # JSON stays at docs/benchmark_results.json (read by the get_benchmark_results
    # MCP tool); the human-readable report lives under the subsystems section.
    json_path = out_dir / "benchmark_results.json"
    md_path = out_dir / "subsystems" / "argument-mining-benchmarks.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)

    # ── Load previous checkpoint ──────────────────────────────────────────────
    previous: Optional[dict] = None
    if json_path.exists():
        try:
            previous = json.loads(json_path.read_text())
            log.info("Loaded previous checkpoint from %s", json_path)
        except Exception:
            log.warning("Could not parse previous %s; skipping gate", json_path)

    # ── Evaluate ──────────────────────────────────────────────────────────────
    log.info("Loading test datasets from %s", args.data)
    try:
        claim_rows = _load_test_claims(args.data)
        stance_rows = _load_test_stance(args.data)
        frame_rows = _load_test_frames(args.data)
    except Exception as e:
        log.error("Failed to load dataset: %s", e)
        return 2

    log.info("Evaluating argument models with backend=%s …", args.backend)
    primary = _evaluate_backend(args.backend, claim_rows, stance_rows, frame_rows)
    claim_metrics = primary["claim_detector"]
    stance_metrics = primary["stance_classifier"]
    frame_metrics = primary["frame_classifier"]

    annotation = _load_annotation_status(args.data)

    # Cross-dataset (skip gracefully if not available)
    external_paths = {name: getattr(args, name) for name in ("fever", "liar", "averitec")}
    cross = _evaluate_external(args.backend, external_paths)
    for name in external_paths:
        if name not in cross:
            log.info("Cross-dataset %s: not available", name.upper())

    # ── Assemble results ──────────────────────────────────────────────────────
    results = {
        "evaluated_at": datetime.now(tz=timezone.utc).isoformat(),
        "claim_detector": claim_metrics,
        "stance_classifier": stance_metrics,
        "frame_classifier": frame_metrics,
        "annotation": annotation,
        "cross_dataset": cross,
    }
    if args.compare_backends:
        auto_metrics = primary if args.backend == "auto" else _evaluate_backend(
            "auto", claim_rows, stance_rows, frame_rows
        )
        heuristic_metrics = primary if args.backend == "heuristic" else _evaluate_backend(
            "heuristic", claim_rows, stance_rows, frame_rows
        )
        auto_external = cross if args.backend == "auto" else _evaluate_external(
            "auto", external_paths
        )
        heuristic_external = cross if args.backend == "heuristic" else _evaluate_external(
            "heuristic", external_paths
        )
        results["backend_comparison"] = {
            "heuristic": heuristic_metrics,
            "cached-pretrained-default": auto_metrics,
        }
        results["cross_dataset_comparison"] = {
            "heuristic": heuristic_external,
            "cached-pretrained-default": auto_external,
        }

    # ── Gate check ────────────────────────────────────────────────────────────
    gate_failures: List[str] = []
    baseline = previous
    if previous:
        comparison = previous.get("backend_comparison", {})
        baseline_key = (
            "heuristic" if args.backend == "heuristic"
            else "cached-pretrained-default"
        )
        baseline = comparison.get(baseline_key, previous)
    if args.gate and previous:
        gate_failures = _check_gate(results, baseline)
    if args.candidate_gate:
        if previous:
            gate_failures.extend(_check_candidate_gate(results, baseline))
        else:
            gate_failures.append("candidate gate requires a previous benchmark checkpoint")

    # ── Write outputs ─────────────────────────────────────────────────────────
    json_path.write_text(json.dumps(results, indent=2))
    log.info("Results written to %s", json_path)

    md_path.write_text(_render_markdown(
        results, args.gate or args.candidate_gate, gate_failures
    ))
    log.info("Benchmark report written to %s", md_path)

    # ── Summary ───────────────────────────────────────────────────────────────
    c_f1 = claim_metrics["overall"]["f1"]
    s_f1 = stance_metrics["overall"]["macro_f1"]
    fm_f1 = frame_metrics["overall"]["macro_f1"]
    c_mode = claim_metrics["overall"]["mode"]
    s_mode = stance_metrics["overall"]["mode"]
    fm_mode = frame_metrics["overall"]["mode"]

    print(f"\n{'─' * 72}")
    print(f"  Argument Mining Benchmark Results")
    print(f"{'─' * 72}")
    print(f"  ClaimDetector    [{c_mode:>10}]  F1 = {c_f1:.4f}   {_bar(c_f1)}")
    print(f"  StanceClassifier [{s_mode:>10}]  F1 = {s_f1:.4f}   {_bar(s_f1)}")
    print(f"  FrameClassifier  [{fm_mode:>10}]  F1 = {fm_f1:.4f}   {_bar(fm_f1)}")
    human = annotation.get("human_evaluation", {})
    print(f"\n  Human evaluation: {human.get('status', 'not_collected')}")
    if cross:
        print(f"\n  Cross-dataset: {', '.join(cross.keys())}")
    print(f"{'─' * 72}")
    if gate_failures:
        print(f"  GATE: FAILED")
        for f in gate_failures:
            print(f"    ✗ {f}")
        print(f"{'─' * 72}\n")
        return 1
    print(f"  GATE: PASSED")
    print(f"{'─' * 72}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
