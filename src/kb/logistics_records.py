"""Shared contract, scopes and helpers of the Economics ``logistics`` feature (#2229, SL02).

Records follow ``noesis-logistics-record-v1``: *port* records keyed by UN/LOCODE and versioned by code-list
release (:mod:`src.kb.logistics_ports`), *series* (logistics indicator mappings: source series id, concept, unit,
frequency, geography kind - port, country or published route - definition reference, licence and, for a freight
index, the licence decision) and *vintages* whose numeric observations live in the existing Economics series
storage (``dataset_series``/``dataset_observations`` and ``economic_vintages`` through
:func:`src.domains.economic.model.register_series`, :mod:`src.kb.logistics_series`). No new series store.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import date, datetime, timezone
from typing import Any

CONTRACT = "noesis-logistics-record-v1"
ANSWER_CONTRACT = "noesis-logistics-answer-v1"
MATCH_CONTRACT = "noesis-logistics-port-match-v1"
LINK_CONTRACT = "noesis-logistics-link-v1"
NOTIFICATION_CONTRACT = "noesis-logistics-notification-v1"
READ_SCOPE = "knowledge:logistics:read"
WRITE_SCOPE = "knowledge:logistics:write"
REVIEW_SCOPE = "knowledge:logistics:review"
DEFAULT_NAMESPACE = "global"
FEATURE = "logistics"
ECONOMIC_DOMAIN = "economics"
STATUSES = ("reported", "confidential", "not_published")
# Keys that would carry a forecast, a derived or rebased index, an inferred route or a merged figure.
FORBIDDEN_KEYS = frozenset({
    "forecast", "forecast_value", "predicted_rate", "rate_prediction", "nowcast", "rebased_value",
    "interpolated_value", "derived_index", "inferred_route", "merged_value", "combined_value",
    "trade_per_throughput", "trend_verdict",
})


class LogisticsError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read",
                                                               f"namespace:{namespace}:write"}
    if required not in scopes or not needed & scopes:
        raise LogisticsError("unauthorized", f"{required} and namespace access are required")


def require_scope(scopes: Iterable[str], required: str) -> None:
    scopes = set(scopes)
    if "operator" not in scopes and required not in scopes:
        raise LogisticsError("unauthorized", f"{required} is required")


def table_exists(conn: Any, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]).fetchone())


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                found.append(f"{path}.{key}")
            found += forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}[{index}]")
    return found


def release_ms(published_on: str | None, published_at: str | None, retrieved_ms: int) -> int:
    """The release clock in epoch ms (UTC); a date alone is its midnight; no date is the retrieval time."""
    if published_at:
        stamp = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return int(stamp.timestamp() * 1000)
    if published_on:
        return int(datetime.combine(date.fromisoformat(published_on), datetime.min.time(),
                                    tzinfo=timezone.utc).timestamp() * 1000)
    return int(retrieved_ms)


def iso_from_ms(value: int | None) -> str | None:
    return None if value is None else datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()


def day_ms(value: str) -> int:
    return release_ms(value, None, 0)


def _selected_features(conn: Any) -> list[str]:
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN "
            "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
        ).fetchall()}
        if len(tables) < 4:
            return []
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle='economics'").fetchone()
        if not managed or managed[0] != "composition":
            return []
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return []
    return list((plan.get("features") or {}).get("economics") or [])


def feature_enabled(conn: Any) -> bool:
    """Whether the Economics bundle's optional ``logistics`` feature is selected in the active composition plan."""
    return FEATURE in _selected_features(conn)


def readiness(conn: Any) -> dict[str, Any]:
    from src.ingestion.logistics_sources import (
        EXCLUSIONS,
        LIVE_VERIFICATION,
        NEVER_SENTENCE,
        PROVIDER_CONTRACTS,
        coverage_report,
    )

    ready = table_exists(conn, "logistics_vintages") and table_exists(conn, "logistics_releases")
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        releases = 0
        if ready:
            releases = int(conn.execute("SELECT count(*) FROM logistics_releases WHERE provider=?",
                                        [provider]).fetchone()[0])
        providers[provider] = {"delivers": contract["delivers"], "access_decision": contract["access_decision"],
                               "live_verification": LIVE_VERIFICATION[provider]["status"], "releases": releases}
    return {
        "feature": FEATURE,
        "selected": feature_enabled(conn),
        "stores_ready": ready,
        "providers": providers,
        "freight_indices": coverage_report()["freight_indices"],
        "exclusions": list(EXCLUSIONS),
        "never": NEVER_SENTENCE,
        "note": "offline fixture evidence and live evidence are reported per release (evidence_origin); no provider "
                "is live until a dated run verifies it",
    }


__all__ = [
    "ANSWER_CONTRACT", "CONTRACT", "DEFAULT_NAMESPACE", "ECONOMIC_DOMAIN", "FEATURE", "LINK_CONTRACT",
    "MATCH_CONTRACT", "NOTIFICATION_CONTRACT", "READ_SCOPE", "REVIEW_SCOPE", "STATUSES", "WRITE_SCOPE",
    "LogisticsError", "authorize", "canonical", "day_ms", "digest", "feature_enabled", "forbidden_keys",
    "iso_from_ms", "load", "readiness", "release_ms", "require_scope", "table_exists",
]
