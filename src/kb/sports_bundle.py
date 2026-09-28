"""Sports bundle readiness: per-provider access decisions, acquisitions and optional features (#2146, SP11).

Enablement belongs to the composition lifecycle coordinator (``packs/sports``);
this module only reports what is selected and what has been acquired, with
offline fixture evidence and live evidence kept apart (``LIVE_VERIFICATION``).
"""

from __future__ import annotations

from typing import Any

from src.ingestion.sports_sources import (
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    REVIEW_BOUNDARY,
)
from src.kb.sports_records import feature_enabled, table_exists

BUNDLE = "sports"
FEATURES = ("sports-identity", "sports-tennis", "sports-forecasts")


def readiness(conn: Any) -> dict[str, Any]:
    ready = table_exists(conn, "sports_acquisitions")
    providers = {}
    for provider, contract in sorted(PROVIDER_CONTRACTS.items()):
        acquisitions = 0
        if ready:
            acquisitions = int(
                conn.execute(
                    "SELECT count(*) FROM sports_acquisitions WHERE provider=?",
                    [provider],
                ).fetchone()[0]
            )
        providers[provider] = {
            "access_decision": contract["access_decision"],
            "reason": contract["reason"],
            "acquisitions": acquisitions,
            "live_verification": LIVE_VERIFICATION[provider],
        }
    return {
        "bundle": BUNDLE,
        "store_ready": ready,
        "status": "ready"
        if any(p["acquisitions"] for p in providers.values())
        else "not_ready",
        "features": {feature: feature_enabled(conn, feature) for feature in FEATURES},
        "providers": providers,
        "evidence": "offline fixture results and live results are reported separately; see live_verification",
        "review_boundary": REVIEW_BOUNDARY,
    }
