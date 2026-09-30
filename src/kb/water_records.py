"""Water and hydrology records for the Climate and Environment pack: stations, observations, water bodies, status.

``noesis-water-record-v1`` (#2582, WA02 #2593) follows the Climate and
Environment record rules (:mod:`src.kb.environment_records`: same scopes,
canonical JSON and digest; source, revision and as-of time on every record)
with four record types, each a *statement* as one provider published it:

* **station** - a gauging station or monitoring location keyed by its native
  identifier (PEGELONLINE station UUID, USGS ``monitoring_location_id``) with
  its number, name, river, location *as published*, gauge zero or vertical
  datum with the validity the source states, published characteristic values
  (thresholds) and published identifiers. A changed location or datum is a new
  revision of the same station; earlier revisions stay readable as vintages.
* **observation** - one published value of one parameter at one station and
  one timestamp: parameter and unit as published, the time as published (no
  resampling), the value as exact decimal text (a published missing value stays
  ``None``) and the **quality state** as published (``provisional``,
  ``approved`` or ``unknown``, with the source's own text) plus per-value
  qualifiers. A provisional value later published as approved is a new revision
  of the same observation, never an overwrite.
* **water_body** - an EU water body keyed by its EU code with the geometry the
  reporting dataset publishes and that dataset's vintage.
* **water_body_assessment** - the Water Framework Directive status of one water
  body in **one reporting cycle** (keyed by EU code and cycle): ecological and
  chemical status elements as published. Cycles are never merged.

Nothing here forecasts, interpolates, fills gaps or assesses: :data:`FORBIDDEN_KEYS`
rejects such fields. The sources carry no personal data; the WA01 minimisation
decision (:data:`MINIMISATION`) rejects personal fields at write time
(:data:`PERSONAL_KEYS`) and strips them from every answer (:func:`minimise`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
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
VALUE_ANSWER_CONTRACT = "noesis-water-value-at-v1"
PLACE_ANSWER_CONTRACT = "noesis-water-place-v1"
STATUS_ANSWER_CONTRACT = "noesis-water-status-history-v1"
NOTIFICATION_CONTRACT = "noesis-water-notification-v1"
BUNDLE_CONTRACT = "noesis-water-evidence-bundle-v1"
SOURCE_PACK = "climate-environment-water"
RECORD_TYPES = ("station", "observation", "water_body", "water_body_assessment")
PROVIDERS = ("pegelonline", "usgs", "eea-wise")
SUBJECT_KINDS = {"station": "station", "observation": "observation", "water_body": "water-body",
                 "water_body_assessment": "water-body"}
EVENTS = ("published", "removed")
QUALITY_STATES = ("provisional", "approved", "unknown")
# Published parameter codes -> the quantity they measure (codes themselves are kept as published).
QUANTITIES = {
    ("pegelonline", "W"): "water_level", ("pegelonline", "Q"): "discharge",
    ("usgs", "00065"): "water_level", ("usgs", "00060"): "discharge",
}
DATUM_KINDS = ("gauge-zero", "vertical-datum")
# Fields that would turn records into forecasts, filled series, risk scores or Noesis-derived status: never stored.
FORBIDDEN_KEYS = frozenset({
    "forecast", "forecast_value", "predicted", "prediction", "interpolated", "interpolation", "gap_filled",
    "filled_value", "resampled", "flood_risk", "flood_risk_score", "risk_score", "risk_class", "flood_probability",
    "return_period_estimate", "derived_status", "noesis_status", "own_assessment", "exceedance_probability",
})
# Personal fields (WA01 minimisation decision): none are needed and none are accepted.
PERSONAL_KEYS = frozenset({
    "email", "e_mail", "phone", "telephone", "fax", "contact", "contact_name", "contact_person", "contact_email",
    "observer", "observer_name", "person", "person_name", "operator_person", "landowner", "owner_name",
    "reporter_name", "home_address",
})
MINIMISATION = {
    "decision": "no personal data",
    "finding": "PEGELONLINE, the USGS Water Data APIs and EEA WISE WFD publish stations, measurements and water-body "
               "status of public agencies; none of the selected fields names a natural person",
    "stored": "station and water-body identifiers, names, agencies, locations, datums, thresholds, observations with "
              "quality state and qualifiers, and WFD status elements as published",
    "excluded": sorted(PERSONAL_KEYS),
    "enforcement": "personal fields are rejected when a record is written and removed from every answer; adapters "
                   "drop any such field and name it in the page receipt",
    "retention": "records are kept as immutable revisions for provenance; nothing personal is stored to retain",
    "access": "knowledge:environment:read plus namespace access; identity review needs knowledge:environment:review",
}
NEVER = (
    "forecast floods or water levels",
    "interpolate, resample or fill gaps in a series",
    "make its own water-body status assessment",
    "score flood risk",
    "merge reporting cycles into one status",
    "infer a causal link between an observation and an event",
)
NEVER_SENTENCE = ("Values and status as each source published them: no flood forecasting, no interpolation or gap "
                  "filling, no own status assessment, no flood-risk scoring and no causal link between an "
                  "observation and an event.")
SCHEMA_VERSIONS = {"water-record": "1.0.0", "water-identity-match": "1.0.0", "water-link": "1.0.0",
                   "water-answer": "1.0.0"}

_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # record type: (required non-null, optional - null goes to unknowns)
    "station": (("native_id", "name"),
                ("number", "short_name", "river", "river_km", "agency", "location", "datums", "thresholds",
                 "series", "identifiers", "site_type", "drainage_area", "country_code", "related")),
    "observation": (("station_key", "parameter", "unit", "time", "quality"),
                    ("parameter_name", "quantity", "value", "qualifiers", "statistic", "time_series_id",
                     "last_modified", "reference")),
    "water_body": (("eu_code", "name"),
                   ("category", "country_code", "rbd_code", "geometry", "geometry_vintage", "geometry_crs",
                    "identifiers")),
    "water_body_assessment": (("eu_code", "cycle", "status_elements"),
                              ("name", "category", "country_code", "rbd_code", "ecological_status",
                               "chemical_status", "modification", "reported_year")),
}
_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})?)?$")


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


# ------------------------------------------------------------------ normalisers


def decimal_text(value: Any) -> str | None:
    """A published number as exact decimal text (never rounded, never resampled); ``None`` stays ``None``."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        _fail("a published value is a number, not a boolean")
    text = repr(value) if isinstance(value, float) else str(value).strip()
    try:
        number = Decimal(text)
    except InvalidOperation:
        _fail(f"{value!r} is not a published decimal value")
    if not number.is_finite():
        _fail("published values are finite")
    return text


def quality(state: Any, published: Any, *, basis: str) -> dict[str, Any]:
    """The quality state as the source publishes it, with its own text and where the state is stated."""
    folded = str(state or "").strip().casefold()
    mapped = folded if folded in QUALITY_STATES else "unknown"
    return {"state": mapped, "published": None if published in (None, "") else str(published), "basis": basis}


def quantity(provider: str, parameter: str) -> str:
    return QUANTITIES.get((provider, str(parameter)), "other")


def minimise(value: Any) -> Any:
    """Remove personal fields (WA01 decision) from an answer; nothing personal is stored, so this is defensive."""
    if isinstance(value, Mapping):
        return {k: minimise(v) for k, v in value.items() if str(k).casefold() not in PERSONAL_KEYS}
    if isinstance(value, list):
        return [minimise(v) for v in value]
    return value


# ------------------------------------------------------------------ statements


def _text(value: Any, field: str, *, limit: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _fail(f"{field} must be nonempty text")
    return value


def _instant(value: Any, field: str) -> None:
    if value is not None and (not isinstance(value, str) or not _INSTANT.fullmatch(value)):
        _fail(f"{field} must be an ISO-8601 date or instant as published")


def _scan(value: Any, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            folded = str(key).casefold()
            if folded in FORBIDDEN_KEYS:
                _fail(f"{path}{key} is a forecast, filled, risk or derived field and is never stored",
                      "forbidden_field")
            if folded in PERSONAL_KEYS:
                _fail(f"{path}{key} is a personal field; the WA01 minimisation decision excludes it",
                      "personal_field")
            _scan(item, f"{path}{key}.")
    elif isinstance(value, list):
        for item in value:
            _scan(item, path)


def _location(location: Any) -> None:
    if location is None:
        return
    lat, lon = (location or {}).get("latitude"), (location or {}).get("longitude")
    if type(lat) not in (int, float) or type(lon) not in (int, float) or not -90 <= lat <= 90 \
            or not -180 <= lon <= 180:
        _fail("location is WGS84 latitude/longitude as published")
    if not location.get("crs"):
        _fail("location states the coordinate reference system the source publishes")


def _typed(record_type: str, published: Mapping[str, Any]) -> None:
    if record_type == "station":
        _location(published.get("location"))
        for datum in published.get("datums") or []:
            if not isinstance(datum, Mapping) or datum.get("kind") not in DATUM_KINDS or not datum.get("unit"):
                _fail("datums state their kind (gauge-zero or vertical-datum) and unit as published")
            decimal_text(datum.get("value"))
            _instant(datum.get("valid_from"), "datum.valid_from")
        for threshold in published.get("thresholds") or []:
            if not isinstance(threshold, Mapping) or not threshold.get("shortname") or not threshold.get("unit"):
                _fail("thresholds are published characteristic values with their name and unit")
            decimal_text(threshold.get("value"))
        for ref in published.get("identifiers") or []:
            if not isinstance(ref, Mapping) or not ref.get("scheme") or not ref.get("value"):
                _fail("identifiers state a scheme and a value as published")
        for ref in published.get("related") or []:
            if not isinstance(ref, Mapping) or not ref.get("scheme") or not ref.get("value") or not ref.get("relation"):
                _fail("related records state scheme, value and the relation the source publishes")
    elif record_type == "observation":
        _instant(published["time"], "time")
        decimal_text(published.get("value"))
        state = published["quality"]
        if not isinstance(state, Mapping) or state.get("state") not in QUALITY_STATES or not state.get("basis"):
            _fail("an observation keeps its quality state (provisional, approved or unknown) and its basis")
        if not isinstance(published.get("qualifiers") or [], list):
            _fail("qualifiers are a list as published")
    elif record_type == "water_body":
        geometry = published.get("geometry")
        if geometry is not None:
            if not isinstance(geometry, Mapping) or geometry.get("type") not in {
                    "Point", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}:
                _fail("geometry is the GeoJSON geometry the reporting dataset publishes")
            if not published.get("geometry_vintage"):
                _fail("a published geometry names its dataset vintage")
    elif record_type == "water_body_assessment":
        elements = published["status_elements"]
        if not isinstance(elements, list) or not elements:
            _fail("an assessment keeps the status elements the cycle publishes")
        for element in elements:
            if not isinstance(element, Mapping) or not element.get("element") or "value" not in element \
                    or not element.get("published_field"):
                _fail("status elements name the element, its value and the field that published it")
        if not re.fullmatch(r"\d{4}", str(published["cycle"])):
            _fail("cycle is the reporting cycle year the database publishes")


def statement(record_type: str, provider: str, record_key: str, *, subject_name: str | None,
              as_published: Mapping[str, Any], source: Mapping[str, Any], event: str = "published",
              effective_date: str | None = None, date_basis: str | None = None,
              unknowns: Iterable[str] = ()) -> dict[str, Any]:
    """Build and validate one provider statement."""
    if record_type not in RECORD_TYPES:
        _fail("unknown water record type")
    if provider not in PROVIDERS:
        _fail("unknown water provider")
    if record_type in {"water_body", "water_body_assessment"} and provider != "eea-wise":
        _fail("water bodies and WFD assessments come from EEA WISE")
    _text(record_key, "record_key", limit=500)
    if event not in EVENTS:
        _fail("event is published or removed")
    required, optional = _FIELDS[record_type]
    published = dict(as_published)
    extra = set(published) - set(required) - set(optional)
    _scan(published)
    if extra:
        _fail(f"unsupported {record_type} field {min(extra)}")
    for field in required:
        if published.get(field) is None:
            _fail(f"{record_type}.{field} is required as published")
    _typed(record_type, published)
    if not isinstance(source, Mapping) or not str(source.get("url") or "").startswith("https://"):
        _fail("source.url must be the provider's https URL")
    _scan(dict(source))
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


def station_subject(provider: str, native_id: str) -> str:
    return f"{provider}:{native_id}"


def water_body_subject(eu_code: str) -> str:
    return f"eu-wb:{eu_code}"


def observation_key(station_key: str, parameter: str, time: str, statistic: str | None = None) -> str:
    return "|".join([station_key, str(parameter), *([statistic] if statistic else []), time])


def subject_key(record_type: str, provider: str, record_key: str, published: Mapping[str, Any]) -> str:
    if record_type == "station":
        return station_subject(provider, record_key)
    if record_type == "observation":
        return str(published.get("station_key") or "")
    return water_body_subject(str(published.get("eu_code") or ""))


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


def changes(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> list[str]:
    """Which published aspects differ between two revisions of one record (for revision labels and notices)."""
    if before is None:
        return ["created"]
    old, new = before["as_published"], after["as_published"]
    if before["effective"]["event"] != after["effective"]["event"]:
        return ["removed" if after["effective"]["event"] == "removed" else "republished"]
    groups = {
        "station": {"location": ("location",), "datum": ("datums",), "thresholds": ("thresholds",),
                    "name": ("name", "short_name"), "river": ("river", "river_km")},
        "observation": {"quality": ("quality",), "value": ("value",), "qualifiers": ("qualifiers",)},
        "water_body": {"geometry": ("geometry", "geometry_vintage", "geometry_crs"), "name": ("name",)},
        "water_body_assessment": {"status": ("status_elements", "ecological_status", "chemical_status")},
    }[after["record_type"]]
    found = [label for label, fields in groups.items() if any(old.get(f) != new.get(f) for f in fields)]
    grouped = {f for fields in groups.values() for f in fields}
    if any(old.get(k) != new.get(k) for k in set(old) | set(new) if k not in grouped):
        found.append("other")
    return found or ["other"]


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
    "CONTRACT", "FORBIDDEN_KEYS", "MINIMISATION", "NEVER", "NEVER_SENTENCE", "PERSONAL_KEYS", "PROVIDERS",
    "QUALITY_STATES", "READ_SCOPE", "RECORD_TYPES", "REVIEW_SCOPE", "SOURCE_PACK", "WRITE_SCOPE", "WaterError",
    "authorize", "canonical", "changes", "decimal_text", "digest", "minimise", "observation_key", "quality",
    "quantity", "register_schemas", "require", "statement", "station_subject", "subject_key", "validate_statement",
    "water_body_subject",
]
