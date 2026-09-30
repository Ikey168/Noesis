"""Water and hydrology sources for the Climate and Environment pack: PEGELONLINE, USGS Water Data, EEA WISE (#2582).

Three providers run as sources of the ``climate-environment-water`` source pack
(``packs/climate-environment/source_packs/``, connector ``water``) through
:mod:`src.ingestion.source_pack_runtime` - licence acceptance, budgets,
receipts, checkpoints and the runtime's same-host HTTPS transport - each under
a recorded access contract (:data:`PROVIDER_CONTRACTS`, documented in
``docs/development/water-evidence/source-audit.md``):

* **PEGELONLINE** (``pegelonline``, WA03) - the WSV REST API v2: a station
  with its time series, gauge zero (value, unit, ``validFrom``) and
  characteristic values, and bounded measurement windows (at most 31 days, the
  documented availability) per station and series. Keyed by station UUID and
  number; timestamps and units as published; no resampling. PEGELONLINE
  publishes unchecked raw values, so every value is ``provisional``.
* **USGS Water Data APIs** (``usgs``, WA04) - OGC API collections: a
  monitoring location (with its vertical datum) and bounded ``continuous`` or
  ``daily`` windows per location and parameter code, each value with its
  ``approval_status`` (Provisional/Approved) and qualifiers. A value published
  again as approved is a new revision of the same observation.
* **EEA WISE WFD** (``eea-wise``, WA05) - surface water-body status rows per
  reporting cycle from the WISE WFD database (Discodata SQL service), keyed by
  EU water-body code and cycle, with ecological and chemical status as
  published; and the reporting dataset's published water-body geometry (EEA
  WISE map service) with its vintage. Cycles are never merged.

GRDC is **not implemented** (:data:`NOT_IMPLEMENTED`): its data policy does not
allow redistribution to third parties or commercial use of the original data.

Every provider is ``unverified-live`` until a dated live run (WA13, #2647);
request paths and field names marked *verify* are authored from public
documentation that could not be read directly from this runtime.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb.water_records import (
    MINIMISATION,
    PERSONAL_KEYS,
    WaterError,
    decimal_text,
    observation_key,
    quality,
    quantity,
    statement,
    station_subject,
)

CONNECTOR = "water"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
PROVIDERS = ("pegelonline", "usgs", "eea-wise")
PROVIDER_HOSTS = {"pegelonline": ("www.pegelonline.wsv.de", "pegelonline.wsv.de"),
                  "usgs": ("api.waterdata.usgs.gov",),
                  "eea-wise": ("discodata.eea.europa.eu", "water.discomap.eea.europa.eu")}
PEGELONLINE_BASE = "/webservices/rest-api/v2"
PEGELONLINE_MAX_DAYS = 31  # the documented measurement availability
USGS_MAX_LIMIT = 1000
USGS_MAX_DAYS = 31
WISE_MAX_CODES = 50
PEGELONLINE_QUALITY = ("raw, unchecked (Rohdaten)", ("PEGELONLINE publishes unchecked raw values without warranty "
                       "(terms of use); every PEGELONLINE value is provisional"))
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "pegelonline": {
        "publisher": "Wasserstrassen- und Schifffahrtsverwaltung des Bundes (WSV), PEGELONLINE",
        "access": "PEGELONLINE REST API v2, HTTPS GET under /webservices/rest-api/v2",
        "endpoints": ["/stations/{uuid}.json?includeTimeseries=true&includeCharacteristicValues=true",
                      "/stations/{uuid}/{timeseries}/measurements.json?start&end (verify parameter names)"],
        "authentication": "none (documented: no authentication or authorization)",
        "licence": "DL-DE->Zero-2.0 (Datenlizenz Deutschland - Zero - Version 2.0), per the PEGELONLINE terms of use",
        "terms_url": "https://www.pegelonline.wsv.de/gast/nutzungsbedingungen",
        "attribution": "not required under DL-DE Zero 2.0; Noesis cites 'PEGELONLINE, WSV' with the request URL",
        "redistribution": "permitted (DL-DE Zero 2.0)",
        "rate_limits": "no documented limit found (unverified); one station and one bounded window per request",
        "availability": "measurements for the last 31 days; older values only as raw-data downloads (not used)",
        "quality": "unchecked raw values; stored as provisional with the published wording",
        "versioning": "a station's gauge zero carries validFrom; a changed location, gauge zero or characteristic "
                      "value is a new station revision; a measurement republished with another value is a new "
                      "observation revision",
        "revision_behaviour": "new revision on change; a declared station no longer served is a dated removal",
    },
    "usgs": {
        "publisher": "U.S. Geological Survey (USGS), Water Data APIs",
        "access": "OGC API - Features collections on api.waterdata.usgs.gov, HTTPS GET",
        "endpoints": ["/ogcapi/v0/collections/monitoring-locations/items/{monitoring_location_id}?f=json",
                      "/ogcapi/v0/collections/continuous/items?monitoring_location_id&parameter_code&time&limit&f=json",
                      ("/ogcapi/v0/collections/daily/items?monitoring_location_id&parameter_code&statistic_id&time"
                      "&limit&f=json (verify)")],
        "authentication": "optional api.data.gov key sent as X-Api-Key, held as the NOESIS_USGS_WATER_API_KEY secret "
                          "reference; never stored in manifests, receipts or records",
        "licence": "U.S. Public Domain for USGS-authored data; credit 'U.S. Geological Survey' requested",
        "terms_url": "https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
        "attribution": "U.S. Geological Survey, Water Data APIs, with the request URL",
        "redistribution": "permitted (public domain); provisional data are subject to revision",
        "rate_limits": "per documentation summaries: a low hourly limit per IP address without a key (50-100 "
                       "requests/hour reported) and 1,000 requests/hour with a key (unverified)",
        "quality": "approval_status Provisional or Approved per value; qualifiers per value (e.g. estimated, "
                   "ice-affected)",
        "versioning": "provisional values are revised or approved later; each published state is a revision of the "
                      "same observation (location, parameter, statistic, time)",
        "revision_behaviour": "new revision on change; provisional and approved are separate revisions",
    },
    "eea-wise": {
        "publisher": "European Environment Agency (EEA), WISE Water Framework Directive database",
        "access": "EEA Discodata SQL service (discodata.eea.europa.eu/sql) for the WISE WFD tables; EEA WISE map "
                  "service (water.discomap.eea.europa.eu, ArcGIS REST query, f=geojson) for published water-body "
                  "geometries",
        "endpoints": [("/sql?query=SELECT ... FROM [WISE_WFD].[...].[SWB_SurfaceWaterBody] WHERE "
                      "euSurfaceWaterBodyCode IN (...) AND cYear IN (...)&p=1&nrOfHits=N (verify table and fields)"),
                      ("/arcgis/rest/services/WISE_WFD/{service}/MapServer/{layer}/query?where&outFields&outSR=4326"
                      "&f=geojson (verify)")],
        "authentication": "none",
        "licence": "CC BY 4.0 (EEA standard re-use policy for WISE datasets; unverified by direct read)",
        "terms_url": "https://www.eea.europa.eu/en/legal-notice",
        "attribution": "European Environment Agency, WISE Water Framework Directive database, with the reporting "
                       "cycle and query",
        "redistribution": "permitted with attribution (CC BY 4.0)",
        "rate_limits": "no documented limit found (unverified); one bounded query per selection",
        "versioning": "status is reported per River Basin Management Plan cycle (cYear, e.g. 2016, 2022); each "
                      "code and cycle is its own record; a republished row is a new revision; a declared row no "
                      "longer served is a dated removal",
        "revision_behaviour": "cycles never merged; new revision on change",
    },
}
NOT_IMPLEMENTED = {
    "grdc": {
        "publisher": "Global Runoff Data Centre (GRDC), Bundesanstalt fuer Gewaesserkunde",
        "decision": "not implemented",
        "reason": "the GRDC terms of use and data sharing conditions do not allow redistribution of downloaded data "
                  "to third parties or the general public and do not allow commercial use of the original data; "
                  "downloads need a registration form; Noesis answers and exports would redistribute values",
        "terms_url": "https://grdc.bafg.de/data/data_portal/",
    },
}
BOUNDED_COVERAGE = {
    "pegelonline": {"stations": 2, "series": ["W (water level)", "Q (discharge) where published"],
                    "window": "at most 31 days per request (documented availability); fixture windows of 1 day",
                    "places": "Dresden (Elbe) and one downstream Elbe gauge"},
    "usgs": {"monitoring_locations": 1, "parameters": ["00060 discharge", "00065 gage height"],
             "window": "at most 31 days and at most 1000 values per request; fixture windows of 1 hour",
             "places": "Washington, DC (Potomac River)"},
    "eea-wise": {"water_bodies": 2, "cycles": ["2016 (2nd RBMP)", "2022 (3rd RBMP)"],
                 "fields": "ecological status or potential, chemical status, category, river basin district",
                 "places": "Elbe water bodies around Dresden"},
    "record_caps": "max_results per source run (see the source pack budgets); selections are explicit",
}
LIVE_VERIFICATION: dict[str, dict[str, Any]] = {
    provider: {"status": "unverified-live", "intended": "live-verified after a dated bounded run (WA13, #2647)",
               "note": "no dated live run from this runtime; offline fixtures only"}
    for provider in PROVIDERS
}
LIVE_VERIFICATION["usgs"]["credential"] = "NOESIS_USGS_WATER_API_KEY optional; not configured"
LIVE_VERIFICATION["grdc"] = {"status": "not-implemented", "note": NOT_IMPLEMENTED["grdc"]["reason"]}
_WISE_TABLE = re.compile(r"^\[WISE_WFD\]\.\[[A-Za-z0-9_]+\]\.\[[A-Za-z0-9_]+\]$")
_EU_CODE = re.compile(r"^[A-Z]{2}[A-Za-z0-9_.\-]{1,80}$")
_WISE_FIELDS = ("cYear", "countryCode", "euRBDCode", "euSurfaceWaterBodyCode", "surfaceWaterBodyName",
                "surfaceWaterBodyCategory", "naturalAWBHMWB", "swEcologicalStatusOrPotentialValue",
                "swEcologicalAssessmentYear", "swChemicalStatusValue", "swChemicalAssessmentYear")


class WaterFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _when(value: Any, field: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise SourcePackError("invalid_manifest", f"{field} is an ISO-8601 instant") from exc


def _window(entry: Mapping[str, Any], max_days: int) -> tuple[str, str]:
    start, end = str(entry.get("start") or ""), str(entry.get("end") or "")
    begin, finish = _when(start, "start"), _when(end, "end")
    if begin.tzinfo is None or finish.tzinfo is None:
        raise SourcePackError("invalid_manifest", "window instants carry an offset")
    if not begin < finish or (finish - begin).total_seconds() > max_days * 86_400:
        raise SourcePackError("invalid_manifest", f"a window is bounded: start < end, at most {max_days} days")
    return start, end


# ------------------------------------------------------------------ selections


def selection_entries(source: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    declared = dict(source.get("water") or {})
    provider = str(declared.get("provider") or "")
    if provider not in PROVIDERS:
        raise SourcePackError("invalid_manifest", f"water sources declare a provider in {PROVIDERS}")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[provider]:
        raise SourcePackError("invalid_manifest", f"{provider} is fetched from {PROVIDER_HOSTS[provider][0]} only")
    entries = [dict(e) for e in declared.get("selection") or []]
    if not 1 <= len(entries) <= int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "a water source selects 1..max_pages pages explicitly")
    kinds = {"pegelonline": {"station", "measurements"}, "usgs": {"monitoring-location", "continuous", "daily"},
             "eea-wise": {"status", "geometry"}}[provider]
    for entry in entries:
        kind = entry.get("kind")
        if kind not in kinds:
            raise SourcePackError("invalid_manifest", f"{provider} selections are one of {sorted(kinds)}")
        if provider == "pegelonline":
            if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                                str(entry.get("uuid") or "")):
                raise SourcePackError("invalid_manifest", "PEGELONLINE selections name a station UUID")
            if kind == "measurements":
                if not re.fullmatch(r"[A-Z]{1,8}", str(entry.get("series") or "")):
                    raise SourcePackError("invalid_manifest", "measurements name a time series shortname (W, Q)")
                _window(entry, PEGELONLINE_MAX_DAYS)
        elif provider == "usgs":
            if not re.fullmatch(r"[A-Z]{2,8}-[0-9A-Za-z]{4,20}", str(entry.get("monitoring_location_id") or "")):
                raise SourcePackError("invalid_manifest", "USGS selections name a monitoring_location_id (USGS-...)")
            if kind in {"continuous", "daily"}:
                if not re.fullmatch(r"\d{5}", str(entry.get("parameter_code") or "")):
                    raise SourcePackError("invalid_manifest", "USGS windows name a five-digit parameter code")
                if kind == "daily" and not re.fullmatch(r"\d{5}", str(entry.get("statistic_id") or "")):
                    raise SourcePackError("invalid_manifest", "daily windows name a statistic_id")
                if not 1 <= int(entry.get("limit") or 0) <= min(USGS_MAX_LIMIT, int(source["budgets"]["max_results"])):
                    raise SourcePackError("invalid_manifest", "USGS windows are bounded: 1 <= limit <= 1000")
                _window(entry, USGS_MAX_DAYS)
        else:
            codes = entry.get("codes") or []
            if not 1 <= len(codes) <= WISE_MAX_CODES or not all(_EU_CODE.fullmatch(str(c)) for c in codes):
                raise SourcePackError("invalid_manifest", "WISE selections name 1..50 EU water-body codes")
            if kind == "status":
                if not _WISE_TABLE.fullmatch(str(entry.get("table") or "")):
                    raise SourcePackError("invalid_manifest", "WISE status selections pin a [WISE_WFD] table")
                cycles = entry.get("cycles") or []
                if not cycles or not all(re.fullmatch(r"\d{4}", str(c)) for c in cycles):
                    raise SourcePackError("invalid_manifest", "WISE status selections name reporting cycle years")
            else:
                if not re.fullmatch(r"[A-Za-z0-9_]{1,80}", str(entry.get("service") or "")) or \
                        not re.fullmatch(r"\d{1,3}", str(entry.get("layer") or "")) or not entry.get("vintage"):
                    raise SourcePackError("invalid_manifest", "WISE geometry selections pin service, layer and vintage")
    return provider, entries


def selection_key(provider: str, entry: Mapping[str, Any]) -> str:
    return provider + ":" + hashlib.sha256(json.dumps({k: entry[k] for k in sorted(entry) if k != "label"},
                                                      sort_keys=True).encode()).hexdigest()[:16]


def _wise_sql(entry: Mapping[str, Any]) -> str:
    codes = ", ".join("'" + str(c) + "'" for c in entry["codes"])
    cycles = ", ".join(str(int(c)) for c in entry["cycles"])
    return (f"SELECT {', '.join(_WISE_FIELDS)} FROM {entry['table']} WHERE euSurfaceWaterBodyCode IN ({codes}) "
            f"AND cYear IN ({cycles}) ORDER BY euSurfaceWaterBodyCode, cYear")


def request_for(provider: str, entry: Mapping[str, Any]) -> tuple[str, str, dict[str, str]]:
    """(role, path, query) of one selected page; paths are relative to the endpoint."""
    kind = entry["kind"]
    if provider == "pegelonline":
        uuid = quote(str(entry["uuid"]))
        if kind == "station":
            return "station", f"{PEGELONLINE_BASE}/stations/{uuid}.json", {
                "includeCharacteristicValues": "true", "includeTimeseries": "true"}
        return "measurements", f"{PEGELONLINE_BASE}/stations/{uuid}/{quote(str(entry['series']))}/measurements.json", {
            "end": str(entry["end"]), "start": str(entry["start"])}
    if provider == "usgs":
        location = quote(str(entry["monitoring_location_id"]))
        if kind == "monitoring-location":
            return "location", f"/ogcapi/v0/collections/monitoring-locations/items/{location}", {"f": "json"}
        query = {"f": "json", "limit": str(int(entry["limit"])), "monitoring_location_id": str(
            entry["monitoring_location_id"]), "parameter_code": str(entry["parameter_code"]),
                 "time": f"{entry['start']}/{entry['end']}"}
        if kind == "daily":
            query["statistic_id"] = str(entry["statistic_id"])
        return kind, f"/ogcapi/v0/collections/{kind}/items", query
    if kind == "status":
        return "status", "/sql", {"nrOfHits": str(len(entry["codes"]) * len(entry["cycles"])), "p": "1",
                                  "query": _wise_sql(entry)}
    codes = ", ".join("'" + str(c) + "'" for c in entry["codes"])
    return "geometry", (f"/arcgis/rest/services/WISE_WFD/{quote(str(entry['service']))}/MapServer/"
                        f"{int(entry['layer'])}/query"), {
        "f": "geojson", "outFields": "euSurfaceWaterBodyCode,surfaceWaterBodyName,surfaceWaterBodyCategory",
        "outSR": "4326", "returnGeometry": "true", "where": f"euSurfaceWaterBodyCode IN ({codes})"}


# ------------------------------------------------------------------ parsers


def _source(url: str, provider: str, origin: str, **extra: Any) -> dict[str, Any]:
    contract = PROVIDER_CONTRACTS[provider]
    return {"url": url, "attribution": contract["attribution"], "terms_url": contract["terms_url"],
            "licence": contract["licence"], "evidence_origin": origin,
            **{k: v for k, v in extra.items() if v is not None}}


def _personal(item: Any, path: str = "") -> list[str]:
    """Personal field names present in a response (dropped, never stored)."""
    found: list[str] = []
    if isinstance(item, Mapping):
        for key, value in item.items():
            if str(key).casefold() in PERSONAL_KEYS:
                found.append(f"{path}{key}")
            else:
                found += _personal(value, f"{path}{key}.")
    elif isinstance(item, list):
        for value in item:
            found += _personal(value, path)
    return found


def _strip(item: Any) -> Any:
    if isinstance(item, Mapping):
        return {k: _strip(v) for k, v in item.items() if str(k).casefold() not in PERSONAL_KEYS}
    if isinstance(item, list):
        return [_strip(v) for v in item]
    return item


def parse_pegelonline_station(body: Mapping[str, Any], url: str, *, origin: str) -> tuple[list[dict], dict]:
    uuid, name = _text(body.get("uuid")), _text(body.get("longname")) or _text(body.get("shortname"))
    if uuid is None or name is None:
        raise WaterFormatError("schema_drift", "PEGELONLINE station lacks uuid or name")
    lat, lon = body.get("latitude"), body.get("longitude")
    location = None if lat is None or lon is None else {"latitude": lat, "longitude": lon,
                                                         "crs": "WGS84 (EPSG:4326) as published"}
    water = dict(body.get("water") or {})
    datums, thresholds, series, carry = [], [], [], {}
    for ts in body.get("timeseries") or []:
        short = _text(ts.get("shortname"))
        if short is None or not _text(ts.get("unit")):
            raise WaterFormatError("schema_drift", "PEGELONLINE time series lacks shortname or unit")
        series.append({"parameter": short, "name": _text(ts.get("longname")), "unit": ts["unit"],
                       "interval_min": ts.get("equidistance")})
        carry[short] = {"unit": ts["unit"], "name": _text(ts.get("longname"))}
        zero = ts.get("gaugeZero")
        if isinstance(zero, Mapping) and zero.get("value") is not None:
            datums.append({"kind": "gauge-zero", "series": short, "value": decimal_text(zero["value"]),
                           "unit": _text(zero.get("unit")) or "unstated", "reference": _text(zero.get("unit")),
                           "valid_from": _text(zero.get("validFrom"))})
        for value in ts.get("characteristicValues") or []:
            if isinstance(value, Mapping) and _text(value.get("shortname")):
                thresholds.append({"series": short, "shortname": value["shortname"],
                                   "longname": _text(value.get("longname")), "value": decimal_text(value.get("value")),
                                   "unit": _text(value.get("unit")) or ts["unit"],
                                   "valid_from": _text(value.get("validFrom")),
                                   "timespan": [_text(value.get("timespanStart")), _text(value.get("timespanEnd"))]
                                   if value.get("timespanStart") else None})
    number = _text(body.get("number"))
    record = statement(
        "station", "pegelonline", uuid, subject_name=name, source=_source(url, "pegelonline", origin),
        as_published={"native_id": uuid, "number": number, "name": name, "short_name": _text(body.get("shortname")),
                      "river": {"shortname": _text(water.get("shortname")), "longname": _text(water.get("longname"))}
                      if water else None,
                      "river_km": decimal_text(body.get("km")), "agency": _text(body.get("agency")),
                      "location": location, "datums": datums or None, "thresholds": thresholds or None,
                      "series": series or None,
                      "identifiers": [{"scheme": "pegelonline-uuid", "value": uuid}]
                      + ([{"scheme": "pegelonline-number", "value": number}] if number else []),
                      "country_code": "DE"})
    return [record], carry


def parse_pegelonline_measurements(body: Any, url: str, *, origin: str, uuid: str, series: str,
                                   meta: Mapping[str, Any]) -> list[dict]:
    if not isinstance(body, list):
        raise WaterFormatError("schema_drift", "PEGELONLINE measurements are a JSON list")
    station = station_subject("pegelonline", uuid)
    records = []
    for item in body:
        stamp = _text((item or {}).get("timestamp"))
        if stamp is None or "value" not in item:
            raise WaterFormatError("schema_drift", "PEGELONLINE measurement lacks timestamp or value")
        records.append(statement(
            "observation", "pegelonline", observation_key(station, series, stamp), subject_name=None,
            source=_source(url, "pegelonline", origin),
            as_published={"station_key": station, "parameter": series, "parameter_name": meta.get("name"),
                          "quantity": quantity("pegelonline", series), "unit": meta["unit"], "time": stamp,
                          "value": decimal_text(item.get("value")),
                          "quality": quality("provisional", PEGELONLINE_QUALITY[0], basis=PEGELONLINE_QUALITY[1]),
                          "reference": "gauge zero (PNP) of the station revision valid at the time"
                          if series == "W" else None}))
    return records


def parse_usgs_location(body: Mapping[str, Any], url: str, *, origin: str) -> list[dict]:
    props = dict(body.get("properties") or {})
    native = _text(body.get("id")) or _text(props.get("id"))
    name = _text(props.get("monitoring_location_name"))
    if native is None or name is None:
        raise WaterFormatError("schema_drift", "USGS monitoring location lacks id or monitoring_location_name")
    geometry = dict(body.get("geometry") or {})
    coords = geometry.get("coordinates") if geometry.get("type") == "Point" else None
    location = None if not coords else {"latitude": coords[1], "longitude": coords[0],
                                        "crs": "EPSG:4326 as served by the OGC API"}
    datums = []
    if props.get("altitude") is not None:
        datums.append({"kind": "vertical-datum", "series": None, "value": decimal_text(props.get("altitude")),
                       "unit": "ft (verify)", "reference": _text(props.get("vertical_datum")),
                       "valid_from": None, "accuracy": decimal_text(props.get("altitude_accuracy"))})
    identifiers = [{"scheme": "usgs-monitoring-location", "value": native}]
    for scheme, field in (("us-state-fips", "state_code"), ("us-county-fips", "county_code"),
                          ("usgs-huc", "hydrologic_unit_code")):
        if _text(props.get(field)):
            value = props[field] if field != "county_code" else f"{props.get('state_code') or ''}{props[field]}"
            identifiers.append({"scheme": scheme, "value": str(value)})
    return [statement(
        "station", "usgs", native, subject_name=name,
        source=_source(f"https://waterdata.usgs.gov/monitoring-location/{native}/", "usgs", origin, api_url=url),
        as_published={"native_id": native, "number": _text(props.get("monitoring_location_number")), "name": name,
                      "agency": _text(props.get("agency_code")), "location": location, "datums": datums or None,
                      "identifiers": identifiers, "site_type": _text(props.get("site_type")),
                      "drainage_area": decimal_text(props.get("drainage_area")),
                      "country_code": _text(props.get("country_code")),
                      "river": None})]


def parse_usgs_values(body: Mapping[str, Any], url: str, *, origin: str, kind: str) -> tuple[list[dict], bool]:
    if body.get("type") != "FeatureCollection" or not isinstance(body.get("features"), list):
        raise WaterFormatError("schema_drift", "USGS response is not a FeatureCollection")
    records = []
    for feature in body["features"]:
        props = dict(feature.get("properties") or {})
        location, parameter, stamp = (_text(props.get("monitoring_location_id")), _text(props.get("parameter_code")),
                                      _text(props.get("time")))
        if location is None or parameter is None or stamp is None or not _text(props.get("unit_of_measure")):
            raise WaterFormatError("schema_drift", "USGS value lacks location, parameter, time or unit")
        statistic = _text(props.get("statistic_id"))
        station = station_subject("usgs", location)
        qualifiers = props.get("qualifier")
        qualifiers = [str(q) for q in qualifiers] if isinstance(qualifiers, list) else (
            [str(qualifiers)] if qualifiers not in (None, "") else [])
        records.append(statement(
            "observation", "usgs", observation_key(station, parameter, stamp, statistic if kind == "daily" else None),
            subject_name=None, source=_source(url, "usgs", origin, collection=kind),
            as_published={"station_key": station, "parameter": parameter, "quantity": quantity("usgs", parameter),
                          "unit": props["unit_of_measure"], "time": stamp, "value": decimal_text(props.get("value")),
                          "quality": quality(props.get("approval_status"), props.get("approval_status"),
                                             basis="USGS approval_status of this value"),
                          "qualifiers": qualifiers or None, "statistic": statistic,
                          "time_series_id": _text(props.get("time_series_id")),
                          "last_modified": _text(props.get("last_modified"))}))
    truncated = any(str(link.get("rel")) == "next" for link in body.get("links") or [] if isinstance(link, Mapping))
    return records, not truncated


def _element(element: str, value: Any, field: str, year: Any) -> dict[str, Any]:
    return {"element": element, "value": _text(value), "published_field": field,
            "assessment_year": _text(year)}


def parse_wise_status(body: Mapping[str, Any], url: str, *, origin: str, entry: Mapping[str, Any]
                      ) -> tuple[list[dict], list[dict]]:
    rows = body.get("results")
    if not isinstance(rows, list):
        raise WaterFormatError("schema_drift", "Discodata response lacks results")
    records, seen = [], set()
    for row in rows:
        code, cycle = _text(row.get("euSurfaceWaterBodyCode")), _text(row.get("cYear"))
        if code is None or cycle is None:
            raise WaterFormatError("schema_drift", "WISE row lacks euSurfaceWaterBodyCode or cYear")
        seen.add((code, cycle))
        elements = [_element("ecological_status_or_potential", row.get("swEcologicalStatusOrPotentialValue"),
                             "swEcologicalStatusOrPotentialValue", row.get("swEcologicalAssessmentYear")),
                    _element("chemical_status", row.get("swChemicalStatusValue"), "swChemicalStatusValue",
                             row.get("swChemicalAssessmentYear"))]
        records.append(statement(
            "water_body_assessment", "eea-wise", f"{code}|{cycle}", subject_name=_text(row.get("surfaceWaterBodyName")),
            source=_source(url, "eea-wise", origin, table=entry["table"], cycle=cycle),
            as_published={"eu_code": code, "cycle": cycle, "status_elements": elements,
                          "name": _text(row.get("surfaceWaterBodyName")),
                          "category": _text(row.get("surfaceWaterBodyCategory")),
                          "country_code": _text(row.get("countryCode")), "rbd_code": _text(row.get("euRBDCode")),
                          "ecological_status": elements[0]["value"], "chemical_status": elements[1]["value"],
                          "modification": _text(row.get("naturalAWBHMWB")), "reported_year": cycle}))
    withdrawn = [{"provider": "eea-wise", "record_type": "water_body_assessment", "record_key": f"{code}|{cycle}",
                  "basis": "the declared code and reporting cycle returned no row from the pinned WISE query"}
                 for code in entry["codes"] for cycle in (str(c) for c in entry["cycles"]) if (code, cycle) not in seen]
    return records, withdrawn


def parse_wise_geometry(body: Mapping[str, Any], url: str, *, origin: str, entry: Mapping[str, Any]
                        ) -> tuple[list[dict], list[dict]]:
    if body.get("type") != "FeatureCollection" or not isinstance(body.get("features"), list):
        raise WaterFormatError("schema_drift", "WISE map service response is not a GeoJSON FeatureCollection")
    records, seen = [], set()
    for feature in body["features"]:
        props = dict(feature.get("properties") or {})
        code = _text(props.get("euSurfaceWaterBodyCode"))
        if code is None:
            raise WaterFormatError("schema_drift", "WISE feature lacks euSurfaceWaterBodyCode")
        seen.add(code)
        records.append(statement(
            "water_body", "eea-wise", code, subject_name=_text(props.get("surfaceWaterBodyName")),
            source=_source(url, "eea-wise", origin, service=entry["service"], layer=str(entry["layer"])),
            as_published={"eu_code": code, "name": _text(props.get("surfaceWaterBodyName")) or code,
                          "category": _text(props.get("surfaceWaterBodyCategory")),
                          "country_code": code[:2], "geometry": feature.get("geometry"),
                          "geometry_vintage": str(entry["vintage"]),
                          "geometry_crs": "EPSG:4326 (outSR=4326 requested)",
                          "identifiers": [{"scheme": "eu-water-body", "value": code}]}))
    withdrawn = [{"provider": "eea-wise", "record_type": "water_body", "record_key": code,
                  "basis": "the declared water-body code returned no feature from the pinned map-service query"}
                 for code in entry["codes"] if code not in seen]
    return records, withdrawn


# ------------------------------------------------------------------ runtime adapter


class WaterSourceAdapter:
    """One page per selected station, measurement window, monitoring location, value window or WISE query."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.provider, self.entries = selection_entries(self.source)
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self._secret = secret if self.provider == "usgs" else None
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "water": {"provider": self.provider, "selected": len(self.entries),
                      "live_verification": LIVE_VERIFICATION[self.provider]["status"]},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "water runs fetch the declared selection only")

    def _get(self, path: str, query: Mapping[str, str]) -> tuple[int, Any, str, str, str]:
        base = self.source["endpoint"].rstrip("/") + path
        url = base + ("?" + urlencode(sorted(query.items())) if query else "")
        headers = {"Accept": "application/json"}
        if self._secret:
            headers["X-Api-Key"] = self._secret
        response = self.transport(url=base, params=dict(query), headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        if (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold() != host:
            raise SourcePackError("network_policy", "response was served from another host")
        status = int(response.get("status", 200))
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "response exceeds its byte limit")
        origin = "fixture" if response.get("origin") == "fixture" else "live"
        sha = hashlib.sha256(raw).hexdigest()
        if status == 404:
            return status, None, url, origin, sha
        if status == 429:
            from src.ingestion.source_pack_runtime import _retry_after_ms

            folded = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
            raise SourcePackError("rate_limited", f"{self.provider} rate limit reached",
                                  retry_after_ms=_retry_after_ms(folded.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"request refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        if self._secret and len(self._secret) >= 8 and self._secret.encode() in raw:
            raise SourcePackError("schema_drift", "provider echoed a credential; response is not safe evidence")
        try:
            return status, json.loads(raw.decode("utf-8-sig")), url, origin, sha
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourcePackError("schema_drift", "response is not UTF-8 JSON") from exc

    def _page(self, entry: Mapping[str, Any], carry: dict[str, Any]) -> dict[str, Any]:
        role, path, query = request_for(self.provider, entry)
        result: dict[str, Any] = {"url": None, "origin": None, "sha": None, "statements": [], "withdrawn": [],
                                  "personal": [], "complete": True, "window": None}
        if entry.get("start"):
            result["window"] = {"start": entry["start"], "end": entry["end"]}
        if role == "measurements" and str(entry["uuid"]) in (carry.get("missing") or []):
            result["outcome"] = "station_not_served"  # nothing is requested for a station the provider withdrew
            return result
        _status, body, url, origin, sha = self._get(path, query)
        result.update(url=url, origin=origin, sha=sha)
        if body is None:
            result["outcome"] = "not_found"
            if role in {"station", "location"}:
                native = str(entry.get("uuid") or entry.get("monitoring_location_id"))
                carry.setdefault("missing", []).append(native)
                result["withdrawn"] = [{"provider": self.provider, "record_type": "station", "record_key": native,
                                        "basis": "the declared station is no longer served by the provider"}]
            return result
        result["outcome"] = "found"
        result["personal"] = _personal(body)
        body = _strip(body)
        if role == "station":
            result["statements"], series = parse_pegelonline_station(body, url, origin=origin)
            carry.setdefault("series", {})[str(entry["uuid"])] = series
        elif role == "measurements":
            meta = dict(dict(carry.get("series") or {}).get(str(entry["uuid"])) or {}).get(str(entry["series"]))
            if meta is None:
                raise SourcePackError("invalid_manifest", "PEGELONLINE measurements are selected after their station "
                                                          "(the unit comes from the station's time series)")
            result["statements"] = parse_pegelonline_measurements(body, url, origin=origin, uuid=str(entry["uuid"]),
                                                                  series=str(entry["series"]), meta=meta)
        elif role == "location":
            result["statements"] = parse_usgs_location(body, url, origin=origin)
        elif role in {"continuous", "daily"}:
            result["statements"], result["complete"] = parse_usgs_values(body, url, origin=origin, kind=role)
        elif role == "status":
            result["statements"], result["withdrawn"] = parse_wise_status(body, url, origin=origin, entry=entry)
        else:
            result["statements"], result["withdrawn"] = parse_wise_geometry(body, url, origin=origin, entry=entry)
        return result

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        state = {"i": 0, "carry": {}} if cursor is None else json.loads(cursor)
        if state.get("scope") not in (None, self.source["source_hash"]):
            raise SourcePackError("cursor_drift", "cursor belongs to a different selection")
        index = int(state.get("i", -1))
        if not 0 <= index < len(self.entries):
            raise SourcePackError("cursor_drift", "cursor names no declared selection")
        entry = self.entries[index]
        carry = dict(state.get("carry") or {})
        try:
            page = self._page(entry, carry)
        except (WaterFormatError, WaterError, KeyError, TypeError, ValueError, IndexError) as exc:
            if isinstance(exc, SourcePackError):
                raise
            raise SourcePackError("schema_drift", f"{getattr(exc, 'code', 'parse')}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(page["statements"]) > limit:
            raise SourcePackError("budget_exhausted", "selection has more statements than the run's result budget")
        records = []
        for item in page["statements"]:
            content = json.dumps(item, sort_keys=True, ensure_ascii=False)
            records.append({
                "id": f"{item['subject']['key']}|{item['record_type']}|{item['record_key']}|"
                      + hashlib.sha256(content.encode()).hexdigest()[:12],
                "title": f"{item['subject'].get('name') or item['subject']['key']}: {item['record_type']}",
                "url": item["source"]["url"], "language": "en", "content": content, "water_record": item})
        label = {k: entry[k] for k in sorted(entry) if k in {"kind", "uuid", "series", "monitoring_location_id",
                                                              "parameter_code", "statistic_id", "codes", "cycles",
                                                              "label", "vintage"}}
        receipt = {"status": 200, "provider": self.provider, "selection": label, "outcome": page["outcome"],
                   "statements": len(records), "url": page["url"], "response_sha256": page["sha"],
                   "window": page["window"], "window_complete": page["complete"],
                   "withdrawn": page["withdrawn"], "personal_fields_dropped": sorted(set(page["personal"])),
                   "evidence_origin": page["origin"], "final_page": index + 1 >= len(self.entries),
                   "licence": PROVIDER_CONTRACTS[self.provider]["licence"],
                   "live_verification": LIVE_VERIFICATION[self.provider]["status"]}
        next_cursor = (json.dumps({"i": index + 1, "scope": self.source["source_hash"], "carry": carry},
                                  sort_keys=True) if index + 1 < len(self.entries) else None)
        return RuntimePage(tuple(records), next_cursor, sum(len(r["content"]) for r in records), receipt=receipt)


FIXTURE_SECRET = "fixture-credential-not-a-real-key"
ADAPTERS = {CONNECTOR: WaterSourceAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path and sorted query; responses are marked as fixture evidence."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout, **_):
        del headers, timeout
        parts = urlsplit(url)
        key = parts.path + ("?" + urlencode(sorted(dict(params or {}).items())) if params else "")
        page = by_key.get(key)
        if page is None:
            return {"status": 404, "headers": {}, "content": b"", "origin": "fixture"}
        body = page.get("body")
        content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture"}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = WaterSourceAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                                 secret=FIXTURE_SECRET)
    records, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


__all__ = [
    "ADAPTERS", "BOUNDED_COVERAGE", "CONNECTOR", "FIXTURE_SECRET", "LIVE_VERIFICATION", "MINIMISATION",
    "NOT_IMPLEMENTED", "PROVIDERS", "PROVIDER_CONTRACTS", "PROVIDER_HOSTS", "WaterFormatError", "WaterSourceAdapter",
    "fixture_transport", "parse_pegelonline_measurements", "parse_pegelonline_station", "parse_usgs_location",
    "parse_usgs_values", "parse_wise_geometry", "parse_wise_status", "replay_native_fixture", "request_for",
    "selection_entries", "selection_key",
]
