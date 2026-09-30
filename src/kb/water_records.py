"""Water and hydrology records for the Climate and Environment pack: stations, observations, water bodies, assessments.

``noesis-water-record-v1`` (#2582, WA02 #2593) extends the Climate and
Environment record model (:mod:`src.kb.environment_records`: same scopes,
canonical JSON and digest rules, source/revision/as-of on every record) with
four record types, each a *statement* as one provider published it:

* **station** - a gauging station or monitoring location (PEGELONLINE station
  UUID and number, USGS monitoring-location id): name, river/water as
  published, location (WGS84 as published), gauge zero or vertical datum with
  its validity, the published time series and the published characteristic
  values (thresholds). A moved location or a new gauge zero is a new revision
  of the station; the store keeps the vintages.
* **observation** - one published value of one parameter at one station and
  time: parameter code, unit, timestamp, value and quality state *as
  published* (PEGELONLINE raw data is ``provisional``; USGS ``Provisional`` /
  ``Approved``) with per-value qualifiers. A provisional value replaced by an
  approved one is a new revision; nothing is resampled, interpolated or gap
  filled.
* **water_body** - a Water Framework Directive water body (EU code) with its
  name, category, country and river basin district as reported.
* **assessment** - the status of one water body in one reporting cycle
  (ecological status or potential, chemical status, the assessment years and
  status elements) exactly as reported. Cycles are separate records and are
  never merged into one status.

Nothing here forecasts, interpolates, fills gaps, assesses status or scores
flood risk: :data:`FORBIDDEN_KEYS` rejects such fields. Personal data are
excluded by the WA01 minimisation decision and rejected at write time
(:data:`PERSONAL_KEYS`). Unknown values are listed in ``unknowns``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from src.kb.environment_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    canonical,
    digest,
)

CONTRACT = "noesis-water-record-v1"
IDENTITY_CONTRACT = "noesis-water-identity-match-v1"
LINK_CONTRACT = "noesis-water-link-v1"
VALUE_ANSWER_CONTRACT = "noesis-water-value-v1"
PLACE_ANSWER_CONTRACT = "noesis-water-place-v1"
STATUS_ANSWER_CONTRACT = "noesis-water-status-history-v1"
NOTIFICATION_CONTRACT = "noesis-water-notification-v1"
SOURCE_PACK = "climate-environment-water"
RECORD_TYPES = ("station", "observation", "water_body", "assessment")
PROVIDERS = ("pegelonline", "usgs", "eea-wise")
SUBJECT_KINDS = {"station": "station", "observation": "observation", "water_body": "water_body",
                 "assessment": "water_body"}
EVENTS = ("published", "removed")
QUALITY_STATES = ("provisional", "approved", "unknown")
# Canonical parameter names; the published parameter code is always kept beside them.
PARAMETERS = {"water_level": "water level / gage height", "discharge": "discharge",
              "water_temperature": "water temperature"}
WATER_BODY_CATEGORIES = ("river", "lake", "transitional", "coastal", "territorial", "groundwater", "unknown")
# Fields that would turn records into forecasts, gap-filled series, Noesis status assessments or risk scores.
FORBIDDEN_KEYS = frozenset({
    "forecast", "forecast_value", "predicted", "prediction", "interpolated", "interpolation", "gap_filled",
    "filled_value", "resampled", "flood_risk", "risk_score", "flood_risk_score", "exceedance_probability",
    "return_period", "derived_status", "noesis_status", "overall_status", "merged_status", "trend",
})
# WA01 data-minimisation decision: no personal data is stored. These keys never enter a record.
PERSONAL_KEYS = frozenset({
    "contact", "contact_name", "contact_person", "person", "person_name", "email", "e_mail", "phone",
    "telephone", "observer", "observer_name", "field_personnel", "party", "technician", "address",
})
NEVER = (
    "forecast floods or water levels",
    "interpolate, resample or fill gaps in a published series",
    "assess water-body status or merge reporting cycles into one status",
    "score flood risk",
    "store personal data of source staff or contacts",
)
NEVER_SENTENCE = ("Values, quality states and statuses as each gauge operator and reporting authority published "
                  "them: no flood forecasting, no interpolation or gap filling, no Noesis status assessment and no "
                  "flood-risk scoring.")
MINIMISATION = {
    "decision": "no-personal-data",
    "stored": "station, observation, water-body and assessment facts published by the gauge operator or reporting "
              "authority; organisational names (agency, competent authority) only",
    "excluded": sorted(PERSONAL_KEYS),
    "redacted": "none needed: selected fields carry no personal data",
    "retention": "revisions are kept with the record; removals by the source are tombstone revisions",
    "who_may_query": "principals with knowledge:environment:read and namespace access",
    "enforcement": "statement() rejects personal keys at write time; parsers drop them and report the drop",
}

_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # record type: (required non-null, optional - null goes to unknowns)
    "station": (("native_id", "name", "location"),
                ("number", "river", "agency", "datum", "km", "timeseries", "thresholds", "site_type",
                 "hydrologic_unit_code", "country_code", "region", "related_identifiers", "drainage_area")),
    "observation": (("station_id", "parameter", "parameter_code", "time", "value", "unit", "quality"),
                    ("statistic", "qualifiers", "time_series_id", "last_modified", "gauge_zero")),
    "water_body": (("eu_code", "name", "category"),
                   ("country_code", "river_basin_district", "reporting_cycle", "geometry", "related_identifiers")),
    "assessment": (("eu_code", "reporting_cycle", "cycle_year"),
                   ("water_body_name", "ecological", "chemical", "elements", "country_code",
                    "river_basin_district", "natural_status")),
}
_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")


class WaterError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    """Environment scope plus namespace access (operator bypasses), as for every environment record."""
    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = ({f"namespace:{namespace}:write"} if write
              else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"})
    if required not in scopes or not needed & scopes:
        raise WaterError("unauthorized", f"{required} and namespace access are required")


def require(scopes: Iterable[str], *required: str) -> None:
    scopes = set(scopes or ())
    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise WaterError("unauthorized", f"{missing[0]} scope is required")


def _fail(message: str, code: str = "invalid_water_record") -> None:
    raise WaterError(code, message)


def quality(state_as_published: Any, *, provider: str) -> dict[str, Any]:
    """The published quality/approval state mapped to provisional/approved/unknown; the published text is kept."""
    text = str(state_as_published or "").strip()
    folded = text.casefold()
    if provider == "pegelonline":
        return {"state": "provisional", "as_published": text or "raw data (Rohdaten)",
                "basis": "PEGELONLINE publishes unchecked raw data only; no approval state is published"}
    if folded in {"approved", "a"}:
        state = "approved"
    elif folded in {"provisional", "p"}:
        state = "provisional"
    else:
        state = "unknown"
    return {"state": state, "as_published": text or None, "basis": "approval status published per value"}


def personal_keys(value: Any, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in PERSONAL_KEYS:
                found.append(f"{path}{key}")
            found += personal_keys(item, f"{path}{key}.")
    elif isinstance(value, list):
        for item in value:
            found += personal_keys(item, path)
    return found


# ------------------------------------------------------------------ statements


def _instant(value: Any, field: str, *, optional: bool = True) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str) or not _INSTANT.fullmatch(value):
        _fail(f"{field} must be an ISO-8601 date or instant as published")


def _forbidden(value: Any, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            folded = str(key).casefold()
            if folded in FORBIDDEN_KEYS:
                _fail(f"{path}{key} is a forecast, gap-filled, assessed or risk field and is never stored",
                      "forbidden_field")
            if folded in PERSONAL_KEYS:
                _fail(f"{path}{key} is personal data excluded by the minimisation decision", "personal_field")
            _forbidden(item, f"{path}{key}.")
    elif isinstance(value, list):
        for item in value:
            _forbidden(item, path)


def _number(value: Any, field: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
        _fail(f"{field} is a number as published")


def _typed(record_type: str, published: Mapping[str, Any]) -> None:
    if record_type == "station":
        location = published["location"]
        if not isinstance(location, Mapping):
            _fail("location is an object")
        lat, lon = location.get("latitude"), location.get("longitude")
        if (lat is not None or lon is not None) and (
                type(lat) not in (int, float) or type(lon) not in (int, float) or not -90 <= lat <= 90
                or not -180 <= lon <= 180):
            _fail("location is WGS84 latitude/longitude as published")
        datum = published.get("datum")
        if datum is not None:
            if not isinstance(datum, Mapping):
                _fail("datum is an object")
            gauge_zero = datum.get("gauge_zero")
            if gauge_zero is not None:
                _number(gauge_zero.get("value"), "datum.gauge_zero.value")
                if not gauge_zero.get("unit"):
                    _fail("a gauge zero states its unit (height reference) as published")
                _instant(gauge_zero.get("valid_from"), "datum.gauge_zero.valid_from")
        for item in published.get("thresholds") or []:
            if not isinstance(item, Mapping) or not item.get("name") or not item.get("unit") \
                    or not item.get("parameter"):
                _fail("thresholds are published characteristic values with name, parameter and unit")
            _number(item.get("value"), "thresholds.value")
        for item in published.get("timeseries") or []:
            if not isinstance(item, Mapping) or not item.get("parameter_code") or not item.get("unit"):
                _fail("time series name their published parameter code and unit")
    elif record_type == "observation":
        if published["parameter"] not in PARAMETERS:
            _fail(f"parameter is one of {', '.join(PARAMETERS)}; the published code is kept beside it")
        _instant(published["time"], "time", optional=False)
        if "T" not in published["time"] and published.get("statistic") in (None, "instantaneous"):
            _fail("an instantaneous observation keeps its published timestamp")
        _number(published["value"], "value")
        q = published["quality"]
        if not isinstance(q, Mapping) or q.get("state") not in QUALITY_STATES:
            _fail("quality states provisional, approved or unknown with the published text")
        for item in published.get("qualifiers") or []:
            if not isinstance(item, str) or not item:
                _fail("qualifiers are the published qualifier codes per value")
        _instant(published.get("last_modified"), "last_modified")
    elif record_type == "water_body":
        if published["category"] not in WATER_BODY_CATEGORIES:
            _fail(f"category is one of {', '.join(WATER_BODY_CATEGORIES)}")
        geometry = published.get("geometry")
        if geometry is not None and (not isinstance(geometry, Mapping) or geometry.get("type") not in {
                "Polygon", "MultiPolygon", "LineString", "MultiLineString"} or not geometry.get("source")):
            _fail("a water-body geometry is stored only as published, with its source")
    elif record_type == "assessment":
        if not re.fullmatch(r"\d{4}", str(published["cycle_year"])):
            _fail("cycle_year is the reporting year of the cycle")
        for key in ("ecological", "chemical"):
            element = published.get(key)
            if element is not None and (not isinstance(element, Mapping) or "value" not in element
                                        or "label" not in element):
                _fail(f"{key} status keeps the reported value and its label")
        for item in published.get("elements") or []:
            if not isinstance(item, Mapping) or not item.get("element") or "value" not in item:
                _fail("status elements keep element and reported value")


def statement(record_type: str, provider: str, record_key: str, *, subject_name: str | None, as_published: Mapping,
              source: Mapping[str, Any], event: str = "published", effective_date: str | None = None,
              date_basis: str | None = None, unknowns: Iterable[str] = ()) -> dict[str, Any]:
    """Build and validate one provider statement."""
    if record_type not in RECORD_TYPES:
        _fail("unknown water record type")
    if provider not in PROVIDERS:
        _fail("unknown water provider")
    if not isinstance(record_key, str) or not record_key.strip() or len(record_key) > 500:
        _fail("record_key must be nonempty text")
    if event not in EVENTS:
        _fail("event is published or removed")
    required, optional = _FIELDS[record_type]
    published = dict(as_published)
    _forbidden(published)
    extra = set(published) - set(required) - set(optional)
    if extra:
        _fail(f"unsupported {record_type} field {min(extra)}")
    for field in required:
        if published.get(field) is None:
            _fail(f"{record_type}.{field} is required as published")
    _typed(record_type, published)
    if not isinstance(source, Mapping) or not str(source.get("url") or "").startswith("https://"):
        _fail("source.url must be the provider's https URL")
    if personal_keys(dict(source)):
        _fail("source metadata carries personal data", "personal_field")
    _instant(effective_date, "effective.date")
    missing = {f for f in optional if published.get(f) is None}
    return {
        "contract": CONTRACT, "record_type": record_type, "provider": provider, "record_key": record_key,
        "subject": {"key": subject_key(record_type, provider, record_key, published),
                    "kind": SUBJECT_KINDS[record_type], "name": subject_name},
        "as_published": {**{f: None for f in optional}, **published},
        "effective": {"event": event, "date": effective_date, "date_basis": date_basis},
        "source": dict(source), "unknowns": sorted(missing | set(unknowns)),
    }


def station_key(provider: str, native_id: str) -> str:
    return f"{provider}:{native_id}"


def subject_key(record_type: str, provider: str, record_key: str, published: Mapping[str, Any]) -> str:
    if record_type == "station":
        return station_key(provider, record_key)
    if record_type == "observation":
        return station_key(provider, str(published.get("station_id")))
    return f"wfd:{published.get('eu_code') or record_key.split('|', 1)[0]}"


def observation_key(station_id: str, parameter_code: str, statistic: str | None, time: str) -> str:
    return f"{station_id}|{parameter_code}|{statistic or 'instantaneous'}|{time}"


def validate_statement(value: Mapping[str, Any]) -> dict[str, Any]:
    """Re-validate a stored or received statement by rebuilding it (round trip)."""
    if not isinstance(value, Mapping) or value.get("contract") != CONTRACT:
        _fail("not a noesis-water-record-v1 statement")
    required, _optional = _FIELDS.get(value.get("record_type"), ((), ()))
    published = {k: v for k, v in dict(value.get("as_published") or {}).items() if k in required or v is not None}
    effective = dict(value.get("effective") or {})
    return statement(value.get("record_type"), value.get("provider"), value.get("record_key"),
                     subject_name=dict(value.get("subject") or {}).get("name"), as_published=published,
                     source=dict(value.get("source") or {}), event=effective.get("event", "published"),
                     effective_date=effective.get("date"), date_basis=effective.get("date_basis"),
                     unknowns=value.get("unknowns") or ())


# ------------------------------------------------------------------ schema registry

SCHEMA_FILES = {"noesis-water-record": "contracts/schemas/jsonschema/noesis-water-record-v1.json"}


def schema_definitions(root: Any = None) -> dict[str, Any]:
    root = Path(root) if root else Path(__file__).resolve().parents[2]
    return {name: json.loads((root / path).read_text()) for name, path in SCHEMA_FILES.items()}


def register_schemas(conn: Any, *, principal_id: str, scopes: Iterable[str], root: Any = None) -> list[dict]:
    """Register the water record schema in the existing schema registry (idempotent per version)."""
    from src.kb.schema_registry import SchemaRegistry

    registry = SchemaRegistry(conn)
    results = []
    for name, content in sorted(schema_definitions(root).items()):
        definition = {
            "contract": "noesis-schema-module-v1", "name": name, "kind": "schema", "semantic_version": "1.0.0",
            "content": content, "owner": "environment.water", "dependencies": [],
            "compatibility_policy": "backward",
            "provenance": {"kind": "imported", "source": "packs/climate-environment (water feature)"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(registry.register(definition, f"water-schema:{name}:1.0.0:{digest(content)[:16]}",
                                         principal_id=principal_id, scopes=set(scopes)))
    return results


__all__ = [
    "CONTRACT", "FORBIDDEN_KEYS", "MINIMISATION", "NEVER", "NEVER_SENTENCE", "PARAMETERS", "PERSONAL_KEYS",
    "PROVIDERS", "QUALITY_STATES", "READ_SCOPE", "RECORD_TYPES", "REVIEW_SCOPE", "SOURCE_PACK", "WRITE_SCOPE",
    "WaterError", "authorize", "canonical", "digest", "observation_key", "personal_keys", "quality",
    "register_schemas", "require", "statement", "station_key", "subject_key", "validate_statement",
]
