#!/usr/bin/env python3
"""Bounded live acceptance run for the mathematics sources of the research-discovery pack.

Installs ``config/source_packs/research.json`` in a throwaway warehouse and
runs only the mathematics sources (zbMATH Open, OEIS, mathlib4, AFP) live
through the real adapters, runtime and projection. For the formal-library
sources it also compares each live file hash with the captured fixture, which
shows whether the offline fixture is byte-identical to the pinned commit.
No credentials are involved.

    python scripts/mathematics_live_check.py --output docs/development/mathematics-evidence/live-check-<date>.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

MATH_SOURCES = {"zbmath-open": "documents", "oeis-sequences": "sequences", "mathlib4-fib": "snapshot",
                "afp-zeckendorf": "snapshot"}


def run(output: Path, *, environment_note: str = "") -> dict:
    import duckdb

    from src.ingestion.source_pack_runtime import SourcePackRuntime
    from src.ingestion.source_packs import SourcePackStore, validate_source_pack

    started = datetime.now(UTC)
    raw = json.loads((REPO_ROOT / "config/source_packs/research.json").read_text())
    raw["version"] = raw["version"] + "-live." + started.strftime("%Y%m%d")
    manifest = validate_source_pack(raw)
    conn = duckdb.connect(":memory:")
    SourcePackStore(conn).install(manifest, principal_id="live-check", enable=True)
    runtime = SourcePackRuntime(conn)
    for source in manifest["sources"]:
        runtime.accept_license(manifest["pack_id"], source["source_id"], principal_id="live-check")
    report: dict = {"check": "mathematics-live-acceptance", "started_at": started.isoformat(),
                    "environment_note": environment_note, "sources": {}}
    for source in manifest["sources"]:
        if source["source_id"] not in MATH_SOURCES:
            continue
        begin = time.monotonic()
        entry: dict = {"connector": source["connector"], "endpoint": source["endpoint"]}
        try:
            receipt = runtime.run(
                {"pack_id": manifest["pack_id"], "run_key": f"live:{source['source_id']}:{started.timestamp()}",
                 "operation": MATH_SOURCES[source["source_id"]], "source_ids": [source["source_id"]],
                 "max_pages": 50, "max_results": 50, "max_bytes": 5_000_000, "timeout_ms": 30_000,
                 "network": "live", "mode": "backfill", "backfill": {"from_ms": 0}},
                principal_id="live-check")
            if not receipt["sources"]:
                entry.update({"status": "blocked", "failure": receipt.get("failures")})
            else:
                item = receipt["sources"][0]
                entry.update({"status": item["status"], "failure": item.get("failure"), "counts": item["counts"]})
        except Exception as exc:  # noqa: BLE001 - every failure is evidence
            entry.update({"status": "failed", "failure": {"code": getattr(exc, "code", type(exc).__name__),
                                                          "message": str(exc)[:300]}})
        entry["elapsed_s"] = round(time.monotonic() - begin, 2)
        if source["connector"] == "formal-library" and entry.get("status") == "complete":
            fixture = json.loads((REPO_ROOT / source["fixture"]["path"]).read_text())
            captured = {p["request"]: p["sha256"] for p in fixture["native_pages"]}
            live = {r[0]: r[1] for r in conn.execute(
                "SELECT path, file_sha256 FROM formal_files WHERE library=? AND commit_sha=?",
                [source["formal"]["library"], source["formal"]["commit"]]).fetchall()}
            prefix = f"/{source['formal']['repository']}/{source['formal']['commit']}/"
            entry["fixture_matches_live"] = {path: captured.get(prefix + path) == digest
                                             for path, digest in sorted(live.items())}
        report["sources"][source["source_id"]] = entry
    counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("math_literature", "math_sequences", "formal_declarations", "formal_dependencies")}
    report["projected"] = counts
    report["acceptance"] = {
        "sources_complete": sorted(k for k, v in report["sources"].items() if v.get("status") == "complete"),
        "sources_unavailable": sorted(k for k, v in report["sources"].items() if v.get("status") != "complete"),
        "formal_fixtures_identical_to_live": all(all(v.get("fixture_matches_live", {}).values())
                                                  for v in report["sources"].values()
                                                  if "fixture_matches_live" in v),
    }
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
    return 0 if not report["acceptance"]["sources_unavailable"] else 1


if __name__ == "__main__":
    sys.exit(main())
