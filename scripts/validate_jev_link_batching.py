#!/usr/bin/env python3
"""
Compare per-link and batched Jev claim-link review on already planned links.

Samples stored ``claim_links`` rows (supports/duplicate/contradicts), reviews
the same links one request per link and at each batch size, and reports
agreement by relation and verdict, request latency, and token use. Nothing
is written to ``claim_links`` or ``claim_link_jev_assessments``, and nothing
turns batching on: ``recommended`` is advice for enabling ``--jev-batch``.

This sends claim spans to TypeSafe and spends from the policy's budget.

Usage:
    python3 scripts/validate_jev_link_batching.py --policy policy.json
    python3 scripts/validate_jev_link_batching.py --policy policy.json \\
        --sample 480 --batch-sizes 25 120 --out batching-report.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("--policy", type=Path, required=True,
                        help="JSON hosted-decision policy (model, rubric, budget, credential_ref)")
    parser.add_argument("--namespace", default="kb-batch-validation",
                        help="Decision namespace for the validation runs")
    parser.add_argument("--sample", type=int, default=240,
                        help="Planned links to review (default 240)")
    parser.add_argument("--seed", type=int, default=0, help="Sampling seed")
    parser.add_argument("--batch-sizes", type=int, nargs="+", default=[25, 120],
                        help="Max links per batched request to compare (default 25 120)")
    parser.add_argument("--min-agreement", type=float, default=0.95,
                        help="Per-relation verdict agreement required to recommend (default 0.95)")
    parser.add_argument("--min-links-per-relation", type=int, default=20,
                        help="Answered links per relation required to recommend (default 20)")
    parser.add_argument("--out", type=Path, default=None, help="Write the JSON report here")
    args = parser.parse_args()

    from src.database.local_analytics_connector import get_shared_connection
    from src.evaluation.jev_link_batching import run_validation, sample_planned_links
    from src.kb.claim_link_review import build_jev_reviewer

    conn = get_shared_connection()
    policy = json.loads(args.policy.read_text())
    items = sample_planned_links(conn, sample=args.sample, seed=args.seed)
    if not items:
        parser.error("no stored supports/duplicate/contradicts links to sample")

    def make_reviewer(batch):
        return build_jev_reviewer(conn, args.namespace, mode="shadow", policy=policy, batch=batch)

    report = run_validation(
        items, make_reviewer, batch_sizes=args.batch_sizes,
        min_agreement=args.min_agreement, min_links_per_relation=args.min_links_per_relation,
    )
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    per_link = report["per_link"]
    print(
        f"per-link: {per_link['requests']} requests, {per_link['wall_s']}s,"
        f" {per_link['input_tokens']} input tokens",
        file=sys.stderr,
    )
    for name, result in report["batched"].items():
        agreement = result["agreement"]
        print(
            f"{name}: {result['usage']['requests']} requests, {result['usage']['wall_s']}s,"
            f" {result['usage']['input_tokens']} input tokens; verdict agreement"
            f" {agreement['verdict_agreement']}, outcome agreement {agreement['outcome_agreement']};"
            f" recommended={result['recommended']}",
            file=sys.stderr,
        )
        for reason in result["not_recommended_because"]:
            print(f"  - {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
