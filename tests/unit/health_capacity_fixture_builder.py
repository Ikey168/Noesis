"""Author the health-system capacity fixtures and pin them into the ``clinical-evidence`` source pack (#2215).

Every fixture is *authored* in the provider's documented native shape - WHO GHO
OData answers (``/Indicator`` and the indicator's values), OECD SDMX-CSV
(``format=csvfile``, with a ``DATAFLOW`` column naming agency, dataflow and
version) and Eurostat SDMX-CSV 1.0 (``hlth_rs_bds1``, ``hlth_sha11_hf``). The
indicator codes, dataflows, datasets and dimension codes are the publishers'
real identifiers (see the source audit; *verify* before the live run); **every
value, year (2096-2098), definition wording and note is fictional**. Nothing
here is a live capture.

``python -m tests.unit.health_capacity_fixture_builder`` rewrites
``tests/fixtures/source_packs/health-capacity-*.json`` and the health-capacity
sources of ``config/source_packs/clinical-evidence.json`` (0.1.3: the 0.1.2
sources verbatim plus these); ``test_capacity_fixtures_and_manifest_are_pinned_and_in_sync``
fails when they drift. Later releases (a revised GHO value, a Eurostat update, an
OECD dataflow version, a new definition edition) are built by the functions
below for tests only and are not pinned.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "tests/fixtures/source_packs"
MANIFEST = ROOT / "config/source_packs/clinical-evidence.json"
VERSION = "0.1.3"
NAMESPACE = "clinical"
CONNECTOR = "health-capacity"
NOTE = (
    "Authored offline fixture in the provider's documented response shape; identifiers are the publishers' codes, "
    "every value, year, definition wording and note is fictional. Not live evidence."
)
DESCRIPTION = (
    "Clinical Evidence: registered trials with version history, posted results, EU registry records and regulatory "
    "records (openFDA, EMA) for pinned selections. Version 0.1.1 adds public-health surveillance series (RKI open "
    "data, WHO GHO, Eurostat health through the SDMX connector, Destatis health through the GENESIS connector) with "
    "reporting and reference dates kept apart. Version 0.1.2 adds medicines regulation (EMA EPARs and product "
    "information, Drugs@FDA submission history through the openFDA connector, DailyMed SPL versions, FDA Drug "
    "Safety Communications). Version 0.1.3 adds health-system capacity indicators (hospital beds, health workforce "
    "and expenditure by financing scheme from WHO GHO, OECD Health Statistics through the SDMX connector and "
    "Eurostat health care resources and expenditure) with definitions and vintages; existing sources are "
    "unchanged. No medical advice, no rankings."
)
MAPPING = {"target_schema": "noesis-surveillance-record-v1", "version": "1.0.0"}
EXTRACTOR = ["surveillance-sources:1.0.0"]
SOURCE_IDS = {
    "gho": "who-gho-health-capacity",
    "oecd": "oecd-health-statistics",
    "eurostat": "eurostat-health-care-resources",
}

# ---------------------------------------------------------------------- WHO GHO

GHO_ENDPOINT = "https://ghoapi.azureedge.net/api"
GHO_METADATA = "https://www.who.int/data/gho/indicator-metadata-registry"
GHO_INDICATORS: dict[str, dict[str, Any]] = {
    "WHS6_102": {
        "name": "Hospital beds (per 10 000 population)",
        "domain": "beds",
        "unit": "per 10 000 population",
        "filter": None,
        "definition": "Hospital beds include inpatient beds available in public, private, general and specialized "
        "hospitals and rehabilitation centres (fictional wording of the indicator metadata). Method of estimation: "
        "as reported by the country (fictional).",
        "rows": [
            {"SpatialDimType": "COUNTRY", "SpatialDim": "DEU", "TimeDim": 2096, "NumericValue": 80.1},
            {"SpatialDimType": "COUNTRY", "SpatialDim": "DEU", "TimeDim": 2097, "NumericValue": 79.4},
            {"SpatialDimType": "COUNTRY", "SpatialDim": "FRA", "TimeDim": 2096, "NumericValue": 59.0},
            {"SpatialDimType": "COUNTRY", "SpatialDim": "FRA", "TimeDim": 2097, "NumericValue": 58.3},
            {"SpatialDimType": "REGION", "SpatialDim": "EUR", "TimeDim": 2097, "NumericValue": 51.2},
        ],
        "last_modified": "Mon, 10 Mar 2098 08:00:00 GMT",
    },
    "HWF_0001": {
        "name": "Medical doctors (per 10 000 population)",
        "domain": "workforce",
        "unit": "per 10 000 population",
        "filter": "SpatialDim eq 'DEU'",
        "definition": "Medical doctors include generalist and specialist medical practitioners (ISCO-08 221) "
        "(fictional wording of the indicator metadata). Method of estimation: national workforce accounts "
        "(fictional).",
        "rows": [
            {"SpatialDimType": "COUNTRY", "SpatialDim": "DEU", "TimeDim": 2096, "NumericValue": 44.2},
            {"SpatialDimType": "COUNTRY", "SpatialDim": "DEU", "TimeDim": 2097, "NumericValue": 45.3},
        ],
        "last_modified": "Mon, 10 Mar 2098 08:00:00 GMT",
        "denominator": {"text": "total population as cited by the indicator metadata (fictional)"},
    },
    "GHED_CHEGDP_SHA2011": {
        "name": "Current health expenditure (CHE) as percentage of gross domestic product (GDP) (%)",
        "domain": "expenditure",
        "unit": "percent of GDP",
        "filter": "SpatialDim eq 'DEU'",
        "definition": "Level of current health expenditure expressed as a percentage of GDP, following the System "
        "of Health Accounts 2011 (fictional wording of the indicator metadata). Method of estimation: Global Health "
        "Expenditure Database (fictional).",
        "rows": [
            {"SpatialDimType": "COUNTRY", "SpatialDim": "DEU", "TimeDim": 2096, "NumericValue": 12.7},
            # Not published for 2097: the answer has the row without a value (shown as unknown, never estimated).
            {"SpatialDimType": "COUNTRY", "SpatialDim": "DEU", "TimeDim": 2097, "NumericValue": None, "Value": ""},
        ],
        "last_modified": "Mon, 10 Mar 2098 08:00:00 GMT",
    },
}


def gho_document(code: str, *, definitions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    spec = GHO_INDICATORS[code]
    document = {
        "label": f"GHO {code} ({spec['domain']}, fictional values)",
        "indicator": code,
        "capacity_domain": spec["domain"],
        "condition_label": {"beds": "Hospital beds", "workforce": "Health workforce",
                            "expenditure": "Health expenditure"}[spec["domain"]],
        "unit": spec["unit"],
        "kind": "observation",
        "definition_locator": f"{GHO_METADATA} (indicator {code}; verify)",
        "case_definitions": definitions if definitions is not None else [{
            "key": code,
            "condition": code,
            "version": "GHO IMR 2090",
            "valid_from": "2090",
            "text": spec["definition"],
            "locator": f"{GHO_METADATA}/imr-details/{code}",
        }],
    }
    if spec["filter"]:
        document["filter"] = spec["filter"]
    if spec.get("denominator"):
        document["denominator"] = copy.deepcopy(spec["denominator"])
    return document


def gho_row(code: str, row: dict[str, Any], number: int) -> dict[str, Any]:
    value = row["NumericValue"]
    return {
        "Id": 97000000 + number,
        "IndicatorCode": code,
        "SpatialDimType": row["SpatialDimType"],
        "SpatialDim": row["SpatialDim"],
        "ParentLocationCode": "EUR",
        "TimeDimType": "YEAR",
        "TimeDim": row["TimeDim"],
        "Dim1Type": None,
        "Dim1": None,
        "Dim2Type": None,
        "Dim2": None,
        "Dim3Type": None,
        "Dim3": None,
        "DataSourceDimType": None,
        "DataSourceDim": None,
        "Value": row.get("Value", "" if value is None else f"{value}"),
        "NumericValue": value,
        "Low": None,
        "High": None,
        "Comments": None,
        "Date": "2098-03-01T09:00:00+01:00",
        "PublishState": "PUBLISHED",
        "TimeDimensionValue": str(row["TimeDim"]),
        "TimeDimensionBegin": f"{row['TimeDim']}-01-01T00:00:00+01:00",
        "TimeDimensionEnd": f"{row['TimeDim']}-12-31T00:00:00+01:00",
    }


def gho_pages(code: str, *, rows: list[dict[str, Any]] | None = None,
              last_modified: str | None = None) -> list[dict[str, Any]]:
    from src.ingestion.surveillance_sources import fixture_request, gho_urls

    spec = GHO_INDICATORS[code]
    urls = gho_urls(gho_document(code), GHO_ENDPOINT)
    body = {
        "@odata.context": f"{GHO_ENDPOINT}/$metadata#{code}",
        "value": [gho_row(code, row, n) for n, row in enumerate(rows or spec["rows"])],
    }
    metadata = {
        "@odata.context": f"{GHO_ENDPOINT}/$metadata#Indicator",
        "value": [{"IndicatorCode": code, "IndicatorName": spec["name"], "Language": "EN"}],
    }
    return [
        {"request": fixture_request(urls["metadata"]), "status": 200,
         "headers": {"Content-Type": "application/json"}, "body": json.dumps(metadata, indent=1)},
        {"request": fixture_request(urls["data"]), "status": 200,
         "headers": {"Content-Type": "application/json", "Last-Modified": last_modified or spec["last_modified"]},
         "body": json.dumps(body, indent=1)},
    ]


# ---------------------------------------------------------------------- OECD Health Statistics (SDMX-CSV)

OECD_ENDPOINT = "https://sdmx.oecd.org/public/rest/data"
OECD_HEADER = "DATAFLOW,REF_AREA,FREQ,MEASURE,UNIT_MEASURE,TIME_PERIOD,OBS_VALUE,OBS_STATUS,UNIT_MULT"
OECD_FLOWS: dict[str, dict[str, Any]] = {
    "beds": {
        "agency": "OECD.ELS.HD",
        "flow": "DSD_HEALTH_REAC_HOSP@DF_BEDS_FUNC",
        "key": "DEU+FRA+OECD.A.HOSP_BEDS.10P3HB",
        "measure": "HOSP_BEDS",
        "label": "Total hospital beds",
        "definition": "All hospital beds which are regularly maintained and staffed and immediately available for "
        "the care of admitted patients (fictional wording of the OECD definition).",
        "source_note": "Sources and methods: national administrative data compiled by OECD Health Statistics "
        "(fictional note).",
        "country_notes": {
            "FRA": "Break in series in 2097: day-care beds are excluded from 2097 onwards (fictional note).",
            "DEU": "Beds in prevention and rehabilitation facilities are excluded (fictional note).",
        },
        "rows": {
            "1.0": [("DEU", "2096", "7.9", "A"), ("DEU", "2097", "7.8", "P"), ("FRA", "2096", "5.7", "A"),
                    ("FRA", "2097", "5.6", "B"), ("OECD", "2097", "4.3", "E")],
            # Version 1.1 of the dataflow: the provisional German 2097 value is final and revised.
            "1.1": [("DEU", "2096", "7.9", "A"), ("DEU", "2097", "7.7", "A"), ("FRA", "2096", "5.7", "A"),
                    ("FRA", "2097", "5.6", "B"), ("OECD", "2097", "4.3", "E")],
        },
        "last_modified": {"1.0": "Tue, 01 Jul 2098 08:00:00 GMT", "1.1": "Mon, 03 Nov 2098 08:00:00 GMT"},
    },
    "workforce": {
        "agency": "OECD.ELS.HD",
        "flow": "DSD_HEALTH_EMP_REAC@DF_PHYS",
        "key": "DEU.A.PRACT_PHYS.10P3HB",
        "measure": "PRACT_PHYS",
        "label": "Practising physicians",
        "definition": "Physicians providing services directly to patients (fictional wording of the OECD "
        "definition).",
        "source_note": "Sources and methods: national registers (fictional note).",
        "country_notes": {},
        "rows": {"1.0": [("DEU", "2096", "4.4", "A"), ("DEU", "2097", "4.5", "A")]},
        "last_modified": {"1.0": "Tue, 01 Jul 2098 08:00:00 GMT"},
    },
}


def oecd_dataflow(domain: str, version: str = "1.0") -> str:
    spec = OECD_FLOWS[domain]
    return f"{spec['agency']},{spec['flow']},{version}"


def oecd_document(domain: str, version: str = "1.0") -> dict[str, Any]:
    spec = OECD_FLOWS[domain]
    return {
        "label": f"OECD Health Statistics {spec['flow']} {version} ({domain}, fictional values)",
        "dataflow": oecd_dataflow(domain, version),
        "key": spec["key"],
        "params": {"startPeriod": "2096"},
        "capacity_domain": domain,
        "condition_label": {"beds": "Hospital beds", "workforce": "Health workforce"}[domain],
        "measure_dimension": "MEASURE",
        "measure_labels": {spec["measure"]: spec["label"]},
        "units": {"10P3HB": "per 1000 population"},
        "source_note": spec["source_note"],
        "country_notes": copy.deepcopy(spec["country_notes"]),
        "aggregate_codes": {"OECD": "OECD member countries"},
        "kind": "observation",
        "case_definitions": [{
            "key": f"oecd:{spec['measure']}",
            "condition": spec["measure"],
            "version": "OECD Health Statistics 2090",
            "valid_from": "2090",
            "text": spec["definition"],
            "locator": "https://data-explorer.oecd.org/ (definitions, sources and methods; verify)",
        }],
    }


def oecd_csv(domain: str, version: str = "1.0") -> str:
    spec = OECD_FLOWS[domain]
    served = f"{spec['agency']}:{spec['flow']}({version})"
    lines = [OECD_HEADER]
    for area, year, value, status in spec["rows"][version]:
        lines.append(f"{served},{area},A,{spec['measure']},10P3HB,{year},{value},{status},0")
    return "\n".join(lines) + "\n"


def oecd_page(domain: str, version: str = "1.0") -> dict[str, Any]:
    from src.ingestion.surveillance_sources import document_url, fixture_request

    return {
        "request": fixture_request(document_url(oecd_document(domain, version), OECD_ENDPOINT, "oecd-sdmx-csv")),
        "status": 200,
        "headers": {"Content-Type": "text/csv", "Last-Modified": OECD_FLOWS[domain]["last_modified"][version]},
        "body": oecd_csv(domain, version),
    }


# ---------------------------------------------------------------------- Eurostat (SDMX-CSV)

EUROSTAT_ENDPOINT = "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data"
EUROSTAT_DATASETS: dict[str, dict[str, Any]] = {
    "hlth_rs_bds1": {
        "domain": "beds",
        "header": "DATAFLOW,LAST UPDATE,freq,facility,unit,geo,TIME_PERIOD,OBS_VALUE,OBS_FLAG",
        "key": "A.HBEDT.P_HTHAB.DE+FR+EU27_2020",
        "measure_dimension": "facility",
        "measure_labels": {"HBEDT": "Available beds in hospitals (HP.1)"},
        "units": {"P_HTHAB": "per 100 000 population"},
        "releases": {
            "15/03/98 11:00:00": [("HBEDT", "P_HTHAB", "DE", "2096", "790.5", ""),
                                  ("HBEDT", "P_HTHAB", "DE", "2097", "782.0", "p"),
                                  ("HBEDT", "P_HTHAB", "FR", "2096", "575.2", ""),
                                  ("HBEDT", "P_HTHAB", "FR", "2097", "569.9", ""),
                                  ("HBEDT", "P_HTHAB", "EU27_2020", "2097", "526.3", "e")],
            # A later update: the provisional German 2097 value is final and revised; nothing else changes.
            "20/09/98 11:00:00": [("HBEDT", "P_HTHAB", "DE", "2096", "790.5", ""),
                                  ("HBEDT", "P_HTHAB", "DE", "2097", "783.4", ""),
                                  ("HBEDT", "P_HTHAB", "FR", "2096", "575.2", ""),
                                  ("HBEDT", "P_HTHAB", "FR", "2097", "569.9", ""),
                                  ("HBEDT", "P_HTHAB", "EU27_2020", "2097", "526.3", "e")],
        },
        "definitions": [{
            "key": "hlth_rs_bds1:HBEDT",
            "condition": "HBEDT",
            "version": "hlth_res_esms 2090",
            "valid_from": "2090",
            "text": "Hospital beds are beds which are regularly maintained and staffed and immediately available for "
            "the care of admitted patients (fictional wording of the Eurostat metadata).",
            "locator": "https://ec.europa.eu/eurostat/cache/metadata/en/hlth_res_esms.htm",
        }],
        "denominator": {
            "text": "average population of the reference year (fictional wording)",
            "cites": {"provider": "eurostat", "provider_code": "demo_pjan",
                      "locator": "https://ec.europa.eu/eurostat/cache/metadata/en/hlth_res_esms.htm (3.4 statistical "
                                 "unit; verify)"},
        },
    },
    "hlth_sha11_hf": {
        "domain": "expenditure",
        "header": "DATAFLOW,LAST UPDATE,freq,unit,icha11_hf,geo,TIME_PERIOD,OBS_VALUE,OBS_FLAG",
        "key": "A.PC_CHE.TOT_HF+HF1.DE",
        "measure_dimension": "icha11_hf",
        "measure_labels": {"TOT_HF": "All financing schemes",
                           "HF1": "Government schemes and compulsory contributory health care financing schemes"},
        "units": {"PC_CHE": "percent of current health expenditure"},
        "releases": {
            "15/03/98 11:00:00": [("PC_CHE", "TOT_HF", "DE", "2096", "100", ""),
                                  ("PC_CHE", "TOT_HF", "DE", "2097", "100", ""),
                                  ("PC_CHE", "HF1", "DE", "2096", "77.9", ""),
                                  ("PC_CHE", "HF1", "DE", "2097", "78.3", "b")],
        },
        "definitions": [
            {
                "key": "hlth_sha11_hf:TOT_HF",
                "condition": "TOT_HF",
                "version": "SHA 2011",
                "valid_from": "2090",
                "text": "Current health expenditure across all financing schemes, System of Health Accounts 2011 "
                "(fictional wording of the Eurostat metadata).",
                "locator": "https://ec.europa.eu/eurostat/cache/metadata/en/hlth_sha11_esms.htm",
            },
            {
                "key": "hlth_sha11_hf:HF1",
                "condition": "HF1",
                "version": "SHA 1.0 HF.1",
                "valid_from": "2090",
                "valid_to": "2096-12-31",
                "text": "General government expenditure on health (SHA 1.0 classification HF.1) (fictional wording).",
                "locator": "https://ec.europa.eu/eurostat/cache/metadata/en/hlth_sha11_esms.htm",
            },
            {
                "key": "hlth_sha11_hf:HF1",
                "condition": "HF1",
                "version": "SHA 2011 HF.1",
                "valid_from": "2097-01-01",
                "text": "Government schemes and compulsory contributory health care financing schemes (SHA 2011 HF.1) "
                "(fictional wording).",
                "locator": "https://ec.europa.eu/eurostat/cache/metadata/en/hlth_sha11_esms.htm",
            },
        ],
    },
}


def eurostat_document(dataset: str) -> dict[str, Any]:
    spec = EUROSTAT_DATASETS[dataset]
    document = {
        "label": f"Eurostat {dataset} ({spec['domain']}, fictional values)",
        "dataset": dataset,
        "key": spec["key"],
        "params": {"startPeriod": "2096"},
        "capacity_domain": spec["domain"],
        "condition_label": {"beds": "Hospital beds", "expenditure": "Health expenditure"}[spec["domain"]],
        "measure_dimension": spec["measure_dimension"],
        "measure_labels": dict(spec["measure_labels"]),
        "units": dict(spec["units"]),
        "kind": "observation",
        "case_definitions": copy.deepcopy(spec["definitions"]),
    }
    if spec.get("denominator"):
        document["denominator"] = copy.deepcopy(spec["denominator"])
    return document


def eurostat_csv(dataset: str, stamp: str = "15/03/98 11:00:00") -> str:
    spec = EUROSTAT_DATASETS[dataset]
    lines = [spec["header"]]
    for first, second, geo, year, value, flag in spec["releases"][stamp]:
        lines.append(f"ESTAT:{dataset.upper()}(1.0),{stamp},A,{first},{second},{geo},{year},{value},{flag}")
    return "\n".join(lines) + "\n"


def eurostat_page(dataset: str, stamp: str = "15/03/98 11:00:00") -> dict[str, Any]:
    from src.ingestion.surveillance_sources import document_url, fixture_request

    return {
        "request": fixture_request(document_url(eurostat_document(dataset), EUROSTAT_ENDPOINT, "eurostat-sdmx-csv")),
        "status": 200,
        "headers": {"Content-Type": "text/csv"},
        "body": eurostat_csv(dataset, stamp),
    }


# ---------------------------------------------------------------------- sources and fixtures

_BUDGETS = {"timeout_ms": 30000, "max_results": 200, "max_bytes": 20000000, "max_pages": 10}


def _source(source_id, *, publisher, endpoint, scope, cadence, temporal, provider, fmt, documents, license_):
    return {
        "mapping": dict(MAPPING),
        "extractor_versions": list(EXTRACTOR),
        "health": {"required": False, "max_staleness_s": 2592000},
        "source_id": source_id,
        "connector": CONNECTOR,
        "publisher": publisher,
        "endpoint": endpoint,
        "scope": scope,
        "update_cadence": cadence,
        "temporal_semantics": temporal,
        "operations": ["records"],
        "schedule": {"kind": "interval", "interval_s": 604800},
        "budgets": dict(_BUDGETS),
        "surveillance": {"namespace": NAMESPACE, "provider": provider, "format": fmt, "documents": documents},
        "license": license_,
        "auth": {"kind": "none"},
    }


def sources() -> list[dict[str, Any]]:
    return [
        _source(
            SOURCE_IDS["gho"],
            publisher="World Health Organization (Global Health Observatory)",
            endpoint=GHO_ENDPOINT,
            scope="Pinned GHO capacity indicators through the existing GHO OData path: hospital beds (WHS6_102), "
            "medical doctors (HWF_0001) and current health expenditure as percent of GDP (GHED_CHEGDP_SHA2011)",
            cadence="irregular per indicator",
            temporal="TimeDim (reference year); Last-Modified or the latest value date as the release clock, "
            "labelled; retrieval time recorded separately",
            provider="who-gho",
            fmt="who-gho-odata",
            documents=[gho_document(code) for code in GHO_INDICATORS],
            license_={"id": "who-data-terms",
                      "terms_url": "https://www.who.int/about/policies/publishing/data-policy/terms-and-conditions",
                      "redistribution": "WHO data terms; attribution, non-commercial for CC BY-NC-SA IGO data "
                                        "(verify)"},
        ),
        _source(
            SOURCE_IDS["oecd"],
            publisher="OECD (OECD Health Statistics)",
            endpoint=OECD_ENDPOINT,
            scope="Pinned OECD Health Statistics dataflows through the existing SDMX connector: hospital beds and "
            "practising physicians for Germany, France and the OECD aggregate",
            cadence="annual release with in-place updates; a new dataflow version is a new vintage",
            temporal="TIME_PERIOD (reference year); Last-Modified or the declared publication date as the release "
            "clock; dataflow version in every native revision",
            provider="oecd-health",
            fmt="oecd-sdmx-csv",
            documents=[oecd_document(domain) for domain in OECD_FLOWS],
            license_={"id": "oecd-terms", "terms_url": "https://www.oecd.org/en/about/terms-conditions.html",
                      "redistribution": "attribution-required; CC BY 4.0 for OECD data (verify)"},
        ),
        _source(
            SOURCE_IDS["eurostat"],
            publisher="Eurostat",
            endpoint=EUROSTAT_ENDPOINT,
            scope="Eurostat health care resources (hlth_rs_bds1 hospital beds) and expenditure by financing scheme "
            "(hlth_sha11_hf) through the existing Eurostat SDMX path",
            cadence="annual",
            temporal="TIME_PERIOD (reference year); LAST UPDATE as the release clock; no reporting date per value",
            provider="eurostat-health",
            fmt="eurostat-sdmx-csv",
            documents=[eurostat_document(dataset) for dataset in EUROSTAT_DATASETS],
            license_={"id": "eurostat-reuse", "terms_url": "https://ec.europa.eu/eurostat/about-us/policies/copyright",
                      "redistribution": "attribution-required"},
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
    return {
        SOURCE_IDS["gho"]: fixture([page for code in GHO_INDICATORS for page in gho_pages(code)]),
        SOURCE_IDS["oecd"]: fixture([oecd_page(domain) for domain in OECD_FLOWS]),
        SOURCE_IDS["eurostat"]: fixture([eurostat_page(dataset) for dataset in EUROSTAT_DATASETS]),
    }


def fixture_path(source_id: str) -> str:
    return f"tests/fixtures/source_packs/health-capacity-{source_id}.json"


def fixture_text(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def build(*, write: bool = True) -> dict[str, Any]:
    from src.ingestion.source_packs import _digest, replay_native_fixture, validate_source_pack

    current = json.loads(MANIFEST.read_text())
    manifest = copy.deepcopy(current)
    # Earlier sources stay verbatim and in place; sources added by later additive releases keep their place after
    # this block, and a later pack version keeps its own version and description.
    first = next((i for i, s in enumerate(manifest["sources"]) if s["connector"] == CONNECTOR),
                 len(manifest["sources"]))
    base = [s for s in manifest["sources"][:first] if s["connector"] != CONNECTOR]
    later = [s for s in manifest["sources"][first:] if s["connector"] != CONNECTOR]
    if tuple(int(p) for p in current["version"].split(".")) <= tuple(int(p) for p in VERSION.split(".")):
        manifest["version"] = VERSION
        manifest["description"] = DESCRIPTION
    added = sources()
    authored = fixtures()
    for source in added:
        text = fixture_text(authored[source["source_id"]])
        if write:
            (ROOT / fixture_path(source["source_id"])).write_text(text)
        source["fixture"] = {"path": fixture_path(source["source_id"]),
                             "sha256": hashlib.sha256(text.encode()).hexdigest(), "expected_output_hash": "0" * 64}
    manifest["sources"] = base + added + later
    validated = validate_source_pack(manifest)
    for source in added:
        compiled = next(s for s in validated["sources"] if s["source_id"] == source["source_id"])
        stored = json.loads(fixture_text(authored[source["source_id"]]))
        source["fixture"]["expected_output_hash"] = _digest(replay_native_fixture(compiled, stored))
    if write:
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    built = build()
    print(f"wrote {MANIFEST.relative_to(ROOT)} {built['version']} with {len(built['sources'])} sources")
