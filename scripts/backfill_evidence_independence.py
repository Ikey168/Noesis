#!/usr/bin/env python3
"""Run one resumable Evidence Independence Graph backfill batch."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--threshold", type=float, default=0.78)
    args = parser.parse_args()
    import duckdb

    from src.osint.independence import run_origin_backfill

    conn = duckdb.connect(str(args.db_path))
    try:
        result = run_origin_backfill(
            conn,
            batch_size=args.batch_size,
            near_duplicate_threshold=args.threshold,
        )
    finally:
        conn.close()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
