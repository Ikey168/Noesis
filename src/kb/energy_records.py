"""Energy Systems record model: generation, load, price, capacity, cross-border flow, energy balance (EN02).

``noesis-energy-record-v1`` is the provider-neutral shape every Energy
Systems adapter emits. One record is **one published series as one release
states it**:

* **Series identity** is ``provider + dataset + native_id``; the release is
  not part of it. A later release of the same series (an ENTSO-E document
  revision, an EIA re-publication, a new Ember or Eurostat dataset release)
  is a new *vintage* of that series, stored beside the earlier ones by
  :class:`src.kb.energy_store.EnergyStore` and never overwriting them.
* **Every observation carries** its unit, interval (resolution), reference
  period, source, release vintage (``release``), publication status
  (``provisional`` / ``revised`` / ``final`` / ``unknown``) and the
  retrieval time (``retrieved_at``, the as-of clock).
* **Observations only.** Forecasts (ENTSO-E day-ahead load forecasts, EIA
  demand forecasts) are never energy records; ``kind`` is fixed to
  ``observation`` and forecast codes are rejected (:data:`FORECAST_CODES`).
* **Values are published text.** Decimal strings or ``None``; floats are
  rejected; flags (Eurostat ``OBS_FLAG``, ENTSO-E positions, EIA units) are
  kept verbatim. Nothing is converted beyond the published unit, gap-filled,
  summed across sources or estimated (emissions included).
* **Capacity** may be zone-level or plant/unit-level; plant/unit records carry
  effective dates (``capacity.effective_from`` / ``effective_to``).
* **Prices** keep their currency unit as published (negative prices included)
  and are written through Market time-series storage by
  :mod:`src.kb.energy_market`; the energy record keeps the reference.
* **Re-published data** (Energy-Charts re-publishing ENTSO-E) carries
  ``derived_from`` and stays a separate provider's record; it is never
  reconciled with the original.

Climate and Environment keeps its own ENTSO-E ``grid_event`` records
(``src/kb/environment_records.py``); :func:`from_grid_event` builds the
energy record from one without a second acquisition path.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

CONTRACT = "noesis-energy-record-v1"
SCHEMA_VERSION = "1.0.0"
READ_SCOPE = "knowledge:energy:read"
WRITE_SCOPE = "knowledge:energy:write"
REVIEW_SCOPE = "knowledge:energy:review"
RECORD_TYPES = ("generation", "load", "price", "capacity", "cross_border_flow", "energy_balance")
STATUSES = ("provisional", "revised", "final", "unknown")
PROVIDERS = ("entsoe", "eia", "ember", "eurostat", "energy-charts")
SUBJECT_KINDS = ("bidding-zone", "control-area", "country", "balancing-area", "plant", "unit", "region")
SUBJECT_SCHEMES = ("eic", "iso3166-alpha2", "iso3166-alpha3", "eurostat-geo", "eia-ba", "eia-plant",
                   "eia-generator", "entsoe-unit", "energy-charts-country", "ember-entity")
RELEASE_BASES = ("document_created", "provider_last_update", "declared_release", "retrieval_time")
# Codes that denote forecasts; a record carrying one is refused (observations only).
FORECAST_CODES = frozenset({"entsoe:A01", "eia:DF", "entsoe:load-forecast"})
# Keys that would carry a derived judgement, estimate or cross-source blend.
FORBIDDEN_KEYS = frozenset({"forecast", "estimate_by_noesis", "emissions_estimate", "blended", "reconciled",
                            "combined_total", "trading_signal", "recommendation"})
_INSTANT = re.compile(r"^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:\d{2}))?)?)?$")
_DURATION = re.compile(r"^P(\d+Y)?(\d+M)?(\d+W)?(\d+D)?(T(\d+H)?(\d+M)?(\d+S)?)?$")
UNITS = ("MW", "MWh", "GWh", "TWh", "GW", "kW", "EUR/MWh", "USD/MWh", "megawatthours", "megawatts", "KTOE", "TJ",
         "GWH", "THS_T", "%", "MW (net summer capacity)")


class EnergyRecordError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _fail(message, code="invalid_energy_record"):
    raise EnergyRecordError(code, message)


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
        _fail(f"{field} must be an ISO-8601 year, month, date or instant with offset")
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


def forbidden_keys(value, path="$"):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_KEYS:
                found.append(f"{path}.{key}")
            found += forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}[{index}]")
    return found


def _subject(value, field):
    if not isinstance(value, dict) or value.get("kind") not in SUBJECT_KINDS:
        _fail(f"{field}.kind must be one of {', '.join(SUBJECT_KINDS)}")
    if value.get("scheme") not in SUBJECT_SCHEMES:
        _fail(f"{field}.scheme must be one of {', '.join(SUBJECT_SCHEMES)}")
    _text(value.get("code"), f"{field}.code", limit=100)
    return {"kind": value["kind"], "scheme": value["scheme"], "code": value["code"], "name": value.get("name")}


def _values(values):
    if not isinstance(values, list) or len(values) > 50_000:
        _fail("values must be a bounded list")
    result, seen = [], set()
    for index, item in enumerate(values):
        if not isinstance(item, dict) or set(item) - {"start", "end", "value", "flags"}:
            _fail(f"values[{index}] uses start/end/value/flags")
        start = _instant(item.get("start"), f"values[{index}].start", optional=False)
        if start in seen:
            _fail(f"duplicate period start {start}")
        seen.add(start)
        result.append({"start": start, "end": _instant(item.get("end"), f"values[{index}].end"),
                       "value": decimal_text(item.get("value"), f"values[{index}].value"),
                       "flags": dict(item.get("flags") or {})})
    return sorted(result, key=lambda v: v["start"])


def _release(release):
    if not isinstance(release, dict):
        _fail("release states the vintage (key, released_at, basis)", "release_required")
    if release.get("basis") not in RELEASE_BASES:
        _fail(f"release.basis must be one of {', '.join(RELEASE_BASES)}", "release_required")
    _text(release.get("key"), "release.key", limit=500)
    released = _instant(release.get("released_at"), "release.released_at")
    if released is None and release["basis"] != "retrieval_time":
        _fail("a release dated by the provider or a declaration states released_at", "release_required")
    return {"key": release["key"], "released_at": released, "basis": release["basis"],
            "label": release.get("label"), "revision": release.get("revision")}


def record(record_type, provider, dataset, native_id, title, *, source_url, attribution, licence, subject, unit,
           resolution, reference_period, values, release, status, retrieved_at, status_basis=None,
           counterpart=None, facets=None, capacity=None, derived_from=None, publisher_figures=None,
           locator=None, unknowns=None, kind="observation"):
    """Build and validate one energy record (fail closed)."""

    if record_type not in RECORD_TYPES:
        _fail(f"record_type must be one of {', '.join(RECORD_TYPES)}")
    if provider not in PROVIDERS:
        _fail("unknown energy provider")
    if kind != "observation":
        _fail("energy records are observations; forecasts are never stored as observations", "forecast_refused")
    _text(dataset, "dataset", limit=300)
    _text(native_id, "native_id", limit=500)
    _text(title, "title")
    if not isinstance(source_url, str) or not source_url.startswith("https://"):
        _fail("source_url must be the provider's https URL")
    _text(attribution, "attribution", limit=500)
    if not isinstance(licence, dict) or not licence.get("id") or not str(licence.get("terms_url") or "").startswith("https://"):
        _fail("licence needs an id and https terms_url")
    _text(unit, "unit", limit=60)
    if resolution is not None and not _DURATION.fullmatch(str(resolution)):
        _fail("resolution must be an ISO-8601 duration")
    if not isinstance(reference_period, dict):
        _fail("reference_period states start and end")
    period = {"start": _instant(reference_period.get("start"), "reference_period.start", optional=False),
              "end": _instant(reference_period.get("end"), "reference_period.end")}
    if status not in STATUSES:
        _fail(f"status must be one of {', '.join(STATUSES)}")
    facets = dict(facets or {})
    for code in (facets.get("process_type"), facets.get("series_type")):
        if code and f"{provider}:{code}" in FORECAST_CODES:
            _fail(f"{provider} {code} is a forecast; forecasts are never stored as observations", "forecast_refused")
    out = {
        "contract": CONTRACT, "schema_version": SCHEMA_VERSION, "record_type": record_type, "kind": "observation",
        "provider": provider, "dataset": dataset, "native_id": native_id, "title": title,
        "source_url": source_url, "attribution": attribution,
        "licence": {"id": licence["id"], "terms_url": licence["terms_url"]},
        "subject": _subject(subject, "subject"),
        "counterpart": None if counterpart is None else _subject(counterpart, "counterpart"),
        "facets": facets, "unit": unit, "resolution": resolution, "reference_period": period,
        "values": _values(values), "release": _release(release), "status": status,
        "status_basis": status_basis, "retrieved_at": _instant(retrieved_at, "retrieved_at", optional=False),
        "capacity": None, "derived_from": None, "publisher_figures": dict(publisher_figures or {}),
        "locator": dict(locator or {}), "unknowns": sorted(set(unknowns or [])),
    }
    if record_type == "cross_border_flow" and out["counterpart"] is None:
        _fail("cross-border flows name the counterpart area and the direction")
    if record_type == "cross_border_flow" and facets.get("direction") not in {"out_of_subject", "into_subject", "net"}:
        _fail("cross-border flows state direction out_of_subject, into_subject or net")
    if record_type == "capacity":
        if not isinstance(capacity, dict) or capacity.get("level") not in {"zone", "plant", "unit"}:
            _fail("capacity records state level zone, plant or unit")
        detail = {"level": capacity["level"],
                  "effective_from": _instant(capacity.get("effective_from"), "capacity.effective_from"),
                  "effective_to": _instant(capacity.get("effective_to"), "capacity.effective_to"),
                  "operating_status": capacity.get("operating_status"), "plant": capacity.get("plant")}
        if detail["level"] != "zone" and detail["effective_from"] is None:
            out["unknowns"] = sorted(set(out["unknowns"]) | {"capacity.effective_from"})
        out["capacity"] = detail
    elif capacity is not None:
        _fail("only capacity records carry capacity details")
    if record_type == "energy_balance" and not (facets.get("nrg_bal") and facets.get("siec")):
        _fail("energy-balance records keep the balance item (nrg_bal) and product (siec) codes as published")
    if derived_from is not None:
        if not isinstance(derived_from, dict) or derived_from.get("provider") not in PROVIDERS or not derived_from.get("basis"):
            _fail("derived_from names the original provider and the publisher's statement")
        out["derived_from"] = {"provider": derived_from["provider"], "basis": derived_from["basis"],
                               "reconciliation": "none: kept separate from the original provider's records"}
    if out["release"]["released_at"] is None:
        out["unknowns"] = sorted(set(out["unknowns"]) | {"release.released_at"})
    if resolution is None:
        out["unknowns"] = sorted(set(out["unknowns"]) | {"resolution"})
    bad = forbidden_keys({k: v for k, v in out.items() if k != "derived_from"})
    if bad:
        _fail(f"records carry published values only; forbidden keys {bad}", "derived_value_refused")
    return out


def validate(value):
    """Re-validate a stored or received record by rebuilding it."""

    if not isinstance(value, dict) or value.get("contract") != CONTRACT:
        _fail("not a noesis-energy-record-v1 record")
    derived = value.get("derived_from")
    return record(
        value.get("record_type"), value.get("provider"), value.get("dataset"), value.get("native_id"),
        value.get("title"), source_url=value.get("source_url"), attribution=value.get("attribution"),
        licence=value.get("licence"), subject=value.get("subject"), unit=value.get("unit"),
        resolution=value.get("resolution"), reference_period=value.get("reference_period"),
        values=value.get("values"), release=value.get("release"), status=value.get("status"),
        retrieved_at=value.get("retrieved_at"), status_basis=value.get("status_basis"),
        counterpart=value.get("counterpart"), facets=value.get("facets"), capacity=value.get("capacity"),
        derived_from=None if derived is None else {"provider": derived.get("provider"), "basis": derived.get("basis")},
        publisher_figures=value.get("publisher_figures"), locator=value.get("locator"),
        unknowns=value.get("unknowns"),
        kind=value.get("kind", "observation"))


def series_key(value):
    """The series identity (provider, dataset, native id); the release is never part of it."""

    return (value["provider"], value["dataset"], value["native_id"])


def values_digest(value):
    return digest(value["values"])


# ------------------------------------------------------------- ENTSO-E reuse

ENTSOE_LICENCE = {"id": "entsoe-transparency-terms",
                  "terms_url": "https://transparency.entsoe.eu/content/static_content/Static%20content/terms%20and%20conditions/terms%20and%20conditions.html"}
ENTSOE_ATTRIBUTION = "ENTSO-E Transparency Platform"


def entsoe_status(document):
    """Publication status of an ENTSO-E document: revision 1 is as first published, later numbers are revisions."""

    try:
        revision = int(str((document or {}).get("revision") or ""))
    except ValueError:
        return "unknown", "ENTSO-E document states no revisionNumber"
    if revision > 1:
        return "revised", f"ENTSO-E revisionNumber {revision} (> 1)"
    return "provisional", "ENTSO-E revisionNumber 1: first publication, may be revised"


def entsoe_release(document, retrieved_at):
    created = (document or {}).get("created")
    key = f"{(document or {}).get('mrid')}@{(document or {}).get('revision')}"
    if created:
        return {"key": key, "released_at": created, "basis": "document_created", "revision": (document or {}).get("revision"),
                "label": f"ENTSO-E document {(document or {}).get('mrid')} revision {(document or {}).get('revision')}"}
    return {"key": key + f"@retrieved:{retrieved_at}", "released_at": None, "basis": "retrieval_time",
            "revision": (document or {}).get("revision"), "label": None}


def from_grid_event(event, *, retrieved_at):
    """An energy generation/load record from a Climate and Environment ENTSO-E ``grid_event`` (same acquisition).

    Only realised observations are converted; forecasts and unavailability
    events stay Climate and Environment records and are returned as ``None``.
    """

    if event.get("provider") != "entsoe" or event.get("record_type") != "grid_event":
        _fail("only ENTSO-E grid events are converted")
    if event.get("kind") != "observation" or event.get("event_type") not in {"generation", "load"}:
        return None
    document = dict(event.get("document") or {})
    zone = dict(event.get("bidding_zone") or {})
    production = event.get("production_type")
    points = event.get("points") or []
    status, basis = entsoe_status(document)
    fuel = None if production is None else {"code": production.get("code"), "label": production.get("label"),
                                            "scheme": "entsoe-psr-type"}
    doc_type = document.get("type") or ("A75" if event["event_type"] == "generation" else "A65")
    native = f"{doc_type}:{zone.get('code')}:{(fuel or {}).get('code') or event['event_type']}:{event.get('resolution')}"
    return record(
        event["event_type"], "entsoe", f"entsoe:{doc_type}:{document.get('process_type') or 'A16'}", native,
        event["title"], source_url="https://transparency.entsoe.eu/", attribution=ENTSOE_ATTRIBUTION,
        licence=ENTSOE_LICENCE,
        subject={"kind": "bidding-zone", "scheme": "eic", "code": zone.get("code"), "name": zone.get("name")},
        unit=event["unit"], resolution=event.get("resolution"),
        reference_period={"start": points[0]["start"] if points else retrieved_at,
                          "end": points[-1]["end"] if points else None},
        values=[{"start": p["start"], "end": p.get("end"), "value": p.get("value"), "flags": dict(p.get("flags") or {})}
                for p in points],
        release=entsoe_release(document, retrieved_at), status=status, status_basis=basis,
        retrieved_at=retrieved_at,
        facets={"document_type": doc_type, "process_type": document.get("process_type"), "fuel": fuel},
        locator={"document_mrid": document.get("mrid"), "revision": document.get("revision"),
                 "environment_native_id": event.get("native_id"), "file": document.get("file")})


# ------------------------------------------------------------ schema registry

SCHEMA_FILES = {"noesis-energy-record": "contracts/schemas/jsonschema/noesis-energy-record-v1.json"}


def schema_definitions(root=None):
    from pathlib import Path

    root = Path(root) if root else Path(__file__).resolve().parents[2]
    return {name: json.loads((root / path).read_text()) for name, path in SCHEMA_FILES.items()}


def validate_against_schema(value, root=None):
    """JSON-schema errors for one record (empty when valid)."""

    from jsonschema import Draft7Validator

    schema = schema_definitions(root)["noesis-energy-record"]
    return sorted(error.message for error in Draft7Validator(schema).iter_errors(value))


def register_schemas(conn, *, principal_id, scopes, root=None):
    """Register the pack's schema versions in the existing schema registry (idempotent per version)."""

    from src.kb.schema_registry import SchemaRegistry

    registry = SchemaRegistry(conn)
    results = []
    for name, content in sorted(schema_definitions(root).items()):
        definition = {
            "contract": "noesis-schema-module-v1", "name": name, "kind": "schema", "semantic_version": SCHEMA_VERSION,
            "content": content, "owner": "energy.core", "dependencies": [], "compatibility_policy": "backward",
            "provenance": {"kind": "imported", "source": "packs/energy"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(registry.register(definition, f"energy-schema:{name}:{SCHEMA_VERSION}:{digest(content)[:16]}",
                                         principal_id=principal_id, scopes=scopes))
    return results
