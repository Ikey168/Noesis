#!/usr/bin/env python3
"""Run the offline Context Assembly regression evaluation."""

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
        "--fixture",
        type=Path,
        default=REPO_ROOT / "tests/fixtures/context_assembly/regression.json",
    )
    args = parser.parse_args()
    from src.kb.context_eval import evaluate_fixture

    report = evaluate_fixture(args.fixture)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
