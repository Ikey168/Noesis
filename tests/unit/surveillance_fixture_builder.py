"""Author the public-health surveillance fixtures and pin them into the ``clinical-evidence`` source pack.

Every fixture is *authored* in the provider's documented native shape - an RKI
open-data CSV release on GitHub, WHO GHO OData answers (``/Indicator`` and the
indicator's values, two pages joined by ``@odata.nextLink``), a Eurostat
SDMX-CSV 1.0 answer for ``hlth_cd_aro`` and a GENESIS-Online metadata answer
plus flat-file CSV. Repository names, indicator codes, table codes and every
value are fictional (years 2097-2099); geography codes are real code-list
codes. The ECDC Surveillance Atlas has no machine interface, so its export is
an operator-supplied CSV under ``tests/fixtures/clinical/surveillance/``.
Nothing here is a live capture.

``python -m tests.unit.surveillance_fixture_builder`` rewrites
``tests/fixtures/source_packs/surveillance-*.json`` and the surveillance
sources of ``config/source_packs/clinical-evidence.json`` (0.1.1: the 0.1.0
sources verbatim plus these); ``test_fixtures_and_manifest_are_pinned_and_in_sync``
fails when they drift. Later releases (a new RKI tag, a Eurostat update) are
built by the functions below for tests only and are not pinned.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tests/fixtures/source_packs"
RAW = ROOT / "tests/fixtures/clinical/surveillance"
MANIFEST = ROOT / "config/source_packs/clinical-evidence.json"
VERSION = "0.1.1"
NAMESPACE = "clinical"
NOTE = (
    "Authored offline fixture in the provider's documented response shape; repository names, indicator codes, "
    "table codes and values are fictional. Not live evidence."
)
DESCRIPTION = (
    "Clinical Evidence: registered trials with version history, posted results, EU registry records and regulatory "
    "records (openFDA, EMA) for pinned selections. Version 0.1.1 adds public-health surveillance series (RKI open "
    "data, WHO GHO, Eurostat health through the SDMX connector, Destatis health through the GENESIS connector) with "
    "reporting and reference dates kept apart; existing sources are unchanged. No medical advice."
)
MAPPING = {"target_schema": "noesis-surveillance-record-v1", "version": "1.0.0"}
EXTRACTOR = ["surveillance-sources:1.0.0"]

# ---------------------------------------------------------------------- RKI open data (GitHub release)

RKI_REPOSITORY = "robert-koch-institut/Fiktive_Tuberkulose-Meldedaten"
RKI_PATH = "Daten/Tuberkulose_Meldedaten.csv"
RKI_HEADER = (
    "IdLandkreis,Altersgruppe,Meldedatum,Refdatum,IstErkrankungsbeginn,AnzahlFall"
)
RKI_ROWS = {
    "2099-01-20": [
        "09162,A15-A34,2098-12-28,2098-12-20,1,3",
        "09162,A15-A34,2099-01-05,2099-01-02,1,2",
        "09162,A15-A34,2099-01-12,2099-01-12,0,1",
        "09162,A35-A59,2098-12-29,2098-12-22,1,1",
        "09184,A15-A34,2099-01-06,2098-12-30,1,1",
        "09184,A15-A34,2099-01-14,2099-01-08,1,2",
    ],
    # The next release: late reports for earlier reference dates, one value revised by the source, new weeks.
    "2099-02-03": [
        "09162,A15-A34,2098-12-28,2098-12-20,1,3",
        "09162,A15-A34,2099-01-05,2099-01-02,1,2",
        "09162,A15-A34,2099-01-12,2099-01-12,0,1",
        "09162,A15-A34,2099-01-25,2099-01-02,1,1",
        "09162,A35-A59,2098-12-29,2098-12-22,1,1",
        "09184,A15-A34,2099-01-06,2098-12-30,1,1",
        "09184,A15-A34,2099-01-14,2099-01-08,1,3",
        "09184,A15-A34,2099-01-28,2099-01-21,1,1",
    ],
}
RKI_CASE_DEFINITIONS = [
    {
        "key": "tuberkulose",
        "version": "2019",
        "valid_from": "2019-01-01",
        "valid_to": "2098-12-31",
        "locator": "https://www.rki.de/DE/Content/Infekt/IfSG/Falldefinition/falldefinition_node.html",
        "icd_scope": ["A15-A19"],
    },
    {
        "key": "tuberkulose",
        "version": "2099",
        "valid_from": "2099-01-01",
        "locator": "https://www.rki.de/DE/Content/Infekt/IfSG/Falldefinition/falldefinition_node.html",
        "icd_scope": ["A15-A19", "A31.0"],
    },
]
RKI_DELAY_NOTE = {
    "text": "Meldungen der letzten drei Wochen sind wegen des Meldeverzugs noch unvollständig (fiktive Angabe).",
    "locator": "https://github.com/robert-koch-institut/Fiktive_Tuberkulose-Meldedaten#meldeverzug",
    "incomplete_recent": {"interval": "week", "count": 3},
}


def rki_document(tag: str = "2099-01-20") -> dict[str, Any]:
    return {
        "label": f"RKI Tuberkulose-Meldedaten {tag} (fictional repository)",
        "repository": RKI_REPOSITORY,
        "tag": tag,
        "path": RKI_PATH,
        "published_on": tag,
        "condition": {
            "scheme": "rki-meldekategorie",
            "code": "Tuberkulose",
            "label": "Tuberkulose",
        },
        "indicator": {
            "code": "AnzahlFall",
            "label": "Gemeldete Fälle",
            "definition_locator": f"https://github.com/{RKI_REPOSITORY}#readme",
        },
        "columns": {
            "reporting_date": "Meldedatum",
            "reference_date": "Refdatum",
            "geography": "IdLandkreis",
            "figure": "AnzahlFall",
            "reference_known": "IstErkrankungsbeginn",
            "dimensions": {"Altersgruppe": "age_group"},
        },
        "geography": {
            "system": "ags",
            "level": "kreis",
            "code_list_version": "2099-01-01",
        },
        "unit": "cases",
        "interval": "day",
        "kind": "observation",
        "case_definitions": copy.deepcopy(RKI_CASE_DEFINITIONS),
        "delay_note": copy.deepcopy(RKI_DELAY_NOTE),
        "citations": [{"kind": "doi", "identifier": "10.5281/zenodo.9900001"}],
    }


def rki_csv(tag: str) -> str:
    return "\n".join([RKI_HEADER, *RKI_ROWS[tag]]) + "\n"


def rki_request(tag: str) -> str:
    return f"/{RKI_REPOSITORY}/{tag}/{RKI_PATH}"


# ---------------------------------------------------------------------- WHO GHO OData

GHO_INDICATORS = {
    "NOE_TB_NOTIF_RATE": {
        "name": "Tuberculosis notification rate, all forms (per 100 000 population) (fictional indicator)",
        "kind": "observation",
        "rows": [
            {"TimeDim": 2097, "NumericValue": 5.3, "Value": "5.3", "Date": None},
            {
                "TimeDim": 2098,
                "NumericValue": 5.6,
                "Value": "5.6",
                "Date": "2099-02-10T09:00:00+01:00",
            },
        ],
    },
    "NOE_TB_INC_EST": {
        "name": "Estimated incidence of tuberculosis (per 100 000 population) (fictional indicator)",
        "kind": "observation",
        "rows": [
            {
                "TimeDim": 2097,
                "NumericValue": 6.1,
                "Value": "6.1 [5.2-7.0]",
                "Low": 5.2,
                "High": 7.0,
                "Date": "2098-11-02T10:00:00+01:00",
            },
            {
                "TimeDim": 2098,
                "NumericValue": 6.4,
                "Value": "6.4 [5.4-7.5]",
                "Low": 5.4,
                "High": 7.5,
                "Date": "2099-02-10T09:00:00+01:00",
            },
        ],
    },
}
GHO_ENDPOINT = "https://ghoapi.azureedge.net/api"


def gho_document(code: str) -> dict[str, Any]:
    return {
        "label": f"GHO {code} Germany (fictional indicator)",
        "indicator": code,
        "filter": "SpatialDim eq 'DEU'",
        "condition_label": "Tuberculosis",
        "unit": "per 100 000 population",
        "kind": GHO_INDICATORS[code]["kind"],
        "denominator": "total population (as stated by the indicator)",
    }


def gho_row(code: str, row: dict[str, Any], number: int) -> dict[str, Any]:
    return {
        "Id": 99000000 + number,
        "IndicatorCode": code,
        "SpatialDimType": "COUNTRY",
        "SpatialDim": "DEU",
        "ParentLocationCode": "EUR",
        "TimeDimType": "YEAR",
        "TimeDim": row["TimeDim"],
        "Dim1Type": "SEX",
        "Dim1": "SEX_BTSX",
        "Dim2Type": None,
        "Dim2": None,
        "Dim3Type": None,
        "Dim3": None,
        "DataSourceDimType": None,
        "DataSourceDim": None,
        "Value": row["Value"],
        "NumericValue": row["NumericValue"],
        "Low": row.get("Low"),
        "High": row.get("High"),
        "Comments": None,
        "Date": row["Date"],
        "TimeDimensionValue": str(row["TimeDim"]),
        "TimeDimensionBegin": f"{row['TimeDim']}-01-01T00:00:00+01:00",
        "TimeDimensionEnd": f"{row['TimeDim']}-12-31T00:00:00+01:00",
    }


def gho_pages(code: str) -> list[dict[str, Any]]:
    from src.ingestion.surveillance_sources import fixture_request, gho_urls

    urls = gho_urls(gho_document(code), GHO_ENDPOINT)
    spec = GHO_INDICATORS[code]
    rows = [gho_row(code, row, n) for n, row in enumerate(spec["rows"])]
    metadata = {
        "@odata.context": f"{GHO_ENDPOINT}/$metadata#Indicator",
        "value": [
            {"IndicatorCode": code, "IndicatorName": spec["name"], "Language": "EN"}
        ],
    }
    pages = [
        {
            "request": fixture_request(urls["metadata"]),
            "status": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(metadata, indent=1),
        }
    ]
    if code == "NOE_TB_NOTIF_RATE":
        # Two pages joined by @odata.nextLink; the last page carries the Last-Modified header.
        next_url = urls["data"] + "&$skip=1"
        first = {
            "@odata.context": f"{GHO_ENDPOINT}/$metadata#{code}",
            "value": rows[:1],
            "@odata.nextLink": next_url,
        }
        second = {
            "@odata.context": f"{GHO_ENDPOINT}/$metadata#{code}",
            "value": rows[1:],
        }
        pages += [
            {
                "request": fixture_request(urls["data"]),
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(first, indent=1),
            },
            {
                "request": fixture_request(next_url),
                "status": 200,
                "headers": {
                    "Content-Type": "application/json",
                    "Last-Modified": "Wed, 11 Feb 2099 08:00:00 GMT",
                },
                "body": json.dumps(second, indent=1),
            },
        ]
    else:
        body = {"@odata.context": f"{GHO_ENDPOINT}/$metadata#{code}", "value": rows}
        pages.append(
            {
                "request": fixture_request(urls["data"]),
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(body, indent=1),
            }
        )
    return pages


# ---------------------------------------------------------------------- Eurostat through SDMX-CSV

EUROSTAT_ENDPOINT = "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data"
EUROSTAT_HEADER = (
    "DATAFLOW,LAST UPDATE,freq,unit,sex,age,icd10,geo,TIME_PERIOD,OBS_VALUE,OBS_FLAG"
)
EUROSTAT_RELEASES = {
    "15/03/99 11:00:00": [
        ("DE", "2097", "301", ""),
        ("DE", "2098", "288", "bp"),
        ("DE2", "2097", "60", ""),
        ("DE2", "2098", ":", "c"),
    ],
    # A later update: the provisional 2098 value is final and revised; nothing else changes.
    "20/09/99 11:00:00": [
        ("DE", "2097", "301", ""),
        ("DE", "2098", "290", "b"),
        ("DE2", "2097", "60", ""),
        ("DE2", "2098", ":", "c"),
    ],
}


def eurostat_document() -> dict[str, Any]:
    return {
        "label": "Eurostat hlth_cd_aro tuberculosis deaths, Germany and Bayern (fictional values)",
        "dataset": "hlth_cd_aro",
        "key": "A.NR.T.TOTAL.A15-A19_B90.DE+DE2",
        "params": {"startPeriod": "2097"},
        "condition_dimension": "icd10",
        "condition_labels": {"A15-A19_B90": "Tuberculosis"},
        "units": {"NR": "deaths"},
        "indicator": {
            "code": "hlth_cd_aro",
            "label": "Causes of death by NUTS 2 regions of residence, "
            "absolute numbers (fictional values)",
        },
        "geography": {"code_list_version": "NUTS 2021"},
        "kind": "observation",
    }


def eurostat_csv(stamp: str = "15/03/99 11:00:00") -> str:
    lines = [EUROSTAT_HEADER]
    for geo, year, value, flag in EUROSTAT_RELEASES[stamp]:
        lines.append(
            f"ESTAT:HLTH_CD_ARO(1.0),{stamp},A,NR,T,TOTAL,A15-A19_B90,{geo},{year},{value},{flag}"
        )
    return "\n".join(lines) + "\n"


def eurostat_request() -> str:
    from src.ingestion.surveillance_sources import document_url, fixture_request

    return fixture_request(
        document_url(eurostat_document(), EUROSTAT_ENDPOINT, "eurostat-sdmx-csv")
    )


# ---------------------------------------------------------------------- Destatis through GENESIS

GENESIS_ENDPOINT = "https://www-genesis.destatis.de/genesisWS/rest/2020"
GENESIS_TABLE = "23211-0004"
GENESIS_ROWS = [
    ("09", "Bayern", "2097", "40"),
    ("09", "Bayern", "2098", "45"),
    ("11", "Berlin", "2097", "."),
    ("11", "Berlin", "2098", "12"),
]


def genesis_document() -> dict[str, Any]:
    return {
        "label": f"{GENESIS_TABLE} Gestorbene: Bundesländer, Jahre, Todesursachen (fictional values)",
        "table": GENESIS_TABLE,
        "geography_attribute": "DLAND",
        "condition_attribute": "TODUR4",
        "measures": {"BEV003": "Gestorbene"},
        "units": {"Anzahl": "deaths"},
        "condition_labels": {"A15-A19": "Tuberkulose"},
        "geography": {
            "system": "ags",
            "level": "land",
            "code_list_version": "2099-01-01",
        },
        "kind": "observation",
    }


def genesis_metadata(updated: str = "12.08.2099 08:00:00h") -> str:
    return (
        json.dumps(
            {
                "Ident": {"Service": "metadata", "Method": "table"},
                "Status": {"Code": 0, "Content": "erfolgreich", "Type": "Information"},
                "Parameter": {"name": GENESIS_TABLE, "area": "all", "language": "de"},
                "Object": {
                    "Code": GENESIS_TABLE,
                    "Content": "Gestorbene: Bundesländer, Jahre, Todesursachen (authored "
                    "fixture: fictional values)",
                    "Time": {"From": "2097", "To": "2098"},
                    "Updated": updated,
                },
            },
            ensure_ascii=False,
            indent=1,
        )
        + "\n"
    )


def genesis_csv() -> str:
    header = (
        "Statistik_Code;Statistik_Label;Zeit_Code;Zeit_Label;Zeit;1_Merkmal_Code;1_Merkmal_Label;"
        "1_Auspraegung_Code;1_Auspraegung_Label;2_Merkmal_Code;2_Merkmal_Label;2_Auspraegung_Code;"
        "2_Auspraegung_Label;BEV003__Gestorbene__Anzahl"
    )
    lines = [header]
    for code, label, year, value in GENESIS_ROWS:
        lines.append(
            ";".join(
                [
                    "23211",
                    "Todesursachenstatistik",
                    "JAHR",
                    "Jahr",
                    year,
                    "DLAND",
                    "Bundesländer",
                    code,
                    label,
                    "TODUR4",
                    "Todesursachen (ICD-10)",
                    "A15-A19",
                    "Tuberkulose",
                    value,
                ]
            )
        )
    return "\n".join(lines) + "\n"


def genesis_requests() -> dict[str, str]:
    from src.ingestion.surveillance_sources import document_url, fixture_request

    return {
        part: fixture_request(
            document_url(
                genesis_document(),
                GENESIS_ENDPOINT,
                "destatis-genesis-ffcsv",
                part=part,
            )
        )
        for part in ("metadata", "data")
    }


# ---------------------------------------------------------------------- ECDC Atlas export (operator-supplied)

ECDC_EXPORT = RAW / "ecdc_atlas_tuberculosis_export.csv"


def ecdc_export(extraction_date: str = "2099-03-01") -> dict[str, Any]:
    return {
        "format": "ecdc-atlas-csv",
        "document": {
            "label": "ECDC Surveillance Atlas export: tuberculosis and Legionnaires' disease, Germany "
            "(fictional values)",
            "url": "https://atlas.ecdc.europa.eu/public/index.aspx",
            "extraction_date": extraction_date,
            "units": {"N": "cases", "N/100000": "per 100 000 population"},
            "aggregate_codes": {"EU_EEA31": "EU/EEA (31 countries)"},
            "interval": "year",
            "kind": "observation",
        },
        "content": ECDC_EXPORT.read_text(),
    }


# ---------------------------------------------------------------------- sources and fixtures


def _source(
    source_id: str,
    *,
    publisher: str,
    endpoint: str,
    scope: str,
    cadence: str,
    temporal: str,
    provider: str,
    fmt: str,
    documents: list[dict[str, Any]],
    license_: dict[str, str],
    auth: dict[str, str] | None = None,
    interval_s: int = 604800,
) -> dict[str, Any]:
    return {
        "mapping": dict(MAPPING),
        "extractor_versions": list(EXTRACTOR),
        "health": {"required": False, "max_staleness_s": 2592000},
        "source_id": source_id,
        "connector": "surveillance",
        "publisher": publisher,
        "endpoint": endpoint,
        "scope": scope,
        "update_cadence": cadence,
        "temporal_semantics": temporal,
        "operations": ["records"],
        "schedule": {"kind": "interval", "interval_s": interval_s},
        "budgets": {
            "timeout_ms": 30000,
            "max_results": 200,
            "max_bytes": 20000000,
            "max_pages": 10,
        },
        "surveillance": {
            "namespace": NAMESPACE,
            "provider": provider,
            "format": fmt,
            "documents": documents,
        },
        "license": license_,
        "auth": auth or {"kind": "none"},
    }


def sources() -> list[dict[str, Any]]:
    return [
        _source(
            "rki-tuberkulose-meldedaten",
            publisher="Robert Koch-Institut",
            endpoint="https://raw.githubusercontent.com",
            scope="One pinned RKI open-data release (tag) of notified tuberculosis cases by district",
            cadence="each release tag is a new vintage; the pinned tag is updated by the operator",
            temporal="Meldedatum (reporting date) and Refdatum (reference date) per value; release date of the "
            "tag as the release clock; retrieval time recorded separately",
            provider="rki-open-data",
            fmt="rki-github-csv",
            documents=[rki_document()],
            license_={
                "id": "cc-by-4.0",
                "terms_url": "https://creativecommons.org/licenses/by/4.0/",
                "redistribution": "attribution to the Robert Koch-Institut and the release (verify per "
                "repository)",
            },
            interval_s=86400,
        ),
        _source(
            "who-gho-tuberculosis",
            publisher="World Health Organization (Global Health Observatory)",
            endpoint=GHO_ENDPOINT,
            scope="Pinned GHO indicators for Germany (tuberculosis notification rate "
            "and estimated incidence)",
            cadence="irregular per indicator",
            temporal="TimeDim (reference year) and Date (reporting date) per "
            "value; Last-Modified or the latest value date as the "
            "release clock, labelled",
            provider="who-gho",
            fmt="who-gho-odata",
            documents=[
                gho_document("NOE_TB_NOTIF_RATE"),
                gho_document("NOE_TB_INC_EST"),
            ],
            license_={
                "id": "who-data-terms",
                "terms_url": "https://www.who.int/about/policies/publishing/"
                "data-policy/terms-and-conditions",
                "redistribution": "WHO data terms; attribution, non-commercial for CC BY-NC-SA IGO data "
                "(verify)",
            },
        ),
        _source(
            "eurostat-causes-of-death-tuberculosis",
            publisher="Eurostat",
            endpoint=EUROSTAT_ENDPOINT,
            scope="Eurostat hlth_cd_aro tuberculosis deaths for Germany and Bayern through the SDMX connector "
            "(same dissemination API as economic-statistics-and-filings/eurostat-dissemination)",
            cadence="annual",
            temporal="TIME_PERIOD (reference year); LAST UPDATE as the release clock; no "
            "reporting date per value",
            provider="eurostat-health",
            fmt="eurostat-sdmx-csv",
            documents=[eurostat_document()],
            license_={
                "id": "eurostat-reuse",
                "terms_url": "https://ec.europa.eu/eurostat/about-us/policies/"
                "copyright",
                "redistribution": "attribution-required",
            },
        ),
        _source(
            "destatis-todesursachen-tuberkulose",
            publisher="Statistisches Bundesamt (Destatis)",
            endpoint=GENESIS_ENDPOINT,
            scope="GENESIS table 23211-0004 tuberculosis deaths by Land through the GENESIS connector "
            "(credentialed)",
            cadence="annual",
            temporal="Zeit (reference year); the table's Updated stamp as the release clock; "
            "no reporting date per value",
            provider="destatis-health",
            fmt="destatis-genesis-ffcsv",
            documents=[genesis_document()],
            license_={
                "id": "dl-de-by-2.0",
                "terms_url": "https://www.govdata.de/dl-de/by-2-0",
                "redistribution": "attribution-required (verify)",
            },
            auth={
                "kind": "required-secret",
                "secret_ref": "NOESIS_DESTATIS_GENESIS_TOKEN",
            },
        ),
    ]


def fixture(pages: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "captured": None,
        "native_pages": pages,
        "note": NOTE,
        "authored": True,
        "provider": "authored",
        "scenarios": ["authored-fixture", "fictional-values"],
    }


def fixtures() -> dict[str, dict[str, Any]]:
    genesis = genesis_requests()
    return {
        "rki-tuberkulose-meldedaten": fixture(
            [
                {
                    "request": rki_request("2099-01-20"),
                    "status": 200,
                    "headers": {"Content-Type": "text/plain"},
                    "body": rki_csv("2099-01-20"),
                }
            ]
        ),
        "who-gho-tuberculosis": fixture(
            gho_pages("NOE_TB_NOTIF_RATE") + gho_pages("NOE_TB_INC_EST")
        ),
        "eurostat-causes-of-death-tuberculosis": fixture(
            [
                {
                    "request": eurostat_request(),
                    "status": 200,
                    "headers": {"Content-Type": "text/csv"},
                    "body": eurostat_csv(),
                }
            ]
        ),
        "destatis-todesursachen-tuberkulose": fixture(
            [
                {
                    "request": genesis["metadata"],
                    "status": 200,
                    "headers": {"Content-Type": "application/json"},
                    "body": genesis_metadata(),
                },
                {
                    "request": genesis["data"],
                    "status": 200,
                    "headers": {"Content-Type": "text/csv"},
                    "body": genesis_csv(),
                },
            ]
        ),
    }


def fixture_path(source_id: str) -> str:
    return f"tests/fixtures/source_packs/surveillance-{source_id}.json"


def fixture_text(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def build(*, write: bool = True) -> dict[str, Any]:
    from src.ingestion.source_packs import (
        _digest,
        replay_native_fixture,
        validate_source_pack,
    )

    current = json.loads(MANIFEST.read_text())
    manifest = copy.deepcopy(current)
    # Sources added after the surveillance block by later additive releases (0.1.2 medicines, #2214) keep their
    # place, and a later pack version keeps its own version and description.
    first = next((i for i, s in enumerate(manifest["sources"]) if s["connector"] == "surveillance"),
                 len(manifest["sources"]))
    base = [s for s in manifest["sources"][:first] if s["connector"] != "surveillance"]
    later = [s for s in manifest["sources"][first:] if s["connector"] != "surveillance"]
    if tuple(int(p) for p in current["version"].split(".")) <= tuple(int(p) for p in VERSION.split(".")):
        manifest["version"] = VERSION
        manifest["description"] = DESCRIPTION
    added = sources()
    authored = fixtures()
    for source in added:
        text = fixture_text(authored[source["source_id"]])
        if write:
            (ROOT / fixture_path(source["source_id"])).write_text(text)
        source["fixture"] = {
            "path": fixture_path(source["source_id"]),
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "expected_output_hash": "0" * 64,
        }
    manifest["sources"] = base + added + later
    validated = validate_source_pack(manifest)
    for source in added:
        compiled = next(
            s for s in validated["sources"] if s["source_id"] == source["source_id"]
        )
        source["fixture"]["expected_output_hash"] = _digest(
            replay_native_fixture(compiled, authored[source["source_id"]])
        )
    if write:
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    built = build()
    print(
        f"wrote {MANIFEST.relative_to(ROOT)} {built['version']} with {len(built['sources'])} sources"
    )
