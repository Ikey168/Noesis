"""Build pinned source-pack fixtures from the authored raw provider files.

The raw files in ``tests/fixtures/environment`` are authored (see its
README). This module routes each planned request of a source selection to its
raw file, writes the native-page fixtures under ``tests/fixtures/source_packs``
and pins their hashes (fixture sha256 + expected normalized output) into the
two manifests the Climate and Environment bundle ships:

* ``packs/climate-environment/source_packs/climate-environment.json``
* ``packs/climate-environment/source_packs/geospatial-berlin-1.2.0.json``
  (the ``geospatial-berlin`` upgrade adding Umweltatlas WFS layers)

Run ``python -m tests.unit.environment.fixture_builder`` after editing a raw
file; ``test_fixtures_are_pinned_and_in_sync`` fails when they drift.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
RAW = ROOT / "tests/fixtures/environment"
OUT = ROOT / "tests/fixtures/source_packs"
PACK = ROOT / "packs/climate-environment/source_packs/climate-environment.json"
UPGRADE = ROOT / "packs/climate-environment/source_packs/geospatial-berlin-1.2.0.json"
BASE_GEOSPATIAL = ROOT / "config/source_packs/geospatial.json"
CAPTURED_AT = "authored 2026-09-26 (not a capture)"
NOTE = ("Authored offline fixture in the provider's documented response shape; identifiers are illustrative and "
        "values are invented. Not live evidence.")
UMWELTATLAS = [
    ("umweltatlas-umweltzone", "umweltatlas_umweltzone.geojson", "https://gdi.berlin.de/services/wfs/ua_umweltzone",
     "ua_umweltzone:umweltzone", "bezeichnung", ["MultiPolygon"],
     "Berlin low-emission zone (Umweltzone) outline; complete collection"),
    ("umweltatlas-laerm-lden", "umweltatlas_laerm_lden.geojson", "https://gdi.berlin.de/services/wfs/ua_stratlaerm_2022",
     "ua_stratlaerm_2022:laerm_lden", "lden_klasse", ["Polygon"],
     "Strategic noise map 2022, road traffic L_DEN bands (computed map, not measurements); complete collection"),
    ("umweltatlas-klimafunktion", "umweltatlas_klimafunktion.geojson", "https://gdi.berlin.de/services/wfs/ua_klimaanalyse_2022",
     "ua_klimaanalyse_2022:klimafunktion", "klimafunktion", ["Polygon"],
     "Climate analysis map 2022, climate functions (model result, not measurements); complete collection"),
]


def _raw(name):
    return (RAW / name).read_bytes()


def _zip(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in members:
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 25, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def route(provider, step, overrides=None):
    """Raw bytes and content type for one planned request (``overrides`` maps a raw file name to another name or to bytes)."""

    overrides = overrides or {}

    def raw(name):
        replacement = overrides.get(name, name)
        return replacement if isinstance(replacement, bytes) else _raw(replacement)

    url, params = step["url"], step["params"]
    path = urlsplit(url).path
    if provider == "openaq":
        parts = path.strip("/").split("/")
        if parts[1] == "locations":
            return raw(f"openaq_location_{parts[2]}.json"), "application/json"
        return raw(f"openaq_sensor_{parts[2]}_hours.json"), "application/json"
    if provider == "uba":
        if path.endswith("/stations/json"):
            return raw("uba_stations.json"), "application/json"
        if path.endswith("/components/json"):
            return raw("uba_components.json"), "application/json"
        return raw(f"uba_measures_{params['station']}_{params['component']}.json"), "application/json"
    if provider == "entsoe":
        document = params["documentType"], params.get("processType")
        if document == ("A75", "A16"):
            return raw("entsoe_a75_generation.xml"), "text/xml"
        if document == ("A65", "A16"):
            return raw("entsoe_a65_load.xml"), "text/xml"
        if document == ("A65", "A01"):
            return raw("entsoe_a65_load_forecast.xml"), "text/xml"
        return _zip([("001-planned.xml", raw("entsoe_a80_planned.xml")),
                     ("002-unplanned.xml", raw("entsoe_a80_unplanned.xml"))]), "application/zip"
    if provider == "smard":
        return raw("smard_" + path.rsplit("/", 1)[1]), "application/json"
    if provider == "eea-industry":
        return raw("eea_industry_berlin_2024.json"), "application/json"
    if provider == "eu-ets":
        return raw("eu_ets_verified_2025.csv"), "text/csv"
    if provider == "open-meteo-archive":
        return raw("open_meteo_archive_era5.json"), "application/json"
    if provider == "open-meteo-forecast":
        if path.endswith("meta.json"):
            return raw("open_meteo_meta_icon_d2.json"), "application/json"
        return raw("open_meteo_forecast_icon_d2.json"), "application/json"
    if provider == "dwd":
        if path.endswith(".txt"):
            return raw("TU_Stundenwerte_Beschreibung_Stationen.txt"), "text/plain"
        return _zip([("produkt_tu_stunde_20250324_20260924_00399.txt",
                      raw("produkt_tu_stunde_20250324_20260924_00399.txt")),
                     ("Metadaten_Geographie_00399.txt", b"Stations_id;Stationshoehe;Geogr.Breite;Geogr.Laenge\n399;37;52.5198;13.4057\n")]), "application/zip"
    raise KeyError(provider)


def _page(step, body, content_type):
    page = {"url": step["url"], "params": dict(step["params"]), "status": 200, "headers": {"Content-Type": content_type},
            "parse": step["parse"], "body_sha256": hashlib.sha256(body).hexdigest()}
    try:
        text = body.decode("utf-8")
        if content_type == "application/zip":
            raise UnicodeDecodeError("zip", b"", 0, 1, "binary")
        page["body"] = text
    except UnicodeDecodeError:
        page["body_base64"] = base64.b64encode(body).decode()
    return page


def _write(path, payload):
    text = json.dumps(payload, indent=1, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def environment_fixture(source):
    from src.ingestion.environment_providers import plan

    spec = source["environment"]
    pages = [_page(step, *route(spec["provider"], step)) for step in plan(spec["provider"], spec["selection"])]
    return {"captured_at": CAPTURED_AT, "provider": spec["provider"], "note": NOTE, "endpoint": source["endpoint"],
            "scenarios": sorted({p["parse"] for p in pages}), "native_pages": pages}


def wfs_fixture(endpoint, type_names, raw_name):
    body = _raw(raw_name).decode("utf-8")
    collection = json.loads(body)
    params = {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": type_names,
              "OUTPUTFORMAT": "application/json", "SRSNAME": "urn:ogc:def:crs:EPSG::25833", "SORTBY": "gml_id",
              "COUNT": 50, "STARTINDEX": 0}
    return {"captured_at": CAPTURED_AT, "provider": "Geoportal Berlin (Umweltatlas) — authored fixture", "license": "dl-de/zero-2-0",
            "note": NOTE + " Geometries are simplified boxes in EPSG:25833.", "endpoint": endpoint,
            "scenarios": ["projected-crs-epsg-25833", "complete-snapshot", "umweltatlas-layer"],
            "native_pages": [{"start_index": 0, "status": 200, "params": params,
                              "body_sha256": hashlib.sha256(body.encode()).hexdigest(), "body": body}],
            "features": len(collection["features"])}


def upgrade_manifest():
    manifest = json.loads(BASE_GEOSPATIAL.read_text())
    manifest["version"] = "1.2.0"
    manifest["description"] += (" Version 1.2.0 (Climate and Environment) adds selected Berlin Umweltatlas layers "
                                "(low-emission zone, strategic noise map bands, climate functions) through the same "
                                "WFS 2.0.0 path; existing sources are unchanged.")
    for source_id, raw_name, endpoint, type_names, title, geometry_types, scope in UMWELTATLAS:
        manifest["sources"].append({
            "source_id": source_id, "endpoint": endpoint, "publisher": "Geoportal Berlin (Umweltatlas)", "scope": scope,
            "update_cadence": "irregular; per Umweltatlas map edition",
            "budgets": {"timeout_ms": 60000, "max_results": 1000, "max_bytes": 5000000, "max_pages": 10},
            "wfs": {"version": "2.0.0", "type_names": type_names, "output_format": "application/json",
                    "srs_name": "urn:ogc:def:crs:EPSG::25833", "axis_order": "east_north", "sort_by": "gml_id",
                    "page_size": 50},
            "geospatial": {"namespace": "global", "title_property": title,
                           "precision": {"status": "unknown", "basis": "Umweltatlas map products declare no positional accuracy in the WFS response"},
                           "geometry_types": geometry_types,
                           "metadata_url": "https://fbinter.stadt-berlin.de/fb/index.jsp",
                           "attribution": "Geoportal Berlin / Umweltatlas (dl-de/zero-2-0; attribution not required)",
                           "live_verification": "blocked",
                           "layer_kind": "map product (modelled or administrative), not measurements"},
            "fixture": {"path": f"tests/fixtures/source_packs/{source_id}.json", "sha256": "0" * 64,
                        "expected_output_hash": "0" * 64},
        })
    return manifest


def _pin(manifest, fixtures):
    from src.ingestion.source_packs import SourcePackConformance, validate_source_pack

    for source in manifest["sources"]:
        if source["source_id"] in fixtures:
            source["fixture"]["sha256"] = fixtures[source["source_id"]]
    result = SourcePackConformance(ROOT).offline(validate_source_pack(manifest))
    outputs = {item["source_id"]: item["output_hash"] for item in result["sources"]}
    for source in manifest["sources"]:
        if source["source_id"] in fixtures:
            source["fixture"]["expected_output_hash"] = outputs[source["source_id"]]
    return manifest


def build(write=True):
    """Regenerate every fixture and manifest; returns {path: text} (written when ``write``)."""

    pack = json.loads(PACK.read_text())
    outputs = {}
    fixtures = {}
    for source in pack["sources"]:
        path = ROOT / source["fixture"]["path"]
        payload = environment_fixture({**pack.get("defaults", {}), **source})
        text = json.dumps(payload, indent=1, ensure_ascii=False) + "\n"
        outputs[path] = text
        fixtures[source["source_id"]] = hashlib.sha256(text.encode()).hexdigest()
        if write:
            path.write_text(text, encoding="utf-8")
    upgrade = upgrade_manifest()
    upgrade_fixtures = {}
    for source_id, raw_name, endpoint, type_names, *_ in UMWELTATLAS:
        path = OUT / f"{source_id}.json"
        text = json.dumps(wfs_fixture(endpoint, type_names, raw_name), indent=1, ensure_ascii=False) + "\n"
        outputs[path] = text
        upgrade_fixtures[source_id] = hashlib.sha256(text.encode()).hexdigest()
        if write:
            path.write_text(text, encoding="utf-8")
    if write:
        pinned_pack = _pin(copy.deepcopy(pack), fixtures)
        pinned_upgrade = _pin(upgrade, upgrade_fixtures)
    else:
        pinned_pack, pinned_upgrade = pack, json.loads(UPGRADE.read_text())
    for path, manifest in ((PACK, pinned_pack), (UPGRADE, pinned_upgrade)):
        text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        outputs[path] = text
        if write:
            path.write_text(text, encoding="utf-8")
    return outputs


if __name__ == "__main__":
    for written in build():
        print(written.relative_to(ROOT))
