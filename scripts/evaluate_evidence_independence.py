#!/usr/bin/env python3
"""Run the offline Evidence Independence Graph evaluation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--development",
        type=Path,
        default=REPO_ROOT / "tests/fixtures/evidence_independence/development.json",
    )
    parser.add_argument(
        "--final-test",
        type=Path,
        default=REPO_ROOT / "tests/fixtures/evidence_independence/final.json",
    )
    args = parser.parse_args()
    from src.osint.independence_eval import evaluate_fixture_files

    report = evaluate_fixture_files(args.development, args.final_test)
    print(json.dumps(report, indent=2, sort_keys=True))
    return int(bool(report["final_test"]["false_independence_cases"]))


if __name__ == "__main__":
    raise SystemExit(main())
