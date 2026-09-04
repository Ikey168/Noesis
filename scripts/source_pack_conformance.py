#!/usr/bin/env python3
"""Validate built-in source packs offline and optionally probe live endpoints."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ingestion.source_packs import SourcePackConformance  # noqa: E402


def _probe(source):
    request = urllib.request.Request(
        source["endpoint"],
        headers={"User-Agent": "Noesis source-pack conformance/1.0", "Range": "bytes=0-1023"},
    )
    with urllib.request.urlopen(request, timeout=min(10, source["budgets"]["timeout_ms"] / 1000)) as response:  # noqa: S310 - validated public HTTPS source-pack endpoint
        return {
            "schema_hash": response.headers.get("ETag"),
            "quota_remaining": response.headers.get("X-RateLimit-Remaining"),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="make explicitly bounded network probes")
    parser.add_argument("--max-requests", type=int, default=10)
    args = parser.parse_args()
    gate = SourcePackConformance(ROOT)
    reports = []
    for path in sorted((ROOT / "config/source_packs").glob("*.json")):
        manifest = json.loads(path.read_text())
        offline = gate.offline(manifest)
        report = {"offline": offline}
        if args.live:
            report["live"] = gate.live(manifest, _probe, enabled=True, max_requests=args.max_requests)
        reports.append(report)
    result = {
        "contract": "noesis-source-pack-suite-v1",
        "offline": True,
        "valid": all(report["offline"]["valid"] for report in reports),
        "packs": reports,
    }
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
