"""Build the pinned water source-pack fixtures and manifest from the authored payloads below (#2582).

Every payload is authored in the provider's documented response shape
(PEGELONLINE REST API v2, USGS Water Data OGC API, EEA Discodata SQL and the
WISE map service). Station UUIDs and numbers, water-body codes, coordinates,
values and status labels are ILLUSTRATIVE and are not live evidence; river and
place names are real only so the bounded coverage reads naturally. Run
``python -m tests.unit.water.fixture_builder`` after editing a payload;
``test_source_pack_is_pinned_and_validates_against_the_schema`` fails when they drift.
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
NOTE = ("Authored offline fixture in the provider's documented response shape. Station UUIDs and numbers, water-body "
        "codes, coordinates, values and status labels are ILLUSTRATIVE, not live evidence; verify against the live "
        "sources before any live run (WA13, #2647).")
BASE = "/webservices/rest-api/v2"


def request(path, query=None):
    return path + ("?" + urlencode(sorted(dict(query).items())) if query else "")


# ------------------------------------------------------------------ PEGELONLINE

DRESDEN, MEISSEN = "aaaaaaaa-0001-4000-8000-00000000d001", "aaaaaaaa-0002-4000-8000-00000000d002"
ELBE = {"shortname": "ELBE", "longname": "ELBE"}
WINDOW = {"start": "2026-09-20T00:00:00+02:00", "end": "2026-09-21T00:00:00+02:00"}


def station(uuid, number, name, lat, lon, km, *, zero=("102.73", "2019-11-01"), discharge=True):
    series = [{"shortname": "W", "longname": "WASSERSTAND ROHDATEN", "unit": "cm", "equidistance": 15,
               "gaugeZero": {"unit": "m. ü. NHN", "value": float(zero[0]), "validFrom": zero[1]},
               "characteristicValues": [
                   {"shortname": "MNW", "longname": "Mittleres Niedrigwasser", "unit": "cm", "value": 90,
                    "validFrom": "2021-01-01", "timespanStart": "2006-11-01", "timespanEnd": "2016-10-31"},
                   {"shortname": "MHW", "longname": "Mittleres Hochwasser", "unit": "cm", "value": 480,
                    "validFrom": "2021-01-01", "timespanStart": "2006-11-01", "timespanEnd": "2016-10-31"}]}]
    if discharge:
        series.append({"shortname": "Q", "longname": "ABFLUSS_ROHDATEN", "unit": "m3/s", "equidistance": 15})
    return {"uuid": uuid, "number": number, "shortname": name, "longname": name, "km": km, "agency": "DRESDEN",
            "longitude": lon, "latitude": lat, "water": ELBE, "timeseries": series}


def measurements(values):
    return [{"timestamp": stamp, "value": value} for stamp, value in values]


DRESDEN_W = [("2026-09-20T00:00:00+02:00", 212.0), ("2026-09-20T00:15:00+02:00", 213.0),
             ("2026-09-20T00:30:00+02:00", 215.0), ("2026-09-20T01:00:00+02:00", 216.0)]  # 00:45 not published
DRESDEN_Q = [("2026-09-20T00:00:00+02:00", 180.5), ("2026-09-20T00:15:00+02:00", 181.2)]
MEISSEN_W = [("2026-09-20T00:00:00+02:00", 198.0), ("2026-09-20T00:15:00+02:00", 199.0)]


def pegelonline_pages(dresden=None, dresden_w=None, meissen_status=200):
    station_query = {"includeCharacteristicValues": "true", "includeTimeseries": "true"}
    pages = [{"request": request(f"{BASE}/stations/{DRESDEN}.json", station_query), "status": 200,
              "body": dresden or station(DRESDEN, "990001", "DRESDEN", 51.054, 13.738, 55.6)},
             {"request": request(f"{BASE}/stations/{MEISSEN}.json", station_query), "status": meissen_status,
              "body": station(MEISSEN, "990002", "MEISSEN", 51.164, 13.475, 82.2, zero=("97.52", "2015-11-01"),
                              discharge=False) if meissen_status == 200 else None},
             {"request": request(f"{BASE}/stations/{DRESDEN}/W/measurements.json", WINDOW), "status": 200,
              "body": measurements(dresden_w or DRESDEN_W)},
             {"request": request(f"{BASE}/stations/{DRESDEN}/Q/measurements.json", WINDOW), "status": 200,
              "body": measurements(DRESDEN_Q)},
             {"request": request(f"{BASE}/stations/{MEISSEN}/W/measurements.json", WINDOW), "status": 200,
              "body": measurements(MEISSEN_W)}]
    return pages


PEGELONLINE_SELECTION = [
    {"kind": "station", "uuid": DRESDEN, "label": "DRESDEN (Elbe) with gauge zero and characteristic values"},
    {"kind": "station", "uuid": MEISSEN, "label": "MEISSEN (Elbe)"},
    {"kind": "measurements", "uuid": DRESDEN, "series": "W", **WINDOW, "label": "DRESDEN water level, one day"},
    {"kind": "measurements", "uuid": DRESDEN, "series": "Q", **WINDOW, "label": "DRESDEN discharge, one day"},
    {"kind": "measurements", "uuid": MEISSEN, "series": "W", **WINDOW, "label": "MEISSEN water level, one day"},
]

# ------------------------------------------------------------------ USGS

POTOMAC = "USGS-01646500"
LOCATION = {"type": "Feature", "id": POTOMAC, "geometry": {"type": "Point", "coordinates": [-77.1276, 38.9498]},
            "properties": {"id": POTOMAC, "agency_code": "USGS", "monitoring_location_number": "01646500",
                           "monitoring_location_name": "POTOMAC RIVER NEAR WASH, DC LITTLE FALLS PUMP STA",
                           "country_code": "US", "state_code": "24", "county_code": "031", "site_type": "Stream",
                           "hydrologic_unit_code": "020700080101", "altitude": 37.2, "altitude_accuracy": 0.1,
                           "vertical_datum": "NAVD88", "drainage_area": 11560}}
CONTINUOUS_WINDOW = {"start": "2026-09-20T12:00:00Z", "end": "2026-09-20T13:00:00Z"}
DAILY_WINDOW = {"start": "2026-09-18T00:00:00Z", "end": "2026-09-20T00:00:00Z"}


def value(parameter, stamp, number, unit, status, qualifier=None, statistic="00011", series="ts-fixture-0001"):
    return {"type": "Feature", "id": f"{series}-{stamp}", "geometry": None,
            "properties": {"time_series_id": series, "monitoring_location_id": POTOMAC, "parameter_code": parameter,
                           "statistic_id": statistic, "time": stamp, "value": number, "unit_of_measure": unit,
                           "approval_status": status, "qualifier": qualifier,
                           "last_modified": "2026-09-20T14:00:00+00:00"}}


def collection(features):
    return {"type": "FeatureCollection", "numberReturned": len(features), "features": features,
            "links": [{"rel": "self", "href": "https://api.waterdata.usgs.gov/ogcapi/v0/collections/continuous/items"}]}


def discharge(status="Provisional", corrected=None):
    stamps = ["2026-09-20T12:00:00+00:00", "2026-09-20T12:15:00+00:00", "2026-09-20T12:30:00+00:00"]
    numbers = ["4210", "4190", corrected or "4180"]
    return [value("00060", stamp, number, "ft^3/s", status, ["Estimated"] if i == 2 else None)
            for i, (stamp, number) in enumerate(zip(stamps, numbers, strict=True))]


def usgs_pages(discharge_values=None):
    query = {"f": "json", "limit": "20", "monitoring_location_id": POTOMAC, "parameter_code": "00060",
             "time": f"{CONTINUOUS_WINDOW['start']}/{CONTINUOUS_WINDOW['end']}"}
    height = {**query, "parameter_code": "00065"}
    daily = {"f": "json", "limit": "5", "monitoring_location_id": POTOMAC, "parameter_code": "00060",
             "statistic_id": "00003", "time": f"{DAILY_WINDOW['start']}/{DAILY_WINDOW['end']}"}
    return [
        {"request": request(f"/ogcapi/v0/collections/monitoring-locations/items/{POTOMAC}", {"f": "json"}),
         "status": 200, "body": LOCATION},
        {"request": request("/ogcapi/v0/collections/continuous/items", query), "status": 200,
         "body": collection(discharge_values or discharge())},
        {"request": request("/ogcapi/v0/collections/continuous/items", height), "status": 200,
         "body": collection([value("00065", "2026-09-20T12:00:00+00:00", "3.61", "ft", "Provisional",
                                   series="ts-fixture-0002"),
                             value("00065", "2026-09-20T12:15:00+00:00", "3.60", "ft", "Provisional",
                                   series="ts-fixture-0002")])},
        {"request": request("/ogcapi/v0/collections/daily/items", daily), "status": 200,
         "body": collection([value("00060", "2026-09-18", "4350", "ft^3/s", "Approved", statistic="00003",
                                   series="ts-fixture-0003")])},
    ]


USGS_SELECTION = [
    {"kind": "monitoring-location", "monitoring_location_id": POTOMAC, "label": "Potomac River at Little Falls"},
    {"kind": "continuous", "monitoring_location_id": POTOMAC, "parameter_code": "00060", "limit": 20,
     **CONTINUOUS_WINDOW, "label": "discharge, one hour"},
    {"kind": "continuous", "monitoring_location_id": POTOMAC, "parameter_code": "00065", "limit": 20,
     **CONTINUOUS_WINDOW, "label": "gage height, one hour"},
    {"kind": "daily", "monitoring_location_id": POTOMAC, "parameter_code": "00060", "statistic_id": "00003",
     "limit": 5, **DAILY_WINDOW, "label": "daily mean discharge, two days"},
]

# ------------------------------------------------------------------ EEA WISE

ELBE_WB, WEISSERITZ_WB = "DERW_DESN_FIX-0001", "DERW_DESN_FIX-0002"
TABLE = "[WISE_WFD].[latest].[SWB_SurfaceWaterBody]"
STATUS_SELECTION = {"kind": "status", "codes": [ELBE_WB, WEISSERITZ_WB], "cycles": ["2016", "2022"], "table": TABLE,
                    "label": "two Elbe water bodies near Dresden, 2nd and 3rd RBMP cycles"}
GEOMETRY_SELECTION = {"kind": "geometry", "codes": [ELBE_WB, WEISSERITZ_WB], "service": "WFD2016_SurfaceWaterBody",
                      "layer": "2", "vintage": "WISE WFD 2016 reference spatial dataset",
                      "label": "published water-body geometries"}


def row(code, cycle, name, eco, chem, eco_year, chem_year):
    return {"cYear": cycle, "countryCode": "DE", "euRBDCode": "DE5000", "euSurfaceWaterBodyCode": code,
            "surfaceWaterBodyName": name, "surfaceWaterBodyCategory": "RW", "naturalAWBHMWB": "Natural",
            "swEcologicalStatusOrPotentialValue": eco, "swEcologicalAssessmentYear": eco_year,
            "swChemicalStatusValue": chem, "swChemicalAssessmentYear": chem_year}


ROWS = [row(ELBE_WB, 2016, "Elbe-3 (fixture)", "Moderate", "Failing to achieve good", 2014, 2015),
        row(ELBE_WB, 2022, "Elbe-3 (fixture)", "Poor", "Failing to achieve good", 2020, 2021),
        row(WEISSERITZ_WB, 2016, "Weisseritz (fixture)", "Good", "Good", 2014, 2015)]


def wise_status_pages(rows=None):
    from src.ingestion.water_sources import request_for

    _, path, query = request_for("eea-wise", STATUS_SELECTION)
    return [{"request": request(path, query), "status": 200, "body": {"results": rows or ROWS}}]


def wise_geometry_pages():
    from src.ingestion.water_sources import request_for

    _, path, query = request_for("eea-wise", GEOMETRY_SELECTION)
    features = [
        {"type": "Feature", "geometry": {"type": "LineString",
                                          "coordinates": [[13.62, 51.00], [13.74, 51.06], [13.90, 51.10]]},
         "properties": {"euSurfaceWaterBodyCode": ELBE_WB, "surfaceWaterBodyName": "Elbe-3 (fixture)",
                        "surfaceWaterBodyCategory": "RW"}},
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[13.30, 51.20], [13.45, 51.18]]},
         "properties": {"euSurfaceWaterBodyCode": WEISSERITZ_WB, "surfaceWaterBodyName": "Weisseritz (fixture)",
                        "surfaceWaterBodyCategory": "RW"}}]
    return [{"request": request(path, query), "status": 200, "body": {"type": "FeatureCollection",
                                                                      "features": features}}]


def later_payloads():
    """A later acquisition: Dresden's gauge zero changes and a high value appears, one level value is corrected,
    Meissen is withdrawn, USGS discharge is approved (one value revised), and a new WFD cycle row appears."""
    moved = station(DRESDEN, "990001", "DRESDEN", 51.054, 13.738, 55.6, zero=("102.70", "2026-09-25"))
    corrected = [DRESDEN_W[0], DRESDEN_W[1], ("2026-09-20T00:30:00+02:00", 214.0), DRESDEN_W[3],
                 ("2026-09-20T01:15:00+02:00", 495.0)]
    pegel = {p["request"]: {"status": p["status"], "body": p["body"]}
             for p in pegelonline_pages(moved, corrected, meissen_status=404)
             if "/stations/" + DRESDEN + ".json" in p["request"] or "/stations/" + MEISSEN + ".json" in p["request"]
             or p["request"].startswith(f"{BASE}/stations/{DRESDEN}/W/")}
    usgs = {p["request"]: {"status": 200, "body": p["body"]}
            for p in usgs_pages(discharge("Approved", corrected="4170"))
            if "parameter_code=00060" in p["request"] and "continuous" in p["request"]}
    wise = {p["request"]: {"status": 200, "body": p["body"]} for p in wise_status_pages(
        ROWS + [row(WEISSERITZ_WB, 2022, "Weisseritz (fixture)", "Moderate", "Good", 2020, 2021)])}
    return {"description": NOTE + " A later acquisition of the same selections.",
            "pegelonline": pegel, "usgs": usgs, "eea-wise": wise}


# ------------------------------------------------------------------ manifest

DEFAULTS = {
    "connector": "water",
    "update_cadence": "daily re-check of the explicit selection; PEGELONLINE publishes 15-minute raw values, USGS "
                      "publishes continuous values that move from provisional to approved, EEA WISE publishes WFD "
                      "status per reporting cycle",
    "temporal_semantics": "observations keep the source's timestamp and unit as published with their quality state; "
                          "stations keep gauge zero or vertical datum with the validity the source states; WFD status "
                          "belongs to one reporting cycle; every distinct payload is an immutable revision by "
                          "retrieval time and a declared record no longer served is a dated removal",
    "mapping": {"target_schema": "noesis-water-record-v1", "version": "1.0.0"},
    "extractor_versions": ["water-sources:1.0.0"],
    "operations": ["water"],
    "schedule": {"kind": "interval", "interval_s": 86400},
    "auth": {"kind": "none"},
    "health": {"required": False, "max_staleness_s": 604800},
    "budgets": {"timeout_ms": 30000, "max_results": 500, "max_bytes": 2000000, "max_pages": 20},
    "policy": {"excluded": ["flood forecasting", "interpolation or gap filling", "own water-body status assessments",
                            "flood-risk scoring"],
               "values": "as published with their quality state; never resampled"},
}
SOURCES = [
    {"source_id": "pegelonline-stations-levels", "endpoint": "https://www.pegelonline.wsv.de",
     "publisher": "Wasserstrassen- und Schifffahrtsverwaltung des Bundes (WSV), PEGELONLINE",
     "scope": "Two Elbe gauges with their time series, gauge zero (value and validFrom) and characteristic values, "
              "and one-day windows of water level and discharge measurements as published (unchecked raw values)",
     "license": {"id": "dl-de-zero-2.0", "terms_url": "https://www.pegelonline.wsv.de/gast/nutzungsbedingungen",
                 "redistribution": "permitted under DL-DE Zero 2.0; Noesis cites PEGELONLINE (WSV) and the request"},
     "water": {"provider": "pegelonline", "namespace": "environment", "live_verification": "unverified-live",
               "coverage_decision": "two stations (Dresden, Meissen) on the Elbe; W and Q windows of at most 31 days "
                                    "(documented availability), one day in the fixture", "selection":
                   PEGELONLINE_SELECTION},
     "fixture": {"path": "tests/fixtures/source_packs/water-pegelonline.json"}},
    {"source_id": "usgs-water-data", "endpoint": "https://api.waterdata.usgs.gov",
     "publisher": "U.S. Geological Survey (USGS), Water Data APIs",
     "scope": "One monitoring location with its vertical datum, bounded continuous discharge and gage-height windows "
              "and a daily mean window, each value with its approval status and qualifiers",
     "license": {"id": "us-public-domain",
                 "terms_url": "https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
                 "redistribution": "public domain; credit the U.S. Geological Survey; provisional data are subject to "
                                   "revision"},
     "auth": {"kind": "optional-secret", "secret_ref": "NOESIS_USGS_WATER_API_KEY"},
     "water": {"provider": "usgs", "namespace": "environment", "live_verification": "unverified-live",
               "coverage_decision": "USGS-01646500 (Potomac River at Little Falls); 00060 and 00065 windows of at "
                                    "most 31 days and 1000 values", "selection": USGS_SELECTION},
     "fixture": {"path": "tests/fixtures/source_packs/water-usgs.json"}},
    {"source_id": "eea-wise-wfd-status", "endpoint": "https://discodata.eea.europa.eu",
     "publisher": "European Environment Agency (EEA), WISE Water Framework Directive database",
     "scope": "Surface water-body ecological and chemical status for two Elbe water bodies in the 2016 and 2022 "
              "reporting cycles, one row per code and cycle as published",
     "license": {"id": "cc-by-4.0", "terms_url": "https://www.eea.europa.eu/en/legal-notice",
                 "redistribution": "permitted with attribution to the EEA (CC BY 4.0)"},
     "water": {"provider": "eea-wise", "namespace": "environment", "live_verification": "unverified-live",
               "coverage_decision": "two water bodies, two reporting cycles, one pinned WISE table",
               "selection": [STATUS_SELECTION]},
     "fixture": {"path": "tests/fixtures/source_packs/water-eea-wise-status.json"}},
    {"source_id": "eea-wise-water-body-geometry", "endpoint": "https://water.discomap.eea.europa.eu",
     "publisher": "European Environment Agency (EEA), WISE WFD reference spatial datasets",
     "scope": "Published geometries of the same two water bodies from one named reporting dataset vintage, used only "
              "to map water bodies to places",
     "license": {"id": "cc-by-4.0", "terms_url": "https://www.eea.europa.eu/en/legal-notice",
                 "redistribution": "permitted with attribution to the EEA (CC BY 4.0)"},
     "water": {"provider": "eea-wise", "namespace": "environment", "live_verification": "unverified-live",
               "coverage_decision": "two water bodies from the 2016 reference spatial dataset",
               "selection": [GEOMETRY_SELECTION]},
     "fixture": {"path": "tests/fixtures/source_packs/water-eea-wise-geometry.json"}},
]
FIXTURES = {
    "pegelonline-stations-levels": ("PEGELONLINE REST API v2 stations and measurements", pegelonline_pages,
                                    ["gauge zero with validFrom", "characteristic values", "a missing 15-minute value",
                                     "a station without discharge"]),
    "usgs-water-data": ("USGS Water Data OGC API monitoring location, continuous and daily values", usgs_pages,
                        ["provisional values with a qualifier", "an approved daily mean", "vertical datum"]),
    "eea-wise-wfd-status": ("EEA Discodata WISE WFD surface water-body status rows", wise_status_pages,
                            ["two cycles for one water body", "a code without a 2022 row"]),
    "eea-wise-water-body-geometry": ("EEA WISE map service water-body geometries (GeoJSON)", wise_geometry_pages,
                                     ["one geometry through Dresden", "one outside"]),
}
FILES = {"pegelonline-stations-levels": "water-pegelonline.json", "usgs-water-data": "water-usgs.json",
         "eea-wise-wfd-status": "water-eea-wise-status.json",
         "eea-wise-water-body-geometry": "water-eea-wise-geometry.json"}


def build(write=True):
    """Regenerate fixtures, later payloads and the pinned manifest; returns {path: text}."""
    from src.ingestion.source_packs import SourcePackConformance, validate_source_pack

    outputs, hashes = {}, {}
    for source_id, (description, pages, scenarios) in FIXTURES.items():
        payload = {"description": f"{description}. {NOTE}", "scenarios": scenarios, "native_pages": pages()}
        text = json.dumps(payload, indent=1, ensure_ascii=False) + "\n"
        path = OUT / FILES[source_id]
        outputs[path] = text
        hashes[source_id] = hashlib.sha256(text.encode()).hexdigest()
        if write:
            path.write_text(text, encoding="utf-8")
    later = json.dumps(later_payloads(), indent=1, ensure_ascii=False) + "\n"
    outputs[LATER] = later
    manifest = {"pack_id": "climate-environment-water", "version": "1.0.0",
                "description": "Climate and Environment water and hydrology sources for the optional water features: "
                               "PEGELONLINE stations, gauge zero and water-level/discharge measurements, USGS Water "
                               "Data monitoring locations and values with approval status, and EEA WISE WFD water-body "
                               "status per reporting cycle with published geometries. Every source is unverified-live "
                               "until a dated run (#2647); GRDC is not implemented (terms); no forecasting, "
                               "interpolation, gap filling, own status assessment or flood-risk scoring.",
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
        pinned = json.loads(PACK.read_text())
        for source, pinned_source in zip(manifest["sources"], pinned["sources"], strict=True):
            source["fixture"]["expected_output_hash"] = pinned_source["fixture"]["expected_output_hash"]
    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    outputs[PACK] = text
    if write:
        PACK.write_text(text, encoding="utf-8")
    return outputs


if __name__ == "__main__":
    for written in build():
        print(written.relative_to(ROOT))
