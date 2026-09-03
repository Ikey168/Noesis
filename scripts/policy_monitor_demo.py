#!/usr/bin/env python3
"""Run the complete fictional policy-monitor workflow without network access."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.policy_monitor import run_demo


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Provision, answer, watch, export, and verify the offline policy scenario."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / "artifacts" / "policy-monitor",
        help="artifact directory (default: artifacts/policy-monitor)",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="DuckDB path (default: <output>/policy-monitor.duckdb)",
    )
    parser.add_argument("--fixture", type=Path, help="alternate CC0 fixture manifest")
    parser.add_argument("--domains", type=Path, help="alternate domain registry")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output.resolve()
    kwargs = {"db_path": (args.database or output / "policy-monitor.duckdb").resolve()}
    if args.fixture is not None:
        kwargs["fixture_path"] = args.fixture.resolve()
    if args.domains is not None:
        kwargs["domains_path"] = args.domains.resolve()
    result = run_demo(output, **kwargs)
    summary = {
        "contract": result["contract"],
        "scenario_id": result["scenario_id"],
        "public_metrics": result["public"]["metrics"],
        "private_guidance_status": result["authorized"]["private_guidance"]["status"],
        "watch_events": [
            event["event_type"] for event in result["watch"]["poll"]["events"]
        ],
        "replay_matches": result["watch"]["replay"]["matches"],
        "bundle_status": result["bundle"]["verification"]["status"],
        "output": str(output),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["bundle_status"] == "valid" and summary["replay_matches"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
