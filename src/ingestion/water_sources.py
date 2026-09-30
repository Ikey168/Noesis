"""Water and hydrology sources for the Climate and Environment pack: PEGELONLINE, USGS Water Data, EEA WISE (#2582).

Three providers run as sources of the ``climate-environment-water`` source
pack (``packs/climate-environment/source_packs/``, connector ``water``)
through :mod:`src.ingestion.source_pack_runtime` - licence acceptance,
budgets, receipts, checkpoints and the runtime's same-host HTTPS transport -
each under a recorded access contract (:data:`PROVIDER_CONTRACTS`, documented
in ``docs/development/water-evidence/source-audit.md``, WA01 #2587). It sits
beside the biodiversity adapter (:mod:`src.ingestion.biodiversity_sources`)
and follows the same rules: explicit bounded selections, fail-closed parsers,
provider hosts only, values as published.

* **PEGELONLINE** (``pegelonline``, WA03 #2597) - WSV gauging stations keyed by
  station UUID *and* number, with the water they stand on, the gauge zero and
  its validity per time series and the published characteristic values; a
  bounded window of raw measurements per declared time series, timestamps and
  units as published, never resampled. PEGELONLINE publishes raw data only, so
  every value is ``provisional``.
* **USGS Water Data** (``usgs``, WA04 #2602) - OGC API monitoring locations and
  daily values for declared parameter codes and a bounded date window;
  ``approval_status`` (Provisional / Approved) and qualifiers per value. A
  provisional value later approved is a new revision; a value withdrawn from a
  complete window is a tombstone revision.
* **EEA WISE** (``eea-wise``, WA05 #2607) - Water Framework Directive
  surface water bodies keyed by EU code, their published geometry vintage, and
  ecological and chemical status per reporting cycle as reported. Cycles are
  separate records; nothing is merged or re-assessed.

GRDC is recorded as ``not-implemented`` (:data:`NOT_IMPLEMENTED`): data are
released on request under terms that exclude redistribution. Every provider is
``unverified-live`` until a dated live run (WA13, #2647); request paths and
field names marked *verify* are authored from public documentation.
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
    WaterError,
    digest,
    observation_key,
    quality,
    statement,
)

CONNECTOR = "water"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
PROVIDERS = ("pegelonline", "usgs", "eea-wise")
PROVIDER_HOSTS = {"pegelonline": ("www.pegelonline.wsv.de",), "usgs": ("api.waterdata.usgs.gov",),
                  "eea-wise": ("discodata.eea.europa.eu",)}
PEGELONLINE_BASE = "/webservices/rest-api/v2"
USGS_BASE = "/ogcapi/v0"
MAX_WINDOW_DAYS = {"pegelonline": 31, "usgs": 31}
MAX_SERIES = 3
MAX_WATER_BODIES = 20
USGS_DAILY_LIMIT = 100
PEGELONLINE_PARAMETERS = {"W": "water_level", "Q": "discharge", "WT": "water_temperature"}
USGS_PARAMETERS = {"00065": "water_level", "00060": "discharge", "00010": "water_temperature"}
USGS_STATISTICS = {"00001": "maximum", "00002": "minimum", "00003": "mean", "00008": "median"}
WISE_TABLE = "[WISE_WFD].[latest].[SWB_SurfaceWaterBody_Status]"  # verify the published view name
WISE_SPATIAL_TABLE = "[WISE_WFD_Spatial].[latest].[SurfaceWaterBody_Geometry]"  # verify (see audit)
WISE_ECOLOGICAL = {"1": "High", "2": "Good", "3": "Moderate", "4": "Poor", "5": "Bad", "Unknown": "Unknown",
                   "Not applicable": "Not applicable"}
WISE_CHEMICAL = {"2": "Good", "3": "Failing to achieve good", "Unknown": "Unknown",
                 "Not applicable": "Not applicable"}
WISE_CATEGORIES = {"RW": "river", "LW": "lake", "TW": "transitional", "CW": "coastal", "TeW": "territorial"}
_PERSONAL_HINT = re.compile(r"(e-?mail|phone|telephone|contact|person|observer|personnel)", re.IGNORECASE)

PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "pegelonline": {
        "publisher": "Wasserstrassen- und Schifffahrtsverwaltung des Bundes (WSV), PEGELONLINE",
        "access": "PEGELONLINE REST API v2, HTTPS GET /webservices/rest-api/v2/stations/{uuid}.json and "
                  "/stations/{uuid}/{timeseries}/measurements.json?start&end",
        "endpoints": [PEGELONLINE_BASE + "/stations/{uuid}.json?includeTimeseries=true&includeCharacteristicValues="
                      "true", PEGELONLINE_BASE + "/stations/{uuid}/{timeseries}/measurements.json?start={iso}&end={iso}"],
        "authentication": "none",
        "licence": "Datenlizenz Deutschland - Zero - Version 2.0 (DL-DE->Zero-2.0) (verify on the imprint and "
                   "terms page)",
        "terms_url": "https://www.pegelonline.wsv.de/gast/nutzungsbedingungen",
        "attribution": "Source: WSV, PEGELONLINE (attribution not required under DL-DE Zero, kept for citation)",
        "redistribution": "permitted without restriction under DL-DE Zero 2.0 (verify)",
        "rate_limits": "no published hard limit (verify); fair use; one station request plus one request per declared "
                       "time series per selection",
        "versioning": "raw, unchecked measurements (Rohdaten) kept by the service for about 31 days; station "
                      "metadata (location, gauge zero with validFrom, characteristic values) updated in place",
        "revision_behaviour": "each distinct published station or value is a new revision; gauge-zero and location "
                              "changes are station revisions; windows are never compared for absence because values "
                              "roll off the 31-day service window",
        "updates_corrections_removals": "no revision marker is published; changes are detected by comparing the "
                                        "published payload between retrievals",
    },
    "usgs": {
        "publisher": "U.S. Geological Survey, Water Data APIs (OGC API - Features)",
        "access": "HTTPS GET /ogcapi/v0/collections/monitoring-locations/items/{id} and "
                  "/ogcapi/v0/collections/daily/items?monitoring_location_id&parameter_code&statistic_id&time&limit",
        "endpoints": [USGS_BASE + "/collections/monitoring-locations/items/{id}?f=json",
                      USGS_BASE + "/collections/daily/items?monitoring_location_id&parameter_code&statistic_id&time"
                                  "&limit&f=json"],
        "authentication": "optional api.data.gov key as the NOESIS_USGS_WATER_API_KEY secret reference (X-Api-Key "
                          "header, verify); without a key requests run under the lower anonymous limit and receipts "
                          "say so",
        "licence": "U.S. Government work, public domain (USGS data policy); cite USGS as the source (verify)",
        "terms_url": "https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
        "attribution": "U.S. Geological Survey, USGS Water Data for the Nation, accessed via api.waterdata.usgs.gov",
        "redistribution": "permitted (public domain); provisional data carry the USGS provisional-data disclaimer",
        "rate_limits": "api.data.gov hourly limits per key or per IP without a key (verify current numbers); the "
                       "pack keeps to one location request and one daily page per selection",
        "versioning": "values carry approval_status (Provisional or Approved) and qualifiers; provisional values may "
                      "be revised or deleted before approval; last_modified per value",
        "revision_behaviour": "a changed value, approval status or qualifier is a new revision; a value absent from a "
                              "later complete window of the same selection is a dated tombstone revision",
        "updates_corrections_removals": "last_modified per value; approval status transitions; deletions detected "
                                        "by comparing complete windows",
    },
    "eea-wise": {
        "publisher": "European Environment Agency, WISE Water Framework Directive database (Member State reporting)",
        "access": "EEA Discodata SQL API, HTTPS GET /sql?query={bounded SELECT}&p=1&nrOfHits={n} over the WISE WFD "
                  "surface water body status view and the water-body geometry view (verify view names)",
        "endpoints": ["/sql?query=SELECT ... FROM " + WISE_TABLE + " WHERE euSurfaceWaterBodyCode IN (...) AND cYear "
                      "IN (...)", "/sql?query=SELECT ... FROM " + WISE_SPATIAL_TABLE + " WHERE "
                      "euSurfaceWaterBodyCode IN (...)"],
        "authentication": "none",
        "licence": "EEA standard re-use policy: CC BY 4.0 unless stated otherwise; data reported by Member States "
                   "(verify per dataset metadata)",
        "terms_url": "https://www.eea.europa.eu/en/legal-notice",
        "attribution": "European Environment Agency, WISE WFD database (reporting cycle as stated), reported by "
                       "the Member State",
        "redistribution": "permitted with attribution (CC BY 4.0)",
        "rate_limits": "no published hard limit (verify); nrOfHits bounds each page; one status page and one "
                       "geometry page per selection",
        "versioning": "one dataset per WFD reporting cycle (2nd RBMP 2016, 3rd RBMP 2022); resubmissions and "
                      "corrections replace rows in the 'latest' views",
        "revision_behaviour": "each cycle is its own assessment record keyed by EU code and cycle; a corrected row "
                              "is a new revision of that cycle's record; cycles are never merged",
        "updates_corrections_removals": "no row-level revision marker; changes detected between retrievals; a "
                                        "withdrawn row is reported as absent in the receipt",
    },
}
NOT_IMPLEMENTED = {
    "grdc": {
        "publisher": "Global Runoff Data Centre (BfG)",
        "status": "not-implemented",
        "reason": "discharge data are released on individual request after registration, for the requester's own "
                  "use; redistribution and publication of the data are not permitted, so records could not be "
                  "cited or exported by Noesis (verify current GRDC data policy)",
        "terms_url": "https://grdc.bafg.de/",
        "revisit": "if GRDC publishes an openly licensed product (e.g. station catalogue under an open licence)",
    },
}
BOUNDED_COVERAGE = {
    "pegelonline": "two declared stations on one water, time series W and Q, one 2-hour window per series (at most "
                   "31 days by the service)",
    "usgs": "two declared monitoring locations, daily mean discharge (00060) and gage height (00065) over one "
            "week each, at most 100 values per page",
    "eea-wise": "three declared surface water bodies in one country, reporting cycles 2016 and 2022, one "
                "geometry vintage each where published",
    "caps": {"series_per_station": MAX_SERIES, "window_days": MAX_WINDOW_DAYS, "water_bodies": MAX_WATER_BODIES,
             "usgs_page_limit": USGS_DAILY_LIMIT},
    "justification": "enough to exercise revisions (gauge zero, location, provisional to approved, corrected cycle "
                     "rows), gaps, thresholds and a place-to-water journey without bulk acquisition",
}
LIVE_VERIFICATION = {
    provider: {"status": "unverified-live", "intended": "live-verified after a dated bounded run (WA13, #2647)",
               "note": "no dated live run from this runtime; offline fixtures only"}
    for provider in PROVIDERS
}
LIVE_VERIFICATION["usgs"]["credential"] = "NOESIS_USGS_WATER_API_KEY optional; not configured here"
LIVE_VERIFICATION["grdc"] = {"status": "not-implemented", "note": NOT_IMPLEMENTED["grdc"]["reason"]}


class WaterFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _number(value: Any) -> float | int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise WaterFormatError("schema_drift", f"not a number: {value!r}")
    if isinstance(value, (int, float)):
        return value
    try:
        number = float(str(value))
    except ValueError as exc:
        raise WaterFormatError("schema_drift", f"not a number: {value!r}") from exc
    return int(number) if number.is_integer() and "." not in str(value) else number


def _instant(text: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(text))
    except ValueError as exc:
        raise SourcePackError("invalid_manifest", f"not an ISO-8601 instant: {text!r}") from exc


def personal_hints(value: Any, path: str = "") -> list[str]:
    """Keys in a raw payload that look like personal data; never stored, reported as dropped."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _PERSONAL_HINT.search(str(key)):
                found.append(f"{path}{key}")
            found += personal_hints(item, f"{path}{key}.")
    elif isinstance(value, list):
        for item in value:
            found += personal_hints(item, path)
    return sorted(set(found))


# ------------------------------------------------------------------ selections


def _window_days(start: str, end: str) -> float:
    a, b = _instant(start), _instant(end)
    if b <= a:
        raise SourcePackError("invalid_manifest", "a window ends after it starts")
    return (b - a).total_seconds() / 86_400


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
    kinds = {"pegelonline": {"station"}, "usgs": {"monitoring_location", "daily"},
             "eea-wise": {"water_body_geometry", "water_body_status"}}[provider]
    for entry in entries:
        kind = entry.get("kind")
        if kind not in kinds:
            raise SourcePackError("invalid_manifest", f"{provider} selections are one of {sorted(kinds)}")
        if provider == "pegelonline":
            if not re.fullmatch(r"[0-9a-f-]{36}", str(entry.get("uuid") or "")) or not entry.get("number"):
                raise SourcePackError("invalid_manifest", "PEGELONLINE selections name the station UUID and number")
            series = list(entry.get("timeseries") or [])
            if not 1 <= len(series) <= MAX_SERIES or set(series) - set(PEGELONLINE_PARAMETERS):
                raise SourcePackError("invalid_manifest", f"1..{MAX_SERIES} time series of {sorted(PEGELONLINE_PARAMETERS)}")
            if _window_days(entry.get("start"), entry.get("end")) > MAX_WINDOW_DAYS["pegelonline"]:
                raise SourcePackError("invalid_manifest", "a PEGELONLINE window is at most 31 days")
        if provider == "usgs":
            if kind == "monitoring_location" and not re.fullmatch(r"[A-Z]+-\d{8,15}", str(entry.get("id") or "")):
                raise SourcePackError("invalid_manifest", "a monitoring location is named by its agency-number id")
            if kind == "daily":
                if not re.fullmatch(r"[A-Z]+-\d{8,15}", str(entry.get("monitoring_location_id") or "")):
                    raise SourcePackError("invalid_manifest", "daily values name their monitoring location")
                if entry.get("parameter_code") not in USGS_PARAMETERS or entry.get("statistic_id") not in \
                        USGS_STATISTICS:
                    raise SourcePackError("invalid_manifest", "daily values name a supported parameter and statistic")
                start, _, end = str(entry.get("time") or "").partition("/")
                if _window_days(start + "T00:00:00+00:00", end + "T00:00:00+00:00") > MAX_WINDOW_DAYS["usgs"]:
                    raise SourcePackError("invalid_manifest", "a daily window is at most 31 days")
                if not 1 <= int(entry.get("limit") or 0) <= min(USGS_DAILY_LIMIT, int(source["budgets"]["max_results"])):
                    raise SourcePackError("invalid_manifest", f"daily pages are small: 1 <= limit <= {USGS_DAILY_LIMIT}")
        if provider == "eea-wise":
            codes = list(entry.get("eu_codes") or [])
            if not 1 <= len(codes) <= MAX_WATER_BODIES or not all(
                    re.fullmatch(r"[A-Z]{2}[A-Za-z0-9_.-]{1,60}", str(c)) for c in codes):
                raise SourcePackError("invalid_manifest", f"1..{MAX_WATER_BODIES} EU water-body codes")
            if kind == "water_body_status" and (not entry.get("cycles") or not all(
                    re.fullmatch(r"\d{4}", str(c)) for c in entry["cycles"])):
                raise SourcePackError("invalid_manifest", "status selections name their reporting cycles")
    return provider, entries


def selection_key(provider: str, entry: Mapping[str, Any], suffix: str = "") -> str:
    return provider + ":" + digest({k: entry[k] for k in sorted(entry) if k != "label"})[:16] + suffix


def wise_sql(entry: Mapping[str, Any]) -> str:
    """The bounded SELECT for one WISE selection; codes and cycles are validated before they are interpolated."""
    codes = ",".join(f"'{c}'" for c in entry["eu_codes"])
    if entry["kind"] == "water_body_geometry":
        return ("SELECT euSurfaceWaterBodyCode, cYear, geometryGeoJSON FROM " + WISE_SPATIAL_TABLE
                + f" WHERE euSurfaceWaterBodyCode IN ({codes}) ORDER BY euSurfaceWaterBodyCode, cYear")
    cycles = ",".join(str(int(c)) for c in entry["cycles"])
    return ("SELECT countryCode, euRBDCode, rbdName, euSurfaceWaterBodyCode, surfaceWaterBodyName, "
            "surfaceWaterBodyCategory, naturalAWBHMWB, cYear, swEcologicalStatusOrPotentialValue, "
            "swEcologicalAssessmentYear, swChemicalStatusValue, swChemicalAssessmentYear FROM " + WISE_TABLE
            + f" WHERE euSurfaceWaterBodyCode IN ({codes}) AND cYear IN ({cycles}) "
              "ORDER BY euSurfaceWaterBodyCode, cYear")


def requests_for(provider: str, entry: Mapping[str, Any]) -> list[tuple[str, str, dict[str, str]]]:
    """(role, path, query) of every request of one selected page; paths are relative to the endpoint."""
    kind = entry["kind"]
    if provider == "pegelonline":
        uuid = quote(str(entry["uuid"]))
        out = [("station", f"{PEGELONLINE_BASE}/stations/{uuid}.json",
                {"includeTimeseries": "true", "includeCharacteristicValues": "true"})]
        for series in entry["timeseries"]:
            out.append((f"measurements:{series}", f"{PEGELONLINE_BASE}/stations/{uuid}/{quote(series)}/measurements.json",
                        {"start": str(entry["start"]), "end": str(entry["end"])}))
        return out
    if provider == "usgs":
        if kind == "monitoring_location":
            return [("location", f"{USGS_BASE}/collections/monitoring-locations/items/{quote(str(entry['id']))}",
                     {"f": "json"})]
        return [("daily", f"{USGS_BASE}/collections/daily/items",
                 {"monitoring_location_id": str(entry["monitoring_location_id"]),
                  "parameter_code": str(entry["parameter_code"]), "statistic_id": str(entry["statistic_id"]),
                  "time": str(entry["time"]), "limit": str(int(entry["limit"])), "f": "json"})]
    hits = str(len(entry["eu_codes"]) * max(1, len(entry.get("cycles") or [1])))
    return [(kind, "/sql", {"query": wise_sql(entry), "p": "1", "nrOfHits": hits})]


# ------------------------------------------------------------------ parsers


def _source(url: str, provider: str, origin: str, **extra: Any) -> dict[str, Any]:
    return {"url": url, "attribution": PROVIDER_CONTRACTS[provider]["attribution"],
            "terms_url": PROVIDER_CONTRACTS[provider]["terms_url"],
            "licence": PROVIDER_CONTRACTS[provider]["licence"].split(" (verify")[0], "evidence_origin": origin,
            **{k: v for k, v in extra.items() if v is not None}}


def _gauge_zero(series: Mapping[str, Any]) -> dict[str, Any] | None:
    zero = dict(series.get("gaugeZero") or {})
    if not zero:
        return None
    return {"value": _number(zero.get("value")), "unit": _text(zero.get("unit")),
            "valid_from": _text(zero.get("validFrom"))}


def parse_pegelonline_station(body: Mapping[str, Any], url: str, *, origin: str) -> dict:
    uuid, number = _text(body.get("uuid")), _text(body.get("number"))
    name = _text(body.get("longname")) or _text(body.get("shortname"))
    if uuid is None or number is None or name is None:
        raise WaterFormatError("schema_drift", "PEGELONLINE station lacks uuid, number or name")
    water = dict(body.get("water") or {})
    river = None
    if _text(water.get("shortname")) or _text(water.get("longname")):
        river = {"name": _text(water.get("longname")) or _text(water.get("shortname")),
                 "identifier": _text(water.get("shortname")), "scheme": "pegelonline-water"}
    timeseries, thresholds = [], []
    gauge_zero = None
    for series in body.get("timeseries") or []:
        code = _text(series.get("shortname"))
        if code is None:
            raise WaterFormatError("schema_drift", "a PEGELONLINE time series lacks its shortname")
        zero = _gauge_zero(series)
        if code == "W" and zero:
            gauge_zero = zero
        timeseries.append({"parameter_code": code, "parameter": PEGELONLINE_PARAMETERS.get(code),
                           "name": _text(series.get("longname")), "unit": _text(series.get("unit")) or "unstated",
                           "equidistance_min": _number(series.get("equidistance")), "gauge_zero": zero})
        for value in series.get("characteristicValues") or []:
            thresholds.append({"name": _text(value.get("shortname")), "label": _text(value.get("longname")),
                               "parameter": PEGELONLINE_PARAMETERS.get(code, code), "parameter_code": code,
                               "unit": _text(value.get("unit")) or _text(series.get("unit")),
                               "value": _number(value.get("value")), "valid_from": _text(value.get("validFrom")),
                               "timespan": [_text(value.get("timespanStart")), _text(value.get("timespanEnd"))],
                               "basis": "characteristic value published by the gauge operator"})
    lat, lon = _number(body.get("latitude")), _number(body.get("longitude"))
    page = f"https://www.pegelonline.wsv.de/gast/stammdaten?pegelnr={number}"
    return statement("station", "pegelonline", uuid, subject_name=name, source=_source(page, "pegelonline", origin,
                                                                                     api_url=url),
                     as_published={"native_id": uuid, "number": number, "name": name, "river": river,
                                   "agency": _text(body.get("agency")),
                                   "location": {"latitude": lat, "longitude": lon, "crs": "EPSG:4326",
                                                "basis": "published station coordinates"},
                                   "datum": {"gauge_zero": gauge_zero} if gauge_zero else None,
                                   "km": _number(body.get("km")), "timeseries": timeseries or None,
                                   "thresholds": [t for t in thresholds if t["name"] and t["value"] is not None]
                                   or None, "country_code": "DE"})


def parse_pegelonline_measurements(body: Any, url: str, *, origin: str, station: Mapping[str, Any],
                                   code: str) -> list[dict]:
    if not isinstance(body, list):
        raise WaterFormatError("schema_drift", "PEGELONLINE measurements are a list")
    published = station["as_published"]
    series = next((s for s in published.get("timeseries") or [] if s["parameter_code"] == code), None)
    if series is None:
        raise WaterFormatError("schema_drift", f"station publishes no time series {code}")
    records = []
    for item in body:
        time, value = _text(item.get("timestamp")), _number(item.get("value"))
        if time is None:
            raise WaterFormatError("schema_drift", "a measurement lacks its timestamp")
        if value is None:
            continue  # nothing published for that time: stays missing
        records.append(statement(
            "observation", "pegelonline", observation_key(published["native_id"], code, None, time),
            subject_name=published["name"], source=_source(url, "pegelonline", origin),
            as_published={"station_id": published["native_id"], "parameter": PEGELONLINE_PARAMETERS[code],
                          "parameter_code": code, "statistic": "instantaneous", "time": time, "value": value,
                          "unit": series["unit"], "quality": quality(None, provider="pegelonline"),
                          "gauge_zero": series.get("gauge_zero") if code == "W" else None}))
    return records


def parse_usgs_location(body: Mapping[str, Any], url: str, *, origin: str) -> dict:
    props = dict(body.get("properties") or {})
    ident = _text(body.get("id"))
    name = _text(props.get("monitoring_location_name"))
    if ident is None or name is None:
        raise WaterFormatError("schema_drift", "USGS monitoring location lacks id or name")
    coords = list(dict(body.get("geometry") or {}).get("coordinates") or [None, None])
    huc = _text(props.get("hydrologic_unit_code"))
    datum = None
    if props.get("altitude") is not None or _text(props.get("vertical_datum")):
        datum = {"altitude": _number(props.get("altitude")), "vertical_datum": _text(props.get("vertical_datum")),
                 "unit": "ft"}
    page = f"https://waterdata.usgs.gov/monitoring-location/{_text(props.get('monitoring_location_number')) or ident}/"
    return statement("station", "usgs", ident, subject_name=name, source=_source(page, "usgs", origin, api_url=url),
                     as_published={"native_id": ident, "number": _text(props.get("monitoring_location_number")),
                                   "name": name, "agency": _text(props.get("agency_code")),
                                   "river": {"name": None, "identifier": huc, "scheme": "usgs-huc"} if huc else None,
                                   "location": {"latitude": _number(coords[1]), "longitude": _number(coords[0]),
                                                "crs": "EPSG:4326", "basis": "published monitoring-location point"},
                                   "datum": datum, "site_type": _text(props.get("site_type")),
                                   "hydrologic_unit_code": huc, "country_code": _text(props.get("country_code")),
                                   "region": _text(props.get("state_name")),
                                   "drainage_area": _number(props.get("drainage_area"))})


def parse_usgs_daily(body: Mapping[str, Any], url: str, *, origin: str, limit: int) -> tuple[list[dict], bool]:
    if body.get("type") != "FeatureCollection" or not isinstance(body.get("features"), list):
        raise WaterFormatError("schema_drift", "USGS daily response is not a FeatureCollection")
    records = []
    for feature in body["features"]:
        props = dict(feature.get("properties") or {})
        location, code = _text(props.get("monitoring_location_id")), _text(props.get("parameter_code"))
        time, stat = _text(props.get("time")), _text(props.get("statistic_id"))
        if location is None or code not in USGS_PARAMETERS or time is None or stat is None:
            raise WaterFormatError("schema_drift", "USGS daily value lacks location, parameter, statistic or time")
        value = _number(props.get("value"))
        if value is None:
            continue  # no value published: stays missing
        qualifiers = props.get("qualifier")
        qualifiers = [str(q) for q in (qualifiers if isinstance(qualifiers, list) else [qualifiers] if qualifiers
                                       else [])]
        records.append(statement(
            "observation", "usgs", observation_key(location, code, stat, time), subject_name=None,
            source=_source(url, "usgs", origin, feature_id=_text(feature.get("id"))),
            as_published={"station_id": location, "parameter": USGS_PARAMETERS[code], "parameter_code": code,
                          "statistic": stat, "time": time, "value": value,
                          "unit": _text(props.get("unit_of_measure")) or "unstated",
                          "quality": quality(props.get("approval_status"), provider="usgs"),
                          "qualifiers": qualifiers or None, "time_series_id": _text(props.get("time_series_id")),
                          "last_modified": _text(props.get("last_modified"))}))
    has_next = any(link.get("rel") == "next" for link in body.get("links") or [] if isinstance(link, Mapping))
    complete = not has_next and len(body["features"]) < limit
    return records, complete


def parse_wise_geometry(body: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(body.get("results"), list):
        raise WaterFormatError("schema_drift", "Discodata response lacks results")
    out: dict[str, dict[str, Any]] = {}
    for row in body["results"]:
        code, year = _text(row.get("euSurfaceWaterBodyCode")), _text(row.get("cYear"))
        raw = row.get("geometryGeoJSON")
        if code is None or year is None:
            raise WaterFormatError("schema_drift", "geometry row lacks code or cycle")
        geometry = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(geometry, Mapping) or geometry.get("type") not in {"Polygon", "MultiPolygon", "LineString",
                                                                              "MultiLineString"}:
            continue  # no usable published geometry: the water body stays without one
        if code not in out or year > out[code]["source"]["cycle"]:
            out[code] = {"type": geometry["type"], "coordinates": geometry["coordinates"],
                         "source": {"dataset": WISE_SPATIAL_TABLE, "cycle": year, "crs": "EPSG:4326"}}
    return out


def parse_wise_status(body: Mapping[str, Any], url: str, *, origin: str,
                      geometries: Mapping[str, Any]) -> list[dict]:
    if not isinstance(body.get("results"), list):
        raise WaterFormatError("schema_drift", "Discodata response lacks results")
    rows = sorted((dict(r) for r in body["results"]), key=lambda r: (str(r.get("euSurfaceWaterBodyCode")),
                                                                     str(r.get("cYear"))))
    records, latest = [], {}
    for row in rows:
        code, year = _text(row.get("euSurfaceWaterBodyCode")), _text(row.get("cYear"))
        if code is None or year is None or not re.fullmatch(r"\d{4}", year):
            raise WaterFormatError("schema_drift", "status row lacks code or reporting cycle")
        rbd = {"code": _text(row.get("euRBDCode")), "name": _text(row.get("rbdName"))} if row.get("euRBDCode") \
            else None
        eco_value, chem_value = _text(row.get("swEcologicalStatusOrPotentialValue")), _text(
            row.get("swChemicalStatusValue"))
        natural = _text(row.get("naturalAWBHMWB"))
        kind = "potential" if natural and natural.casefold() in {"heavily modified", "artificial", "hmwb", "awb"} \
            else "status"
        ecological = None if eco_value is None else {
            "value": eco_value, "label": WISE_ECOLOGICAL.get(eco_value, eco_value), "kind": kind,
            "assessment_year": _text(row.get("swEcologicalAssessmentYear"))}
        chemical = None if chem_value is None else {
            "value": chem_value, "label": WISE_CHEMICAL.get(chem_value, chem_value),
            "assessment_year": _text(row.get("swChemicalAssessmentYear"))}
        elements = [e for e in (
            {"element": f"ecological {kind}", "value": eco_value} if eco_value is not None else None,
            {"element": "chemical status", "value": chem_value} if chem_value is not None else None) if e]
        records.append(statement(
            "assessment", "eea-wise", f"{code}|{year}", subject_name=_text(row.get("surfaceWaterBodyName")),
            source=_source(url, "eea-wise", origin, reporting_cycle=year),
            as_published={"eu_code": code, "reporting_cycle": f"WFD reporting {year}", "cycle_year": year,
                          "water_body_name": _text(row.get("surfaceWaterBodyName")), "ecological": ecological,
                          "chemical": chemical, "elements": elements or None,
                          "country_code": _text(row.get("countryCode")), "river_basin_district": rbd,
                          "natural_status": natural}))
        latest[code] = (row, year, rbd)
    for code, (row, year, rbd) in sorted(latest.items()):
        category = WISE_CATEGORIES.get(str(row.get("surfaceWaterBodyCategory") or ""), "unknown")
        records.append(statement(
            "water_body", "eea-wise", code, subject_name=_text(row.get("surfaceWaterBodyName")),
            source=_source(url, "eea-wise", origin, reporting_cycle=year),
            as_published={"eu_code": code, "name": _text(row.get("surfaceWaterBodyName")) or code,
                          "category": category, "country_code": _text(row.get("countryCode")),
                          "river_basin_district": rbd, "reporting_cycle": year,
                          "geometry": geometries.get(code)},
            unknowns=[] if code in geometries else ["geometry (no published geometry in the selection)"]))
    return records


# ------------------------------------------------------------------ runtime adapter


class WaterSourceAdapter:
    """One page per selected station window, monitoring location, daily window or WISE selection."""

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
        self._secret = secret
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

    def _get(self, path: str, query: Mapping[str, str]) -> tuple[int, Any, str, str]:
        base = self.source["endpoint"].rstrip("/") + path
        url = base + ("?" + urlencode(sorted(query.items())) if query else "")
        headers = {"Accept": "application/json"}
        if self.provider == "usgs" and self._secret:
            headers["X-Api-Key"] = self._secret  # verify the header name for api.waterdata.usgs.gov
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
        if status == 404:
            return status, None, url, origin
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
            return status, json.loads(raw.decode("utf-8-sig")), url, origin
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourcePackError("schema_drift", "response is not UTF-8 JSON") from exc

    def _page(self, entry: Mapping[str, Any], carry: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {"statements": [], "dropped": [], "snapshots": [], "requests": 0, "origin": "live",
                                  "window": None, "unsupported": []}
        station = None
        for role, path, query in requests_for(self.provider, entry):
            _status, body, url, origin = self._get(path, query)
            result["requests"] += 1
            result["origin"] = origin
            result.setdefault("url", url)
            if body is None:
                if role in {"station", "location"}:
                    result["outcome"] = "not_found"
                    return result
                continue
            result["dropped"] += personal_hints(body, f"{role}:")
            if role == "station":
                station = parse_pegelonline_station(body, url, origin=origin)
                result["statements"].append(station)
            elif role.startswith("measurements:"):
                code = role.split(":", 1)[1]
                result["statements"] += parse_pegelonline_measurements(body, url, origin=origin, station=station,
                                                                       code=code)
                result["window"] = {"start": entry["start"], "end": entry["end"]}
                # PEGELONLINE keeps ~31 days: a value rolling off is not a removal, so windows are never compared.
                result["snapshots"].append({"selection_key": selection_key(self.provider, entry, f":{code}"),
                                            "provider": self.provider, "complete": False, "url": url,
                                            "prefix": f"{entry['uuid']}|{code}|instantaneous|"})
            elif role == "location":
                result["statements"].append(parse_usgs_location(body, url, origin=origin))
            elif role == "daily":
                records, complete = parse_usgs_daily(body, url, origin=origin, limit=int(entry["limit"]))
                result["statements"] += records
                result["window"] = {"time": entry["time"]}
                result["snapshots"].append({
                    "selection_key": selection_key(self.provider, entry), "provider": self.provider,
                    "complete": complete, "url": url,
                    "prefix": f"{entry['monitoring_location_id']}|{entry['parameter_code']}|{entry['statistic_id']}|"})
            elif role == "water_body_geometry":
                carry.setdefault("geometries", {}).update(parse_wise_geometry(body))
            else:
                result["statements"] += parse_wise_status(body, url, origin=origin,
                                                          geometries=dict(carry.get("geometries") or {}))
                reported = {s["as_published"]["eu_code"] for s in result["statements"]}
                result["unreported"] = sorted(set(entry["eu_codes"]) - reported)
        result["outcome"] = "found"
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
        except (WaterFormatError, WaterError, KeyError, TypeError, ValueError) as exc:
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
                "url": item["source"]["url"], "language": "und", "content": content, "water_record": item})
        label = {k: entry[k] for k in sorted(entry) if k in {"kind", "uuid", "number", "id", "monitoring_location_id",
                                                              "parameter_code", "statistic_id", "eu_codes", "cycles",
                                                              "label"}}
        receipt = {"status": 200, "provider": self.provider, "selection": label, "outcome": page["outcome"],
                   "statements": len(records), "requests": page["requests"], "window": page.get("window"),
                   "personal_fields_dropped": sorted(set(page["dropped"])), "evidence_origin": page["origin"],
                   "unreported_codes": page.get("unreported", []), "final_page": index + 1 >= len(self.entries),
                   "snapshots": page["snapshots"], "attribution": PROVIDER_CONTRACTS[self.provider]["attribution"],
                   "licence": PROVIDER_CONTRACTS[self.provider]["licence"],
                   "credential": ("configured" if self._secret else "not configured (anonymous rate limit)")
                   if self.provider == "usgs" else "none required",
                   "minimisation": MINIMISATION["decision"],
                   "live_verification": LIVE_VERIFICATION[self.provider]["status"]}
        next_cursor = (json.dumps({"i": index + 1, "scope": self.source["source_hash"], "carry": carry},
                                  sort_keys=True) if index + 1 < len(self.entries) else None)
        return RuntimePage(tuple(records), next_cursor, sum(len(r["content"]) for r in records), receipt=receipt)


FIXTURE_SECRET = None  # fixtures run without the optional USGS key (anonymous limits)
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
    "ADAPTERS", "BOUNDED_COVERAGE", "CONNECTOR", "FIXTURE_SECRET", "LIVE_VERIFICATION", "NOT_IMPLEMENTED",
    "PROVIDERS", "PROVIDER_CONTRACTS", "PROVIDER_HOSTS", "WaterFormatError", "WaterSourceAdapter",
    "fixture_transport", "parse_pegelonline_measurements", "parse_pegelonline_station", "parse_usgs_daily",
    "parse_usgs_location", "parse_wise_geometry", "parse_wise_status", "personal_hints", "replay_native_fixture",
    "requests_for", "selection_entries", "selection_key", "wise_sql",
]
