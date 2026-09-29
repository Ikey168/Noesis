"""Bounded acquisition of air-quality, grid, emissions and climate records (E01, E03-E07).

Every provider has a recorded access contract (:data:`PROVIDER_CONTRACTS`):
documented access path, terms, authentication, rate limits, pagination,
cadence, retained evidence, what it publishes (observation, model output or
forecast), its identifiers, units and coordinate reference system, and the
fallback when access is unavailable. Live state is kept separately in
:data:`LIVE_VERIFICATION`; nothing here is live-verified until a dated run
says so, and credentialed providers stay ``unverified-live`` until then.

A provider *selection* (a bounded, explicit set of stations, zones, facilities
or grid points) compiles to an ordered request :func:`plan`. The same plan and
the same fail-closed parsers serve two acquisition paths:

* the source-pack runtime, through the ``environment`` native connector
  (:class:`EnvironmentSourceAdapter`, cursor = plan position + small carry),
  whose ``noesis-environment-record-v1`` pages are projected by
  :class:`src.kb.environment_store.EnvironmentProjector`; and
* :class:`EnvironmentClient` over :class:`~src.ingestion.provider_execution.DurableHTTP`
  (explicit budget, exact hosts, durable replayable receipts) for explicit
  MCP acquisitions and the live check.

Parsers never invent values: a missing value stays ``None``, published
reason text is kept verbatim, a kind outside what the dataset publishes is
rejected, and a response whose shape does not match raises
``ProviderError('schema_drift')``. Berlin Umweltatlas layers are acquired by
the existing WFS 2.0.0 adapter (``src/ingestion/wfs_api.py``) through a
``geospatial-berlin`` source-pack upgrade, not by this module.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from src.ingestion.provider_execution import DurableHTTP, ProviderError, canonical, digest
from src.kb import environment_records as er

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_SCHEMA = "noesis-environment-record-v1"
FIXTURE_SECRET = "fixture-credential-not-a-real-key"
PROVIDER_HOSTS = {
    "openaq": {"api.openaq.org"},
    "uba": {"www.umweltbundesamt.de"},
    "entsoe": {"web-api.tp.entsoe.eu"},
    "smard": {"www.smard.de"},
    "eea-industry": {"discodata.eea.europa.eu"},
    "eu-ets": {"climate.ec.europa.eu"},
    "umweltatlas": {"gdi.berlin.de"},
    "open-meteo-archive": {"archive-api.open-meteo.com"},
    "open-meteo-forecast": {"api.open-meteo.com"},
    "dwd": {"opendata.dwd.de"},
    "copernicus-cams": {"ads.atmosphere.copernicus.eu"},
}
_UNAVAILABLE = "record the provider failure with its code; keep the last acquired revision and mark dependent views stale; never infer values"
PROVIDER_CONTRACTS = {
    "openaq": {
        "documentation": "https://docs.openaq.org/",
        "access": "REST API v3 (JSON): /v3/locations/{id}, /v3/sensors/{id}/hours",
        "authentication": "required API key sent as the X-API-Key header (secret ref NOESIS_OPENAQ_API_KEY)",
        "terms": "OpenAQ terms of use; data licences are per location (licenses[] in the location record) and are retained",
        "rate_limits": "per-key limits documented by OpenAQ (60/min, 2000/h at the time of the audit); budgets stay far below",
        "pagination": "page/limit; one bounded page per sensor, truncation recorded when a page is full",
        "cadence": "hourly at most",
        "retained_evidence": "raw JSON per request (digest, snapshot), location licences, JSON pointers",
        "publishes": {"observation": "aggregated hourly sensor values (/hours)"},
        "identifiers": "OpenAQ location id, sensor id; station names often carry the EEA station code",
        "units": "per parameter (e.g. µg/m³, ppm) as published",
        "crs": "EPSG:4326 coordinates{latitude, longitude}",
        "coverage": "explicitly selected locations and sensors only; OpenAQ re-publishes other networks (e.g. UBA/EEA)",
        "overlap": "stations also published by UBA are linked on the shared EEA station code and distance, never merged",
        "unavailable_fallback": _UNAVAILABLE,
    },
    "uba": {
        "documentation": "https://www.umweltbundesamt.de/daten/luft/luftdaten/doc",
        "access": "Luftdaten API v3 (JSON): stations/json, components/json, measures/json",
        "authentication": "none",
        "terms": "Umweltbundesamt data licence dl-de/by-2-0; attribution 'Umweltbundesamt' with source URL",
        "rate_limits": "none documented; one request per station/component and a small request budget",
        "pagination": "none; bounded by date range, station and component",
        "cadence": "hourly at most",
        "retained_evidence": "raw JSON per request, index arrays, JSON pointers",
        "publishes": {"observation": "station measurements; current-year data are provisional per UBA"},
        "identifiers": "numeric UBA station id and EEA station code (e.g. DEBE068)",
        "units": "from components/json (e.g. µg/m³)",
        "crs": "EPSG:4326 (station longitude/latitude)",
        "time": "measure timestamps are local time without offset; interpreted as CET (UTC+01:00) as documented for the API, recorded with that basis",
        "value_status": "declared by the source selection (data_status provisional|validated); never inferred",
        "coverage": "explicitly selected stations and components only",
        "unavailable_fallback": _UNAVAILABLE,
    },
    "entsoe": {
        "documentation": "https://transparencyplatform.zendesk.com/hc/en-us/articles/12845911031188-How-to-get-security-token",
        "access": "Transparency Platform RESTful API (XML): A75 generation per type, A65 load (A16 actual, A01 day-ahead), A80 generation-unit unavailability",
        "authentication": "required securityToken query parameter (secret ref NOESIS_ENTSOE_SECURITY_TOKEN); token by e-mail request to the platform",
        "terms": "ENTSO-E Transparency Platform terms and conditions; reuse with source attribution",
        "rate_limits": "400 requests/min per token documented; budgets stay far below",
        "pagination": "periodStart/periodEnd windows (max one year); unavailability documents are delivered as a ZIP of XML documents",
        "cadence": "hourly at most",
        "retained_evidence": "raw XML/ZIP per request, document mRID and revisionNumber",
        "publishes": {"observation": "realised generation and actual load (processType A16)",
                      "forecast": "day-ahead load forecast (processType A01), issue time = document createdDateTime"},
        "identifiers": "EIC bidding zone codes, production unit mRID, document mRID + revision",
        "units": "MAW (MW)",
        "crs": "none (areas by EIC code); zone geometry is an authored coarse outline, see config/environment/bidding_zones.json",
        "coverage": "explicitly selected bidding zone and time window",
        "unavailable_fallback": _UNAVAILABLE,
    },
    "smard": {
        "documentation": "https://www.smard.de/home/downloadcenter/download-marktdaten",
        "access": "SMARD chart_data JSON files: /app/chart_data/{filter}/{region}/{filter}_{region}_{resolution}_{timestamp}.json",
        "authentication": "none",
        "terms": "Bundesnetzagentur | SMARD.de, CC BY 4.0",
        "rate_limits": "none documented; one file per selection",
        "pagination": "weekly files addressed by timestamp (from index_{resolution}.json); the timestamp is pinned in the selection",
        "cadence": "hourly at most",
        "retained_evidence": "raw JSON per file, meta_data.created",
        "publishes": {"observation": "realised generation and consumption (e.g. filters 410, 4068)",
                      "forecast": "forecast consumption (filter 411)"},
        "identifiers": "filter id, region code, resolution, timestamp",
        "units": "MWh per interval",
        "crs": "none (region codes)",
        "filters": {"410": {"title": "Stromverbrauch: Gesamt (Netzlast)", "event_type": "load", "kind": "observation"},
                    "411": {"title": "Prognostizierter Stromverbrauch: Gesamt", "event_type": "load", "kind": "forecast"},
                    "4068": {"title": "Stromerzeugung: Photovoltaik", "event_type": "generation", "kind": "observation"},
                    "4067": {"title": "Stromerzeugung: Wind Onshore", "event_type": "generation", "kind": "observation"}},
        "coverage": "explicitly selected filter/region/resolution/timestamp; the filter table is the adapter's declared mapping",
        "unavailable_fallback": _UNAVAILABLE,
    },
    "eea-industry": {
        "documentation": "https://industry.eea.europa.eu/",
        "access": "EEA Discodata SQL endpoint (JSON) over the 'Industrial Reporting under IED and E-PRTR' publication; the query is pinned in the adapter",
        "authentication": "none",
        "terms": "EEA standard re-use policy (CC BY 4.0); cite the EEA dataset version",
        "rate_limits": "none documented; nrOfHits bounded",
        "pagination": "p/nrOfHits; one bounded page, truncation recorded",
        "cadence": "yearly publication; weekly checks at most",
        "retained_evidence": "raw JSON, pinned SQL text, dataset field names",
        "publishes": {"observation": "annual reported releases (method M/C/E kept as published)"},
        "identifiers": "facility INSPIRE id, ETS identifier where published",
        "units": "kg/year as published (totalPollutantQuantityKg)",
        "crs": "EPSG:4326 (Longitude/Latitude)",
        "coverage": "explicitly selected city/country and reporting year",
        "note": "Discodata table and column names follow the published dataset schema and are unverified until a dated live run",
        "unavailable_fallback": _UNAVAILABLE,
    },
    "eu-ets": {
        "documentation": "https://climate.ec.europa.eu/eu-action/eu-emissions-trading-system-eu-ets/union-registry_en",
        "access": "Union Registry verified-emissions/compliance export, read from an explicitly pinned download URL as delimited text (CSV/semicolon)",
        "authentication": "none",
        "terms": "European Commission reuse policy (Decision 2011/833/EU)",
        "rate_limits": "one file per selection",
        "pagination": "none (one annual file)",
        "cadence": "yearly publication (compliance data after 30 September); republications are new vintages",
        "retained_evidence": "raw file bytes, row numbers, published compliance codes",
        "publishes": {"observation": "verified emissions (tCO2e) and allocated allowances per installation and year"},
        "identifiers": "registry code + installation identifier, permit identifier",
        "units": "tCO2e for verified emissions; allowances (not converted)",
        "crs": "none published; installations are placed only through a published link (e.g. EEA ETSIdentifier)",
        "compliance": "compliance codes are recorded as published; no compliance determination is derived",
        "xlsx": "the Commission publishes XLSX workbooks; XLSX parsing is not implemented (not implemented: needs a reviewed converter)",
        "coverage": "explicitly selected installations",
        "unavailable_fallback": _UNAVAILABLE,
    },
    "umweltatlas": {
        "documentation": "https://fbinter.stadt-berlin.de/fb/index.jsp",
        "access": "WFS 2.0.0 GetFeature (GeoJSON, EPSG:25833) via src/ingestion/wfs_api.py as a geospatial-berlin source-pack upgrade",
        "authentication": "none",
        "terms": "dl-de/zero-2-0 (Geoportal Berlin)",
        "rate_limits": "none documented; WFS paging budgets",
        "pagination": "COUNT/STARTINDEX with pinned SORTBY",
        "cadence": "irregular (map editions)",
        "retained_evidence": "native WFS envelopes, feature revisions and snapshots",
        "publishes": {"features": "map layers (zones, noise bands, climate functions); modelled map products are labelled as such in the layer metadata"},
        "identifiers": "WFS feature id",
        "units": "per layer property as published",
        "crs": "EPSG:25833 projected to EPSG:4326 by the geospatial owner",
        "note": "service and type names follow the Geoportal naming scheme and must be confirmed by a live GetCapabilities run",
        "coverage": "selected layers; complete collection snapshots",
        "unavailable_fallback": _UNAVAILABLE,
    },
    "open-meteo-archive": {
        "documentation": "https://open-meteo.com/en/docs",
        "access": "Historical Weather API (JSON): archive-api.open-meteo.com/v1/archive",
        "authentication": "none (non-commercial use; commercial use requires an API subscription)",
        "terms": "Open-Meteo terms; data CC BY 4.0 with attribution; underlying ERA5 from Copernicus",
        "rate_limits": "10,000 calls/day non-commercial",
        "pagination": "none; bounded date range",
        "cadence": "daily at most",
        "retained_evidence": "raw JSON, requested model, returned grid-cell coordinates",
        "publishes": {"model": "reanalysis (ERA5 family) — never an observation"},
        "identifiers": "grid cell (returned latitude/longitude) + model",
        "units": "hourly_units as published (e.g. °C, mm)",
        "crs": "EPSG:4326; grid resolution as documented per model (ERA5 0.25°, ERA5-Land 0.1°)",
        "coverage": "explicitly selected coordinates, variables and dates",
        "unavailable_fallback": _UNAVAILABLE,
    },
    "open-meteo-forecast": {
        "documentation": "https://open-meteo.com/en/docs",
        "access": "Forecast API (JSON): api.open-meteo.com/v1/forecast with a pinned model, plus the model's meta.json for the run time",
        "authentication": "none (non-commercial use)",
        "terms": "Open-Meteo terms; CC BY 4.0; DWD ICON data",
        "rate_limits": "10,000 calls/day non-commercial",
        "pagination": "none; bounded forecast_days",
        "cadence": "per model run (ICON-D2: 3-hourly)",
        "retained_evidence": "raw JSON, meta.json run time",
        "publishes": {"forecast": "model forecasts with issue time from meta.json last_run_initialisation_time"},
        "identifiers": "grid cell + model",
        "units": "hourly_units as published",
        "crs": "EPSG:4326; ICON-D2 ~2.2 km as documented",
        "coverage": "explicitly selected coordinates, variables and model",
        "unavailable_fallback": _UNAVAILABLE,
    },
    "dwd": {
        "documentation": "https://opendata.dwd.de/",
        "access": "CDC open data files: climate/hourly/<parameter>/<recent|historical>/ station description and product ZIP",
        "authentication": "none",
        "terms": "DWD open data, CC BY 4.0 (GeoNutzV); cite 'Deutscher Wetterdienst'",
        "rate_limits": "none documented",
        "pagination": "none; one ZIP per station and period",
        "cadence": "daily (recent files are updated daily)",
        "retained_evidence": "raw ZIP bytes, product file name, per-value quality level QN",
        "publishes": {"observation": "station observations; 'recent' = not yet completely quality controlled (provisional), 'historical' = quality controlled (validated)"},
        "identifiers": "5-digit DWD station id",
        "units": "per parameter description (TT_TU °C, RF_TU %)",
        "crs": "EPSG:4326 (geoBreite/geoLaenge)",
        "time": "MESS_DATUM in UTC for hourly products",
        "coverage": "explicitly selected station, parameter and period",
        "unavailable_fallback": _UNAVAILABLE,
    },
    "copernicus-cams": {
        "documentation": "https://ads.atmosphere.copernicus.eu/",
        "access": "not implemented",
        "authentication": "Atmosphere Data Store account, personal API key and per-dataset licence acceptance",
        "terms": "Copernicus licence; per-dataset acceptance required",
        "status": "not implemented",
        "reason": ("CAMS requires an ADS account, a personal key and dataset licence acceptance that have not been "
                   "verified for this deployment; CAMS products are model analyses/forecasts and would enter as kind "
                   "'model'/'forecast' only. No scraping or keyless access is attempted."),
        "unavailable_fallback": "not acquired; dossiers list CAMS as not implemented",
    },
}
# Live state per provider, updated only from a dated bounded run
# (scripts/environment_live_check.py; reports under docs/development/environment-evidence/).
_LIVE_REPORT = "docs/development/environment-evidence/live-check-2026-09-27.json"
LIVE_VERIFICATION = {
    provider: {"status": "blocked", "checked_at": "2026-09-27", "report": _LIVE_REPORT,
               "failure_code": "provider_failed", "failure_type": "ConnectError",
               "note": "dated bounded run could not connect (build environment egress policy); coverage not validated; "
                       "offline fixtures only"}
    for provider in PROVIDER_CONTRACTS if provider != "copernicus-cams"
}
for _provider in ("openaq", "entsoe"):  # credentialed providers
    LIVE_VERIFICATION[_provider].update(
        credential="missing", note="no credential configured (credential_missing) and the keyless reachability probe "
                                   "could not connect; stays unverified until a dated run with a credential")
LIVE_VERIFICATION["copernicus-cams"] = {"status": "not implemented", "note": PROVIDER_CONTRACTS["copernicus-cams"]["reason"]}
SECRETS = {"openaq": ("header", "X-API-Key", "NOESIS_OPENAQ_API_KEY"),
           "entsoe": ("param", "securityToken", "NOESIS_ENTSOE_SECURITY_TOKEN")}
MAX_STEPS = 40
_EIC = re.compile(r"^[0-9A-Z-]{16}$")
_ENTSOE_DOCUMENTS = {
    "generation": {"documentType": "A75", "processType": "A16", "zone": "in_Domain", "event_type": "generation", "kind": "observation"},
    "load": {"documentType": "A65", "processType": "A16", "zone": "outBiddingZone_Domain", "event_type": "load", "kind": "observation"},
    "load-forecast": {"documentType": "A65", "processType": "A01", "zone": "outBiddingZone_Domain", "event_type": "load", "kind": "forecast"},
    "unavailability": {"documentType": "A80", "zone": "biddingZone_Domain", "event_type": "unavailability", "kind": "observation"},
}
_PSR = {"B01": "Biomass", "B04": "Fossil Gas", "B02": "Fossil Brown coal/Lignite", "B05": "Fossil Hard coal",
        "B06": "Fossil Oil", "B14": "Nuclear", "B16": "Solar", "B18": "Wind Offshore", "B19": "Wind Onshore",
        "B10": "Hydro Pumped Storage", "B11": "Hydro Run-of-river and poundage", "B20": "Other"}
_BUSINESS = {"A53": "planned", "A54": "unplanned"}
OPEN_METEO_MODELS = {
    "era5": {"name": "ERA5", "dataset": "Copernicus ERA5 reanalysis (via Open-Meteo Historical Weather API)",
             "grid_resolution_m": 25000, "grid_resolution_basis": "0.25° as documented by Open-Meteo"},
    "era5_land": {"name": "ERA5-Land", "dataset": "Copernicus ERA5-Land reanalysis (via Open-Meteo)",
                  "grid_resolution_m": 9000, "grid_resolution_basis": "0.1° as documented by Open-Meteo"},
    "icon_d2": {"name": "DWD ICON-D2", "dataset": "DWD ICON-D2 (via Open-Meteo Forecast API)", "meta": "dwd_icon_d2",
                "grid_resolution_m": 2200, "grid_resolution_basis": "~2.2 km as documented by Open-Meteo"},
}
DWD_PARAMETERS = {"air_temperature": {"code": "TU", "columns": {"TT_TU": ("air temperature 2 m", "°C"),
                                                                "RF_TU": ("relative humidity", "%")}}}


# --------------------------------------------------------------------- helpers


def _json(content):
    """Decode JSON keeping every number as exact text (no float rounding)."""

    try:
        return json.loads(content, parse_float=str, parse_int=str)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProviderError("schema_drift", "provider response is not valid JSON") from exc


def _num(value, field):
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "null", "NaN"}:
        return None
    try:
        return er.decimal_text(text, field)
    except er.EnvironmentRecordError as exc:
        raise ProviderError("schema_drift", f"{field} is not numeric") from exc


def _float(value, field):
    try:
        return float(str(value))
    except (TypeError, ValueError) as exc:
        raise ProviderError("schema_drift", f"{field} is not a coordinate") from exc


def _utc(value):
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _duration(text):
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?", text or "")
    if not match or not any(match.groups()):
        raise ProviderError("schema_drift", f"unsupported resolution {text!r}")
    return timedelta(hours=int(match.group(1) or 0), minutes=int(match.group(2) or 0))


def _record(record):
    """Validate an adapter record through the E02 constructors (fail closed)."""

    try:
        return er.validate(record)
    except er.EnvironmentRecordError as exc:
        raise ProviderError("schema_drift", f"record failed validation: {exc}") from exc


def _build(factory, *args, **kwargs):
    try:
        return factory(*args, **kwargs)
    except er.EnvironmentRecordError as exc:
        raise ProviderError(exc.code if exc.code.startswith("kind") else "schema_drift", str(exc)) from exc


# ------------------------------------------------------------------ selections


def _bounded_list(values, field, limit):
    if not isinstance(values, list) or not 1 <= len(values) <= limit:
        raise ProviderError("unbounded_selection", f"{field} must list 1-{limit} explicit items")
    return [str(v) for v in values]


def plan(provider, selection):
    """Ordered, bounded request steps for one explicit selection."""

    if provider not in PROVIDER_CONTRACTS or provider in {"umweltatlas", "copernicus-cams"}:
        raise ProviderError("not_implemented", f"{provider} has no environment adapter (see PROVIDER_CONTRACTS)")
    selection = dict(selection or {})
    steps = []
    if provider == "openaq":
        for location in _bounded_list(selection.get("locations"), "locations", 10):
            steps.append({"parse": "openaq_location", "url": f"https://api.openaq.org/v3/locations/{int(location)}",
                          "params": {}})
        for sensor in _bounded_list(selection.get("sensors"), "sensors", 20):
            steps.append({"parse": "openaq_hours", "url": f"https://api.openaq.org/v3/sensors/{int(sensor)}/hours",
                          "params": {"datetime_from": selection["datetime_from"], "datetime_to": selection["datetime_to"],
                                     "limit": str(int(selection.get("limit", 100))), "page": "1"},
                          "context": {"sensor": str(int(sensor))}})
    elif provider == "uba":
        base = "https://www.umweltbundesamt.de/api/air_data/v3"
        if selection.get("data_status") not in {"provisional", "validated"}:
            raise ProviderError("unbounded_selection", "UBA selections declare data_status provisional or validated")
        stations = _bounded_list(selection.get("stations"), "stations", 10)
        components = _bounded_list(selection.get("components"), "components", 6)
        window = {"date_from": selection["date_from"], "time_from": str(selection.get("time_from", 1)),
                  "date_to": selection["date_to"], "time_to": str(selection.get("time_to", 24))}
        steps.append({"parse": "uba_stations", "url": base + "/stations/json",
                      "params": {"use": "airquality", "lang": "en", "date_from": window["date_from"],
                                 "date_to": window["date_to"]}, "context": {"stations": stations}})
        steps.append({"parse": "uba_components", "url": base + "/components/json", "params": {"lang": "en", "index": "id"}})
        for station in stations:
            for component in components:
                steps.append({"parse": "uba_measures", "url": base + "/measures/json",
                              "params": {**window, "station": station, "component": component,
                                         "scope": str(selection.get("scope", 2))},
                              "context": {"station": station, "component": component,
                                          "data_status": selection["data_status"]}})
    elif provider == "entsoe":
        zone = str(selection.get("bidding_zone") or "")
        if not _EIC.fullmatch(zone):
            raise ProviderError("unbounded_selection", "ENTSO-E selections name one EIC bidding zone")
        for document in _bounded_list(selection.get("documents"), "documents", 4):
            spec = _ENTSOE_DOCUMENTS.get(document)
            if spec is None:
                raise ProviderError("unbounded_selection", f"unsupported ENTSO-E document {document}")
            params = {"documentType": spec["documentType"], spec["zone"]: zone,
                      "periodStart": selection["period_start"], "periodEnd": selection["period_end"]}
            if "processType" in spec:
                params["processType"] = spec["processType"]
            steps.append({"parse": "entsoe", "url": "https://web-api.tp.entsoe.eu/api", "params": params,
                          "context": {"document": document, "zone": zone, "zone_name": selection.get("zone_name")}})
    elif provider == "smard":
        filters = PROVIDER_CONTRACTS["smard"]["filters"]
        for item in selection.get("files") or []:
            if str(item.get("filter")) not in filters or item.get("resolution") not in {"quarterhour", "hour", "day"}:
                raise ProviderError("unbounded_selection", "SMARD files need a declared filter and resolution")
            f, region, res, ts = str(item["filter"]), str(item["region"]), item["resolution"], int(item["timestamp"])
            steps.append({"parse": "smard", "url": f"https://www.smard.de/app/chart_data/{f}/{region}/{f}_{region}_{res}_{ts}.json",
                          "params": {}, "context": {"filter": f, "region": region, "resolution": res, "timestamp": ts,
                                                    "zone": selection.get("bidding_zone")}})
        if not steps or len(steps) > 8:
            raise ProviderError("unbounded_selection", "SMARD selections pin 1-8 files")
    elif provider == "eea-industry":
        city, country, year = str(selection.get("city") or ""), str(selection.get("country") or ""), int(selection["reporting_year"])
        if not re.fullmatch(r"[A-Z]{2}", country) or not re.fullmatch(r"[\w .\-äöüÄÖÜß]{2,60}", city):
            raise ProviderError("unbounded_selection", "EEA selections name one country code and city")
        steps.append({"parse": "eea_industry", "url": "https://discodata.eea.europa.eu/sql",
                      "params": {"query": eea_query(country, city, year), "p": "1", "nrOfHits": str(int(selection.get("limit", 500)))},
                      "context": {"country": country, "city": city, "year": year}})
    elif provider == "eu-ets":
        url = str(selection.get("export_url") or "")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in PROVIDER_HOSTS["eu-ets"]:
            raise ProviderError("unbounded_selection", "EU ETS selections pin an official export URL")
        steps.append({"parse": "eu_ets", "url": url, "params": {},
                      "context": {"installations": _bounded_list(selection.get("installations"), "installations", 50),
                                  "year": int(selection["year"]), "released_at": selection.get("released_at")}})
    elif provider in {"open-meteo-archive", "open-meteo-forecast"}:
        model = str(selection.get("model") or "")
        if model not in OPEN_METEO_MODELS or (provider == "open-meteo-forecast") != ("meta" in OPEN_METEO_MODELS[model]):
            raise ProviderError("unbounded_selection", "Open-Meteo selections pin a declared model for their endpoint")
        variables = _bounded_list(selection.get("hourly"), "hourly", 6)
        params = {"latitude": str(selection["latitude"]), "longitude": str(selection["longitude"]),
                  "hourly": ",".join(variables), "models": model, "timezone": "GMT"}
        context = {"model": model, "variables": variables}
        if provider == "open-meteo-archive":
            params.update(start_date=selection["start_date"], end_date=selection["end_date"])
            steps.append({"parse": "open_meteo", "url": "https://archive-api.open-meteo.com/v1/archive",
                          "params": params, "context": context})
        else:
            params.update(forecast_days=str(int(selection.get("forecast_days", 1))))
            steps.append({"parse": "open_meteo_meta", "params": {}, "context": context,
                          "url": f"https://api.open-meteo.com/data/{OPEN_METEO_MODELS[model]['meta']}/static/meta.json"})
            steps.append({"parse": "open_meteo", "url": "https://api.open-meteo.com/v1/forecast",
                          "params": params, "context": context})
    elif provider == "dwd":
        parameter = DWD_PARAMETERS.get(str(selection.get("parameter")))
        station, period = str(selection.get("station") or ""), selection.get("period")
        if parameter is None or not re.fullmatch(r"\d{5}", station) or period not in {"recent", "historical"}:
            raise ProviderError("unbounded_selection", "DWD selections pin a parameter, 5-digit station and period")
        base = f"https://opendata.dwd.de/climate_environment/CDC/observations_germany/climate/hourly/{selection['parameter']}/{period}/"
        code = parameter["code"]
        steps.append({"parse": "dwd_stations", "url": base + f"{code}_Stundenwerte_Beschreibung_Stationen.txt",
                      "params": {}, "context": {"station": station}})
        name = (f"stundenwerte_{code}_{station}_akt.zip" if period == "recent"
                else str(selection.get("historical_file") or ""))
        if not re.fullmatch(rf"stundenwerte_{code}_{station}_[\w]+\.zip", name):
            raise ProviderError("unbounded_selection", "historical DWD selections pin the product file name")
        steps.append({"parse": "dwd_product", "url": base + name, "params": {},
                      "context": {"station": station, "parameter": selection["parameter"], "period": period}})
    if not steps or len(steps) > MAX_STEPS:
        raise ProviderError("unbounded_selection", "selection compiles to no or too many requests")
    return steps


def eea_query(country, city, year):
    """The pinned Discodata SQL (one joined facility/release table read)."""

    return ("SELECT f.FacilityInspireId, f.facilityName, f.parentCompanyName, f.city, f.countryCode, f.Longitude, "
            "f.Latitude, f.EPRTRAnnexIMainActivityCode, f.EPRTRAnnexIMainActivityLabel, f.ETSIdentifier, "
            "f.permitURL, f.dateOfGranting, f.permitAuthority, r.reportingYear, r.pollutant, r.medium, "
            "r.totalPollutantQuantityKg, r.AccidentalPollutantQuantityKG, r.methodCode "
            "FROM [IED].[latest].[Facility] f LEFT JOIN [IED].[latest].[PollutantRelease] r "
            "ON r.FacilityInspireId = f.FacilityInspireId AND r.reportingYear = " + str(int(year)) + " "
            f"WHERE f.countryCode = '{country}' AND f.city = '{city}' ORDER BY f.FacilityInspireId, r.pollutant, r.medium")


# --------------------------------------------------------------------- parsers


def parse_openaq_location(content, context, carry, *, url):
    payload = _json(content)
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or len(results) != 1:
        raise ProviderError("schema_drift", "OpenAQ location response needs exactly one result")
    item = results[0]
    coords = item.get("coordinates") or {}
    if "id" not in item or "latitude" not in coords or "longitude" not in coords:
        raise ProviderError("schema_drift", "OpenAQ location lacks id or coordinates")
    location_id = str(item["id"])
    sensors = dict(carry.get("sensors") or {})
    for sensor in item.get("sensors") or []:
        parameter = sensor.get("parameter") or {}
        sensors[str(sensor["id"])] = {"location": location_id, "parameter": parameter.get("name"),
                                      "units": parameter.get("units"), "display": parameter.get("displayName")}
    name = str(item.get("name") or location_id)
    codes = sorted(set(re.findall(r"\b[A-Z]{2}[A-Z]{2}\d{3}\b", name)))
    record = _build(er.station, "openaq", location_id, name, source_url=f"https://explore.openaq.org/locations/{location_id}",
                    geometry={"type": "Point", "coordinates": [_float(coords["longitude"], "longitude"),
                                                               _float(coords["latitude"], "latitude")]},
                    identifiers={"openaq_location_id": location_id, **({"eea_station_code": codes[0]} if codes else {})},
                    network=(item.get("provider") or {}).get("name"),
                    active_from=((item.get("datetimeFirst") or {}).get("utc")),
                    active_to=None,
                    properties={"locality": item.get("locality"), "owner": (item.get("owner") or {}).get("name"),
                                "is_monitor": item.get("isMonitor"), "is_mobile": item.get("isMobile"),
                                "licenses": [lic.get("name") for lic in item.get("licenses") or []],
                                "json_pointer": "/results/0"})
    return [record], {**carry, "sensors": sensors}


def _openaq_interval(text):
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})", str(text or ""))
    if not match:
        return None
    hours, minutes, seconds = (int(v) for v in match.groups())
    return f"PT{hours}H" if not minutes and not seconds else f"PT{hours}H{minutes}M{seconds}S"


def parse_openaq_hours(content, context, carry, *, url):
    payload = _json(content)
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise ProviderError("schema_drift", "OpenAQ hours response has no results list")
    sensor = context["sensor"]
    meta = (carry.get("sensors") or {}).get(sensor)
    if meta is None:
        raise ProviderError("schema_drift", "sensor is not listed by any selected location")
    values, unit, interval, parameter = [], None, None, meta.get("parameter")
    for index, row in enumerate(results):
        period = row.get("period") or {}
        start = ((period.get("datetimeFrom") or {}).get("utc"))
        if not start:
            raise ProviderError("schema_drift", f"result {index} has no period start")
        param = row.get("parameter") or {}
        unit = unit or param.get("units")
        parameter = parameter or param.get("name")
        interval = interval or _openaq_interval(period.get("interval"))
        values.append({"start": start, "end": (period.get("datetimeTo") or {}).get("utc"),
                       "value": _num(row.get("value"), "value"), "status": "unknown",
                       "flags": {"has_flags": (row.get("flagInfo") or {}).get("hasFlags"),
                                 "coverage_percent": _num((row.get("coverage") or {}).get("percentComplete"), "coverage"),
                                 "json_pointer": f"/results/{index}"}})
    if not values:
        return [], carry
    limit = int(dict(context.get("params") or {}).get("limit") or 0)
    record = _build(er.series, "openaq", f"sensor:{sensor}", f"OpenAQ {parameter} at location {meta['location']}",
                    source_url=f"https://explore.openaq.org/locations/{meta['location']}",
                    location={"kind": "station", "ref": f"openaq:{meta['location']}"},
                    indicator={"code": parameter, "name": meta.get("display") or parameter, "scheme": "openaq-parameter"},
                    unit=unit or meta.get("units"), interval=interval, aggregation="mean", kind="observation",
                    values=values, status_basis="OpenAQ publishes no validation flag; status unknown",
                    release={"basis": "acquisition", "page_full": bool(limit and len(values) >= limit)})
    return [record], carry


def parse_uba_stations(content, context, carry, *, url):
    payload = _json(content)
    indices, data = payload.get("indices"), payload.get("data")
    if not isinstance(indices, list) or not isinstance(data, dict):
        raise ProviderError("schema_drift", "UBA stations response lacks indices/data")
    position = {name: i for i, name in enumerate(indices)}
    needed = ("station id", "station code", "station name", "station longitude", "station latitude")
    if any(name not in position for name in needed):
        raise ProviderError("schema_drift", "UBA station indices changed")
    records = []
    for station_id in context["stations"]:
        row = data.get(station_id)
        if row is None:
            continue  # absence is recorded as coverage, never as a closed station

        def field(name, row=row):
            index = position.get(name)
            return None if index is None or index >= len(row) else row[index]

        records.append(_build(
            er.station, "uba", str(station_id), str(field("station name")),
            source_url=f"https://www.umweltbundesamt.de/en/data/air/air-data/stations/eJxrWpScv9QoNT1RIRFaNU0ElVBVlp-X3_QJCB6y-/{station_id}",
            geometry={"type": "Point", "coordinates": [_float(field("station longitude"), "longitude"),
                                                       _float(field("station latitude"), "latitude")]},
            identifiers={"uba_station_id": str(station_id), "eea_station_code": str(field("station code"))},
            network=field("network name"), active_from=field("station active from"), active_to=field("station active to"),
            properties={"setting": field("station setting name"), "type": field("station type name"),
                        "city": field("station city"), "json_pointer": f"/data/{station_id}"}))
    missing = sorted(set(context["stations"]) - {r["native_id"] for r in records})
    return records, {**carry, "uba_missing_stations": missing}


def parse_uba_components(content, context, carry, *, url):
    payload = _json(content)
    if not isinstance(payload, dict):
        raise ProviderError("schema_drift", "UBA components response is not an object")
    components = {}
    for key, row in payload.items():
        if key in {"count", "indices"} or not isinstance(row, list) or len(row) < 5:
            continue
        components[str(row[0])] = {"code": row[1], "symbol": row[2], "unit": row[3], "name": row[4]}
    if not components:
        raise ProviderError("schema_drift", "UBA components response lists no components")
    return [], {**carry, "uba_components": components}


def _uba_time(text):
    # UBA API timestamps: "YYYY-MM-DD HH:MM:SS" local standard time (CET), "24:00:00" meaning end of day.
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})", str(text))
    if not match:
        raise ProviderError("schema_drift", f"unexpected UBA timestamp {text!r}")
    day = datetime.fromisoformat(match.group(1)).replace(tzinfo=timezone(timedelta(hours=1)))
    return _utc(day + timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))


def parse_uba_measures(content, context, carry, *, url):
    payload = _json(content)
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise ProviderError("schema_drift", "UBA measures response lacks data")
    station, component = context["station"], context["component"]
    meta = (carry.get("uba_components") or {}).get(component)
    if meta is None:
        raise ProviderError("schema_drift", "component is not listed by components/json")
    rows = data.get(station) or {}
    values = []
    for start, row in sorted(rows.items()):
        if not isinstance(row, list) or len(row) < 4 or str(row[0]) != component:
            raise ProviderError("schema_drift", "UBA measure row does not match the requested component")
        values.append({"start": _uba_time(start), "end": _uba_time(row[3]), "value": _num(row[2], "value"),
                       "status": context["data_status"],
                       "flags": {"scope": str(row[1]), "index": row[4] if len(row) > 4 else None,
                                 "json_pointer": f"/data/{station}/{start}"}})
    if not values:
        return [], carry
    scope = str(values[0]["flags"]["scope"])
    record = _build(er.series, "uba", f"{station}:{component}:{scope}", f"UBA {meta['code']} at station {station}",
                    source_url="https://www.umweltbundesamt.de/en/data/air/air-data",
                    location={"kind": "station", "ref": f"uba:{station}"},
                    indicator={"code": meta["code"], "name": meta["name"], "scheme": "uba-component", "id": component},
                    unit=meta["unit"], interval="PT1H" if scope == "2" else "P1D" if scope in {"1", "6"} else None,
                    aggregation="mean" if scope in {"1", "2"} else "unknown", kind="observation", values=values,
                    status_basis=("declared by the source selection (data_status=" + context["data_status"]
                                  + "); UBA marks current-year data as provisional"),
                    release={"basis": "acquisition", "timezone_basis": "CET (UTC+01:00) as documented for the API"})
    return [record], carry


_NS = re.compile(r"^\{[^}]*\}")


def _strip(element):
    for node in element.iter():
        node.tag = _NS.sub("", node.tag)
    return element


def _text_at(node, path):
    found = node.find(path)
    return None if found is None or found.text is None else found.text.strip()


def _entsoe_documents(content):
    if content[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = sorted(n for n in archive.namelist() if n.endswith(".xml"))
            if len(names) > 500:
                raise ProviderError("input_limit", "too many documents in ENTSO-E archive")
            return [(name, archive.read(name)) for name in names]
    return [("document.xml", content)]


def _instant_z(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def _points(period, *, resolution, curve=None):
    """Period points; curve A03 (variable blocks) holds a value until the next point or the interval end."""

    start = _instant_z(_text_at(period, "timeInterval/start"))
    end = _instant_z(_text_at(period, "timeInterval/end"))
    step = _duration(resolution)
    rows = [(int(_text_at(p, "position")), _text_at(p, "quantity")) for p in period.findall("Point")]
    points = []
    for index, (position, quantity) in enumerate(rows):
        begin = start + step * (position - 1)
        if curve == "A03":
            finish = start + step * (rows[index + 1][0] - 1) if index + 1 < len(rows) else end
        else:
            finish = begin + step
        points.append({"start": _utc(begin), "end": _utc(finish), "value": _num(quantity, "quantity"),
                       "status": "unknown", "flags": {"position": position, "curve_type": curve}})
    return points


def parse_entsoe(content, context, carry, *, url):
    records = []
    zone = context["zone"]
    spec = _ENTSOE_DOCUMENTS[context["document"]]
    for name, raw in _entsoe_documents(content):
        try:
            root = _strip(ET.fromstring(raw))
        except ET.ParseError as exc:
            raise ProviderError("schema_drift", "ENTSO-E response is not XML") from exc
        if root.tag == "Acknowledgement_MarketDocument":
            code, text = _text_at(root, "Reason/code"), _text_at(root, "Reason/text")
            if code == "999":
                return [], {**carry, "entsoe_no_data": [*carry.get("entsoe_no_data", []), context["document"]]}
            raise ProviderError("provider_rejected", f"ENTSO-E acknowledgement {code}: {(text or '')[:120]}")
        document = {"mrid": _text_at(root, "mRID"), "revision": _text_at(root, "revisionNumber"),
                    "type": _text_at(root, "type"), "created": _text_at(root, "createdDateTime"),
                    "process_type": _text_at(root, "process.processType"), "doc_status": _text_at(root, "docStatus/value"),
                    "file": name}
        if not document["mrid"]:
            raise ProviderError("schema_drift", "ENTSO-E document has no mRID")
        for index, series in enumerate(root.findall("TimeSeries")):
            unit = {"MAW": "MW"}.get(_text_at(series, "quantity_Measure_Unit.name") or "", _text_at(series, "quantity_Measure_Unit.name"))
            if unit is None:
                raise ProviderError("schema_drift", "ENTSO-E time series has no unit")
            psr = _text_at(series, "MktPSRType/psrType") or _text_at(series, "production_RegisteredResource.pSRType.psrType")
            production = None if psr is None else {"code": psr, "label": _PSR.get(psr)}
            ts_id = _text_at(series, "mRID") or str(index + 1)
            if spec["event_type"] == "unavailability":
                business = _text_at(series, "businessType")
                start = f"{_text_at(series, 'start_DateAndOrTime.date')}T{(_text_at(series, 'start_DateAndOrTime.time') or '00:00:00Z')}"
                end_date = _text_at(series, "end_DateAndOrTime.date")
                end = None if end_date is None else f"{end_date}T{(_text_at(series, 'end_DateAndOrTime.time') or '00:00:00Z')}"
                periods = series.findall("Available_Period")
                points = [p for period in periods for p in _points(period, resolution=_text_at(period, "resolution"),
                                                                   curve=_text_at(series, "curveType"))]
                resource = {"mrid": _text_at(series, "production_RegisteredResource.mRID"),
                            "name": _text_at(series, "production_RegisteredResource.name"),
                            "location": _text_at(series, "production_RegisteredResource.location.name"),
                            "unit_mrid": _text_at(series, "production_RegisteredResource.pSRType.powerSystemResources.mRID"),
                            "unit_name": _text_at(series, "production_RegisteredResource.pSRType.powerSystemResources.name")}
                reasons = [{"code": _text_at(r, "code"), "text": _text_at(r, "text")}
                           for r in [*root.findall("Reason"), *series.findall("Reason")]]
                records.append(_build(
                    er.grid_event, "entsoe", f"{document['mrid']}:{ts_id}",
                    f"Unavailability of {resource['unit_name'] or resource['name'] or resource['mrid']}",
                    source_url="https://transparency.entsoe.eu/outage-domain/r2/unavailabilityOfProductionAndGenerationUnits/show",
                    event_type="unavailability", bidding_zone={"code": zone, "name": context.get("zone_name")},
                    kind="observation", resolution=_text_at(periods[0], "resolution") if periods else None,
                    production_type=production, unit=unit, points=points,
                    unavailability={"kind": _BUSINESS.get(business, "unknown"), "business_type": business,
                                    "resource": resource,
                                    "nominal_capacity": _num(_text_at(series, "production_RegisteredResource.pSRType.powerSystemResources.nominalP"), "nominalP"),
                                    "start": start, "end": end, "reason": reasons,
                                    "status": {"A05": "active", "A09": "cancelled", "A13": "withdrawn"}.get(document["doc_status"] or "", document["doc_status"])},
                    document=document))
                continue
            points = [p for period in series.findall("Period")
                      for p in _points(period, resolution=_text_at(period, "resolution"), curve=_text_at(series, "curveType"))]
            resolution = _text_at(series, "Period/resolution")
            label = "Actual generation" if spec["event_type"] == "generation" else "Actual total load" if spec["kind"] == "observation" else "Day-ahead total load forecast"
            records.append(_build(
                er.grid_event, "entsoe", f"{document['mrid']}:{ts_id}:{psr or spec['event_type']}",
                f"{label}{' – ' + production['label'] if production and production['label'] else ''} ({zone})",
                source_url="https://transparency.entsoe.eu/",
                event_type=spec["event_type"], bidding_zone={"code": zone, "name": context.get("zone_name")},
                kind=spec["kind"], resolution=resolution, production_type=production, unit=unit, points=points,
                document={**document, "issue_time": document["created"] if spec["kind"] == "forecast" else None}))
    return records, carry


def parse_smard(content, context, carry, *, url):
    payload = _json(content)
    rows = payload.get("series") if isinstance(payload, dict) else None
    meta = payload.get("meta_data") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not isinstance(meta, dict):
        raise ProviderError("schema_drift", "SMARD file lacks series/meta_data")
    spec = PROVIDER_CONTRACTS["smard"]["filters"][context["filter"]]
    step = {"quarterhour": timedelta(minutes=15), "hour": timedelta(hours=1), "day": timedelta(days=1)}[context["resolution"]]
    points = []
    for index, row in enumerate(rows):
        if not isinstance(row, list) or len(row) != 2:
            raise ProviderError("schema_drift", "SMARD series rows are [timestamp, value]")
        begin = datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC)
        points.append({"start": _utc(begin), "end": _utc(begin + step), "value": _num(row[1], "value"),
                       "status": "unknown", "flags": {"json_pointer": f"/series/{index}"}})
    created = meta.get("created")
    released = None if created is None else _utc(datetime.fromtimestamp(int(created) / 1000, tz=UTC))
    record = _build(er.grid_event, "smard", f"{context['filter']}:{context['region']}:{context['resolution']}:{context['timestamp']}",
                    f"SMARD {spec['title']} ({context['region']}, {context['resolution']})",
                    source_url="https://www.smard.de/home/downloadcenter/download-marktdaten",
                    event_type=spec["event_type"], bidding_zone={"code": context["region"], "within": context.get("zone")},
                    kind=spec["kind"], resolution={"quarterhour": "PT15M", "hour": "PT1H", "day": "P1D"}[context["resolution"]],
                    production_type=None, unit="MWh", points=points,
                    document={"filter": context["filter"], "created": released, "meta_version": meta.get("version"),
                              "issue_time": released if spec["kind"] == "forecast" else None})
    return [record], carry


def parse_eea_industry(content, context, carry, *, url):
    payload = _json(content)
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ProviderError("schema_drift", "Discodata response has no results list")
    required = {"FacilityInspireId", "facilityName", "Longitude", "Latitude"}
    facilities: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or required - set(row):
            raise ProviderError("schema_drift", f"Discodata row {index} lacks facility fields")
        item = facilities.setdefault(row["FacilityInspireId"], {"row": row, "releases": [], "index": index})
        if row.get("pollutant"):
            item["releases"].append({
                "year": int(row["reportingYear"]), "pollutant": row["pollutant"], "medium": row.get("medium"),
                "value": _num(row.get("totalPollutantQuantityKg"), "totalPollutantQuantityKg"), "unit": "kg",
                "method": row.get("methodCode"),
                "accidental": _num(row.get("AccidentalPollutantQuantityKG"), "AccidentalPollutantQuantityKG"),
                "kind": "observation"})
    records = []
    for inspire_id, item in sorted(facilities.items()):
        row = item["row"]
        permits = [{"permit_id": None, "url": row.get("permitURL"), "issued": row.get("dateOfGranting"),
                    "authority": row.get("permitAuthority"), "text": None, "status": "as published"}] if row.get("permitURL") else []
        records.append(_build(
            er.facility, "eea-industry", inspire_id, row["facilityName"],
            source_url="https://industry.eea.europa.eu/",
            geometry={"type": "Point", "coordinates": [_float(row["Longitude"], "Longitude"), _float(row["Latitude"], "Latitude")]},
            operator={"name": row.get("parentCompanyName"), "identifiers": {}},
            activities=[{"scheme": "E-PRTR Annex I", "code": row.get("EPRTRAnnexIMainActivityCode"),
                         "label": row.get("EPRTRAnnexIMainActivityLabel")}] if row.get("EPRTRAnnexIMainActivityCode") else [],
            permits=permits, releases=item["releases"],
            identifiers={"inspire_id": inspire_id, **({"ets_identifier": row["ETSIdentifier"]} if row.get("ETSIdentifier") else {})},
            reporting_year=context["year"], address=f"{row.get('city')}, {row.get('countryCode')}",
            unknowns=[] if item["releases"] else ["releases"]))
    return records, {**carry, "eea_truncated": len(rows) >= int(dict(context.get("params") or {}).get("nrOfHits") or 10**9)}


def parse_eu_ets(content, context, carry, *, url):
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ProviderError("schema_drift", "ETS export is not UTF-8 text") from exc
    reader = csv.DictReader(io.StringIO(text), delimiter=";" if text.count(";") > text.count(",") else ",")
    year = context["year"]
    needed = {"REGISTRY_CODE", "INSTALLATION_IDENTIFIER", "INSTALLATION_NAME", "ACCOUNT_HOLDER_NAME",
              f"VERIFIED_EMISSIONS_{year}", f"ALLOCATION_{year}", f"COMPLIANCE_CODE_{year}"}
    if not reader.fieldnames or needed - set(reader.fieldnames):
        raise ProviderError("schema_drift", "ETS export columns changed")
    wanted = set(context["installations"])
    records = []
    released = context.get("released_at")
    for line, row in enumerate(reader, start=2):
        native = f"{row['REGISTRY_CODE']}_{row['INSTALLATION_IDENTIFIER']}"
        if native not in wanted:
            continue
        facility = _build(
            er.facility, "eu-ets", native, row["INSTALLATION_NAME"], source_url=url, geometry=None,
            operator={"name": row.get("ACCOUNT_HOLDER_NAME") or None,
                      "identifiers": {k: row[k] for k in ("COMPANY_REGISTRATION_NUMBER",) if row.get(k)}},
            activities=[{"scheme": "EU ETS Annex I activity", "code": row.get("MAIN_ACTIVITY_TYPE_CODE")}]
            if row.get("MAIN_ACTIVITY_TYPE_CODE") else [],
            permits=[{"permit_id": row["PERMIT_IDENTIFIER"], "url": None, "issued": None, "authority": None,
                      "text": None, "status": "as published"}] if row.get("PERMIT_IDENTIFIER") else [],
            releases=[], identifiers={"registry": row["REGISTRY_CODE"], "installation_id": row["INSTALLATION_IDENTIFIER"],
                                      "ets_identifier": native},
            reporting_year=year, unknowns=["geometry_not_published_by_registry"])
        records.append(facility)
        compliance = {"code": row.get(f"COMPLIANCE_CODE_{year}") or None,
                      "text": row.get(f"COMPLIANCE_TEXT_{year}") or None, "row": line,
                      "note": "as published; no compliance determination is derived"}
        for indicator, column, unit in (("verified_emissions", f"VERIFIED_EMISSIONS_{year}", "tCO2e"),
                                        ("allocated_allowances", f"ALLOCATION_{year}", "allowances")):
            records.append(_build(
                er.series, "eu-ets", f"{native}:{indicator}", f"{row['INSTALLATION_NAME']} {indicator.replace('_', ' ')}",
                source_url=url, location={"kind": "facility", "ref": f"eu-ets:{native}"},
                indicator={"code": indicator, "name": indicator.replace("_", " "), "scheme": "eu-ets"},
                unit=unit, interval="P1Y", aggregation="total", kind="observation",
                values=[{"start": f"{year}-01-01", "end": f"{year + 1}-01-01", "value": _num(row.get(column), column),
                         "status": "validated" if indicator == "verified_emissions" else "unknown",
                         "flags": {"compliance": compliance, "row": line, "column": column}}],
                status_basis="verified emissions are reported as verified by the registry; allocations carry no status",
                release={"basis": "caller_supplied" if released else "provider_vintage_fallback", "released_at": released}))
    missing = sorted(wanted - {r["native_id"] for r in records if r["record_type"] == "facility"})
    return records, {**carry, "ets_missing_installations": missing}


def parse_open_meteo_meta(content, context, carry, *, url):
    payload = _json(content)
    initialised = payload.get("last_run_initialisation_time") if isinstance(payload, dict) else None
    if initialised is None:
        raise ProviderError("schema_drift", "Open-Meteo model metadata lacks last_run_initialisation_time")
    issue = _utc(datetime.fromtimestamp(int(initialised), tz=UTC))
    return [], {**carry, "issue_time": issue,
                "availability_time": None if payload.get("last_run_availability_time") is None
                else _utc(datetime.fromtimestamp(int(payload["last_run_availability_time"]), tz=UTC))}


def parse_open_meteo(content, context, carry, *, url):
    payload = _json(content)
    hourly, units = (payload.get("hourly"), payload.get("hourly_units")) if isinstance(payload, dict) else (None, None)
    if not isinstance(hourly, dict) or not isinstance(units, dict) or not isinstance(hourly.get("time"), list):
        raise ProviderError("schema_drift", "Open-Meteo response lacks hourly data")
    if payload.get("utc_offset_seconds") not in ("0", 0):
        raise ProviderError("schema_drift", "Open-Meteo request pins GMT; response offset differs")
    forecast = "forecast" in urlsplit(url).path
    provider = "open-meteo-forecast" if forecast else "open-meteo-archive"
    model = dict(OPEN_METEO_MODELS[context["model"]])
    model.pop("meta", None)
    model.update(version=None, documentation="https://open-meteo.com/en/docs")
    if forecast:
        model.update(issue_time=carry.get("issue_time"),
                     issue_time_basis="Open-Meteo meta.json last_run_initialisation_time read before the forecast request")
    lat, lon = _float(payload["latitude"], "latitude"), _float(payload["longitude"], "longitude")
    cell = f"{context['model']}:{lat:.4f},{lon:.4f}"
    records = []
    times = hourly["time"]
    for variable in context["variables"]:
        column = hourly.get(variable)
        if not isinstance(column, list) or len(column) != len(times):
            raise ProviderError("schema_drift", f"Open-Meteo variable {variable} is missing or misaligned")
        values = []
        for index, (stamp, value) in enumerate(zip(times, column, strict=True)):
            begin = datetime.fromisoformat(stamp).replace(tzinfo=UTC)
            values.append({"start": _utc(begin), "end": _utc(begin + timedelta(hours=1)), "value": _num(value, variable),
                           "status": "unknown", "flags": {"json_pointer": f"/hourly/{variable}/{index}"}})
        kind = "forecast" if forecast else "model"
        issue = model.get("issue_time") if forecast else None
        records.append(_build(
            er.series, provider, f"{cell}:{variable}" + (f":{issue}" if forecast else ""),
            f"{model['name']} {variable} at {lat:.4f},{lon:.4f}" + (f" (run {issue})" if forecast else " (reanalysis)"),
            source_url="https://open-meteo.com/en/docs",
            location={"kind": "grid-cell", "ref": f"{provider}:{cell}",
                      "geometry": {"type": "Point", "coordinates": [lon, lat]},
                      "elevation_m": payload.get("elevation"), "precision_m": model["grid_resolution_m"] / 2},
            indicator={"code": variable, "name": variable, "scheme": "open-meteo-variable"},
            unit=units.get(variable), interval="PT1H", aggregation="unknown" if forecast else "unknown", kind=kind,
            values=values, model=model,
            status_basis="model output; values are not observations and carry no validation status"))
    return records, carry


def _dwd_station_rows(text):
    rows = {}
    for line in text.splitlines():
        if not re.match(r"^\s*\d{3,5}\s+\d{8}\s+\d{8}", line):
            continue
        head = line.split()
        station_id, start, end, height, lat, lon = head[:6]
        rest = re.split(r"\s{2,}", line.split(lon, 1)[1].strip())
        rows[station_id.zfill(5)] = {"from": start, "to": end, "height": height, "lat": lat, "lon": lon,
                                     "name": rest[0] if rest else None, "state": rest[1] if len(rest) > 1 else None}
    return rows


def parse_dwd_stations(content, context, carry, *, url):
    text = content.decode("latin-1")
    if "Stations_id" not in text:
        raise ProviderError("schema_drift", "DWD station description header missing")
    row = _dwd_station_rows(text).get(context["station"])
    if row is None:
        return [], {**carry, "dwd_station_missing": context["station"]}
    to = row["to"]
    record = _build(er.station, "dwd", context["station"], row["name"] or context["station"],
                    source_url=url, geometry={"type": "Point", "coordinates": [_float(row["lon"], "lon"), _float(row["lat"], "lat")]},
                    identifiers={"dwd_station_id": context["station"]}, network="DWD", elevation_m=_float(row["height"], "height"),
                    active_from=f"{row['from'][:4]}-{row['from'][4:6]}-{row['from'][6:]}",
                    active_to=None,
                    properties={"state": row["state"], "last_data_date": f"{to[:4]}-{to[4:6]}-{to[6:]}"})
    return [record], carry


def parse_dwd_product(content, context, carry, *, url):
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = [n for n in archive.namelist() if n.startswith("produkt_") and n.endswith(".txt")]
            if len(names) != 1:
                raise ProviderError("schema_drift", "DWD archive must contain exactly one product file")
            text = archive.read(names[0]).decode("latin-1")
            product = names[0]
    except zipfile.BadZipFile as exc:
        raise ProviderError("schema_drift", "DWD product is not a ZIP archive") from exc
    reader = csv.reader(io.StringIO(text), delimiter=";")
    header = [h.strip() for h in next(reader)]
    columns = DWD_PARAMETERS[context["parameter"]]["columns"]
    if not {"STATIONS_ID", "MESS_DATUM", "QN_9"} <= set(header) or set(columns) - set(header):
        raise ProviderError("schema_drift", "DWD product columns changed")
    position = {name: i for i, name in enumerate(header)}
    series_values = {column: [] for column in columns}
    status = "provisional" if context["period"] == "recent" else "validated"
    for line, row in enumerate(reader, start=2):
        if not row or not row[0].strip():
            continue
        if row[position["STATIONS_ID"]].strip().zfill(5) != context["station"]:
            raise ProviderError("schema_drift", "DWD product belongs to another station")
        stamp = row[position["MESS_DATUM"]].strip()
        begin = datetime.strptime(stamp, "%Y%m%d%H").replace(tzinfo=UTC)
        for column in columns:
            raw = row[position[column]].strip()
            series_values[column].append({"start": _utc(begin), "end": _utc(begin + timedelta(hours=1)),
                                          "value": None if raw in {"-999", "-999.0"} else _num(raw, column),
                                          "status": status, "quality": row[position["QN_9"]].strip(),
                                          "flags": {"line": line, "file": product}})
    records = []
    for column, (name, unit) in columns.items():
        records.append(_build(
            er.series, "dwd", f"{context['station']}:{column}", f"DWD {name} at station {context['station']}",
            source_url=url, location={"kind": "station", "ref": f"dwd:{context['station']}"},
            indicator={"code": column, "name": name, "scheme": "dwd-cdc"}, unit=unit, interval="PT1H",
            aggregation="instant" if column in {"TT_TU", "RF_TU"} else "unknown", kind="observation",
            values=series_values[column],
            status_basis=f"DWD '{context['period']}' product: " + ("not yet completely quality controlled" if status == "provisional"
                                                                    else "quality controlled"),
            release={"basis": "acquisition", "period": context["period"], "product": product}))
    return records, carry


PARSERS: dict[str, Callable[..., tuple[list[dict[str, Any]], dict[str, Any]]]] = {
    "openaq_location": parse_openaq_location, "openaq_hours": parse_openaq_hours,
    "uba_stations": parse_uba_stations, "uba_components": parse_uba_components, "uba_measures": parse_uba_measures,
    "entsoe": parse_entsoe, "smard": parse_smard, "eea_industry": parse_eea_industry, "eu_ets": parse_eu_ets,
    "open_meteo_meta": parse_open_meteo_meta, "open_meteo": parse_open_meteo,
    "dwd_stations": parse_dwd_stations, "dwd_product": parse_dwd_product,
}


def parse_step(step, content, carry):
    context = {**dict(step.get("context") or {}), "params": step.get("params")}
    records, carry = PARSERS[step["parse"]](content, context, dict(carry or {}), url=step["url"])
    return [_record(r) for r in records], carry


def coverage(provider, carry, records):
    notes = {k: v for k, v in carry.items() if k.endswith(("missing", "missing_stations", "missing_installations",
                                                           "no_data", "truncated")) and v}
    return {"complete": False, "provider": provider, "records": len(records),
            "notes": notes, "basis": "explicit bounded selection; never comprehensive provider coverage"}


# ------------------------------------------------------- source-pack adapter


def _secret_request(provider, secret, params, headers):
    slot = SECRETS.get(provider)
    if slot is None:
        return params, headers
    if not secret:
        raise ProviderError("authentication_failed", f"{provider} requires {slot[2]}")
    if slot[0] == "header":
        return params, {**headers, slot[1]: secret}
    return {**params, slot[1]: secret}, headers


def runtime_record(record, *, response_sha256):
    language = "de" if record["provider"] in {"uba", "dwd", "smard"} else "en"
    return {"id": f"{record['provider']}:{record['record_type']}:{record['native_id']}", "title": record["title"],
            "language": language, "url": record["source_url"], "environment_record": record,
            "response_sha256": response_sha256}


class EnvironmentSourceAdapter:
    """``environment`` native connector: one plan step per runtime page."""

    accepts_transport = True

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from functools import partial

        from src.ingestion.source_pack_runtime import HTTPSPageAdapter
        from src.ingestion.source_packs import SourcePackError

        self.source = json.loads(json.dumps(source))
        spec = dict(self.source.get("environment") or {})
        self.provider = str(spec.get("provider") or "")
        try:
            self.steps = plan(self.provider, spec.get("selection"))
        except ProviderError as exc:
            raise SourcePackError("unbounded_source", str(exc)) from exc
        for step in self.steps:
            if urlsplit(step["url"]).hostname not in PROVIDER_HOSTS[self.provider]:
                raise SourcePackError("network_policy", "environment requests stay on the provider's declared hosts")
        self.secret = secret
        self.transport = transport or partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "environment": {"provider": self.provider, "steps": len(self.steps),
                            "live_verification": LIVE_VERIFICATION[self.provider]["status"]},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _scope(self) -> str:
        return digest({"source_hash": self.source["source_hash"], "steps": self.steps})

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms
        from src.ingestion.source_packs import SourcePackError

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "environment runs use the pinned selection")
        state = {} if cursor is None else json.loads(cursor)
        if cursor is not None and state.get("scope") != self._scope():
            raise SourcePackError("cursor_drift", "cursor belongs to a different selection")
        index = int(state.get("i", 0))
        if index >= len(self.steps):
            return RuntimePage((), None, 0, receipt={"status": 200})
        step = self.steps[index]
        try:
            params, headers = _secret_request(self.provider, self.secret, dict(step["params"]), {"Accept": "*/*"})
        except ProviderError as exc:
            raise SourcePackError("authentication_failed", str(exc)) from exc
        response = self.transport(url=step["url"], params=params, headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        status = int(response.get("status", 200))
        response_headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "source response exceeds its byte limit")
        if status == 429:
            raise SourcePackError("rate_limited", f"{self.provider} rate limit reached",
                                  retry_after_ms=_retry_after_ms(response_headers.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"{self.provider} refused the credential (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"{self.provider} returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"{self.provider} returned HTTP {status}")
        if self.secret and len(self.secret) >= 8 and self.secret.encode() in raw:
            raise SourcePackError("schema_drift", "provider echoed a credential; response is not safe evidence")
        response_sha256 = hashlib.sha256(raw).hexdigest()
        try:
            records, carry = parse_step(step, raw, state.get("carry") or {})
        except ProviderError as exc:
            raise SourcePackError("schema_drift" if exc.code != "authentication_failed" else exc.code, str(exc)) from exc
        more = index + 1 < len(self.steps)
        next_cursor = (json.dumps({"i": index + 1, "scope": self._scope(), "carry": carry}, sort_keys=True)
                       if more else None)
        return RuntimePage(tuple(runtime_record(r, response_sha256=response_sha256) for r in records), next_cursor,
                           len(raw), receipt={"status": status, "step": index, "steps": len(self.steps),
                                              "parse": step["parse"], "response_sha256": response_sha256,
                                              "provider": self.provider, "coverage": coverage(self.provider, carry, records)})


ADAPTERS = {"environment": EnvironmentSourceAdapter}


def _fixture_body(page):
    if "body_base64" in page:
        return base64.b64decode(page["body_base64"])
    body = page.get("body")
    return json.dumps(body).encode() if isinstance(body, (dict, list)) else (body or "").encode()


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Serve authored native pages keyed by URL + public query (credentials are ignored)."""

    by_key = {(page["url"], canonical(dict(page.get("params") or {}))): page for page in pages}

    def transport(*, url, params, headers, timeout, **_):
        del headers, timeout
        public = {k: v for k, v in dict(params or {}).items() if k not in {"securityToken"}}
        page = by_key.get((url, canonical(public)))
        if page is None:
            return {"status": 404, "headers": {}, "content": b""}
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": _fixture_body(page)}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = EnvironmentSourceAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                                       secret=FIXTURE_SECRET)
    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {}}, cursor=cursor)
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records


# ------------------------------------------------------ DurableHTTP client


class EnvironmentClient:
    """Explicit bounded acquisition through one DurableHTTP budget per provider."""

    def __init__(self, http: DurableHTTP, *, principal_id, secret: str | None = None):
        if http.provider not in PROVIDER_HOSTS or not http.hosts <= PROVIDER_HOSTS[http.provider]:
            raise ValueError("environment client requires an exact provider-specific host policy")
        self.http, self.principal_id, self.secret = http, principal_id, secret

    def acquire(self, selection, observation):
        provider = self.http.provider
        steps = plan(provider, selection)
        carry, records, captures = {}, [], []
        slot = SECRETS.get(provider)
        if slot and not self.secret:
            raise ProviderError("authentication_failed", f"{provider} requires {slot[2]}")
        for index, step in enumerate(steps):
            secret_headers = {slot[1]: self.secret} if slot and slot[0] == "header" else None
            secret_params = {slot[1]: self.secret} if slot and slot[0] == "param" else None
            captured = self.http.request(f"{observation}:{index}:{step['parse']}", step["url"], principal_id=self.principal_id,
                                         params=dict(step["params"]), headers={"Accept": "*/*"},
                                         secret_headers=secret_headers, secret_params=secret_params, max_bytes=20_000_000)
            captures.append(captured)
            parsed, carry = parse_step(step, captured.content, carry)
            records.extend(parsed)
        return {"records": records, "coverage": coverage(provider, carry, records)}, captures


def acquire(client, selection, *, namespace, scopes, reuse_notice, observation, principal_id, store=None):
    """Run one bounded selection; failures are recorded and never alter stored values."""

    from src.kb.environment_store import EnvironmentEvidenceStore

    store = store or EnvironmentEvidenceStore(client.http.conn, now=client.http.now)
    provider = client.http.provider
    try:
        parsed, captures = client.acquire(selection, observation)
    except (ProviderError, ValueError) as exc:
        failure = store.fail(provider, exc, namespace=namespace, scopes=scopes, observation_id=observation)
        return {"ok": False, "provider": provider, "failure": failure}
    return {"ok": True, "provider": provider,
            **store.ingest(provider, parsed, captures, namespace=namespace, scopes=scopes,
                           reuse_notice=reuse_notice, principal_id=principal_id)}
