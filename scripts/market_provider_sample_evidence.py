"""Evaluate a vendor-authorized market-data sample without retaining payloads.

The operator supplies a normalized manifest containing request/response hashes,
coverage declarations and rights answers obtained under an evaluation license.
This script never calls a vendor and never treats fixture evidence or a missing
credential as live validation.

Usage::

    python scripts/market_provider_sample_evidence.py \
        --manifest provider-sample.json \
        --output config/market/acceptance_packs/live-price-provider.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

TARGET_TICKERS = ("MSFT", "ORCL", "CRM", "ADBE", "NOW")
COVERAGE_AREAS = (
    "prices",
    "corporate_actions",
    "fundamentals",
    "estimates",
    "transcripts",
    "ownership",
)
RIGHTS_AREAS = (
    "local_retention",
    "internal_display",
    "external_display",
    "derived_data",
    "report_export",
)
HASH_LENGTH = 64


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _valid_hash(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == HASH_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _check(name: str, passed: bool, details: Any, *, blocked: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "status": "passed" if passed else ("blocked" if blocked else "failed"),
        "details": details,
    }


def evaluate(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return a payload-free, deterministic coverage and rights receipt."""

    if manifest.get("contract") != "noesis-market-provider-sample-manifest-v1":
        raise ValueError("manifest contract must be noesis-market-provider-sample-manifest-v1")
    provider = manifest.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider must be nonempty text")
    evidence_kind = manifest.get("evidence_kind")
    if evidence_kind not in {"live_provider", "fixture"}:
        raise ValueError("evidence_kind must be live_provider or fixture")

    authorization = manifest.get("authorization") or {}
    access_status = authorization.get("status")
    authorized = access_status == "authorized" and bool(authorization.get("license_reference"))
    blocked = access_status in {"credential_blocked", "not_requested"}

    rights = manifest.get("rights") or {}
    rights_values = {area: rights.get(area) for area in RIGHTS_AREAS}
    rights_explicit = all(value in {"confirmed", "denied"} for value in rights_values.values())

    receipt_rows = manifest.get("receipts") or []
    receipts: dict[str, dict[str, Any]] = {}
    malformed_receipts: list[str] = []
    for index, row in enumerate(receipt_rows):
        receipt_id = row.get("receipt_id") if isinstance(row, dict) else None
        valid = (
            isinstance(receipt_id, str)
            and bool(receipt_id)
            and receipt_id not in receipts
            and isinstance(row.get("endpoint"), str)
            and bool(row.get("endpoint"))
            and type(row.get("http_status")) is int
            and 200 <= row["http_status"] < 300
            and type(row.get("records")) is int
            and row["records"] >= 0
            and _valid_hash(row.get("request_sha256"))
            and _valid_hash(row.get("response_sha256"))
            and isinstance(row.get("requested_at"), str)
            and isinstance(row.get("retrieved_at"), str)
        )
        if not valid:
            malformed_receipts.append(str(receipt_id or index))
            continue
        receipts[receipt_id] = row

    instruments = manifest.get("instruments") or []
    active = {
        row.get("ticker")
        for row in instruments
        if isinstance(row, dict) and row.get("identity_kind") == "active"
    }
    edge_rows = [
        row
        for row in instruments
        if isinstance(row, dict) and row.get("identity_kind") in {"delisted", "recycled"}
    ]
    universe_complete = set(TARGET_TICKERS).issubset(active) and len(edge_rows) >= 2

    malformed_instruments: list[str] = []
    missing_receipts: list[str] = []
    price_history_gaps: list[str] = []
    correction_observed = False
    coverage_totals = {
        area: {"available": 0, "unavailable": 0, "not_entitled": 0}
        for area in COVERAGE_AREAS
    }
    for index, row in enumerate(instruments):
        if not isinstance(row, dict):
            malformed_instruments.append(str(index))
            continue
        sample_id = str(row.get("sample_id") or row.get("ticker") or index)
        required_text = ("ticker", "identity_kind", "provider_instrument_id", "venue", "currency", "timezone")
        if any(not isinstance(row.get(field), str) or not row[field] for field in required_text):
            malformed_instruments.append(sample_id)
            continue
        coverage = row.get("coverage") or {}
        for area in COVERAGE_AREAS:
            declaration = coverage.get(area) or {}
            status = declaration.get("status")
            if status not in coverage_totals[area]:
                malformed_instruments.append(f"{sample_id}:{area}")
                continue
            coverage_totals[area][status] += 1
            references = declaration.get("receipt_ids") or []
            if status == "available" and not references:
                missing_receipts.append(f"{sample_id}:{area}")
            for receipt_id in references:
                if receipt_id not in receipts:
                    missing_receipts.append(f"{sample_id}:{area}:{receipt_id}")
        history = row.get("history") or {}
        prices_available = (coverage.get("prices") or {}).get("status") == "available"
        history_valid = (
            _valid_date(history.get("first_date"))
            and _valid_date(history.get("last_date"))
            and history["first_date"] <= history["last_date"]
            and history.get("raw_adjusted_distinguished") is True
            and history.get("missing_sessions_checked") is True
            and history.get("timezone_boundary_checked") is True
            and history.get("correction_status")
            in {"observed", "no_correction_in_sample", "not_available"}
        )
        if prices_available and not history_valid:
            price_history_gaps.append(sample_id)
        correction_observed = correction_observed or history.get("correction_status") == "observed"

    all_price_samples = all(
        ((row.get("coverage") or {}).get("prices") or {}).get("status") == "available"
        for row in instruments
        if isinstance(row, dict)
    ) and bool(instruments)
    sample_complete = (
        universe_complete
        and not malformed_instruments
        and not missing_receipts
        and not price_history_gaps
        and all_price_samples
        and correction_observed
    )
    checks = [
        _check("authorized_sample_access", authorized, {"status": access_status}, blocked=blocked),
        _check("rights_answers_explicit", rights_explicit, rights_values, blocked=blocked),
        _check(
            "representative_universe",
            universe_complete,
            {"required_active": list(TARGET_TICKERS), "observed_active": sorted(active), "edge_identity_count": len(edge_rows)},
            blocked=blocked,
        ),
        _check(
            "hashed_request_response_receipts",
            bool(receipts) and not malformed_receipts and not missing_receipts,
            {"valid_receipts": len(receipts), "malformed": malformed_receipts, "missing_references": missing_receipts},
            blocked=blocked,
        ),
        _check(
            "price_history_calendar_and_corrections",
            all_price_samples and not price_history_gaps and correction_observed,
            {"all_samples_have_prices": all_price_samples, "history_gaps": price_history_gaps, "correction_observed": correction_observed},
            blocked=blocked,
        ),
        _check(
            "research_dataset_coverage_declared",
            not malformed_instruments and bool(instruments),
            {"malformed": sorted(set(malformed_instruments)), "coverage": coverage_totals},
            blocked=blocked,
        ),
    ]

    if evidence_kind == "fixture":
        status = "fixture_verified_only" if all(item["status"] == "passed" for item in checks) else "fixture_incomplete"
    elif blocked:
        status = "credential_blocked"
    elif authorized and rights_explicit and sample_complete and not malformed_receipts:
        status = "live_evaluated"
    else:
        status = "live_incomplete"
    return {
        "contract": "noesis-market-provider-sample-evaluation-v1",
        "pack_id": f"market-provider-sample:{provider}",
        "status": status,
        "evidence_kind": evidence_kind,
        "provider": provider,
        "recorded_at": manifest.get("recorded_at"),
        "manifest_sha256": _digest(manifest),
        "authorization": {"status": access_status, "license_reference": authorization.get("license_reference")},
        "rights": rights_values,
        "checks": checks,
        "coverage_comparison": coverage_totals,
        "receipt_count": len(receipts),
        "instrument_count": len(instruments),
        "limitations": [
            "This receipt stores hashes and normalized findings, not vendor payloads or credentials.",
            "live_evaluated means the authorized sample was assessed; it is not a procurement decision or production entitlement.",
            "Fixture evidence cannot satisfy live-provider acceptance.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(json.loads(args.manifest.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"provider": result["provider"], "status": result["status"], "checks": {row["name"]: row["status"] for row in result["checks"]}}))
    return 0 if result["status"] in {"live_evaluated", "fixture_verified_only"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
