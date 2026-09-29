#!/usr/bin/env python3
"""Bounded live checks for the Corporate Ownership and Registries providers.

Runs one small acquisition per provider under its recorded access contract
(``PROVIDER_CONTRACTS``) through the real ``SourcePackRuntime`` with
``network=live`` and writes a JSON report that keeps live results strictly
apart from offline fixture evidence. The selection is one public company:
Apple Inc. for GLEIF (LEI HWUPKR0MPOU8FGXBT394) and SEC EDGAR (CIK 320193),
and BP P.L.C. (company 00102498) for Companies House. For each source the
report records the run receipt status, the per-source failure code (preflight
or transport), page and record counts, and a host connectivity probe, so a
blocked or unreachable source is recorded with its cause rather than hidden.

Credentials are read from the environment only (``NOESIS_COMPANIES_HOUSE_API_KEY``
and ``NOESIS_SEC_CONTACT``, a real contact for the SEC fair-access User-Agent);
without them the runtime preflight records ``credential_missing`` and no
request is sent to that provider. Open Ownership BODS has no verified dataset
path from this runtime, so only a host probe is recorded for it.

    python scripts/ownership_live_check.py --output docs/development/ownership-evidence/live-check-<date>.json
"""

from __future__ import annotations

import argparse
import copy
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

NAMESPACE = "ownership-live-check"
SCOPES = {"knowledge:ownership:read", "knowledge:ownership:write", "knowledge:ingestion:execute",
          f"namespace:{NAMESPACE}:read", f"namespace:{NAMESPACE}:write"}
SELECTION = {
    "gleif-level2": {"lei": {"namespace": NAMESPACE, "leis": ["HWUPKR0MPOU8FGXBT394"]}},
    "companies-house": {"ownership": {"companies": ["00102498"]}},
    "sec-edgar-ownership": {"ownership": {"ciks": ["0000320193"], "filings": [], "max_filings": 20}},
}
PROBES = {
    "gleif": "https://api.gleif.org/api/v1/lei-records/HWUPKR0MPOU8FGXBT394",
    "companies-house": "https://api.company-information.service.gov.uk/",
    "sec-edgar": "https://data.sec.gov/",
    "open-ownership": "https://bods-data.openownership.org/",
}
SOURCE_PROVIDER = {"gleif-level2": "gleif", "companies-house": "companies-house", "sec-edgar-ownership": "sec-edgar"}


def live_manifest() -> dict:
    manifest = json.loads((REPO_ROOT / "config/source_packs/corporate-ownership.json").read_text())
    manifest = copy.deepcopy(manifest)
    manifest["pack_id"] = "corporate-ownership-live-check"
    manifest["sources"] = [s for s in manifest["sources"] if s["source_id"] in SELECTION]
    for source in manifest["sources"]:
        overrides = SELECTION[source["source_id"]]
        for key, value in overrides.items():
            source[key] = {**source.get(key, {}), **value}
        source.setdefault("ownership", {})["namespace"] = NAMESPACE
    return manifest


def probe(url: str) -> dict:
    """One GET through the environment's egress path; records reachability, never parsed as evidence."""
    started = time.time()
    request = urllib.request.Request(url, headers={"User-Agent": "Noesis ownership live-check connectivity probe",
                                                   "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return {"url": url, "reachable": True, "http_status": response.status,
                    "elapsed_seconds": round(time.time() - started, 2)}
    except urllib.error.HTTPError as exc:
        return {"url": url, "reachable": True, "http_status": exc.code, "elapsed_seconds": round(time.time() - started, 2)}
    except Exception as exc:  # noqa: BLE001 - the failure is the evidence
        reason = getattr(exc, "reason", exc)
        return {"url": url, "reachable": False, "error_type": type(exc).__name__, "error": str(reason)[:300],
                "elapsed_seconds": round(time.time() - started, 2)}


def run(output: Path, *, environment_note: str = "") -> dict:
    import duckdb

    from src.ingestion.ownership_providers import PROVIDER_CONTRACTS
    from src.ingestion.source_pack_runtime import SourcePackRuntime
    from src.ingestion.source_packs import SourcePackStore, validate_source_pack
    from src.kb.ownership_store import OwnershipStore

    conn = duckdb.connect()
    manifest = validate_source_pack(live_manifest())
    SourcePackStore(conn).install(manifest, principal_id="live-check", enable=True)
    runtime = SourcePackRuntime(conn)
    for source in manifest["sources"]:
        runtime.accept_license(manifest["pack_id"], source["source_id"], principal_id="live-check")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = {"contract": "noesis-ownership-live-check-v1", "evidence_kind": "live",
              "started_at": datetime.now(UTC).isoformat(), "environment": environment_note,
              "selection": SELECTION, "sources": {}, "probes": {},
              "note": "Live evidence only; offline fixture results are reported separately by the unit and acceptance "
                      "tests. Nothing here is a beneficial-ownership, sanctions or AML determination."}
    for source in manifest["sources"]:
        operation = source["operations"][0]
        started = time.time()
        try:
            receipt = runtime.run({"pack_id": manifest["pack_id"], "run_key": f"live-{stamp}-{source['source_id']}",
                                   "operation": operation, "source_ids": [source["source_id"]], "network": "live",
                                   "max_results": 200, "max_bytes": 20_000_000, "max_pages": 12, "timeout_ms": 60_000},
                                  principal_id="live-check", secret_resolver=lambda ref: os.environ.get(ref))
            error = None
        except Exception as exc:  # noqa: BLE001 - every failure is evidence
            receipt, error = None, {"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)[:300]}
        row = conn.execute("SELECT status, pages, fetched, failure_json FROM source_pack_source_runs WHERE run_id=? AND "
                           "source_id=?", [receipt["run_id"], source["source_id"]]).fetchone() if receipt else None
        failures = [f for f in (receipt or {}).get("failures") or [] if f.get("source_id") == source["source_id"]]
        report["sources"][source["source_id"]] = {
            "provider": SOURCE_PROVIDER[source["source_id"]],
            "access_contract": PROVIDER_CONTRACTS[SOURCE_PROVIDER[source["source_id"]]]["access"],
            "run_status": (receipt or {}).get("status"), "run_error": error,
            "source_status": row[0] if row else None, "pages": row[1] if row else 0, "fetched": row[2] if row else 0,
            "failure": json.loads(row[3]) if row and row[3] else None, "preflight_failures": failures,
            "elapsed_seconds": round(time.time() - started, 2),
        }
    counts = {}
    if conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='ownership_records'").fetchone():
        counts = dict(conn.execute("SELECT provider, count(*) FROM ownership_records GROUP BY provider").fetchall())
    report["ownership_records_by_provider"] = counts
    if conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='lei_current'").fetchone():
        report["gleif_projection"] = OwnershipStore(conn).project_gleif(
            NAMESPACE, SELECTION["gleif-level2"]["lei"]["leis"], lei_namespace=NAMESPACE, run_id=f"live-{stamp}",
            principal_id="live-check")
    for provider, url in PROBES.items():
        report["probes"][provider] = probe(url)
    report["open-ownership"] = {"attempted": False, "reason": "no BODS dataset path has been verified from this "
                                                                "runtime; only a host probe is recorded"}
    report["finished_at"] = datetime.now(UTC).isoformat()

    def code(value: dict) -> str:
        if value["run_error"]:
            return value["run_error"]["code"]
        if value["failure"]:
            return "transport:" + (value["failure"].get("code") if isinstance(value["failure"], dict) else str(value["failure"]))
        preflight = [d for f in value["preflight_failures"] if f.get("classification") == "preflight"
                     for d in f.get("detail") or []]
        if preflight:
            return "preflight:" + ",".join(preflight)
        return "ok" if value["source_status"] == "complete" else str(value["source_status"])

    report["summary"] = {sid: code(v) for sid, v in report["sources"].items()}
    report["summary"]["open-ownership-bods"] = "not attempted; probe " + (
        "reachable" if report["probes"]["open-ownership"]["reachable"] else "unreachable: "
        + report["probes"]["open-ownership"]["error_type"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--environment-note", default="", help="where the check ran (network policy, proxy)")
    args = parser.parse_args()
    report = run(args.output, environment_note=args.environment_note)
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
