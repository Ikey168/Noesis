#!/usr/bin/env python3
"""Generate or verify the migrated bundles' provider descriptors and overlays (C09.2-C09.4).

The ownership table in src/composition/bundles.py is the source; the
descriptors under config/composition/providers/ and the composition.json
overlays it renders are committed. ``--check`` fails when any is stale.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    from src.composition import bundles

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        stale = bundles.stale()
        for path in stale:
            print(f"stale generated composition file: {path}", file=sys.stderr)
        return 1 if stale else 0
    for path in bundles.write():
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
