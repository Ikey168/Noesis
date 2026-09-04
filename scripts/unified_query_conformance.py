#!/usr/bin/env python3
"""Run the unified query plane against a deterministic offline corpus."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.kb.unified_query import QueryCatalog, StaticQueryAdapter, UnifiedQueryEngine


def main() -> int:
    fixture = json.loads(
        (ROOT / "tests/fixtures/unified_query/evaluation.json").read_text()
    )
    adapters = [
        StaticQueryAdapter(
            source["source_id"], source["items"], domains=source["domains"]
        )
        for source in fixture["sources"]
    ]
    engine = UnifiedQueryEngine(QueryCatalog(adapters))
    scopes = {"knowledge:read"}
    plan = engine.plan(fixture["request"], scopes=scopes)
    result = engine.execute(fixture["request"], scopes=scopes)
    replay = engine.replay(fixture["request"], result, scopes=scopes)
    evaluation = engine.evaluate(result, expected_ids=fixture["expected_ids"])
    report = {
        "contract": "noesis-unified-query-conformance-v1",
        "passed": bool(
            replay["matched"]
            and evaluation["passed"]
            and evaluation["metrics"]["recall"] == 1
        ),
        "plan_hash": plan["plan_hash"],
        "replay_hash": result["replay_hash"],
        "sources": plan["selected_sources"],
        "items": len(result["items"]),
        "evaluation": evaluation,
    }
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
