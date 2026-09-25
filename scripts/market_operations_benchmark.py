"""Measure bounded local market-operations ingestion and health-query capacity."""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.domains.market.operations import MarketOperationsStore  # noqa: E402

NAMESPACE = "market:operations-benchmark"
PRINCIPAL = "operator:benchmark"
SCOPES = {"operator"}


def run_benchmark(rows: int) -> dict[str, Any]:
    if type(rows) is not int or not 1 <= rows <= 10_000:
        raise ValueError("rows must be between 1 and 10000")
    conn = duckdb.connect(":memory:")
    store = MarketOperationsStore(conn)
    measurements = [
        {
            "measurement_id": f"benchmark:latency:{index}",
            "metric": "query_latency_ms",
            "provider": "local-benchmark",
            "observed_at_ms": index,
            "value": float(index % 25),
            "status": "observed",
            "dimensions": {"benchmark": "bounded-operations-ingest-v1"},
        }
        for index in range(rows)
    ]
    started = time.perf_counter_ns()
    receipt = store.record_measurements(
        NAMESPACE,
        measurements=measurements,
        owner=PRINCIPAL,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    ingest_elapsed_ns = max(1, time.perf_counter_ns() - started)
    query_started = time.perf_counter_ns()
    health = store.provider_health(
        NAMESPACE,
        provider="local-benchmark",
        window_start_ms=0,
        window_end_ms=rows,
        principal_id=PRINCIPAL,
        scopes=SCOPES,
    )
    query_elapsed_ns = max(1, time.perf_counter_ns() - query_started)
    conn.close()
    return {
        "contract": "noesis-market-local-operations-benchmark-v1",
        "evidence_kind": "local_benchmark",
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "target": {
            "measurement_rows": rows,
            "provider": "local-benchmark",
            "database": "in-memory DuckDB",
            "concurrency": 1,
        },
        "observed": {
            "accepted_rows": receipt["count"],
            "ingest_elapsed_ms": ingest_elapsed_ns / 1_000_000,
            "ingest_rows_per_second": rows * 1_000_000_000 / ingest_elapsed_ns,
            "provider_health_query_ms": query_elapsed_ns / 1_000_000,
            "provider_health_state": health["providers"]["local-benchmark"]["state"],
        },
        "cost": {
            "provider_requests": 0,
            "provider_cost_micros": 0,
            "infrastructure_cost_status": "not_measured",
        },
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "limitations": [
            "This is a single-process local operations-store benchmark, not production market-data throughput.",
            "No provider requests are made, so provider cost is zero and infrastructure cost remains unmeasured.",
            "Production concurrency, retention, backup duration and notification delivery require deployment telemetry.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "config/market/acceptance_packs/local-operations-benchmark.json"
        ),
    )
    args = parser.parse_args()
    result = run_benchmark(args.rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "measured",
                "rows": result["observed"]["accepted_rows"],
                "rows_per_second": round(
                    result["observed"]["ingest_rows_per_second"], 2
                ),
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
