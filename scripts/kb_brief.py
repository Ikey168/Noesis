#!/usr/bin/env python3
"""
Render the daily brief (a thin consumer of the KB contract, #960).

Usage:
    python3 scripts/kb_brief.py                       # all domains, last 24h
    python3 scripts/kb_brief.py --domains papers      # research-only brief
    python3 scripts/kb_brief.py --since 2026-07-20 --budget 10
    python3 scripts/kb_brief.py --out brief.md        # write instead of print
    python3 scripts/kb_brief.py --harvest             # pull feeds + assign first

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
    args = parser.parse_args()

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
        run_claim_linking_pass(conn)
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
