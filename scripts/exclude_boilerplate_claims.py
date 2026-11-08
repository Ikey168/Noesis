#!/usr/bin/env python3
"""
Mark boilerplate claims excluded (reversibly) and withhold their links.

Excludes navigation, marketing and share-button text, bylines, fragments too
short to state anything, and wording repeated across documents of the same
site. Claims are never deleted; links touching them are moved to the
reversible withheld store. Without --apply this only prints what would change.

Usage:
    python3 scripts/exclude_boilerplate_claims.py
    python3 scripts/exclude_boilerplate_claims.py --apply --min-site-documents 3
    python3 scripts/exclude_boilerplate_claims.py --undo kb-claim-exclusions-abc123
    python3 scripts/exclude_boilerplate_claims.py --restore CLAIM_ID [CLAIM_ID ...]
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
    parser.add_argument("--apply", action="store_true", help="Make the changes (default: plan only)")
    parser.add_argument("--min-site-documents", type=int, default=3,
                        help="Repeated wording counts as boilerplate from this many documents of one site")
    parser.add_argument("--keep-links", action="store_true",
                        help="Exclude claims but leave their existing links in place")
    parser.add_argument("--undo", metavar="RUN_ID", help="Revert an exclusion pass")
    parser.add_argument("--restore", nargs="+", metavar="CLAIM_ID",
                        help="Reinstate specific claims and keep them from being excluded again")
    parser.add_argument("--note", default=None, help="Why restored claims are kept")
    args = parser.parse_args()

    from src.database.local_analytics_connector import get_shared_connection
    from src.kb.claim_exclusions import (
        restore_claims,
        run_claim_exclusion_pass,
        undo_claim_exclusions,
    )

    conn = get_shared_connection()
    if args.undo:
        result = undo_claim_exclusions(conn, args.undo)
    elif args.restore:
        result = restore_claims(conn, args.restore, note=args.note)
    else:
        result = run_claim_exclusion_pass(
            conn, min_site_documents=args.min_site_documents,
            withhold_links=not args.keep_links, dry_run=not args.apply,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
