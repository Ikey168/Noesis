"""Operational weather records: location vintages, observation reports, forecast issuances, warnings (WX02, #2165).

``noesis-weather-record-v1`` is the provider-neutral shape every Weather adapter
emits. It **extends** the Climate & Environment records and never duplicates
them:

* **Stations stay the environment owner's.** Every record names its station
  by ``station`` = ``{"provider", "native_id"}``, the key of an
  ``environment_records`` ``station`` record (``src/kb/environment_records.py``).
  There is no weather station table. A station the environment owner does not
  know yet is registered through that owner (:func:`environment_station`), never
  here.
* **Every record is published by someone else.** ``provider`` is one of the
  external publishers in :data:`PROVIDERS` and ``issuer`` names the
  organisation. No record type can hold a Noesis-produced forecast: an issuer
  or producer naming Noesis is rejected, and so is any blending, bias-correction
  or re-forecast marker (:data:`FORBIDDEN_FIELDS`).
* **Three clocks stay apart.** ``reference_time`` is the source's own time
  (observation time, issue time, CAP ``sent``, ``valid_from``), each forecast
  element has its ``valid_time`` and lead time, and the store records the
  acquisition time per revision. All times are UTC ISO strings
  (``YYYY-MM-DDTHH:MM:SSZ``) or dates (``YYYY-MM-DD``) and are compared as
  times, never as text.
* **Missing is absent.** A published missing value (``-999``, ``-``, ``null``)
  leaves the ``value`` key out and records ``missing`` with the source's marker.
  Nothing is defaulted, and the string ``"None"`` is never written. QC flags are
  kept verbatim with their scheme. The common vocabulary is added by
  :mod:`src.kb.weather_normalise`.

Records carry ``source``/``provider``, ``source_record_id``, ``reference_time``,
``locator`` (URL plus pointer) and ``attribution``. The store adds the revision,
``retrieved_at`` and the change kind.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

CONTRACT = "noesis-weather-record-v1"
READ_SCOPE = "knowledge:weather:read"
WRITE_SCOPE = "knowledge:weather:write"
REVIEW_SCOPE = "knowledge:weather:review"
RECORD_TYPES = (
    "station_location_vintage",
    "observation_report",
    "forecast_issuance",
    "warning",
)
PAIR_TYPE = "verification_pair"
# External publishers. Each record's provider is one of these; Noesis is never a provider.
PROVIDERS = ("dwd-cdc", "dwd-mosmix", "dwd-cap", "aviationweather", "nws", "open-meteo")
ISSUERS = {
    "dwd-cdc": "Deutscher Wetterdienst",
    "dwd-mosmix": "Deutscher Wetterdienst",
    "dwd-cap": "Deutscher Wetterdienst",
    "aviationweather": "NOAA / National Weather Service, Aviation Weather Center",
    "nws": "NOAA / National Weather Service",
    "open-meteo": "Open-Meteo",
}
ATTRIBUTION = {
    "dwd-cdc": "Quelle: Deutscher Wetterdienst (CC BY 4.0)",
    "dwd-mosmix": "Quelle: Deutscher Wetterdienst (CC BY 4.0)",
    "dwd-cap": "Quelle: Deutscher Wetterdienst",
    "aviationweather": "NOAA / National Weather Service, Aviation Weather Center (public domain)",
    "nws": "NOAA / National Weather Service (public domain)",
    "open-meteo": "Open-Meteo (CC BY 4.0)",
}
# Environment-owner station providers (``environment_records.station``) a weather record may name.
STATION_PROVIDERS = ("dwd", "dwd-mosmix", "aviationweather")
REPORT_TYPES = ("dwd-10min", "dwd-hourly", "metar", "speci")
LOCATION_KINDS = ("station", "grid-cell", "grid-point")
PRODUCTS = ("MOSMIX_L", "nws-gridpoint-hourly", "TAF", "open-meteo-forecast")
ELEMENT_KINDS = ("value", "probability")
MSG_TYPES = ("Alert", "Update", "Cancel", "Ack", "Error")
CODE_SCHEMES = ("WARNCELLID", "UGC", "SAME", "EXCLUDE_POLYGON")
MATCH_RULES = ("same-station", "declared-grid-point", "equivalent-station")
# Anything that would let a record hold a Noesis-made forecast or a post-processed one.
FORBIDDEN_FIELDS = frozenset(
    {
        "bias_corrected",
        "blended",
        "ensemble_blend",
        "downscaled",
        "nowcast",
        "reforecast",
        "calibrated_by",
        "producer",
    }
)
_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Fields that belong to one acquisition, not to what the source states: excluded from the content hash.
VOLATILE = frozenset({"locator", "release", "source_time", "precedence"})


class WeatherRecordError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _fail(message: str, code: str = "invalid_weather_record") -> None:
    raise WeatherRecordError(code, message)


# ------------------------------------------------------------------ time


def utc(value: Any) -> str:
    """A source time as UTC ``YYYY-MM-DDTHH:MM:SSZ`` (ISO with offset, ``Z``, or epoch seconds)."""

    if isinstance(value, bool):
        _fail("time must be an instant")
    if isinstance(value, (int, float)):
        moment = datetime.fromtimestamp(value, tz=UTC)
    else:
        text = str(value or "").strip()
        if not text:
            _fail("time is required")
        if re.fullmatch(r"\d{9,11}", text):
            moment = datetime.fromtimestamp(int(text), tz=UTC)
        else:
            try:
                moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                _fail(f"unparseable time {text!r}")
            if moment.tzinfo is None:
                _fail(
                    f"time {text!r} has no offset; a source time is never assumed to be UTC"
                )
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ms(value: Any) -> int | None:
    """UTC ISO instant or date (UTC midnight) -> epoch milliseconds."""

    if value is None:
        return None
    text = str(value)
    if _DATE.fullmatch(text):
        text += "T00:00:00Z"
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def iso(value_ms: int) -> str:
    return datetime.fromtimestamp(int(value_ms) / 1000, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _instant(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _INSTANT.fullmatch(value):
        _fail(f"{field} must be a UTC instant YYYY-MM-DDTHH:MM:SSZ")
    return value


def _date(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        _fail(f"{field} must be a date YYYY-MM-DD")
    return value


def _text(
    value: Any, field: str, *, optional: bool = False, limit: int = 4000
) -> str | None:
    if value is None and optional:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > limit
        or value == "None"
    ):
        _fail(f"{field} must be nonempty text")
    return value


def decimal_text(value: Any, field: str = "value") -> str:
    """Exact decimal text as published; floats and non-finite numbers are rejected."""

    if not isinstance(value, str):
        _fail(f"{field} must be decimal text, not {type(value).__name__}")
    try:
        number = Decimal(value)
    except InvalidOperation:
        _fail(f"{field} is not a decimal number")
    if not number.is_finite():
        _fail(f"{field} must be finite")
    return value


# --------------------------------------------------------------- pieces


def station_ref(provider: str, native_id: str) -> dict[str, str]:
    if provider not in STATION_PROVIDERS:
        _fail(f"station provider must be one of {', '.join(STATION_PROVIDERS)}")
    _text(native_id, "station.native_id", limit=100)
    return {"provider": provider, "native_id": native_id}


def station_key(ref: dict[str, str]) -> str:
    return f"{ref['provider']}:{ref['native_id']}"


def _station(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"provider", "native_id"}:
        _fail("station is {provider, native_id} of an environment station record")
    return station_ref(value["provider"], value["native_id"])


def _locator(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not str(value.get("url") or "").startswith(
        "https://"
    ):
        _fail("locator needs the provider's https URL")
    return {
        k: value[k]
        for k in ("url", "pointer", "file", "line")
        if value.get(k) is not None
    }


def _qc(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not str(value.get("scheme") or "").strip():
        _fail(f"{field} needs a QC scheme (and the native flag when published)")
    out = {"scheme": value["scheme"]}
    if value.get("native") is not None:
        out["native"] = _text(str(value["native"]), f"{field}.native", limit=40)
    return out


def _point(geometry: Any, field: str) -> dict[str, Any]:
    if not isinstance(geometry, dict) or geometry.get("type") != "Point":
        _fail(f"{field} must be a GeoJSON Point")
    coords = geometry.get("coordinates")
    if (
        not isinstance(coords, list)
        or len(coords) != 2
        or any(type(v) not in (int, float) for v in coords)
        or not -180 <= coords[0] <= 180
        or not -90 <= coords[1] <= 90
    ):
        _fail(f"{field} needs [longitude, latitude] in EPSG:4326")
    return {"type": "Point", "coordinates": [float(coords[0]), float(coords[1])]}


def _ring(ring: Any, field: str) -> list[list[float]]:
    if not isinstance(ring, list) or len(ring) < 4:
        _fail(f"{field} must be a closed ring of at least four positions")
    out = []
    for position in ring:
        if (
            not isinstance(position, list)
            or len(position) != 2
            or any(type(v) not in (int, float) for v in position)
            or not -180 <= position[0] <= 180
            or not -90 <= position[1] <= 90
        ):
            _fail(f"{field} positions are [longitude, latitude]")
        out.append([float(position[0]), float(position[1])])
    if out[0] != out[-1]:
        _fail(f"{field} must be closed")
    return out


def _geometry(geometry: Any, field: str) -> dict[str, Any]:
    if isinstance(geometry, dict) and geometry.get("type") == "Polygon":
        rings = geometry.get("coordinates")
        if not isinstance(rings, list) or not rings:
            _fail(f"{field} polygon needs rings")
        return {"type": "Polygon", "coordinates": [_ring(r, field) for r in rings]}
    return _point(geometry, field)


def _guard(extra: dict[str, Any]) -> None:
    bad = FORBIDDEN_FIELDS & set(extra)
    if bad:
        _fail(
            f"weather records hold published data only; {sorted(bad)} is not allowed",
            "not_published_data",
        )


def _base(
    record_type: str,
    provider: str,
    source_record_id: str,
    reference_time: str,
    *,
    locator: Any,
    record_key: str,
    unknowns: Any = None,
    release: Any = None,
    source_time: Any = None,
    precedence: int = 0,
) -> dict[str, Any]:
    if record_type not in RECORD_TYPES:
        _fail("unknown weather record type")
    if provider not in PROVIDERS:
        _fail(f"provider must be one of {', '.join(PROVIDERS)}")
    if type(precedence) is not int or not 0 <= precedence <= 9:
        _fail("precedence is a small non-negative integer tier")
    record = {
        "contract": CONTRACT,
        "record_type": record_type,
        "provider": provider,
        "issuer": ISSUERS[provider],
        "attribution": ATTRIBUTION[provider],
        "source_record_id": _text(source_record_id, "source_record_id", limit=500),
        "record_key": _text(record_key, "record_key", limit=800),
        "reference_time": reference_time,
        "locator": _locator(locator),
        "precedence": precedence,
        "unknowns": sorted(set(unknowns or [])),
    }
    if source_time is not None:
        record["source_time"] = _instant(source_time, "source_time")
    if release:
        if not isinstance(release, dict):
            _fail("release is an object describing the published file or vintage")
        record["release"] = {k: v for k, v in sorted(release.items()) if v is not None}
    return record


# -------------------------------------------------------------- records


def location_vintage(
    provider: str,
    station: dict[str, str],
    *,
    latitude: str,
    longitude: str,
    valid_from: str,
    valid_to: str | None = None,
    elevation_m: str | None = None,
    name: str | None = None,
    locator: dict[str, Any],
    release: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One published location of a station with its validity as published (``valid_to`` absent = open)."""

    station = _station(station)
    _date(valid_from, "valid_from")
    _date(valid_to, "valid_to", optional=True)
    if valid_to is not None and valid_to < valid_from:
        _fail("valid_to precedes valid_from")
    lat, lon = decimal_text(latitude, "latitude"), decimal_text(longitude, "longitude")
    if not -90 <= Decimal(lat) <= 90 or not -180 <= Decimal(lon) <= 180:
        _fail("coordinates are out of range")
    unknowns = []
    record = _base(
        "station_location_vintage",
        provider,
        f"{station_key(station)}@{valid_from}",
        valid_from,
        locator=locator,
        record_key=f"{station_key(station)}|{valid_from}",
        release=release,
        unknowns=unknowns,
    )
    record.update(
        station=station,
        latitude=lat,
        longitude=lon,
        valid_from=valid_from,
        crs="EPSG:4326",
    )
    if valid_to is not None:
        record["valid_to"] = valid_to
    else:
        record["open_ended"] = True
    if elevation_m is not None:
        record["elevation_m"] = decimal_text(elevation_m, "elevation_m")
    else:
        record["unknowns"] = sorted({*record["unknowns"], "elevation_m"})
    if name is not None:
        record["name"] = _text(name, "name", limit=200)
    return record


def parameter(
    name: str,
    *,
    unit: str,
    qc: dict[str, Any],
    value: str | None = None,
    missing: str | None = None,
) -> dict[str, Any]:
    """One published parameter value; a missing value is absent and states the source's marker."""

    item = {
        "parameter": _text(name, "parameter", limit=80),
        "unit": _text(unit, "unit", limit=40),
        "qc": _qc(qc, f"{name}.qc"),
    }
    if value is None:
        item["missing"] = _text(missing or "not published", f"{name}.missing", limit=80)
    else:
        item["value"] = decimal_text(value, f"{name}.value")
    return item


def observation_report(
    provider: str,
    station: dict[str, str],
    observed_at: str,
    report_type: str,
    *,
    parameters: list[dict[str, Any]],
    locator: dict[str, Any],
    correction: str | None = None,
    raw_text: str | None = None,
    precedence: int = 0,
    source_time: str | None = None,
    release: dict[str, Any] | None = None,
    product_group: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One published report; ``product_group`` (e.g. a DWD parameter group) is part of its identity."""

    _guard(extra)
    if extra:
        _fail(f"unsupported observation fields {sorted(extra)}")
    station = _station(station)
    _instant(observed_at, "observed_at")
    if report_type not in REPORT_TYPES:
        _fail(f"report_type must be one of {', '.join(REPORT_TYPES)}")
    if not isinstance(parameters, list) or not parameters or len(parameters) > 60:
        _fail("an observation report carries 1-60 parameters")
    params = []
    for item in parameters:
        if not isinstance(item, dict):
            _fail("parameters are objects")
        params.append(
            parameter(
                item.get("parameter"),
                unit=item.get("unit"),
                qc=item.get("qc"),
                value=item.get("value"),
                missing=item.get("missing"),
            )
        )
    names = [p["parameter"] for p in params]
    if len(set(names)) != len(names):
        _fail("a parameter appears twice in one report")
    group = _text(product_group, "product_group", optional=True, limit=80)
    key = "|".join(
        [station_key(station), report_type, *([group] if group else []), observed_at]
    )
    record = _base(
        "observation_report",
        provider,
        key,
        observed_at,
        locator=locator,
        record_key=key,
        release=release,
        source_time=source_time,
        precedence=precedence,
    )
    record.update(
        station=station,
        observed_at=observed_at,
        report_type=report_type,
        parameters=sorted(params, key=lambda p: p["parameter"]),
    )
    if group:
        record["product_group"] = group
    if correction is not None:
        record["correction"] = _text(correction, "correction", limit=20)
    if raw_text is not None:
        record["raw_text"] = _text(raw_text, "raw_text", limit=2000)
    if report_type in {"metar", "speci"} and raw_text is None:
        _fail("METAR/SPECI reports keep their raw text verbatim")
    return record


def forecast_element(
    parameter_name: str,
    valid_time: str,
    *,
    issued_at: str,
    unit: str,
    value: str | None = None,
    kind: str = "value",
    valid_to: str | None = None,
    missing: str | None = None,
) -> dict[str, Any]:
    """One published forecast value; lead time = valid time - issue time (seconds)."""

    _instant(valid_time, "valid_time")
    _instant(valid_to, "valid_to", optional=True)
    if kind not in ELEMENT_KINDS:
        _fail(f"element kind must be one of {', '.join(ELEMENT_KINDS)}")
    lead = (ms(valid_time) - ms(issued_at)) // 1000
    element = {
        "parameter": _text(parameter_name, "parameter", limit=80),
        "valid_time": valid_time,
        "lead_time_s": lead,
        "unit": _text(unit, "unit", limit=40),
        "kind": kind,
    }
    if valid_to is not None:
        element["valid_to"] = valid_to
    if value is None:
        element["missing"] = _text(missing or "not published", "missing", limit=80)
    else:
        element["value"] = decimal_text(value, f"{parameter_name}.value")
        if kind == "probability" and not 0 <= Decimal(value) <= 100:
            _fail("probabilities are published percentages")
    return element


def forecast_issuance(
    provider: str,
    product: str,
    location: dict[str, Any],
    issued_at: str,
    *,
    run_id: str,
    elements: list[dict[str, Any]],
    locator: dict[str, Any],
    model: str | None = None,
    originator: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    raw_text: str | None = None,
    generated_at: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One published run (issuance vintage). A later run is a new issuance, never an overwrite."""

    _guard(extra)
    if extra:
        _fail(f"unsupported issuance fields {sorted(extra)}")
    if product not in PRODUCTS:
        _fail(f"product must be one of {', '.join(PRODUCTS)}")
    _instant(issued_at, "issued_at")
    if not isinstance(location, dict) or location.get("kind") not in LOCATION_KINDS:
        _fail("location needs a kind (station, grid-cell, grid-point)")
    loc = {
        "kind": location["kind"],
        "ref": _text(location.get("ref"), "location.ref", limit=300),
    }
    if location.get("geometry") is not None:
        loc["geometry"] = _geometry(location["geometry"], "location.geometry")
    if location["kind"] == "station":
        loc["station"] = _station(location.get("station"))
    elif location.get("declared_station") is not None:
        loc["declared_station"] = _station(location["declared_station"])
        loc["declared_basis"] = _text(
            location.get("declared_basis") or "source selection", "declared_basis"
        )
    if location["kind"] != "station" and "geometry" not in loc:
        _fail("a grid location carries its published geometry")
    if not isinstance(elements, list) or len(elements) > 20_000:
        _fail("elements must be a bounded list")
    out, seen = [], set()
    for item in elements:
        element = forecast_element(
            item.get("parameter"),
            item.get("valid_time"),
            issued_at=issued_at,
            unit=item.get("unit"),
            value=item.get("value"),
            kind=item.get("kind", "value"),
            valid_to=item.get("valid_to"),
            missing=item.get("missing"),
        )
        key = (element["parameter"], element["valid_time"])
        if key in seen:
            _fail(f"element {key} appears twice in one issuance")
        seen.add(key)
        out.append(element)
    key = "|".join([provider, product, model or "-", loc["ref"], issued_at])
    record = _base(
        "forecast_issuance",
        provider,
        _text(run_id, "run_id", limit=300),
        issued_at,
        locator=locator,
        record_key=key,
    )
    record.update(
        product=product,
        location=loc,
        issued_at=issued_at,
        run_id=run_id,
        elements=sorted(out, key=lambda e: (e["parameter"], e["valid_time"])),
        kind="forecast",
        notice="published forecast as issued; not an observation and not made by Noesis",
    )
    if model is not None:
        record["model"] = _text(model, "model", limit=80)
    if originator is not None:
        record["originator"] = _text(originator, "originator", limit=200)
    for field, value in (
        ("valid_from", valid_from),
        ("valid_to", valid_to),
        ("generated_at", generated_at),
    ):
        if value is not None:
            record[field] = _instant(value, field)
    if raw_text is not None:
        record["raw_text"] = _text(raw_text, "raw_text", limit=8000)
    if not out and raw_text is None:
        _fail("an issuance carries elements or its raw published text")
    return record


def _reference(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        _fail("references are {sender, identifier, sent}")
    ref = {
        "sender": _text(value.get("sender"), "reference.sender", limit=300),
        "identifier": _text(value.get("identifier"), "reference.identifier", limit=300),
    }
    if value.get("sent") is not None:
        ref["sent"] = _instant(value["sent"], "reference.sent")
    return ref


def parse_cap_references(text: str | None) -> list[dict[str, str]]:
    """CAP ``references``: whitespace-separated ``sender,identifier,sent`` triples."""

    result = []
    for triple in (text or "").split():
        parts = triple.split(",")
        if len(parts) != 3:
            _fail(
                f"CAP reference {triple!r} is not sender,identifier,sent",
                "schema_drift",
            )
        result.append(
            {"sender": parts[0], "identifier": parts[1], "sent": utc(parts[2])}
        )
    return result


def _area(value: Any, index: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"areas[{index}] is an object")
    codes = []
    for code in value.get("codes") or []:
        if not isinstance(code, dict) or code.get("scheme") not in CODE_SCHEMES:
            _fail(f"areas[{index}] codes need a scheme in {', '.join(CODE_SCHEMES)}")
        codes.append(
            {
                "scheme": code["scheme"],
                "value": _text(str(code.get("value") or ""), "code.value", limit=40),
            }
        )
    polygons = [
        _ring(ring, f"areas[{index}].polygon") for ring in value.get("polygons") or []
    ]
    if not codes and not polygons:
        _fail(f"areas[{index}] needs area codes or polygons as published")
    area = {
        "codes": sorted(codes, key=lambda c: (c["scheme"], c["value"])),
        "polygons": polygons,
    }
    if value.get("description") is not None:
        area["description"] = _text(
            value["description"], "area.description", limit=2000
        )
    return area


def warning(
    provider: str,
    identifier: str,
    sender: str,
    sent: str,
    msg_type: str,
    *,
    locator: dict[str, Any],
    event: str | None,
    severity: str | None,
    urgency: str | None,
    certainty: str | None,
    areas: list[dict[str, Any]],
    references: list[dict[str, Any]] | None = None,
    status: str | None = None,
    scope: str | None = None,
    onset: str | None = None,
    effective: str | None = None,
    expires: str | None = None,
    headline: str | None = None,
    description: str | None = None,
    instruction: str | None = None,
    language: str | None = None,
    event_code: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One CAP message as issued; Update/Cancel thread to the messages they reference at read time."""

    _guard(extra)
    if extra:
        _fail(f"unsupported warning fields {sorted(extra)}")
    _text(identifier, "identifier", limit=300)
    _text(sender, "sender", limit=300)
    _instant(sent, "sent")
    if msg_type not in MSG_TYPES:
        _fail(f"msgType must be one of {', '.join(MSG_TYPES)}")
    refs = [_reference(r) for r in references or []]
    if msg_type in {"Update", "Cancel"} and not refs:
        _fail("an Update or Cancel names the messages it references", "schema_drift")
    if not isinstance(areas, list) or (msg_type != "Cancel" and not areas):
        _fail("a warning names its areas as published")
    record = _base(
        "warning",
        provider,
        identifier,
        sent,
        locator=locator,
        record_key=f"{sender}|{identifier}",
    )
    record.update(
        identifier=identifier,
        sender=sender,
        sent=sent,
        msg_type=msg_type,
        references=sorted(
            refs, key=lambda r: (r.get("sent") or "", r["sender"], r["identifier"])
        ),
        areas=[_area(a, i) for i, a in enumerate(areas)],
        quoted_as_issued=True,
        notice="the issuer's message quoted as issued; Noesis adds no advice",
    )
    unknowns = []
    for field, value in (
        ("event", event),
        ("severity", severity),
        ("urgency", urgency),
        ("certainty", certainty),
        ("status", status),
        ("scope", scope),
        ("headline", headline),
        ("description", description),
        ("instruction", instruction),
        ("language", language),
        ("event_code", event_code),
    ):
        if value is None or (isinstance(value, str) and not value.strip()):
            if field in {"event", "severity", "urgency", "certainty"}:
                unknowns.append(field)
            continue
        record[field] = _text(value, field, limit=8000)
    for field, value in (
        ("onset", onset),
        ("effective", effective),
        ("expires", expires),
    ):
        if value is None:
            if field != "effective":
                unknowns.append(field)
            continue
        record[field] = _instant(value, field)
    record["unknowns"] = sorted(set(unknowns))
    return record


def verification_pair(
    *,
    element: dict[str, Any],
    observation: dict[str, Any],
    match_rule: str,
    tolerance_s: int,
    parameter_name: str,
    unit: str,
    forecast_value: str,
    observed_value: str,
    kind: str = "value",
) -> dict[str, Any]:
    """A published forecast element matched to a recorded observation under a stated rule (no new forecast)."""

    if match_rule not in MATCH_RULES:
        _fail(f"match_rule must be one of {', '.join(MATCH_RULES)}")
    for side, fields in (
        ("element", {"revision_id", "parameter", "valid_time"}),
        ("observation", {"revision_id", "observed_at"}),
    ):
        value = element if side == "element" else observation
        if not isinstance(value, dict) or fields - set(value):
            _fail(f"{side} reference needs {sorted(fields)}")
    if type(tolerance_s) is not int or tolerance_s < 0:
        _fail("tolerance_s is a non-negative integer")
    return {
        "contract": CONTRACT,
        "record_type": PAIR_TYPE,
        "element": dict(element),
        "observation": dict(observation),
        "match_rule": match_rule,
        "tolerance_s": tolerance_s,
        "parameter": parameter_name,
        "unit": unit,
        "forecast_value": decimal_text(forecast_value, "forecast_value"),
        "observed_value": decimal_text(observed_value, "observed_value"),
        "kind": kind,
    }


def environment_station(
    provider: str,
    native_id: str,
    title: str,
    *,
    source_url: str,
    longitude: float,
    latitude: float,
    identifiers: dict[str, str],
    elevation_m: float | None = None,
    network: str | None = None,
) -> dict[str, Any]:
    """An environment ``station`` record, built through the environment owner's own constructor.

    Weather adapters emit these for stations the environment owner may not know
    yet; :class:`src.kb.weather_store.WeatherStore` registers them through
    :class:`src.kb.environment_store.EnvironmentStore` only when the owner has no
    such station. This module never stores a station itself.
    """

    from src.kb import environment_records as er

    return er.station(
        provider,
        native_id,
        title,
        source_url=source_url,
        geometry={"type": "Point", "coordinates": [longitude, latitude]},
        identifiers=identifiers,
        network=network,
        elevation_m=elevation_m,
    )


def content(record: dict[str, Any]) -> dict[str, Any]:
    """The normalised, source-independent content a revision is compared on."""

    return {k: v for k, v in record.items() if k not in VOLATILE}


def content_hash(record: dict[str, Any]) -> str:
    return digest(content(record))


VALIDATORS = {
    "station_location_vintage": location_vintage,
    "observation_report": observation_report,
    "forecast_issuance": forecast_issuance,
    "warning": warning,
}


def validate(record: Any) -> dict[str, Any]:
    """Rebuild a record through its constructor (fail closed); returns the normalised record."""

    if not isinstance(record, dict) or record.get("contract") != CONTRACT:
        _fail("not a noesis-weather-record-v1 record")
    _guard(record)
    kind = record.get("record_type")
    common = {"locator": record.get("locator")}
    if kind == "station_location_vintage":
        rebuilt = location_vintage(
            record["provider"],
            record.get("station"),
            latitude=record.get("latitude"),
            longitude=record.get("longitude"),
            valid_from=record.get("valid_from"),
            valid_to=record.get("valid_to"),
            elevation_m=record.get("elevation_m"),
            name=record.get("name"),
            release=record.get("release"),
            **common,
        )
    elif kind == "observation_report":
        rebuilt = observation_report(
            record["provider"],
            record.get("station"),
            record.get("observed_at"),
            record.get("report_type"),
            parameters=record.get("parameters"),
            correction=record.get("correction"),
            product_group=record.get("product_group"),
            raw_text=record.get("raw_text"),
            precedence=record.get("precedence", 0),
            source_time=record.get("source_time"),
            release=record.get("release"),
            **common,
        )
    elif kind == "forecast_issuance":
        rebuilt = forecast_issuance(
            record["provider"],
            record.get("product"),
            record.get("location"),
            record.get("issued_at"),
            run_id=record.get("run_id"),
            elements=record.get("elements"),
            model=record.get("model"),
            originator=record.get("originator"),
            valid_from=record.get("valid_from"),
            valid_to=record.get("valid_to"),
            raw_text=record.get("raw_text"),
            generated_at=record.get("generated_at"),
            **common,
        )
    elif kind == "warning":
        rebuilt = warning(
            record["provider"],
            record.get("identifier"),
            record.get("sender"),
            record.get("sent"),
            record.get("msg_type"),
            event=record.get("event"),
            severity=record.get("severity"),
            urgency=record.get("urgency"),
            certainty=record.get("certainty"),
            areas=record.get("areas"),
            references=record.get("references"),
            status=record.get("status"),
            scope=record.get("scope"),
            onset=record.get("onset"),
            effective=record.get("effective"),
            expires=record.get("expires"),
            headline=record.get("headline"),
            description=record.get("description"),
            instruction=record.get("instruction"),
            language=record.get("language"),
            event_code=record.get("event_code"),
            **common,
        )
    else:
        _fail("unknown weather record type")
    if record.get("issuer") not in (None, rebuilt["issuer"]):
        _fail(
            "issuer is the provider's publisher; a record never names Noesis as issuer",
            "not_published_data",
        )
    return rebuilt


# ------------------------------------------------------------ schema registry

SCHEMA_PATH = "contracts/schemas/jsonschema/noesis-weather-record-v1.json"


def schema_definition(root: Any = None) -> dict[str, Any]:
    from pathlib import Path

    base = Path(root) if root else Path(__file__).resolve().parents[2]
    return json.loads((base / SCHEMA_PATH).read_text())


def register_schemas(
    conn: Any, *, principal_id: str, scopes: Any, root: Any = None
) -> list[dict[str, Any]]:
    """Register ``noesis-weather-record-v1`` in the shared schema registry (idempotent per version)."""

    from src.kb.schema_registry import SchemaRegistry

    content_ = schema_definition(root)
    definition = {
        "contract": "noesis-schema-module-v1",
        "name": "weather-record",
        "kind": "schema",
        "semantic_version": "1.0.0",
        "content": content_,
        "owner": "weather.observations",
        "dependencies": [],
        "compatibility_policy": "backward",
        "provenance": {"kind": "imported", "source": SCHEMA_PATH},
        "actor": {"principal_id": principal_id, "kind": "service"},
    }
    return [
        SchemaRegistry(conn).register(
            definition,
            f"weather-schema:weather-record:1.0.0:{digest(content_)[:16]}",
            principal_id=principal_id,
            scopes=scopes,
        )
    ]
