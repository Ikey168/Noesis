#!/usr/bin/env python3
"""Migrate a warehouse's bundles to composition management (C09.2, C09.3).

Installs the shipped manifests and provider descriptors, selects the bundles
the legacy registry has enabled, activates one generation, cuts every bundle
over to the coordinator and verifies source-pack pins, cursors and runs are
unchanged. Run it once per deployment; it is idempotent.

    python scripts/migrate_composition.py --db data/noesis.duckdb
    python scripts/migrate_composition.py --db data/noesis.duckdb --bundles legal market
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    import duckdb

    from src.composition.migration import migrate
    from src.domains import registry

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", required=True, help="DuckDB warehouse path")
    parser.add_argument("--bundles", nargs="*", help="bundles to migrate (default: every shipped bundle)")
    parser.add_argument("--principal", default="operator")
    args = parser.parse_args()
    registry.load_config()
    conn = duckdb.connect(args.db)
    try:
        result = migrate(conn, principal_id=args.principal, bundles=args.bundles)
    finally:
        conn.close()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["activation"] in {"applied", None} else 1


if __name__ == "__main__":
    sys.exit(main())
