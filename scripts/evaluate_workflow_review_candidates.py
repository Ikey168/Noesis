#!/usr/bin/env python3
"""Emit reproducible availability/evidence for workflow-review candidates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.evaluation.workflow_review import candidate_readiness, human_evaluation_status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--human-status", default="data/argument_mining/human_eval/status.json")
    parser.add_argument("--output")
    args = parser.parse_args()
    payload = {
        "contract": "noesis-workflow-review-candidate-status-v1",
        "human_evaluation": human_evaluation_status(args.human_status),
        "candidates": candidate_readiness(),
        "claims": {
            "live_benchmarks_run": False,
            "paid_provider_results": False,
            "independent_human_labels_collected_by_this_script": False,
        },
    }
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
