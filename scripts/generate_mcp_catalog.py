#!/usr/bin/env python3
"""Generate or verify the machine-readable Noesis MCP capability artifact."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    from src.mcp_host.catalog import CATALOG_ARTIFACT, PACK_CONFIG, build_catalog_sync

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=CATALOG_ARTIFACT)
    args = parser.parse_args()
    pack_payload = json.loads(PACK_CONFIG.read_text(encoding="utf-8"))
    payload = build_catalog_sync(
        granted_scopes={"public", "knowledge:read", "operator"},
        enabled_pack_names=pack_payload.get("enabled_packs", []),
        configured_backends=set(),
        include_unusable=True,
    )
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.check:
        if not args.output.exists() or args.output.read_text() != rendered:
            print(f"stale MCP catalog artifact: {args.output}", file=sys.stderr)
            return 1
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
