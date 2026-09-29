#!/usr/bin/env python3
"""Bounded live checks for the Funding & Grants providers.

Runs one small acquisition per provider under its recorded access contract
(``PROVIDER_CONTRACTS``) through the real ``DurableHTTP`` runtime, and writes
a JSON report that keeps live results strictly apart from offline fixture
evidence. For each provider the report records the observation time, HTTP
metadata (``last-modified``/``etag`` where sent), parsed record counts, the
open status, dates and a sample of rule citations to verify by hand against
the official page — or the exact failure code when the source was
unreachable, blocked or changed shape.

Nothing is submitted and no funder is contacted; only public pages are read.

    python scripts/funding_live_check.py --output docs/development/funding-evidence/live-check.json
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

NAMESPACE = "funding-live-check"
SCOPES = {"knowledge:funding:read", "knowledge:funding:write", "knowledge:ingestion:execute",
          f"namespace:{NAMESPACE}:read", f"namespace:{NAMESPACE}:write"}
SELECTIONS = [
    ("nlnet", "nlnet_calls", (), {}),
    ("eu-ft", "eu_search_all", (), {"text": "open source", "page_size": 20, "max_pages": 1}),
    ("foerderdatenbank", "foerderdatenbank_programme",
     ("https://www.foerderdatenbank.de/FDB/Content/DE/Foerderprogramm/Bund/BMWi/exist-gruendungsstipendium.html",), {}),
    ("exist", "exist_programme", ("https://www.exist.de/EXIST/Navigation/EN/Start-up-grant/start-up-grant.html",), {}),
]


def _summarize(conn, provider, result):
    from src.kb.funding_opportunities import FundingOpportunityStore

    store = FundingOpportunityStore(conn, initialize=False)
    items = [o for o in store.list(NAMESPACE, scopes=SCOPES, providers=[provider])]
    return [{
        "opportunity_id": o["opportunity_id"], "title": o["record"]["title"], "source_url": o["record"]["source_url"],
        "record_kind": o["record"]["record_kind"], "derived_state": o["status"]["state"],
        "next_deadline": o["status"].get("next_deadline"),
        "rule_citations_to_verify": [{"requirement_id": r["requirement_id"], "quote": r["locator"].get("quote")}
                                     for r in (o["record"].get("requirements") or [])[:3]],
        "unknowns": o["record"]["unknowns"],
    } for o in items[:10]]


def run(output, *, reuse_notice, environment_note=""):
    import duckdb

    from src.ingestion.funding_providers import PROVIDER_CONTRACTS, PROVIDER_HOSTS, FundingClient, FundingEvidenceStore, acquire
    from src.ingestion.provider_execution import DurableHTTP

    conn = duckdb.connect()
    store = FundingEvidenceStore(conn)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = {"contract": "noesis-funding-live-check-v1", "evidence_kind": "live",
              "started_at": datetime.now(UTC).isoformat(), "environment": environment_note, "providers": {},
              "note": "Live evidence only; offline fixture results are reported separately by the unit/acceptance tests."}
    for provider, method, args, kwargs in SELECTIONS:
        http = DurableHTTP(conn, budget_id=f"live-{provider}-{stamp}", provider=provider, principal_id="live-check",
                           allowed_hosts=PROVIDER_HOSTS[provider], reuse_notice=reuse_notice, max_requests=5,
                           max_bytes=50_000_000)
        client = FundingClient(http, principal_id="live-check")
        started = time.time()
        try:
            result = acquire(client, provider, lambda: getattr(client, method)(*args, f"live-{stamp}", **kwargs),
                             namespace=NAMESPACE, scopes=SCOPES, reuse_notice=reuse_notice, observation=f"live-{stamp}-{provider}",
                             store=store)
        except Exception as exc:  # noqa: BLE001 - every failure is evidence
            result = {"ok": False, "failure": {"failure_code": getattr(exc, "code", type(exc).__name__)}}
        receipts = [json.loads(row[0]) for row in conn.execute(
            "SELECT receipt_json FROM provider_execution_requests WHERE budget_id=?", [http.budget_id]).fetchall()]
        report["providers"][provider] = {
            "access_contract": PROVIDER_CONTRACTS[provider]["access"],
            "ok": result["ok"], "failure": result.get("failure"),
            "elapsed_seconds": round(time.time() - started, 2),
            "requests": [{"url": r["request"]["url"], "state": r["state"], "http_status": r.get("http_status"),
                          "failure_code": r.get("failure_code"), "failure_type": r.get("failure_type"), "observed_at_ms": r["observed_at_ms"],
                          "response_headers": r.get("response_headers"), "execution": r["execution"]} for r in receipts],
            "records": _summarize(conn, provider, result) if result["ok"] else [],
            "coverage": result.get("coverage"),
        }
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["summary"] = {p: ("ok" if v["ok"] else (v["failure"] or {}).get("failure_code")) for p, v in report["providers"].items()}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--environment-note", default="", help="where the check ran (network policy, proxy)")
    parser.add_argument("--reuse-notice", default="Public funder pages read for coverage validation; cite source URLs.")
    args = parser.parse_args()
    report = run(args.output, reuse_notice=args.reuse_notice, environment_note=args.environment_note)
    print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
