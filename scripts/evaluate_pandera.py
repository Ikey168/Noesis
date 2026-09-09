"""Measure the optional Pandera validator against native bounded ingestion."""

import argparse
import json
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import duckdb

from src.kb.dataset_intelligence import INGEST_SCOPE, DatasetIntelligenceStore
from src.kb.pandera_evaluation import PIN, validate_release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=1000)
    args = parser.parse_args()
    if not 100 <= args.rows <= 10_000:
        parser.error("rows must be between 100 and 10000")
    conn = duckdb.connect()
    try:
        store = DatasetIntelligenceStore(conn, now=lambda: 1000)
        columns = [
            {"name": "geo", "type": "string", "nullable": False},
            {"name": "period", "type": "date", "nullable": False},
            {"name": "value", "type": "number", "nullable": True},
        ]
        dataset = store.register_dataset(
            "economic",
            "destatis",
            "berlin-cpi",
            "1",
            "Berlin CPI",
            "Synthetic benchmark fixture",
            {"id": "CC-BY-4.0"},
            [
                {
                    "name": "observations",
                    "identity": "observations",
                    "primary_key": ["geo", "period"],
                    "columns": columns,
                }
            ],
            [],
            [],
            principal_id="operator",
            scopes={"operator"},
            observed_at_ms=1,
        )
        table_id = dataset["tables"][0]["table_id"]
        rows = [f"DE,{2000 + i:04d}-01-01,{i / 10:.1f}" for i in range(args.rows)]
        csv = "geo,period,value\n" + "\n".join(rows) + "\n"
        native_release = store.register_release(
            "economic",
            dataset["dataset_id"],
            "native",
            "native",
            retrieved_at_ms=1,
            principal_id="operator",
            scopes={"operator"},
            provenance={"fixture": "synthetic-eu-de"},
        )
        start = time.perf_counter()
        native = store.ingest(
            "economic",
            native_release["release_id"],
            table_id,
            "csv",
            csv,
            {"fixture": "native"},
            principal_id="operator",
            scopes={INGEST_SCOPE},
        )
        native_ms = (time.perf_counter() - start) * 1000
        candidate_release = store.register_release(
            "economic",
            dataset["dataset_id"],
            "pandera",
            "pandera",
            retrieved_at_ms=1,
            principal_id="operator",
            scopes={"operator"},
            provenance={"fixture": "synthetic-eu-de"},
        )
        start = time.perf_counter()
        candidate = validate_release(
            store,
            "economic",
            candidate_release["release_id"],
            table_id,
            content=csv,
            constraints={
                "version": "benchmark-v1",
                "ranges": {"value": {"min": 0, "max": 1000}},
                "unique": [],
                "cross_fields": [],
            },
            principal_id="operator",
            scopes={"operator"},
        )
        candidate_ms = (time.perf_counter() - start) * 1000
        report = {
            "schema": "noesis-pandera-evaluation-v1",
            "pandera_version": PIN,
            "fixture": "synthetic German CSV; EU/German shape, no human labels",
            "rows": args.rows,
            "native": {
                "elapsed_ms": native_ms,
                "status": native["status"],
                "inserted": native["counts"]["inserted"],
            },
            "pandera": {
                "elapsed_ms": candidate_ms,
                "status": candidate["status"],
                "errors": candidate["error_count"],
                "coercions": candidate["coercion_count"],
            },
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "decision": "defer production replacement: candidate adds richer checks and locators but is slower and retains a separate optional receipt path",
        }
        args.out.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
