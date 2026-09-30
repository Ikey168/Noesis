"""Bounded, receipted acquisition of critical infrastructure registries (CI01, CI03-CI06; #2359, #2367-#2376).

Every source has a recorded access decision (:data:`PROVIDER_CONTRACTS`,
audit: ``docs/development/infrastructure-evidence/source-audit.md``):

* endpoints;
* licence, redistribution and attribution;
* rate limits and bounds;
* release/as-of semantics;
* the bounded coverage the Geospatial ``infrastructure`` feature acquires
  (:data:`BOUNDED_COVERAGE`).

Restricted layers are listed in :data:`EXCLUDED` and never requested. Live
state is kept separately in :data:`LIVE_VERIFICATION`; nothing is
live-verified until a dated run says so.

The sources:

* **WRI Global Power Plant Database** (``gppd``): the v1.3.0 release CSV,
  filtered to the selected countries. Records are keyed by ``gppd_idnr``, with
  WEPP ids kept as published and generation estimates flagged as estimates.
* **Global Energy Monitor** (``gem``): one operator-declared tracker release
  file (coal plant units, gas pipelines, LNG terminals). Records are keyed by
  the GEM id. Owners and parents are kept with shares as published, and wiki
  notes are cited by URL only. Every record carries the non-commercial licence
  constraint until GEM terms are verified.
* **OpenStreetMap via Overpass** (``osm``): one bbox and tag set per
  selection, with ``timeout``/``maxsize``/``limit`` in the query text (the
  receipt). Records are keyed by element and carry version and changeset.
  Editor identities are dropped. Operator tags are assertions, not identities.
* **EIA energy infrastructure layers** (``eia``): ArcGIS FeatureServer layers
  queried as GeoJSON within a bbox, keyed by the publisher's feature ids.
* **ENTSOG Transparency Platform** (``entsog``): points and firm technical
  capacity per direction as published. The API gives no coordinates, so the
  geometry stays unknown and is never geocoded.

One plan/parse pipeline serves two paths:

* :func:`acquire`: an explicit bounded selection, with an injected or
  DurableHTTP-backed fetch and one receipt per step;
* the source-pack runtime: the ``infrastructure`` connector,
  :class:`InfrastructureSourceAdapter`, whose pages the
  :class:`src.kb.infrastructure_assets.InfrastructureProjector` stores.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from src.ingestion.provider_execution import ProviderError
from src.kb import infrastructure_assets as ia

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_SCHEMA = "noesis-infrastructure-asset-record-v1"
FIXTURE_SECRET = None  # every infrastructure source is unauthenticated
GPPD_URL = ("https://raw.githubusercontent.com/wri/global-power-plant-database/v1.3.0/output_database/"
            "global_power_plant_database.csv")
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
ENTSOG_API = "https://transparency.entsog.eu/api/v1"
EIA_SERVICE = "https://services7.arcgis.com/FGr1D95XCGALKXqM/arcgis/rest/services"
EIA_LAYERS = {
    "power-plants": {"url": f"{EIA_SERVICE}/Power_Plants/FeatureServer/0/query", "asset_class": "power_plant",
                     "title": "Power Plants"},
    "lng-terminals": {"url": f"{EIA_SERVICE}/Liquefied_Natural_Gas_Import_Exports_and_Terminals/FeatureServer/0/query",
                      "asset_class": "lng_terminal", "title": "Liquefied Natural Gas Import/Export Terminals"},
    "gas-pipelines": {"url": f"{EIA_SERVICE}/Natural_Gas_Interstate_and_Intrastate_Pipelines/FeatureServer/0/query",
                      "asset_class": "pipeline", "title": "Natural Gas Interstate and Intrastate Pipelines"},
}
PROVIDER_HOSTS = {"gppd": {"raw.githubusercontent.com"}, "gem": {"globalenergymonitor.org"},
                  "osm": {"overpass-api.de"}, "eia": {"services7.arcgis.com"}, "entsog": {"transparency.entsog.eu"}}
MAX_ROWS = 500
MAX_POINTS = 20
MAX_BBOX_DEGREES = 1.0
NEVER = ("Only attributes the publisher releases: no vulnerability, criticality or dependency assessment, no asset "
         "valuation, no inferred routes or geocoding of unlocated assets, no planet-wide or continuous OSM mirroring.")
_UNAVAILABLE = ("record the failed step with its code in a receipt; stored revisions stay as they are and the provider "
                "reads as stale; nothing is inferred")
LICENCES = {
    "gppd": {"id": "cc-by-4.0", "terms_url": "https://creativecommons.org/licenses/by/4.0/",
             "constraints": ["attribution required"]},
    "gem": {"id": "gem-terms-non-commercial-until-verified", "terms_url": "https://globalenergymonitor.org/about/terms-of-use/",
            "constraints": ["non-commercial use only (recorded per CI01 until the tracker's terms are verified)",
                            "attribution required", "wiki notes cited by URL, never copied"]},
    "osm": {"id": "odbl-1.0", "terms_url": "https://www.openstreetmap.org/copyright",
            "constraints": ["ODbL 1.0 share-alike: a derived database shared publicly is offered under ODbL",
                            "attribution '© OpenStreetMap contributors' required"]},
    "eia": {"id": "eia-public-domain", "terms_url": "https://www.eia.gov/about/copyrights_reuse.php",
            "constraints": ["cite EIA as the source"]},
    "entsog": {"id": "entsog-transparency-terms", "terms_url": "https://transparency.entsog.eu/",
               "constraints": ["attribution required (verify ENTSOG reuse terms at the first live run)"]},
}
ATTRIBUTION = {
    "gppd": "World Resources Institute, Global Power Plant Database (CC BY 4.0)",
    "gem": "Global Energy Monitor",
    "osm": "© OpenStreetMap contributors (ODbL 1.0)",
    "eia": "Source: U.S. Energy Information Administration (EIA)",
    "entsog": "Source: ENTSOG Transparency Platform",
}
DATASET_URLS = {"gppd": "https://datasets.wri.org/dataset/globalpowerplantdatabase",
                "gem": "https://globalenergymonitor.org/projects/", "osm": "https://www.openstreetmap.org/",
                "eia": "https://atlas.eia.gov/", "entsog": "https://transparency.entsog.eu/"}
PROVIDER_CONTRACTS = {
    "gppd": {
        "decision": "acquire (unverified-live)",
        "documentation": "https://datasets.wri.org/dataset/globalpowerplantdatabase",
        "endpoints": [GPPD_URL + " (release tag v1.3.0; verify the tag path)"],
        "authentication": "none",
        "terms": "CC BY 4.0", "attribution": ATTRIBUTION["gppd"], "redistribution": "permitted with attribution",
        "rate_limits": "one request per selection (~10 MB); rows filtered to the selected countries, at most 500",
        "revisions": "a static versioned release: the database version (1.3.0, 2021-06-02) is the declared release; "
                     "year_of_capacity_data dates the capacity; estimated generation columns are publisher estimates",
        "verify": "column names (gppd_idnr, capacity_mw, wepp_id, estimated_generation_gwh_YYYY) and the tag path",
        "unavailable_fallback": _UNAVAILABLE,
    },
    "gem": {
        "decision": "acquire an operator-declared tracker release file only (unverified-live)",
        "documentation": "https://globalenergymonitor.org/projects/",
        "endpoints": ["https://globalenergymonitor.org/<declared release file>.csv (downloads are released through a "
                      "request form; the operator declares the file, label and release date)"],
        "authentication": "none (the release file is obtained under GEM's terms)",
        "terms": "GEM terms of use; some downloads state non-commercial terms (verify per tracker)",
        "attribution": ATTRIBUTION["gem"],
        "redistribution": "non-commercial only until verified; wiki notes cited by URL, not copied",
        "rate_limits": "one file per selection; rows filtered to the selected countries, at most 500",
        "revisions": "tracker releases are declared (label and date); a status differing from the previous release "
                     "becomes a dated status revision, earlier statuses kept",
        "verify": "tracker column names (GEM unit/phase ID, Status, Owner, Parent, Capacity (MW), Start year, "
                  "Retired year, Other IDs (location), Route/WKT) per tracker release",
        "unavailable_fallback": _UNAVAILABLE,
    },
    "osm": {
        "decision": "acquire bounded Overpass extracts (unverified-live)",
        "documentation": "https://wiki.openstreetmap.org/wiki/Overpass_API",
        "endpoints": [OVERPASS_URL + "?data=<query> (query text stored as the receipt)"],
        "authentication": "none",
        "terms": "ODbL 1.0", "attribution": ATTRIBUTION["osm"],
        "redistribution": "share-alike for a derivative database",
        "rate_limits": "[timeout:60][maxsize:16777216], one bbox <= 1 x 1 degree, declared tags only, out meta geom "
                       "<= 500 elements; public instance usage policy (~10000 queries/day, verify)",
        "revisions": "osm3s.timestamp_osm_base dates the extract; element version and changeset kept; a new element "
                     "version in a later extract is a new revision, an unchanged element is not",
        "verify": "cross-reference tags ref:gppd and ref:eia (tag usage) and the lifecycle-prefix conventions",
        "unavailable_fallback": _UNAVAILABLE,
    },
    "eia": {
        "decision": "acquire (unverified-live)",
        "documentation": "https://atlas.eia.gov/",
        "endpoints": [layer["url"] + " (f=geojson, bbox envelope, resultRecordCount)" for layer in EIA_LAYERS.values()],
        "authentication": "none",
        "terms": "U.S. government data, public domain", "attribution": ATTRIBUTION["eia"],
        "redistribution": "permitted",
        "rate_limits": "resultRecordCount <= 500, one request per layer and bbox; truncation recorded",
        "revisions": "no per-feature release date: a declared layer release dates the revision, else retrieval time "
                     "(labelled); OBJECTID is not stable across releases, Plant_Code is",
        "verify": "service host, layer paths and field names (Plant_Code, Total_MW, Utility_Name, Status, Docket)",
        "unavailable_fallback": _UNAVAILABLE,
    },
    "entsog": {
        "decision": "acquire (unverified-live)",
        "documentation": "https://transparency.entsog.eu/api/archiveDirectories/8/api-manual/TP_REG715_Documentation_TP_API%20-%20v2.1.pdf",
        "endpoints": [f"{ENTSOG_API}/connectionpoints", f"{ENTSOG_API}/operationaldatas (indicator Firm Technical)"],
        "authentication": "none",
        "terms": "ENTSOG Transparency Platform terms (verify)", "attribution": ATTRIBUTION["entsog"],
        "redistribution": "permitted with attribution (verify)",
        "rate_limits": "at most 20 point keys and a 7-day window per selection",
        "revisions": "lastUpdateDateTime dates each capacity; units (e.g. kWh/d) and direction (entry/exit) as "
                     "published; points carry no WGS84 coordinates, so geometry stays unknown",
        "verify": "field names pointKey, pointLabel, pointType, directionKey, indicator, unit, value, lastUpdateDateTime",
        "unavailable_fallback": _UNAVAILABLE,
    },
}
EXCLUDED = [
    {"source": "eia", "what": "layers or fields marked Critical Energy/Electric Infrastructure Information (CEII) or "
                               "for official use", "reason": "restricted; never requested"},
    {"source": "entsog", "what": "the capacity map (PDF) and TSO-login data", "reason": "not an open machine-readable "
                                                                                          "release"},
    {"source": "gem", "what": "wiki page text and data shared under separate agreement", "reason": "cited by URL only; "
                                                                                                   "not redistributable"},
    {"source": "osm", "what": "planet files, diffs and continuous mirroring; editor user names and ids",
     "reason": "bounded extracts only; personal data is not needed"},
]
BOUNDED_COVERAGE = {
    "areas": [
        {"id": "de-lusatia", "bbox": [14.2, 51.3, 14.8, 51.8],
         "sources": ["osm", "gppd", "gem"], "note": "all geometry-bearing European sources overlap here"},
        {"id": "de", "bbox": [5.86, 47.27, 15.04, 55.06], "sources": ["gppd", "gem", "entsog"],
         "note": "country selections; ENTSOG points carry no geometry"},
        {"id": "us-gulf-coast", "bbox": [-94.5, 29.0, -88.8, 31.0], "sources": ["eia", "gem"],
         "note": "LNG terminals and natural-gas pipelines"},
        {"id": "us-southern-california", "bbox": [-118.5, 33.5, -116.0, 35.5], "sources": ["eia"],
         "note": "EIA power plants; pairs with the Energy Systems EIA-860M (CISO) plants"},
    ],
    "countries": {"gppd": ["DEU"], "gem": ["Germany", "United States"]},
    "asset_classes": {"gppd": ["power_plant"], "gem": ["generating_unit", "pipeline", "lng_terminal"],
                      "osm": ["power_plant", "generating_unit", "transmission_line", "substation", "pipeline"],
                      "eia": ["power_plant", "lng_terminal", "pipeline"], "entsog": ["gas_point"]},
    "limits": {"max_rows": MAX_ROWS, "max_points": MAX_POINTS, "overpass_bbox_degrees": MAX_BBOX_DEGREES,
               "overpass_timeout_s": 60, "overpass_maxsize_bytes": 16_777_216, "entsog_window_days": 7},
    "mines": "covered by the record model; no mine tracker is selected in 1.0.0",
}
LIVE_VERIFICATION = {
    provider: {"status": "unverified-live", "checked_at": None, "report": None,
               "note": "no dated bounded live run yet (CI14, #2401); offline fixtures only"}
    for provider in PROVIDER_CONTRACTS
}
OSM_TAGS = ("power=plant", "power=generator", "power=line", "power=substation", "man_made=pipeline")
OSM_LIFECYCLE = {"construction": "construction", "proposed": "proposed", "planned": "proposed", "disused": "mothballed",
                 "abandoned": "retired", "demolished": "retired", "razed": "retired", "removed": "retired"}
OSM_CLASSES = {("power", "plant"): "power_plant", ("power", "generator"): "generating_unit",
               ("power", "line"): "transmission_line", ("power", "substation"): "substation",
               ("man_made", "pipeline"): "pipeline"}
OSM_IDENTIFIER_TAGS = {"wikidata": "wikidata", "ref:gppd": "gppd_idnr", "ref:eia": "eia_plant_code", "ref": "osm_ref"}
OSM_CAPACITY_TAGS = {"plant:output:electricity": "electrical_capacity",
                     "generator:output:electricity": "electrical_capacity", "voltage": "voltage"}
GEM_STATUSES = {"announced": "proposed", "pre-permit": "proposed", "permitted": "proposed", "proposed": "proposed",
                "shelved": "proposed", "construction": "construction", "operating": "operating", "idle": "mothballed",
                "mothballed": "mothballed", "retired": "retired", "cancelled": "cancelled"}
EIA_STATUSES = {"operating": "operating", "under construction": "construction", "approved": "proposed",
                "proposed": "proposed", "retired": "retired", "cancelled": "cancelled"}
GEM_TRACKERS = {
    "coal-plants": {"title": "Global Coal Plant Tracker", "asset_class": "generating_unit",
                    "id": "GEM unit/phase ID", "id_scheme": "gem_unit_id", "location_id": "GEM location ID", "name": ("Plant name", "Unit name"),
                    "country": "Country/Area", "capacity": [("Capacity (MW)", "MW", "electrical_capacity")],
                    "geometry": "point"},
    "gas-pipelines": {"title": "Global Gas Infrastructure Tracker: pipelines", "asset_class": "pipeline",
                      "id": "ProjectID", "id_scheme": "gem_project_id", "location_id": None, "name": ("PipelineName", "SegmentName"),
                      "country": "Countries", "capacity": [("Capacity", "CapacityUnits", "throughput_capacity")],
                      "geometry": "wkt"},
    "lng-terminals": {"title": "Global Gas Infrastructure Tracker: LNG terminals", "asset_class": "lng_terminal",
                      "id": "TerminalID", "id_scheme": "gem_terminal_id", "location_id": None, "name": ("TerminalName", "UnitName"),
                      "country": "Country", "capacity": [("Capacity", "CapacityUnits", "throughput_capacity")],
                      "geometry": "point"},
}
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WKT_LINE = re.compile(r"^\s*LINESTRING\s*\((.*)\)\s*$", re.I)
_QUANTITY = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z/%]+)?\s*$")
_OTHER_ID_SCHEMES = {"wri": "gppd_idnr", "gppd": "gppd_idnr", "wepp": "wepp_id", "eia": "eia_plant_code"}


# --------------------------------------------------------------------- helpers


def _json(content):
    try:
        return json.loads(content, parse_float=str, parse_int=str)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProviderError("schema_drift", "provider response is not valid JSON") from exc


def _csv(content):
    try:
        text = content.decode("utf-8-sig") if isinstance(content, (bytes, bytearray)) else str(content)
    except UnicodeDecodeError as exc:
        raise ProviderError("schema_drift", "release file is not UTF-8 CSV") from exc
    return list(csv.DictReader(io.StringIO(text)))


def _num(value, field):
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", "null", "NaN", "nan", "None"}:
        return None
    try:
        return ia.decimal_text(text, field)
    except ia.InfrastructureError as exc:
        raise ProviderError("schema_drift", f"{field} is not numeric") from exc


def _year(value):
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}(\.0+)?", text):
        return text[:4]
    return None


def _clean(value):
    text = str(value if value is not None else "").strip()
    return text or None


def _build(*args, **kwargs):
    try:
        return ia.record(*args, **kwargs)
    except ia.InfrastructureError as exc:
        raise ProviderError("schema_drift" if exc.code != "sensitive_enrichment_refused" else exc.code, str(exc)) from exc


def _declared_release(selection, retrieved_at, *, key_prefix):
    release = dict(selection.get("release") or {})
    if release:
        if not _ISO_DAY.fullmatch(str(release.get("released_on") or "")) or not release.get("label"):
            raise ProviderError("unbounded_selection", "a declared release states its label and released_on date")
        return {"key": f"{key_prefix}:{release['label']}", "released_at": release["released_on"],
                "basis": "declared_release", "label": release["label"]}
    return {"key": f"{key_prefix}:retrieved:{retrieved_at}", "released_at": None, "basis": "retrieval_time",
            "label": None}


def _bbox(value):
    if not isinstance(value, list) or len(value) != 4:
        raise ProviderError("unbounded_selection", "bbox is [west, south, east, north]")
    try:
        west, south, east, north = (float(v) for v in value)
    except (TypeError, ValueError) as exc:
        raise ProviderError("unbounded_selection", "bbox values are numbers") from exc
    if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
        raise ProviderError("unbounded_selection", "bbox is outside WGS84 bounds or empty")
    return [west, south, east, north]


def _limit(value, default, ceiling, field):
    amount = int(value if value is not None else default)
    if not 1 <= amount <= ceiling:
        raise ProviderError("unbounded_selection", f"{field} must be between 1 and {ceiling}")
    return amount


def _point_geometry(lon, lat, *, crs, basis_note):
    if lon is None or lat is None:
        return None, None
    precision, basis = ia.precision_from_decimals(lon, lat)
    return ({"type": "Point", "coordinates": [float(lon), float(lat)]},
            {"crs_published": crs, "precision_m": precision, "precision_basis": f"{basis}; {basis_note}"})


def _owners(text, role):
    """``Name [60%]; Other [40%]`` (GEM) -> assertions with the shares as published."""

    owners = []
    for part in [p.strip() for p in re.split(r";", str(text or "")) if p.strip()]:
        match = re.fullmatch(r"(.*?)\s*\[\s*([0-9.]+)\s*%\s*\]\s*", part)
        if match:
            owners.append({"role": role, "name": match.group(1).strip(), "share": match.group(2),
                           "share_text": part})
        else:
            owners.append({"role": role, "name": part, "share": None, "share_text": None})
    return owners


def _quantity(text):
    match = _QUANTITY.fullmatch(str(text or ""))
    if not match:
        return None, None
    return match.group(1), match.group(2)


def _selection_countries(selection, key="countries", limit=10):
    countries = selection.get(key)
    if not isinstance(countries, list) or not 1 <= len(countries) <= limit:
        raise ProviderError("unbounded_selection", f"{key} lists 1-{limit} explicit countries")
    return [str(c) for c in countries]


# ------------------------------------------------------------------ selections


def overpass_query(bbox, tags, *, limit, timeout=60, maxsize=16_777_216):
    """The Overpass QL text for one bounded extract (also the receipt)."""

    west, south, east, north = bbox
    lines = []
    for tag in tags:
        key, value = tag.split("=", 1)
        lines.append(f'  nwr["{key}"="{value}"];')
        for prefix in OSM_LIFECYCLE:
            lines.append(f'  nwr["{prefix}:{key}"="{value}"];')
    return (f"[out:json][timeout:{timeout}][maxsize:{maxsize}][bbox:{south},{west},{north},{east}];\n(\n"
            + "\n".join(lines) + f"\n);\nout meta geom {limit};")


def plan(provider, selection):
    """Ordered, bounded request steps for one explicit selection."""

    selection = dict(selection or {})
    if provider == "gppd":
        _selection_countries(selection)
        _limit(selection.get("max_rows"), 200, MAX_ROWS, "max_rows")
        return [{"url": GPPD_URL, "params": {}, "parse": "gppd", "context": {}}]
    if provider == "gem":
        tracker = str(selection.get("tracker") or "")
        if tracker not in GEM_TRACKERS:
            raise ProviderError("unbounded_selection", f"tracker is one of {sorted(GEM_TRACKERS)}")
        url = str(selection.get("file_url") or "")
        if not url.startswith("https://") or urlsplit(url).hostname not in PROVIDER_HOSTS["gem"]:
            raise ProviderError("network_policy", "GEM release files are declared on globalenergymonitor.org")
        if not selection.get("release"):
            raise ProviderError("unbounded_selection", "a GEM selection declares its tracker release (label, released_on)")
        _selection_countries(selection)
        _limit(selection.get("max_rows"), 200, MAX_ROWS, "max_rows")
        return [{"url": url, "params": {}, "parse": "gem", "context": {"tracker": tracker}}]
    if provider == "osm":
        bbox = _bbox(selection.get("bbox"))
        if bbox[2] - bbox[0] > MAX_BBOX_DEGREES or bbox[3] - bbox[1] > MAX_BBOX_DEGREES:
            raise ProviderError("unbounded_selection", "an Overpass bbox is at most 1 x 1 degree")
        tags = list(selection.get("tags") or OSM_TAGS)
        if not tags or set(tags) - set(OSM_TAGS):
            raise ProviderError("unbounded_selection", f"tags are a subset of {list(OSM_TAGS)}")
        limit = _limit(selection.get("limit"), 200, MAX_ROWS, "limit")
        query = overpass_query(bbox, tags, limit=limit)
        return [{"url": OVERPASS_URL, "params": {"data": query}, "parse": "overpass",
                 "context": {"area": selection.get("area"), "bbox": bbox, "tags": tags, "limit": limit}}]
    if provider == "eia":
        layer = str(selection.get("layer") or "")
        if layer not in EIA_LAYERS:
            raise ProviderError("unbounded_selection", f"layer is one of {sorted(EIA_LAYERS)}")
        bbox = _bbox(selection.get("bbox"))
        count = _limit(selection.get("max_records"), 200, MAX_ROWS, "max_records")
        params = {"where": "1=1", "geometry": ",".join(str(v) for v in bbox), "geometryType": "esriGeometryEnvelope",
                  "inSR": "4326", "outSR": "4326", "spatialRel": "esriSpatialRelIntersects", "outFields": "*",
                  "f": "geojson", "resultRecordCount": str(count)}
        return [{"url": EIA_LAYERS[layer]["url"], "params": params, "parse": "eia",
                 "context": {"layer": layer, "max_records": count}}]
    if provider == "entsog":
        keys = selection.get("point_keys")
        if not isinstance(keys, list) or not 1 <= len(keys) <= MAX_POINTS:
            raise ProviderError("unbounded_selection", f"point_keys lists 1-{MAX_POINTS} explicit point keys")
        start, end = str(selection.get("from") or ""), str(selection.get("to") or "")
        if not (_ISO_DAY.fullmatch(start) and _ISO_DAY.fullmatch(end)):
            raise ProviderError("unbounded_selection", "from and to are ISO dates")
        days = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days
        if not 0 <= days <= 7:
            raise ProviderError("unbounded_selection", "the ENTSOG window is at most 7 days")
        joined = ",".join(str(k) for k in keys)
        return [{"url": f"{ENTSOG_API}/connectionpoints", "params": {"pointKey": joined, "limit": str(len(keys))},
                 "parse": "entsog_points", "context": {}},
                {"url": f"{ENTSOG_API}/operationaldatas",
                 "params": {"pointKey": joined, "indicator": "Firm Technical", "periodType": "day", "from": start,
                            "to": end, "limit": str(len(keys) * 2 * (days + 1))},
                 "parse": "entsog_capacity", "context": {}}]
    raise ProviderError("unbounded_selection", "unknown infrastructure provider")


# --------------------------------------------------------------------- parsers


def parse_gppd(content, context, carry, *, url):
    selection = context["selection"]
    countries = set(_selection_countries(selection))
    max_rows = _limit(selection.get("max_rows"), 200, MAX_ROWS, "max_rows")
    rows = _csv(content)
    if rows and not {"gppd_idnr", "capacity_mw", "country"} <= set(rows[0]):
        raise ProviderError("schema_drift", "GPPD columns gppd_idnr, capacity_mw and country are required")
    release_spec = dict(selection.get("release") or {"label": "Global Power Plant Database v1.3.0",
                                                       "released_on": "2021-06-02"})
    release = _declared_release({"release": release_spec}, context["retrieved_at"], key_prefix="gppd")
    records, selected = [], [(i, r) for i, r in enumerate(rows) if r.get("country") in countries]
    if len(selected) > max_rows:
        carry["gppd_truncated"] = f"{len(selected)} rows in the selected countries; {max_rows} kept"
    for index, row in selected[:max_rows]:
        geometry, receipt = _point_geometry(_clean(row.get("longitude")), _clean(row.get("latitude")),
                                            crs="EPSG:4326 (latitude/longitude columns)",
                                            basis_note=f"geolocation_source: {row.get('geolocation_source') or 'not stated'}")
        estimates, reported = [], {}
        for key, value in sorted(row.items()):
            year = key.rsplit("_", 1)[-1]
            if key.startswith("estimated_generation_gwh_") and _clean(value):
                estimates.append({"metric": "annual_generation", "year": int(year), "value": _num(value, key),
                                  "unit": "GWh", "basis": "publisher estimate",
                                  "note": _clean(row.get(f"estimated_generation_note_{year}"))})
            elif key.startswith("generation_gwh_") and _clean(value):
                reported[year] = _num(value, key)
        owners = [{"role": "owner", "name": row["owner"].strip(), "share": None, "share_text": None}] \
            if _clean(row.get("owner")) else []
        identifiers = [{"scheme": "gppd_idnr", "value": row["gppd_idnr"]}]
        if _clean(row.get("wepp_id")):
            identifiers.append({"scheme": "wepp_id", "value": row["wepp_id"].strip()})
        records.append(_build(
            "gppd", "gppd:global_power_plant_database", row["gppd_idnr"], "power_plant", name=_clean(row.get("name")),
            source_url=DATASET_URLS["gppd"], attribution=ATTRIBUTION["gppd"], licence=LICENCES["gppd"],
            release=release, retrieved_at=context["retrieved_at"], country=row.get("country"),
            identifiers=identifiers, geometry=geometry, geometry_receipt=receipt,
            capacities=[{"metric": "electrical_capacity", "value": _num(row.get("capacity_mw"), "capacity_mw"),
                         "unit": "MW", "effective_date": _year(row.get("year_of_capacity_data")),
                         "effective_basis": "year_of_capacity_data" if _year(row.get("year_of_capacity_data"))
                         else None, "published_text": row.get("capacity_mw")}],
            owners=owners, estimates=estimates,
            attributes={k: row[k] for k in ("primary_fuel", "other_fuel1", "other_fuel2", "other_fuel3",
                                            "commissioning_year", "source", "url", "geolocation_source",
                                            "generation_data_source", "year_of_capacity_data")
                        if _clean(row.get(k))} | ({"reported_generation_gwh": reported} if reported else {}),
            locator={"url": url, "csv_line": index + 2, "gppd_idnr": row["gppd_idnr"]},
            unknowns=["status (GPPD publishes no per-plant status)"]))
    return records, carry


def _gem_status(row, release):
    published = _clean(row.get("Status"))
    if published is None:
        return None
    normalized = GEM_STATUSES.get(published.casefold(), "unknown")
    retired, start = _year(row.get("Retired year")), _year(row.get("Start year"))
    if normalized == "retired" and retired:
        return {"published": published, "normalized": normalized, "effective_date": retired,
                "effective_basis": "GEM retired year"}
    if normalized == "operating" and start:
        return {"published": published, "normalized": normalized, "effective_date": start,
                "effective_basis": "GEM start year"}
    return {"published": published, "normalized": normalized, "effective_date": None,
            "effective_basis": f"not stated by GEM; applies from the release that first published it ({release['label']})"}


def _wkt_line(text):
    match = _WKT_LINE.fullmatch(str(text or ""))
    if not match:
        return None
    points = []
    for pair in match.group(1).split(","):
        lon, lat = pair.split()
        points.append((lon, lat))
    precision, basis = ia.precision_from_decimals(*[v for p in points for v in p])
    return ({"type": "LineString", "coordinates": [[float(lon), float(lat)] for lon, lat in points]},
            {"crs_published": "EPSG:4326 (WKT route as published)", "precision_m": precision,
             "precision_basis": f"{basis}; GEM routes are approximate as published"})


def parse_gem(content, context, carry, *, url):
    selection = context["selection"]
    spec = GEM_TRACKERS[context["tracker"]]
    countries = set(_selection_countries(selection))
    max_rows = _limit(selection.get("max_rows"), 200, MAX_ROWS, "max_rows")
    release = _declared_release(selection, context["retrieved_at"], key_prefix=f"gem:{context['tracker']}")
    rows = _csv(content)
    if rows and not {spec["id"], "Status"} <= set(rows[0]):
        raise ProviderError("schema_drift", f"GEM {spec['title']} columns {spec['id']} and Status are required")
    selected = [(i, r) for i, r in enumerate(rows)
                if any(c.strip() in countries for c in str(r.get(spec["country"]) or "").split(","))]
    if len(selected) > max_rows:
        carry["gem_truncated"] = f"{len(selected)} rows in the selected countries; {max_rows} kept"
    records = []
    for index, row in selected[:max_rows]:
        identifiers = [{"scheme": spec["id_scheme"], "value": row[spec["id"]]}]
        if spec["location_id"] and _clean(row.get(spec["location_id"])):
            identifiers.append({"scheme": "gem_location_id", "value": row[spec["location_id"]].strip()})
        for part in str(row.get("Other IDs (location)") or row.get("Other IDs") or "").split(";"):
            if ":" in part:
                scheme, value = (p.strip() for p in part.split(":", 1))
                mapped = _OTHER_ID_SCHEMES.get(scheme.casefold())
                if mapped and value:
                    identifiers.append({"scheme": mapped, "value": value})
        if spec["geometry"] == "wkt":
            geometry, receipt = _wkt_line(row.get("Route")) or (None, None)
        else:
            accuracy = _clean(row.get("Location accuracy")) or "not stated"
            geometry, receipt = _point_geometry(_clean(row.get("Longitude")), _clean(row.get("Latitude")),
                                                crs="EPSG:4326 (latitude/longitude columns)",
                                                basis_note=f"GEM location accuracy: {accuracy}")
        capacities = []
        for column, unit_spec, metric in spec["capacity"]:
            if not _clean(row.get(column)):
                continue
            unit = row.get(unit_spec) if unit_spec in row else unit_spec
            capacities.append({"metric": metric, "value": _num(row.get(column), column), "unit": _clean(unit) or "unit not stated",
                               "effective_date": None, "effective_basis": f"as published in {release['label']}",
                               "published_text": row.get(column)})
        name = " ".join(p for p in (_clean(row.get(c)) for c in spec["name"]) if p) or None
        wiki = _clean(row.get("Wiki URL")) or _clean(row.get("WikiURL"))
        records.append(_build(
            "gem", f"gem:{context['tracker']}", row[spec["id"]], spec["asset_class"], name=name,
            source_url=url, attribution=f"{ATTRIBUTION['gem']}, {spec['title']}, {release['label']}",
            licence=LICENCES["gem"], release=release, retrieved_at=context["retrieved_at"],
            country=_clean(row.get(spec["country"])), identifiers=identifiers, geometry=geometry,
            geometry_receipt=receipt, status=_gem_status(row, release), capacities=capacities,
            owners=_owners(row.get("Owner"), "owner") + _owners(row.get("Parent"), "parent"),
            attributes={k: v for k, v in row.items() if k in {"Start year", "Retired year", "Location accuracy",
                                                              "Combustion technology", "Coal type", "Fuel",
                                                              "FacilityType", "LengthKnownKm"} and _clean(v)},
            cited_notes=[{"url": wiki, "label": "GEM wiki page (cited, not copied)"}] if wiki and wiki.startswith("https://")
            else [], locator={"url": url, "csv_line": index + 2, "tracker": context["tracker"]}))
    return records, carry


def _osm_class(tags):
    for (key, value), asset_class in OSM_CLASSES.items():
        if tags.get(key) == value:
            return asset_class, None, f"{key}={value}"
    for prefix, normalized in OSM_LIFECYCLE.items():
        for (key, value), asset_class in OSM_CLASSES.items():
            if tags.get(f"{prefix}:{key}") == value:
                return asset_class, normalized, f"{prefix}:{key}={value}"
    return None, None, None


def parse_overpass(content, context, carry, *, url):
    data = _json(content)
    if not isinstance(data, dict) or "elements" not in data:
        raise ProviderError("schema_drift", "Overpass JSON has no elements")
    stamp = str((data.get("osm3s") or {}).get("timestamp_osm_base") or "")
    if not stamp:
        raise ProviderError("schema_drift", "Overpass response states no timestamp_osm_base")
    stamp = stamp.replace(".000", "")
    release = {"key": f"osm-base:{stamp}", "released_at": stamp, "basis": "extract_timestamp",
               "label": f"Overpass extract, OSM base {stamp}"}
    carry["osm_timestamp_osm_base"] = stamp
    elements = data["elements"]
    if len(elements) >= int(context["limit"]):
        carry["osm_truncated"] = f"the extract returned the query limit of {context['limit']} elements"
    records, skipped = [], 0
    for element in elements:
        tags = {str(k): str(v) for k, v in (element.get("tags") or {}).items()}
        asset_class, lifecycle, matched = _osm_class(tags)
        if asset_class is None:
            skipped += 1
            continue
        kind, number = element["type"], str(element["id"])
        geometry, receipt, unknowns = None, None, []
        basis = "OSM stores coordinates at 7 decimal places; mapping accuracy is not stated"
        if kind == "node" and element.get("lat") is not None:
            geometry = {"type": "Point", "coordinates": [float(element["lon"]), float(element["lat"])]}
        elif kind == "way" and element.get("geometry"):
            points = [[float(p["lon"]), float(p["lat"])] for p in element["geometry"]]
            closed = len(points) >= 4 and points[0] == points[-1]
            geometry = ({"type": "Polygon", "coordinates": [points]}
                        if closed and asset_class in {"power_plant", "substation", "lng_terminal"}
                        else {"type": "LineString", "coordinates": points})
        else:
            unknowns.append("geometry (relation members are not assembled)")
        if geometry is not None:
            receipt = {"crs_published": "EPSG:4326 (OSM)", "precision_m": 0.006, "precision_basis": basis}
        status = None
        if lifecycle:
            status = {"published": matched, "normalized": lifecycle, "effective_date": None,
                      "effective_basis": "OSM lifecycle prefix; no date stated"}
        capacities = []
        for tag, metric in OSM_CAPACITY_TAGS.items():
            if tag not in tags:
                continue
            value, unit = _quantity(tags[tag])
            if tag == "voltage" and value is not None:
                unit = unit or "V"
            capacities.append({"metric": metric, "value": value, "unit": unit or "as published",
                               "effective_date": None, "effective_basis": "OSM tag (no date stated)",
                               "published_text": tags[tag]})
        identifiers = [{"scheme": "osm_element", "value": f"{kind}/{number}"}]
        for tag, scheme in OSM_IDENTIFIER_TAGS.items():
            if tags.get(tag):
                identifiers.append({"scheme": scheme, "value": tags[tag]})
        owners = []
        for role in ("operator", "owner"):
            if tags.get(role):
                owner_ids = [{"scheme": "wikidata", "value": tags[f"{role}:wikidata"]}] \
                    if tags.get(f"{role}:wikidata") else []
                owners.append({"role": role, "name": tags[role], "share": None, "share_text": None,
                               "identifiers": owner_ids})
        records.append(_build(
            "osm", "osm:overpass", f"{kind}/{number}", asset_class, name=tags.get("name"),
            source_url=f"https://www.openstreetmap.org/{kind}/{number}", attribution=ATTRIBUTION["osm"],
            licence=LICENCES["osm"], release=release, retrieved_at=context["retrieved_at"], country=None,
            identifiers=identifiers, geometry=geometry, geometry_receipt=receipt, status=status,
            capacities=capacities, owners=owners, attributes={"tags": tags},
            element={"type": kind, "id": number, "version": str(element.get("version")),
                     "changeset": str(element.get("changeset")), "timestamp": element.get("timestamp")},
            locator={"url": url, "query_area": context.get("area"), "matched_tag": matched},
            unknowns=unknowns + ["status (OSM states no lifecycle for mapped features without a prefix)"]
            if not lifecycle else unknowns))
    if skipped:
        carry["osm_skipped"] = f"{skipped} elements outside the declared tag classes"
    return records, carry


def parse_eia(content, context, carry, *, url):
    data = _json(content)
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        raise ProviderError("schema_drift", "EIA layer query did not return a GeoJSON FeatureCollection")
    layer = context["layer"]
    spec = EIA_LAYERS[layer]
    features = data.get("features") or []
    if len(features) >= int(context["max_records"]) or data.get("exceededTransferLimit") in {True, "true"}:
        carry["eia_truncated"] = f"the layer returned its record limit ({context['max_records']})"
    release = _declared_release(context["selection"], context["retrieved_at"], key_prefix=f"eia:{layer}")
    records = []
    for feature in features:
        props = {str(k): v for k, v in (feature.get("properties") or {}).items()}
        geometry = feature.get("geometry")
        geom, receipt = None, None
        if geometry and geometry.get("type") == "Point":
            geom, receipt = _point_geometry(geometry["coordinates"][0], geometry["coordinates"][1],
                                            crs="EPSG:4326 (outSR=4326)", basis_note="layer states no positional accuracy")
        elif geometry and geometry.get("type") in {"LineString", "MultiLineString"}:
            coords = geometry["coordinates"]
            flat = coords if geometry["type"] == "LineString" else [p for line in coords for p in line]
            precision, basis = ia.precision_from_decimals(*[v for p in flat for v in p])
            geom = {"type": geometry["type"], "coordinates": _floats(coords)}
            receipt = {"crs_published": "EPSG:4326 (outSR=4326)", "precision_m": precision,
                       "precision_basis": f"{basis}; generalised network geometry as published"}
        identifiers, owners, capacities, status, refs = [], [], [], None, []
        if layer == "power-plants":
            native = f"plant:{props.get('Plant_Code')}"
            identifiers.append({"scheme": "eia_plant_code", "value": str(props.get("Plant_Code"))})
            name = _clean(props.get("Plant_Name"))
            if _clean(props.get("Utility_Name")):
                utility_ids = [{"scheme": "eia_utility_id", "value": str(props["Utility_ID"])}] \
                    if _clean(props.get("Utility_ID")) else []
                owners.append({"role": "operator", "name": props["Utility_Name"], "share": None, "share_text": None,
                               "identifiers": utility_ids})
            if _clean(props.get("Total_MW")):
                capacities.append({"metric": "electrical_capacity", "value": _num(props["Total_MW"], "Total_MW"),
                                   "unit": "MW", "effective_date": _eia_period(props.get("Period")),
                                   "effective_basis": "layer Period field" if _eia_period(props.get("Period")) else None,
                                   "published_text": str(props["Total_MW"])})
        else:
            native = f"{layer}:{props.get('OBJECTID')}"
            identifiers.append({"scheme": f"eia_{layer.replace('-', '_')}_objectid", "value": str(props.get("OBJECTID"))})
            name = _clean(props.get("Name")) or _clean(props.get("Pipename"))
            company = _clean(props.get("Company")) or _clean(props.get("Operator"))
            if company:
                owners.append({"role": "operator", "name": company, "share": None, "share_text": None})
            if layer == "lng-terminals":
                if _clean(props.get("Capacity_Bcfd")):
                    capacities.append({"metric": "throughput_capacity", "value": _num(props["Capacity_Bcfd"], "Capacity"),
                                       "unit": "Bcf/d", "effective_date": None, "effective_basis": None,
                                       "direction": {"export": "exit", "import": "entry"}.get(
                                           str(props.get("Type") or "").casefold()),
                                       "published_text": str(props["Capacity_Bcfd"])})
                if _clean(props.get("Status")):
                    status = {"published": props["Status"], "normalized": EIA_STATUSES.get(
                        str(props["Status"]).casefold(), "unknown"), "effective_date": None,
                              "effective_basis": "not stated by the layer"}
                if _clean(props.get("Docket")):
                    refs.append({"scheme": "ferc-docket", "identifier": str(props["Docket"]).strip(),
                                 "relation": "authorised_under", "locator": "layer field Docket"})
        records.append(_build(
            "eia", f"eia:{layer}", native, spec["asset_class"], name=name, source_url=DATASET_URLS["eia"],
            attribution=f"{ATTRIBUTION['eia']}, {spec['title']} layer", licence=LICENCES["eia"], release=release,
            retrieved_at=context["retrieved_at"], country="US", identifiers=identifiers, geometry=geom,
            geometry_receipt=receipt, status=status, capacities=capacities, owners=owners,
            attributes={k: v for k, v in props.items() if k in {"PrimSource", "State", "County", "Source", "Period",
                                                                "Type", "TYPEPIPE"} and _clean(v)},
            cited_references=refs, locator={"url": url, "layer": layer, "objectid": str(props.get("OBJECTID"))}))
    return records, carry


def _floats(value):
    if isinstance(value, list):
        return [_floats(v) for v in value]
    return float(value)


def _eia_period(value):
    text = str(value or "").strip()
    return f"{text[:4]}-{text[4:6]}" if re.fullmatch(r"\d{6}", text) else None


def parse_entsog_points(content, context, carry, *, url):
    data = _json(content)
    points = data.get("connectionpoints") if isinstance(data, dict) else None
    if not isinstance(points, list):
        raise ProviderError("schema_drift", "ENTSOG connectionpoints response has no connectionpoints list")
    carry["entsog_points"] = {str(p["pointKey"]): {k: p.get(k) for k in ("pointKey", "pointLabel", "pointType",
                                                                         "tSOCountry", "lastUpdateDateTime")}
                              for p in points if p.get("pointKey")}
    requested = set(context["selection"].get("point_keys") or [])
    missing = sorted(requested - set(carry["entsog_points"]))
    if missing:
        carry["entsog_no_data"] = f"no connection point published for {missing}"
    return [], carry


def parse_entsog_capacity(content, context, carry, *, url):
    data = _json(content)
    rows = data.get("operationaldatas") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ProviderError("schema_drift", "ENTSOG operationaldatas response has no operationaldatas list")
    points = carry.get("entsog_points") or {}
    by_point: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("indicator") != "Firm Technical":
            continue
        by_point.setdefault(str(row.get("pointKey")), []).append(row)
    records = []
    for key, point in sorted(points.items()):
        capacity_rows = by_point.get(key, [])
        updates = sorted(str(r.get("lastUpdateDateTime")) for r in capacity_rows if r.get("lastUpdateDateTime"))
        updates += [str(point["lastUpdateDateTime"])] if point.get("lastUpdateDateTime") else []
        stamp = max(updates) if updates else None
        release = ({"key": f"entsog:{key}:{stamp}", "released_at": stamp, "basis": "provider_last_update",
                    "label": f"ENTSOG last update {stamp}"} if stamp else
                   {"key": f"entsog:{key}:retrieved:{context['retrieved_at']}", "released_at": None,
                    "basis": "retrieval_time", "label": None})
        capacities, operators = [], {}
        for row in capacity_rows:
            capacities.append({"metric": "firm_technical_capacity", "value": _num(row.get("value"), "value"),
                               "unit": str(row.get("unit") or "unit not stated"),
                               "direction": {"entry": "entry", "exit": "exit"}.get(str(row.get("directionKey")).casefold()),
                               "period": f"{row.get('periodFrom')}/{row.get('periodTo')}",
                               "effective_date": str(row.get("periodFrom") or "")[:10] or None,
                               "effective_basis": "periodFrom", "published_text": str(row.get("value"))})
            if row.get("operatorKey"):
                operators[str(row["operatorKey"])] = str(row.get("operatorLabel") or row["operatorKey"])
        records.append(_build(
            "entsog", "entsog:connectionpoints", key, "gas_point", name=_clean(point.get("pointLabel")),
            source_url=f"{ENTSOG_API}/connectionpoints?pointKey={key}", attribution=ATTRIBUTION["entsog"],
            licence=LICENCES["entsog"], release=release, retrieved_at=context["retrieved_at"],
            country=_clean(point.get("tSOCountry")), identifiers=[{"scheme": "entsog_point_key", "value": key}],
            capacities=capacities,
            owners=[{"role": "operator", "name": label, "share": None, "share_text": None,
                     "identifiers": [{"scheme": "entsog_operator_key", "value": op}]}
                    for op, label in sorted(operators.items())],
            attributes={"pointType": point.get("pointType")} if point.get("pointType") else {},
            locator={"url": url, "pointKey": key},
            unknowns=["geometry (ENTSOG publishes no WGS84 coordinates; never geocoded)"]))
    return records, carry


PARSERS = {"gppd": parse_gppd, "gem": parse_gem, "overpass": parse_overpass, "eia": parse_eia,
           "entsog_points": parse_entsog_points, "entsog_capacity": parse_entsog_capacity}


def parse_step(step, content, carry, *, retrieved_at, selection=None):
    context = {**dict(step.get("context") or {}), "params": step.get("params"), "retrieved_at": retrieved_at,
               "selection": dict(selection or {})}
    records, carry = PARSERS[step["parse"]](content, context, dict(carry or {}), url=step["url"])
    try:
        return [ia.validate(r) for r in records], carry
    except ia.InfrastructureError as exc:
        raise ProviderError("schema_drift", f"record failed validation: {exc}") from exc


def coverage(provider, carry, records):
    notes = {k: v for k, v in carry.items()
             if k.endswith(("no_data", "truncated", "skipped", "timestamp_osm_base")) and v}
    return {"complete": False, "provider": provider, "records": len(records), "notes": notes,
            "basis": "explicit bounded selection; never comprehensive publisher coverage"}


# ------------------------------------------------------------- acquisition


def acquire(conn, provider, selection, *, namespace, scopes, principal_id, fetch, run_id=None, execution="injected",
            now=None):
    """Run one bounded selection step by step; each step is receipted, failures change no stored value.

    ``fetch(url=..., params=..., headers=...)`` returns ``{"status", "headers", "content"}``: a fixture
    transport offline, a DurableHTTP-backed fetch (:func:`durable_fetch`) live.
    """

    store = ia.InfrastructureStore(conn, now=now)
    steps = plan(provider, selection)
    for step in steps:
        if urlsplit(step["url"]).hostname not in PROVIDER_HOSTS[provider]:
            raise ProviderError("network_policy", "infrastructure requests stay on the publisher's declared hosts")
    run_id = run_id or "infra-run:" + ia.digest([namespace, provider, selection, store.now()])[:24]
    carry, results, failures = {}, [], []
    counts = {"assets": 0, "revisions": 0, "unchanged": 0, "status_revisions": 0, "capacity_revisions": 0}
    for index, step in enumerate(steps):
        request = {"url": step["url"], "params": dict(step["params"]), "parse": step["parse"], "step": index}
        try:
            response = fetch(url=step["url"], params=dict(step["params"]), headers={"Accept": "*/*"})
            status = int(response.get("status", 200))
            raw = response.get("content", b"")
            raw = raw.encode() if isinstance(raw, str) else bytes(raw)
            if status == 429:
                raise ProviderError("rate_limited", f"{provider} rate limit reached")
            if status in {401, 403}:
                raise ProviderError("access_denied", f"{provider} refused access (HTTP {status})")
            if status >= 400:
                raise ProviderError("source_unavailable" if status >= 500 else "schema_drift",
                                    f"{provider} returned HTTP {status}")
            retrieved = ia.iso(store.now())
            records, carry = parse_step(step, raw, carry, retrieved_at=retrieved, selection=selection)
        except ProviderError as exc:
            failures.append(store.fail(namespace, provider, exc, source=f"{provider}:{step['parse']}", request=request,
                                       run_id=run_id, principal_id=principal_id, execution=execution))
            break  # later steps depend on earlier ones (e.g. ENTSOG points before capacities)
        receipt_id = store.receipt(namespace, provider=provider, source=f"{provider}:{step['parse']}", request=request,
                                   response_sha256=hashlib.sha256(raw).hexdigest(), size=len(raw), status="ok",
                                   execution=execution, run_id=run_id, records=len(records), principal_id=principal_id,
                                   coverage=coverage(provider, carry, records))
        applied = store.apply(namespace, records, receipt_id=receipt_id, run_id=run_id, principal_id=principal_id,
                              scopes=scopes, execution=execution)
        for key in counts:
            counts[key] += applied[key]
        results.append({"receipt_id": receipt_id, "records": len(records), **applied})
    return {"ok": not failures, "provider": provider, "run_id": run_id, "steps": len(steps), "applied": counts,
            "receipts": results, "failures": failures, "coverage": coverage(provider, carry, []),
            "live_verification": LIVE_VERIFICATION[provider]["status"]}


def durable_fetch(http, *, principal_id, observation):
    """A ``fetch`` over :class:`~src.ingestion.provider_execution.DurableHTTP` (exact hosts, budget, receipts)."""

    counter = {"n": 0}

    def fetch(*, url, params, headers):
        counter["n"] += 1
        captured = http.request(f"{observation}:{counter['n']}", url, principal_id=principal_id, params=params,
                                headers=headers, max_bytes=20_000_000)
        return {"status": 200, "headers": {}, "content": captured.content}

    return fetch


# ------------------------------------------------------- source-pack adapter


def runtime_record(record, *, response_sha256):
    return {"id": f"{record['provider']}:{record['dataset']}:{record['native_id']}:{record['release']['key']}",
            "title": record["name"] or record["native_id"], "language": "und", "url": record["source_url"],
            "infrastructure_record": record, "response_sha256": response_sha256}


class InfrastructureSourceAdapter:
    """``infrastructure`` native connector: one plan step per runtime page."""

    accepts_transport = True

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None, now: Callable[[], int] | None = None) -> None:
        from functools import partial

        from src.ingestion.source_pack_runtime import HTTPSPageAdapter
        from src.ingestion.source_packs import SourcePackError

        del secret  # every infrastructure source is unauthenticated
        self.source = json.loads(json.dumps(source))
        spec = dict(self.source.get("infrastructure") or {})
        self.provider = str(spec.get("provider") or "")
        self.selection = dict(spec.get("selection") or {})
        try:
            self.steps = plan(self.provider, self.selection)
        except ProviderError as exc:
            raise SourcePackError("unbounded_source", str(exc)) from exc
        for step in self.steps:
            if urlsplit(step["url"]).hostname not in PROVIDER_HOSTS[self.provider]:
                raise SourcePackError("network_policy", "infrastructure requests stay on the publisher's declared hosts")
        self.now = now
        self.transport = transport or partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "infrastructure": {"provider": self.provider, "steps": len(self.steps),
                               "live_verification": LIVE_VERIFICATION[self.provider]["status"]},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _scope(self) -> str:
        return ia.digest({"source_hash": self.source["source_hash"], "steps": self.steps})

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        import time

        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms
        from src.ingestion.source_packs import SourcePackError

        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "infrastructure runs use the pinned selection")
        state = {} if cursor is None else json.loads(cursor)
        if cursor is not None and state.get("scope") != self._scope():
            raise SourcePackError("cursor_drift", "cursor belongs to a different selection")
        index = int(state.get("i", 0))
        if index >= len(self.steps):
            return RuntimePage((), None, 0, receipt={"status": 200})
        step = self.steps[index]
        response = self.transport(url=step["url"], params=dict(step["params"]), headers={"Accept": "*/*"},
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
        if status >= 500:
            raise SourcePackError("source_unavailable", f"{self.provider} returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"{self.provider} returned HTTP {status}")
        response_sha256 = hashlib.sha256(raw).hexdigest()
        clock = self.now or (lambda: int(time.time() * 1000))
        retrieved = datetime.fromtimestamp(clock() / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            records, carry = parse_step(step, raw, state.get("carry") or {}, retrieved_at=retrieved,
                                        selection=self.selection)
        except ProviderError as exc:
            raise SourcePackError("schema_drift", str(exc)) from exc
        more = index + 1 < len(self.steps)
        next_cursor = (json.dumps({"i": index + 1, "scope": self._scope(), "carry": carry}, sort_keys=True)
                       if more else None)
        return RuntimePage(tuple(runtime_record(r, response_sha256=response_sha256) for r in records), next_cursor,
                           len(raw), receipt={"status": status, "step": index, "steps": len(self.steps),
                                              "parse": step["parse"], "response_sha256": response_sha256,
                                              "provider": self.provider,
                                              "request": {"url": step["url"], "params": dict(step["params"])},
                                              "coverage": coverage(self.provider, carry, records)})


ADAPTERS = {"infrastructure": InfrastructureSourceAdapter}


def _fixture_body(page):
    if "body_base64" in page:
        return base64.b64decode(page["body_base64"])
    body = page.get("body")
    return json.dumps(body).encode() if isinstance(body, (dict, list)) else (body or "").encode()


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Serve authored native pages keyed by URL + query."""

    by_key = {(page["url"], ia.canonical(dict(page.get("params") or {}))): page for page in pages}

    def transport(*, url, params, headers, timeout=None, **_):
        del headers, timeout
        page = by_key.get((url, ia.canonical(dict(params or {}))))
        if page is None:
            return {"status": 404, "headers": {}, "content": b""}
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": _fixture_body(page)}

    return transport


def fixture_clock(fixture: Mapping[str, Any]) -> Callable[[], int]:
    """The fixture's declared retrieval time (deterministic replay)."""

    stamp = datetime.fromisoformat(str(fixture.get("retrieved_at") or "2026-09-29T00:00:00Z").replace("Z", "+00:00"))
    return lambda: int(stamp.timestamp() * 1000)


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = InfrastructureSourceAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                                          now=fixture_clock(fixture))
    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {}}, cursor=cursor)
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records
