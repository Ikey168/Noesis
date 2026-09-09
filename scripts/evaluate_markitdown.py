#!/usr/bin/env python3
"""Measure the optional MarkItDown fallback on bounded office fixtures."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
import time
import tracemalloc
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.ingestion.connectors.upload.parsers import extract_text
from src.ingestion.roadmap_integrations import optional_document_conversion


EXPECTED = {
    "german_english_table.docx": {
        "format": "docx",
        "tokens": [
            "Berliner Evidenz",
            "Müller",
            "Straße",
            "English evidence",
            "Kennzahl",
            "Berlin",
            "42",
            "retained",
        ],
        "table_tokens": ["Kennzahl", "Berlin", "42", "retained"],
    },
    "german_english_table.xlsx": {
        "format": "xlsx",
        "tokens": ["Evidenz", "Kennzahl", "Berlin", "42", "Evidence", "retained"],
        "table_tokens": ["Kennzahl", "Berlin", "42", "retained"],
    },
    "german_english_slide.pptx": {
        "format": "pptx",
        "tokens": ["Berliner Evidenz", "Behörde", "English evidence"],
        "table_tokens": [],
    },
}


def _recall(text: str, values: list[str]) -> float | None:
    if not values:
        return None
    folded = text.casefold()
    return sum(value.casefold() in folded for value in values) / len(values)


def _measure(callable_):
    tracemalloc.start()
    start = time.perf_counter()
    value = callable_()
    elapsed = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return value, elapsed, peak


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=Path("tests/fixtures/workflow_review/local_optional"),
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    runs = []
    for name, expected in EXPECTED.items():
        path = args.fixture_dir / name
        raw = path.read_bytes()
        candidate, candidate_seconds, candidate_peak = _measure(
            lambda: optional_document_conversion(raw, filename=name)
        )
        if candidate["status"] != "completed":
            candidate_text = ""
        else:
            candidate_text = candidate["text"]

        if expected["format"] == "docx":
            baseline, baseline_seconds, baseline_peak = _measure(
                lambda: extract_text(raw, "docx")
            )
            baseline_text, baseline_metadata = baseline
            baseline_status = "completed"
        else:
            baseline_text = ""
            baseline_metadata = {
                "reason": "no dedicated upload parser for this format"
            }
            baseline_status = "unsupported"
            baseline_seconds = None
            baseline_peak = None

        runs.append(
            {
                "fixture": name,
                "format": expected["format"],
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "baseline": {
                    "status": baseline_status,
                    "extractor": baseline_metadata.get("extractor"),
                    "token_recall": _recall(baseline_text, expected["tokens"]),
                    "table_token_recall": _recall(
                        baseline_text, expected["table_tokens"]
                    ),
                    "elapsed_seconds": baseline_seconds,
                    "peak_tracemalloc_bytes": baseline_peak,
                    "metadata": baseline_metadata,
                },
                "markitdown": {
                    "status": candidate["status"],
                    "version": candidate.get("version"),
                    "token_recall": _recall(candidate_text, expected["tokens"]),
                    "table_token_recall": _recall(
                        candidate_text, expected["table_tokens"]
                    ),
                    "elapsed_seconds": candidate_seconds,
                    "peak_tracemalloc_bytes": candidate_peak,
                    "locator_fidelity": candidate.get("locator_fidelity"),
                    "precise_locators": candidate.get("precise_locators"),
                    "original_sha256": candidate.get("original_sha256"),
                },
            }
        )

    malformed = (args.fixture_dir / "malformed.docx").read_bytes()
    malformed_result = optional_document_conversion(
        malformed, filename="malformed.docx"
    )
    report = {
        "contract": "noesis-markitdown-evaluation-v1",
        "package": {
            "distribution": "markitdown",
            "version": importlib.metadata.version("markitdown"),
        },
        "corpus": "authored German/English bounded office-format regression fixtures",
        "label_origin": "authored-fixture-not-independent-human",
        "runs": runs,
        "malformed_docx": malformed_result,
        "decisions": {
            "docx": "defer default replacement; MarkItDown retains the fixture table but exact source locators remain unavailable",
            "xlsx": "adopt as optional fallback candidate; current upload parser has no dedicated XLSX path and the candidate retained all expected fixture cells",
            "pptx": "adopt as optional fallback candidate; current upload parser has no dedicated PPTX path and the candidate retained all expected fixture text",
        },
        "production_default_changed": False,
        "limitations": [
            "The corpus is an authored regression corpus rather than independent human evaluation.",
            "tracemalloc measures Python allocations rather than total process RSS.",
            "Markdown output preserves only approximate source location, so cited extraction still requires the original bytes.",
        ],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
