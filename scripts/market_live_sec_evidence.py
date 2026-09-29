"""Record live SEC EDGAR evidence for the market acceptance packs.

This is an operator tool, not a CI test: it makes real SEC requests and writes
a live-source pack separate from the deterministic fixture pack. It never
stores filing payloads, only accessions, locators, counts and diagnostics.

SEC fair-access rules require a descriptive ``NOESIS_EDGAR_USER_AGENT``. The
script sleeps between issuers so it stays well below SEC request limits.

Usage::

    NOESIS_EDGAR_USER_AGENT="Noesis research bot (market fact reconciliation)" \\
        python scripts/market_live_sec_evidence.py statements \\
        --output config/market/acceptance_packs/live-sec-statements.json
    python scripts/market_live_sec_evidence.py materials \\
        --output config/market/acceptance_packs/live-sec-materials.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_TICKERS = ("MSFT", "ORCL", "CRM", "ADBE", "NOW")
REQUEST_PAUSE_SECONDS = 1.0


def _latest_accessions(submissions: dict, forms: tuple[str, ...]) -> dict[str, str]:
    recent = (submissions.get("filings") or {}).get("recent") or {}
    latest: dict[str, str] = {}
    for form, accession in zip(recent.get("form") or [], recent.get("accessionNumber") or []):
        if form in forms and form not in latest:
            latest[form] = accession
    return latest


def statements(tickers: tuple[str, ...], forms: tuple[str, ...]) -> dict:
    from src.ingestion.connectors.edgar import (
        EdgarClient,
        reconcile_market_financial_facts_with_sec,
    )

    client = EdgarClient()
    if not client.configured:
        raise SystemExit("set NOESIS_EDGAR_USER_AGENT to a descriptive SEC User-Agent")
    rows = []
    for ticker in tickers:
        cik = client.resolve_ticker(ticker)
        if cik is None:
            rows.append({"ticker": ticker, "status": "unresolved_ticker"})
            continue
        for form, accession in _latest_accessions(client.submissions(cik), forms).items():
            started = time.monotonic()
            try:
                receipt = reconcile_market_financial_facts_with_sec(
                    cik,
                    issuer_id=f"issuer:{ticker.lower()}",
                    namespace="market:live-sec-evidence",
                    accession=accession,
                    client=client,
                )
            except Exception as exc:  # recorded, never converted to a pass
                rows.append({
                    "ticker": ticker,
                    "cik": cik,
                    "form": form,
                    "accession": accession,
                    "status": "error",
                    "error": type(exc).__name__,
                })
                continue
            recon = receipt["reconciliation"]
            rows.append({
                "ticker": ticker,
                "cik": cik,
                "form": receipt["filing_form"] or form,
                "accession": accession,
                "filing_url": receipt["filing_url"],
                "status": "consistent" if recon["value_status"] == "consistent" else "review_required",
                "compared_facts": recon["filing_fact_count"],
                "matched": recon["matched"],
                "mismatched": recon["mismatched"],
                "missing_normalized": recon["missing_normalized"],
                "unmatched_normalized": recon["unmatched_normalized"],
                "conflicting_contexts": recon["conflicting_contexts"],
                "unsupported_mapping_rows": recon["unsupported_mappings"],
                "coverage": {
                    key: value
                    for key, value in recon["coverage"].items()
                    if key != "dimensional_concepts"
                },
                "inline_diagnostics": dict(
                    Counter(item.get("code") for item in receipt["inline_xbrl"]["diagnostics"])
                ),
                "value_diagnostics": [
                    item
                    for item in recon["diagnostics"]
                    if item.get("code") != "unsupported_accounting_mapping"
                ][:25],
                "seconds": round(time.monotonic() - started, 1),
            })
            time.sleep(REQUEST_PAUSE_SECONDS)
    consistent = [row for row in rows if row.get("status") == "consistent"]
    return {
        "pack_id": "market-live-sec-statements-v1",
        "status": "live_verified" if rows and len(consistent) == len(rows) else "live_review_required",
        "evidence_kind": "live_provider",
        "provider": "sec_edgar",
        "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "check": "companyfacts_vs_inline_xbrl_statement_reconciliation",
        "universe": list(tickers),
        "forms": list(forms),
        "totals": {
            "filings": len(rows),
            "consistent_filings": len(consistent),
            "compared_facts": sum(row.get("compared_facts", 0) for row in rows),
            "matched": sum(row.get("matched", 0) for row in rows),
            "mismatched": sum(row.get("mismatched", 0) for row in rows),
        },
        "rows": rows,
        "limitations": [
            "Compares SEC CompanyFacts with the same accession's Inline XBRL; it does not validate a commercial price or fundamentals vendor.",
            "Dimensional (segment/member) facts and filer extension taxonomies are counted as outside CompanyFacts coverage, not compared.",
            "No analyst usefulness review is implied by this pack.",
        ],
    }


def materials(tickers: tuple[str, ...]) -> dict:
    from src.ingestion.connectors.edgar import EdgarClient
    from src.ingestion.connectors.edgar_materials import harvest_sec_company_materials

    client = EdgarClient()
    if not client.configured:
        raise SystemExit("set NOESIS_EDGAR_USER_AGENT to a descriptive SEC User-Agent")
    rows = []
    for ticker in tickers:
        try:
            harvest = harvest_sec_company_materials(
                ticker, issuer_id=f"issuer:{ticker.lower()}", client=client
            )
        except Exception as exc:  # recorded, never converted to a pass
            rows.append({"ticker": ticker, "status": "error", "error": type(exc).__name__})
            continue
        acquired = [row for row in harvest["materials"] if row["licensing_status"] == "public"]
        guidance = [row for row in acquired if row["kind"] == "guidance"]
        rows.append({
            "ticker": ticker,
            "cik": harvest["cik"],
            "status": "acquired" if harvest["coverage"]["earnings_release"] else "missing_earnings_materials",
            "coverage": harvest["coverage"],
            "unavailable": harvest["unavailable"],
            "earnings_periods": sorted({row["reporting_period"] for row in acquired if row["kind"] == "earnings_release"}),
            "guidance_statements": sum(len(row["statements"]) for row in guidance),
            "guidance_example": guidance[-1]["statements"][0]["text"][:240] if guidance else None,
            "segment_members": next((row["segment_members"] for row in acquired if row["kind"] == "segment_disclosure"), []),
            "corrections": sum(1 for row in acquired if row.get("corrects_material_id")),
            "duplicates": sum(1 for row in acquired if row.get("duplicate_of")),
            "sample_locators": [row["document_locator"] for row in acquired if row["kind"] == "earnings_release"][:2],
            "diagnostics": harvest["diagnostics"],
        })
    complete = [row for row in rows if row.get("status") == "acquired"]
    return {
        "pack_id": "market-live-sec-materials-v1",
        "status": "live_verified" if rows and len(complete) == len(rows) else "live_review_required",
        "evidence_kind": "live_provider",
        "provider": "sec_edgar",
        "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "check": "company_materials_acquisition",
        "universe": list(tickers),
        "rows": rows,
        "limitations": [
            "Guidance statements are keyword-and-number matched management claims, not verified figures.",
            "Transcripts, analyst consensus and 13F holdings are recorded unavailable; no licensed provider was used.",
            "No analyst usefulness review is implied by this pack.",
        ],
    }


def dossiers(tickers: tuple[str, ...]) -> dict:
    import duckdb

    from src.domains.market.dossier_inputs import build_dossier_inputs, quarterly_revenue_from_facts
    from src.domains.market.research import MarketResearchStore
    from src.ingestion.connectors.edgar import EdgarClient, harvest_market_financial_facts
    from src.ingestion.connectors.edgar_materials import harvest_sec_company_materials

    client = EdgarClient()
    if not client.configured:
        raise SystemExit("set NOESIS_EDGAR_USER_AGENT to a descriptive SEC User-Agent")
    namespace = "market:live-sec-evidence"
    scopes = {"market:research:read", "market:research:write", f"namespace:{namespace}:write", f"namespace:{namespace}:read"}
    rows = []
    for ticker in tickers:
        issuer = f"issuer:{ticker.lower()}"
        try:
            harvest = harvest_sec_company_materials(ticker, issuer_id=issuer, client=client, max_insider_filings=3, max_ownership_filings=3)
            facts = harvest_market_financial_facts(ticker, issuer_id=issuer, namespace=namespace, client=client)
        except Exception as exc:  # recorded, never converted to a pass
            rows.append({"ticker": ticker, "status": "error", "error": type(exc).__name__})
            continue
        now = int(time.time() * 1000)
        store = MarketResearchStore(duckdb.connect(":memory:"), now=lambda: now)
        materials_artifact = store.save_materials(
            namespace, issuer_id=issuer, artifact_id=f"materials:{ticker}", version=1,
            materials=harvest["materials"], cutoff_ms=now, owner=None, principal_id="evidence", scopes=scopes,
        )
        inputs = build_dossier_inputs(
            materials_artifact,
            quarterly_revenue=quarterly_revenue_from_facts(facts["facts"]),
            release_texts=harvest["release_texts"],
        )
        dossier = store.company_dossier(
            namespace, issuer_id=issuer, artifact_id=f"dossier:{ticker}", version=1, statements=[],
            materials=inputs["materials"], comparisons=inputs["comparisons"], evidence=inputs["evidence"],
            cutoff_ms=now, owner=None, principal_id="evidence", scopes=scopes,
            segment_disclosures=inputs["segment_disclosures"], ownership=inputs["ownership"],
        )
        checks = inputs["headline_reconciliation"]
        rows.append({
            "ticker": ticker,
            "status": "reconciled" if checks and all(c["status"] != "mismatch" for c in checks) else "review_required",
            "headline_reconciliation": [
                {k: c[k] for k in ("reporting_period", "release_value", "filed_value", "tolerance", "status", "filed_derivation")}
                for c in checks
            ],
            "guidance_comparisons": [
                {"range": c["guidance"]["range_text"], "actual": c["actual"], "outcome": c["guidance"]["outcome"]}
                for c in dossier["comparisons"] if c.get("guidance")
            ],
            "management_claims": len(dossier["uncertainty"]["management_claims"]),
            "gaps": sorted({gap["code"] for gap in inputs["gaps"]}),
            "dossier_source_gaps": dossier["uncertainty"]["source_gaps"],
            "unavailable_sources": inputs["unavailable_sources"],
            "dossier_record_hash": dossier["record_hash"],
        })
        time.sleep(REQUEST_PAUSE_SECONDS)
    reconciled = [row for row in rows if row.get("status") == "reconciled"]
    return {
        "pack_id": "market-live-sec-dossiers-v1",
        "status": "live_verified" if rows and len(reconciled) == len(rows) else "live_review_required",
        "evidence_kind": "live_provider",
        "provider": "sec_edgar",
        "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "check": "company_dossier_headline_reconciliation_and_guidance",
        "universe": list(tickers),
        "rows": rows,
        "limitations": [
            "Headline figures are compared with filed quarterly revenue at the precision the release states.",
            "Guidance comparisons exist only where a release states a quarterly revenue range; consensus is unlicensed and unavailable.",
            "No analyst usefulness review is implied by this pack.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    stmt = sub.add_parser("statements", help="reconcile latest 10-K/10-Q statements")
    stmt.add_argument("--tickers", nargs="+", default=list(DEFAULT_TICKERS))
    stmt.add_argument("--forms", nargs="+", default=["10-K", "10-Q"])
    stmt.add_argument("--output", type=Path, required=True)
    mat = sub.add_parser("materials", help="acquire earnings, guidance, segment and ownership materials")
    mat.add_argument("--tickers", nargs="+", default=list(DEFAULT_TICKERS))
    mat.add_argument("--output", type=Path, required=True)
    dos = sub.add_parser("dossiers", help="build dossiers and reconcile release headlines to filings")
    dos.add_argument("--tickers", nargs="+", default=list(DEFAULT_TICKERS))
    dos.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "dossiers":
        pack = dossiers(tuple(args.tickers))
    elif args.command == "materials":
        pack = materials(tuple(args.tickers))
    else:
        pack = statements(tuple(args.tickers), tuple(args.forms))
    args.output.write_text(json.dumps(pack, indent=1, sort_keys=False) + "\n")
    print(json.dumps(pack.get("totals") or {row["ticker"]: row["status"] for row in pack["rows"]}))
    return 0 if pack["status"] == "live_verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
