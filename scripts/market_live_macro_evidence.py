"""Record live official-statistics evidence for market macro inputs (#1661).

Operator tool: makes real provider requests and writes a live-source pack
separate from fixture evidence. Only series identifiers, clocks, counts and
diagnostics are stored, never observation payloads. A missing credential is
recorded as ``credential_blocked`` and never converted into a pass.

Usage::

    python scripts/market_live_macro_evidence.py \\
        --output config/market/acceptance_packs/live-macro-sources.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PRESERVED_FIELDS = (
    "provider_vintage_ms",
    "vintage_basis",
    "provider_release_at_ms",
    "provider_release_time_status",
    "acquired_at_ms",
    "seasonal_adjustment",
)


def _requests():
    from src.ingestion.connectors.dataset.eurostat import EurostatConnector
    from src.ingestion.connectors.dataset.fred import FredConnector
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector
    from src.ingestion.connectors.dataset.worldbank import WorldBankConnector

    yield "eurostat", EurostatConnector, [
        {"dataset": "prc_hicp_manr", "geography": "DE", "coicop": "CP00"},
        {"dataset": "namq_10_gdp", "geography": "FR", "unit": "CLV_PCH_PRE", "s_adj": "SCA", "na_item": "B1GQ"},
    ]
    yield "worldbank", WorldBankConnector, [("NY.GDP.MKTP.CD", "US"), ("FP.CPI.TOTL.ZG", "DE")]
    yield "ecb_sdmx", lambda: SDMXConnector("ECB"), [
        {"flow": "EXR", "key": "D.USD.EUR.SP00.A", "lastNObservations": 30},
        {"flow": "ICP", "key": "M.U2.N.000000.4.ANR", "lastNObservations": 24},
    ]
    yield "fred", FredConnector, [
        {"series": "CPIAUCSL", "geography": "US"},
        {
            "series": "CPIAUCSL",
            "geography": "US",
            "vintage_date": "2020-03-01",
            "observation_start": "2019-01-01",
            "observation_end": "2020-02-01",
        },
    ]


def record() -> dict:
    providers = []
    for name, factory, queries in _requests():
        try:
            connector = factory()
        except Exception as exc:  # optional dependency or configuration
            providers.append({"provider": name, "status": "unavailable", "error": type(exc).__name__})
            continue
        series, diagnostics, statuses = [], [], []
        for query in queries:
            report = connector.harvest_with_report(query)
            statuses.append(report["status"])
            diagnostics.extend(report["diagnostics"])
            for item in report["records"]:
                values = [obs for obs in item.observations if obs.value is not None]
                series.append({
                    "series_id": item.series_id,
                    "frequency": item.frequency,
                    "unit": item.unit,
                    "geography": item.geography,
                    "observations": len(values),
                    "first_period": values[0].period if values else None,
                    "last_period": values[-1].period if values else None,
                    "preserved": {key: item.metadata.get(key) for key in PRESERVED_FIELDS},
                })
        if statuses and all(status == "blocked" for status in statuses):
            status = "credential_blocked"
        elif statuses and all(status == "available" for status in statuses):
            status = "live_verified"
        elif series:
            status = "live_with_coverage_gaps"
        else:
            status = "unavailable"
        provider_result = {
            "provider": name,
            "status": status,
            "series": series,
            "diagnostics": diagnostics,
        }
        if name == "fred" and connector.configured:
            try:
                releases = connector.series_releases("CPIAUCSL")
                release_ids = [row["release_id"] for row in releases["releases"]]
                calendar = connector.release_dates(
                    release_ids[0],
                    realtime_start="2025-01-01",
                    realtime_end="2027-12-31",
                    include_release_dates_with_no_data=True,
                ) if release_ids else None
                provider_result["release_calendar"] = {
                    "series_id": "CPIAUCSL",
                    "release_ids": release_ids,
                    "release_id": calendar["release_id"] if calendar else None,
                    "date_count": len(calendar["dates"]) if calendar else 0,
                    "first_date": calendar["dates"][0]["date"] if calendar and calendar["dates"] else None,
                    "last_date": calendar["dates"][-1]["date"] if calendar and calendar["dates"] else None,
                    "coverage": calendar["coverage"] if calendar else "no_release_membership",
                    "release_time_precision": calendar["release_time_precision"] if calendar else None,
                    "include_release_dates_with_no_data": True,
                }
                if not calendar or not calendar["dates"]:
                    provider_result["status"] = "live_with_coverage_gaps"
                    provider_result["diagnostics"].append(
                        {"stage": "release_calendar", "code": "empty_release_calendar"}
                    )
            except Exception as exc:
                provider_result["status"] = "live_with_coverage_gaps"
                provider_result["release_calendar"] = {
                    "status": "unavailable",
                    "error": type(exc).__name__,
                }
                provider_result["diagnostics"].append(
                    {"stage": "release_calendar", "code": type(exc).__name__}
                )
        providers.append(provider_result)
    return {
        "pack_id": "market-live-macro-sources-v1",
        "evidence_kind": "live_provider",
        "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "providers": providers,
        "limitations": [
            "Provider release times are recorded only where the response carries them; otherwise the status explains the fallback clock.",
            "The bounded CPI example demonstrates one ALFRED vintage; it does not establish revision coverage for every FRED series.",
            "No analyst review is implied by this pack.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pack = record()
    args.output.write_text(json.dumps(pack, indent=1) + "\n")
    print(json.dumps({item["provider"]: item["status"] for item in pack["providers"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
