#!/usr/bin/env python3
"""Bounded live checks for the Climate and Environment providers (E14).

Runs one small acquisition per provider under its recorded access contract
(``PROVIDER_CONTRACTS``) through the real ``DurableHTTP`` runtime and writes a
JSON report that keeps live results strictly apart from offline fixture
evidence. For each provider the report records the request receipts (URL,
HTTP status or exact failure code/type, observation time, ``last-modified``/
``etag`` where sent), parsed record counts and kinds, or the failure.

* Credentialed providers (OpenAQ, ENTSO-E) use ``NOESIS_OPENAQ_API_KEY`` /
  ``NOESIS_ENTSOE_SECURITY_TOKEN`` when set. Without a credential the check
  sends one keyless reachability probe to the documented endpoint and records
  ``credential_missing`` next to the transport outcome; it never guesses keys.
* Berlin Umweltatlas layers are probed with one WFS ``GetCapabilities``
  request per declared service (the acquisition path itself is the existing
  WFS adapter).
* Copernicus/CAMS is not implemented and is reported as such, without a request.

    python scripts/environment_live_check.py --output docs/development/environment-evidence/live-check-<date>.json
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

NAMESPACE = "environment-live-check"
SCOPES = {"knowledge:environment:read", "knowledge:environment:write", "knowledge:ingestion:execute",
          f"namespace:{NAMESPACE}:read", f"namespace:{NAMESPACE}:write"}
PACK = REPO_ROOT / "packs/climate-environment/source_packs/climate-environment.json"
UPGRADE = REPO_ROOT / "packs/climate-environment/source_packs/geospatial-berlin-1.2.0.json"


def _receipts(conn, budget_id):
    rows = conn.execute("SELECT receipt_json FROM provider_execution_requests WHERE budget_id=? ORDER BY request_key",
                        [budget_id]).fetchall()
    result = []
    for (raw,) in rows:
        receipt = json.loads(raw)
        result.append({"url": receipt["request"]["url"], "state": receipt["state"], "http_status": receipt.get("http_status"),
                       "failure_code": receipt.get("failure_code"), "failure_type": receipt.get("failure_type"),
                       "observed_at_ms": receipt["observed_at_ms"], "response_headers": receipt.get("response_headers"),
                       "execution": receipt["execution"], "bytes": receipt.get("bytes")})
    return result


def _failure(exc):
    return {"failure_code": getattr(exc, "code", type(exc).__name__), "failure_type": type(exc).__name__}


def run(output, *, reuse_notice, environment_note=""):
    import duckdb

    from src.config.env import resolve_env
    from src.ingestion.environment_providers import (
        LIVE_VERIFICATION,
        PROVIDER_CONTRACTS,
        PROVIDER_HOSTS,
        SECRETS,
        EnvironmentClient,
        acquire,
        plan,
    )
    from src.ingestion.provider_execution import DurableHTTP
    from src.kb.environment_store import EnvironmentEvidenceStore

    conn = duckdb.connect()
    store = EnvironmentEvidenceStore(conn)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = {"contract": "noesis-environment-live-check-v1", "evidence_kind": "live",
              "started_at": datetime.now(UTC).isoformat(), "environment": environment_note, "providers": {},
              "live_verification_before": {p: v["status"] for p, v in LIVE_VERIFICATION.items()},
              "note": "Live evidence only; offline fixture results are reported separately by the unit and acceptance tests."}
    selections = {s["environment"]["provider"]: s["environment"]["selection"] for s in json.loads(PACK.read_text())["sources"]}
    for provider, selection in selections.items():
        budget_id = f"live-{provider}-{stamp}"
        http = DurableHTTP(conn, budget_id=budget_id, provider=provider, principal_id="live-check",
                           allowed_hosts=PROVIDER_HOSTS[provider], reuse_notice=reuse_notice, max_requests=12,
                           max_bytes=100_000_000)
        secret = resolve_env(SECRETS[provider][2]) if provider in SECRETS else None
        started = time.time()
        credential = None if provider not in SECRETS else ("present" if secret else "missing")
        records, result = [], None
        try:
            if provider in SECRETS and not secret:
                step = plan(provider, selection)[0]
                try:
                    http.request(f"live-{stamp}:probe", step["url"], principal_id="live-check", params=step["params"],
                                 max_bytes=2_000_000)
                    result = {"ok": False, "failure": {"failure_code": "credential_missing",
                                                       "note": "host reachable without a key; no data acquired"}}
                except Exception as exc:  # noqa: BLE001 - every failure is evidence
                    result = {"ok": False, "failure": {"failure_code": "credential_missing",
                                                       "transport": _failure(exc)}}
            else:
                client = EnvironmentClient(http, principal_id="live-check", secret=secret)
                result = acquire(client, selection, namespace=NAMESPACE, scopes=SCOPES, reuse_notice=reuse_notice,
                                 observation=f"live-{stamp}-{provider}", principal_id="live-check", store=store)
                if result["ok"]:
                    records = [{"record_type": r["record_type"], "kind": r["kind"], "provider": r["provider"]}
                               for r in store.store.records(NAMESPACE, scopes=SCOPES, provider=provider)]
        except Exception as exc:  # noqa: BLE001 - every failure is evidence
            result = {"ok": False, "failure": _failure(exc)}
        report["providers"][provider] = {
            "access_contract": PROVIDER_CONTRACTS[provider]["access"], "credential": credential,
            "ok": result["ok"], "failure": result.get("failure"), "elapsed_seconds": round(time.time() - started, 2),
            "requests": _receipts(conn, budget_id), "records": records, "coverage": result.get("coverage"),
        }
    # Umweltatlas: one GetCapabilities per declared WFS service (acquisition itself is the existing WFS adapter).
    upgrade = json.loads(UPGRADE.read_text())
    layers = [s for s in upgrade["sources"] if s["source_id"].startswith("umweltatlas-")]
    budget_id = f"live-umweltatlas-{stamp}"
    http = DurableHTTP(conn, budget_id=budget_id, provider="umweltatlas", principal_id="live-check",
                       allowed_hosts=PROVIDER_HOSTS["umweltatlas"], reuse_notice=reuse_notice, max_requests=len(layers))
    started, failures = time.time(), []
    for source in layers:
        try:
            http.request(f"live-{stamp}:{source['source_id']}", source["endpoint"], principal_id="live-check",
                         params={"SERVICE": "WFS", "REQUEST": "GetCapabilities", "VERSION": "2.0.0"},
                         headers={"Accept": "application/xml"}, max_bytes=5_000_000)
        except Exception as exc:  # noqa: BLE001 - every failure is evidence
            failures.append({"source_id": source["source_id"], **_failure(exc)})
    report["providers"]["umweltatlas"] = {
        "access_contract": PROVIDER_CONTRACTS["umweltatlas"]["access"], "credential": None,
        "ok": not failures, "failure": failures[0] if failures else None, "failures": failures,
        "elapsed_seconds": round(time.time() - started, 2), "requests": _receipts(conn, budget_id), "records": [],
        "coverage": None}
    report["providers"]["copernicus-cams"] = {"access_contract": "not implemented", "ok": False,
                                              "failure": {"failure_code": "not_implemented",
                                                          "reason": PROVIDER_CONTRACTS["copernicus-cams"]["reason"]},
                                              "requests": [], "records": []}
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["summary"] = {p: ("ok" if v["ok"] else (v["failure"] or {}).get("failure_code")) for p, v in report["providers"].items()}
    report["transport_failures"] = {p: sorted({r["failure_type"] for r in v["requests"] if r.get("failure_type")})
                                    for p, v in report["providers"].items()}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--environment-note", default="", help="where the check ran (network policy, proxy)")
    parser.add_argument("--reuse-notice", default="Public environmental data read for coverage validation; cite sources.")
    args = parser.parse_args()
    report = run(args.output, reuse_notice=args.reuse_notice, environment_note=args.environment_note)
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
