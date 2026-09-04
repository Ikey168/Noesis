#!/usr/bin/env python3
"""Execute all 22 configured sources without network access."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, load_source_packs


def main() -> int:
    conn = duckdb.connect(":memory:")
    manifests = load_source_packs(ROOT / "config/source_packs")
    store = SourcePackStore(conn)
    runtime = SourcePackRuntime(conn, sleep=lambda _delay: None)
    receipts = []
    try:
        for manifest in manifests:
            store.install(manifest, principal_id="reference", enable=True)
            adapters = runtime.fixture_adapters(manifest["pack_id"], ROOT)
            for source in manifest["sources"]:
                runtime.accept_license(
                    manifest["pack_id"], source["source_id"], principal_id="reference"
                )
                receipts.append(
                    runtime.run(
                        {
                            "pack_id": manifest["pack_id"],
                            "run_key": f"reference:{source['source_id']}",
                            "operation": source["operations"][0],
                            "source_ids": [source["source_id"]],
                            "required_sources": [source["source_id"]],
                            "network": "disabled",
                        },
                        principal_id="reference",
                        adapters={source["source_id"]: adapters[source["source_id"]]},
                        secret_resolver=lambda _: "fixture-secret",
                        dns_resolver=lambda _: ["8.8.8.8"],
                    )
                )
        coverage = runtime.runtime_coverage()
        domains = sorted(coverage["domains"])
        passed = (
            len(receipts) == 22
            and all(item["watermark"] for item in receipts)
            and len(domains) == 6
        )
        print(
            json.dumps(
                {
                    "contract": "noesis-source-pack-runtime-reference-v1",
                    "passed": passed,
                    "packs": len(manifests),
                    "sources": len(receipts),
                    "domains": domains,
                    "documents": conn.execute(
                        "SELECT COUNT(*) FROM documents"
                    ).fetchone()[0],
                    "watermarks": {
                        item["pack_id"]: item["watermark"] for item in receipts
                    },
                    "coverage": coverage,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0 if passed else 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
