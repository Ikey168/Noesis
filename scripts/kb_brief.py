#!/usr/bin/env python3
"""
Render the daily brief (a thin consumer of the KB contract, #960).

Usage:
    python3 scripts/kb_brief.py                       # all domains, last 24h
    python3 scripts/kb_brief.py --domains papers      # research-only brief
    python3 scripts/kb_brief.py --since 2026-07-20 --budget 10
    python3 scripts/kb_brief.py --out brief.md        # write instead of print
    python3 scripts/kb_brief.py --harvest             # pull feeds + assign first
    python3 scripts/kb_brief.py --harvest --jev-links shadow --jev-policy policy.json

Pair with the harvest for a one-command daily run:
    make kb-brief
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the Noesis daily brief")
    parser.add_argument("--domains", nargs="*", default=None,
                        help="Domains to include (default: all configured)")
    parser.add_argument("--since", default=None,
                        help="ISO-8601 UTC floor (default: 24h ago)")
    parser.add_argument("--budget", type=int, default=15,
                        help="Max ranked items across all domains (default 15)")
    parser.add_argument("--out", type=Path, default=None,
                        help="Write markdown to a file instead of stdout")
    parser.add_argument("--harvest", action="store_true",
                        help="Harvest feeds + run membership/consolidation first")
    parser.add_argument("--jev-links", choices=("off", "shadow", "gate"), default="off",
                        help="Review proposed claim links with Jev during --harvest:"
                             " shadow records assessments, gate also withholds links"
                             " Jev answers against (default off)")
    parser.add_argument("--jev-policy", type=Path, default=None,
                        help="JSON hosted-decision policy pinning model, rubric,"
                             " budget and credential_ref (required with --jev-links)")
    parser.add_argument("--jev-batch", action="store_true",
                        help="Pack many links per Jev request (default: one per link)."
                             " Validate first with scripts/validate_jev_link_batching.py")
    parser.add_argument("--jev-batch-state-tokens", type=int, default=24_000,
                        help="Estimated state tokens per batched request (default 24000)")
    parser.add_argument("--jev-batch-max-links", type=int, default=120,
                        help="Max links per batched request (default 120)")
    parser.add_argument("--jev-batch-concurrency", type=int, default=1,
                        help="Batched requests in flight, up to the policy's"
                             " max_concurrent (default 1)")
    parser.add_argument("--jev-batch-relations", nargs="+", default=["duplicate"],
                        choices=["duplicate", "supports", "contradicts", "supersedes"],
                        help="Relations packed into batches (default: duplicate only; the"
                             " rest stay per-link until their validation gate passes)")
    parser.add_argument("--jev-tie-break", choices=["abstain", "label", "probabilities"],
                        default="abstain",
                        help="How to settle a Jev label that disagrees with its probabilities")
    parser.add_argument("--jev-same-subject-gate", type=float, default=None,
                        help="Withhold confirmed conflicts whose same-subject probability is below this")
    parser.add_argument("--jev-namespace", default="kb",
                        help="Decision namespace for Jev link reviews (default kb)")
    parser.add_argument("--email", action="store_true",
                        help="Email the result to NOESIS_DAILY_BRIEF_TO")
    args = parser.parse_args()
    if args.jev_links != "off" and (not args.harvest or args.jev_policy is None):
        parser.error("--jev-links needs --harvest and --jev-policy")
    if args.jev_batch and args.jev_links == "off":
        parser.error("--jev-batch needs --jev-links shadow|gate")

    from src.database.local_analytics_connector import get_shared_connection

    conn = get_shared_connection()

    if args.harvest:
        from src.ingestion.connectors.blog.connector import BlogConnector
        from src.ingestion.document_store import DocumentStore
        from src.kb.claim_links import run_claim_linking_pass
        from src.kb.clusters import run_clustering_pass
        from src.kb.entities import run_entity_canonicalization_pass
        from src.kb.membership import run_membership_pass

        summary = BlogConnector(fetch_full_text=False, limit_per_feed=20).harvest_run(
            store=DocumentStore(conn)
        )
        print(
            f"harvest: {summary.inserted} new, {summary.duplicate} duplicates,"
            f" {summary.fetch_errors} fetch errors",
            file=sys.stderr,
        )
        run_membership_pass(conn)
        from src.kb.claim_exclusions import run_claim_exclusion_pass

        # Boilerplate claims are excluded (reversibly) before linking, so
        # they cost neither NLI calls nor Jev questions.
        exclusions = run_claim_exclusion_pass(conn)
        print(f"claim exclusions: {exclusions['claims_excluded']} new,"
              f" {exclusions['links_withheld']} links withheld", file=sys.stderr)
        jev_reviewer = None
        if args.jev_links != "off":
            import json

            from src.kb.claim_link_review import build_jev_reviewer

            batch = None
            if args.jev_batch:
                from src.kb.claim_link_batch import BatchConfig

                batch = BatchConfig(
                    state_token_budget=args.jev_batch_state_tokens,
                    max_links=args.jev_batch_max_links,
                    concurrency=args.jev_batch_concurrency,
                    relations=tuple(args.jev_batch_relations),
                )
            jev_reviewer = build_jev_reviewer(
                conn, args.jev_namespace, mode=args.jev_links,
                policy=json.loads(args.jev_policy.read_text()), batch=batch,
                tie_break=args.jev_tie_break, same_subject_gate=args.jev_same_subject_gate,
            )
        links = run_claim_linking_pass(conn, jev_reviewer=jev_reviewer)
        if "jev" in links:
            print(f"claim links jev review: {links['jev']}", file=sys.stderr)
        run_clustering_pass(conn)
        run_entity_canonicalization_pass(conn)

    from src.kb.brief import generate_brief

    brief = generate_brief(
        domains=args.domains, since=args.since, budget=args.budget, conn=conn
    )
    if args.out:
        args.out.write_text(brief["markdown"])
        print(f"wrote {args.out} ({brief['meta']['kept']} items,"
              f" {brief['meta']['dropped']} dropped)", file=sys.stderr)
    else:
        print(brief["markdown"])
    if args.email:
        import os
        from datetime import datetime, timezone
        from src.reports.email_sender import send_markdown_email

        recipient = os.getenv("NOESIS_DAILY_BRIEF_TO", "").strip()
        if not recipient:
            parser.error("--email requires NOESIS_DAILY_BRIEF_TO")
        send_markdown_email(
            to=recipient,
            subject=f"[Noesis] Daily cross-domain brief — {datetime.now(timezone.utc):%Y-%m-%d}",
            markdown=brief["markdown"],
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
