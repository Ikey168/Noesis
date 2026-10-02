"""Shared contract, scopes and helpers of the Economics ``economics.extractives`` provider (#2653, EX02).

Records follow ``noesis-extractives-record-v2``:

* **EITI records** (:mod:`src.kb.extractives_store`) - ``eiti_report``, ``government_agency``, ``revenue_stream``,
  ``company``, ``project`` (with licences) and ``company_payment`` records, each an immutable revision keyed by
  its record key and tied to one report version (the release). A revised report is a new version: a changed record
  adds a revision, a record the new version no longer states becomes a ``removed`` revision. Government-reported
  and company-reported amounts and the report's own discrepancy are kept apart, in the currency as reported.
* **Commodity series** - production, reserves, imports and exports keyed by source, commodity, statistic, unit
  and country, whose annual values per publication are appended vintages. The numbers are registered in the
  existing Economics series storage (``dataset_series``/``dataset_observations`` and ``economic_vintages`` through
  :func:`src.domains.economic.model.register_series`); value text, status (reported, withheld, not available,
  qualitative), estimated/revised markers and notes stay in ``extractives_values``.

Every record carries its source, record revision and as-of time (the release's declared publication date and the
retrieval time). Personal fields follow the EX01 minimisation decision and are refused at write time
(:func:`check_minimised`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, date, datetime
from typing import Any

CONTRACT = "noesis-extractives-record-v2"
ANSWER_CONTRACT = "noesis-extractives-answer-v1"
MATCH_CONTRACT = "noesis-extractives-match-v1"
LINK_CONTRACT = "noesis-extractives-link-v1"
NOTIFICATION_CONTRACT = "noesis-extractives-notification-v1"
READ_SCOPE = "knowledge:extractives:read"
WRITE_SCOPE = "knowledge:extractives:write"
REVIEW_SCOPE = "knowledge:extractives:review"
DEFAULT_NAMESPACE = "global"
BUNDLE = "economics"
FEATURES = ("extractives-eiti", "extractives-usgs", "extractives-bgs")
ECONOMIC_DOMAIN = "economics"
EITI_RECORD_TYPES = ("eiti_report", "government_agency", "revenue_stream", "company", "project", "company_payment")
# Keys that would carry a reconciliation beyond the report, an own estimate, a risk score or a forecast.
FORBIDDEN_KEYS = frozenset({
    "reconciled_value", "adjusted_discrepancy", "own_estimate", "estimated_reserves", "filled_value",
    "imputed_value", "risk_score", "corruption_risk", "governance_score", "governance_risk", "red_flag",
    "price_forecast", "forecast", "predicted_price", "converted_amount", "amount_in_usd", "blended_value",
    "merged_value", "total_across_reports",
})


class ExtractivesError(ValueError):
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
        raise ExtractivesError("unauthorized", f"{required} and namespace access are required")


def require_scope(scopes: Iterable[str], required: str) -> None:
    scopes = set(scopes)
    if "operator" not in scopes and required not in scopes:
        raise ExtractivesError("unauthorized", f"{required} is required")


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


def check_minimised(record: dict[str, Any]) -> None:
    """Refuse a record that carries a personal field or an individual's name (the EX01 decision)."""
    from src.ingestion.extractives_sources import WITHHELD_NAME, personal_keys

    found = personal_keys(record)
    if found:
        raise ExtractivesError("personal_data_refused", "records carry no personal fields under the minimisation "
                               "decision", paths=found)
    if record.get("natural_person") and (record.get("name_as_published") != WITHHELD_NAME or record.get("identifiers")):
        raise ExtractivesError("personal_data_refused", "an individual payer's name and identifiers are withheld")
    if forbidden_keys(record):
        raise ExtractivesError("invalid_release", "records carry no reconciliation beyond the report, own estimate, "
                               "risk score, forecast or converted amount", paths=forbidden_keys(record))


def release_ms(published_on: str | None, published_at: str | None, retrieved_ms: int) -> int:
    if published_at:
        stamp = datetime.fromisoformat(str(published_at))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return int(stamp.timestamp() * 1000)
    if published_on:
        return int(datetime.combine(date.fromisoformat(published_on), datetime.min.time(),
                                    tzinfo=UTC).timestamp() * 1000)
    return int(retrieved_ms)


def iso_from_ms(value: int | None) -> str | None:
    return None if value is None else datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat()


def day_ms(value: str) -> int:
    return release_ms(value, None, 0)


def as_of_ms(value: Any) -> int | None:
    """An ISO day or instant, or epoch ms, as epoch ms; ``None`` stays ``None``."""
    if value is None or value == "":
        return None
    if isinstance(value, int):
        return value
    raw = str(value)
    if len(raw) == 10:
        return day_ms(raw) + 86_400_000 - 1  # the whole day
    return release_ms(None, raw, 0)


def _selected_features(conn: Any) -> list[str]:
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN "
            "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
        ).fetchall()}
        if len(tables) < 4:
            return []
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle=?", [BUNDLE]).fetchone()
        if not managed or managed[0] != "composition":
            return []
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return []
    return list((plan.get("features") or {}).get(BUNDLE) or [])


def feature_enabled(conn: Any, feature: str | None = None) -> bool:
    """Whether an extractives feature (any of them when ``feature`` is None) is selected in the active plan."""
    selected = set(_selected_features(conn))
    return bool(selected & set(FEATURES)) if feature is None else feature in selected


def readiness(conn: Any) -> dict[str, Any]:
    from src.ingestion.extractives_sources import (
        EXCLUSIONS,
        LIVE_VERIFICATION,
        MINIMISATION,
        NEVER_SENTENCE,
        PROVIDER_CONTRACTS,
    )
    from src.ingestion.extractives_sources import (
        FEATURES as PROVIDER_FEATURES,
    )

    ready = table_exists(conn, "extractives_releases")
    selected = set(_selected_features(conn))
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        releases = 0
        if ready:
            releases = int(conn.execute("SELECT count(*) FROM extractives_releases WHERE provider=?",
                                        [provider]).fetchone()[0])
        providers[provider] = {"delivers": contract["delivers"], "access_decision": contract["access_decision"],
                               "feature": PROVIDER_FEATURES[provider],
                               "feature_selected": PROVIDER_FEATURES[provider] in selected,
                               "live_verification": LIVE_VERIFICATION[provider]["status"], "releases": releases}
    return {
        "features": list(FEATURES),
        "selected": sorted(selected & set(FEATURES)),
        "stores_ready": ready,
        "providers": providers,
        "links": {"ownership": table_exists(conn, "ownership_records"), "trade": table_exists(conn, "trade_series"),
                  "energy": table_exists(conn, "energy_series"),
                  "public_finance": table_exists(conn, "public_finance_lines"),
                  "infrastructure": table_exists(conn, "infra_assets"),
                  "note": "a link target whose store is absent is reported as provider_absent, never dropped"},
        "minimisation": MINIMISATION,
        "exclusions": list(EXCLUSIONS),
        "never": NEVER_SENTENCE,
        "note": "offline fixture evidence and live evidence are reported per release (evidence_origin); no provider "
                "is live until a dated run verifies it",
    }


__all__ = [
    "ANSWER_CONTRACT", "CONTRACT", "DEFAULT_NAMESPACE", "ECONOMIC_DOMAIN", "EITI_RECORD_TYPES", "FEATURES",
    "LINK_CONTRACT", "MATCH_CONTRACT", "NOTIFICATION_CONTRACT", "READ_SCOPE", "REVIEW_SCOPE", "WRITE_SCOPE",
    "ExtractivesError", "as_of_ms", "authorize", "canonical", "check_minimised", "day_ms", "digest",
    "feature_enabled", "forbidden_keys", "iso_from_ms", "load", "readiness", "release_ms", "require_scope",
    "table_exists",
]
