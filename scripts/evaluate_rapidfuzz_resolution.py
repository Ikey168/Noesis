#!/usr/bin/env python3
"""Run the frozen optional RapidFuzz/SequenceMatcher entity-name benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.evaluation.fuzzy_resolution import benchmark_fuzzy_resolution


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/workflow_review/fuzzy_names_split.json"),
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    report = (
        benchmark_fuzzy_resolution(fixture["development"], test_cases=fixture["test"])
        if isinstance(fixture, dict)
        else benchmark_fuzzy_resolution(fixture)
    )
    report["label_origin"] = (
        fixture.get("label_origin", "unspecified")
        if isinstance(fixture, dict)
        else "authored-regression"
    )
    rendered = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
