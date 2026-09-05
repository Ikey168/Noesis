#!/usr/bin/env python3
"""Prove scheduled source-to-query maintenance across all built-in domains offline."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ingestion.source_pack_runtime import RuntimePage, SourcePackRuntime
from src.ingestion.source_packs import (
    SourcePackError,
    SourcePackStore,
    load_source_packs,
)
from src.kb.maintenance import MaintenanceOrchestrator
from src.kb.unified_query import UnifiedQueryEngine, build_local_catalog


class _UnavailableAdapter:
    def __init__(self, wrapped: Any) -> None:
        self.wrapped = wrapped

    def describe(self) -> dict[str, Any]:
        return self.wrapped.describe()

    def fetch_page(
        self, request: Mapping[str, Any], *, cursor: str | None
    ) -> RuntimePage:
        raise SourcePackError("source_unavailable", "injected optional-source outage")


def main() -> int:
    conn = duckdb.connect(":memory:")
    manifests = load_source_packs(ROOT / "config/source_packs")
    store = SourcePackStore(conn)
    runtime = SourcePackRuntime(conn, sleep=lambda _: None)
    for manifest in manifests:
        store.install(manifest, principal_id="reference", enable=True, now_ms=1)
        for source in manifest["sources"]:
            runtime.accept_license(
                manifest["pack_id"], source["source_id"], principal_id="reference"
            )
        runtime.set_schedule(
            manifest["pack_id"],
            {"kind": "interval", "interval_s": 60, "next_run_at_ms": 10},
            principal_id="reference",
        )

    orchestrator = MaintenanceOrchestrator(conn, root=ROOT, now=lambda: 1_000, execution_mode="fixture")
    degraded_pack = manifests[-1]["pack_id"]

    def adapters(pack_id: str) -> Mapping[str, Any]:
        values = runtime.fixture_adapters(pack_id, ROOT)
        if pack_id == degraded_pack:
            source_id = min(values)
            values[source_id] = _UnavailableAdapter(values[source_id])
        return values

    enqueue = orchestrator.enqueue_due(at_ms=1_000, principal_id="reference")
    drain = orchestrator.drain(
        "reference-worker",
        max_jobs=10,
        enqueue=False,
        principal_id="reference",
        adapter_provider=adapters,
        secret_resolver=lambda _: "fixture-secret",
        dns_resolver=lambda _: ["8.8.8.8"],
    )
    generations = [job["generation"] for job in drain["jobs"]]
    domains = sorted(
        {domain for manifest in manifests for domain in manifest["domains"]}
    )
    query = UnifiedQueryEngine(
        build_local_catalog(conn, domains=domains, include_memory=False)
    ).execute(
        {
            "query": "noesis",
            "scope": {
                "domains": [
                    domain for manifest in manifests for domain in manifest["domains"]
                ]
            },
            "surfaces": ["lexical"],
            "budgets": {
                "max_results": 100,
                "per_source_results": 100,
                "token_budget": 2000,
            },
        },
        scopes={"knowledge:read"},
    )
    replays = [
        orchestrator.replay_generation(item["generation_id"]) for item in generations
    ]
    statuses = {item["status"] for item in generations}
    passed = bool(
        len(enqueue["created"]) == 6
        and len(generations) == 6
        and domains
        == ["economic", "osint", "political", "research", "scientific", "technical"]
        and statuses == {"complete", "partial"}
        and all(item["matched"] for item in replays)
        and query["items"]
    )
    report = {
        "contract": "noesis-knowledge-maintenance-reference-v1",
        "passed": passed,
        "packs": len(manifests),
        "sources": sum(len(item["sources"]) for item in manifests),
        "domains": domains,
        "generation_statuses": sorted(statuses),
        "query_items": len(query["items"]),
        "replay_verified": all(item["matched"] for item in replays),
        "health": orchestrator.health(),
    }
    print(json.dumps(report, sort_keys=True, indent=2))
    conn.close()
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
