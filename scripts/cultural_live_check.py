#!/usr/bin/env python3
"""Bounded live acceptance run for the DDB and Europeana cultural sources.

Installs the scientific source pack in a throwaway warehouse and runs only its
``ddb`` and ``europeana`` sources live with keys from ``NOESIS_DDB_API_KEY``
and ``NOESIS_EUROPEANA_API_KEY`` (never written). The report records counts,
missing fields, the rights-category distribution, place/coordinate coverage,
cross-provider identifier matches, bytes, hashes, latencies and failures.

    python scripts/cultural_live_check.py --output docs/development/cultural-evidence/live-check-<date>.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SOURCES = ["ddb-berlin-photographs", "europeana-berlin-images"]
SCOPES = {"knowledge:cultural:read", "knowledge:cultural:write", "namespace:global:write"}


def run(output: Path, *, environment_note: str = "") -> dict:
    import duckdb

    from src.ingestion.source_pack_runtime import SourcePackRuntime
    from src.ingestion.source_packs import SourcePackStore, validate_source_pack
    from src.kb.cultural import CulturalStore, readiness

    started = datetime.now(UTC)
    manifest = validate_source_pack(json.loads((REPO_ROOT / "config/source_packs/scientific.json").read_text()))
    conn = duckdb.connect(":memory:")
    SourcePackStore(conn).install(manifest, principal_id="live-check", enable=True)
    runtime = SourcePackRuntime(conn)
    for source in manifest["sources"]:
        runtime.accept_license(manifest["pack_id"], source["source_id"], principal_id="live-check")
    secrets = {name: os.environ.get(name) for name in ("NOESIS_DDB_API_KEY", "NOESIS_EUROPEANA_API_KEY")}
    report: dict = {"check": "cultural-live-acceptance", "started_at": started.isoformat(),
                    "environment_note": environment_note,
                    "credentials_present": {k: bool(v) for k, v in secrets.items()},
                    "readiness": readiness(conn, secrets=lambda name: secrets.get(name)), "sources": {}}
    for source_id in SOURCES:
        begin = time.monotonic()
        entry: dict = {}
        try:
            receipt = runtime.run(
                {"pack_id": manifest["pack_id"], "run_key": f"live:{source_id}:{started.timestamp()}",
                 "operation": "objects", "source_ids": [source_id], "max_results": 200, "max_bytes": 20_000_000,
                 "timeout_ms": 60_000, "network": "live"},
                principal_id="live-check", secret_resolver=lambda name: secrets.get(name))
            if not receipt["sources"]:
                entry = {"status": "blocked", "failure": receipt.get("failures")}
            else:
                item = receipt["sources"][0]
                entry = {"status": item["status"], "failure": item.get("failure"), "counts": item["counts"]}
        except Exception as exc:  # noqa: BLE001 - every failure is evidence
            entry = {"status": "failed", "failure": {"code": getattr(exc, "code", type(exc).__name__),
                                                     "message": str(exc)[:300]}}
        entry["elapsed_s"] = round(time.monotonic() - begin, 2)
        report["sources"][source_id] = entry
    store = CulturalStore(conn)
    objects = store.search("global", scopes=SCOPES, limit=100)["objects"]
    report["objects"] = {
        "count": len(objects),
        "by_provider": Counter(o["provider"] for o in objects),
        "rights_categories": Counter(o["rights"]["category"] for o in objects),
        "place_states": Counter(p["state"] for o in objects for p in o["places"]),
        "missing": {field: sum(1 for o in objects if not o.get(field))
                    for field in ("dates", "creators", "places", "languages", "representations")},
    }
    report["matches"] = Counter(m["candidate_state"] for m in store.propose_matches("global", scopes=SCOPES)["matches"])
    both = all(v.get("status") == "complete" for v in report["sources"].values())
    report["acceptance"] = {"both_providers_live": both,
                            "note": "Fixture success or one provider never establishes production readiness."}
    report["finished_at"] = datetime.now(UTC).isoformat()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n")
    conn.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--environment-note", default="")
    args = parser.parse_args()
    report = run(args.output, environment_note=args.environment_note)
    print(json.dumps(report["acceptance"], indent=2))
    return 0 if report["acceptance"]["both_providers_live"] else 1


if __name__ == "__main__":
    sys.exit(main())
