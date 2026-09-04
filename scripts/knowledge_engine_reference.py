#!/usr/bin/env python3
"""Run the deterministic Knowledge Engine 1.0 reference workflow."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.kb.workflows import (  # noqa: E402
    WorkflowError,
    WorkflowStore,
    reference_handlers,
    reference_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="DuckDB path; defaults to an isolated in-memory run")
    parser.add_argument("--namespace", default="reference")
    parser.add_argument("--run-key", default="offline-v1")
    parser.add_argument(
        "--exercise-recovery",
        action="store_true",
        help="interrupt after extraction and resume from durable receipts",
    )
    args = parser.parse_args()
    fixture = json.loads(
        (ROOT / "tests/fixtures/knowledge_engine_reference/corpus.json").read_text()
    )
    conn = duckdb.connect(args.database or ":memory:")
    store = WorkflowStore(conn)
    manifest = reference_manifest(args.namespace)
    handlers = reference_handlers(conn)
    initial = {"documents": fixture["documents"], "fixture_clock_ms": fixture["clock_ms"]}
    recovered = False
    if args.exercise_recovery:
        try:
            store.execute(
                manifest,
                handlers,
                initial,
                run_key=args.run_key,
                fail_after=2,
                now_ms=fixture["clock_ms"],
            )
        except WorkflowError as exc:
            if exc.code != "injected_failure":
                raise
            recovered = True
    result = store.execute(
        manifest,
        handlers,
        initial,
        run_key=args.run_key,
        now_ms=fixture["clock_ms"],
    )
    report = dict(result["state"]["report"])
    report.update(
        {
            "status": result["status"],
            "manifest_hash": result["manifest_hash"],
            "receipt_count": len(result["receipts"]),
            "recovered": recovered,
            "coverage": result["watermark"]["coverage"],
        }
    )
    print(json.dumps(report, sort_keys=True, indent=2))
    conn.close()
    return 0 if report["verified"] and report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
