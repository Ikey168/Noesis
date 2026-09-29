#!/usr/bin/env python3
"""Generate or verify the catalog shadow-mode diff for the registered bundles (C04.4)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    from src.composition.shadow import SHADOW_REPORT, render, shadow_report
    from src.mcp_host.catalog import PACK_CONFIG

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=SHADOW_REPORT)
    args = parser.parse_args()
    enabled = json.loads(PACK_CONFIG.read_text()).get("enabled_packs", [])
    rendered = render(asyncio.run(shadow_report(
        granted_scopes={"public", "knowledge:read", "operator"}, enabled_pack_names=enabled,
        configured_backends=set())))
    if args.check:
        if not args.output.exists() or args.output.read_text() != rendered:
            print(f"stale composition shadow report: {args.output}", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
