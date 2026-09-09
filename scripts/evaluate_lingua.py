#!/usr/bin/env python3
"""Compare optional Lingua detection with the existing langdetect baseline."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import resource
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.rag.language_detection import detect_with_lingua  # noqa: E402


def _langdetect(text: str, threshold: float) -> dict:
    from langdetect import DetectorFactory, detect_langs

    DetectorFactory.seed = 0
    try:
        values = detect_langs(text)
    except Exception as exc:
        return {"language": "unknown", "confidence": 0.0, "uncertain": True, "failure": type(exc).__name__}
    if not values:
        return {"language": "unknown", "confidence": 0.0, "uncertain": True}
    best = values[0]
    confidence = float(best.prob)
    return {
        "language": best.lang if confidence >= threshold else "unknown",
        "confidence": confidence,
        "uncertain": confidence < threshold,
    }


def _segments(text: str, expected: list[dict]) -> list[tuple[int, int, str]]:
    cursor = 0
    result = []
    for item in expected:
        start = text.index(item["text"], cursor)
        end = start + len(item["text"])
        result.append((start, end, item["language"]))
        cursor = end
    return result


def _evaluate(cases: list[dict], lingua_threshold: float, baseline_threshold: float) -> dict:
    rows = []
    lingua_correct = baseline_correct = total = 0
    lingua_abstain = baseline_abstain = 0
    lingua_seconds = baseline_seconds = 0.0
    for case in cases:
        expected = _segments(case["text"], case["segments"])
        lingua_result = detect_with_lingua(
            case["text"], confidence_threshold=lingua_threshold, detect_segments=True
        )
        candidate_segments = lingua_result["segments"]
        if len(candidate_segments) != len(expected):
            # The fixture deliberately aligns to the sentence boundaries used by
            # the candidate. Preserve mismatch as errors rather than massaging it.
            candidate_labels = [row["language"] for row in candidate_segments]
        else:
            candidate_labels = [row["language"] for row in candidate_segments]

        baseline_segments = []
        for start, end, _ in expected:
            started = time.perf_counter()
            detected = _langdetect(case["text"][start:end], baseline_threshold)
            baseline_seconds += time.perf_counter() - started
            baseline_segments.append({"start": start, "end": end, **detected})

        # Rerun candidate segments under timing without changing outputs used for scoring.
        started = time.perf_counter()
        timed_candidate = detect_with_lingua(
            case["text"], confidence_threshold=lingua_threshold, detect_segments=True
        )
        lingua_seconds += time.perf_counter() - started
        candidate_segments = timed_candidate["segments"]

        expected_labels = [value[2] for value in expected]
        for index, expected_label in enumerate(expected_labels):
            candidate_label = candidate_segments[index]["language"] if index < len(candidate_segments) else "unknown"
            baseline_label = baseline_segments[index]["language"]
            lingua_correct += candidate_label == expected_label
            baseline_correct += baseline_label == expected_label
            lingua_abstain += candidate_label == "unknown"
            baseline_abstain += baseline_label == "unknown"
            total += 1
        rows.append(
            {
                "id": case["id"],
                "expected": [
                    {"start": start, "end": end, "language": language}
                    for start, end, language in expected
                ],
                "lingua": candidate_segments,
                "langdetect": baseline_segments,
                "offsets_reconstruct_input": all(
                    case["text"][row["start"] : row["end"]] == row["text"]
                    for row in candidate_segments
                ),
            }
        )
    return {
        "segments": total,
        "lingua_accuracy": lingua_correct / total if total else 1.0,
        "langdetect_accuracy": baseline_correct / total if total else 1.0,
        "lingua_abstention_rate": lingua_abstain / total if total else 0.0,
        "langdetect_abstention_rate": baseline_abstain / total if total else 0.0,
        "lingua_seconds": lingua_seconds,
        "langdetect_seconds": baseline_seconds,
        "rows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/workflow_review/language_detection.json"),
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text())

    # Pick an abstention threshold on the development split only.
    thresholds = (0.55, 0.65, 0.75, 0.85, 0.95)
    development = []
    for threshold in thresholds:
        result = _evaluate(fixture["development"], threshold, threshold)
        development.append(
            {
                "threshold": threshold,
                "lingua_accuracy": result["lingua_accuracy"],
                "lingua_abstention_rate": result["lingua_abstention_rate"],
                "langdetect_accuracy": result["langdetect_accuracy"],
                "langdetect_abstention_rate": result["langdetect_abstention_rate"],
            }
        )
    selected = max(
        development,
        key=lambda row: (
            row["lingua_accuracy"],
            -row["lingua_abstention_rate"],
            row["threshold"],
        ),
    )["threshold"]
    test = _evaluate(fixture["test"], selected, selected)
    report = {
        "contract": "noesis-lingua-evaluation-v1",
        "package": {
            "lingua-language-detector": importlib.metadata.version("lingua-language-detector"),
            "langdetect": "1.0.9-source-package",
        },
        "configured_languages": ["de", "en"],
        "label_origin": fixture["label_origin"],
        "development_thresholds": development,
        "selected_confidence_threshold": selected,
        "test": test,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "coordinate_system": "normalized-input-char-offset-v1",
        "production_default_changed": False,
        "decision": "defer production-default change; the Lingua path provides useful confidence, abstention and mixed-language offsets, but #1510 still requires independently human-labelled language/span evaluation",
        "limitations": [
            "Fixture labels are authored regression labels rather than the independent human labels required by issue #1510.",
            "Only German and English are enabled in this evaluation; changing the language set changes confidence values.",
            "RSS is process-wide and includes both detector libraries loaded for comparison.",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
