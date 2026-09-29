#!/usr/bin/env python3
"""Bounded live acceptance run for the Legal pack (CELLAR, federal courts, Berlin).

Installs ``config/source_packs/legal.json`` in a throwaway warehouse, records
the operator's terms acceptance for this run only, and executes every source
live through the real runtime and Legal projection. The report records per
source: status, failure code, counts, per-selection outcomes, response hashes,
latency, and for each acquired work its jurisdiction, identifiers, versions
and sourced dates. No credentials are involved or written.

The pinned Berlin selection is a fictional fixture; pass ``--berlin`` with a
JSON list of real portal selections for a meaningful Berlin check. Retrieval
relevance is not evaluated here.

    python scripts/legal_live_check.py --output docs/development/legal-evidence/live-check-<date>.json
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

SCOPES = {"knowledge:legal:read", "knowledge:legal:write", "namespace:global:write"}


def run(output: Path, *, berlin: list | None = None, environment_note: str = "") -> dict:
    import duckdb

    from src.ingestion.source_pack_runtime import SourcePackRuntime
    from src.ingestion.source_packs import SourcePackStore, validate_source_pack
    from src.kb.legal import LegalStore, readiness

    started = datetime.now(UTC)
    raw = json.loads((REPO_ROOT / "config/source_packs/legal.json").read_text())
    raw["version"] = "1.0.0-live." + started.strftime("%Y%m%d")
    if berlin:
        for source in raw["sources"]:
            if source["connector"] == "berlin-law":
                source["legal"]["selection"] = berlin
    manifest = validate_source_pack(raw)
    conn = duckdb.connect(":memory:")
    SourcePackStore(conn).install(manifest, principal_id="live-check", enable=True)
    runtime = SourcePackRuntime(conn)
    for source in manifest["sources"]:
        runtime.accept_license(manifest["pack_id"], source["source_id"], principal_id="live-check")
    store = LegalStore(conn)
    report: dict = {"check": "legal-live-acceptance", "started_at": started.isoformat(),
                    "environment_note": environment_note, "berlin_selection": "operator" if berlin else "fixture",
                    "sources": {}}
    for source in manifest["sources"]:
        begin = time.monotonic()
        entry: dict = {"connector": source["connector"], "endpoint": source["endpoint"]}
        try:
            receipt = runtime.run(
                {"pack_id": manifest["pack_id"], "run_key": f"live:{source['source_id']}:{started.timestamp()}",
                 "operation": "records", "source_ids": [source["source_id"]], "max_results": 200,
                 "max_bytes": 50_000_000, "timeout_ms": 120_000, "network": "live"},
                principal_id="live-check")
            if not receipt["sources"]:
                entry.update({"status": "blocked", "failure": receipt.get("failures")})
            else:
                item = receipt["sources"][0]
                entry.update({"status": item["status"], "failure": item.get("failure"), "counts": item["counts"],
                              "outcomes": [o for o in store.selection_outcomes("global", receipt["run_id"])
                                           if o["source_id"] == source["source_id"]]})
        except Exception as exc:  # noqa: BLE001 - every failure is evidence
            entry.update({"status": "failed", "failure": {"code": getattr(exc, "code", type(exc).__name__),
                                                          "message": str(exc)[:300]}})
        entry["elapsed_s"] = round(time.monotonic() - begin, 2)
        report["sources"][source["source_id"]] = entry
    works = conn.execute("SELECT work_id FROM legal_works ORDER BY work_id").fetchall()
    report["works"] = []
    for (work_id,) in works:
        inspected = store.inspect("global", work_id, scopes=SCOPES)
        report["works"].append({
            "jurisdiction": inspected["jurisdiction"], "work_kind": inspected["work_kind"],
            "identifiers": inspected["identifiers"],
            "versions": [{"language": v["language"], "content_coverage": v["content_coverage"],
                          "passages": v["passage_count"], "text_sha256": v["text_sha256"],
                          "facts": [(f["fact"], f["value"]) for f in v["facts"]]} for v in inspected["versions"]],
        })
    report["readiness"] = readiness(conn)
    jurisdictions = {w["jurisdiction"] for w in report["works"]}
    report["acceptance"] = {
        "sources_complete": sorted(k for k, v in report["sources"].items() if v.get("status") == "complete"),
        "jurisdictions_demonstrated": sorted(jurisdictions),
        "all_three_jurisdictions": jurisdictions >= {"EU", "DE", "DE-BE"},
        "retrieval_relevance": "not evaluated (see config/legal/retrieval_modes.json)",
    }
    report["finished_at"] = datetime.now(UTC).isoformat()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n")
    conn.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--berlin", type=Path, help="JSON list of real berlin-law selections")
    parser.add_argument("--environment-note", default="")
    args = parser.parse_args()
    berlin = json.loads(args.berlin.read_text()) if args.berlin else None
    report = run(args.output, berlin=berlin, environment_note=args.environment_note)
    print(json.dumps(report["acceptance"], indent=2))
    return 0 if report["acceptance"]["all_three_jurisdictions"] else 1


if __name__ == "__main__":
    sys.exit(main())
