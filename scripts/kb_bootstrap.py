#!/usr/bin/env python3
"""
One-command knowledge-domain bootstrap.

Seeds the feed subscriptions from ``config/domains.yml``, harvests the
subscribed feeds into the unified documents sink, runs the membership pass,
builds the per-domain views, and prints a coverage summary per domain.

Usage:
    python3 scripts/kb_bootstrap.py                 # seed + harvest + assign
    python3 scripts/kb_bootstrap.py --no-harvest    # offline: seed + assign only
    python3 scripts/kb_bootstrap.py --limit 10      # cap entries per feed
    python3 scripts/kb_bootstrap.py --embeddings    # also use the embedding
                                                    # method (env-configured
                                                    # provider; downloads a
                                                    # model on first use)

Harvest requires network access; failures per feed are reported and skipped,
never fatal — the command is safe to re-run any time (seeding, harvest, and
membership are all idempotent).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the knowledge domains")
    parser.add_argument("--no-harvest", action="store_true",
                        help="Skip feed harvesting (offline mode)")
    parser.add_argument("--limit", type=int, default=20,
                        help="Max entries per feed (default 20)")
    parser.add_argument("--full-text", action="store_true",
                        help="Fetch full article bodies (slower)")
    parser.add_argument("--embeddings", action="store_true",
                        help="Enable the embedding membership method via the "
                             "env-configured provider")
    args = parser.parse_args()

    from src.database.local_analytics_connector import get_shared_connection
    from src.ingestion.document_store import DocumentStore
    from src.kb import load_registry
    from src.kb.membership import ensure_domain_views, run_membership_pass
    from src.kb.seeding import seed_domain_feeds

    registry = load_registry()
    print(f"Domains: {', '.join(registry.names())}")

    seeded = seed_domain_feeds(registry)
    print(
        f"Feeds: {len(seeded['added'])} added, {len(seeded['retagged'])} retagged,"
        f" {len(seeded['unchanged'])} unchanged"
    )

    conn = get_shared_connection()

    if not args.no_harvest:
        from src.ingestion.connectors.blog.connector import BlogConnector

        connector = BlogConnector(
            fetch_full_text=args.full_text, limit_per_feed=args.limit
        )
        summary = connector.harvest_run(store=DocumentStore(conn))
        print(
            f"Harvest: {summary.discovered} feeds discovered,"
            f" {summary.documents} entries parsed, {summary.inserted} stored,"
            f" {summary.duplicate} duplicates, {summary.fetch_errors} fetch errors"
        )
        for source_id, per_source in sorted(summary.per_source.items()):
            if per_source.get("fetch_errors") or per_source.get("parse_errors"):
                print(
                    f"  [!] {source_id}: fetch_errors="
                    f"{per_source.get('fetch_errors', 0)} parse_errors="
                    f"{per_source.get('parse_errors', 0)}"
                )

    provider = None
    if args.embeddings:
        from services.embeddings.provider import get_embedding_provider

        provider = get_embedding_provider()
        # Fill missing document vectors first so membership can use them.
        from src.ingestion.embed import embed_documents

        embedded = embed_documents(conn, provider=provider)
        print(f"Embeddings: {embedded} documents embedded")

    membership = run_membership_pass(conn, registry, provider=provider)
    ensure_domain_views(conn, registry)

    print("\nCoverage:")
    for name in registry.names():
        definition = registry.get(name)
        if definition.backing != "corpus-view":
            continue
        coverage = registry.resolve(name, conn=conn).coverage()
        counts = membership["domains"].get(name, {})
        methods = coverage.get("assignment_methods") or {}
        method_text = (
            ", ".join(f"{key}={value}" for key, value in sorted(methods.items()))
            or "none"
        )
        print(
            f"  {name:<12} documents={coverage['documents']:<5}"
            f" scanned_now={counts.get('scanned', 0):<5} methods: {method_text}"
        )

    print("\nDone. Re-run any time; every step is idempotent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
