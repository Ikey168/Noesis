"""Tourism statistics records for the Economics ``economics.tourism`` provider (#2739, TO02).

One record contract, ``noesis-tourism-statistics-record-v2`` (the wave's record contract line is 2.x), covers the
record types the provider owns:

* ``release`` - one acquired publication (a Eurostat SDMX-CSV response as of its ``LAST UPDATE``) with its release clock
  and basis (``provider_last_update``, ``declared_release`` or ``retrieval_time``), retrieval clock, document key,
  dataflow version, digests and evidence origin;
* ``series`` - keyed by dataset, indicator (nights spent, arrivals, establishments, bedrooms, bed places), residence
  of guest (``c_resid``), accommodation type (``nace_r2``), unit, frequency and geography with its NUTS version. A
  monthly national and an annual NUTS 2 series are different series, and so is the same code under another NUTS
  version; an annual total is never computed from months;
* ``definition`` - the definition the source states (scope, methodology notes, cited documents, the
  establishment-size threshold each country applies, the capacity reference date), revisioned;
* ``vintage`` - one release of one series with release and retrieval clocks, the definition revision in force and the
  changes against the previous vintage. A revised provisional month is a new vintage, never an overwrite; a series a
  complete later release no longer states gets a ``removed_by_source`` vintage, never a deletion;
* ``observation`` - period, value exactly as published, status (``reported``, ``confidential``, ``not_published``)
  and ``OBS_FLAG`` letters verbatim. A confidential cell (``c``) is a status and carries no value;
* ``comparability_note`` - source-stated breaks, provisional periods and definition differences, and reviewer notes.

Numeric values also live in the Economics series storage (``economic_vintages``, ``dataset_observations``) through
:func:`src.domains.economic.model.register_series`, domain ``economics``, as the labour track does; no new series
store or record shape (``statistical-series``).

**Minimisation decision (TO01).** The sources publish aggregate statistics; none returns data about a person, a guest
or an individual establishment. :func:`check_item` refuses at write time any key that would carry a person-level or
establishment-level field and any key that would carry a derived, filled, blended or forecast value (an occupancy
rate, an average, a per-capita or per-bed figure, a nowcast, a filled month or region, an annual total from months).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.ingestion.tourism_sources import (
    ACCOMMODATION_TYPES,
    CONCEPTS,
    DATASETS,
    EXCLUSIONS,
    FREQUENCIES,
    LIVE_VERIFICATION,
    NEVER_SENTENCE,
    NOT_IMPLEMENTED_ANSWER,
    NUTS_VERSIONS,
    PROVIDER_CONTRACTS,
    PROVIDERS,
    STATUSES,
    excluded_dataset,
    period_matches,
)

CONTRACT = "noesis-tourism-statistics-record-v2"
ANSWER_CONTRACT = "noesis-tourism-statistics-answer-v1"
READ_SCOPE = "knowledge:tourism:read"
WRITE_SCOPE = "knowledge:tourism:write"
REVIEW_SCOPE = "knowledge:tourism:review"
DEFAULT_NAMESPACE = "global"
ECONOMIC_DOMAIN = "economics"
PROVIDER_ID = "economics.tourism"
FEATURE = "tourism-statistics"
RECORD_TYPES = ("release", "series", "definition", "vintage", "observation", "comparability_note")
CHANGE_KINDS = ("new_series", "new_period", "revised_value", "definition_change", "removed_by_source")
SCHEMA_FILE = f"contracts/schemas/jsonschema/{CONTRACT}.json"
# Keys that would carry data about a person, a guest or a single establishment.
PERSONAL_DATA_FIELDS = frozenset({
    "person_id", "person_name", "full_name", "first_name", "last_name", "date_of_birth", "birth_date", "email",
    "phone", "home_address", "address", "national_id", "passport_number", "guest_id", "guest_name", "traveller_id",
    "traveler_id", "respondent_id", "booking_id", "host_id", "host_name", "listing_id", "establishment_id",
    "establishment_name", "hotel_name", "property_id", "company_id", "company_name", "business_name", "vat_number",
    "microdata", "record_weight", "survey_weight",
})
# Keys that would carry a derived, filled, blended or forecast number.
FORBIDDEN_KEYS = frozenset({
    "nowcast", "nowcasted", "forecast", "forecast_value", "projection", "predicted", "imputed", "imputed_value",
    "interpolated", "gap_filled", "filled_value", "filled_month", "filled_region", "blended", "blended_value",
    "combined_value", "average_value", "average_length_of_stay", "occupancy_rate", "bed_occupancy_rate",
    "room_occupancy_rate", "net_occupancy_rate", "per_capita", "per_capita_value", "per_bed", "per_bed_value",
    "nights_per_bed", "nights_per_inhabitant", "tourism_intensity", "own_seasonal_adjustment",
    "seasonally_adjusted_value", "annual_total_from_months", "summed_months", "derived_value", "derived_rate",
    "share_value", "unconfidential_value", "estimated_cell",
})
MINIMISATION = {
    "decision": "published aggregates only; no person-level, guest-level or establishment-level field is stored",
    "stored": "published aggregates per place, period and series key, with flags, notes, definitions and citations",
    "redacted": "nothing (no personal field is ever acquired)",
    "excluded": "traveller survey microdata behind the demand-side tour_dem_* tables; establishment-level returns; "
                "Eurostat's experimental short-stay platform statistics (tour_ce_oa*); confidential cells, stored as "
                "their status only",
    "retention": "release vintages are kept for provenance; no erasure workflow applies",
    "who_may_query": f"principals holding {READ_SCOPE} and access to the namespace",
    "enforced_by": "tourism_records.check_item at write time",
}


class TourismError(ValueError):
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
        raise TourismError("unauthorized", f"{required} and namespace access are required")


def require_scope(scopes: Iterable[str], required: str) -> None:
    scopes = set(scopes or ())
    if "operator" not in scopes and required not in scopes:
        raise TourismError("unauthorized", f"{required} is required for this part of the answer")


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
    """Validate one published series item before anything is written (TO02 rules and the TO01 minimisation)."""
    leaked = personal_data_paths(dict(item))
    if leaked:
        raise TourismError("personal_data", "records never carry person-, guest- or establishment-level data",
                           fields=leaked)
    derived = forbidden_paths(dict(item))
    if derived:
        raise TourismError("derived_value", "published records carry no derived, filled, blended or forecast value",
                           fields=derived)
    for key in ("provider", "dataset", "native_key", "indicator", "residence", "accommodation", "area", "unit",
                "definition", "frequency"):
        if not item.get(key):
            raise TourismError("invalid_record", f"a tourism series states its {key}")
    if item["provider"] not in PROVIDERS:
        raise TourismError("invalid_record", f"provider is one of {PROVIDERS}")
    if excluded_dataset(str(item["dataset"])):
        raise TourismError("excluded_dataset", "demand-side survey tables and experimental platform data are excluded")
    dataset = DATASETS.get(str(item["dataset"]))
    if dataset is None or dataset["provider"] != item["provider"]:
        raise TourismError("invalid_record", "the dataset is one of the provider's audited datasets")
    concept = dict(item["indicator"]).get("concept")
    if concept not in CONCEPTS or concept not in dataset["concepts"]:
        raise TourismError("invalid_record", f"indicator concept is one of {dataset['concepts']}")
    if item["frequency"] not in FREQUENCIES or item["frequency"] != dataset["frequency"]:
        raise TourismError("invalid_record", f"{item['dataset']} is a {dataset['frequency']} dataset; monthly and "
                                             "annual series are never converted into each other")
    if dict(item["accommodation"]).get("code") not in ACCOMMODATION_TYPES:
        raise TourismError("invalid_record", "accommodation type is a NACE Rev.2 I55 code as published")
    area = dict(item["area"])
    if area.get("scheme") != "eurostat-geo" or not area.get("code") or str(area.get("nuts_version")) not in \
            NUTS_VERSIONS:
        raise TourismError("invalid_record", "a series states its geography with its NUTS version")
    if not dict(dict(item["definition"]).get("coverage_thresholds") or {}):
        raise TourismError("invalid_record", "a series records the establishment-size threshold as a definition note")
    periods = set()
    for obs in item.get("observations") or []:
        if obs.get("status") not in STATUSES:
            raise TourismError("invalid_record", f"each observation states its status ({STATUSES})")
        if obs["status"] != "reported" and obs.get("value") is not None:
            raise TourismError("invalid_record", "a confidential or unpublished cell carries no value (a status, never "
                                                 "a value)")
        if "c" in str(dict(obs.get("flags") or {}).get("OBS_FLAG", "")) and obs["status"] != "confidential":
            raise TourismError("invalid_record", "a cell flagged c is stored as confidential")
        if not period_matches(obs["period"], item["frequency"]):
            raise TourismError("invalid_record", f"{obs['period']} is not a {item['frequency']} period; an annual "
                                                 "total is never computed from months")
        if obs["period"] in periods:
            raise TourismError("invalid_record", "a series states a period twice")
        periods.add(obs["period"])


def comparability_basis(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Recorded differences between two series keys (facts of the keys; nothing is harmonised or blended)."""
    differences = []
    fields = (("dataset", "different_dataset"), ("frequency", "different_frequency"),
              ("residence", "different_residence"), ("accommodation", "different_accommodation_type"),
              ("unit", "different_unit"), ("area", "different_place"))
    for field, kind in fields:
        a, b = left.get(field), right.get(field)
        if field in {"residence", "accommodation", "unit"}:
            a, b = (a or {}).get("code"), (b or {}).get("code")
        elif field == "area":
            a = {k: (a or {}).get(k) for k in ("code", "nuts_version")}
            b = {k: (b or {}).get(k) for k in ("code", "nuts_version")}
            if a["code"] == b["code"] and a["nuts_version"] != b["nuts_version"]:
                kind = "different_nuts_version"
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
    """Whether the Economics bundle's optional ``tourism-statistics`` feature is selected in the active plan."""
    return FEATURE in _selected_features(conn)


def readiness(conn: Any) -> dict[str, Any]:
    ready = table_exists(conn, "tourism_vintages") and table_exists(conn, "tourism_releases")
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        if contract["status"] == "not-implemented":
            providers[provider] = {"delivers": contract["delivers"], "access_decision": contract["status"],
                                   "live_verification": LIVE_VERIFICATION[provider]["status"], "releases": 0,
                                   "stale": None, "reason": NOT_IMPLEMENTED_ANSWER["reason"]}
            continue
        releases, state = 0, {"stale": True, "reason": "never acquired"}
        if ready:
            releases = int(conn.execute("SELECT count(*) FROM tourism_releases WHERE provider=?",
                                        [provider]).fetchone()[0])
        if table_exists(conn, "tourism_receipts"):
            rows = conn.execute("SELECT outcome FROM tourism_receipts WHERE provider=? ORDER BY created_at_ms, "
                                "receipt_id", [provider]).fetchall()
            if any(r[0] in {"applied", "unchanged"} for r in rows):
                state = {"stale": rows[-1][0] == "failed", "reason": "last run failed" if rows[-1][0] == "failed"
                         else None}
            elif rows:
                state = {"stale": True, "reason": "last run failed"}
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
        results.append(registry.register(definition, f"tourism-schema:{name}:2.0.0:{digest(content)[:16]}",
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
    "TourismError",
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
