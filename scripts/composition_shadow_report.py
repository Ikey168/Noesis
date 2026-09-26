#!/usr/bin/env python3
"""Generate or verify the catalog shadow-mode diff report for pack composition (C04.4).

The report lists, per bundle, every tool where the legacy catalog and the
composition disagree on pack attribution, required data or state. Each
disagreement must carry an annotation in shadow_annotations.json before a
bundle's lifecycle is cut over.
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
    from src.composition.deployment import (
        SHADOW_REPORT,
        load_annotations,
        shadow_report,
        unannotated,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=SHADOW_REPORT)
    args = parser.parse_args()
    report = shadow_report()
    rendered = json.dumps(report, indent=1, sort_keys=True) + "\n"
    missing = unannotated(report, load_annotations())
    if args.check:
        stale = not args.output.exists() or args.output.read_text() != rendered
        if stale:
            print(f"stale shadow report: {args.output}", file=sys.stderr)
        for key in missing:
            print(f"unannotated disagreement: {key}", file=sys.stderr)
        return 1 if stale or missing else 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered)
    print(f"{args.output}: {report['disagreements']} disagreements, {len(missing)} unannotated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
