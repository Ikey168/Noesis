"""Industry and business statistics records for the Economics ``economics.business`` provider (#2738, IB02).

One record contract, ``noesis-business-statistics-record-v2`` (the wave's record contract line is 2.x), covers the
record types the provider owns:

* ``release`` - one acquired publication (a Eurostat SDMX-CSV response as of its ``LAST UPDATE`` or a CBP year
  response) with its release clock and basis (``provider_last_update``, ``declared_release`` or ``retrieval_time``),
  retrieval clock, document key, dataflow version or NAICS variable, digests and evidence origin;
* ``series`` - keyed by source, dataset, indicator, classification (NACE Rev.2, or NAICS with its vintage), size
  class, place, adjustment (``NSA``, ``CA``, ``SCA`` or not applicable), unit with its index base year, and frequency.
  An adjusted and an unadjusted series, a rebased index and another NAICS vintage are different series;
* ``definition`` - the definition the source states (statistical unit, scope, methodology notes, cited documents,
  CBP's disclosure protection), revisioned: a changed definition is a new revision;
* ``vintage`` - one release of one series with release and retrieval clocks, the definition revision and dataflow
  version in force and the changes against the previous vintage. A revision is a new vintage, never an overwrite; a
  series a complete later release no longer states gets a ``removed_by_source`` vintage, never a deletion;
* ``observation`` - period, value exactly as published, status (``reported``, ``confidential``, ``withheld``,
  ``not_published``) and flags verbatim (Eurostat ``OBS_FLAG``, CBP noise flags and withheld markers). A withheld,
  suppressed or confidential cell carries no value: never a zero, never filled, never reconstructed;
* ``comparability_note`` - source-stated breaks, provisional periods and base-year changes, and reviewer notes.

Numeric values also live in the Economics series storage (``economic_vintages``, ``dataset_observations``) through
:func:`src.domains.economic.model.register_series`, domain ``economics``, as the labour and income tracks do.

**Minimisation decision (IB01).** The sources publish aggregate statistics about establishments and enterprises;
none returns data about a person. :func:`check_item` refuses at write time any key that would carry a person-level
or firm-level field (business-register or survey microdata, a single business's figures) and any key that would carry
a derived, filled, blended, re-based, reconstructed or forecast value.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.ingestion.business_statistics_sources import (
    ADJUSTMENTS,
    CLASSIFICATION_SCHEMES,
    CONCEPTS,
    EXCLUSIONS,
    LIVE_VERIFICATION,
    NEVER_SENTENCE,
    PROVIDER_CONTRACTS,
    PROVIDERS,
    STATISTICAL_UNITS,
    STATUSES,
)

CONTRACT = "noesis-business-statistics-record-v2"
ANSWER_CONTRACT = "noesis-business-statistics-answer-v1"
READ_SCOPE = "knowledge:business:read"
WRITE_SCOPE = "knowledge:business:write"
REVIEW_SCOPE = "knowledge:business:review"
DEFAULT_NAMESPACE = "global"
ECONOMIC_DOMAIN = "economics"
PROVIDER_ID = "economics.business"
FEATURE = "business-statistics"
RECORD_TYPES = ("release", "series", "definition", "vintage", "observation", "comparability_note")
CHANGE_KINDS = ("new_series", "new_period", "revised_value", "definition_change", "removed_by_source")
SCHEMA_FILE = f"contracts/schemas/jsonschema/{CONTRACT}.json"
# Keys that would carry data about a person or a single business (register or survey microdata).
PERSONAL_DATA_FIELDS = frozenset({
    "person_id", "person_name", "full_name", "first_name", "last_name", "date_of_birth", "birth_date", "email",
    "phone", "home_address", "address", "national_id", "tax_id", "ssn", "employee_name", "employee_id", "owner_name",
    "proprietor", "enterprise_id", "establishment_id", "firm_id", "company_id", "company_name", "business_name",
    "legal_name", "ein", "vat_number", "duns", "register_number", "registration_number", "lei", "microdata",
    "record_weight", "survey_weight", "respondent_id",
})
# Keys that would carry a derived, filled, blended, re-based, reconstructed or forecast number.
FORBIDDEN_KEYS = frozenset({
    "nowcast", "nowcasted", "forecast", "forecast_value", "projection", "predicted", "imputed", "imputed_value",
    "interpolated", "gap_filled", "filled_value", "blended", "blended_value", "combined_value", "average_value",
    "harmonised_value", "rebased_value", "rebased", "own_seasonal_adjustment", "seasonally_adjusted_value",
    "reconstructed_value", "unsuppressed_value", "derived_value", "derived_rate", "per_establishment",
    "share_value", "estimated_cell",
})
MINIMISATION = {
    "decision": "published aggregates only; no person-level or firm-level field is stored",
    "stored": "published aggregates per place, period and series key, with flags, noise and suppression markers, "
              "notes, definitions and citations",
    "redacted": "nothing (no personal field is ever acquired)",
    "excluded": "business-register and survey microdata, confidential and suppressed cells (stored as their status "
                "only), ZIP-code Business Patterns and Nonemployer Statistics, any attempt to infer a single "
                "business's figures",
    "retention": "release vintages are kept for provenance; no erasure workflow applies",
    "who_may_query": f"principals holding {READ_SCOPE} and access to the namespace",
    "enforced_by": "business_statistics_records.check_item at write time",
}


class BusinessError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **({"details": self.details} if self.details else {})}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load(value: Any, default: Any) -> Any:
    return default if value in (None, "") else json.loads(value)


def to_ms(value: Any) -> int | None:
    """ISO date/time (or epoch ms) to epoch milliseconds; naive values are UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    raw = str(value).strip().replace("Z", "+00:00")
    if len(raw) == 10:
        raw += "T00:00:00+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read",
                                                             f"namespace:{namespace}:write"}
    if required not in scopes or not needed & scopes:
        raise BusinessError("unauthorized", f"{required} and namespace access are required")


def require_scope(scopes: Iterable[str], required: str) -> None:
    scopes = set(scopes or ())
    if "operator" not in scopes and required not in scopes:
        raise BusinessError("unauthorized", f"{required} is required for this part of the answer")


def table_exists(conn: Any, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]).fetchone())


def _paths(value: Any, names: frozenset[str], path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            here = f"{path}.{key}"
            if str(key).casefold() in names:
                found.append(here)
            found += _paths(item, names, here)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found += _paths(item, names, f"{path}[{index}]")
    return found


def personal_data_paths(value: Any) -> list[str]:
    return _paths(value, PERSONAL_DATA_FIELDS)


def forbidden_paths(value: Any) -> list[str]:
    return _paths(value, FORBIDDEN_KEYS)


def check_item(item: Mapping[str, Any]) -> None:
    """Validate one published series item before anything is written (IB02 rules and the IB01 minimisation)."""
    leaked = personal_data_paths(dict(item))
    if leaked:
        raise BusinessError("personal_data", "records never carry person- or firm-level data", fields=leaked)
    derived = forbidden_paths(dict(item))
    if derived:
        raise BusinessError("derived_value", "published records carry no derived, filled, blended, re-based, "
                                             "reconstructed or forecast value", fields=derived)
    for key in ("provider", "dataset", "native_key", "indicator", "classification", "size_class", "area", "unit",
                "definition", "statistical_unit", "frequency"):
        if not item.get(key):
            raise BusinessError("invalid_record", f"a business series states its {key}")
    if item["provider"] not in PROVIDERS:
        raise BusinessError("invalid_record", f"provider is one of {PROVIDERS}")
    if dict(item["indicator"]).get("concept") not in CONCEPTS:
        raise BusinessError("invalid_record", f"indicator concept is one of {CONCEPTS}")
    classification = dict(item["classification"])
    if str(classification.get("version")) not in CLASSIFICATION_SCHEMES.get(str(classification.get("scheme")), ()):
        raise BusinessError("invalid_record", "a series states its classification (NACE Rev.2 or a NAICS vintage)")
    if item.get("adjustment") not in ADJUSTMENTS:
        raise BusinessError("invalid_record", f"adjustment is one of {ADJUSTMENTS}")
    if item["statistical_unit"] not in STATISTICAL_UNITS:
        raise BusinessError("invalid_record", f"statistical unit is one of {STATISTICAL_UNITS}")
    periods = set()
    for obs in item.get("observations") or []:
        if obs.get("status") not in STATUSES:
            raise BusinessError("invalid_record", f"each observation states its status ({STATUSES})")
        if obs["status"] != "reported" and obs.get("value") is not None:
            raise BusinessError("invalid_record", "a confidential, withheld or unpublished cell carries no value "
                                                  "(never a zero, never reconstructed)")
        if obs["period"] in periods:
            raise BusinessError("invalid_record", "a series states a period twice")
        periods.add(obs["period"])


def comparability_basis(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Recorded differences between two series keys (facts of the keys; nothing is harmonised or blended)."""
    differences = []
    fields = (("provider", "different_source"), ("statistical_unit", "different_statistical_unit"),
              ("classification", "different_classification"), ("adjustment", "different_adjustment"),
              ("unit", "different_unit_or_base_year"), ("size_class", "different_size_class"),
              ("frequency", "different_frequency"), ("area", "different_place"))
    for field, kind in fields:
        a, b = left.get(field), right.get(field)
        if field == "classification":
            a = {k: (a or {}).get(k) for k in ("scheme", "version", "code")}
            b = {k: (b or {}).get(k) for k in ("scheme", "version", "code")}
        elif field in {"unit", "size_class", "area"}:
            keys = ("code", "base_year") if field == "unit" else ("scheme", "code") if field == "area" else ("code",)
            a = {k: (a or {}).get(k) for k in keys}
            b = {k: (b or {}).get(k) for k in keys}
        if a != b:
            differences.append({"kind": kind, "field": field, "left": a, "right": b})
    return differences


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
    """Whether the Economics bundle's optional ``business-statistics`` feature is selected in the active plan."""
    return FEATURE in _selected_features(conn)


def readiness(conn: Any) -> dict[str, Any]:
    ready = table_exists(conn, "business_vintages") and table_exists(conn, "business_releases")
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        releases, state = 0, {"stale": True, "reason": "never acquired"}
        if ready:
            releases = int(conn.execute("SELECT count(*) FROM business_releases WHERE provider=?",
                                        [provider]).fetchone()[0])
        if table_exists(conn, "business_receipts"):
            rows = conn.execute("SELECT outcome FROM business_receipts WHERE provider=? ORDER BY created_at_ms, "
                                "receipt_id", [provider]).fetchall()
            successes = [r for r in rows if r[0] in {"applied", "unchanged"}]
            if successes:
                state = {"stale": rows[-1][0] == "failed", "reason": "last run failed" if rows[-1][0] == "failed"
                         else None}
        providers[provider] = {"delivers": contract["delivers"], "access_decision": contract["status"],
                               "live_verification": LIVE_VERIFICATION[provider]["status"], "releases": releases,
                               **state}
    return {
        "feature": FEATURE,
        "selected": feature_enabled(conn),
        "stores_ready": ready,
        "series_storage": "economic_indicators, economic_series_map, economic_vintages and dataset_observations",
        "providers": providers,
        "exclusions": list(EXCLUSIONS),
        "never": NEVER_SENTENCE,
        "minimisation": MINIMISATION["decision"],
        "note": "offline fixture evidence and live evidence are reported per release (evidence_origin); no provider "
                "is live until a dated run verifies it",
    }


def schema_definitions(root: Path | None = None) -> dict[str, dict[str, Any]]:
    base = root or Path(__file__).resolve().parents[2]
    return {CONTRACT: json.loads((base / SCHEMA_FILE).read_text())}


def register_schemas(conn: Any, *, principal_id: str, scopes: Iterable[str], root: Path | None = None) -> list[dict]:
    """Register the record schema in the existing schema registry (idempotent per version)."""
    from src.kb.schema_registry import SchemaRegistry

    registry = SchemaRegistry(conn)
    results = []
    for name, content in sorted(schema_definitions(root).items()):
        definition = {
            "contract": "noesis-schema-module-v1", "name": name, "kind": "schema", "semantic_version": "2.0.0",
            "content": content, "owner": PROVIDER_ID, "dependencies": [], "compatibility_policy": "backward",
            "provenance": {"kind": "imported", "source": "packs/economics"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(registry.register(definition, f"business-schema:{name}:2.0.0:{digest(content)[:16]}",
                                         principal_id=principal_id, scopes=set(scopes)))
    return results


__all__ = [
    "ANSWER_CONTRACT",
    "CHANGE_KINDS",
    "CONTRACT",
    "ECONOMIC_DOMAIN",
    "EXCLUSIONS",
    "FEATURE",
    "MINIMISATION",
    "PROVIDER_ID",
    "READ_SCOPE",
    "RECORD_TYPES",
    "REVIEW_SCOPE",
    "WRITE_SCOPE",
    "BusinessError",
    "authorize",
    "canonical",
    "check_item",
    "comparability_basis",
    "digest",
    "feature_enabled",
    "forbidden_paths",
    "iso",
    "personal_data_paths",
    "readiness",
    "register_schemas",
    "require_scope",
    "table_exists",
    "to_ms",
]
