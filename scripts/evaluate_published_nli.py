"""Compare pinned NLI backends on separate published XNLI validation/test rows."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.argument_mining.model_diagnostics import prf
from src.evaluation.mining_runtime import evaluate_policy, fit_policy
from src.evaluation.published_nli import LABELS, prepare
from src.evaluation.runtime_jobs import execute_job


def summarize(result):
    summary = {}
    for language in ("de", "en"):
        validation = [
            r
            for r in result["rows"]
            if r["language"] == language and r["split"] == "validation"
        ]
        test = [
            r
            for r in result["rows"]
            if r["language"] == language and r["split"] == "test"
        ]
        policy = fit_policy(
            validation,
            LABELS,
            task="nli",
            model_version=result["model"]["model"] + "@" + result["model"]["revision"],
            template_version="native-premise-hypothesis-v1",
            minimum_coverage=0.8,
        )
        calibrated = evaluate_policy(test, policy)
        truth = [set(r["labels"]) for r in test]
        predictions = [
            {LABELS[max(range(3), key=r["scores"].__getitem__)]} for r in test
        ]
        raw = prf(truth, predictions, LABELS)
        raw["brier"] = statistics.mean(
            sum(
                (score - (label in row["labels"])) ** 2
                for label, score in zip(LABELS, row["scores"], strict=True)
            )
            for row in test
        )
        confusion = Counter(
            (next(iter(t)), next(iter(p)))
            for t, p in zip(truth, predictions, strict=True)
        )
        raw["confusion_matrix"] = {
            expected: {
                predicted: confusion[expected, predicted] for predicted in LABELS
            }
            for expected in LABELS
        }
        raw["accuracy"] = statistics.mean(
            t == p for t, p in zip(truth, predictions, strict=True)
        )
        latencies = sorted(r["elapsed_s"] for r in test)
        summary[language] = {
            "validation_rows": len(validation),
            "test_rows": len(test),
            "raw": raw,
            "policy": policy,
            "calibrated": calibrated,
            "inference_p50_s": statistics.median(latencies),
            "inference_p95_s": latencies[math.ceil(len(latencies) * 0.95) - 1],
            "test_pairs_per_second": len(test) / sum(latencies),
        }
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    source, rows = prepare(args.data_dir)
    report = {
        "contract": "noesis-published-nli-comparison-v1",
        "source": source,
        "dependency_versions": {
            name: importlib.metadata.version(name)
            for name in ("torch", "transformers", "duckdb")
        },
        "configuration": {
            "per_class_per_language_per_split": 20,
            "rows_per_model": len(rows),
            "timeout_s": 600,
            "max_worker_rss_bytes": 3 * 1024**3,
            "calibration": "language-specific temperature and abstention fitted only on validation; minimum validation coverage 0.8",
        },
        "runs": {},
        "limitations": [
            "Non-commercial research diagnostic on published human NLI labels and professional translations",
            "Small balanced subset, not the full XNLI benchmark",
            "Model-training contamination of public benchmarks is not ruled out",
            "No Berlin/legal-specific task-readiness claim",
            "NLI entailment is textual support, not factual truth",
            "Stance and frames require their own target labels, hypothesis templates and separate evaluations; not certified by these NLI results",
            "Does not replace issue 1420's specified Noesis human collection",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    for kind in ("baseline", "mdeberta"):
        job = execute_job(
            "nli-benchmark",
            {"backend": kind, "rows": rows},
            timeout_s=600,
            max_rss_bytes=3 * 1024**3,
        )
        report["runs"][kind] = {"job": job}
        if job["status"] == "completed":
            report["runs"][kind]["summary"] = summarize(job["result"])
        args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(kind, job["status"], flush=True)
    report["decision"] = (
        "Defer default replacement: published NLI diagnostics do not establish domain-specific support, stance or frame readiness."
    )
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
