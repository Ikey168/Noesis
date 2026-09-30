"""Build the pinned real-estate source-pack fixtures and manifest from the authored payloads below (#2228).

Every payload is authored in the publisher's documented file or response shape.
Transaction ids, postcodes (the fictional ``ZZ`` area), prices, parcel ids,
index values, dates (2098-2099) and geometries are FICTIONAL; GSS, INSEE and
GEO codes are real only so the bounded coverage reads naturally. Nothing here is
live evidence. Run ``python -m tests.unit.real_estate.fixture_builder`` after
editing a payload; ``test_fixtures_are_pinned_and_in_sync`` fails when they drift.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "packs/geospatial/source_packs/geospatial-real-estate.json"
OUT = ROOT / "tests/fixtures/source_packs"
NOTE = ("Authored offline fixture in the publisher's documented shape; identifiers, prices, index values, dates and "
        "geometries are FICTIONAL and not live evidence; verify against the live sources before any live run "
        "(RE13, #2519).")
PPD_PATH = "/pp-monthly-update-new-version.csv"
EUROSTAT_PATH = "/eurostat/api/dissemination/statistics/1.0/data/prc_hpi_q"
FR_SRS, DE_SRS = "urn:ogc:def:crs:EPSG::25831", "urn:ogc:def:crs:EPSG::25832"
FR_BBOX = [452000.0, 5410500.0, 453500.0, 5412000.0]
DE_BBOX = [356000.0, 5644500.0, 357500.0, 5646000.0]
FR_PROPERTIES = ["localId", "namespace", "nationalCadastralReference", "label", "areaValue", "beginLifespanVersion"]
DE_PROPERTIES = FR_PROPERTIES


def last_modified(day: str) -> str:
    from datetime import datetime

    return datetime.fromisoformat(day).strftime("%a, %d %b %Y 08:00:00 GMT")


def request(path: str, query: dict[str, str] | None = None) -> str:
    from src.ingestion.real_estate_sources import fixture_request

    return fixture_request(path, query)


def page(key: str, body: str, content_type: str = "text/csv", *, modified: str | None = None) -> dict[str, Any]:
    headers = {"Content-Type": content_type}
    if modified:
        headers["Last-Modified"] = last_modified(modified)
    return {"request": key, "status": 200, "headers": headers, "body": body}


def box(x: float, y: float, size: float = 40.0) -> dict[str, Any]:
    return {"type": "Polygon", "coordinates": [[[x, y], [x + size, y], [x + size, y + size], [x, y + size], [x, y]]]}


# ------------------------------------------------------------------ HM Land Registry Price Paid Data

TX1, TX2, TX3, TX4 = ("{A1B2C3D4-0001-4E00-9F00-000000000001}", "{A1B2C3D4-0002-4E00-9F00-000000000002}",
                      "{A1B2C3D4-0003-4E00-9F00-000000000003}", "{A1B2C3D4-0004-4E00-9F00-000000000004}")


def _csv(rows: list[list[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\n")
    writer.writerows(rows)
    return buffer.getvalue()


def ppd_csv(release: int) -> str:
    if release == 1:
        rows = [
            [TX1, "450000", "2099-01-10 00:00", "ZZ1 1AA", "F", "N", "L", "12", "FLAT 3", "EXAMPLE STREET", "",
             "LONDON", "FICTIONAL BOROUGH", "GREATER LONDON", "A", "A"],
            [TX2, "780000", "2099-01-17 00:00", "ZZ1 2BB", "D", "Y", "F", "7", "", "SAMPLE ROAD", "", "LONDON",
             "FICTIONAL BOROUGH", "GREATER LONDON", "B", "A"],
            [TX3, "210000", "2099-01-20 00:00", "XX9 9ZZ", "T", "N", "F", "1", "", "ELSEWHERE LANE", "", "OTHERTOWN",
             "OTHER DISTRICT", "OTHER COUNTY", "A", "A"],
        ]
    else:
        rows = [
            [TX1, "455000", "2099-01-10 00:00", "ZZ1 1AA", "F", "N", "L", "12", "FLAT 3", "EXAMPLE STREET", "",
             "LONDON", "FICTIONAL BOROUGH", "GREATER LONDON", "A", "C"],
            [TX2, "780000", "2099-01-17 00:00", "ZZ1 2BB", "D", "Y", "F", "7", "", "SAMPLE ROAD", "", "LONDON",
             "FICTIONAL BOROUGH", "GREATER LONDON", "B", "D"],
            [TX4, "320000", "2099-02-03 00:00", "ZZ2 3CC", "T", "N", "F", "3", "", "MODEL CLOSE", "", "LONDON",
             "FICTIONAL BOROUGH", "GREATER LONDON", "A", "A"],
        ]
    return _csv(rows)


def ppd_pages(release: int = 1) -> list[dict[str, Any]]:
    return [page(request(PPD_PATH), ppd_csv(release), modified="2099-02-26" if release == 1 else "2099-03-27")]


# ------------------------------------------------------------------ UK House Price Index

UKHPI_HEADER = ["Date", "RegionName", "AreaCode", "AveragePrice", "Index", "IndexSA", "1m%Change", "12m%Change",
                "AveragePriceSA", "SalesVolume"]
UKHPI_RELEASES = {
    "2099-03": ("2099-03-18", [("01/12/2098", "City of Westminster", "E09000033", "901000", "151.2", "12"),
                               ("01/01/2099", "City of Westminster", "E09000033", "905500", "151.9", "9"),
                               ("01/12/2098", "England", "E92000001", "301200", "139.4", "60100"),
                               ("01/01/2099", "England", "E92000001", "302000", "139.8", "55210"),
                               ("01/01/2099", "Wales", "W92000004", "201000", "137.0", "3100")]),
    "2099-04": ("2099-04-15", [("01/12/2098", "City of Westminster", "E09000033", "901000", "151.2", "12"),
                               ("01/01/2099", "City of Westminster", "E09000033", "907100", "152.2", "11"),
                               ("01/02/2099", "City of Westminster", "E09000033", "910300", "152.7", "8"),
                               ("01/12/2098", "England", "E92000001", "301200", "139.4", "60100"),
                               ("01/01/2099", "England", "E92000001", "302000", "139.8", "57002"),
                               ("01/02/2099", "England", "E92000001", "302600", "140.1", "49870")]),
}


def ukhpi_path(release: str) -> str:
    return f"/market-trend-data/house-price-index-data/UK-HPI-full-file-{release}.csv"


def ukhpi_document(release: str) -> dict[str, Any]:
    return {"label": f"UK HPI full file {release} (fictional values)", "path": ukhpi_path(release),
            "release": release, "published_on": UKHPI_RELEASES[release][0],
            "geographies": ["E09000033", "E92000001"], "since": "2098-12",
            "base_period": "January 2015 = 100 (as published)"}


def ukhpi_csv(release: str) -> str:
    rows = [UKHPI_HEADER] + [[d, name, code, price, index, "", "", "", "", volume]
                             for d, name, code, price, index, volume in UKHPI_RELEASES[release][1]]
    return _csv(rows)


def ukhpi_pages() -> list[dict[str, Any]]:
    """Both releases: the manifest declares 2099-03; a later pack version declaring 2099-04 replays the second."""
    return [page(request(ukhpi_path(release)), ukhpi_csv(release)) for release in UKHPI_RELEASES]


# ------------------------------------------------------------------ DVF géolocalisées

DVF_HEADER = ["id_mutation", "date_mutation", "numero_disposition", "nature_mutation", "valeur_fonciere",
              "adresse_numero", "adresse_suffixe", "adresse_nom_voie", "adresse_code_voie", "code_postal",
              "code_commune", "nom_commune", "code_departement", "ancien_code_commune", "ancien_nom_commune",
              "id_parcelle", "ancien_id_parcelle", "numero_volume", "lot1_numero", "lot1_surface_carrez",
              "nombre_lots", "code_type_local", "type_local", "surface_reelle_bati", "nombre_pieces_principales",
              "code_nature_culture", "nature_culture", "code_nature_culture_speciale", "nature_culture_speciale",
              "surface_terrain", "longitude", "latitude"]
DVF_RELEASES = {"2099-04": "2099-04-15", "2099-10": "2099-10-14"}


def _dvf_row(mutation, date, value, number, street, parcel, lots, code_local, local, surface, rooms):
    return [mutation, date, "000001", "Vente", value, number, "", street, "0001", "75004", "75104",
            "Paris 4e Arrondissement", "75", "", "", parcel, "", "", "", "", lots, code_local, local, surface, rooms,
            "", "", "", "", "", "", ""]


def dvf_csv(release: str) -> str:
    rows = [DVF_HEADER,
            # One mutation over two parcels and two locals: one published value for the whole mutation.
            _dvf_row("2098-1001", "2098-03-14", "1250000,00", "10", "RUE FICTIVE", "75104000AB0012", "2", "2",
                     "Appartement", "85", "3"),
            _dvf_row("2098-1001", "2098-03-14", "1250000,00", "10", "RUE FICTIVE", "75104000AB0013", "2", "3",
                     "Dépendance", "", "0")]
    if release == "2099-04":
        rows += [_dvf_row("2098-1002", "2098-06-02", "640000,00", "4", "QUAI IMAGINAIRE", "75104000AB0014", "1",
                          "2", "Appartement", "48", "2"),
                 _dvf_row("2098-1003", "2098-07-21", "99000,00", "4", "QUAI IMAGINAIRE", "75104000AB0015", "1", "3",
                          "Dépendance", "", "0")]
    else:
        rows += [_dvf_row("2098-1002", "2098-06-02", "645000,00", "4", "QUAI IMAGINAIRE", "75104000AB0014", "1",
                          "2", "Appartement", "48", "2"),
                 _dvf_row("2098-1004", "2098-11-09", "380000,00", "22", "RUE FICTIVE", "75104000AB0012", "1", "2",
                          "Appartement", "30", "1")]
    return _csv(rows)


def dvf_path(release: str) -> str:
    return f"/geo-dvf/{release}/csv/2098/communes/75/75104.csv"


def dvf_document(release: str) -> dict[str, Any]:
    return {"label": f"DVF géolocalisées {release}, Paris 4e, 2098 (fictional mutations)", "path": dvf_path(release),
            "release": release, "published_on": DVF_RELEASES[release], "commune": "75104", "year": "2098"}


def dvf_pages() -> list[dict[str, Any]]:
    """Both releases: the manifest declares 2099-04; a later pack version declaring 2099-10 replays the second."""
    return [page(request(dvf_path(release)), dvf_csv(release)) for release in DVF_RELEASES]


# ------------------------------------------------------------------ Eurostat prc_hpi_q

EUROSTAT_FILTERS = {"freq": "Q", "purchase": "TOTAL", "unit": "I15_Q"}
EUROSTAT_VALUES = {
    1: {"updated": "2099-01-10T11:00:00+0100", "times": ["2098-Q1", "2098-Q2"],
        "FR": {"0": 131.2, "1": 132.0}, "DE": {"0": 158.4, "1": 157.9}, "status": {"1": "p"}},
    2: {"updated": "2099-04-11T11:00:00+0100", "times": ["2098-Q1", "2098-Q2", "2098-Q3"],
        "FR": {"0": 131.2, "1": 132.4, "2": 132.9}, "DE": {"0": 158.4, "1": 157.9, "2": 158.8}, "status": {"2": "p"}},
}


def eurostat_cube(geo: str, vintage: int, *, unit: str = "I15_Q", unit_label: str = "Index, 2015=100") -> str:
    spec = EUROSTAT_VALUES[vintage]
    times = spec["times"]
    return json.dumps({
        "version": "2.0", "class": "dataset", "label": "House price index (2015 = 100) - quarterly data",
        "source": "ESTAT", "updated": spec["updated"], "id": ["freq", "purchase", "unit", "geo", "time"],
        "size": [1, 1, 1, 1, len(times)], "value": spec[geo], "status": spec["status"],
        "dimension": {
            "freq": {"label": "Time frequency", "category": {"index": {"Q": 0}, "label": {"Q": "Quarterly"}}},
            "purchase": {"label": "Purchases", "category": {"index": {"TOTAL": 0},
                                                            "label": {"TOTAL": "Total"}}},
            "unit": {"label": "Unit of measure", "category": {"index": {unit: 0}, "label": {unit: unit_label}}},
            "geo": {"label": "Geopolitical entity", "category": {"index": {geo: 0},
                                                                 "label": {geo: {"FR": "France",
                                                                                 "DE": "Germany"}[geo]}}},
            "time": {"label": "Time", "category": {"index": {t: i for i, t in enumerate(times)},
                                                   "label": {t: t for t in times}}}},
        "extension": {"status": {"label": {"p": "provisional", "e": "estimated", "b": "break in time series"}}},
    }, sort_keys=True)


EUROSTAT_DOCUMENT = {"label": "Eurostat prc_hpi_q, TOTAL, I15_Q, FR and DE", "path": EUROSTAT_PATH,
                     "dataset": "prc_hpi_q", "geo": ["FR", "DE"], "since": "2098-Q1", "filters": EUROSTAT_FILTERS}


def eurostat_pages(vintage: int = 1) -> list[dict[str, Any]]:
    from src.ingestion.real_estate_sources import request_for

    return [page(request(path, query), eurostat_cube(geo, vintage), "application/json")
            for geo, (path, query) in request_for("eurostat-hpi", EUROSTAT_DOCUMENT).items()]


# ------------------------------------------------------------------ INSPIRE cadastral parcels (WFS)

FR_PARCELS = {
    "75104000AB0012": (452600.0, 5410900.0),
    "75104000AB0013": (452640.0, 5410900.0),
}
DE_PARCELS = {"053001001000120003______": (356700.0, 5645000.0)}


def fr_features(snapshot: int = 1) -> list[dict[str, Any]]:
    features = []
    for number, (reference, (x, y)) in enumerate(sorted(FR_PARCELS.items()), start=1):
        size = 40.0 if not (snapshot == 2 and reference.endswith("0013")) else 55.0
        properties = {"localId": reference, "namespace": "FR.CADASTRALPARCELS",
                      "nationalCadastralReference": reference, "label": f"AB {int(reference[-4:])}",
                      "areaValue": int(size * size / 4), "beginLifespanVersion": "2090-01-01T00:00:00Z"}
        if number == 1:
            # A service ignoring PROPERTYNAME: the rights-holder attribute must never be kept.
            properties["proprietaire"] = "NOM FICTIF (must be dropped)"
        features.append({"type": "Feature", "id": f"CP.CadastralParcel.{reference}", "geometry": box(x, y, size),
                         "properties": properties})
    return features


def de_features() -> list[dict[str, Any]]:
    return [{"type": "Feature", "id": f"cp.{reference}", "geometry": box(x, y, 30.0),
             "properties": {"localId": f"DENW36AL{reference[:10]}FL", "namespace": "https://registry.gdi-de.org/id/"
                            "de.nw.inspire.cp.alkis", "nationalCadastralReference": reference, "label": "12/3",
                            "areaValue": 225, "beginLifespanVersion": "2091-05-01T00:00:00Z"}}
            for reference, (x, y) in DE_PARCELS.items()]


def wfs_params(type_names: str, srs: str, bbox: list[float], properties: list[str]) -> dict[str, Any]:
    return {"SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature", "TYPENAMES": type_names,
            "OUTPUTFORMAT": "application/json", "SRSNAME": srs, "SORTBY": "localId", "COUNT": 50, "STARTINDEX": 0,
            "PROPERTYNAME": ",".join(properties),
            "BBOX": ",".join(str(float(v)) for v in bbox) + "," + srs}


def wfs_pages(features: list[dict[str, Any]], *, srs: str, params: dict[str, Any], timestamp: str) -> list[dict]:
    body = json.dumps({"type": "FeatureCollection", "features": features, "numberMatched": len(features),
                       "numberReturned": len(features), "timeStamp": timestamp,
                       "crs": {"type": "name", "properties": {"name": srs}}}, ensure_ascii=False, sort_keys=True)
    return [{"start_index": 0, "status": 200, "params": params, "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
             "body": body}]


def fr_pages(snapshot: int = 1) -> list[dict[str, Any]]:
    return wfs_pages(fr_features(snapshot), srs=FR_SRS,
                     params=wfs_params("CP.CadastralParcel", FR_SRS, FR_BBOX, FR_PROPERTIES),
                     timestamp="2099-03-01T08:00:00Z" if snapshot == 1 else "2099-09-01T08:00:00Z")


def de_pages() -> list[dict[str, Any]]:
    return wfs_pages(de_features(), srs=DE_SRS, params=wfs_params("cp:CadastralParcel", DE_SRS, DE_BBOX,
                                                                  DE_PROPERTIES),
                     timestamp="2099-03-01T08:00:00Z")


# ------------------------------------------------------------------ manifest

NATIVE_COMMON = {
    "connector": "real-estate",
    "mapping": {"target_schema": "noesis-real-estate-record-v1", "version": "1.0.0"},
    "extractor_versions": ["real-estate-sources:1.0.0"],
    "operations": ["publications"],
    "auth": {"kind": "none"},
}
PARCEL_ATTRIBUTES = {"local_id": "localId", "namespace": "namespace", "national_reference": "nationalCadastralReference",
                     "label": "label", "area_m2": "areaValue", "valid_from": "beginLifespanVersion"}


def _wfs_source(source_id, endpoint, publisher, scope, type_names, srs, bbox, provider, country, reference_scheme,
                licence, attribution):
    return {
        "source_id": source_id, "connector": "wfs", "endpoint": endpoint, "publisher": publisher, "scope": scope,
        "update_cadence": "re-read quarterly within the pinned bbox",
        "temporal_semantics": "provider-snapshot geometry with beginLifespanVersion as published; a changed geometry "
                              "or reference is a new parcel revision",
        "license": licence,
        "mapping": {"target_schema": "noesis-real-estate-record-v1", "version": "1.0.0"},
        "extractor_versions": ["wfs-2.0.0-geojson:1.0.0", "geospatial-feature-projection:1.0.0",
                               "real-estate-parcel-projection:1.0.0"],
        "operations": ["features"],
        "budgets": {"timeout_ms": 60000, "max_results": 500, "max_bytes": 5000000, "max_pages": 10},
        "health": {"required": False, "max_staleness_s": 15552000},
        "wfs": {"version": "2.0.0", "type_names": type_names, "output_format": "application/json", "srs_name": srs,
                "axis_order": "east_north", "sort_by": "localId", "page_size": 50, "id_property": "localId",
                "bbox": bbox, "property_names": list(FR_PROPERTIES)},
        "geospatial": {"namespace": "global", "title_property": "label",
                       "precision": {"status": "unknown", "basis": "the WFS response declares no positional accuracy"},
                       "geometry_types": ["Polygon", "MultiPolygon"], "attribution": attribution,
                       "live_verification": "unverified-live"},
        "real_estate": {"kind": "parcels", "provider": provider, "country": country, "namespace": "global",
                        "reference_scheme": reference_scheme, "attributes": dict(PARCEL_ATTRIBUTES),
                        "live_verification": "unverified-live"},
    }


def sources() -> list[dict[str, Any]]:
    return [
        {"source_id": "hmlr-price-paid-data", "endpoint": "https://price-paid-data.publicdata.landregistry.gov.uk",
         "publisher": "HM Land Registry (Price Paid Data)",
         "scope": "The monthly Price Paid Data change file, kept for the declared postcode districts only: "
                  "additions, changes and deletions of residential sales as published",
         **copy.deepcopy(NATIVE_COMMON),
         "update_cadence": "monthly (20th working day, verify)",
         "temporal_semantics": "transfer date as published; the file's Last-Modified month is the release; C rows "
                               "are revisions and D rows withdraw a transaction, never erasing it",
         "license": {"id": "ogl-uk-3.0", "terms_url": "https://www.nationalarchives.gov.uk/doc/open-government-"
                     "licence/version/3/", "redistribution": "permitted with the HM Land Registry attribution"},
         "budgets": {"timeout_ms": 120000, "max_results": 5000, "max_bytes": 40000000, "max_pages": 1},
         "schedule": {"kind": "interval", "interval_s": 2592000},
         "health": {"required": False, "max_staleness_s": 5184000},
         "real_estate": {"provider": "hmlr-ppd", "namespace": "global", "live_verification": "unverified-live",
                         "documents": [{"label": "PPD monthly change file (fictional rows)", "path": PPD_PATH,
                                        "release_from": "last-modified",
                                        "postcode_districts": ["ZZ1", "ZZ2"]}]},
         "fixture": {"path": "tests/fixtures/source_packs/real-estate-ppd.json"}},
        {"source_id": "hmlr-uk-hpi", "endpoint": "https://publicdata.landregistry.gov.uk",
         "publisher": "HM Land Registry / ONS (UK House Price Index)",
         "scope": "The UK HPI full file of each declared monthly release, kept for the declared GSS geography codes "
                  "and months: index, average price and sales volume as published",
         **copy.deepcopy(NATIVE_COMMON),
         "update_cadence": "monthly", "temporal_semantics": "monthly periods; each release is a vintage and revised "
                                                            "months are revisions",
         "license": {"id": "ogl-uk-3.0", "terms_url": "https://www.nationalarchives.gov.uk/doc/open-government-"
                     "licence/version/3/", "redistribution": "permitted with attribution"},
         "budgets": {"timeout_ms": 120000, "max_results": 2000, "max_bytes": 60000000, "max_pages": 4},
         "schedule": {"kind": "interval", "interval_s": 2592000},
         "health": {"required": False, "max_staleness_s": 5184000},
         "real_estate": {"provider": "hmlr-ukhpi", "namespace": "global", "live_verification": "unverified-live",
                         "documents": [ukhpi_document("2099-03")]},
         "fixture": {"path": "tests/fixtures/source_packs/real-estate-ukhpi.json"}},
        {"source_id": "dvf-geolocalisees", "endpoint": "https://files.data.gouv.fr",
         "publisher": "DGFiP / Etalab (Demandes de valeurs foncières géolocalisées)",
         "scope": "The DVF file of each declared commune, year and semi-annual release: mutations with their date, "
                  "nature, published value and cadastral parcel ids",
         **copy.deepcopy(NATIVE_COMMON),
         "update_cadence": "semi-annual (April, October)",
         "temporal_semantics": "mutation date as published; each release is a vintage; a changed mutation is a "
                               "revision and one absent from a later release is a dated removal",
         "license": {"id": "etalab-2.0", "terms_url": "https://www.etalab.gouv.fr/licence-ouverte-open-licence/",
                     "redistribution": "permitted with attribution; no re-identification of persons and no indexing "
                                       "by external search engines (DVF conditions)"},
         "budgets": {"timeout_ms": 60000, "max_results": 2000, "max_bytes": 10000000, "max_pages": 4},
         "schedule": {"kind": "interval", "interval_s": 7776000},
         "health": {"required": False, "max_staleness_s": 20736000},
         "real_estate": {"provider": "dvf", "namespace": "global", "live_verification": "unverified-live",
                         "documents": [dvf_document("2099-04")]},
         "fixture": {"path": "tests/fixtures/source_packs/real-estate-dvf.json"}},
        {"source_id": "eurostat-house-price-index", "endpoint": "https://ec.europa.eu",
         "publisher": "Eurostat (prc_hpi_q)",
         "scope": "House price index prc_hpi_q for the declared GEO codes, purchase TOTAL and unit I15_Q since the "
                  "declared quarter, with flags as published",
         **copy.deepcopy(NATIVE_COMMON),
         "update_cadence": "quarterly",
         "temporal_semantics": "quarterly periods; the cube's updated stamp is the vintage; revised quarters are "
                               "revisions; a rebased index is a new series edition",
         "license": {"id": "eurostat-reuse", "terms_url": "https://ec.europa.eu/eurostat/about-us/policies/copyright",
                     "redistribution": "permitted with attribution (Commission Decision 2011/833/EU)"},
         "budgets": {"timeout_ms": 60000, "max_results": 1000, "max_bytes": 2000000, "max_pages": 2},
         "schedule": {"kind": "interval", "interval_s": 2592000},
         "health": {"required": False, "max_staleness_s": 10368000},
         "real_estate": {"provider": "eurostat-hpi", "namespace": "global", "live_verification": "unverified-live",
                         "documents": [EUROSTAT_DOCUMENT]},
         "fixture": {"path": "tests/fixtures/source_packs/real-estate-eurostat.json"}},
        {**_wfs_source("inspire-cp-france", "https://data.geopf.fr/wfs/ows",
                       "IGN / DGFiP (INSPIRE Cadastral Parcels, France)",
                       "INSPIRE cadastral parcels inside the pinned Paris 4e bounding box: localId, namespace, "
                       "nationalCadastralReference, label, area and lifespan start; no rights-holder attribute",
                       "CP.CadastralParcel", FR_SRS, FR_BBOX, "inspire-cp-fr", "FR", "fr-id-parcelle",
                       {"id": "etalab-2.0", "terms_url": "https://www.etalab.gouv.fr/licence-ouverte-open-licence/",
                        "redistribution": "permitted with attribution (operator must confirm for the CP layer)"},
                       "IGN - DGFiP, Parcellaire (INSPIRE CP) (Licence Ouverte 2.0; verify)"),
         "fixture": {"path": "tests/fixtures/source_packs/real-estate-inspire-fr.json"}},
        {**_wfs_source("inspire-cp-nordrhein-westfalen",
                       "https://www.wfs.nrw.de/geobasis/wfs_nw_inspire-flurstuecke_alkis",
                       "Geobasis NRW (INSPIRE Cadastral Parcels, Nordrhein-Westfalen)",
                       "INSPIRE cadastral parcels inside the pinned Köln-Altstadt bounding box: localId, namespace, "
                       "nationalCadastralReference, label, area and lifespan start",
                       "cp:CadastralParcel", DE_SRS, DE_BBOX, "inspire-cp-de-nw", "DE", "de-flurstueckskennzeichen",
                       {"id": "dl-de-zero-2.0", "terms_url": "https://www.govdata.de/dl-de/zero-2-0",
                        "redistribution": "permitted-without-conditions"},
                       "Geobasis NRW (dl-de/zero-2-0)"),
         "fixture": {"path": "tests/fixtures/source_packs/real-estate-inspire-de-nw.json"}},
    ]


DEFAULTS = {
    "connector": "real-estate",
    "update_cadence": "per source; monthly (PPD, UK HPI), semi-annual (DVF), quarterly (Eurostat, parcels)",
    "temporal_semantics": "published dates as released; every release read is a vintage; corrections, deletions "
                          "and removals are revisions, never overwrites",
    "mapping": {"target_schema": "noesis-real-estate-record-v1", "version": "1.0.0"},
    "extractor_versions": ["real-estate-sources:1.0.0"],
    "operations": ["publications"],
    "schedule": {"kind": "interval", "interval_s": 2592000},
    "auth": {"kind": "none"},
    "health": {"required": False, "max_staleness_s": 10368000},
    "budgets": {"timeout_ms": 60000, "max_results": 1000, "max_bytes": 10000000, "max_pages": 4},
    "policy": {"excluded": ["property valuation or price estimates", "owner or party profiling",
                            "re-identification of individuals", "investment advice",
                            "prices apportioned, converted, averaged or interpolated"]},
}
FIXTURES = {
    "hmlr-price-paid-data": ("real-estate-ppd", "HM Land Registry PPD monthly change file", ppd_pages,
                             ["standard and additional price paid rows", "a row outside the declared districts"]),
    "hmlr-uk-hpi": ("real-estate-ukhpi", "UK HPI full file of release 2099-03", ukhpi_pages,
                    ["borough and England rows", "a geography outside the declaration",
                     "release 2099-04 revising January and adding February (declared by a later pack version)"]),
    "dvf-geolocalisees": ("real-estate-dvf", "DVF géolocalisées Paris 4e 2098, release 2099-04", dvf_pages,
                          ["a mutation over two parcels and two locals", "single-parcel mutations",
                           "release 2099-10 correcting one value, dropping one mutation and adding one (declared by "
                           "a later pack version)"]),
    "eurostat-house-price-index": ("real-estate-eurostat", "Eurostat prc_hpi_q JSON-stat cubes for FR and DE",
                                   eurostat_pages, ["a provisional flag"]),
    "inspire-cp-france": ("real-estate-inspire-fr", "INSPIRE CP WFS GetFeature response (France, EPSG:25831)",
                          fr_pages, ["two parcels named by DVF", "a rights-holder property the service should not "
                                                                  "have sent (dropped)"]),
    "inspire-cp-nordrhein-westfalen": ("real-estate-inspire-de-nw",
                                       "INSPIRE CP WFS GetFeature response (NRW, EPSG:25832)", de_pages,
                                       ["one parcel"]),
}


def build(write: bool = True) -> dict[Path, str]:
    """Regenerate fixtures and the pinned manifest; returns {path: text}."""
    from src.ingestion.source_packs import SourcePackConformance, validate_source_pack

    outputs, hashes = {}, {}
    for source_id, (name, description, pages, scenarios) in FIXTURES.items():
        payload = {"description": f"{description}. {NOTE}", "scenarios": scenarios, "native_pages": pages()}
        text = json.dumps(payload, indent=1, ensure_ascii=False) + "\n"
        path = OUT / f"{name}.json"
        outputs[path] = text
        hashes[source_id] = hashlib.sha256(text.encode()).hexdigest()
        if write:
            path.write_text(text, encoding="utf-8")
    manifest = {"pack_id": "geospatial-real-estate", "version": "1.0.0",
                "description": "Geospatial real-estate sources for the optional real-estate feature: HM Land Registry "
                               "Price Paid Data and UK House Price Index, French DVF mutations with parcel ids, "
                               "Eurostat house price indices and INSPIRE cadastral parcels (France, Nordrhein-"
                               "Westfalen) through the existing WFS path. Every source is unverified-live until a "
                               "dated run (#2519); no valuation, owner profiling or investment advice.",
                "domains": ["geospatial"], "defaults": DEFAULTS, "sources": sources()}
    for source in manifest["sources"]:
        source["fixture"]["sha256"] = hashes[source["source_id"]]
        source["fixture"]["expected_output_hash"] = "0" * 64
    if write:
        result = SourcePackConformance(ROOT).offline(validate_source_pack(manifest))
        by_id = {item["source_id"]: item["output_hash"] for item in result["sources"]}
        for source in manifest["sources"]:
            source["fixture"]["expected_output_hash"] = by_id[source["source_id"]]
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
