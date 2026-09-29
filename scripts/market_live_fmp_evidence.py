"""Collect payload-free technical evidence from an FMP evaluation credential.

Only request/response hashes, counts, date bounds and coverage findings are
written.  Raw vendor payloads and ``FMP_API_KEY`` are never persisted or
printed.  A working credential does not establish commercial retention,
display, derived-data, export or redistribution rights.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    from scripts.market_provider_sample_evidence import COVERAGE_AREAS, evaluate
except ModuleNotFoundError:  # Direct ``python scripts/...`` execution.
    from market_provider_sample_evidence import COVERAGE_AREAS, evaluate

API_BASE = "https://financialmodelingprep.com/stable"
TARGETS = {
    "MSFT": ("NASDAQ", "XNAS"),
    "ORCL": ("NYSE", "XNYS"),
    "CRM": ("NYSE", "XNYS"),
    "ADBE": ("NASDAQ", "XNAS"),
    "NOW": ("NYSE", "XNYS"),
}
RETRYABLE = {429, 500, 502, 503, 504}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Probe:
    def __init__(self, api_key: str, *, timeout: float = 30, attempts: int = 3):
        self._api_key = api_key
        self.timeout = timeout
        self.attempts = attempts
        self.receipts: list[dict[str, Any]] = []

    def get(self, receipt_id: str, endpoint: str, **params: Any) -> tuple[int, Any]:
        clean = {key: str(value) for key, value in params.items() if value is not None}
        public_url = f"{API_BASE}/{endpoint}?{urlencode(clean)}"
        request_url = f"{public_url}&{urlencode({'apikey': self._api_key})}"
        requested_at = _utc_now()
        status = 0
        payload: Any = None
        for attempt in range(1, self.attempts + 1):
            try:
                request = Request(
                    request_url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "Noesis/market-evidence",
                    },
                )
                with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                    status = int(response.status)
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except HTTPError as exc:
                status = int(exc.code)
                try:
                    payload = json.loads(exc.read().decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    payload = {"error": "non-json provider response"}
                if status not in RETRYABLE or attempt == self.attempts:
                    break
            except Exception:
                if attempt == self.attempts:
                    payload = {"error": "provider unavailable"}
                    break
            time.sleep(min(2 ** (attempt - 1), 8))
        count = len(payload) if isinstance(payload, list) else (1 if payload else 0)
        self.receipts.append(
            {
                "receipt_id": receipt_id,
                "endpoint": f"{API_BASE}/{endpoint}",
                "http_status": status,
                "records": count,
                "requested_at": requested_at,
                "retrieved_at": _utc_now(),
                "request_sha256": _digest({"endpoint": endpoint, "params": clean}),
                "response_sha256": _digest(payload),
            }
        )
        return status, payload


def _dates(rows: Any) -> list[str]:
    if not isinstance(rows, list):
        return []
    values = {
        str(row.get("date"))
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("date"), str)
        and len(row["date"]) == 10
    }
    return sorted(values)


def _coverage(status: str, *receipt_ids: str) -> dict[str, Any]:
    return {"status": status, "receipt_ids": list(receipt_ids)}


def collect(api_key: str, *, as_of: date) -> tuple[dict[str, Any], dict[str, Any]]:
    probe = Probe(api_key)
    history_start = as_of.replace(year=as_of.year - 10)
    holiday_receipts: dict[str, str] = {}
    holiday_dates: dict[str, set[str]] = {}
    for exchange in {exchange for exchange, _ in TARGETS.values()}:
        receipt_id = f"fmp:holidays:{exchange}:{as_of.year}"
        status, rows = probe.get(receipt_id, "holidays-by-exchange", exchange=exchange)
        holiday_receipts[exchange] = receipt_id
        holiday_dates[exchange] = set(_dates(rows)) if status == 200 else set()

    instruments = []
    for ticker, (exchange, mic) in TARGETS.items():
        ids: dict[str, list[str]] = {}
        results: dict[str, tuple[int, Any]] = {}
        oldest_end = history_start.replace(year=history_start.year + 1)
        recent_start = as_of.replace(year=as_of.year - 1)
        requests = {
            "profile": ("profile", {"symbol": ticker}),
            "raw_oldest": (
                "historical-price-eod/non-split-adjusted",
                {"symbol": ticker, "from": history_start, "to": oldest_end},
            ),
            "raw_recent": (
                "historical-price-eod/non-split-adjusted",
                {"symbol": ticker, "from": recent_start, "to": as_of},
            ),
            "adjusted_oldest": (
                "historical-price-eod/full",
                {"symbol": ticker, "from": history_start, "to": oldest_end},
            ),
            "adjusted_recent": (
                "historical-price-eod/full",
                {"symbol": ticker, "from": recent_start, "to": as_of},
            ),
            "dividends": (
                "dividends",
                {"symbol": ticker, "from": history_start, "to": as_of},
            ),
            "splits": (
                "splits",
                {"symbol": ticker, "from": history_start, "to": as_of},
            ),
            "fundamentals": ("income-statement", {"symbol": ticker, "limit": 2}),
            "estimates": ("analyst-estimates", {"symbol": ticker, "limit": 2}),
            "transcripts": ("earning-call-transcript-dates", {"symbol": ticker}),
            "ownership": (
                "institutional-ownership/symbol-positions-summary",
                {"symbol": ticker},
            ),
        }
        for area, (endpoint, params) in requests.items():
            receipt_id = f"fmp:{ticker}:{area}"
            ids[area] = [receipt_id]
            results[area] = probe.get(receipt_id, endpoint, **params)
        raw_statuses = [results[name][0] for name in ("raw_oldest", "raw_recent")]
        adjusted_statuses = [
            results[name][0] for name in ("adjusted_oldest", "adjusted_recent")
        ]
        raw_rows = [
            row
            for name in ("raw_oldest", "raw_recent")
            for row in (results[name][1] if isinstance(results[name][1], list) else [])
        ]
        raw_dates = _dates(raw_rows)
        duplicate_count = (
            len(raw_rows) - len(raw_dates) if isinstance(raw_rows, list) else 0
        )
        recent_check_start = as_of - timedelta(days=31)
        recent_observed = {
            day for day in raw_dates if day >= recent_check_start.isoformat()
        }
        expected = {
            (recent_check_start + timedelta(days=offset)).isoformat()
            for offset in range((as_of - recent_check_start).days)
            if (recent_check_start + timedelta(days=offset)).weekday() < 5
        } - holiday_dates[exchange]
        missing_sessions = sorted(expected - recent_observed)
        profile_rows = results["profile"][1]
        profile = (
            profile_rows[0] if isinstance(profile_rows, list) and profile_rows else {}
        )

        def available(area: str) -> bool:
            status, payload = results[area]
            return status == 200 and isinstance(payload, list)

        prices_available = all(
            status == 200 for status in raw_statuses + adjusted_statuses
        ) and bool(raw_dates)
        actions_available = available("dividends") and available("splits")
        fundamentals_available = available("fundamentals")
        estimates_available = available("estimates")
        instruments.append(
            {
                "sample_id": f"fmp:{ticker}",
                "ticker": ticker,
                "identity_kind": "active",
                "provider_instrument_id": str(profile.get("symbol") or ticker),
                "venue": mic,
                "currency": str(profile.get("currency") or "USD"),
                "timezone": "America/New_York",
                "history": {
                    "first_date": raw_dates[0]
                    if raw_dates
                    else history_start.isoformat(),
                    "last_date": raw_dates[-1] if raw_dates else as_of.isoformat(),
                    "raw_adjusted_distinguished": all(
                        status == 200 for status in raw_statuses
                    )
                    and all(status == 200 for status in adjusted_statuses),
                    "missing_sessions_checked": True,
                    "timezone_boundary_checked": bool(raw_dates),
                    "correction_status": "no_correction_in_sample",
                    "duplicate_bars": duplicate_count,
                    "recent_missing_sessions": missing_sessions,
                    "sampled_windows": [
                        [history_start.isoformat(), oldest_end.isoformat()],
                        [recent_start.isoformat(), as_of.isoformat()],
                    ],
                },
                "coverage": {
                    "prices": _coverage(
                        "available" if prices_available else "unavailable",
                        *(
                            ids["raw_oldest"]
                            + ids["raw_recent"]
                            + ids["adjusted_oldest"]
                            + ids["adjusted_recent"]
                            + [holiday_receipts[exchange]]
                            if prices_available
                            else []
                        ),
                    ),
                    "corporate_actions": _coverage(
                        "available" if actions_available else "unavailable",
                        *(
                            ids["dividends"] + ids["splits"]
                            if actions_available
                            else []
                        ),
                    ),
                    "fundamentals": _coverage(
                        "available" if fundamentals_available else "unavailable",
                        *(ids["fundamentals"] if fundamentals_available else []),
                    ),
                    "estimates": _coverage(
                        "available" if estimates_available else "unavailable",
                        *(ids["estimates"] if estimates_available else []),
                    ),
                    "transcripts": _coverage(
                        "available" if available("transcripts") else "not_entitled",
                        *(ids["transcripts"] if available("transcripts") else []),
                    ),
                    "ownership": _coverage(
                        "available" if available("ownership") else "not_entitled",
                        *(ids["ownership"] if available("ownership") else []),
                    ),
                },
            }
        )

    delisted_status, delisted_rows = probe.get(
        "fmp:delisted:sample", "delisted-companies", page=0, limit=50
    )
    candidates = []
    if delisted_status == 200 and isinstance(delisted_rows, list):
        for row in delisted_rows:
            if not isinstance(row, dict):
                continue
            delisted_date = str(row.get("delistedDate") or "")
            exchange = str(row.get("exchange") or "").upper()
            if delisted_date <= as_of.isoformat() and any(
                token in exchange for token in ("NASDAQ", "NYSE", "AMEX")
            ):
                candidates.append(row)
            if len(candidates) == 2:
                break
    for index, row in enumerate(candidates):
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("symbol") or f"DELISTED{index + 1}")
        exchange = str(row.get("exchange") or "unknown")
        receipt_id = f"fmp:{ticker}:delisted-price"
        price_status, price_rows = probe.get(
            receipt_id,
            "historical-price-eod/non-split-adjusted",
            symbol=ticker,
            **{"from": history_start, "to": as_of},
        )
        price_dates = _dates(price_rows)
        coverage = {area: _coverage("unavailable") for area in COVERAGE_AREAS}
        coverage["prices"] = _coverage(
            "available" if price_status == 200 and bool(price_dates) else "unavailable",
            *([receipt_id] if price_status == 200 else []),
        )
        instruments.append(
            {
                "sample_id": f"fmp:delisted:{ticker}",
                "ticker": ticker,
                "identity_kind": "delisted",
                "provider_instrument_id": ticker,
                "venue": exchange,
                "currency": "USD",
                "timezone": "America/New_York",
                "history": {
                    "first_date": price_dates[0]
                    if price_dates
                    else str(row.get("ipoDate") or history_start),
                    "last_date": price_dates[-1]
                    if price_dates
                    else str(row.get("delistedDate") or as_of),
                    "raw_adjusted_distinguished": False,
                    "missing_sessions_checked": False,
                    "timezone_boundary_checked": False,
                    "correction_status": "no_correction_in_sample",
                },
                "coverage": coverage,
            }
        )

    successful_receipts = [
        receipt for receipt in probe.receipts if 200 <= receipt["http_status"] < 300
    ]
    manifest = {
        "contract": "noesis-market-provider-sample-manifest-v1",
        "evidence_kind": "live_provider",
        "provider": "fmp",
        "recorded_at": _utc_now(),
        "authorization": {
            "status": "authorized",
            "license_reference": f"fmp-credential-scoped-technical-access:{as_of}",
            "raw_payload_retention": "not_retained",
            "commercial_rights_status": "unverified",
        },
        "rights": {
            "local_retention": "pending",
            "internal_display": "pending",
            "external_display": "pending",
            "derived_data": "pending",
            "report_export": "pending",
        },
        "receipts": successful_receipts,
        "instruments": instruments,
        "endpoint_statuses": {
            receipt["receipt_id"]: receipt["http_status"] for receipt in probe.receipts
        },
        "limitations": [
            "A valid API key proves technical endpoint access, not commercial rights.",
            "Transcript and institutional-ownership endpoints were probed and entitlement failures are explicit.",
            "No live provider correction was observed in this point-in-time sample.",
        ],
    }
    return manifest, evaluate(manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest-output",
        type=Path,
        default=Path("config/market/acceptance_packs/live-fmp-sample-manifest.json"),
    )
    parser.add_argument(
        "--evaluation-output",
        type=Path,
        default=Path("config/market/acceptance_packs/live-price-provider.json"),
    )
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    api_key = os.getenv("FMP_API_KEY")
    if not api_key:
        raise SystemExit("FMP_API_KEY is required")
    manifest, evaluation = collect(api_key, as_of=args.as_of)
    for path, payload in (
        (args.manifest_output, manifest),
        (args.evaluation_output, evaluation),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "provider": "fmp",
                "status": evaluation["status"],
                "instruments": evaluation["instrument_count"],
                "receipts": evaluation["receipt_count"],
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
