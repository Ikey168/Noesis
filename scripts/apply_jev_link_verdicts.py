#!/usr/bin/env python3
"""
Apply a claim-linking run's stored Jev verdicts (no new Jev requests).

Withholds the links Jev rejected and reverses the supports links whose
direction Jev reversed, exactly as gate mode would have. Links are moved to a
reversible withheld store, never deleted. Without --apply this only prints
the plan.

Usage:
    python3 scripts/apply_jev_link_verdicts.py --run-id kb-claim-links-abc123
    python3 scripts/apply_jev_link_verdicts.py --run-id kb-claim-links-abc123 --apply \\
        --keep CLAIM_A CLAIM_B supports --note "sentence plus hashes"
    python3 scripts/apply_jev_link_verdicts.py --undo jev-verdicts:kb-claim-links-abc123:1762600000000
    python3 scripts/apply_jev_link_verdicts.py --keep-only --keep CLAIM_A CLAIM_B contradicts
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
    parser.add_argument("--run-id", help="Linking run whose stored verdicts to apply")
    parser.add_argument("--apply", action="store_true", help="Make the changes (default: plan only)")
    parser.add_argument("--keep", nargs=3, action="append", default=[],
                        metavar=("CLAIM_A", "CLAIM_B", "RELATION"),
                        help="Override Jev for a link as stored (repeatable)")
    parser.add_argument("--note", default=None, help="Why the kept links override Jev")
    parser.add_argument("--keep-only", action="store_true",
                        help="Only record --keep overrides and restore those links")
    parser.add_argument("--undo", metavar="OP_ID", help="Revert an applied operation")
    args = parser.parse_args()

    from src.database.local_analytics_connector import get_shared_connection
    from src.kb.jev_verdicts import apply_jev_verdicts, keep_links, undo_jev_verdicts

    conn = get_shared_connection()
    if args.undo:
        result = undo_jev_verdicts(conn, args.undo)
    elif args.keep_only:
        if not args.keep:
            parser.error("--keep-only needs at least one --keep")
        result = keep_links(conn, [tuple(k) for k in args.keep], note=args.note)
    else:
        if not args.run_id:
            parser.error("--run-id is required")
        result = apply_jev_verdicts(
            conn, args.run_id, apply=args.apply, keep=[tuple(k) for k in args.keep], note=args.note,
        )
    print(json.dumps(result, indent=2, sort_keys=True, default=list))
    return 0


if __name__ == "__main__":
    sys.exit(main())
