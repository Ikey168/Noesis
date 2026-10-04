"""Waste and circular-economy records for the Climate and Environment ``environment.waste`` provider (#2740, WC02).

One record contract, ``noesis-waste-record-v2`` (the wave's record contract line is 2.x), covers what the provider
writes. No new store or record shape is introduced (taxonomy shapes ``statistical-series`` and ``observations``):

* **Statistical series** (Eurostat waste, Eurostat circular economy, OECD municipal waste) are keyed by source,
  dataset, indicator, waste category (EWC-Stat), hazardousness, NACE activity or households, treatment operation
  (``wst_oper``), unit as published, place and periodicity. Values live in the Economics series storage
  (``economic_vintages``, ``dataset_observations``) through :func:`src.domains.economic.model.register_series`; the
  ``waste_*`` index tables keep the release, vintage, flag and definition bookkeeping.
* **Facility transfer rows** (EEA Industrial Reporting) are keyed by the facility INSPIRE id, reporting year,
  hazardous or non-hazardous, recovery (R) or disposal (D) and domestic or transboundary, with the quantity in tonnes
  as published and the method code (measured, calculated, estimated). They are ``observation_series`` records of the
  existing ``environment.core`` store (:class:`src.kb.environment_store.EnvironmentStore`, ``environment_vintages``,
  compared through :mod:`src.kb.environment_vintages`) located at the ``environment.core`` facility record
  (``eea-industry:<INSPIRE id>``); no second facility register is created.

Each changed release is an appended vintage with release and retrieval clocks; a changed row for a past reporting year
is a new vintage, never an overwrite; a series or row a later complete release no longer states gets a
``removed_by_source`` vintage. Biennial gaps stay absent and a missing facility row is never stored as zero.

**Minimisation decision (WC01).** Eurostat and OECD publish aggregates. Per facility only the INSPIRE id, reporting
year and the published transfer quantities, codes and method are stored. :func:`check_item` refuses at write time any
operator, parent-company, address, contact or competent-authority field and any derived, filled, blended or forecast
value.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.ingestion.waste_sources import (
    CONCEPTS,
    EXCLUSIONS,
    LIVE_VERIFICATION,
    METHOD_CODES,
    NEVER_SENTENCE,
    PERIODICITIES,
    PROVIDER_CONTRACTS,
    SERIES_PROVIDERS,
    STATUSES,
    TRANSFER_DESTINATION,
    TRANSFER_HAZARD,
    TRANSFER_PROVIDER,
    TRANSFER_TREATMENT,
)

CONTRACT = "noesis-waste-record-v2"
ANSWER_CONTRACT = "noesis-waste-answer-v1"
READ_SCOPE = "knowledge:waste:read"
WRITE_SCOPE = "knowledge:waste:write"
REVIEW_SCOPE = "knowledge:waste:review"
ENVIRONMENT_READ = "knowledge:environment:read"
ENVIRONMENT_WRITE = "knowledge:environment:write"
DEFAULT_NAMESPACE = "environment"
ECONOMIC_DOMAIN = "economics"
PROVIDER_ID = "environment.waste"
BUNDLE = "climate-environment"
FEATURES = {
    "eurostat-waste": "waste-eurostat",
    "eurostat-circular-economy": "waste-eurostat-circular-economy",
    "eea-industry-waste-transfers": "waste-eea-transfers",
    "oecd-municipal-waste": "waste-oecd",
}
RECORD_TYPES = ("release", "series", "definition", "vintage", "observation", "transfer_row")
CHANGE_KINDS = ("new_series", "new_period", "revised_value", "definition_change", "removed_by_source")
SCHEMA_FILE = f"contracts/schemas/jsonschema/{CONTRACT}.json"
# Keys that would carry an operator, parent company, address, contact or competent authority (or a person).
PERSONAL_DATA_FIELDS = frozenset({
    "operator", "operator_name", "operatorname", "parent_company", "parentcompany", "parentcompanyname",
    "parent_company_name", "company_name", "facility_name", "facilityname", "address", "street", "street_address",
    "streetname", "postal_code", "postcode", "contact", "contact_person", "contact_name", "email", "phone",
    "telephone", "fax", "competent_authority", "competentauthority", "permit_authority", "permitauthority",
    "authority", "authority_contact", "person_id", "person_name", "full_name", "first_name", "last_name",
    "date_of_birth", "home_address", "national_id",
})
# Keys that would carry a derived, filled, blended or forecast number.
FORBIDDEN_KEYS = frozenset({
    "nowcast", "nowcasted", "forecast", "forecast_value", "projection", "predicted", "imputed", "imputed_value",
    "interpolated", "gap_filled", "filled_value", "filled_year", "blended", "blended_value", "combined_value",
    "average_value", "harmonised_value", "reconciled_value", "derived_value", "derived_rate", "own_rate",
    "recycling_rate_computed", "per_capita", "per_capita_value", "material_flow", "national_total",
    "summed_transfers", "total_transfers", "estimated_cell",
})
MINIMISATION = {
    "decision": "published aggregates and, per facility, only the INSPIRE id, reporting year and the published "
                "transfer quantities, codes and method",
    "stored": "aggregates per place, period and series key with flags, notes, definitions and citations; per facility "
              "only the INSPIRE id (linking to the environment.core facility record), reporting year and the "
              "published transfer quantities, codes and method",
    "redacted": "nothing is acquired that needs redaction: the waste query selects no operator, parent-company, "
                "address, contact or authority column; operator names stay where environment.core already holds them",
    "excluded": "waste-shipment notifications, permit documents, facility inspection records, any person-level field, "
                "and Eurostat or OECD confidential cells (stored as their status only); a derived, filled, blended "
                "or forecast value is refused at write time",
    "retention": "release vintages are kept for provenance; no erasure workflow applies, since no personal field is "
                 "stored",
    "who_may_query": f"principals holding {READ_SCOPE} and access to the namespace",
    "enforced_by": "waste_records.check_item at write time and every MCP answer",
}


class WasteError(ValueError):
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
        raise WasteError("unauthorized", f"{required} and namespace access are required")


def require_scope(scopes: Iterable[str], required: str) -> None:
    scopes = set(scopes or ())
    if "operator" not in scopes and required not in scopes:
        raise WasteError("unauthorized", f"{required} is required for this part of the answer")


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


def _minimised(item: Mapping[str, Any]) -> None:
    leaked = personal_data_paths(dict(item))
    if leaked:
        raise WasteError("personal_data", "waste records never carry operator, parent-company, address, contact or "
                                          "competent-authority fields", fields=leaked)
    derived = forbidden_paths(dict(item))
    if derived:
        raise WasteError("derived_value", "published records carry no derived, filled, blended or forecast value",
                         fields=derived)


def check_item(item: Mapping[str, Any]) -> None:
    """Validate one published item before anything is written (WC02 rules and the WC01 minimisation)."""
    _minimised(item)
    if item.get("kind") == "transfer":
        return check_transfer(item)
    for key in ("provider", "dataset", "native_key", "indicator", "waste_category", "hazard", "activity",
                "operation", "unit", "area", "periodicity", "definition"):
        if not item.get(key):
            raise WasteError("invalid_record", f"a waste series states its {key}")
    if item["provider"] not in SERIES_PROVIDERS:
        raise WasteError("invalid_record", f"a series provider is one of {SERIES_PROVIDERS}")
    if dict(item["indicator"]).get("concept") not in CONCEPTS:
        raise WasteError("invalid_record", f"indicator concept is one of {CONCEPTS}")
    if item["periodicity"] not in PERIODICITIES:
        raise WasteError("invalid_record", f"periodicity is one of {PERIODICITIES}")
    periods = set()
    for obs in item.get("observations") or []:
        if obs.get("status") not in STATUSES:
            raise WasteError("invalid_record", f"each observation states its status ({STATUSES})")
        if obs["status"] != "reported" and obs.get("value") is not None:
            raise WasteError("invalid_record", "a confidential or unpublished cell carries no value (never a zero)")
        if obs["period"] in periods:
            raise WasteError("invalid_record", "a series states a period twice")
        if item["periodicity"] == "biennial" and obs.get("filled"):
            raise WasteError("derived_value", "a biennial gap is never filled")
        periods.add(obs["period"])
    return None


def check_transfer(item: Mapping[str, Any]) -> None:
    """A facility transfer row: the keys and codes as published, a quantity, never a zero for a missing row."""
    if item.get("provider") != TRANSFER_PROVIDER:
        raise WasteError("invalid_record", f"transfer rows come from {TRANSFER_PROVIDER}")
    for key in ("inspire_id", "reporting_year", "hazardous", "treatment", "destination", "unit", "method",
                "definition"):
        if item.get(key) in (None, "", {}):
            raise WasteError("invalid_record", f"a transfer row states its {key}")
    if dict(item["hazardous"]).get("code") not in TRANSFER_HAZARD or \
            dict(item["treatment"]).get("code") not in TRANSFER_TREATMENT or \
            dict(item["destination"]).get("code") not in TRANSFER_DESTINATION:
        raise WasteError("invalid_record", "hazardousness, R/D and destination use the audited codes")
    method = dict(item["method"]).get("code")
    if method is not None and method not in METHOD_CODES:
        raise WasteError("invalid_record", f"the method code is one of {sorted(METHOD_CODES)}")
    if item.get("quantity") is None:
        raise WasteError("invalid_record", "a stored transfer row carries its published quantity; a row the "
                                           "facility did not report is absent, never a zero")
    if dict(item["unit"]).get("code") != "t":
        raise WasteError("invalid_record", "transfer quantities are stored in tonnes as published")


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


def selected_features(conn: Any) -> list[str]:
    """The optional waste features selected in the active composition plan (all default off)."""
    return sorted(f for f in _selected_features(conn) if f in FEATURES.values())


def feature_enabled(conn: Any, provider: str | None = None) -> bool:
    chosen = selected_features(conn)
    return bool(chosen) if provider is None else FEATURES[provider] in chosen


def readiness(conn: Any, namespace: str = DEFAULT_NAMESPACE) -> dict[str, Any]:
    from src.kb.waste_store import WasteStore

    ready = table_exists(conn, "waste_releases")
    store = WasteStore(conn, initialize=False)
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        releases = 0
        if ready:
            releases = int(conn.execute("SELECT count(*) FROM waste_releases WHERE namespace=? AND provider=?",
                                        [namespace, provider]).fetchone()[0])
        state = store.provider_state(namespace, provider)
        providers[provider] = {"delivers": contract["delivers"], "access_decision": contract["status"],
                               "live_verification": LIVE_VERIFICATION[provider]["status"],
                               "feature": FEATURES[provider], "releases": releases,
                               "stale": state["stale"], "reason": state.get("reason")}
    chosen = selected_features(conn)
    return {
        "provider": PROVIDER_ID,
        "features": {f: f in chosen for f in FEATURES.values()},
        "selected": bool(chosen),
        "stores_ready": ready,
        "series_storage": "economic_indicators, economic_series_map, economic_vintages and dataset_observations",
        "transfer_storage": "environment_records, environment_vintages and environment_values (environment.core), "
                            "located at the environment.core eea-industry facility records",
        "links": {
            "environment.core facilities": {"status": "available" if table_exists(conn, "environment_records")
                                            else "provider_absent"},
            "chemicals.substances": {"status": "available" if table_exists(conn, "substance_records")
                                     else "provider_absent"},
            "products": {"status": "available" if table_exists(conn, "product_records") else "provider_absent"},
        },
        "providers": providers,
        "exclusions": list(EXCLUSIONS),
        "never": NEVER_SENTENCE,
        "minimisation": MINIMISATION["decision"],
        "note": "offline fixture evidence and live evidence are reported per release (evidence_origin); no provider "
                "is live until a dated run verifies it (WC13)",
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
            "provenance": {"kind": "imported", "source": "packs/climate-environment"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(registry.register(definition, f"waste-schema:{name}:2.0.0:{digest(content)[:16]}",
                                         principal_id=principal_id, scopes=set(scopes)))
    return results


__all__ = [
    "ANSWER_CONTRACT",
    "CHANGE_KINDS",
    "CONTRACT",
    "DEFAULT_NAMESPACE",
    "ECONOMIC_DOMAIN",
    "EXCLUSIONS",
    "FEATURES",
    "MINIMISATION",
    "PROVIDER_ID",
    "READ_SCOPE",
    "RECORD_TYPES",
    "REVIEW_SCOPE",
    "WRITE_SCOPE",
    "WasteError",
    "authorize",
    "canonical",
    "check_item",
    "check_transfer",
    "digest",
    "feature_enabled",
    "forbidden_paths",
    "iso",
    "personal_data_paths",
    "readiness",
    "register_schemas",
    "require_scope",
    "selected_features",
    "table_exists",
    "to_ms",
]
