#!/usr/bin/env python3
"""Bounded live checks for the Public Procurement providers.

Runs each implemented source of the ``procurement`` source pack once through
the real source-pack runtime with ``network: live`` (budget: one page, one
source per run) and writes a JSON report that keeps live results strictly
apart from offline fixture evidence. Per provider the report records the
observation time, the runtime preflight (DNS/network policy, credential,
licence), the source receipt (status, pages, records, failure code and
classification, HTTP metadata from the adapter receipt) and, when records
were read, the parsed notices with deadlines as published and requirement
quotes to verify by hand against the official notice.

Only public notice data is read. No bid is submitted and no buyer, portal or
contracting authority is contacted. SAM.gov needs the ``NOESIS_SAM_API_KEY``
environment variable; without it the source is recorded as blocked
(``credential_missing``), never skipped silently.

    python scripts/procurement_live_check.py --output docs/development/procurement-evidence/live-check.json
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

SCOPES = {"knowledge:procurement:read", "namespace:procurement:read", "operator"}


def _records(conn, provider):
    from src.kb.procurement_notices import ProcurementNoticeStore

    store = ProcurementNoticeStore(conn, initialize=False)
    return [{
        "procedure_id": p["procedure_id"], "notice_id": p["cause"]["notice_id"], "stage": p["cause"]["stage"],
        "title": p["record"]["title"], "source_url": p["record"]["source_url"], "derived_state": p["status"]["state"],
        "next_deadline_as_published": (p["status"]["next_deadline"] or {}).get("text"),
        "requirement_quotes_to_verify": [{"requirement_id": r["requirement_id"], "quote": (r.get("locator") or {}).get("quote")}
                                         for r in (p["record"].get("requirements") or [])[:3]],
        "unknowns": p["record"]["unknowns"],
    } for p in store.list("procurement", scopes=SCOPES, providers=[provider])[:10]]


def _probe(source):
    """One direct request through the same native adapter, keeping the exact error code and transport detail."""
    from src.ingestion.source_pack_runtime import RuntimeAdapterFactory

    secret = os.environ.get((source.get("auth") or {}).get("secret_ref") or "")
    try:
        adapter = RuntimeAdapterFactory().compile(source, secret=secret)
        page = adapter.fetch_page({"operation": "notices", "parameters": {}}, cursor=None)
        return {"ok": True, "records": len(page.records), "receipt": {k: v for k, v in dict(page.receipt or {}).items() if k != "request"}}
    except Exception as exc:  # noqa: BLE001 - every failure is evidence
        details = getattr(exc, "details", {}) or {}
        return {"ok": False, "code": getattr(exc, "code", type(exc).__name__), "message": str(exc)[:300],
                "transport_detail": details.get("transport_detail")}


def run(output, *, environment_note=""):
    import duckdb

    from src.ingestion.procurement_providers import PROVIDER_CONTRACTS
    from src.ingestion.source_pack_runtime import SourcePackRuntime
    from src.ingestion.source_packs import SourcePackStore, validate_source_pack

    conn = duckdb.connect()
    pack = validate_source_pack(json.loads((REPO_ROOT / "config/source_packs/procurement.json").read_text()))
    SourcePackStore(conn).install(pack, principal_id="live-check", enable=True, now_ms=int(time.time() * 1000))
    runtime = SourcePackRuntime(conn, sleep=lambda _d: None)
    for source in pack["sources"]:
        runtime.accept_license("procurement", source["source_id"], principal_id="live-check")
    secret_resolver = lambda ref: os.environ.get(ref)  # noqa: E731 - the key is read, never recorded
    report = {"contract": "noesis-procurement-live-check-v1", "evidence_kind": "live", "started_at": datetime.now(UTC).isoformat(),
              "environment": environment_note, "providers": {},
              "note": "Live evidence only; offline fixture results are reported separately by the unit and acceptance tests.",
              "not_implemented": {p: c["reason"] for p, c in PROVIDER_CONTRACTS.items() if c["status"] != "implemented"}}
    for source in pack["sources"]:
        provider = source["procurement"]["provider"]
        request = {"pack_id": "procurement", "run_key": f"live-{provider}-{time.time_ns()}", "operation": "notices",
                   "source_ids": [source["source_id"]], "network": "live", "max_results": 100, "max_bytes": 20_000_000,
                   "max_pages": 1, "timeout_ms": 60_000, "retries": 0}
        preflight = runtime.preflight(request, secret_available=lambda ref: bool(os.environ.get(ref)))["sources"][0]
        started = time.time()
        try:
            receipt = runtime.run(request, principal_id="live-check", secret_resolver=secret_resolver)
            source_receipt = next((s for s in receipt.get("sources") or [] if s["source_id"] == source["source_id"]), None)
            if source_receipt is None:
                # The runtime did not execute the source: its preflight blocked it.
                result = {"run_status": receipt["status"], "source_status": "blocked",
                          "failure": {"code": (preflight["failures"] or ["not_executed"])[0], "stage": "preflight"}}
            else:
                result = {"run_status": receipt["status"], "source_status": source_receipt["status"],
                          "counts": {k: source_receipt["counts"][k] for k in ("pages", "fetched", "normalized", "bytes")},
                          "failure": source_receipt.get("failure"), "retries": source_receipt.get("retries")}
        except Exception as exc:  # noqa: BLE001 - every failure is evidence
            result = {"run_status": "error", "source_status": "failed",
                      "failure": {"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)[:300]}}
        result["adapter_probe"] = _probe(source)
        ok = result.get("source_status") == "complete" and (result.get("counts") or {}).get("fetched", 0) > 0
        report["providers"][provider] = {
            "source_id": source["source_id"], "endpoint": source["endpoint"], "access_contract": PROVIDER_CONTRACTS[provider]["access"],
            "observed_at": datetime.now(UTC).isoformat(), "elapsed_seconds": round(time.time() - started, 2),
            "preflight": {"ready": preflight["ready"], "failures": preflight["failures"],
                          "authentication": {k: v for k, v in preflight["authentication"].items() if k != "value"}},
            "ok": ok, **result, "records": _records(conn, provider) if ok else [],
        }
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["summary"] = {p: ("ok" if v["ok"] else {"runtime": (v.get("failure") or {}).get("code") or v["source_status"],
                                                   "adapter": v["adapter_probe"].get("code")})
                         for p, v in report["providers"].items()}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--environment-note", default="", help="where the check ran (network policy, proxy)")
    args = parser.parse_args()
    report = run(args.output, environment_note=args.environment_note)
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
