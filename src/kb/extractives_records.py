"""Extractives records: vocabulary, validation and the EX01 minimisation rule (#2653, EX02).

Records follow contract ``noesis-extractives-record-v1`` and are written by :mod:`src.kb.extractives_store`:

* **release** - one acquired publication (an EITI summary, a USGS MCS data-release table, a BGS OGC API page) with
  its release clock, basis label, digests, licence and attribution terms and evidence origin.
* **EITI report revision** - keyed by country and fiscal period; each changed publication is a new revision
  (``revision_of`` its predecessor), a withdrawn summary is a revision stating the withdrawal. It keeps the report
  label and version, the fiscal period and currency as reported.
* **revenue stream / payment line** - government agency, revenue stream (name and GFS code as reported), company
  and project as reported, amount text with the currency the report states and who reported it (``government``
  or ``company``). Government- and company-reported figures are separate lines.
* **discrepancy** - the report's own reconciliation line (government figure, company figure, discrepancy and
  explanation as published); nothing else is reconciled.
* **commodity series** - source, commodity (name and form as published), statistic (production, reserves,
  capacity, imports, exports), unit and country; numbers and vintages live in the Economics series storage
  (``register_series``). **vintage** - one publication of a series (USGS annual release, BGS edition) with its
  changes; **observation** - year, value text as published, status (``reported``, ``withheld``,
  ``not_available``, ``symbol_only``), estimated and revised flags as published.

Records are immutable: a correction or removal by the source is a new revision or vintage, never a deletion.
Nothing here converts currencies, sums across reports, fills withheld values, estimates reserves, forecasts or
scores risk.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any

from src.ingestion.extractives_sources import (
    EXCLUSIONS,
    FEATURES,
    MINIMISATION,
    NEVER_SENTENCE,
    REDACTED_NAME,
    REPORTED_BY,
    STATISTICS,
    STATUSES,
    personal_keys,
)

CONTRACT = "noesis-extractives-record-v1"
ANSWER_CONTRACT = "noesis-extractives-answer-v1"
READ_SCOPE = "knowledge:extractives:read"
WRITE_SCOPE = "knowledge:extractives:write"
REVIEW_SCOPE = "knowledge:extractives:review"
DEFAULT_NAMESPACE = "global"
DOMAIN = "economics"
RECORD_TYPES = ("release", "eiti_report", "payment", "discrepancy", "commodity_series", "vintage", "observation")
# Keys that would carry a derived, converted, summed, estimated, scored or forecast number.
FORBIDDEN_KEYS = frozenset({
    "risk_score", "corruption_risk", "governance_score", "governance_risk", "red_flag", "red_flags",
    "estimated_reserves", "reserve_estimate", "own_estimate", "forecast", "forecast_value", "price_forecast",
    "projection", "predicted", "nowcast", "imputed", "imputed_value", "filled_value", "gap_filled",
    "converted_amount", "amount_usd", "usd_amount", "exchange_rate_applied", "total_all_reports",
    "reconciled_amount", "adjusted_discrepancy", "blended", "blended_value", "combined_value",
})
SUBJECT_PREFIX = "extractives:"


class ExtractivesError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load(value: Any, default: Any) -> Any:
    return default if value in (None, "") else json.loads(value)


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = ({f"namespace:{namespace}:write"} if write
              else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"})
    if required not in scopes or not needed & scopes:
        raise ExtractivesError("unauthorized", f"{required} and namespace access are required")


def require_scope(scopes: Iterable[str], required: str) -> None:
    scopes = set(scopes)
    if "operator" not in scopes and required not in scopes:
        raise ExtractivesError("unauthorized", f"{required} is required for this part of the answer")


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
    """The release clock (UTC epoch ms): the stated instant, a date's midnight, else the retrieval time."""
    if published_on:
        return int(datetime.combine(date.fromisoformat(published_on), datetime.min.time(),
                                    tzinfo=UTC).timestamp() * 1000)
    if published_at:
        stamp = datetime.fromisoformat(str(published_at))
        return int((stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)).timestamp() * 1000)
    return int(retrieved_ms)


def iso_from_ms(value: int | None) -> str | None:
    return None if value is None else datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat()


def selected_features(conn: Any) -> list[str]:
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


def feature_enabled(conn: Any, feature: str | None = None) -> bool:
    """Whether an Economics ``extractives-*`` feature (or the named one) is selected in the active plan."""
    selected = selected_features(conn)
    return feature in selected if feature else any(f in selected for f in FEATURES)


def check_minimised(item: Mapping[str, Any]) -> None:
    """EX01 minimisation, enforced at write time: no natural-person field; natural persons are redacted."""
    found = personal_keys(dict(item))
    if found:
        raise ExtractivesError("personal_data_refused", "natural-person fields are not stored (EX01 minimisation)",
                               paths=found)

    def companies():
        for line in item.get("company_payments") or []:
            if line.get("company"):
                yield line["company"]
        for line in item.get("discrepancies") or []:
            if line.get("company"):
                yield line["company"]

    for company in companies():
        if company.get("natural_person") and (company.get("name_as_reported") != REDACTED_NAME
                                              or company.get("identifiers")):
            raise ExtractivesError("personal_data_refused", "a natural-person reporting entity is stored redacted")


def check_item(item: Mapping[str, Any]) -> None:
    if forbidden_keys(dict(item)):
        raise ExtractivesError("invalid_release", "published records carry no derived, converted, scored or "
                                                  "forecast value")
    check_minimised(item)
    if item.get("kind") == "eiti_report":
        for key in ("report", "country", "fiscal_period"):
            if not item.get(key):
                raise ExtractivesError("invalid_release", f"an EITI report states its {key}")
        for line in list(item.get("government_revenues") or []) + list(item.get("company_payments") or []):
            if line.get("reported_by") not in REPORTED_BY:
                raise ExtractivesError("invalid_release", "each payment line states who reported it")
            if line.get("amount_text") is not None and not line.get("currency"):
                raise ExtractivesError("invalid_release", "an amount keeps the currency the report states")
        return
    if item.get("kind") != "commodity_series":
        raise ExtractivesError("invalid_release", "unknown extractives item kind")
    for key in ("provider", "commodity", "statistic", "unit", "country"):
        if not item.get(key):
            raise ExtractivesError("invalid_release", f"a commodity series states its {key}")
    if item["statistic"] not in STATISTICS:
        raise ExtractivesError("invalid_release", f"statistic is one of {STATISTICS}")
    periods = set()
    for obs in item.get("observations") or []:
        if obs.get("status") not in STATUSES:
            raise ExtractivesError("invalid_release", "each observation states its status")
        if obs["status"] != "reported" and obs.get("value") is not None:
            raise ExtractivesError("invalid_release", "a withheld or unavailable value carries no number")
        if obs["period"] in periods:
            raise ExtractivesError("invalid_release", "a series states a year twice")
        periods.add(obs["period"])


def company_key(country: Mapping[str, Any], company: Mapping[str, Any], *, report_key: str, line: int) -> str:
    """Stable subject key: published identifiers first, else the name as reported; redacted entities per line."""
    if company.get("redacted"):
        return SUBJECT_PREFIX + "company:" + digest(["redacted", report_key, line])[:24]
    basis = (["identifiers", company["identifiers"]] if company.get("identifiers")
             else ["name", str(company.get("name_as_reported") or "").casefold().strip()])
    return SUBJECT_PREFIX + "company:" + digest([country.get("code"), basis])[:24]


def project_key(country: Mapping[str, Any], project: Mapping[str, Any]) -> str:
    basis = (["identifiers", project["identifiers"]] if project.get("identifiers")
             else ["name", str(project.get("name_as_reported") or "").casefold().strip()])
    return SUBJECT_PREFIX + "project:" + digest([country.get("code"), basis])[:24]


def commodity_key(provider: str, commodity: Mapping[str, Any]) -> str:
    return SUBJECT_PREFIX + "commodity:" + digest([provider, commodity.get("name"), commodity.get("form")])[:24]


__all__ = [
    "ANSWER_CONTRACT",
    "CONTRACT",
    "DEFAULT_NAMESPACE",
    "EXCLUSIONS",
    "FORBIDDEN_KEYS",
    "MINIMISATION",
    "NEVER_SENTENCE",
    "READ_SCOPE",
    "RECORD_TYPES",
    "REVIEW_SCOPE",
    "WRITE_SCOPE",
    "ExtractivesError",
    "authorize",
    "check_item",
    "check_minimised",
    "commodity_key",
    "company_key",
    "feature_enabled",
    "forbidden_keys",
    "project_key",
]
