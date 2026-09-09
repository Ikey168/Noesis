"""Compare actual native/Pint calculations on authored exact-arithmetic cases."""

import argparse
import json
import resource
import statistics
import time
from pathlib import Path

import duckdb

from src.integrations.common import version
from src.kb.quantitative import CALCULATE_SCOPE, WRITE_SCOPE, QuantitativeStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    store = QuantitativeStore(duckdb.connect(), now=lambda: 100)
    options = {"principal_id": "fixture", "scopes": {CALCULATE_SCOPE, WRITE_SCOPE}}
    store.register_unit("berlin", "m/s", {"length": 1, "time": -1}, **options)
    cases = []
    for value, source, target, expected in [
        ("1.2", "kilometre", "m", "1200.000000"),
        ("0", "celsius", "K", "273.150000"),
        ("273.15", "K", "C", "0.000000"),
        ("25", "%", "fraction", "0.250000"),
        ("1.2345665", "m", "m", "1.234566"),
    ]:
        cases.append(
            {
                "kind": "conversion",
                "value": value,
                "source": source,
                "target": target,
                "expected": {"native": expected, "pint": expected},
            }
        )
    for expression, target, inputs, native, candidate in [
        (
            "a / b",
            "m/s",
            {"a": ("100", "m", {"length": 1}), "b": ("10", "s", {"time": 1})},
            "10.000000",
            "10.000000",
        ),
        (
            "(a + b) / 2",
            "K",
            {
                "a": ("300", "K", {"temperature": 1}),
                "b": ("302", "K", {"temperature": 1}),
            },
            "301.000000",
            "301.000000",
        ),
        (
            "a + b",
            "m",
            {"a": ("1", "km", {"length": 1}), "b": ("500", "m", {"length": 1})},
            "501.000000",
            "1500.000000",
        ),
        (
            "a * b",
            "m",
            {"a": ("25", "%", {}), "b": ("200", "m", {"length": 1})},
            "5000.000000",
            "50.000000",
        ),
    ]:
        metric = store.register_metric(
            "berlin",
            canonical_name="Authored " + expression,
            definition="Exact-arithmetic fixture; not empirical Berlin measurements",
            unit=target,
            frequency="instant",
            population={"geography": "Berlin", "fixture": True},
            formula={
                "expression": expression,
                "input_dimensions": {k: v[2] for k, v in inputs.items()},
            },
            **options,
        )
        cases.append(
            {
                "kind": "formula",
                "metric_id": metric["metric_id"],
                "expression": expression,
                "inputs": {
                    k: {"value": v[0], "unit_id": v[1], "dimension": v[2]}
                    for k, v in inputs.items()
                },
                "expected": {"native": native, "pint": candidate},
            }
        )
    for case in cases:
        case["backends"] = {}
        for backend in ("native", "pint"):
            timings = []
            for _ in range(20):
                started = time.perf_counter()
                if case["kind"] == "conversion":
                    result = store.convert(
                        "berlin",
                        case["value"],
                        case["source"],
                        case["target"],
                        backend=backend,
                        **options,
                    )
                else:
                    result = store.evaluate_formula(
                        "berlin",
                        case["metric_id"],
                        case["inputs"],
                        backend=backend,
                        **options,
                    )
                timings.append((time.perf_counter() - started) * 1000)
            assert result["result"]["value"] == case["expected"][backend]
            case["backends"][backend] = {
                "median_ms": statistics.median(timings),
                "p95_ms": sorted(timings)[18],
                "receipt": result,
            }
    report = {
        "pint_version": version("pint"),
        "repetitions": 20,
        "cases": cases,
        "fixture": "authored exact arithmetic with hand-specified expected values",
        "differences": "Native formula arithmetic assumes values already share the intended scale; Pint carries explicit units through each operation.",
        "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "decision": "adopt explicit optional Pint physical conversions and non-offset dimensional formulas; preserve native defaults and economic semantics",
    }
    Path(args.out).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {"cases": len(cases), "peak_rss_kib": report["process_peak_rss_kib"]}
        )
    )
    store.conn.close()


if __name__ == "__main__":
    main()
