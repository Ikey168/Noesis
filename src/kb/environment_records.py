"""Environmental record model: stations, series, facilities, grid events and vintages.

``noesis-environment-record-v1`` is the provider-neutral shape every Climate
and Environment adapter emits (E02). It keeps apart what environmental
sources blur together:

* **Kind is mandatory.** Every series and grid event is an ``observation``
  (measured/reported), ``model`` output (including reanalysis such as ERA5)
  or a ``forecast``. There is no default: a record without a kind is
  rejected, and a provider or dataset that only publishes model output or
  forecasts can never emit an ``observation`` (:data:`PROVIDER_KINDS`).
* **Units travel with every value.** Native unit text is kept as published
  and mapped to a pint expression (:data:`UNIT_EXPRESSIONS`); normalisation
  goes through :func:`src.integrations.units.convert_physical` (the existing
  pint-based owner), with its receipt retained.
* **Places are the geospatial owner's.** Stations, grid cells and facilities
  carry WGS84 point geometry that the store projects into
  ``geospatial_places`` / ``geospatial_geometries`` and the feature store; no
  spatial table is added here.
* **Unknowns stay unknown.** Absent values are ``None`` and listed in
  ``unknowns``; nothing is defaulted, inferred or attributed. No record
  asserts a compliance determination, an attribution or a projection.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from functools import lru_cache
from typing import Any

CONTRACT = "noesis-environment-record-v1"
DOSSIER_CONTRACT = "noesis-environment-dossier-v1"
READ_SCOPE = "knowledge:environment:read"
WRITE_SCOPE = "knowledge:environment:write"
REVIEW_SCOPE = "knowledge:environment:review"
RECORD_TYPES = ("station", "observation_series", "facility", "grid_event", "indicator_vintage")
KINDS = ("observation", "model", "forecast")
AGGREGATIONS = ("instant", "mean", "sum", "total", "max", "min", "unknown")
VALUE_STATUS = ("provisional", "validated", "unknown")
GRID_EVENT_TYPES = ("generation", "load", "unavailability")
UNAVAILABILITY_KINDS = ("planned", "unplanned", "unknown")
LOCATION_KINDS = ("station", "grid-cell", "facility", "bidding-zone")
PROVIDERS = ("openaq", "uba", "entsoe", "smard", "eea-industry", "eu-ets", "umweltatlas",
             "open-meteo-archive", "open-meteo-forecast", "dwd", "copernicus-cams")
# What each provider (dataset) can publish. A value outside this set is
# rejected at validation: reanalysis and forecasts can never become observations.
PROVIDER_KINDS = {
    "openaq": {"observation"},
    "uba": {"observation"},
    "entsoe": {"observation", "forecast"},
    "smard": {"observation", "forecast"},
    "eea-industry": {"observation"},
    "eu-ets": {"observation"},
    "umweltatlas": set(),  # feature layers only (no series)
    "open-meteo-archive": {"model"},
    "open-meteo-forecast": {"forecast"},
    "dwd": {"observation"},
    "copernicus-cams": set(),  # not implemented
}
# Native unit text (as published) -> pint expression. Anything else is kept as
# published and marked ``unit_unmapped``; it is never guessed.
UNIT_EXPRESSIONS = {
    "µg/m³": "microgram / meter ** 3", "µg/m3": "microgram / meter ** 3", "ug/m3": "microgram / meter ** 3",
    "ug/m³": "microgram / meter ** 3", "mg/m³": "milligram / meter ** 3", "mg/m3": "milligram / meter ** 3",
    "ppm": "ppm", "ppb": "ppb",
    "°C": "degree_Celsius", "degC": "degree_Celsius", "K": "kelvin",
    "%": "percent", "mm": "millimeter", "m/s": "meter / second", "km/h": "kilometer / hour",
    "hPa": "hectopascal", "W/m²": "watt / meter ** 2",
    "MW": "megawatt", "MWh": "megawatt_hour", "GWh": "gigawatt_hour", "kW": "kilowatt",
    "kg": "kilogram", "t": "tonne", "tCO2e": "tonne",
}
# Canonical unit per dimension for normalised values (pint expressions).
CANONICAL_UNITS = {
    "[mass] / [length] ** 3": "microgram / meter ** 3",
    "[temperature]": "degree_Celsius",
    "[length]": "millimeter",
    "[mass]": "kilogram",
    "[mass] * [length] ** 2 / [time] ** 3": "megawatt",
    "[mass] * [length] ** 2 / [time] ** 2": "megawatt_hour",
    "[length] / [time]": "meter / second",
    "[mass] / [time] ** 3": "watt / meter ** 2",
    "[mass] / [length] / [time] ** 2": "hectopascal",
    "dimensionless": "percent",
}
_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:\d{2}))?$")
_DURATION = re.compile(r"^P(\d+Y)?(\d+M)?(\d+W)?(\d+D)?(T(\d+H)?(\d+M)?(\d+S)?)?$")


class EnvironmentRecordError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _fail(message, code="invalid_environment_record"):
    raise EnvironmentRecordError(code, message)


def _text(value, field, *, optional=False, limit=2000):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _fail(f"{field} must be nonempty text")
    return value


def _instant(value, field, *, optional=True):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _INSTANT.fullmatch(value):
        _fail(f"{field} must be an ISO-8601 date or instant with offset")
    return value


def decimal_text(value, field="value"):
    """Exact decimal text, or ``None`` for a published missing value; floats are rejected."""

    if value is None:
        return None
    if not isinstance(value, str):
        _fail(f"{field} must be a decimal string or null, not {type(value).__name__}")
    try:
        number = Decimal(value)
    except InvalidOperation:
        _fail(f"{field} is not a decimal number")
    if not number.is_finite():
        _fail(f"{field} must be finite")
    return value


def _point(geometry, field="geometry"):
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        _fail(f"{field} must be a WGS84 GeoJSON Point")
    coords = geometry.get("coordinates")
    if (not isinstance(coords, list) or len(coords) != 2
            or any(type(v) not in (int, float) for v in coords)
            or not -180 <= coords[0] <= 180 or not -90 <= coords[1] <= 90):
        _fail(f"{field} needs [longitude, latitude] in EPSG:4326")
    return {"type": "Point", "coordinates": [float(coords[0]), float(coords[1])]}


def unit_expression(unit):
    """The pint expression for a published unit, or ``None`` when unmapped."""

    return UNIT_EXPRESSIONS.get(unit) if isinstance(unit, str) else None


@lru_cache(maxsize=256)
def _dimension(expression):
    return str(_registry().Quantity(1, expression).dimensionality) if expression else None


@lru_cache(maxsize=1)
def _registry():
    import pint

    return pint.UnitRegistry()


@lru_cache(maxsize=256)
def _affine(expression, target_expression):
    """Calibrate the exact affine map of a pint conversion (two receipts: at 0 and at 1)."""

    from src.integrations.units import convert_physical

    zero = convert_physical("0", expression, target_expression, precision=12)
    one = convert_physical("1", expression, target_expression, precision=12)
    intercept = Decimal(zero["result"]["value"])
    slope = Decimal(one["result"]["value"]) - intercept
    return slope, intercept, one["result"]["dimension"], one["sha256"]


def normalise(value, unit, *, target=None, precision=6):
    """Convert a published value through the existing pint owner (``convert_physical``).

    The conversion map for a unit pair is calibrated once from two
    ``convert_physical`` receipts (units are linear or affine) and applied with
    exact decimals; the receipt digest is returned with the value. ``target``
    defaults to the canonical unit of the value's dimension. An unmapped unit or
    a missing value yields ``None`` rather than a guess.
    """

    expression = unit_expression(unit)
    if value is None or expression is None:
        return None
    if target is None:
        target = CANONICAL_UNITS.get(_dimension(expression))
        if target is None:
            return None
    target_expression = UNIT_EXPRESSIONS.get(target, target)
    slope, intercept, dimension, receipt = _affine(expression, target_expression)
    result = (Decimal(str(value)) * slope + intercept).quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_EVEN)
    return {"value": str(result), "unit": target_expression, "dimension": dimension, "receipt_sha256": receipt,
            "method": "pint convert_physical (affine calibration receipt)"}


def _kind(provider, kind, field="kind"):
    if kind is None:
        _fail(f"{field} is mandatory and never defaulted", "kind_required")
    if kind not in KINDS:
        _fail(f"{field} must be one of {', '.join(KINDS)}", "invalid_kind")
    allowed = PROVIDER_KINDS.get(provider, set())
    if kind not in allowed:
        _fail(f"provider {provider} publishes {sorted(allowed) or 'no series'}, not {kind}", "kind_not_published")
    return kind


def _model(kind, model):
    """Model/forecast metadata; forecasts keep an issue-time slot (possibly unknown)."""

    if kind == "observation":
        if model not in (None, {}):
            _fail("an observation carries no model metadata", "kind_conflict")
        return None
    if not isinstance(model, dict) or not str(model.get("name") or "").strip():
        _fail(f"{kind} series need model/dataset metadata with a name", "model_required")
    allowed = {"name", "dataset", "version", "issue_time", "issue_time_basis", "grid_resolution_m",
               "grid_resolution_basis", "documentation"}
    if set(model) - allowed:
        _fail("unsupported model metadata field")
    if kind == "forecast" and "issue_time" not in model:
        _fail("forecasts record an issue_time (null when the provider does not publish one)", "issue_time_required")
    _instant(model.get("issue_time"), "model.issue_time")
    resolution = model.get("grid_resolution_m")
    if resolution is not None and (type(resolution) not in (int, float) or resolution <= 0):
        _fail("grid_resolution_m must be a positive distance")
    return dict(model)


def _values(values, unit):
    if not isinstance(values, list) or len(values) > 50_000:
        _fail("values must be a bounded list")
    result, seen = [], set()
    for index, item in enumerate(values):
        if not isinstance(item, dict) or set(item) - {"start", "end", "value", "status", "flags", "quality"}:
            _fail(f"values[{index}] uses start/end/value/status/flags/quality")
        start = _instant(item.get("start"), f"values[{index}].start", optional=False)
        _instant(item.get("end"), f"values[{index}].end")
        if start in seen:
            _fail(f"duplicate period start {start}")
        seen.add(start)
        status = item.get("status", "unknown")
        if status not in VALUE_STATUS:
            _fail(f"values[{index}].status must be one of {', '.join(VALUE_STATUS)}")
        result.append({"start": start, "end": item.get("end"), "value": decimal_text(item.get("value"), f"values[{index}].value"),
                       "status": status, "flags": dict(item.get("flags") or {}), "quality": item.get("quality")})
    return result


def _base(record_type, provider, native_id, title, source_url, unknowns):
    if record_type not in RECORD_TYPES:
        _fail("unknown record type")
    if provider not in PROVIDERS:
        _fail("unknown environmental provider")
    _text(native_id, "native_id", limit=500)
    _text(title, "title")
    if not isinstance(source_url, str) or not source_url.startswith("https://"):
        _fail("source_url must be the provider's https URL")
    return {"contract": CONTRACT, "record_type": record_type, "provider": provider, "native_id": native_id,
            "title": title, "source_url": source_url, "unknowns": sorted(set(unknowns or []))}


def station(provider, native_id, title, *, source_url, geometry, identifiers, network=None,
            elevation_m=None, active_from=None, active_to=None, properties=None, unknowns=None):
    record = _base("station", provider, native_id, title, source_url, unknowns)
    if not isinstance(identifiers, dict) or not identifiers:
        _fail("stations keep their provider identifiers")
    record.update(geometry=_point(geometry), crs="EPSG:4326", identifiers=dict(sorted(identifiers.items())),
                  network=network, elevation_m=elevation_m, active_from=_instant(active_from, "active_from"),
                  active_to=_instant(active_to, "active_to"), properties=dict(properties or {}))
    for key in ("network", "elevation_m", "active_to"):
        if record[key] is None:
            record["unknowns"] = sorted(set(record["unknowns"]) | {key})
    return record


def series(provider, native_id, title, *, source_url, location, indicator, unit, interval, aggregation, kind,
           values, model=None, status_basis=None, release=None, unknowns=None):
    """An observation/model/forecast series with values at one location."""

    record = _base("observation_series", provider, native_id, title, source_url, unknowns)
    if not isinstance(location, dict) or location.get("kind") not in LOCATION_KINDS:
        _fail("series location needs a kind (station, grid-cell, facility, bidding-zone)")
    _text(location.get("ref"), "location.ref", limit=500)
    if location["kind"] == "grid-cell":
        location = {**location, "geometry": _point(location.get("geometry"), "location.geometry")}
    if not isinstance(indicator, dict) or not str(indicator.get("code") or "").strip():
        _fail("series need an indicator code as published")
    _text(unit, "unit", limit=40)
    if interval is not None and not _DURATION.fullmatch(str(interval)):
        _fail("interval must be an ISO-8601 duration")
    if aggregation not in AGGREGATIONS:
        _fail(f"aggregation must be one of {', '.join(AGGREGATIONS)}")
    kind = _kind(provider, kind)
    record.update(location=dict(location), indicator=dict(indicator), unit=unit,
                  unit_pint=unit_expression(unit), interval=interval, aggregation=aggregation, kind=kind,
                  model=_model(kind, model), values=_values(values, unit),
                  status_basis=status_basis, release=dict(release or {}))
    if record["unit_pint"] is None:
        record["unknowns"] = sorted(set(record["unknowns"]) | {"unit_unmapped"})
    if interval is None:
        record["unknowns"] = sorted(set(record["unknowns"]) | {"interval"})
    if kind == "forecast" and record["model"].get("issue_time") is None:
        record["unknowns"] = sorted(set(record["unknowns"]) | {"model.issue_time"})
    if record["model"] is not None and record["model"].get("version") is None:
        record["unknowns"] = sorted(set(record["unknowns"]) | {"model.version"})
    return record


def facility(provider, native_id, title, *, source_url, geometry, operator, activities=None, permits=None,
             releases=None, identifiers=None, reporting_year=None, address=None, unknowns=None):
    record = _base("facility", provider, native_id, title, source_url, unknowns)
    if not isinstance(operator, dict) or "name" not in operator:
        _fail("facilities keep the operator as published (name may be null)")
    releases_out = []
    for index, item in enumerate(releases or []):
        if not isinstance(item, dict):
            _fail("releases are objects")
        year = item.get("year")
        if type(year) is not int:
            _fail(f"releases[{index}].year must be the reporting year")
        _text(item.get("pollutant"), f"releases[{index}].pollutant", limit=200)
        _text(item.get("unit"), f"releases[{index}].unit", limit=40)
        releases_out.append({"year": year, "pollutant": item["pollutant"], "medium": item.get("medium"),
                             "value": decimal_text(item.get("value"), f"releases[{index}].value"),
                             "unit": item["unit"], "unit_pint": unit_expression(item["unit"]),
                             "method": item.get("method"), "accidental": item.get("accidental"),
                             "kind": _kind(provider, item.get("kind"), f"releases[{index}].kind")})
    permits_out = []
    for index, item in enumerate(permits or []):
        if not isinstance(item, dict) or not (item.get("permit_id") or item.get("url")):
            _fail(f"permits[{index}] needs an identifier or URL as published")
        permits_out.append({key: item.get(key) for key in ("permit_id", "url", "issued", "authority", "text", "status")})
    record.update(geometry=None if geometry is None else _point(geometry), crs="EPSG:4326",
                  operator={"name": operator.get("name"), "identifiers": dict(operator.get("identifiers") or {})},
                  activities=[dict(a) for a in activities or []], permits=permits_out, releases=releases_out,
                  identifiers=dict(identifiers or {}), reporting_year=reporting_year, address=address)
    if geometry is None:
        record["unknowns"] = sorted(set(record["unknowns"]) | {"geometry"})
    if operator.get("name") is None:
        record["unknowns"] = sorted(set(record["unknowns"]) | {"operator.name"})
    return record


def grid_event(provider, native_id, title, *, source_url, event_type, bidding_zone, kind, resolution=None,
               production_type=None, unit, points=None, unavailability=None, document=None, unknowns=None):
    """Generation, load or unavailability as published; outages are never inferred."""

    record = _base("grid_event", provider, native_id, title, source_url, unknowns)
    if event_type not in GRID_EVENT_TYPES:
        _fail(f"event_type must be one of {', '.join(GRID_EVENT_TYPES)}")
    if not isinstance(bidding_zone, dict) or not str(bidding_zone.get("code") or "").strip():
        _fail("grid events keep their bidding zone / area code")
    kind = _kind(provider, kind)
    if resolution is not None and not _DURATION.fullmatch(str(resolution)):
        _fail("resolution must be an ISO-8601 duration")
    _text(unit, "unit", limit=40)
    out_points = _values([{k: v for k, v in p.items()} for p in points or []], unit)
    details = None
    if event_type == "unavailability":
        if not isinstance(unavailability, dict):
            _fail("unavailability events carry unit, capacity, period and reason as published")
        if unavailability.get("kind") not in UNAVAILABILITY_KINDS:
            _fail("unavailability kind is planned, unplanned or unknown")
        _instant(unavailability.get("start"), "unavailability.start", optional=False)
        _instant(unavailability.get("end"), "unavailability.end")
        details = {
            "kind": unavailability["kind"], "business_type": unavailability.get("business_type"),
            "resource": dict(unavailability.get("resource") or {}),
            "nominal_capacity": decimal_text(unavailability.get("nominal_capacity"), "nominal_capacity"),
            "start": unavailability["start"], "end": unavailability.get("end"),
            "reason": [{"code": r.get("code"), "text": r.get("text")} for r in unavailability.get("reason") or []],
            "status": unavailability.get("status"),
            "source": "published unavailability document (not inferred from news or load data)",
        }
        if not details["reason"]:
            record["unknowns"] = sorted(set(record["unknowns"]) | {"reason"})
    elif unavailability is not None:
        _fail("only unavailability events carry unavailability details")
    record.update(event_type=event_type, bidding_zone=dict(bidding_zone), kind=kind, resolution=resolution,
                  production_type=None if production_type is None else dict(production_type), unit=unit,
                  unit_pint=unit_expression(unit), points=out_points, unavailability=details,
                  document=dict(document or {}))
    return record


def indicator_vintage(provider, native_id, title, *, source_url, series_native_id, released_at, release_basis,
                      status, values_digest, unknowns=None):
    record = _base("indicator_vintage", provider, native_id, title, source_url, unknowns)
    if status not in VALUE_STATUS:
        _fail("vintage status must be provisional, validated or unknown")
    record.update(series_native_id=_text(series_native_id, "series_native_id", limit=500),
                  released_at=_instant(released_at, "released_at"), release_basis=release_basis,
                  status=status, values_digest=values_digest)
    return record


VALIDATORS = {
    "station": station, "observation_series": series, "facility": facility, "grid_event": grid_event,
    "indicator_vintage": indicator_vintage,
}


def validate(record):
    """Re-validate a stored or received record by rebuilding it through its constructor."""

    if not isinstance(record, dict) or record.get("contract") != CONTRACT:
        _fail("not a noesis-environment-record-v1 record")
    kind = record.get("record_type")
    fields = {k: v for k, v in record.items() if k not in {"contract", "record_type", "provider", "native_id", "title",
                                                          "source_url", "crs", "unit_pint"}}
    common = (record.get("provider"), record.get("native_id"), record.get("title"))
    if kind == "station":
        rebuilt = station(*common, source_url=record["source_url"], geometry=fields["geometry"],
                          identifiers=fields["identifiers"], network=fields.get("network"),
                          elevation_m=fields.get("elevation_m"), active_from=fields.get("active_from"),
                          active_to=fields.get("active_to"), properties=fields.get("properties"),
                          unknowns=fields.get("unknowns"))
    elif kind == "observation_series":
        rebuilt = series(*common, source_url=record["source_url"], location=fields.get("location"),
                         indicator=fields.get("indicator"), unit=fields.get("unit"), interval=fields.get("interval"),
                         aggregation=fields.get("aggregation"), kind=fields.get("kind"), values=fields.get("values"),
                         model=fields.get("model"), status_basis=fields.get("status_basis"),
                         release=fields.get("release"), unknowns=fields.get("unknowns"))
    elif kind == "facility":
        rebuilt = facility(*common, source_url=record["source_url"], geometry=fields.get("geometry"),
                           operator=fields.get("operator"), activities=fields.get("activities"),
                           permits=fields.get("permits"), releases=fields.get("releases"),
                           identifiers=fields.get("identifiers"), reporting_year=fields.get("reporting_year"),
                           address=fields.get("address"), unknowns=fields.get("unknowns"))
    elif kind == "grid_event":
        rebuilt = grid_event(*common, source_url=record["source_url"], event_type=fields.get("event_type"),
                             bidding_zone=fields.get("bidding_zone"), kind=fields.get("kind"),
                             resolution=fields.get("resolution"), production_type=fields.get("production_type"),
                             unit=fields.get("unit"), points=fields.get("points"),
                             unavailability=fields.get("unavailability"), document=fields.get("document"),
                             unknowns=fields.get("unknowns"))
    elif kind == "indicator_vintage":
        rebuilt = indicator_vintage(*common, source_url=record["source_url"],
                                    series_native_id=fields.get("series_native_id"),
                                    released_at=fields.get("released_at"), release_basis=fields.get("release_basis"),
                                    status=fields.get("status"), values_digest=fields.get("values_digest"),
                                    unknowns=fields.get("unknowns"))
    else:
        _fail("unknown record type")
    return rebuilt


# ------------------------------------------------------------ schema registry

SCHEMA_FILES = {
    "noesis-environment-record": "contracts/schemas/jsonschema/noesis-environment-record-v1.json",
    "noesis-environment-dossier": "contracts/schemas/jsonschema/noesis-environment-dossier-v1.json",
}


def schema_definitions(root=None):
    from pathlib import Path

    root = Path(root) if root else Path(__file__).resolve().parents[2]
    return {name: json.loads((root / path).read_text()) for name, path in SCHEMA_FILES.items()}


def register_schemas(conn, *, principal_id, scopes, root=None):
    """Register the pack's schemas in the existing schema registry (idempotent per version)."""

    from src.kb.schema_registry import SchemaRegistry

    registry = SchemaRegistry(conn)
    results = []
    for name, content in sorted(schema_definitions(root).items()):
        definition = {
            "contract": "noesis-schema-module-v1", "name": name, "kind": "schema", "semantic_version": "1.0.0",
            "content": content, "owner": "environment.core", "dependencies": [], "compatibility_policy": "backward",
            "provenance": {"kind": "imported", "source": "packs/climate-environment"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(registry.register(definition, f"environment-schema:{name}:1.0.0:{digest(content)[:16]}",
                                         principal_id=principal_id, scopes=scopes))
    return results
