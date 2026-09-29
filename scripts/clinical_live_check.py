#!/usr/bin/env python3
"""Bounded live check for the Clinical Evidence sources (H14).

Installs ``config/source_packs/clinical-evidence.json`` in a throwaway
warehouse and runs each source live, one at a time, through the real
source-pack runtime, native adapters and projection with small budgets. It
also sends one plain HTTPS probe per provider host (the same urllib transport
policy the runtime uses) so a failure is recorded with its cause. An openFDA
key is used only if ``NOESIS_OPENFDA_API_KEY`` is set; it is never written to
the report. Nothing is submitted anywhere; only public records are read.

The report keeps live evidence strictly apart from the offline fixture
evidence of the unit and acceptance tests.

    python scripts/clinical_live_check.py --output docs/development/clinical-evidence/live-check-<date>.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PROBES = {
    "ctgov": "https://clinicaltrials.gov/api/v2/version",
    "ctis": "https://euclinicaltrials.eu/ctis-public-api/retrieve/2023-509001-12-00",
    "euctr": "https://www.clinicaltrialsregister.eu/ctr-search/search?query=diabetes",
    "openfda": "https://api.fda.gov/drug/label.json?limit=1",
    "ema": "https://www.ema.europa.eu/en/medicines/download-medicine-data",
    "prospero": "https://www.crd.york.ac.uk/prospero/",
}


def probe(url: str) -> dict:
    """One plain GET with the runtime's transport policy; the cause of any failure is kept."""
    started = time.monotonic()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}),
                                    timeout=15) as response:
            outcome = {"outcome": "reachable", "http_status": response.status, "bytes": len(response.read(65536))}
    except urllib.error.HTTPError as exc:
        outcome = {"outcome": "http_error", "http_status": exc.code}
    except urllib.error.URLError as exc:
        outcome = {"outcome": "unreachable", "error_type": type(exc.reason).__name__, "error": str(exc.reason)[:200]}
    except Exception as exc:  # noqa: BLE001 - every failure is evidence
        outcome = {"outcome": "unreachable", "error_type": type(exc).__name__, "error": str(exc)[:200]}
    outcome["elapsed_s"] = round(time.monotonic() - started, 2)
    return outcome


def run(output: Path, *, environment_note: str = "") -> dict:
    import duckdb

    from src.ingestion.clinical_providers import CONNECTOR_PROVIDER, PROVIDER_CONTRACTS
    from src.ingestion.source_pack_runtime import SourcePackRuntime
    from src.ingestion.source_packs import SourcePackStore, validate_source_pack
    from src.kb.clinical_records import ClinicalRecordStore

    started = datetime.now(UTC)
    raw = json.loads((REPO_ROOT / "config/source_packs/clinical-evidence.json").read_text())
    raw["version"] = raw["version"] + "-live." + started.strftime("%Y%m%d")
    manifest = validate_source_pack(raw)
    conn = duckdb.connect(":memory:")
    SourcePackStore(conn).install(manifest, principal_id="live-check", enable=True)
    runtime = SourcePackRuntime(conn)
    for source in manifest["sources"]:
        runtime.accept_license(manifest["pack_id"], source["source_id"], principal_id="live-check")
    report: dict = {"contract": "noesis-clinical-live-check-v1", "evidence_kind": "live",
                    "started_at": started.isoformat(), "environment_note": environment_note,
                    "note": "Live evidence only. Offline fixture evidence is reported by tests/unit/clinical and "
                            "tests/unit/domains/test_clinical_evidence_acceptance.py and is never live coverage.",
                    "probes": {}, "sources": {}}
    for provider, url in PROBES.items():
        report["probes"][provider] = {"url": url, "contract_status": PROVIDER_CONTRACTS[provider]["status"],
                                      **probe(url)}
    for source in manifest["sources"]:
        begin = time.monotonic()
        entry: dict = {"connector": source["connector"], "provider": CONNECTOR_PROVIDER[source["connector"]],
                       "endpoint": source["endpoint"], "selection": source.get("clinical")}
        try:
            receipt = runtime.run(
                {"pack_id": manifest["pack_id"], "run_key": f"live:{source['source_id']}:{started.timestamp()}",
                 "operation": "records", "source_ids": [source["source_id"]], "max_pages": 5, "max_results": 50,
                 "max_bytes": 20_000_000, "timeout_ms": 30_000, "network": "live", "mode": "backfill",
                 "backfill": {"from_ms": 0}, "retries": 0},
                principal_id="live-check", secret_resolver=lambda ref: os.environ.get(ref) or None)
            if not receipt["sources"]:
                entry.update(status="blocked", failure=receipt.get("failures") or receipt.get("preflight"))
            else:
                item = receipt["sources"][0]
                entry.update(status=item["status"], failure=item.get("failure"), counts=item["counts"],
                             retries=item.get("retries"))
        except Exception as exc:  # noqa: BLE001 - every failure is evidence
            entry.update(status="failed", failure={"code": getattr(exc, "code", type(exc).__name__),
                                                   "message": str(exc)[:300]})
        entry["elapsed_s"] = round(time.monotonic() - begin, 2)
        report["sources"][source["source_id"]] = entry
    store = ClinicalRecordStore(conn)
    namespaces = sorted({s["clinical"]["namespace"] for s in manifest["sources"]})
    report["projected_records"] = {ns: len(store.find(ns, scopes={"operator"})) for ns in namespaces}
    live_ok = sorted(k for k, v in report["sources"].items() if v.get("status") == "complete")
    report["live_verification"] = {
        provider: ({"status": "verified-live-bounded", "date": started.date().isoformat()}
                   if any(report["sources"][s]["provider"] == provider and s in live_ok for s in report["sources"])
                   else {"status": "unverified-live",
                         "last_check": {"date": started.date().isoformat(),
                                        "failure_codes": sorted({(v.get("failure") or {}).get("code") or v["status"]
                                                                 for v in report["sources"].values()
                                                                 if v["provider"] == provider}),
                                        "probe": report["probes"][provider]["outcome"]}})
        for provider in ("ctgov", "ctis", "euctr", "openfda", "ema")}
    report["live_verification"]["prospero"] = {"status": "not-implemented",
                                               "reason": PROVIDER_CONTRACTS["prospero"]["reason"]}
    report["summary"] = {"sources_complete": live_ok,
                         "sources_failed": sorted(k for k in report["sources"] if k not in live_ok)}
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
    print(json.dumps({"summary": report["summary"], "live_verification": report["live_verification"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
