#!/usr/bin/env python3
"""Operate the prediction-blind Noesis human-evaluation workflow.

Examples::

    python scripts/human_annotation.py export --db data/noesis.duckdb --size 750
    python scripts/human_annotation.py analyze --a annotator_a.csv --b annotator_b.csv
    python scripts/human_annotation.py finalize --a annotator_a.csv --b annotator_b.csv \
        --adjudication adjudication.csv
    python scripts/human_annotation.py predict --gold human_gold.jsonl
    python scripts/human_annotation.py evaluate --gold human_gold.jsonl \
        --predictions active-model.csv

The program prepares and validates artifacts; it never generates human labels.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.argument_mining.human_eval import (
    DEFAULT_MIN_PER_SOURCE,
    HumanEvalError,
    analyze_assignments,
    export_assignments,
    finalize,
)
from src.argument_mining.human_eval_metrics import (
    evaluate_predictions,
    predict_gold,
)

DEFAULT_OUT = REPO / "data/argument_mining/human_eval"


def _manifest(args: argparse.Namespace) -> Path:
    if args.manifest:
        return args.manifest
    candidate = args.a.resolve().parent / "manifest.json"
    return candidate if candidate.is_file() else args.out / "manifest.json"


def _sample(args: argparse.Namespace) -> dict:
    minimum = args.minimum_per_source
    if minimum is None:
        minimum = (
            max(1, min(DEFAULT_MIN_PER_SOURCE, args.size // 6))
            if args.pilot
            else DEFAULT_MIN_PER_SOURCE
        )
    return export_assignments(
        args.db,
        args.out,
        args.size,
        args.seed,
        languages=args.languages.split(","),
        minimum_per_source=minimum,
        max_per_document=args.max_per_document,
        pilot=args.pilot,
    )


def _analyze(args: argparse.Namespace) -> dict:
    return analyze_assignments(
        args.a,
        args.b,
        manifest_path=_manifest(args),
        out=args.out,
        guideline_changes=args.guideline_change,
    )


def _finalize(args: argparse.Namespace) -> dict:
    return finalize(
        args.a,
        args.b,
        args.adjudication,
        args.out,
        manifest_path=_manifest(args),
    )


def _predict(args: argparse.Namespace) -> dict:
    output = args.output or args.gold.with_name("active-model.predictions.csv")
    return predict_gold(
        args.gold,
        output,
        model_id=args.model_id,
        claim_threshold=args.claim_threshold,
        stance_threshold=args.stance_threshold,
        frame_threshold=args.frame_threshold,
    )


def _evaluate(args: argparse.Namespace) -> dict:
    return evaluate_predictions(
        args.gold,
        args.predictions,
        args.out,
        baseline_path=args.baseline,
        experiment_path=args.experiment,
        minimum_slice_n=args.minimum_slice_n,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("export", "sample"):
        sample = sub.add_parser(
            name, help="create two blinded assignments from real documents"
        )
        sample.add_argument("--db", type=Path, required=True)
        sample.add_argument("--out", type=Path, default=DEFAULT_OUT)
        sample.add_argument("--size", type=int, default=750)
        sample.add_argument("--seed", type=int, default=2026)
        sample.add_argument("--languages", default="en")
        sample.add_argument("--minimum-per-source", type=int)
        sample.add_argument("--max-per-document", type=int, default=5)
        sample.add_argument(
            "--pilot",
            action="store_true",
            help="allow a 12-499 item protocol pilot; never marks collection complete",
        )
        sample.set_defaults(handler=_sample)

    analyze = sub.add_parser(
        "analyze", help="validate two assignments and report agreement"
    )
    analyze.add_argument("--a", type=Path, required=True)
    analyze.add_argument("--b", type=Path, required=True)
    analyze.add_argument("--manifest", type=Path)
    analyze.add_argument("--out", type=Path, default=DEFAULT_OUT)
    analyze.add_argument(
        "--guideline-change",
        action="append",
        default=[],
        help="record one pilot-driven protocol clarification; repeat as needed",
    )
    analyze.set_defaults(handler=_analyze)

    finish = sub.add_parser(
        "finalize", help="publish preserved gold after adjudication"
    )
    finish.add_argument("--a", type=Path, required=True)
    finish.add_argument("--b", type=Path, required=True)
    finish.add_argument("--adjudication", type=Path)
    finish.add_argument("--manifest", type=Path)
    finish.add_argument("--out", type=Path, default=DEFAULT_OUT)
    finish.set_defaults(handler=_finalize)

    predict = sub.add_parser(
        "predict", help="run active pinned models on the untouched test split"
    )
    predict.add_argument("--gold", type=Path, required=True)
    predict.add_argument("--output", type=Path)
    predict.add_argument("--model-id")
    predict.add_argument("--claim-threshold", type=float, default=0.0)
    predict.add_argument("--stance-threshold", type=float, default=0.0)
    predict.add_argument("--frame-threshold", type=float, default=0.0)
    predict.set_defaults(handler=_predict)

    evaluate = sub.add_parser(
        "evaluate", help="publish metrics, calibration, abstention, and slices"
    )
    evaluate.add_argument("--gold", type=Path, required=True)
    evaluate.add_argument("--predictions", type=Path, required=True)
    evaluate.add_argument("--baseline", type=Path)
    evaluate.add_argument(
        "--experiment",
        type=Path,
        help="JSON record of label, threshold, calibration, or training choices",
    )
    evaluate.add_argument("--out", type=Path, default=DEFAULT_OUT / "reports")
    evaluate.add_argument("--minimum-slice-n", type=int, default=10)
    evaluate.set_defaults(handler=_evaluate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args.handler(args)
    except (HumanEvalError, OSError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
