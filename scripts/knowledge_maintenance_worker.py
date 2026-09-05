#!/usr/bin/env python3
"""Run the durable Noesis knowledge-maintenance worker."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config.env import warehouse_path
from src.kb.maintenance import MaintenanceOrchestrator, fixture_adapter_provider


def _secret(name: str) -> str | None:
    return os.environ.get(name) if name.startswith("NOESIS_") else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="DuckDB warehouse path")
    parser.add_argument(
        "--config", default=str(ROOT / "config/knowledge-maintenance.json")
    )
    parser.add_argument("--owner-id", default=f"{socket.gethostname()}:{os.getpid()}")
    parser.add_argument(
        "--once", action="store_true", help="perform one bounded polling tick"
    )
    parser.add_argument("--max-jobs", type=int, help="override jobs processed per tick")
    parser.add_argument(
        "--live-network",
        action="store_true",
        help="explicitly enable live source access",
    )
    args = parser.parse_args()
    settings = json.loads(Path(args.config).read_text(encoding="utf-8"))
    network = "live" if args.live_network else str(settings.get("network", "disabled"))
    if network == "live" and not args.live_network:
        raise SystemExit("live network requires --live-network")
    max_jobs = min(max(1, args.max_jobs or int(settings["max_jobs_per_tick"])), 100)
    poll = min(max(1, int(settings["poll_interval_s"])), 60)
    stop = threading.Event()
    for name in (signal.SIGINT, signal.SIGTERM):
        signal.signal(name, lambda *_: stop.set())

    database = args.database or warehouse_path(str(ROOT / "data/neuronews.duckdb"))
    conn = duckdb.connect(database)
    orchestrator = MaintenanceOrchestrator(conn, root=ROOT,
        execution_mode=settings.get("execution_mode", "production"),
        extractor_definition=settings.get("extractor_definition"),
        embedding_configuration=settings.get("embedding_configuration"))
    adapters = None if network == "live" else fixture_adapter_provider(orchestrator)
    dns = None if network == "live" else lambda _: ["8.8.8.8"]
    print(
        json.dumps(
            {
                "contract": "noesis-maintenance-readiness-v1",
                "ready": True,
                "owner_id": args.owner_id,
                "network": network,
            }
        ),
        flush=True,
    )
    try:
        while not stop.is_set():
            orchestrator.enqueue_due(
                max_catchup=int(settings["max_catchup_windows"]),
                principal_id=args.owner_id,
                network=network,
            )
            result = orchestrator.drain(
                args.owner_id,
                max_jobs=max_jobs,
                enqueue=False,
                lease_ms=int(settings["lease_ms"]),
                adapter_provider=adapters,
                secret_resolver=_secret,
                dns_resolver=dns,
                cancelled=stop.is_set,
            )
            print(json.dumps(result, sort_keys=True), flush=True)
            if args.once:
                return (
                    0
                    if all(
                        job["status"] in {"complete", "partial"}
                        for job in result["jobs"]
                    )
                    else 1
                )
            stop.wait(poll)
        return 0
    finally:
        # A claimed-but-not-started lease is safely recovered after expiry; active
        # source/workflow stages observe the cancellation event above.
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
