"""Reproduce the optional Pint conversion evaluation; no network or model labels."""

import argparse
import json
import platform
import resource
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import duckdb

from src.kb.pint_evaluation import PIN, PintUnitEvaluation
from src.kb.quantitative import QuantitativeStore

CASES = [
    ("Berlin distance", "1.2345675", "km", "m", "1234.567500"),
    ("Freezing water", "0", "C", "K", "273.150000"),
    ("Kelvin offset", "273.15", "K", "C", "0.000000"),
    ("Survey share", "12.5", "percent", "ratio", "0.125000"),
    ("Duration", "0.333333333333", "h", "s", "1200.000000"),
    ("Mass", "1000", "g", "kg", "1.000000"),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--repeats", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 100:
        parser.error("repeats must be 1..100")
    conn = duckdb.connect()
    try:
        store = QuantitativeStore(conn)
        candidate = PintUnitEvaluation(store)
        auth = {"principal_id": "evaluation", "scopes": {"operator"}}
        results = []
        for label, backend in [("native", store), ("pint", candidate)]:
            elapsed = []
            checks = []
            for iteration in range(args.repeats):
                for name, value, source, target, expected in CASES:
                    start = time.perf_counter()
                    receipt = backend.convert(
                        "evaluation", value, source, target, **auth
                    )
                    elapsed.append((time.perf_counter() - start) * 1000)
                    if iteration == 0:
                        checks.append(
                            {
                                "case": name,
                                "expected": expected,
                                "actual": receipt["result"]["value"],
                                "passed": expected == receipt["result"]["value"],
                            }
                        )
            results.append(
                {
                    "backend": label,
                    "checks": checks,
                    "first_call_ms": elapsed[0],
                    "median_warm_ms": statistics.median(
                        elapsed[len(CASES) :] or elapsed
                    ),
                    "calls": len(elapsed),
                }
            )
        report = {
            "schema": "noesis-pint-evaluation-v1",
            "python": platform.python_version(),
            "pint": PIN,
            "fixture_kind": "synthetic independently specified arithmetic references; not human quality labels",
            "results": results,
            "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "decision": "defer default adoption: exact conversion parity on six cases; added dependency and cold-start cost; optional compound-dimension utility only",
        }
        output = json.dumps(report, indent=2) + "\n"
        if args.out:
            args.out.write_text(output)
        else:
            print(output, end="")
        if not all(c["passed"] for r in results for c in r["checks"]):
            raise SystemExit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
