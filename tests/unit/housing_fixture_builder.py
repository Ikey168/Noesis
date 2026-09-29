"""Author the Geospatial housing fixtures and pin them into the ``geospatial-berlin`` 1.3.0 upgrade manifest.

Every fixture is *authored* in the provider's documented native shape - WFS
2.0.0 GetFeature GeoJSON envelopes for the FIS-Broker layers, a declared CSV
layout for the Mietspiegel table and the Statistik Berlin-Brandenburg building
tables, the GENESIS-Online metadata answer and flat-file CSV for Destatis. All
identifiers, plan numbers, values and dates are fictional (years 2097-2101);
geometries are simple boxes in EPSG:25833 around central Berlin. Nothing here
is a live capture.

``python -m tests.unit.housing_fixture_builder`` rewrites the fixtures under
``tests/fixtures/source_packs/geospatial-housing-*.json`` and the manifest
``packs/geospatial/source_packs/geospatial-berlin-1.3.0.json`` (the 1.2.0
upgrade's sources verbatim plus the housing sources);
``test_fixtures_and_manifest_are_pinned_and_in_sync`` fails when they drift.
Later snapshots (a new Stichtag, a changed plan stage, a new edition) are built
by the functions below for tests only and are not pinned in the manifest.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tests/fixtures/source_packs"
BASE = ROOT / "packs/climate-environment/source_packs/geospatial-berlin-1.2.0.json"
MANIFEST = ROOT / "packs/geospatial/source_packs/geospatial-berlin-1.3.0.json"
VERSION = "1.3.0"
AUTHORED = "authored 2026-09-28 (not a capture)"
NOTE = (
    "Authored offline fixture in the provider's documented response shape; identifiers, plan numbers, values and "
    "dates are fictional. Not live evidence."
)
SRS = "urn:ogc:def:crs:EPSG::25833"
LICENSE_ZERO = {
    "id": "dl-de-zero-2.0",
    "terms_url": "https://www.govdata.de/dl-de/zero-2-0",
    "redistribution": "permitted-without-conditions (operator must confirm for this layer)",
}


def box(x: float, y: float, size: float = 1000.0) -> dict[str, Any]:
    ring = [[x, y], [x + size, y], [x + size, y + size], [x, y + size], [x, y]]
    return {"type": "Polygon", "coordinates": [ring]}


# ---------------------------------------------------------------------- WFS layers (FIS-Broker)

BORIS = {
    "source_id": "berlin-boris-bodenrichtwerte",
    "endpoint": "https://gdi.berlin.de/services/wfs/brw",
    "type_names": "brw:bodenrichtwertzonen",
    "sort_by": "wnum",
}
BPLAN = {
    "source_id": "berlin-bebauungsplaene",
    "endpoint": "https://gdi.berlin.de/services/wfs/bplan",
    "type_names": "bplan:bplan_geltungsbereiche",
    "sort_by": "planname",
}
WOHNLAGEN = {
    "source_id": "berlin-wohnlagen",
    "endpoint": "https://gdi.berlin.de/services/wfs/wohnlagen",
    "type_names": "wohnlagen:wohnlagen_2099",
    "sort_by": "blk",
}
# Zones: A and B share the edge x=392000; C lies north of A. The address sits inside A.
ZONES = {
    "1099001": {
        "at": (391000.0, 5820000.0),
        "name": "Musterkiez Nord (fiktiv)",
        "use": "W",
        "gfz": "2.5",
    },
    "1099002": {
        "at": (392000.0, 5820000.0),
        "name": "Beispielplatz Ost (fiktiv)",
        "use": "MI",
        "gfz": "3.0",
    },
    "1099003": {
        "at": (391000.0, 5821000.0),
        "name": "Probeviertel (fiktiv)",
        "use": "W",
        "gfz": "2.0",
    },
}
STICHTAG_VALUES = {
    "2099-01-01": {"1099001": 5400, "1099002": "6100", "1099003": 4800},
    "2100-01-01": {"1099001": 5900, "1099002": "6300", "1099003": 4800},
}


def boris_features(stichtag: str) -> list[dict[str, Any]]:
    features = []
    for number, (wnum, zone) in enumerate(sorted(ZONES.items()), start=1):
        features.append(
            {
                "type": "Feature",
                "id": f"bodenrichtwertzonen.{number}",
                "geometry": box(*zone["at"]),
                "properties": {
                    "wnum": wnum,
                    "brw": STICHTAG_VALUES[stichtag][wnum],
                    "stag": stichtag,
                    "nuta": zone["use"],
                    "gfz": zone["gfz"],
                    "entw": "B",
                    "beit": "frei",
                    "brzname": zone["name"],
                    "gena": "Berlin",
                },
            }
        )
    return features


PLANS = {
    "1-99a": {"at": (391200.0, 5820200.0), "size": 600.0, "bezirk": "Mitte"},
    "1-98": {"at": (392100.0, 5820100.0), "size": 800.0, "bezirk": "Mitte"},
}


def bplan_features(snapshot: int) -> list[dict[str, Any]]:
    stage_99a = (
        {
            "status": "Aufstellungsbeschluss",
            "afs_beschl": "15.03.2098",
            "afs_abl": "ABl. 2098 S. 777",
            "festsg_am": "",
            "gvbl": "",
        }
        if snapshot == 1
        else {
            "status": "festgesetzt",
            "afs_beschl": "15.03.2098",
            "afs_abl": "ABl. 2098 S. 777",
            "festsg_am": "15.06.2099",
            "gvbl": "GVBl. 2099 S. 321",
        }
    )
    stages = {
        "1-99a": stage_99a,
        "1-98": {
            "status": "festgesetzt",
            "afs_beschl": "10.01.2095",
            "afs_abl": "",
            "festsg_am": "01.02.2097",
            "gvbl": "GVBl. 2097 S. 55",
        },
    }
    return [
        {
            "type": "Feature",
            "id": f"bplan_geltungsbereiche.{planname}",
            "geometry": box(*plan["at"], plan["size"]),
            "properties": {
                "planname": planname,
                "bezirk": plan["bezirk"],
                "bereich": f"Geltungsbereich {planname} (fiktiv)",
                **stages[planname],
            },
        }
        for planname, plan in sorted(PLANS.items())
    ]


def wohnlagen_features() -> list[dict[str, Any]]:
    return [
        {
            "type": "Feature",
            "id": "wohnlagen_2099.blk-0001",
            "geometry": box(391000.0, 5820000.0),
            "properties": {"blk": "blk-0001", "wol": "mittel"},
        },
        {
            "type": "Feature",
            "id": "wohnlagen_2099.blk-0002",
            "geometry": box(392000.0, 5820000.0),
            "properties": {"blk": "blk-0002", "wol": "gut"},
        },
    ]


def wfs_fixture(
    layer: dict[str, Any],
    features: list[dict[str, Any]],
    scenarios: list[str],
    timestamp: str,
) -> dict[str, Any]:
    body = json.dumps(
        {
            "type": "FeatureCollection",
            "features": features,
            "totalFeatures": len(features),
            "numberMatched": len(features),
            "numberReturned": len(features),
            "timeStamp": timestamp,
            "crs": {"type": "name", "properties": {"name": SRS}},
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    params = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "TYPENAMES": layer["type_names"],
        "OUTPUTFORMAT": "application/json",
        "SRSNAME": SRS,
        "SORTBY": layer["sort_by"],
        "COUNT": 50,
        "STARTINDEX": 0,
    }
    return {
        "captured_at": AUTHORED,
        "provider": "Geoportal Berlin (FIS-Broker) - authored fixture",
        "license": "dl-de/zero-2-0",
        "source_url": layer["endpoint"],
        "retrieved": AUTHORED,
        "note": NOTE + " Geometries are boxes in EPSG:25833.",
        "endpoint": layer["endpoint"],
        "scenarios": scenarios,
        "native_pages": [
            {
                "start_index": 0,
                "status": 200,
                "params": params,
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "body": body,
            }
        ],
    }


def wfs_source(
    layer: dict[str, Any],
    *,
    publisher: str,
    scope: str,
    cadence: str,
    title: str,
    housing: dict[str, Any],
    metadata_url: str,
) -> dict[str, Any]:
    return {
        "source_id": layer["source_id"],
        "endpoint": layer["endpoint"],
        "publisher": publisher,
        "scope": scope,
        "update_cadence": cadence,
        "temporal_semantics": "provider-snapshot geometry; housing attributes carry their own valuation date, "
        "stage date or edition, never the acquisition time",
        "license": LICENSE_ZERO,
        "mapping": {"target_schema": "noesis-housing-record-v1", "version": "1.0.0"},
        "extractor_versions": [
            "wfs-2.0.0-geojson:1.0.0",
            "geospatial-feature-projection:1.0.0",
            "housing-attribute-projection:1.0.0",
        ],
        "budgets": {
            "timeout_ms": 60000,
            "max_results": 5000,
            "max_bytes": 20000000,
            "max_pages": 20,
        },
        "health": {"required": False, "max_staleness_s": 31536000},
        "wfs": {
            "version": "2.0.0",
            "type_names": layer["type_names"],
            "output_format": "application/json",
            "srs_name": SRS,
            "axis_order": "east_north",
            "sort_by": layer["sort_by"],
            "page_size": 50,
        },
        "geospatial": {
            "namespace": "global",
            "title_property": title,
            "precision": {
                "status": "unknown",
                "basis": "the WFS response declares no positional accuracy",
            },
            "geometry_types": ["Polygon", "MultiPolygon"],
            "metadata_url": metadata_url,
            "attribution": f"Geoportal Berlin / {publisher} (dl-de/zero-2-0; verify attribution terms)",
            "live_verification": "blocked",
        },
        "housing": housing,
    }


BORIS_DECLARATION = {
    "layer": "land-value",
    "namespace": "global",
    "attributes": {
        "zone_id": "wnum",
        "land_value": "brw",
        "valuation_date": "stag",
        "use": "nuta",
        "zone_name": "brzname",
    },
    "qualifiers": {
        "floor_area_ratio": "gfz",
        "development_state": "entw",
        "contribution_state": "beit",
        "municipality": "gena",
    },
    "unit": {"published": "EUR/m²", "currency": "EUR"},
    "number_format": "plain",
}
BPLAN_DECLARATION = {
    "layer": "development-plan",
    "namespace": "global",
    "attributes": {
        "plan_id": "planname",
        "stage": "status",
        "district": "bezirk",
        "plan_label": "bereich",
    },
    "stages": {
        "Aufstellungsbeschluss": "aufstellungsbeschluss",
        "frühzeitige Beteiligung": "fruehzeitige_beteiligung",
        "öffentliche Auslegung": "oeffentliche_auslegung",
        "festgesetzt": "festgesetzt",
        "aufgehoben": "aufgehoben",
    },
    "stage_dates": {"aufstellungsbeschluss": "afs_beschl", "festgesetzt": "festsg_am"},
    "reference_attributes": {
        "afs_abl": {
            "scheme": "berlin-amtsblatt",
            "relation": "stage_decided_by",
            "stage": "aufstellungsbeschluss",
        },
        "gvbl": {
            "scheme": "berlin-gvbl",
            "relation": "published_in",
            "stage": "festgesetzt",
        },
    },
}
WOHNLAGEN_DECLARATION = {
    "layer": "residential-area",
    "namespace": "global",
    "attributes": {"category": "wol"},
    "edition": {
        "edition_id": "berliner-mietspiegel-2099",
        "edition": "Berliner Mietspiegel 2099",
        "valid_from": "2099-05-15",
    },
}

# ---------------------------------------------------------------------- tabular publications

MIETSPIEGEL_ENDPOINT = "https://www.stadtentwicklung.berlin.de"
STATBB_ENDPOINT = "https://www.statistik-berlin-brandenburg.de"
GENESIS_ENDPOINT = "https://www-genesis.destatis.de/genesisWS/rest/2020"
MIETSPIEGEL_COLUMNS = {
    "cell_key": "Feld",
    "dimensions": {
        "Wohnlage": "wohnlage",
        "Baualter": "baualter",
        "Wohnflaeche": "wohnflaeche",
    },
    "lower": "Unterwert",
    "middle": "Mittelwert",
    "upper": "Oberwert",
}


def mietspiegel_document(edition: str = "2099") -> dict[str, Any]:
    base = f"{MIETSPIEGEL_ENDPOINT}/wohnen/mietspiegel/{edition}"
    return {
        "label": f"Berliner Mietspiegel {edition} - Mietspiegeltabelle (fiktiv)",
        "url": f"{base}/mietspiegeltabelle.csv",
        "edition_id": f"berliner-mietspiegel-{edition}",
        "edition": f"Berliner Mietspiegel {edition}",
        "published_on": f"{edition}-05-15",
        "qualifying_date": f"{int(edition) - 1}-09-01",
        "qualifying_date_semantics": "Stichtag der Datenerhebung (rents agreed or changed in the four years before)",
        "valid_from": f"{edition}-05-15",
        "publication_url": f"{base}/mietspiegel{edition}.pdf",
        "page": "S. 14, Mietspiegeltabelle",
        "extraction": "operator-extracted table from the PDF edition (authored fixture)",
        "unit": "EUR/m² monthly",
        "number_format": "de",
        "columns": MIETSPIEGEL_COLUMNS,
        "references": [
            {
                "scheme": "berlin-amtsblatt",
                "identifier": f"ABl. {edition} S. 1234",
                "relation": "published_in",
                "locator": "Bekanntmachung",
            },
            {
                "scheme": "berlin-gvbl",
                "identifier": "GVBl. 2098 S. 42",
                "relation": "legal_basis",
                "locator": "§ 1",
            },
            {
                "scheme": "de-bgbl",
                "identifier": "BGBl. I 2097 S. 99",
                "relation": "legal_basis",
                "locator": "§ 558d BGB",
            },
        ],
    }


def mietspiegel_csv(edition: str = "2099") -> str:
    shift = 0 if edition == "2099" else 1
    rows = [
        ("A1", "einfach", "bis 1918", "40 bis unter 60 m²", "6,10", "7,45", "9,80"),
        ("B1", "mittel", "bis 1918", "40 bis unter 60 m²", "6,90", "8,20", "10,50"),
        ("C1", "gut", "bis 1918", "40 bis unter 60 m²", "7,60", "9,10", "11,90"),
        ("B2", "mittel", "1919 bis 1949", "40 bis unter 60 m²", "6,40", "7,80", "9,70"),
        ("B3", "mittel", "1950 bis 1964", "90 m² und mehr", "-", "-", "-"),
    ]
    lines = ["Feld;Wohnlage;Baualter;Wohnflaeche;Unterwert;Mittelwert;Oberwert"]
    for key, *dims, low, mid, high in rows:
        values = [
            v
            if v == "-"
            else f"{float(v.replace(',', '.')) + shift * 0.5:.2f}".replace(".", ",")
            for v in (low, mid, high)
        ]
        lines.append(";".join([key, *dims, *values]))
    return "\n".join(lines) + "\n"


STATBB_COLUMNS = {
    "geography": "Bezirk",
    "geography_label": "Bezirksname",
    "period": "Jahr",
    "values": {"Wohnungen": None, "Wohnflaeche": None},
}


def statbb_document(statistic: str) -> dict[str, Any]:
    measures = (
        {"Wohnungen": "dwellings_permitted", "Wohnflaeche": "floor_area_permitted"}
        if statistic == "permits"
        else {"Wohnungen": "dwellings_completed", "Wohnflaeche": "floor_area_completed"}
    )
    units = {
        measure: ("dwellings" if measure.startswith("dwellings") else "1000 m²")
        for measure in measures.values()
    }
    name = "baugenehmigungen" if statistic == "permits" else "baufertigstellungen"
    return {
        "label": f"Statistik Berlin-Brandenburg {name} 2098 nach Bezirken (fiktiv)",
        "url": f"{STATBB_ENDPOINT}/downloads/bautaetigkeit/{name}-2098-bezirke.csv",
        "statistic": statistic,
        "number_format": "de",
        "columns": {**STATBB_COLUMNS, "values": measures},
        "units": units,
        "total_codes": {"00": {"scheme": "ags", "code": "11"}},
    }


STATBB_PUBLISHED = {"permits": "2099-03-20", "completions": "2099-05-10"}


def last_modified(day: str) -> str:
    """An HTTP Last-Modified value for a publication day (the file states no date of its own)."""
    from datetime import datetime, timezone
    from email.utils import format_datetime

    return format_datetime(
        datetime.fromisoformat(day + "T09:00:00").replace(tzinfo=timezone.utc),
        usegmt=True,
    )


def statbb_csv(statistic: str, *, revised: bool = False) -> str:
    rows = (
        [
            ("00", "Berlin", "2098", "1520", "98,4"),
            ("01", "Mitte", "2098", "310", "20,1"),
            ("02", "Friedrichshain-Kreuzberg", "2098", "205", "13,0"),
        ]
        if statistic == "permits"
        else [
            ("00", "Berlin", "2098", "1210", "80,2"),
            ("01", "Mitte", "2098", "260", "16,9"),
            ("02", "Friedrichshain-Kreuzberg", "2098", "150", "9,6"),
        ]
    )
    if revised:
        rows = [
            (r[0], r[1], r[2], str(int(r[3]) + 12), r[4]) if r[0] == "01" else r
            for r in rows
        ]
    lines = [
        "Bezirk;Bezirksname;Jahr;Wohnungen;Wohnflaeche",
        *(";".join(r) for r in rows),
    ]
    return "\n".join(lines) + "\n"


GENESIS_TABLES = {
    "31111-0004": {
        "label": "Baugenehmigungen im Hochbau: Bundesländer, Jahre (fiktiv)",
        "value": "BAUGEN__Genehmigte Wohnungen__Anzahl",
        "measure": "dwellings_permitted",
        "updated": "10.04.2099 08:00:00h",
        "rows": [
            ("11", "Berlin", "2097", "1390"),
            ("11", "Berlin", "2098", "1480"),
            ("09", "Bayern", "2097", "6100"),
            ("09", "Bayern", "2098", "."),
        ],
        "statistic": "31111",
        "statistic_label": "Statistik der Baugenehmigungen",
    },
    "31231-0003": {
        "label": "Baufertigstellungen im Hochbau: Bundesländer, Jahre (fiktiv)",
        "value": "BAUFER__Fertiggestellte Wohnungen__Anzahl",
        "measure": "dwellings_completed",
        "updated": "20.05.2099 08:00:00h",
        "rows": [
            ("11", "Berlin", "2097", "1100"),
            ("11", "Berlin", "2098", "1210"),
            ("09", "Bayern", "2097", "5200"),
            ("09", "Bayern", "2098", "5350"),
        ],
        "statistic": "31231",
        "statistic_label": "Statistik der Baufertigstellungen",
    },
}


def genesis_document(table: str) -> dict[str, Any]:
    spec = GENESIS_TABLES[table]
    return {
        "label": f"{table} {spec['label']}",
        "table": table,
        "geography_attribute": "DLAND",
        "frequency": "annual",
        "measures": {spec["value"].split("__")[0]: spec["measure"]},
    }


def genesis_metadata(table: str, *, updated: str | None = None) -> str:
    spec = GENESIS_TABLES[table]
    return (
        json.dumps(
            {
                "Ident": {"Service": "metadata", "Method": "table"},
                "Status": {"Code": 0, "Content": "erfolgreich", "Type": "Information"},
                "Parameter": {"name": table, "area": "all", "language": "de"},
                "Object": {
                    "Code": table,
                    "Content": spec["label"] + " (authored fixture: fictional values)",
                    "Time": {"From": "2097", "To": "2098"},
                    "Updated": updated or spec["updated"],
                },
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n"
    )


def genesis_csv(
    table: str, *, overrides: dict[tuple[str, str], str] | None = None
) -> str:
    spec = GENESIS_TABLES[table]
    header = (
        "Statistik_Code;Statistik_Label;Zeit_Code;Zeit_Label;Zeit;1_Merkmal_Code;1_Merkmal_Label;"
        "1_Auspraegung_Code;1_Auspraegung_Label;" + spec["value"]
    )
    lines = [header]
    for code, label, year, value in spec["rows"]:
        value = (overrides or {}).get((code, year), value)
        lines.append(
            ";".join(
                [
                    spec["statistic"],
                    spec["statistic_label"],
                    "JAHR",
                    "Jahr",
                    year,
                    "DLAND",
                    "Bundesländer",
                    code,
                    label,
                    value,
                ]
            )
        )
    return "\n".join(lines) + "\n"


def tabular_fixture(
    pages: list[dict[str, Any]], scenarios: list[str], source_url: str, license_id: str
) -> dict[str, Any]:
    return {
        "captured": None,
        "authored": AUTHORED,
        "source_url": source_url,
        "retrieved": AUTHORED,
        "license": license_id,
        "note": NOTE,
        "scenarios": scenarios,
        "native_pages": pages,
    }


def page(
    request: str, body: str, content_type: str, *, modified: str | None = None
) -> dict[str, Any]:
    headers = {"Content-Type": content_type}
    if modified:
        headers["Last-Modified"] = last_modified(modified)
    return {"request": request, "status": 200, "headers": headers, "body": body}


def tabular_sources() -> list[dict[str, Any]]:
    common = {
        "mapping": {"target_schema": "noesis-housing-record-v1", "version": "1.0.0"},
        "extractor_versions": ["housing-sources:1.0.0"],
        "operations": ["publications"],
        "schedule": {"kind": "interval", "interval_s": 2592000},
        "health": {"required": False, "max_staleness_s": 63072000},
    }
    return [
        {
            "source_id": "berlin-mietspiegel",
            "connector": "housing",
            "endpoint": MIETSPIEGEL_ENDPOINT,
            "publisher": "Senatsverwaltung für Stadtentwicklung, Bauen und Wohnen Berlin (Berliner Mietspiegel)",
            "scope": "The Mietspiegeltabelle of each declared Berliner Mietspiegel edition (cells by Wohnlage, "
            "Baualter and Wohnfläche with lower, middle and upper values), with the edition's qualifying date, "
            "publication URL and page (operator must verify the table file per edition)",
            "update_cadence": "every two years (qualified Mietspiegel)",
            "temporal_semantics": "each edition with its qualifying date and the date it applies from; an edition "
            "never overwrites another",
            "license": {
                "id": "berlin-official-publication",
                "terms_url": "https://www.berlin.de/impressum/",
                "redistribution": "official publication, reuse with attribution (operator must confirm)",
            },
            "auth": {"kind": "none"},
            "budgets": {
                "timeout_ms": 60000,
                "max_results": 500,
                "max_bytes": 2000000,
                "max_pages": 4,
            },
            "housing": {
                "namespace": "global",
                "provider": "berlin-mietspiegel",
                "format": "rent-index-table-csv",
                "documents": [mietspiegel_document("2099")],
            },
            **common,
        },
        {
            "source_id": "statistik-bb-bautaetigkeit",
            "connector": "housing",
            "endpoint": STATBB_ENDPOINT,
            "publisher": "Amt für Statistik Berlin-Brandenburg",
            "scope": "Building permits (Baugenehmigungen) and completions (Baufertigstellungen) of dwellings per "
            "Berlin Bezirk and year in a declared column layout (operator must verify files and columns)",
            "update_cadence": "annual (monthly tables exist)",
            "temporal_semantics": "reporting year per row and the publication date as the vintage; revised figures "
            "are new vintages",
            "license": {
                "id": "cc-by-3.0-de",
                "terms_url": "https://www.statistik-berlin-brandenburg.de/impressum",
                "redistribution": "CC BY 3.0 DE with attribution (operator must confirm)",
            },
            "auth": {"kind": "none"},
            "budgets": {
                "timeout_ms": 60000,
                "max_results": 1000,
                "max_bytes": 2000000,
                "max_pages": 4,
            },
            "housing": {
                "namespace": "global",
                "provider": "statistik-bb",
                "format": "statbb-building-csv",
                "documents": [
                    statbb_document("permits"),
                    statbb_document("completions"),
                ],
            },
            **common,
        },
        {
            "source_id": "destatis-genesis-bautaetigkeit",
            "connector": "housing",
            "endpoint": GENESIS_ENDPOINT,
            "publisher": "Statistisches Bundesamt (Destatis), GENESIS-Online",
            "scope": "GENESIS tables 31111 (building permits) and 31231 (building completions) by Land and year, "
            "with the table's Updated stamp (credentialed; operator must verify table codes and value codes)",
            "update_cadence": "annual",
            "temporal_semantics": "the table's Updated stamp as the vintage and Zeit as the reference year",
            "license": {
                "id": "dl-de-by-2.0",
                "terms_url": "https://www-genesis.destatis.de/genesis/online?Menu=Impressum",
                "redistribution": "Datenlizenz Deutschland - Namensnennung 2.0 (operator must confirm)",
            },
            "auth": {
                "kind": "required-secret",
                "secret_ref": "NOESIS_DESTATIS_GENESIS_TOKEN",
            },
            "budgets": {
                "timeout_ms": 60000,
                "max_results": 100,
                "max_bytes": 10000000,
                "max_pages": 2,
            },
            "housing": {
                "namespace": "global",
                "provider": "destatis",
                "format": "destatis-genesis-ffcsv",
                "documents": [
                    genesis_document("31111-0004"),
                    genesis_document("31231-0003"),
                ],
            },
            **common,
        },
    ]


def tabular_fixtures() -> dict[str, dict[str, Any]]:
    from src.ingestion.housing_sources import fixture_request

    mietspiegel = mietspiegel_document("2099")
    statbb = [statbb_document("permits"), statbb_document("completions")]
    genesis_pages = []
    for table in GENESIS_TABLES:
        document = genesis_document(table)
        genesis_pages += [
            page(
                fixture_request(document, GENESIS_ENDPOINT, part="metadata"),
                genesis_metadata(table),
                "application/json",
            ),
            page(
                fixture_request(document, GENESIS_ENDPOINT),
                genesis_csv(table),
                "text/csv",
            ),
        ]
    return {
        "berlin-mietspiegel": tabular_fixture(
            [page(fixture_request(mietspiegel), mietspiegel_csv("2099"), "text/csv")],
            [
                "operator-extracted-table",
                "edition-with-qualifying-date",
                "unpublished-cell",
            ],
            mietspiegel["publication_url"],
            "berlin-official-publication",
        ),
        "statistik-bb-bautaetigkeit": tabular_fixture(
            [
                page(
                    fixture_request(d),
                    statbb_csv(d["statistic"]),
                    "text/csv",
                    modified=STATBB_PUBLISHED[d["statistic"]],
                )
                for d in statbb
            ],
            [
                "permits",
                "completions",
                "bezirk-codes",
                "land-total-code",
                "last-modified-vintage",
            ],
            statbb[0]["url"],
            "cc-by-3.0-de",
        ),
        "destatis-genesis-bautaetigkeit": tabular_fixture(
            genesis_pages,
            ["genesis-31111", "genesis-31231", "table-updated-stamp", "genesis-sign"],
            "https://www-genesis.destatis.de/",
            "dl-de-by-2.0",
        ),
    }


# ---------------------------------------------------------------------- manifest


def wfs_sources_and_fixtures() -> tuple[
    list[dict[str, Any]], dict[str, dict[str, Any]]
]:
    sources = [
        wfs_source(
            BORIS,
            publisher="Gutachterausschuss für Grundstückswerte in Berlin (BORIS Bodenrichtwerte)",
            scope="Bodenrichtwertzonen of one Stichtag (zone number, value, valuation date, type of use and "
            "qualifiers); complete collection (operator must verify the layer per Stichtag)",
            cadence="annual (Stichtag 1 January)",
            title="brzname",
            housing=BORIS_DECLARATION,
            metadata_url="https://www.boris-berlin.de/",
        ),
        wfs_source(
            BPLAN,
            publisher="Senatsverwaltung für Stadtentwicklung, Bauen und Wohnen Berlin "
            "(Bebauungspläne)",
            scope="Bebauungsplan areas with plan number, district, published procedural stage and stage "
            "dates; complete collection (operator must verify stage attribute and vocabulary)",
            cadence="irregular; continuous plan procedures",
            title="planname",
            housing=BPLAN_DECLARATION,
            metadata_url="https://fbinter.stadt-berlin.de/fb/index.jsp",
        ),
        wfs_source(
            WOHNLAGEN,
            publisher="Senatsverwaltung für Stadtentwicklung, Bauen und Wohnen Berlin "
            "(Wohnlagenkarte zum Mietspiegel)",
            scope="Wohnlage categories (einfach, mittel, gut) of the Mietspiegel 2099 edition per block; "
            "complete collection (operator must verify blocks versus address points)",
            cadence="per Mietspiegel edition",
            title="blk",
            housing=WOHNLAGEN_DECLARATION,
            metadata_url="https://www.stadtentwicklung.berlin.de/wohnen/mietspiegel/",
        ),
    ]
    fixtures = {
        BORIS["source_id"]: wfs_fixture(
            BORIS,
            boris_features("2099-01-01"),
            [
                "projected-crs-epsg-25833",
                "complete-snapshot",
                "land-value-zones",
                "valuation-date",
                "shared-zone-edge",
            ],
            "2099-03-01T08:00:00Z",
        ),
        BPLAN["source_id"]: wfs_fixture(
            BPLAN,
            bplan_features(1),
            [
                "projected-crs-epsg-25833",
                "complete-snapshot",
                "plan-stage",
                "stage-date",
                "explicit-references",
            ],
            "2099-03-01T08:00:00Z",
        ),
        WOHNLAGEN["source_id"]: wfs_fixture(
            WOHNLAGEN,
            wohnlagen_features(),
            ["projected-crs-epsg-25833", "complete-snapshot", "edition-category"],
            "2099-06-01T08:00:00Z",
        ),
    }
    return sources, fixtures


def fixture_path(source_id: str) -> str:
    return f"tests/fixtures/source_packs/geospatial-housing-{source_id}.json"


def build() -> dict[str, Any]:
    from src.ingestion.source_packs import (
        _digest,
        replay_native_fixture,
        validate_source_pack,
    )

    base = json.loads(BASE.read_text())
    manifest = copy.deepcopy(base)
    manifest["version"] = VERSION
    manifest["description"] += (
        " Version 1.3.0 (Geospatial housing) adds Berlin housing, land-value and planning sources: BORIS "
        "Bodenrichtwert zones, Bebauungsplan areas and Wohnlagen through the same WFS 2.0.0 path, the Berliner "
        "Mietspiegel table, Statistik Berlin-Brandenburg building permits and completions and Destatis GENESIS "
        "31111/31231 through the housing connector; existing sources are unchanged."
    )
    wfs_sources, fixtures = wfs_sources_and_fixtures()
    fixtures |= tabular_fixtures()
    added = wfs_sources + tabular_sources()
    for source in added:
        text = (
            json.dumps(
                fixtures[source["source_id"]],
                ensure_ascii=False,
                indent=1,
                sort_keys=True,
            )
            + "\n"
        )
        path = ROOT / fixture_path(source["source_id"])
        path.write_text(text)
        source["fixture"] = {
            "path": fixture_path(source["source_id"]),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "expected_output_hash": "0" * 64,
        }
    manifest["sources"] = base["sources"] + added
    validated = validate_source_pack(manifest)
    for source in added:
        compiled = next(
            s for s in validated["sources"] if s["source_id"] == source["source_id"]
        )
        output = replay_native_fixture(compiled, fixtures[source["source_id"]])
        source["fixture"]["expected_output_hash"] = _digest(output)
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    built = build()
    print(f"wrote {MANIFEST.relative_to(ROOT)} with {len(built['sources'])} sources")
