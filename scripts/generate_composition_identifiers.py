#!/usr/bin/env python3
"""Generate or verify the preserved-identifier fixture for pack composition (C01.4)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    from src.composition.identifiers import (
        FIXTURE,
        build_reference_catalog,
        preserved_identifiers,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=FIXTURE)
    args = parser.parse_args()
    rendered = (
        json.dumps(preserved_identifiers(build_reference_catalog()), indent=1, sort_keys=True)
        + "\n"
    )
    if args.check:
        if not args.output.exists() or args.output.read_text() != rendered:
            print(f"stale preserved-identifier fixture: {args.output}", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
