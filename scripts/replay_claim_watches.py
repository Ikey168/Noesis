#!/usr/bin/env python3
"""Replay retained Claim Watch transitions and compare stored logical events."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify deterministic Claim Watch replay for a watermark range."
    )
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--principal-id", required=True)
    parser.add_argument("--watch-id", required=True)
    parser.add_argument("--from-watermark", type=int, required=True)
    parser.add_argument("--to-watermark", type=int, required=True)
    args = parser.parse_args()

    import duckdb

    from src.kb.watches import WatchError, replay_watch

    try:
        conn = duckdb.connect(str(args.db_path), read_only=False)
        result = replay_watch(
            conn,
            args.principal_id,
            args.watch_id,
            from_watermark=args.from_watermark,
            to_watermark=args.to_watermark,
        )
    except WatchError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": str(exc)}}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["matches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
