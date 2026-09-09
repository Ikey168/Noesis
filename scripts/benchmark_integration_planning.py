"""Reproducible comparison of actual planner previews on bounded authored cases."""

import argparse
import json
import platform
import random
import resource
import statistics
import time
from pathlib import Path

import duckdb

from src.integrations.common import version
from src.kb.source_planner import (
    READ_SCOPE,
    WRITE_SCOPE,
    SourcePlannerError,
    SourcePlannerStore,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    rng = random.Random(1513)
    runs = []
    for case in range(24):
        conn = duckdb.connect()
        store = SourcePlannerStore(conn, now=lambda: 100)
        for index in range(6):
            store.register_capability(
                "berlin",
                f"fixture-{index}",
                "1",
                coverage={"domains": ["berlin"], "evidence_classes": ["primary"]},
                authority={
                    "score": rng.choice([0.6, 0.8, 1]),
                    "basis": "authored fixture",
                },
                access={
                    "license_id": "fixture",
                    "terms_accepted": True,
                    "redistribution": True,
                },
                latency={"p95_ms": 100},
                cost={"per_query": rng.randint(0, 4)},
                rate_limits={"requests_per_minute": 10},
                query_forms=rng.choice([["search"], ["series"], ["search", "series"]]),
                connector={
                    "kind": "source-pack",
                    "pack_id": "fixture",
                    "source_id": f"fixture-{index}",
                },
                dependency_group=f"group-{index % 3}",
                principal_id="benchmark",
                scopes={WRITE_SCOPE},
                observed_at_ms=10,
                provenance={"fixture": case},
            )
        objective = store.create_objective(
            "berlin",
            "Welche Berliner Förderung besteht und wie hat sich ihre Nutzung verändert?",
            [
                {"question": "Förderbedingungen", "query_form": "search"},
                {"question": "Nutzung im Zeitverlauf", "query_form": "series"},
            ],
            ["primary"],
            {
                "domain": "berlin",
                "budget": rng.randint(1, 12),
                "max_sources": rng.randint(1, 4),
                "min_independence": rng.randint(1, 3),
                "required_sources": ["fixture-0"] if case % 3 == 0 else [],
            },
            principal_id="benchmark",
            scopes={WRITE_SCOPE},
            observed_at_ms=20,
        )
        for backend in ("greedy", "cp-sat"):
            timings, result = [], None
            for _ in range(3):
                started = time.perf_counter()
                try:
                    plan = store.preview(
                        "berlin",
                        objective["objective_id"],
                        at_ms=30,
                        scopes={READ_SCOPE},
                        optimizer=backend,
                    )
                    result = {
                        "feasible": plan["feasible"],
                        "coverage": plan["coverage"],
                        "projected_cost": plan["budget"]["projected"],
                        "selected": [s["source_id"] for s in plan["steps"]],
                        "plan_hash": plan["plan_hash"],
                        "solver_status": plan["constraints"]
                        .get("optimization", {})
                        .get("status"),
                    }
                except SourcePlannerError as exc:
                    if not exc.code.startswith("optimizer_"):
                        raise
                    result = {
                        "feasible": False,
                        "solver_status": exc.code.removeprefix("optimizer_").upper(),
                    }
                timings.append((time.perf_counter() - started) * 1000)
            runs.append(
                {"case": case, "backend": backend, **result, "latency_ms": timings}
            )
        conn.close()
    summary = {}
    for backend in ("greedy", "cp-sat"):
        values = [r for r in runs if r["backend"] == backend]
        latency = sorted(t for r in values for t in r["latency_ms"])
        summary[backend] = {
            "feasible_cases": sum(r["feasible"] for r in values),
            "p50_ms": statistics.median(latency),
            "p95_ms": latency[int(0.95 * (len(latency) - 1))],
        }
    output = {
        "fixture_kind": "24 authored Berlin research objectives with seeded synthetic capabilities; costs are projected fixture units, not provider prices",
        "seed": 1513,
        "versions": {"ortools": version("ortools"), "duckdb": version("duckdb")},
        "hardware": platform.platform(),
        "process_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "runs": runs,
        "summary": summary,
        "decision": "Adopt as an explicit optional minimum-cost planner for bounded problems. Keep greedy default: authority is not part of the CP-SAT objective, and solving adds latency. No paid acquisition was executed.",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
