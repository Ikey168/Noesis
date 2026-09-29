"""Evaluate frozen typed-decision predictions without making provider calls."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from src.evaluation.typed_decisions import (
        evaluate_acceptance_policy,
        fit_acceptance_policy,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("validation", type=Path)
    parser.add_argument("test", type=Path)
    parser.add_argument("--task", required=True)
    parser.add_argument("--labels", nargs="+", required=True)
    parser.add_argument("--model-version", required=True)
    parser.add_argument("--rubric-version", required=True)
    parser.add_argument("--minimum-coverage", type=float, default=0.8)
    parser.add_argument("--release-criteria", type=Path, required=True)
    parser.add_argument("--coverage-requirements", type=Path, required=True)
    parser.add_argument("--input-usd-per-million", type=float, default=0.042)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    validation = json.loads(args.validation.read_text())
    test = json.loads(args.test.read_text())
    policy = fit_acceptance_policy(
        validation,
        labels=args.labels,
        task=args.task,
        model_version=args.model_version,
        rubric_version=args.rubric_version,
        minimum_coverage=args.minimum_coverage,
        release_criteria=json.loads(args.release_criteria.read_text()),
        coverage_requirements=json.loads(args.coverage_requirements.read_text()),
    )
    report = {
        "policy": policy,
        "held_out": evaluate_acceptance_policy(
            test, policy, input_usd_per_million=args.input_usd_per_million
        ),
    }
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
