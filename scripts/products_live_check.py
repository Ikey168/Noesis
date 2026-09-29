#!/usr/bin/env python3
"""Bounded live acceptance run for the Products pack (Open Icecat + EPREL).

The pinned pack selects a fictional fixture cohort, so a live run needs an
operator-chosen reference cohort of real display models:

    {"icecat": [{"brand": "...", "product_code": "..."}, {"gtin": "..."}],
     "eprel":  [{"registration_number": "..."}]}

The script installs the pack with that selection in a throwaway warehouse,
accepts the source terms on the operator's behalf only for this run, executes
one bounded live acquisition per provider through the real runtime, proposes
cross-provider matches and, when at least three models are matched, builds a
comparison. The JSON report records dated counts, per-selector outcomes,
response hashes, quota headers and failure codes. Credentials are read from
``NOESIS_EPREL_API_KEY`` / ``NOESIS_ICECAT_API_TOKEN`` and never written.

A failed or unreachable provider stays a failure in the report; offline
fixtures and one working provider never satisfy two-provider acceptance.

    python scripts/products_live_check.py --cohort cohort.json \\
        --output docs/development/products-evidence/live-check.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

NAMESPACE = "global"
SCOPES = {"knowledge:products:read", "knowledge:products:write", "knowledge:products:review",
          f"namespace:{NAMESPACE}:write"}


def live_manifest(cohort: dict, *, version: str) -> dict:
    raw = json.loads((REPO_ROOT / "config/source_packs/products.json").read_text())
    raw["version"] = version
    for source in raw["sources"]:
        selection = cohort.get(source["connector"])
        if not selection:
            raise SystemExit(f"cohort has no {source['connector']} selection")
        source["product"]["selection"] = selection
    return raw


def run(cohort: dict, output: Path, *, environment_note: str = "") -> dict:
    import duckdb

    from src.ingestion.source_pack_runtime import SourcePackRuntime
    from src.ingestion.source_packs import SourcePackStore, validate_source_pack
    from src.kb.products import ProductStore, readiness

    started = datetime.now(UTC)
    conn = duckdb.connect(":memory:")
    manifest = validate_source_pack(live_manifest(cohort, version="1.0.0-live." + started.strftime("%Y%m%d")))
    SourcePackStore(conn).install(manifest, principal_id="live-check", enable=True)
    runtime = SourcePackRuntime(conn)
    for source in manifest["sources"]:
        runtime.accept_license(manifest["pack_id"], source["source_id"], principal_id="live-check")
    secrets = {name: os.environ.get(name) for name in ("NOESIS_EPREL_API_KEY", "NOESIS_ICECAT_API_TOKEN")}
    report: dict = {
        "check": "products-live-acceptance",
        "started_at": started.isoformat(),
        "environment_note": environment_note,
        "credentials_present": {name: bool(value) for name, value in secrets.items()},
        "readiness": readiness(conn, secrets=lambda name: secrets.get(name)),
        "providers": {},
    }
    for source in manifest["sources"]:
        begin = time.monotonic()
        entry: dict = {"source_id": source["source_id"], "endpoint": source["endpoint"],
                       "selection_size": len(source["product"]["selection"])}
        try:
            receipt = runtime.run(
                {"pack_id": manifest["pack_id"], "run_key": f"live:{source['source_id']}:{started.timestamp()}",
                 "operation": "models", "source_ids": [source["source_id"]], "max_results": 50,
                 "max_bytes": 5_000_000, "timeout_ms": 60_000, "network": "live"},
                principal_id="live-check", secret_resolver=lambda name: secrets.get(name),
            )
            if not receipt["sources"]:
                entry.update({"status": "blocked", "failure": receipt.get("failures")})
                report["providers"][source["connector"]] = {**entry, "elapsed_s": round(time.monotonic() - begin, 2)}
                continue
            item = receipt["sources"][0]
            entry.update({
                "status": item["status"], "failure": item.get("failure"), "counts": item["counts"],
                "retries": item.get("retries"), "projection": item.get("projection"),
                "outcomes": ProductStore(conn).selection_outcomes(NAMESPACE, receipt["run_id"]),
            })
        except Exception as exc:  # noqa: BLE001 - every failure is evidence, not a crash
            entry.update({"status": "failed", "failure": {"code": getattr(exc, "code", type(exc).__name__),
                                                          "message": str(exc)[:300]}})
        entry["elapsed_s"] = round(time.monotonic() - begin, 2)
        report["providers"][source["connector"]] = entry
    store = ProductStore(conn)
    variants = store.lookup(NAMESPACE, scopes=SCOPES, limit=100)["variants"]
    report["variants"] = [{k: v[k] for k in ("provider", "provider_record_id", "brand", "designation",
                                             "provider_revision", "record_state")} for v in variants]
    both = all(item.get("status") == "complete" for item in report["providers"].values())
    if both:
        candidates = store.propose_matches(NAMESPACE, scopes=SCOPES, principal_id="live-check")["candidates"]
        report["match_candidates"] = [{k: c[k] for k in ("left_model_id", "right_model_id", "candidate_state",
                                                         "confidence", "reasons")} for c in candidates]
        proposed = [c for c in candidates if c["candidate_state"] == "proposed"]
        report["comparison"] = None
        if len(proposed) >= 3:
            # Matches stay unreviewed here: a human reviewer accepts them.
            report["comparison_note"] = "three proposed matches exist; review them before comparing"
    report["acceptance"] = {
        "two_provider_live_run": "passed" if both else "outstanding",
        "cross_source_overlap": ("candidates-recorded" if both and report.get("match_candidates")
                                 else "outstanding"),
        "note": "Offline fixtures and a single working provider never satisfy two-provider acceptance.",
    }
    report["finished_at"] = datetime.now(UTC).isoformat()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n")
    conn.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cohort", type=Path, required=True, help="JSON file with icecat and eprel selectors")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--environment-note", default="")
    args = parser.parse_args()
    report = run(json.loads(args.cohort.read_text()), args.output, environment_note=args.environment_note)
    print(json.dumps(report["acceptance"], indent=2))
    return 0 if report["acceptance"]["two_provider_live_run"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
