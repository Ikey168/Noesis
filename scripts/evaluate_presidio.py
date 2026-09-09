#!/usr/bin/env python3
"""Execute Presidio redaction accuracy and leak checks on German/English fixtures."""

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

from src.evaluation.presidio_redaction import (
    PresidioConfiguration,
    PresidioRedactor,
    exact_span_metrics,
)


def _expected(text, values):
    result = []
    for value in values:
        start = text.index(value["text"])
        result.append((start, start + len(value["text"]), value["entity_type"]))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/workflow_review/presidio_redaction.json"),
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text())
    config = PresidioConfiguration(score_threshold=0.5)
    start = time.perf_counter()
    redactor = PresidioRedactor.from_spacy_models(configuration=config)
    startup_seconds = time.perf_counter() - start
    runs = []
    aggregate_expected = []
    aggregate_detected = []
    offset = 0
    for case in fixture["cases"]:
        text = case["text"]
        start = time.perf_counter()
        detected = redactor.detect(text, language=case["language"])
        elapsed = time.perf_counter() - start
        expected = _expected(text, case["expected"])
        metrics = exact_span_metrics(expected, detected)
        original = {
            "source_id": case["id"],
            "source_revision": "fixture-v1",
            "text": text,
        }
        before = dict(original)
        artifact = redactor.redact(original, language=case["language"])
        detected_values = [row["text"] for row in detected]
        artifact_metadata = json.dumps(
            {key: value for key, value in artifact.items() if key != "text"},
            ensure_ascii=False,
        )
        metadata_leak = any(value in artifact_metadata for value in detected_values)
        runs.append(
            {
                "id": case["id"],
                "language": case["language"],
                "elapsed_seconds": elapsed,
                "expected": [list(value) for value in expected],
                "detected": detected,
                "metrics": metrics,
                "original_unchanged": original == before,
                "detected_removed_text_leaked_to_metadata": metadata_leak,
                "artifact_traceable_by_hash": bool(
                    artifact["source_reference"]["sha256"]
                )
                and all(row.get("locator_id") for row in artifact["decisions"]),
            }
        )
        aggregate_expected.extend((a + offset, b + offset, c) for a, b, c in expected)
        aggregate_detected.extend(
            {**row, "start": row["start"] + offset, "end": row["end"] + offset}
            for row in detected
        )
        offset += len(text) + 1
    aggregate = exact_span_metrics(aggregate_expected, aggregate_detected)
    report = {
        "contract": "noesis-presidio-evaluation-v1",
        "package": {
            "presidio-analyzer": importlib.metadata.version("presidio-analyzer"),
            "spacy": importlib.metadata.version("spacy"),
            "de_core_news_sm": importlib.metadata.version("de-core-news-sm"),
            "en_core_web_sm": importlib.metadata.version("en-core-web-sm"),
        },
        "configuration": {
            "languages": list(config.languages),
            "entities": list(config.entities),
            "score_threshold": config.score_threshold,
            "policy_version": config.policy_version,
            "execution": "local-only",
        },
        "label_origin": fixture["label_origin"],
        "startup_seconds": startup_seconds,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "runs": runs,
        "aggregate_exact_span_metrics": aggregate,
        "all_originals_unchanged": all(run["original_unchanged"] for run in runs),
        "no_detected_removed_text_leaked_to_metadata": all(
            not run["detected_removed_text_leaked_to_metadata"] for run in runs
        ),
        "production_default_changed": False,
        "decision": "defer unattended redaction; permit explicit local review of derived suggestions. Measured missed spans and false positives prevent treating this configuration as an export-safety certificate.",
        "limitations": [
            "Labels are authored regression expectations, not independent human annotation.",
            "Small spaCy models are evaluated here; language/model changes require a new policy version and benchmark.",
            "Presidio detection reduces disclosure risk but cannot guarantee complete PII removal.",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
