"""Authored offline fixtures for the Geospatial ``infrastructure`` feature (#2223).

Every body below is written by hand in the publisher's documented response shape (GPPD CSV, GEM tracker CSV
exports, Overpass JSON, ArcGIS GeoJSON, ENTSOG JSON). Every asset, owner, identifier and value is synthetic
("Fixture ..."): none of it is live evidence. ``python -m tests.unit.infrastructure.fixture_builder`` rewrites
``tests/fixtures/source_packs/infrastructure-*.json``, ``tests/fixtures/infrastructure/*.json`` and the source
pack ``config/source_packs/geospatial-infrastructure.json`` with pinned SHA-256 and expected output hashes.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path

from src.ingestion import infrastructure_sources as src

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "config/source_packs/geospatial-infrastructure.json"
RETRIEVED_AT = "2026-09-25T06:00:00Z"
RETRIEVED_LATER = "2026-09-28T06:00:00Z"
NOTE = ("Authored offline fixture in the publisher's documented response shape; every asset, owner and identifier is "
        "synthetic. Not live evidence.")
GEM_FILE = "https://globalenergymonitor.org/wp-content/uploads/fixture/"
LUSATIA = [14.2, 51.3, 14.8, 51.8]
GULF = [-94.5, 29.0, -88.8, 31.0]
SOCAL = [-118.5, 33.5, -116.0, 35.5]
SELECTIONS = {
    "gppd-deu": ("gppd", {"countries": ["DEU"], "max_rows": 200,
                          "release": {"label": "Global Power Plant Database v1.3.0", "released_on": "2021-06-02"}}),
    "gem-coal-plants-de": ("gem", {"tracker": "coal-plants", "file_url": GEM_FILE + "gcpt-2026-01.csv",
                                   "release": {"label": "GCPT January 2026", "released_on": "2026-01-20"},
                                   "countries": ["Germany"], "max_rows": 200}),
    "gem-gas-pipelines-de": ("gem", {"tracker": "gas-pipelines", "file_url": GEM_FILE + "ggit-pipelines-2026-06.csv",
                                     "release": {"label": "GGIT June 2026", "released_on": "2026-06-10"},
                                     "countries": ["Germany"], "max_rows": 200}),
    "gem-lng-terminals-us": ("gem", {"tracker": "lng-terminals", "file_url": GEM_FILE + "ggit-lng-2026-06.csv",
                                     "release": {"label": "GGIT June 2026", "released_on": "2026-06-10"},
                                     "countries": ["United States"], "max_rows": 200}),
    "osm-lusatia": ("osm", {"area": "de-lusatia", "bbox": LUSATIA, "tags": list(src.OSM_TAGS), "limit": 200}),
    "eia-power-plants-socal": ("eia", {"layer": "power-plants", "bbox": SOCAL, "max_records": 200,
                                       "release": {"label": "EIA Power Plants layer July 2026",
                                                   "released_on": "2026-08-01"}}),
    "eia-lng-terminals-gulf": ("eia", {"layer": "lng-terminals", "bbox": GULF, "max_records": 200}),
    "eia-gas-pipelines-gulf": ("eia", {"layer": "gas-pipelines", "bbox": GULF, "max_records": 200}),
    "entsog-de-points": ("entsog", {"point_keys": ["ITP-09991", "ITP-09992"], "from": "2026-09-20",
                                    "to": "2026-09-21"}),
}
LATER = {
    "gem_coal_plants_release_2": ("gem-coal-plants-de", {
        **SELECTIONS["gem-coal-plants-de"][1], "file_url": GEM_FILE + "gcpt-2026-07.csv",
        "release": {"label": "GCPT July 2026", "released_on": "2026-07-20"}}),
    "osm_lusatia_later": ("osm-lusatia", SELECTIONS["osm-lusatia"][1]),
}
SOURCE_META = {
    "gppd-deu": ("World Resources Institute", "GPPD v1.3.0 power plants in Germany (DEU); at most 200 rows",
                 "static release", ["declared-release", "estimates", "country-filter"]),
    "gem-coal-plants-de": ("Global Energy Monitor", "Global Coal Plant Tracker units in Germany from one declared "
                                                   "release file", "semi-annual tracker releases",
                           ["status", "owners-with-shares", "cross-references", "non-commercial"]),
    "gem-gas-pipelines-de": ("Global Energy Monitor", "Global Gas Infrastructure Tracker pipelines in Germany from one "
                                                     "declared release file", "semi-annual tracker releases",
                             ["route-wkt", "status", "owners"]),
    "gem-lng-terminals-us": ("Global Energy Monitor", "Global Gas Infrastructure Tracker LNG terminals in the United "
                                                     "States from one declared release file",
                             "semi-annual tracker releases", ["status", "owners"]),
    "osm-lusatia": ("OpenStreetMap contributors", "Overpass extract of power and pipeline features in the de-lusatia "
                                                  "bbox (<= 200 elements)", "continuous edits; bounded extracts",
                    ["element-versions", "lifecycle-prefix", "editor-identity-dropped", "relation-without-geometry"]),
    "eia-power-plants-socal": ("U.S. Energy Information Administration", "EIA Power Plants layer in the "
                                                                          "us-southern-california bbox",
                               "monthly layer updates", ["plant-code", "capacity"]),
    "eia-lng-terminals-gulf": ("U.S. Energy Information Administration", "EIA LNG terminals layer in the us-gulf-coast "
                                                                          "bbox", "irregular layer updates",
                               ["status", "docket", "retrieval-time-release"]),
    "eia-gas-pipelines-gulf": ("U.S. Energy Information Administration", "EIA natural-gas pipelines layer in the "
                                                                          "us-gulf-coast bbox",
                               "irregular layer updates", ["line-geometry"]),
    "entsog-de-points": ("ENTSOG", "Two ENTSOG connection points in Germany and their firm technical capacity per "
                                   "direction for one day", "daily", ["entry-exit", "no-geometry"]),
}


def _csv(header, rows):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=header, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: row.get(k, "") for k in header})
    return buffer.getvalue()


def _page(step, body):
    return {"url": step["url"], "params": step["params"], "status": 200, "body": body}


def _step(name, index=0, selection=None):
    provider, default = SELECTIONS[name]
    return src.plan(provider, selection or default)[index]


GPPD_HEADER = ["country", "country_long", "name", "gppd_idnr", "capacity_mw", "latitude", "longitude", "primary_fuel",
               "other_fuel1", "other_fuel2", "other_fuel3", "commissioning_year", "owner", "source", "url",
               "geolocation_source", "wepp_id", "year_of_capacity_data", "generation_gwh_2018", "generation_gwh_2019",
               "generation_data_source", "estimated_generation_gwh_2017", "estimated_generation_note_2017"]


def gppd_pages():
    rows = [
        {"country": "DEU", "country_long": "Germany", "name": "Fixture Lignite Plant Nord", "gppd_idnr": "DEU9990001",
         "capacity_mw": "1500.0", "latitude": "51.6620", "longitude": "14.4560", "primary_fuel": "Coal",
         "commissioning_year": "1985.0", "owner": "Fixture Energie AG", "source": "Fixture registry",
         "url": "https://example.org/fixture-registry", "geolocation_source": "Fixture registry", "wepp_id": "99001",
         "year_of_capacity_data": "2019.0", "generation_gwh_2018": "9100.5", "generation_gwh_2019": "8700.2",
         "generation_data_source": "Fixture statistics", "estimated_generation_gwh_2017": "9050.0",
         "estimated_generation_note_2017": "CAPACITY-FACTOR-V1"},
        {"country": "DEU", "country_long": "Germany", "name": "Fixture Gaskraftwerk Sued", "gppd_idnr": "DEU9990002",
         "capacity_mw": "400.0", "latitude": "51.4200", "longitude": "14.3300", "primary_fuel": "Gas",
         "commissioning_year": "2010.0", "owner": "Fixture Kraftwerke Sued GmbH", "source": "Fixture registry",
         "url": "https://example.org/fixture-registry", "geolocation_source": "Fixture registry",
         "year_of_capacity_data": "2019.0"},
        {"country": "FRA", "country_long": "France", "name": "Fixture Centrale", "gppd_idnr": "FRA9990003",
         "capacity_mw": "900.0", "latitude": "48.1", "longitude": "2.2", "primary_fuel": "Nuclear"},
    ]
    return [_page(_step("gppd-deu"), _csv(GPPD_HEADER, rows))]


GCPT_HEADER = ["GEM location ID", "GEM unit/phase ID", "Country/Area", "Plant name", "Unit name", "Owner", "Parent",
               "Capacity (MW)", "Status", "Start year", "Retired year", "Latitude", "Longitude", "Location accuracy",
               "Wiki URL", "Other IDs (location)"]


def gem_coal_pages(release=1):
    common = {"GEM location ID": "L100001", "Country/Area": "Germany", "Plant name": "Fixture Lignite Plant Nord",
              "Owner": "Fixture Energie AG [100%]", "Latitude": "51.6618", "Longitude": "14.4561",
              "Location accuracy": "exact", "Wiki URL": "https://www.gem.wiki/Fixture_Lignite_Plant_Nord",
              "Other IDs (location)": "WRI: DEU9990001; WEPP: 99001"}
    parent = ("Fixture Holding SE [60%]; Stadtwerke Fixture [40%]" if release == 1
              else "Fixture Holding SE [75%]; Stadtwerke Fixture [25%]")
    rows = [
        {**common, "GEM unit/phase ID": "G100001", "Unit name": "Unit A", "Parent": parent, "Capacity (MW)": "750",
         "Status": "operating" if release == 1 else "retired", "Start year": "1985",
         "Retired year": "" if release == 1 else "2026"},
        {**common, "GEM unit/phase ID": "G100002", "Unit name": "Unit B", "Parent": parent,
         "Capacity (MW)": "750" if release == 1 else "760", "Status": "operating", "Start year": "1987"},
        {"GEM location ID": "L100009", "GEM unit/phase ID": "G100009", "Country/Area": "Poland",
         "Plant name": "Fixture Elektrownia", "Unit name": "1", "Owner": "Fixture Energia SA [100%]",
         "Capacity (MW)": "500", "Status": "operating", "Latitude": "51.1", "Longitude": "17.0"},
    ]
    selection = LATER["gem_coal_plants_release_2"][1] if release == 2 else None
    return [_page(_step("gem-coal-plants-de", selection=selection), _csv(GCPT_HEADER, rows))]


def gem_pipeline_pages():
    header = ["ProjectID", "PipelineName", "SegmentName", "Countries", "Status", "Owner", "Parent", "Capacity",
              "CapacityUnits", "LengthKnownKm", "Start year", "Route", "WikiURL"]
    rows = [{"ProjectID": "P0001", "PipelineName": "Fixture Ostsee-Anbindung", "Countries": "Germany",
             "Status": "operating", "Owner": "Fixture Gastransport GmbH [100%]", "Capacity": "22",
             "CapacityUnits": "bcm/y", "LengthKnownKm": "58", "Start year": "2012",
             "Route": "LINESTRING (14.251 51.351, 14.480 51.550, 14.699 51.749)",
             "WikiURL": "https://www.gem.wiki/Fixture_Ostsee-Anbindung"}]
    return [_page(_step("gem-gas-pipelines-de"), _csv(header, rows))]


def gem_lng_pages():
    header = ["TerminalID", "TerminalName", "UnitName", "Country", "FacilityType", "Status", "Owner", "Parent",
              "Capacity", "CapacityUnits", "Start year", "Latitude", "Longitude", "Location accuracy", "WikiURL"]
    rows = [{"TerminalID": "T0001", "TerminalName": "Fixture LNG Terminal", "UnitName": "Train 1",
             "Country": "United States", "FacilityType": "Export", "Status": "construction",
             "Owner": "Fixture LNG LLC [100%]", "Parent": "Fixture Energy Corp [100%]", "Capacity": "4.5",
             "CapacityUnits": "mtpa", "Latitude": "29.7400", "Longitude": "-93.8700", "Location accuracy": "approximate",
             "WikiURL": "https://www.gem.wiki/Fixture_LNG_Terminal"}]
    return [_page(_step("gem-lng-terminals-us"), _csv(header, rows))]


def _way(number, points, tags, version="3", changeset="990001", timestamp="2026-05-01T10:00:00Z"):
    lons = [p[0] for p in points]
    lats = [p[1] for p in points]
    return {"type": "way", "id": number, "version": version, "changeset": changeset, "timestamp": timestamp,
            "user": "fixture-mapper", "uid": 424242,
            "bounds": {"minlat": min(lats), "minlon": min(lons), "maxlat": max(lats), "maxlon": max(lons)},
            "nodes": list(range(number * 10, number * 10 + len(points))),
            "geometry": [{"lat": lat, "lon": lon} for lon, lat in points], "tags": tags}


def _square(lon, lat, d=0.004):
    return [[lon - d, lat - d], [lon + d, lat - d], [lon + d, lat + d], [lon - d, lat + d], [lon - d, lat - d]]


def osm_elements(later=False):
    elements = [
        _way(9001, _square(14.4560, 51.6620), {"power": "plant", "name": "Fixture Lignite Plant Nord",
                                               "operator": "Fixture Energie AG", "operator:wikidata": "Q999001",
                                               "plant:source": "coal", "plant:output:electricity": "1450 MW"}),
        _way(9002, _square(14.3302, 51.4201, 0.002),
             {"power": "plant", "name": "Fixture Gaskraftwerk Sued", "ref:gppd": "DEU9990002",
              "operator": "Fixture Kraftwerke Sued GmbH" if not later else "Fixture Kraftwerke Sued Neu GmbH",
              "plant:output:electricity": "420 MW"},
             version="4" if later else "3", changeset="990077" if later else "990001",
             timestamp="2026-09-26T08:00:00Z" if later else "2026-05-01T10:00:00Z"),
        _way(9101, [[14.4620, 51.6620], [14.5300, 51.6000], [14.6000, 51.5500]],
             {"power": "line", "voltage": "380000", "operator": "Fixture Netz GmbH", "cables": "6"}),
        _way(9102, _square(14.6000, 51.5500, 0.001), {"power": "substation", "name": "Fixture Umspannwerk",
                                                      "operator": "Fixture Netz GmbH", "voltage": "380000;110000"}),
        _way(9201, [[14.2520, 51.3520], [14.4810, 51.5490], [14.6980, 51.7480]],
             {"man_made": "pipeline", "substance": "gas", "name": "Fixture Ostsee-Anbindung",
              "operator": "Fixture Gastransport GmbH"}),
        {"type": "node", "id": 9301, "lat": "51.4000000", "lon": "14.7000000", "version": "2", "changeset": "990002",
         "timestamp": "2025-11-02T09:00:00Z", "user": "fixture-mapper", "uid": 424242,
         "tags": {"disused:power": "plant", "name": "Fixture Kraftwerk Alt"}},
        {"type": "relation", "id": 9401, "version": "1", "changeset": "990003", "timestamp": "2024-01-01T00:00:00Z",
         "user": "fixture-mapper", "uid": 424242,
         "tags": {"power": "plant", "type": "multipolygon", "name": "Fixture Solarpark Ost"}},
    ]
    if later:
        elements.append({"type": "node", "id": 9302, "lat": "51.7000000", "lon": "14.6500000", "version": "1",
                         "changeset": "990078", "timestamp": "2026-09-26T09:00:00Z", "user": "fixture-mapper",
                         "uid": 424242, "tags": {"power": "generator", "name": "Fixture Windrad 1",
                                                 "generator:output:electricity": "4.2 MW",
                                                 "operator": "Fixture Wind GmbH"}})
    return elements


def osm_pages(later=False):
    body = {"version": 0.6, "generator": "Overpass API (fixture)",
            "osm3s": {"timestamp_osm_base": "2026-09-27T21:00:00Z" if later else "2026-09-24T21:00:00Z",
                      "copyright": "The data included in this document is from www.openstreetmap.org. The data is "
                                   "made available under ODbL."},
            "elements": osm_elements(later)}
    return [_page(_step("osm-lusatia"), body)]


def eia_pages(layer):
    name = {"power-plants": "eia-power-plants-socal", "lng-terminals": "eia-lng-terminals-gulf",
            "gas-pipelines": "eia-gas-pipelines-gulf"}[layer]
    if layer == "power-plants":
        features = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [-117.3012, 34.8021]},
                     "properties": {"OBJECTID": 1, "Plant_Code": 99901, "Plant_Name": "Fixture Solar Park",
                                    "Utility_ID": 88801, "Utility_Name": "Fixture Solar LLC", "State": "California",
                                    "County": "San Bernardino", "PrimSource": "solar", "Total_MW": 300.0,
                                    "Source": "EIA-860M", "Period": "202607"}}]
    elif layer == "lng-terminals":
        features = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [-93.8702, 29.7398]},
                     "properties": {"OBJECTID": 7, "Name": "Fixture LNG Terminal", "Company": "Fixture LNG LLC",
                                    "Type": "Export", "Status": "Operating", "Capacity_Bcfd": 4.5,
                                    "Docket": "CP99-001-000", "State": "LA"}}]
    else:
        features = [{"type": "Feature", "geometry": {"type": "LineString", "coordinates": [
            [-93.9, 29.8], [-93.5, 30.1], [-92.8, 30.3]]},
            "properties": {"OBJECTID": 12, "Operator": "Fixture Gulf Pipeline Co", "TYPEPIPE": "Interstate"}}]
    return [_page(_step(name), {"type": "FeatureCollection", "features": features})]


def entsog_pages():
    points = {"connectionpoints": [
        {"pointKey": "ITP-09991", "pointLabel": "Fixture Grenzuebergang Nord",
         "pointType": "Cross-Border Transmission IP within EU", "tSOCountry": "DE",
         "lastUpdateDateTime": "2026-09-19T08:00:00Z"},
        {"pointKey": "ITP-09992", "pointLabel": "Fixture Speicher Sued", "pointType": "Storage point",
         "tSOCountry": "DE", "lastUpdateDateTime": "2026-09-19T08:00:00Z"}],
        "meta": {"total": 2, "count": 2}}

    def row(point, direction, value, operator="DE-TSO-9991", label="Fixture Gastransport GmbH",
            indicator="Firm Technical"):
        return {"pointKey": point, "operatorKey": operator, "operatorLabel": label, "directionKey": direction,
                "indicator": indicator, "periodType": "day", "periodFrom": "2026-09-20T06:00:00Z",
                "periodTo": "2026-09-21T06:00:00Z", "unit": "kWh/d", "value": value,
                "lastUpdateDateTime": "2026-09-20T10:15:00Z"}

    data = {"operationaldatas": [row("ITP-09991", "entry", 1000000000), row("ITP-09991", "exit", 800000000),
                                 row("ITP-09991", "entry", 612000000, indicator="Physical Flow"),
                                 row("ITP-09992", "entry", 90000000, operator="DE-SSO-9992",
                                     label="Fixture Speicher GmbH")]}
    return [_page(_step("entsog-de-points", 0), points), _page(_step("entsog-de-points", 1), data)]


def source_fixture(provider, pages, scenarios, retrieved_at=RETRIEVED_AT):
    return {"captured_at": "authored 2026-09-29 (not a capture)", "provider": provider, "note": NOTE,
            "retrieved_at": retrieved_at, "scenarios": scenarios, "native_pages": pages}


SOURCE_FIXTURES = {
    "gppd-deu": lambda: gppd_pages(),
    "gem-coal-plants-de": lambda: gem_coal_pages(1),
    "gem-gas-pipelines-de": lambda: gem_pipeline_pages(),
    "gem-lng-terminals-us": lambda: gem_lng_pages(),
    "osm-lusatia": lambda: osm_pages(),
    "eia-power-plants-socal": lambda: eia_pages("power-plants"),
    "eia-lng-terminals-gulf": lambda: eia_pages("lng-terminals"),
    "eia-gas-pipelines-gulf": lambda: eia_pages("gas-pipelines"),
    "entsog-de-points": lambda: entsog_pages(),
}
LATER_FIXTURES = {
    "gem_coal_plants_release_2": lambda: gem_coal_pages(2),
    "osm_lusatia_later": lambda: osm_pages(later=True),
}


def fixture_path(name):
    if name in SOURCE_FIXTURES:
        return ROOT / "tests/fixtures/source_packs" / f"infrastructure-{name}.json"
    return ROOT / "tests/fixtures/infrastructure" / f"{name}.json"


def load(name):
    return json.loads(fixture_path(name).read_text())


def selection(name):
    if name in LATER:
        base, chosen = LATER[name]
        return SELECTIONS[base][0], chosen
    return SELECTIONS[name]


def _source_entry(name):
    provider, chosen = SELECTIONS[name]
    publisher, scope, cadence, _ = SOURCE_META[name]
    licence = src.LICENCES[provider]
    endpoint = src.plan(provider, chosen)[0]["url"]
    temporal = {"gppd": "declared release (database version); year_of_capacity_data dates capacities",
                "gem": "declared tracker release; status changes across releases become dated status revisions",
                "osm": "Overpass timestamp_osm_base dates the extract; element version and changeset kept",
                "eia": "declared layer release when stated, else retrieval time (labelled)",
                "entsog": "lastUpdateDateTime per capacity (provider_last_update)"}[provider]
    return {
        "source_id": f"infrastructure-{name}", "endpoint": endpoint, "publisher": publisher, "scope": scope,
        "license": {"id": licence["id"], "terms_url": licence["terms_url"],
                    "redistribution": src.PROVIDER_CONTRACTS[provider]["redistribution"]},
        "update_cadence": cadence, "temporal_semantics": temporal,
        "infrastructure": {"provider": provider, "namespace": "infrastructure",
                           "access_decision": src.PROVIDER_CONTRACTS[provider]["decision"],
                           "live_verification": src.LIVE_VERIFICATION[provider]["status"], "selection": chosen},
        "fixture": {"path": str(fixture_path(name).relative_to(ROOT)), "sha256": "0" * 64,
                    "expected_output_hash": "0" * 64},
    }


def pack_manifest():
    return {
        "pack_id": "geospatial-infrastructure", "version": "1.0.0",
        "description": ("Critical infrastructure registries for the Geospatial pack's optional infrastructure feature: "
                        "WRI GPPD power plants, declared Global Energy Monitor tracker releases (non-commercial "
                        "constraint recorded), bounded OpenStreetMap Overpass extracts (ODbL), EIA energy "
                        "infrastructure layers and ENTSOG points with firm technical capacity. Assets, status "
                        "revisions, capacities and owner assertions as published; geometries projected onto the "
                        "existing geospatial store. Audit: docs/development/infrastructure-evidence/source-audit.md. "
                        "Every source is unverified-live until a dated run. No vulnerability assessment or valuation."),
        "domains": ["geospatial"],
        "defaults": {
            "connector": "infrastructure",
            "mapping": {"target_schema": "noesis-infrastructure-asset-record-v1", "version": "1.0.0"},
            "extractor_versions": ["infrastructure-sources:1.0.0"], "operations": ["observe"],
            "schedule": {"kind": "interval", "interval_s": 604800}, "auth": {"kind": "none"},
            "budgets": {"timeout_ms": 60000, "max_results": 500, "max_bytes": 20000000, "max_pages": 4},
            "health": {"required": False, "max_staleness_s": 1209600},
        },
        "sources": [_source_entry(name) for name in SELECTIONS],
    }


def write_all():
    from src.ingestion.source_packs import _digest, validate_source_pack

    written = {}
    fixture_path("gppd-deu").parent.mkdir(parents=True, exist_ok=True)
    fixture_path("osm_lusatia_later").parent.mkdir(parents=True, exist_ok=True)
    for name, build in SOURCE_FIXTURES.items():
        provider = SELECTIONS[name][0]
        body = source_fixture(provider, build(), SOURCE_META[name][3])
        fixture_path(name).write_text(json.dumps(body, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    for name, build in LATER_FIXTURES.items():
        provider = selection(name)[0]
        body = source_fixture(provider, build(), ["later-release"], RETRIEVED_LATER)
        fixture_path(name).write_text(json.dumps(body, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    manifest = pack_manifest()
    validated = {s["source_id"]: s for s in validate_source_pack(manifest)["sources"]}
    for entry in manifest["sources"]:
        path = ROOT / entry["fixture"]["path"]
        raw = path.read_bytes()
        records = src.replay_native_fixture(validated[entry["source_id"]], json.loads(raw))
        entry["fixture"]["sha256"] = hashlib.sha256(raw).hexdigest()
        entry["fixture"]["expected_output_hash"] = _digest(records)
        written[entry["source_id"]] = len(records)
    PACK.write_text(json.dumps(manifest, indent=1, ensure_ascii=False) + "\n")
    return written


if __name__ == "__main__":
    print(json.dumps(write_all(), indent=1))
