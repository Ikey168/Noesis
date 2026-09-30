"""Build the pinned water source-pack fixtures and manifest from the authored payloads below.

Every payload is authored in the provider's documented response shape. Station
UUIDs, numbers, names, monitoring-location ids, EU water-body codes,
coordinates, values and statuses are FICTIONAL ("Exampla", "Northwind",
"Nordfluss") and are not live evidence. Run
``python -m tests.unit.water.fixture_builder`` after editing a payload;
``test_fixtures_are_pinned_and_in_sync`` fails when they drift.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "packs/climate-environment/source_packs/climate-environment-water.json"
OUT = ROOT / "tests/fixtures/source_packs"
LATER = ROOT / "tests/fixtures/water/later_payloads.json"
NOTE = ("Authored offline fixture in the provider's documented response shape. Stations, identifiers, codes, "
        "coordinates, values and statuses are FICTIONAL and ILLUSTRATIVE, not live evidence; verify against the live "
        "sources before any live run (WA13, #2647).")


def request(path, query=None):
    return path + ("?" + urlencode(sorted(dict(query).items())) if query else "")


# ------------------------------------------------------------------ PEGELONLINE

PO = "/webservices/rest-api/v2"
EXAMPLA, NORTHWIND = "aaaa1111-0000-4000-8000-00000000e001", "aaaa1111-0000-4000-8000-00000000e002"
START, END = "2026-09-20T00:00:00+02:00", "2026-09-20T02:00:00+02:00"
WINDOW = {"start": START, "end": END}
STATION_QUERY = {"includeTimeseries": "true", "includeCharacteristicValues": "true"}


def po_station(uuid, number, name, lon, lat, zero, series=("W",), *, valid_from="2019-11-01"):
    timeseries = []
    for code in series:
        item = {"shortname": code, "longname": {"W": "WASSERSTAND ROHDATEN", "Q": "ABFLUSS ROHDATEN"}[code],
                "unit": {"W": "cm", "Q": "m3/s"}[code], "equidistance": 15}
        if code == "W":
            item["gaugeZero"] = {"unit": "m. ü. NHN", "value": zero, "validFrom": valid_from}
            item["characteristicValues"] = [
                {"shortname": "MW", "longname": "Mittelwasser", "unit": "cm", "value": 320,
                 "validFrom": "2021-01-01", "timespanStart": "2010-11-01", "timespanEnd": "2020-10-31"},
                {"shortname": "MHW", "longname": "Mittleres Hochwasser", "unit": "cm", "value": 520,
                 "validFrom": "2021-01-01", "timespanStart": "2010-11-01", "timespanEnd": "2020-10-31"}]
        timeseries.append(item)
    return {"uuid": uuid, "number": number, "shortname": name, "longname": name, "km": 12.3 if uuid == EXAMPLA else 47.8,
            "agency": "WSA EXAMPLA (FIXTURE)", "longitude": lon, "latitude": lat,
            "water": {"shortname": "NORDFLUSS", "longname": "NORDFLUSS"}, "timeseries": timeseries}


def po_values(values):
    return [{"timestamp": t, "value": v} for t, v in values]


EXAMPLA_W = [("2026-09-20T00:00:00+02:00", 512.0), ("2026-09-20T00:15:00+02:00", 515.0),
             ("2026-09-20T00:30:00+02:00", 519.0),
             # 00:45 is not published: the gap stays missing
             ("2026-09-20T01:00:00+02:00", 524.0), ("2026-09-20T01:15:00+02:00", 530.0),
             ("2026-09-20T01:30:00+02:00", 541.0)]
EXAMPLA_Q = [("2026-09-20T00:00:00+02:00", 145.2), ("2026-09-20T00:15:00+02:00", 146.0),
             ("2026-09-20T00:30:00+02:00", 147.1), ("2026-09-20T01:00:00+02:00", 149.9)]
NORTHWIND_W = [("2026-09-20T00:00:00+02:00", 402.0), ("2026-09-20T00:15:00+02:00", 403.0),
               ("2026-09-20T00:30:00+02:00", 404.0)]


def po_pages(exampla_w=EXAMPLA_W, exampla_zero=(30.12, "2019-11-01"), northwind_lat=52.40):
    return [
        {"request": request(f"{PO}/stations/{EXAMPLA}.json", STATION_QUERY), "status": 200,
         "body": po_station(EXAMPLA, "59990001", "EXAMPLA", 13.40, 52.52, exampla_zero[0], ("W", "Q"),
                            valid_from=exampla_zero[1])},
        {"request": request(f"{PO}/stations/{EXAMPLA}/W/measurements.json", WINDOW), "status": 200,
         "body": po_values(exampla_w)},
        {"request": request(f"{PO}/stations/{EXAMPLA}/Q/measurements.json", WINDOW), "status": 200,
         "body": po_values(EXAMPLA_Q)},
        {"request": request(f"{PO}/stations/{NORTHWIND}.json", STATION_QUERY), "status": 200,
         "body": po_station(NORTHWIND, "59990002", "NORTHWIND BRÜCKE", 13.60, northwind_lat, 28.50,
                            valid_from="2020-04-01")},
        {"request": request(f"{PO}/stations/{NORTHWIND}/W/measurements.json", WINDOW), "status": 200,
         "body": po_values(NORTHWIND_W)},
    ]


PO_SELECTION = [
    {"kind": "station", "uuid": EXAMPLA, "number": "59990001", "timeseries": ["W", "Q"], **WINDOW,
     "label": "EXAMPLA on the NORDFLUSS, water level and discharge"},
    {"kind": "station", "uuid": NORTHWIND, "number": "59990002", "timeseries": ["W"], **WINDOW,
     "label": "NORTHWIND BRÜCKE on the NORDFLUSS, water level"},
]

# ------------------------------------------------------------------ USGS

OGC = "/ogcapi/v0/collections"
CREEK, RIVER = "USGS-99990001", "USGS-99990002"
CREEK_QUERY = {"monitoring_location_id": CREEK, "parameter_code": "00060", "statistic_id": "00003",
               "time": "2026-09-01/2026-09-06", "limit": "100", "f": "json"}
RIVER_QUERY = {"monitoring_location_id": RIVER, "parameter_code": "00065", "statistic_id": "00003",
               "time": "2026-09-01/2026-09-03", "limit": "100", "f": "json"}


def location(ident, number, name, lon, lat):
    return {"type": "Feature", "id": ident, "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {"agency_code": "USGS", "monitoring_location_number": number,
                           "monitoring_location_name": name, "site_type": "Stream", "state_name": "Maryland",
                           "country_code": "US", "hydrologic_unit_code": "020700089999", "altitude": 250.0,
                           "vertical_datum": "NAVD88", "drainage_area": 11.2}}


def daily(ident, code, day, value, status, qualifier=None, modified="2026-09-07T12:00:00Z"):
    return {"type": "Feature", "id": f"{ident}-{code}-{day}",
            "properties": {"time_series_id": f"ts-{ident}-{code}", "monitoring_location_id": ident,
                           "parameter_code": code, "statistic_id": "00003", "time": day, "value": value,
                           "unit_of_measure": {"00060": "ft^3/s", "00065": "ft"}[code], "approval_status": status,
                           "qualifier": qualifier, "last_modified": modified}}


CREEK_VALUES = [daily(CREEK, "00060", "2026-09-01", "12.5", "Approved"),
                daily(CREEK, "00060", "2026-09-02", "12.9", "Approved"),
                daily(CREEK, "00060", "2026-09-03", "13.4", "Provisional"),
                daily(CREEK, "00060", "2026-09-04", "14.1", "Provisional", ["e"]),
                # 2026-09-05 is not published: the gap stays missing
                daily(CREEK, "00060", "2026-09-06", "15.0", "Provisional")]
RIVER_VALUES = [daily(RIVER, "00065", "2026-09-01", "3.21", "Provisional"),
                daily(RIVER, "00065", "2026-09-02", "3.25", "Provisional"),
                daily(RIVER, "00065", "2026-09-03", "3.30", "Provisional")]


def collection(features):
    return {"type": "FeatureCollection", "features": features, "numberReturned": len(features),
            "links": [{"rel": "self", "href": "https://api.waterdata.usgs.gov/ogcapi/v0/collections/daily/items"}]}


def usgs_pages(creek_values=CREEK_VALUES):
    return [
        {"request": request(f"{OGC}/monitoring-locations/items/{CREEK}", {"f": "json"}), "status": 200,
         "body": location(CREEK, "99990001", "EXAMPLE CREEK NEAR NORTHWIND, MD (FIXTURE)", -76.90, 39.00)},
        {"request": request(f"{OGC}/monitoring-locations/items/{RIVER}", {"f": "json"}), "status": 200,
         "body": location(RIVER, "99990002", "EXAMPLA RIVER AT SAMPLE TOWN, MD (FIXTURE)", -77.10, 39.10)},
        {"request": request(f"{OGC}/daily/items", CREEK_QUERY), "status": 200, "body": collection(creek_values)},
        {"request": request(f"{OGC}/daily/items", RIVER_QUERY), "status": 200, "body": collection(RIVER_VALUES)},
    ]


USGS_SELECTION = [
    {"kind": "monitoring_location", "id": CREEK, "label": "Example Creek near Northwind (fixture)"},
    {"kind": "monitoring_location", "id": RIVER, "label": "Exampla River at Sample Town (fixture)"},
    {"kind": "daily", "monitoring_location_id": CREEK, "parameter_code": "00060", "statistic_id": "00003",
     "time": "2026-09-01/2026-09-06", "limit": 100, "label": "daily mean discharge, one week"},
    {"kind": "daily", "monitoring_location_id": RIVER, "parameter_code": "00065", "statistic_id": "00003",
     "time": "2026-09-01/2026-09-03", "limit": 100, "label": "daily mean gage height"},
]

# ------------------------------------------------------------------ EEA WISE

WB1, WB2, WB3 = "DEFX_EXAMPLA_01", "DEFX_EXAMPLA_02", "DEFX_NORTHWIND_03"
CODES = [WB1, WB2, WB3]
GEOMETRY = {"kind": "water_body_geometry", "eu_codes": CODES, "label": "published geometry vintage"}
STATUS = {"kind": "water_body_status", "eu_codes": CODES, "cycles": ["2016", "2022"],
          "label": "status per reporting cycle"}
WISE_SELECTION = [GEOMETRY, STATUS]


def wise_request(entry):
    from src.ingestion.water_sources import requests_for

    (_role, path, query), = requests_for("eea-wise", entry)
    return request(path, query)


def wise_row(code, name, category, year, eco, chem, *, natural="Natural", eco_year=None, chem_year=None):
    return {"countryCode": "DE", "euRBDCode": "DEFX9990", "rbdName": "Nordfluss basin (fixture)",
            "euSurfaceWaterBodyCode": code, "surfaceWaterBodyName": name, "surfaceWaterBodyCategory": category,
            "naturalAWBHMWB": natural, "cYear": year, "swEcologicalStatusOrPotentialValue": eco,
            "swEcologicalAssessmentYear": eco_year or str(int(year) - 1), "swChemicalStatusValue": chem,
            "swChemicalAssessmentYear": chem_year or str(int(year) - 1)}


STATUS_ROWS = [
    wise_row(WB1, "Nordfluss Exampla reach (fixture)", "RW", 2016, "3", "3"),
    wise_row(WB1, "Nordfluss Exampla reach (fixture)", "RW", 2022, "4", "3"),
    wise_row(WB2, "Exampla lake (fixture)", "LW", 2016, "2", "2"),
    wise_row(WB2, "Exampla lake (fixture)", "LW", 2022, "Unknown", "3"),
]
LATER_ROWS = STATUS_ROWS + [wise_row(WB3, "Northwind canalised reach (fixture)", "RW", 2022, "3", "2",
                                     natural="Heavily modified")]
# A published water-body geometry (EPSG:4326) inside the Exampla district; WB2 and WB3 publish none.
WB1_GEOMETRY = {"type": "LineString", "coordinates": [[13.38, 52.51], [13.41, 52.52], [13.44, 52.53]]}


def wise_pages(rows=STATUS_ROWS):
    return [
        {"request": wise_request(GEOMETRY), "status": 200,
         "body": {"results": [{"euSurfaceWaterBodyCode": WB1, "cYear": 2022,
                               "geometryGeoJSON": json.dumps(WB1_GEOMETRY)},
                              {"euSurfaceWaterBodyCode": WB2, "cYear": 2022, "geometryGeoJSON": None}]}},
        {"request": wise_request(STATUS), "status": 200, "body": {"results": rows}},
    ]


# ------------------------------------------------------------------ later acquisition


def later_payloads():
    """A later acquisition: gauge zero and location changes, a raw value corrected and a gap published, USGS values
    approved and revised and one provisional value withdrawn, and a new reporting cycle for a water body."""
    exampla_w = [(t, 540.0 if t.endswith("01:30:00+02:00") else v) for t, v in EXAMPLA_W]
    exampla_w.append(("2026-09-20T01:45:00+02:00", 548.0))
    po = {p["request"]: {"body": p["body"]} for p in po_pages(exampla_w, (30.02, "2026-09-25"), 52.401)}
    creek = [CREEK_VALUES[0], CREEK_VALUES[1],
             daily(CREEK, "00060", "2026-09-03", "13.4", "Approved", modified="2026-09-28T09:00:00Z"),
             daily(CREEK, "00060", "2026-09-04", "14.0", "Approved", modified="2026-09-28T09:00:00Z")]
    usgs = {p["request"]: {"body": p["body"]} for p in usgs_pages(creek)}
    wise = {p["request"]: {"body": p["body"]} for p in wise_pages(LATER_ROWS)}
    return {"description": NOTE + " A later acquisition of the same selections.",
            "pegelonline": po, "usgs": usgs, "eea-wise": wise}


# ------------------------------------------------------------------ manifest

DEFAULTS = {
    "connector": "water",
    "update_cadence": "hourly re-check of the declared PEGELONLINE windows, daily for USGS daily values and monthly "
                      "for the WISE reporting cycles; every refresh stays inside the declared selection",
    "temporal_semantics": "observations carry their published timestamp (with offset) or day and a quality state as "
                          "published; stations and water bodies are revisions by retrieval time; assessments belong "
                          "to one WFD reporting cycle; every distinct payload is an immutable revision and absence "
                          "from a later complete USGS window is a dated tombstone",
    "mapping": {"target_schema": "noesis-water-record-v1", "version": "1.0.0"},
    "extractor_versions": ["water-sources:1.0.0"],
    "operations": ["water"],
    "schedule": {"kind": "interval", "interval_s": 3600},
    "auth": {"kind": "none"},
    "health": {"required": False, "max_staleness_s": 604800},
    "budgets": {"timeout_ms": 30000, "max_results": 200, "max_bytes": 2000000, "max_pages": 10},
    "policy": {"excluded": ["flood forecasting", "interpolation, resampling or gap filling",
                            "own water-body status assessments or merged cycles", "flood-risk scoring",
                            "personal data"],
               "values": "as published, with quality state and qualifiers"},
}
SOURCES = [
    {"source_id": "pegelonline-stations-levels", "endpoint": "https://www.pegelonline.wsv.de",
     "publisher": "WSV PEGELONLINE",
     "scope": "Two declared gauging stations on one water (station UUID and number), their gauge zero with validity "
              "and published characteristic values, and one bounded window of raw water-level and discharge "
              "measurements per declared time series",
     "license": {"id": "dl-de-zero-2.0", "terms_url": "https://www.pegelonline.wsv.de/gast/nutzungsbedingungen",
                 "redistribution": "permitted without restriction (Datenlizenz Deutschland Zero 2.0); attribution "
                                   "kept for citation"},
     "water": {"provider": "pegelonline", "namespace": "environment", "live_verification": "unverified-live",
               "coverage_decision": "two stations, W and Q, one 2-hour window each", "selection": PO_SELECTION},
     "fixture": {"path": "tests/fixtures/source_packs/water-pegelonline.json"}},
    {"source_id": "usgs-water-data", "endpoint": "https://api.waterdata.usgs.gov",
     "publisher": "U.S. Geological Survey Water Data APIs",
     "scope": "Two declared monitoring locations and one week of daily mean discharge or gage height each, with "
              "approval status and qualifiers per value",
     "license": {"id": "us-public-domain",
                 "terms_url": "https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
                 "redistribution": "permitted (U.S. public domain); cite USGS; provisional-data disclaimer kept"},
     "auth": {"kind": "optional-secret", "secret_ref": "NOESIS_USGS_WATER_API_KEY"},
     "water": {"provider": "usgs", "namespace": "environment", "live_verification": "unverified-live",
               "coverage_decision": "two monitoring locations, one week of daily values each, at most 100 values "
                                    "per page", "selection": USGS_SELECTION},
     "fixture": {"path": "tests/fixtures/source_packs/water-usgs.json"}},
    {"source_id": "eea-wise-wfd-status", "endpoint": "https://discodata.eea.europa.eu",
     "publisher": "European Environment Agency WISE WFD database",
     "scope": "Three declared surface water bodies (EU codes), their published geometry vintage and ecological and "
              "chemical status per WFD reporting cycle (2016, 2022) as reported",
     "license": {"id": "cc-by-4.0", "terms_url": "https://www.eea.europa.eu/en/legal-notice",
                 "redistribution": "permitted with attribution to the EEA and the reporting Member State"},
     "water": {"provider": "eea-wise", "namespace": "environment", "live_verification": "unverified-live",
               "coverage_decision": "three water bodies, two reporting cycles", "selection": WISE_SELECTION},
     "fixture": {"path": "tests/fixtures/source_packs/water-eea-wise.json"}},
]
FIXTURES = {
    "pegelonline-stations-levels": ("PEGELONLINE REST API v2 stations and measurements", po_pages, "pegelonline",
                                    ["two stations on one water", "gauge zero with validity",
                                     "characteristic values MW and MHW", "a 15-minute value not published (gap)",
                                     "water level and discharge"]),
    "usgs-water-data": ("USGS OGC API monitoring locations and daily values", usgs_pages, "usgs",
                        ["approved and provisional values", "an estimated qualifier", "a missing day"]),
    "eea-wise-wfd-status": ("EEA Discodata WISE WFD geometry and status rows", wise_pages, "eea-wise",
                            ["two reporting cycles", "an unknown ecological status",
                             "a water body without a published geometry", "a water body not yet reported"]),
}


def build(write=True):
    """Regenerate fixtures, later payloads and the pinned manifest; returns {path: text}."""
    from src.ingestion.source_packs import SourcePackConformance, validate_source_pack

    outputs, hashes = {}, {}
    for source_id, (description, pages, provider, scenarios) in FIXTURES.items():
        payload = {"description": f"{description}. {NOTE}", "scenarios": scenarios, "native_pages": pages()}
        text = json.dumps(payload, indent=1, ensure_ascii=False) + "\n"
        path = OUT / f"water-{provider}.json"
        outputs[path] = text
        hashes[source_id] = hashlib.sha256(text.encode()).hexdigest()
        if write:
            path.write_text(text, encoding="utf-8")
    later = json.dumps(later_payloads(), indent=1, ensure_ascii=False) + "\n"
    outputs[LATER] = later
    manifest = {"pack_id": "climate-environment-water", "version": "1.0.0",
                "description": "Climate and Environment water and hydrology sources for the optional water feature: "
                               "PEGELONLINE stations and raw water levels, USGS Water Data monitoring locations and "
                               "daily values with approval status, and EEA WISE Water Framework Directive water-body "
                               "status per reporting cycle. Every source is unverified-live until a dated run "
                               "(#2647); GRDC is not implemented; no forecasting, gap filling, own status "
                               "assessments or flood-risk scores.",
                "domains": ["environment"], "defaults": DEFAULTS, "sources": copy.deepcopy(SOURCES)}
    for source in manifest["sources"]:
        source["fixture"]["sha256"] = hashes[source["source_id"]]
        source["fixture"]["expected_output_hash"] = "0" * 64
    if write:
        LATER.parent.mkdir(parents=True, exist_ok=True)
        LATER.write_text(later, encoding="utf-8")
        result = SourcePackConformance(ROOT).offline(validate_source_pack(manifest))
        outputs_by_id = {item["source_id"]: item["output_hash"] for item in result["sources"]}
        for source in manifest["sources"]:
            source["fixture"]["expected_output_hash"] = outputs_by_id[source["source_id"]]
    else:
        pinned = {s["source_id"]: s for s in json.loads(PACK.read_text())["sources"]}
        for source in manifest["sources"]:
            source["fixture"]["expected_output_hash"] = pinned[source["source_id"]]["fixture"]["expected_output_hash"]
    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    outputs[PACK] = text
    if write:
        PACK.write_text(text, encoding="utf-8")
    return outputs


if __name__ == "__main__":
    for written in build():
        print(written.relative_to(ROOT))
