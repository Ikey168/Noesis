"""Natural-hazard record model: events, advisories, alerts and impact estimates as published (NH02, #2308).

``noesis-hazard-record-v1`` is the provider-neutral shape every Natural Hazards
adapter emits (``src/ingestion/hazard_sources.py``). It keeps apart what hazard
feeds blur together:

* **Parameters are quoted, with units.** Magnitude (with its type), depth,
  intensity, wind, pressure, burnt area or return-period thresholds are
  stored exactly as the issuing body published them: decimal text plus the
  published unit. Nothing is converted, averaged or recomputed.
* **Every update is a revision.** A record is keyed by provider, record type
  and native identifier; each publisher update (USGS ``updated``, EMSC
  ``lastupdate``, a GDACS episode, an NHC advisory number, an EFFIS
  ``LASTUPDATE``, a GloFAS issue time) is appended by
  :class:`~src.kb.hazards_store.HazardStore` and never overwrites the previous
  one. ``revision_key`` names the publisher's own revision marker.
* **Estimates stay the publisher's.** PAGER alert levels and GDACS alert
  scores are ``impact_estimate`` records carrying the product and its version;
  they are never a Noesis finding, risk score or damage estimate.
* **Modelled output is labelled.** GloFAS flood notifications are the
  publisher's modelled output with model name, version and issue time.
* **Geometry belongs to geospatial.** Record geometry is WGS84 GeoJSON that
  the store projects into ``geospatial_places`` / ``geospatial_geometries``
  through :class:`~src.kb.geospatial.GeospatialStore`; no spatial table is
  added here.
* **Unknowns stay unknown.** Absent values are ``None`` and listed in
  ``unknowns``. No record predicts, scores risk, estimates damage or advises.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

CONTRACT = "noesis-hazard-record-v1"
READ_SCOPE = "knowledge:hazards:read"
WRITE_SCOPE = "knowledge:hazards:write"
REVIEW_SCOPE = "knowledge:hazards:review"
RECORD_TYPES = ("hazard_event", "advisory", "alert", "impact_estimate")
HAZARD_TYPES = ("earthquake", "tropical_cyclone", "flood", "wildfire", "volcano", "drought")
PROVIDERS = ("usgs", "emsc", "gdacs", "nhc", "effis", "glofas")
# What each provider publishes (record types, hazard types). A record outside
# this set is rejected: e.g. EMSC never issues advisories, NHC never scores impact.
PROVIDER_SCOPE = {
    "usgs": ({"hazard_event", "impact_estimate"}, {"earthquake"}),
    "emsc": ({"hazard_event"}, {"earthquake"}),
    "gdacs": ({"hazard_event", "alert", "impact_estimate"}, set(HAZARD_TYPES)),
    "nhc": ({"hazard_event", "advisory"}, {"tropical_cyclone"}),
    "effis": ({"hazard_event"}, {"wildfire"}),
    "glofas": ({"alert"}, {"flood"}),
}
ISSUING_BODIES = {
    "usgs": "U.S. Geological Survey (USGS) Earthquake Hazards Program",
    "emsc": "European-Mediterranean Seismological Centre (EMSC-CSEM)",
    "gdacs": "Global Disaster Alert and Coordination System (GDACS; UN OCHA and European Commission JRC)",
    "nhc": "NOAA National Hurricane Center (NHC)",
    "effis": "European Forest Fire Information System (EFFIS; Copernicus Emergency Management Service)",
    "glofas": "Global Flood Awareness System (GloFAS; Copernicus Emergency Management Service)",
}
# Exclusions every answer carries; tests assert no output field claims otherwise.
NEVER = ("hazard prediction or forecasting by Noesis", "risk scores", "damage or loss estimates beyond quoting the publisher",
         "evacuation, safety or protective-action advice", "attribution of events to climate change or other causes")
ESTIMATE_NOTICE = "the publisher's own estimate, quoted with its product version; not a Noesis finding"
MODEL_NOTICE = "the publisher's modelled output, quoted with model version and issue time; not an observation"
_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ADVISORY_NUMBER = re.compile(r"^[0-9]{1,3}[A-Z]?$")
_GEOMETRY_TYPES = {"Point", "LineString", "Polygon", "MultiPolygon"}


class HazardRecordError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _fail(message, code="invalid_hazard_record"):
    raise HazardRecordError(code, message)


def _text(value, field, *, optional=False, limit=4000):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _fail(f"{field} must be nonempty text")
    return value


def instant(value, field, *, optional=True):
    """An ISO-8601 instant with offset (or a date for day-resolution publishers)."""

    if value is None and optional:
        return None
    if not isinstance(value, str) or not (_INSTANT.fullmatch(value) or _DAY.fullmatch(value)):
        _fail(f"{field} must be an ISO-8601 instant with offset or a date")
    return value


def decimal_text(value, field="value"):
    """Exact decimal text as published, or ``None``; floats are rejected so nothing is rounded."""

    if value is None:
        return None
    if not isinstance(value, str):
        _fail(f"{field} must be decimal text or null, not {type(value).__name__}")
    try:
        number = Decimal(value)
    except InvalidOperation:
        _fail(f"{field} is not a decimal number")
    if not number.is_finite():
        _fail(f"{field} must be finite")
    return value


def _geometry(value, field="geometry"):
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("type") not in _GEOMETRY_TYPES:
        _fail(f"{field} must be WGS84 GeoJSON ({', '.join(sorted(_GEOMETRY_TYPES))}) or null")
    from src.kb.geospatial import GeospatialError, _validate_geometry

    try:
        kind, coordinates = _validate_geometry(value)
    except GeospatialError as exc:
        _fail(f"{field}: {exc}")
    return {"type": kind, "coordinates": coordinates}


def _parameter(item, index):
    if not isinstance(item, dict):
        _fail(f"parameters[{index}] must be an object")
    name = _text(item.get("name"), f"parameters[{index}].name", limit=80)
    unit = item.get("unit")
    if unit is not None:
        _text(unit, f"parameters[{index}].unit", limit=40)
    value = item.get("value")
    if isinstance(value, str) and item.get("kind", "number") == "number":
        decimal_text(value, f"parameters[{index}].value")
    elif value is not None and not isinstance(value, str):
        _fail(f"parameters[{index}].value must be text as published")
    return {"name": name, "value": value, "unit": unit, "kind": item.get("kind", "number"),
            "qualifier": item.get("qualifier"), "as_published": item.get("as_published")}


def _parameters(items):
    if not isinstance(items, list):
        _fail("parameters must be a list")
    result = [_parameter(item, index) for index, item in enumerate(items)]
    names = [(p["name"], p["qualifier"]) for p in result]
    if len(set(names)) != len(names):
        _fail("parameters are unique by name and qualifier")
    return sorted(result, key=lambda p: (p["name"], p["qualifier"] or ""))


def _model(model, *, required):
    if model is None:
        if required:
            _fail("modelled output needs model name, version and issue time", "model_required")
        return None
    if not isinstance(model, dict):
        _fail("model must be an object")
    return {"name": _text(model.get("name"), "model.name", limit=200),
            "version": model.get("version"),
            "issued_at": instant(model.get("issued_at"), "model.issued_at", optional=False),
            "notice": MODEL_NOTICE}


def validate(record: dict[str, Any]) -> dict[str, Any]:
    """Validate one record; returns a normalised copy or raises :class:`HazardRecordError`."""

    if not isinstance(record, dict) or record.get("contract") != CONTRACT:
        _fail(f"contract must be {CONTRACT}")
    record_type, provider = record.get("record_type"), record.get("provider")
    if record_type not in RECORD_TYPES:
        _fail(f"record_type must be one of {', '.join(RECORD_TYPES)}")
    if provider not in PROVIDERS:
        _fail(f"provider must be one of {', '.join(PROVIDERS)}")
    types, hazards = PROVIDER_SCOPE[provider]
    if record_type not in types:
        _fail(f"{provider} does not publish {record_type} records", "not_published_by_provider")
    hazard = record.get("hazard_type")
    if hazard not in hazards:
        _fail(f"{provider} does not publish {hazard!r} records", "not_published_by_provider")
    for forbidden in ("risk_score", "damage_estimate", "prediction", "advice", "safety_advice", "attribution"):
        if forbidden in record:
            _fail(f"{forbidden} is outside the pack's scope", "excluded_field")
    out = {
        "contract": CONTRACT, "record_type": record_type, "provider": provider, "hazard_type": hazard,
        "native_id": _text(record.get("native_id"), "native_id", limit=200),
        "issuing_body": _text(record.get("issuing_body") or ISSUING_BODIES[provider], "issuing_body", limit=300),
        "title": _text(record.get("title"), "title", limit=500),
        "source_url": _text(record.get("source_url"), "source_url", limit=2000),
        "revision_key": _text(str(record.get("revision_key")) if record.get("revision_key") is not None else None,
                              "revision_key", limit=200),
        "published_at": instant(record.get("published_at"), "published_at"),
        "geometry": _geometry(record.get("geometry")),
        "geometry_role": record.get("geometry_role"),
        "countries": sorted({str(c) for c in record.get("countries") or []}),
        "identifiers": {str(k): v for k, v in sorted(dict(record.get("identifiers") or {}).items())},
        "locator": dict(record.get("locator") or {}),
        "unknowns": sorted({str(u) for u in record.get("unknowns") or []}),
        "status": record.get("status"),
    }
    if not out["source_url"].startswith("https://"):
        _fail("source_url must be an https URL")
    if out["published_at"] is None and "published_at" not in out["unknowns"]:
        out["unknowns"] = sorted({*out["unknowns"], "published_at"})
    if record_type == "hazard_event":
        out.update(_event(record))
    elif record_type == "advisory":
        out.update(_advisory(record))
    elif record_type == "alert":
        out.update(_alert(record))
    else:
        out.update(_estimate(record))
    return out


def _event(record):
    status = record.get("status")
    if status is not None:
        _text(status, "status", limit=80)
    merged_into = record.get("merged_into")
    if status == "merged" and not merged_into:
        _fail("a merged event names the event it was merged into")
    return {"event_time": instant(record.get("event_time"), "event_time"),
            "event_end": instant(record.get("event_end"), "event_end"),
            "parameters": _parameters(record.get("parameters") or []),
            "status": status, "status_scheme": record.get("status_scheme"),
            "merged_into": merged_into, "episode_id": record.get("episode_id"),
            "modelled": False}


def _advisory(record):
    number = str(record.get("advisory_number") or "")
    if not _ADVISORY_NUMBER.fullmatch(number):
        _fail("advisory_number is the issuing body's number, e.g. '12' or '12A' (intermediate)")
    issued = instant(record.get("issued_at"), "issued_at", optional=False)
    track = []
    for index, point in enumerate(record.get("forecast_track") or []):
        track.append({"valid_at": instant(point.get("valid_at"), f"forecast_track[{index}].valid_at", optional=False),
                      "label": point.get("label"),
                      "geometry": _geometry(point.get("geometry"), f"forecast_track[{index}].geometry"),
                      "max_wind": point.get("max_wind"), "stage": point.get("stage")})
    warnings = []
    for index, item in enumerate(record.get("watches_warnings") or []):
        warnings.append({"type": _text(item.get("type"), f"watches_warnings[{index}].type", limit=120),
                         "area_text": _text(item.get("area_text"), f"watches_warnings[{index}].area_text"),
                         "action": item.get("action", "in effect"),
                         "geometry": _geometry(item.get("geometry"), f"watches_warnings[{index}].geometry")})
    return {"storm_id": _text(record.get("storm_id"), "storm_id", limit=20),
            "storm_name": record.get("storm_name"),
            "advisory_number": number, "advisory_kind": _text(record.get("advisory_kind"), "advisory_kind", limit=80),
            "issued_at": issued, "event_native_id": _text(record.get("event_native_id"), "event_native_id"),
            "parameters": _parameters(record.get("parameters") or []),
            "forecast_track": track,
            "forecast_basis": "the issuing body's forecast as issued; never extended or recomputed" if track else None,
            "cone": dict(record["cone"]) if record.get("cone") else None,
            "watches_warnings": warnings,
            "status": record.get("status") or "issued"}


def _alert(record):
    level = _text(record.get("level"), "level", limit=80)
    modelled = bool(record.get("modelled"))
    return {"event_native_id": record.get("event_native_id"),
            "level": level, "level_scheme": record.get("level_scheme"),
            "wording": record.get("wording"),
            "episode_id": record.get("episode_id"),
            "issued_at": instant(record.get("issued_at"), "issued_at", optional=False),
            "valid_from": instant(record.get("valid_from"), "valid_from"),
            "valid_to": instant(record.get("valid_to"), "valid_to"),
            "validity_basis": record.get("validity_basis") or "published validity window",
            "thresholds": [dict(t) for t in record.get("thresholds") or []],
            "modelled": modelled,
            "model": _model(record.get("model"), required=modelled),
            "status": record.get("status") or "issued"}


def _estimate(record):
    product = _text(record.get("product"), "product", limit=120)
    version = record.get("product_version")
    if version is None:
        _fail("an impact estimate cites its product version", "product_version_required")
    estimate = dict(record.get("estimate") or {})
    if not estimate:
        _fail("an impact estimate quotes at least one published value")
    return {"event_native_id": _text(record.get("event_native_id"), "event_native_id"),
            "product": product, "product_version": str(version), "estimate": estimate,
            "notice": ESTIMATE_NOTICE, "status": record.get("status") or "published"}


# --------------------------------------------------------------------- builders


def event(provider, native_id, title, *, hazard_type, source_url, revision_key, published_at, event_time,
          parameters, geometry=None, status=None, status_scheme=None, identifiers=None, countries=(),
          event_end=None, merged_into=None, episode_id=None, locator=None, unknowns=(), geometry_role=None):
    return validate({"contract": CONTRACT, "record_type": "hazard_event", "provider": provider, "native_id": native_id,
                     "title": title, "hazard_type": hazard_type, "source_url": source_url, "revision_key": revision_key,
                     "published_at": published_at, "event_time": event_time, "event_end": event_end,
                     "parameters": list(parameters), "geometry": geometry, "geometry_role": geometry_role,
                     "status": status, "status_scheme": status_scheme, "identifiers": identifiers or {},
                     "countries": list(countries), "merged_into": merged_into, "episode_id": episode_id,
                     "locator": locator or {}, "unknowns": list(unknowns)})


def parameter(name, value, unit, *, kind="number", qualifier=None, as_published=None):
    return {"name": name, "value": value, "unit": unit, "kind": kind, "qualifier": qualifier,
            "as_published": as_published}


def parameter_map(record):
    """``{(name, qualifier): parameter}`` for revision comparison."""

    return {(p["name"], p.get("qualifier")): p for p in record.get("parameters") or []}


def parameter_changes(before, after):
    """Every changed parameter (and location/status) between two revisions of one record, as published."""

    changes = []
    old, new = parameter_map(before or {}), parameter_map(after)
    names = [k[0] for k in old] + [k[0] for k in new]
    # A parameter is compared by name (a magnitude re-published with another magnitude type is one change);
    # only a name published several times in one revision is compared per qualifier.
    single = {n for n in names if sum(1 for k in old if k[0] == n) <= 1 and sum(1 for k in new if k[0] == n) <= 1}
    old = {(k[0], None if k[0] in single else k[1]): v for k, v in old.items()}
    new = {(k[0], None if k[0] in single else k[1]): v for k, v in new.items()}

    def shown(p):
        return None if p is None else {"value": p["value"], "unit": p["unit"], "qualifier": p.get("qualifier")}

    for key in sorted(set(old) | set(new), key=lambda k: (k[0], k[1] or "")):
        a, b = old.get(key), new.get(key)
        if shown(a) != shown(b):
            changes.append({"parameter": key[0], "qualifier": key[1], "before": shown(a), "after": shown(b)})
    for field in ("geometry", "status", "level", "wording", "valid_from", "valid_to", "estimate", "product_version",
                  "watches_warnings", "forecast_track"):
        if (before or {}).get(field) != after.get(field) and (before is not None or after.get(field) is not None):
            changes.append({"parameter": field, "qualifier": None, "before": (before or {}).get(field),
                            "after": after.get(field)})
    return changes


def schema_definitions(root=None):
    from pathlib import Path

    base = Path(root) if root else Path(__file__).resolve().parents[2]
    path = base / "contracts/schemas/jsonschema/noesis-hazard-record-v1.json"
    return {CONTRACT: json.loads(path.read_text())}


def register_schemas(conn, *, principal_id, scopes, root=None):
    """Register ``noesis-hazard-record-v1`` in the existing schema registry (idempotent per version)."""

    from src.kb.schema_registry import SchemaRegistry

    registry = SchemaRegistry(conn)
    results = []
    for name, content in sorted(schema_definitions(root).items()):
        definition = {
            "contract": "noesis-schema-module-v1", "name": name, "kind": "schema", "semantic_version": "1.0.0",
            "content": content, "owner": "hazards.core", "dependencies": [], "compatibility_policy": "backward",
            "provenance": {"kind": "imported", "source": "packs/natural-hazards"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(registry.register(definition, f"hazards-schema:{name}:1.0.0:{digest(content)[:16]}",
                                         principal_id=principal_id, scopes=scopes))
    return results
