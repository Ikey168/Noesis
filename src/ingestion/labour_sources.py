"""Labour and employment statistics sources for the Economics ``labour-statistics`` feature (#2219, LB01 and LB03-LB06).

Four providers are recorded under an access contract (:data:`PROVIDER_CONTRACTS`), each implemented as a format of
the ``labour-statistics`` source-pack connector:

* **ILOSTAT** (``ilostat``, format ``ilostat-sdmx-csv``) - ILO labour indicators through the ILOSTAT SDMX REST API,
  read by the existing :class:`~src.ingestion.connectors.dataset.sdmx.SDMXConnector` (provider ``ILO``, SDMX-CSV).
  A document names one dataflow (``ILO,DF_...,version``) and one series key. ILO modelled estimates and nationally
  reported series come from different dataflows and carry different ``estimate_type`` values in the series key, so
  they are never merged. The ``SOURCE`` and ``NOTE_*`` attributes (survey, coverage, indicator and classification
  notes) and break flags are kept as source notes for the definition and comparability records.
* **OECD** (``oecd``, format ``oecd-sdmx-csv``) - OECD Data Explorer labour dataflows through the same connector
  (provider ``OECD``), keyed by dataflow (agency, id, version) and series key; measure, unit and adjustment
  dimensions are stored as published and the dataflow version each response states is part of the vintage.
* **Eurostat LFS** (``eurostat-lfs``, format ``eurostat-sdmx-csv``) - Labour Force Survey datasets through the same
  connector (provider ``ESTAT``); the ``LAST UPDATE`` column dates the release and ``OBS_FLAG`` letters (``b`` break
  in series, ``u`` low reliability, ``p`` provisional, ``c`` confidential, ``d`` definition differs) are stored
  verbatim. ESMS/LFS methodology notes are declared per document and become definition and comparability notes.
* **US BLS** (``bls``, format ``bls-timeseries-json``) - BLS Public Data API v2 series keyed by BLS series ID; the
  series-ID components (survey, seasonal adjustment, area, industry, occupation, data type) are decoded as BLS
  documents them. Footnotes (``P`` preliminary, ``R`` revised) are stored per observation; the registration key is
  a runtime secret sent as a query parameter and never recorded in a URL, receipt or record.

Every provider is ``unverified-live`` until a dated live run (``docs/development/labour-evidence/``); endpoint,
parameter and field names marked *verify* come from the providers' public documentation. Nothing here nowcasts,
forecasts, fills a missing period, blends sources or re-harmonises a definition.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

CONNECTOR = "labour-statistics"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-labour-release-v1"
NEVER_SENTENCE = (
    "Published labour-force indicators as each source released them: sources and definition bases side by side, "
    "never blended or re-harmonised; no nowcast, no forecast and no filled period."
)
EXCLUSIONS = (
    "nowcasting labour-market indicators",
    "labour-market forecasts",
    "filling missing, suppressed or confidential periods",
    "blending or averaging series from different sources or definition bases",
    "re-harmonising national definitions to ILO or OECD definitions",
    "deriving indicators (rates, ratios or totals) that no source published",
)

PROVIDER_HOSTS = {
    "ilostat": {"sdmx.ilo.org"},
    "oecd": {"sdmx.oecd.org"},
    "eurostat-lfs": {"ec.europa.eu"},
    "bls": {"api.bls.gov"},
}
SDMX_PROVIDERS = {"ilostat": "ILO", "oecd": "OECD", "eurostat-lfs": "ESTAT"}

PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "ilostat": {
        "delivers": "ILO labour indicators: nationally reported series and ILO modelled estimates, kept apart",
        "access_decision": "unverified-live",
        "reason": "documented SDMX 2.1 REST API without authentication; dataflow ids and the SDMX-CSV shape not yet "
        "run live from this runtime",
        "access": "api (ILOSTAT SDMX REST API, SDMX 2.1, SDMX-CSV via format=csv; SDMX-ML also served)",
        "entry_points": [
            "https://sdmx.ilo.org/rest/data/ILO,{DATAFLOW},{version}/{key}?format=csv (verify)",
            "https://sdmx.ilo.org/rest/dataflow/ILO (dataflow list; verify)",
        ],
        "sdmx_version": "SDMX 2.1 REST",
        "authentication": "none",
        "rate_limits": "no published per-user quota (verify); each run is bounded by the declared keys, the source's "
        "max_pages and max_results and the connector's observation ceiling",
        "identifiers": {
            "dataflows": "DF_ + ILOSTAT indicator id (UNE_DEAP_SEX_AGE_RT nationally reported unemployment rate; "
            "UNE_2EAP_SEX_AGE_RT ILO modelled estimate; EMP_TEMP_SEX_ECO_NB employment by economic activity; "
            "EMP_TEMP_SEX_OCU_NB employment by occupation)",
            "areas": "REF_AREA as ISO 3166-1 alpha-3 (regional aggregates with X-codes stay distinct)",
            "classifications": "CLASSIF1 codes ECO_ISIC4_* (ISIC Rev.4) and OCU_ISCO08_* (ISCO-08)",
        },
        "indicators": {
            "employment": "EMP_TEMP_SEX_AGE_NB / _ECO_NB / _OCU_NB (thousands; national definitions per source note)",
            "unemployment": "UNE_DEAP_SEX_AGE_RT (national) and UNE_2EAP_SEX_AGE_RT (ILO modelled estimates)",
            "wages": "EAR_4MTH_SEX_ECO_CUR_NB (mean nominal monthly earnings; national definitions)",
            "hours": "HOW_TEMP_SEX_ECO_NB (mean weekly hours actually worked)",
            "vacancies": "not published by ILOSTAT as a harmonised series (out of scope)",
        },
        "definition_basis": "ILO harmonised concepts (19th ICLS Resolution I, 2013) for modelled estimates; "
        "nationally reported series follow the national source's definitions as stated in NOTE_* attributes",
        "seasonal_adjustment": "annual series are not seasonally adjusted; monthly/quarterly series state it in the "
        "dataflow (verify)",
        "update_cadence": "continuous; each country-indicator is updated when received (dataflow updates)",
        "temporal_semantics": "a changed dataflow response is a new release dated by the declared release date, else "
        "the retrieval time (labelled); the SDMX-CSV response carries no update stamp (verify)",
        "release_signals": "ILOSTAT 'last update' per indicator on the bulk download page and the dataflow annotations "
        "(verify); modelled estimates follow the ILO Trends/WESO release calendar",
        "revision_model": "a re-published country-indicator with changed values is a new vintage; earlier vintages stay",
        "terms": "ILO copyright; reuse permitted with attribution under the ILOSTAT terms (CC BY 4.0 for the database, "
        "verify)",
        "retained_evidence": "raw SDMX-CSV per request (file digest), NOTE_* and SOURCE attributes verbatim",
        "coverage": "declared dataflows, keys and periods only",
        "unavailable_fallback": "a failed request leaves earlier vintages current and is reported in the receipt",
        "verify": ["endpoint and format=csv", "dataflow ids and versions", "attribute names (OBS_STATUS, NOTE_SOURCE, "
                   "NOTE_INDICATOR, NOTE_CLASSIF, SOURCE, UNIT_MULT)", "reuse terms"],
    },
    "oecd": {
        "delivers": "OECD labour statistics (harmonised unemployment rates, employment, earnings, hours) per dataflow",
        "access_decision": "unverified-live",
        "reason": "documented SDMX REST API (.Stat Suite) without authentication; dataflow references not yet run "
        "live from this runtime",
        "access": "api (OECD Data Explorer SDMX REST API, SDMX-CSV via format=csvfile)",
        "entry_points": ["https://sdmx.oecd.org/public/rest/data/{AGENCY},{DSD@DATAFLOW},{version}/{key}"
                         "?format=csvfile (verify)"],
        "sdmx_version": "SDMX 2.1 REST (.Stat Suite; SDMX 3.0 also served)",
        "authentication": "none",
        "rate_limits": "about 20 data queries per minute per IP address (verify the current figure); each run is "
        "bounded by the source's max_pages",
        "identifiers": {
            "dataflows": "OECD.SDD.TPS,DSD_LFS@DF_IALFS_UNE_M (monthly harmonised unemployment), "
            "DSD_LFS@DF_IALFS_EMP_WAP_Q (employment), DSD_EARNINGS@AV_AN_WAGE (average annual wages) - verify",
            "areas": "REF_AREA as ISO 3166-1 alpha-3 (OECD, EA20 and other aggregates stay distinct)",
            "dimensions": "MEASURE, UNIT_MEASURE, TRANSFORMATION, ADJUSTMENT (Y seasonally adjusted, N not "
            "adjusted, T trend-cycle - verify), SEX, AGE",
        },
        "indicators": {
            "employment": "DF_IALFS_EMP_WAP_Q", "unemployment": "DF_IALFS_UNE_M (harmonised)",
            "wages": "AV_AN_WAGE (average annual wages)", "hours": "DF_HOURS (average annual hours actually worked)",
            "vacancies": "not in scope (national registered vacancies, not harmonised)",
        },
        "definition_basis": "OECD harmonised unemployment rates follow the ILO guidelines; employment and earnings "
        "series follow national definitions as documented in each dataflow's metadata",
        "seasonal_adjustment": "ADJUSTMENT dimension as published (SA, NSA, trend-cycle)",
        "update_cadence": "monthly (harmonised unemployment); quarterly or annual for others",
        "temporal_semantics": "a changed response or dataflow version is a new release dated by the declared release "
        "date, else the retrieval time (labelled); the dataflow version the response states is kept per vintage",
        "release_signals": "OECD release calendar (Unemployment Rates news release); dataflow version increments",
        "revision_model": "data updates and dataflow version changes are new vintages; earlier vintages stay",
        "terms": "OECD terms and conditions: reuse with attribution (verify)",
        "retained_evidence": "raw SDMX-CSV per request (file digest) with the DATAFLOW column",
        "coverage": "declared dataflows, keys and periods only",
        "unavailable_fallback": "a failed or rate-limited request leaves earlier vintages current",
        "verify": ["dataflow references and versions", "format=csvfile shape", "ADJUSTMENT codes", "rate limit"],
    },
    "eurostat-lfs": {
        "delivers": "EU Labour Force Survey employment and unemployment by NUTS region, sex, age, NACE and ISCO",
        "access_decision": "unverified-live",
        "reason": "documented dissemination API without authentication, read through the SDMX connector's ESTAT "
        "SDMX-CSV path; dataset codes not yet run live from this runtime",
        "access": "api (Eurostat SDMX 2.1 dissemination API, SDMX-CSV with LAST UPDATE and OBS_FLAG; JSON-stat also "
        "served)",
        "entry_points": ["https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{dataset}/{key}"
                         "?format=SDMX-CSV (verify)"],
        "sdmx_version": "SDMX 2.1 REST",
        "authentication": "none",
        "rate_limits": "no published per-user limit (verify); large extractions are asynchronous and out of scope",
        "identifiers": {
            "datasets": "lfsa_urgan (unemployment rates by sex, age, citizenship), lfst_r_lfu3rt (unemployment "
            "rates by NUTS 2), lfsa_egan2 (employment by NACE Rev.2), lfsa_egais (employment by ISCO-08), "
            "jvs_q_nace2 (job vacancy statistics) - verify",
            "areas": "geo as Eurostat GEO codes (country = ISO alpha-2 except EL and UK; NUTS 1/2 codes)",
            "classifications": "nace_r2 (NACE Rev.2), isco08 (ISCO-08 major groups OC1..OC9)",
        },
        "indicators": {
            "employment": "lfsa_egan2, lfsa_egais", "unemployment": "lfsa_urgan, lfst_r_lfu3rt",
            "wages": "earn_ses_* (Structure of Earnings Survey, four-yearly; not in the first coverage)",
            "hours": "lfsa_ewhan2 (average usual weekly hours)", "vacancies": "jvs_q_nace2",
        },
        "definition_basis": "EU-LFS definitions (Regulation (EU) 2019/1700) aligned with the ILO concepts; national "
        "deviations flagged d (definition differs) per observation",
        "seasonal_adjustment": "annual LFS series are not seasonally adjusted; the s_adj dimension where published",
        "update_cadence": "annual and quarterly releases with revisions of earlier years",
        "temporal_semantics": "the SDMX-CSV LAST UPDATE column dates the release; the time dimension is the reference "
        "period",
        "release_signals": "LAST UPDATE per dataset; Eurostat release calendar",
        "revision_model": "a changed dataset with a new LAST UPDATE is a new vintage; earlier vintages stay",
        "flags": "OBS_FLAG letters stored verbatim: b break in time series, u low reliability, p provisional, "
        "c confidential, d definition differs, e estimated (by the publisher), z not applicable",
        "terms": "Eurostat reuse policy (Commission Decision 2011/833/EU): reuse with attribution",
        "retained_evidence": "raw SDMX-CSV per request (file digest), OBS_FLAG letters and the LAST UPDATE stamp",
        "coverage": "declared datasets, keys and periods only",
        "unavailable_fallback": "a failed request leaves earlier vintages current",
        "verify": ["dataset codes and dimension order", "LAST UPDATE format", "OBS_FLAG letters", "ESMS page URLs"],
    },
    "bls": {
        "delivers": "US BLS labour series (CPS, CES, LAUS, JOLTS, OEWS) keyed by BLS series ID",
        "access_decision": "unverified-live",
        "reason": "documented REST API; version 2 requires a free registration key; not yet run live",
        "access": "api (BLS Public Data API v2, JSON; GET per series)",
        "entry_points": ["https://api.bls.gov/publicAPI/v2/timeseries/data/{seriesid}?startyear=&endyear="
                         "&catalog=true&registrationkey= (verify)"],
        "authentication": "registration key (required-secret NOESIS_BLS_KEY) sent as the registrationkey query "
        "parameter; a secret, never stored in URLs, receipts or records",
        "rate_limits": "v2 with key: 500 queries per day, 50 series per query, 20 years per query (v1 without key: "
        "25 queries, 25 series, 10 years); each run is bounded by max_pages (one request per series)",
        "identifiers": {
            "series_id": "survey prefix (LN CPS, CE CES, LA LAUS, JT JOLTS, OE OEWS), seasonal code S/U, then the "
            "survey's own area, industry, occupation and data-type fields (decoded per BLS series-ID formats)",
        },
        "indicators": {
            "employment": "CES data type 01 (all employees, thousands); CPS LNS12000000",
            "unemployment": "CPS LNS14000000 / LNU04000000; LAUS measure 03 (rate)",
            "wages": "OEWS data type 04 (annual mean wage); CES data type 03 (average hourly earnings)",
            "hours": "CES data type 02 (average weekly hours)",
            "vacancies": "JOLTS data element JO (job openings), level L or rate R",
        },
        "definition_basis": "national (US) definitions of each survey as documented in the BLS Handbook of Methods; "
        "CPS unemployment follows ILO-consistent concepts but is not an ILO harmonised series",
        "seasonal_adjustment": "S seasonally adjusted, U not seasonally adjusted (series-ID position 3)",
        "update_cadence": "monthly (CPS, CES, LAUS, JOLTS), annual (OEWS)",
        "temporal_semantics": "the declared release date (BLS release calendar) when stated, else the retrieval time "
        "(labelled); the API states no release timestamp",
        "release_signals": "BLS release calendar; P (preliminary) footnotes; the annual CES benchmark revision and "
        "annual CPS/LAUS seasonal-factor revisions",
        "revision_model": "a preliminary value and its revision are distinct vintages; annual benchmark revisions are "
        "new vintages and never delete earlier values",
        "terms": "US government work in the public domain; BLS asks for citation (verify)",
        "retained_evidence": "raw JSON per request (file digest), footnotes per observation, the catalog entry",
        "coverage": "declared series IDs and years only",
        "unavailable_fallback": "a daily-threshold refusal stops the run (rate_limited) and leaves earlier vintages",
        "verify": ["series-ID layouts per survey", "catalog field names", "threshold message text", "footnote codes"],
    },
}
LIVE_VERIFICATION = {
    provider: {
        "status": contract["access_decision"],
        "intended": "verified-live after a dated bounded run (LB13, #2493)",
        "note": "no dated live run from this runtime; offline fixtures only",
    }
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# The bounded first coverage (LB01). No record set implies complete coverage of any provider.
BOUNDED_COVERAGE = {
    "places": {
        "Germany": {"ilostat": "DEU", "oecd": "DEU", "eurostat-lfs": "DE"},
        "Berlin (NUTS 2 DE30)": {"eurostat-lfs": "DE30"},
        "United States": {"ilostat": "USA", "oecd": "USA", "bls": "national surveys (CPS, CES, JOLTS, OEWS)"},
        "California": {"bls": "LAUS state area ST0600000000000 (FIPS 06)"},
    },
    "sectors": {"manufacturing": {"ISIC Rev.4": "C", "NACE Rev.2": "C", "NAICS 2022": "31-33 (CES supersector 30)"}},
    "occupations": {"professionals / software developers": {"ISCO-08": "2 (major group)", "SOC 2018": "15-1252"}},
    "indicators": ["employment", "unemployment rate", "wages (OEWS annual mean wage)", "vacancies (JOLTS)"],
    "periods": "two to three most recent reference periods per declared series",
    "record_cap": "max_results series per response and max_pages requests per run",
}
FORMATS = {
    "ilostat-sdmx-csv": {"provider": "ilostat"},
    "oecd-sdmx-csv": {"provider": "oecd"},
    "eurostat-sdmx-csv": {"provider": "eurostat-lfs"},
    "bls-timeseries-json": {"provider": "bls"},
}
CONCEPTS = (
    "employment",
    "employment_rate",
    "unemployment",
    "unemployment_rate",
    "labour_force",
    "labour_force_participation_rate",
    "earnings",
    "hours_worked",
    "job_vacancies",
    "job_vacancy_rate",
)
MEASURES = ("level", "rate", "mean", "median")
SEASONAL = ("NSA", "SA", "trend")
DEFINITION_BASES = ("ilo-harmonised", "oecd-harmonised", "eu-lfs", "national")
ESTIMATE_TYPES = ("national-reported", "ilo-modelled", "harmonised", "survey", "administrative")
STATUSES = ("reported", "confidential", "not_published")
# Flag letters meaning a confidential or break observation (Eurostat OBS_FLAG, ILO/OECD OBS_STATUS - verify).
CONFIDENTIAL_FLAGS = {"c", "C"}
BREAK_FLAGS = {"b", "B"}
NOTE_ATTRIBUTES = ("SOURCE", "NOTE_SOURCE", "NOTE_INDICATOR", "NOTE_CLASSIF", "COMMENT_OBS", "NOTE")
FLAG_ATTRIBUTES = ("OBS_FLAG", "OBS_STATUS", "CONF_STATUS")
UNIT_MULT_ATTRIBUTES = ("UNIT_MULT",)
# BLS series-ID layouts (BLS "Series ID formats" help pages; verify before a live run).
BLS_SURVEYS = {
    "LN": "Current Population Survey (Labor Force Statistics)",
    "CE": "Current Employment Statistics (national)",
    "LA": "Local Area Unemployment Statistics",
    "JT": "Job Openings and Labor Turnover Survey",
    "OE": "Occupational Employment and Wage Statistics",
}
BLS_SEASONAL = {"S": "SA", "U": "NSA"}
BLS_CES_SUPERSECTORS = {
    "00": ("total nonfarm", None),
    "05": ("total private", None),
    "30": ("manufacturing", "31-33"),
    "20": ("construction", "23"),
    "60": ("professional and business services", "54-56"),
}
BLS_LAUS_MEASURES = {"03": "unemployment rate", "04": "unemployment", "05": "employment", "06": "labor force"}
BLS_LAUS_AREA_TYPES = {"ST": "state", "CN": "county", "MT": "metropolitan area", "CT": "city"}
BLS_CES_DATA_TYPES = {"01": "all employees, thousands", "02": "average weekly hours of all employees",
                      "03": "average hourly earnings of all employees", "11": "average weekly earnings of all employees"}
BLS_JOLTS_ELEMENTS = {"JO": "job openings", "HI": "hires", "TS": "total separations", "QU": "quits"}
BLS_OEWS_DATA_TYPES = {"01": "employment", "03": "hourly mean wage", "04": "annual mean wage"}
BLS_OEWS_AREA_TYPES = {"N": "national", "S": "state", "M": "metropolitan area"}


class LabourFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def unverified(provider: str) -> bool:
    return PROVIDER_CONTRACTS.get(provider, {}).get("access_decision") != "verified-live"


def text(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    return raw or None


def decimal_text(value: Any) -> str | None:
    """A published number as exact decimal text; ``None`` for a missing or non-numeric value (never zero)."""
    raw = text(value)
    if raw is None:
        return None
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return format(number, "f")


def iso_day(value: Any) -> str | None:
    raw = text(value)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return None


def normalise_period(period: str, frequency: str) -> str:
    """SDMX and BLS period codes in the ``dataset-series-v1`` forms (``2099``, ``2099-Q1``, ``2099-01``)."""
    raw = str(period).strip()
    match = re.fullmatch(r"(\d{4})-?M(\d{2})", raw)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    match = re.fullmatch(r"(\d{4})-?Q([1-4])", raw)
    if match:
        return f"{match.group(1)}-Q{match.group(2)}"
    return raw


# ------------------------------------------------------------------ BLS series IDs


def decode_bls_series_id(series_id: str) -> dict[str, Any]:
    """Decode a BLS series ID into survey, seasonal adjustment, area, industry, occupation and data type."""
    sid = str(series_id).strip().upper()
    prefix = sid[:2]
    if prefix not in BLS_SURVEYS or len(sid) < 4 or sid[2] not in BLS_SEASONAL:
        raise LabourFormatError("invalid_document", f"not a supported BLS series ID: {series_id!r}")
    out: dict[str, Any] = {
        "series_id": sid,
        "survey": {"code": prefix, "name": BLS_SURVEYS[prefix]},
        "seasonal_code": sid[2],
        "seasonal_adjustment": BLS_SEASONAL[sid[2]],
        "area": None,
        "industry": None,
        "occupation": None,
        "data_type": None,
        "layout": "BLS series-ID format (verify against the survey's help page)",
    }
    if prefix == "LN":
        out["area"] = {"scheme": "iso3166-1-alpha2", "code": "US", "label": "United States", "basis": "CPS is national"}
        out["data_type"] = {"code": sid[3:], "label": "CPS series code"}
    elif prefix == "CE":
        if len(sid) != 13:
            raise LabourFormatError("invalid_document", "a CES series ID has 13 characters")
        industry, supersector = sid[3:11], sid[3:5]
        label, naics = BLS_CES_SUPERSECTORS.get(supersector, (None, None))
        out["area"] = {"scheme": "iso3166-1-alpha2", "code": "US", "label": "United States", "basis": "CES national"}
        out["industry"] = {"scheme": "bls-ces-industry", "code": industry, "supersector": supersector,
                           "label": label, "naics": naics}
        out["data_type"] = {"code": sid[11:13], "label": BLS_CES_DATA_TYPES.get(sid[11:13])}
    elif prefix == "LA":
        if len(sid) != 20:
            raise LabourFormatError("invalid_document", "a LAUS series ID has 20 characters")
        area = sid[3:18]
        out["area"] = {"scheme": "bls-laus-area", "code": area, "area_type": BLS_LAUS_AREA_TYPES.get(area[:2]),
                       **({"fips_state": area[2:4]} if area[:2] == "ST" else {})}
        out["data_type"] = {"code": sid[18:20], "label": BLS_LAUS_MEASURES.get(sid[18:20])}
    elif prefix == "JT":
        if len(sid) != 21:
            raise LabourFormatError("invalid_document", "a JOLTS series ID has 21 characters")
        out["industry"] = {"scheme": "bls-jolts-industry", "code": sid[3:9]}
        state, area = sid[9:11], sid[11:16]
        out["area"] = (
            {"scheme": "iso3166-1-alpha2", "code": "US", "label": "United States", "basis": "JOLTS state 00 is "
             "the national total"}
            if state == "00" else {"scheme": "us-fips-state", "code": state}
        )
        out["size_class"] = sid[16:18]
        out["data_type"] = {"code": sid[18:20], "label": BLS_JOLTS_ELEMENTS.get(sid[18:20]),
                            "rate_or_level": {"L": "level", "R": "rate"}.get(sid[20], sid[20])}
    elif prefix == "OE":
        if len(sid) != 25:
            raise LabourFormatError("invalid_document", "an OEWS series ID has 25 characters")
        area_type, area = sid[3], sid[4:11]
        out["area"] = (
            {"scheme": "iso3166-1-alpha2", "code": "US", "label": "United States", "basis": "OEWS national"}
            if area_type == "N" else {"scheme": "bls-oews-area", "code": area,
                                      "area_type": BLS_OEWS_AREA_TYPES.get(area_type)}
        )
        out["industry"] = {"scheme": "bls-oews-industry", "code": sid[11:17]}
        occupation = sid[17:23]
        out["occupation"] = {"scheme": "SOC", "version": "2018", "code": f"{occupation[:2]}-{occupation[2:]}",
                             "native": occupation}
        out["data_type"] = {"code": sid[23:25], "label": BLS_OEWS_DATA_TYPES.get(sid[23:25])}
    return out


# ------------------------------------------------------------------ declarations


def labour_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("labour_statistics") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError(
            "invalid_manifest", "labour-statistics sources declare a known provider and its runtime format"
        )
    documents = list(declared.get("documents") or [])
    if not documents:
        raise SourcePackError("invalid_manifest", "a labour-statistics source declares its documents")
    if len(documents) > int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "more declared requests than the source's page budget")
    host = (urlsplit(source["endpoint"]).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[declared["provider"]]:
        raise SourcePackError("invalid_manifest", "the endpoint is not the provider's documented host")
    urls = []
    for document in documents:
        try:
            check_document(fmt, document)
            url = document_url(fmt, document)
        except (LabourFormatError, ValueError) as exc:
            raise SourcePackError("invalid_manifest", str(exc)) from exc
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError("invalid_manifest", "declared documents are HTTPS resources on the endpoint's host")
        urls.append(url)
    if len(set(urls)) != len(urls):
        raise SourcePackError("invalid_manifest", "each declared document is a distinct request")
    return declared


def check_document(fmt: str, document: Mapping[str, Any]) -> None:
    if not text(document.get("label")):
        raise LabourFormatError("invalid_document", "a document has a label")
    release = document.get("release")
    if release is not None and iso_day(dict(release).get("published_on")) is None:
        raise LabourFormatError("invalid_document", "a declared release states its publication date")
    indicator = dict(document.get("indicator") or {})
    if indicator.get("concept") not in CONCEPTS or indicator.get("measure") not in MEASURES:
        raise LabourFormatError("invalid_document", f"a document states its indicator concept ({CONCEPTS}) and measure")
    definition = dict(document.get("definition") or {})
    if definition.get("basis") not in DEFINITION_BASES:
        raise LabourFormatError("invalid_document", f"a document states its definition basis ({DEFINITION_BASES})")
    if document.get("estimate_type") not in ESTIMATE_TYPES:
        raise LabourFormatError("invalid_document", f"a document states its estimate type ({ESTIMATE_TYPES})")
    if not dict(document.get("unit") or {}).get("label"):
        raise LabourFormatError("invalid_document", "a document states its unit")
    for ref in document.get("references") or []:
        if not text(dict(ref).get("identifier")) or not text(dict(ref).get("kind")):
            raise LabourFormatError("invalid_document", "each reference states its kind and identifier as published")
    if fmt == "bls-timeseries-json":
        decode_bls_series_id(str(document.get("series_id") or ""))
        start, end = int(document.get("startyear") or 0), int(document.get("endyear") or 0)
        if not start or not end or not 0 <= end - start < 20:
            raise LabourFormatError("invalid_document", "a BLS document states startyear and endyear (at most 20 years)")
        return
    seasonal = document.get("seasonal_adjustment")
    if isinstance(seasonal, Mapping):
        if not seasonal.get("dimension") or not set(dict(seasonal.get("codes") or {}).values()) <= set(SEASONAL):
            raise LabourFormatError("invalid_document", "a seasonal-adjustment dimension maps its codes to NSA/SA/trend")
    elif seasonal not in SEASONAL:
        raise LabourFormatError("invalid_document", "a document states its seasonal adjustment (NSA, SA or trend)")
    area = dict(document.get("area") or {})
    if not area.get("dimension") or not area.get("scheme"):
        raise LabourFormatError("invalid_document", "an SDMX document names its area dimension and code scheme")
    for role in ("sector", "occupation"):
        spec = document.get(role)
        if spec is not None and not (dict(spec).get("dimension") and dict(spec).get("scheme")
                                     and dict(spec).get("version")):
            raise LabourFormatError("invalid_document", f"a {role} dimension states its classification and version")


def document_url(fmt: str, document: Mapping[str, Any], *, secret: str | None = None) -> str:
    """The request URL; the BLS registration key is only added for the transport (never recorded)."""
    if fmt == "bls-timeseries-json":
        params = {"startyear": str(document["startyear"]), "endyear": str(document["endyear"]), "catalog": "true"}
        if secret:
            params["registrationkey"] = secret
        return (
            f"https://api.bls.gov/publicAPI/v2/timeseries/data/{str(document['series_id']).upper()}?"
            + urlencode(sorted(params.items()))
        )
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector

    provider = SDMX_PROVIDERS[FORMATS[fmt]["provider"]]
    url, query = SDMXConnector(provider).csv_url(
        str(document.get("flow") or ""), str(document.get("key") or ""), dict(document.get("params") or {})
    )
    return url + "?" + urlencode(sorted(query.items()))


def public_url(url: str) -> str:
    """A request URL without the registration key."""
    base, _, query = url.partition("?")
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k.casefold() != "registrationkey"]
    return base + ("?" + urlencode(kept) if kept else "")


# ------------------------------------------------------------------ parsing


def _declared_release(document: Mapping[str, Any]) -> tuple[str | None, str | None]:
    release = dict(document.get("release") or {})
    return iso_day(release.get("published_on")), text(release.get("label"))


def _definition(document: Mapping[str, Any], provider: str) -> dict[str, Any]:
    definition = dict(document.get("definition") or {})
    return {
        "provider": provider,
        "indicator_code": text(dict(document["indicator"]).get("code")) or text(document.get("series_id")),
        "concept": document["indicator"]["concept"],
        "measure": document["indicator"]["measure"],
        "basis": definition["basis"],
        "estimate_type": document["estimate_type"],
        "age_bounds": text(definition.get("age_bounds")),
        "coverage": text(definition.get("coverage")),
        "source_text": text(definition.get("source_text")),
        "methodology_notes": [str(n) for n in definition.get("methodology_notes") or []],
    }


def _flag_letters(attributes: Mapping[str, Any]) -> set[str]:
    letters: set[str] = set()
    for key in FLAG_ATTRIBUTES:
        value = text(attributes.get(key))
        if value:
            letters |= set(value)
    return letters


def _classification(spec: Mapping[str, Any] | None, dims: Mapping[str, str]) -> dict[str, Any] | None:
    if not spec:
        return None
    raw = dims.get(spec["dimension"])
    if raw is None:
        return None
    code = str(raw)
    prefix = str(spec.get("strip_prefix") or "")
    stripped = code[len(prefix):] if prefix and code.startswith(prefix) else code
    return {"scheme": spec["scheme"], "version": str(spec["version"]), "code": stripped, "native": code,
            **({"label": dict(spec.get("labels") or {}).get(code)} if dict(spec.get("labels") or {}).get(code) else {})}


def parse_sdmx(raw: bytes, *, fmt: str, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    """Declared SDMX-CSV series as labour items: one per dimension combination, flags and notes verbatim."""
    from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector
    from src.integrations.common import IntegrationError

    provider = FORMATS[fmt]["provider"]
    connector = SDMXConnector(SDMX_PROVIDERS[provider])
    ref = SeriesRef(locator=str(document["flow"]) + "/" + str(document.get("key") or ""),
                    metadata={"flow": str(document["flow"])}, title=document.get("label"))
    try:
        records = connector.parse_csv(RawSeries(ref, raw, content_type="text/csv", source_url=url, fetched_at=0))
    except IntegrationError as exc:
        raise LabourFormatError("schema_drift", f"{exc.code}: {exc}") from exc
    if not records:
        raise LabourFormatError("schema_drift", "the response states no series")
    definition = _definition(document, provider)
    area_spec = dict(document["area"])
    seasonal = document["seasonal_adjustment"]
    items, dataflows, last_update = [], set(), None
    for record in records:
        meta = record.metadata
        dims = {str(k): str(v) for k, v in dict(meta["dimensions"]).items()}
        dataflows.add(meta.get("dataflow"))
        last_update = meta.get("provider_last_update_at") or last_update
        area_code = dims.get(area_spec["dimension"])
        if area_code is None:
            raise LabourFormatError("schema_drift", "the response lacks the declared area dimension")
        if isinstance(seasonal, Mapping):
            code = dims.get(seasonal["dimension"])
            adjustment = dict(seasonal["codes"]).get(code)
            if adjustment is None:
                raise LabourFormatError("schema_drift", f"unmapped seasonal-adjustment code {code!r}")
        else:
            adjustment = seasonal
        observations, notes, breaks, multipliers = [], {}, [], set()
        for observation in sorted(record.observations, key=lambda o: o.period):
            attributes = {str(k): str(v) for k, v in dict(meta["observation_attributes"].get(observation.period)
                                                         or {}).items()}
            value_text = meta["original_values"].get(observation.period)
            value = decimal_text(value_text)
            letters = _flag_letters(attributes)
            status = "reported" if value is not None else (
                "confidential" if letters & CONFIDENTIAL_FLAGS else "not_published")
            period = normalise_period(observation.period, record.frequency)
            if letters & BREAK_FLAGS:
                breaks.append(period)
            for key in NOTE_ATTRIBUTES:
                if attributes.get(key):
                    notes.setdefault((key, attributes[key]), []).append(period)
            multiplier = next((attributes[k] for k in UNIT_MULT_ATTRIBUTES if attributes.get(k)), None)
            if multiplier is not None:
                multipliers.add(multiplier)
            observations.append({
                "period": period,
                "value_text": value_text if value_text not in (None, "") else None,
                "value": value,
                "status": status,
                "flags": {k: attributes[k] for k in sorted(attributes) if k in FLAG_ATTRIBUTES},
                "attributes": {k: attributes[k] for k in sorted(attributes) if k not in FLAG_ATTRIBUTES},
                "footnotes": [],
                "unit_multiplier": multiplier,
            })
        source_notes = [
            {"kind": "source-attribute", "attribute": key, "value": value, "periods": periods}
            for (key, value), periods in sorted(notes.items())
        ]
        if breaks:
            source_notes.append({"kind": "break", "attribute": "flag", "value": "break in series", "periods": breaks})
        items.append({
            "provider": provider,
            "native_key": ".".join(dims[k] for k in dims),
            "dataflow": {"reference": str(document["flow"]), "stated": meta.get("dataflow")},
            "indicator": dict(document["indicator"]),
            "definition": definition,
            "estimate_type": document["estimate_type"],
            "seasonal_adjustment": adjustment,
            "frequency": record.frequency,
            "unit": dict(document["unit"]),
            "unit_multiplier": sorted(multipliers)[0] if len(multipliers) == 1 else None,
            "area": {"scheme": area_spec["scheme"], "code": area_code,
                     **({"label": dict(area_spec.get("labels") or {}).get(area_code)}
                        if dict(area_spec.get("labels") or {}).get(area_code) else {})},
            "sector": _classification(document.get("sector"), dims),
            "occupation": _classification(document.get("occupation"), dims),
            "dimensions": dims,
            "source_notes": source_notes,
            "references": [dict(r) for r in document.get("references") or []],
            "denominator": dict(document["denominator"]) if document.get("denominator") else None,
            "revision_window_periods": document.get("revision_window_periods"),
            "observations": observations,
        })
    items.sort(key=lambda i: i["native_key"])
    declared_on, declared_label = _declared_release(document)
    if last_update:
        stamp = datetime.fromisoformat(last_update)
        stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
        published_at, published_on, basis = stamp.isoformat(), stamp.date().isoformat(), "provider_last_update"
    elif declared_on:
        published_at, published_on, basis = None, declared_on, "declared_release"
    else:
        published_at, published_on, basis = None, None, "retrieval_time"
    stated = sorted(d for d in dataflows if d)
    return {
        "provider": provider,
        "format": fmt,
        "items": items,
        "item_count": len(items),
        "published_on": published_on,
        "published_at": published_at,
        "release_basis": basis,
        "release_label": declared_label,
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": digest(items),
        "structure": {"dataflow": str(document["flow"]), "dataflow_stated": stated,
                      "dataflow_version": _flow_version(stated[0] if stated else str(document["flow"])),
                      "last_update": last_update, "series": len(items)},
    }


def _flow_version(reference: str) -> str | None:
    match = re.search(r"\((\d+(?:\.\d+)*)\)\s*$", reference) or re.search(r",(\d+(?:\.\d+)*)\s*$", reference)
    return match.group(1) if match else None


def parse_bls(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    """One BLS series response: values and footnotes per period, the series ID decoded."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LabourFormatError("schema_drift", "BLS response is not JSON") from exc
    status = str(body.get("status") or "")
    messages = [str(m) for m in body.get("message") or []]
    if status != "REQUEST_SUCCEEDED":
        joined = " ".join(messages).casefold()
        code = "rate_limited" if "threshold" in joined or "daily" in joined else "schema_drift"
        raise LabourFormatError(code, f"BLS request not processed: {status}")
    series = list(dict(body.get("Results") or {}).get("series") or [])
    sid = str(document["series_id"]).upper()
    entry = next((s for s in series if str(s.get("seriesID") or "").upper() == sid), None)
    if entry is None:
        raise LabourFormatError("schema_drift", "the response does not state the requested series")
    decoded = decode_bls_series_id(sid)
    observations = []
    seen = set()
    for row in entry.get("data") or []:
        period_code = str(row.get("period") or "")
        year = str(row.get("year") or "")
        if re.fullmatch(r"M(0[1-9]|1[0-2])", period_code):
            period, frequency = f"{year}-{period_code[1:]}", "monthly"
        elif period_code == "A01":
            period, frequency = year, "annual"
        elif re.fullmatch(r"Q0[1-4]", period_code):
            period, frequency = f"{year}-Q{period_code[-1]}", "quarterly"
        else:
            continue  # M13 annual averages and semiannual periods are not requested (catalog annualaverage=false)
        if period in seen:
            raise LabourFormatError("schema_drift", "the response repeats a period")
        seen.add(period)
        value_text = text(row.get("value"))
        value = decimal_text(value_text)
        footnotes = [
            {k: text(f.get(k)) for k in ("code", "text") if text(f.get(k))}
            for f in row.get("footnotes") or []
            if isinstance(f, Mapping) and (text(f.get("code")) or text(f.get("text")))
        ]
        observations.append({
            "period": period,
            "value_text": value_text,
            "value": value,
            "status": "reported" if value is not None else "not_published",
            "flags": {"footnote_codes": "".join(sorted(f["code"] for f in footnotes if f.get("code")))}
            if any(f.get("code") for f in footnotes) else {},
            "attributes": {"latest": str(row["latest"])} if row.get("latest") is not None else {},
            "footnotes": footnotes,
            "unit_multiplier": document.get("unit_multiplier"),
            "_frequency": frequency,
        })
    frequencies = {o.pop("_frequency") for o in observations}
    if len(frequencies) > 1:
        raise LabourFormatError("schema_drift", "the response mixes frequencies")
    observations.sort(key=lambda o: o["period"])
    area = dict(document.get("area") or {}) or decoded["area"]
    industry = decoded["industry"]
    sector = None
    if industry and industry.get("naics"):
        sector = {"scheme": "NAICS", "version": "2022", "code": industry["naics"],
                  "native": industry["code"], "native_scheme": industry["scheme"],
                  "basis": "BLS states the NAICS basis of the CES supersector (verify)"}
    elif industry and industry["code"].strip("0"):
        sector = {"scheme": industry["scheme"], "version": "as published", "code": industry["code"],
                  "native": industry["code"]}
    occupation = decoded["occupation"]
    if occupation and occupation["native"] == "000000":
        occupation = None
    catalog = dict(entry.get("catalog") or {})
    item = {
        "provider": "bls",
        "native_key": sid,
        "dataflow": {"reference": decoded["survey"]["code"], "stated": decoded["survey"]["name"]},
        "indicator": dict(document["indicator"]),
        "definition": _definition(document, "bls"),
        "estimate_type": document["estimate_type"],
        "seasonal_adjustment": decoded["seasonal_adjustment"],
        "frequency": next(iter(frequencies), "irregular"),
        "unit": dict(document["unit"]),
        "unit_multiplier": document.get("unit_multiplier"),
        "area": area,
        "sector": sector,
        "occupation": occupation,
        "dimensions": {"series_id": sid, "decoded": decoded},
        "source_notes": [
            {"kind": "catalog", "attribute": key, "value": str(catalog[key]), "periods": []}
            for key in sorted(catalog) if key in {"series_title", "survey_name", "measure_data_type", "seasonality"}
        ],
        "references": [dict(r) for r in document.get("references") or []],
        "denominator": dict(document["denominator"]) if document.get("denominator") else None,
        "revision_window_periods": document.get("revision_window_periods"),
        "observations": observations,
    }
    declared_on, declared_label = _declared_release(document)
    return {
        "provider": "bls",
        "format": "bls-timeseries-json",
        "items": [item],
        "item_count": 1,
        "published_on": declared_on,
        "published_at": None,
        "release_basis": "declared_release" if declared_on else "retrieval_time",
        "release_label": declared_label,
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": digest([item]),
        "structure": {"series_id": sid, "survey": decoded["survey"], "catalog": catalog, "messages": messages,
                      "dataflow_version": None},
    }


# ------------------------------------------------------------------ adapter


class LabourStatisticsAdapter:
    """Fetch the declared labour documents on the runtime's default transport; one page (one release) per document."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        transport: Callable[..., Mapping[str, Any]] | None = None,
        secret: str | None = None,
    ) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = labour_declaration(self.source)
        self.secret = secret
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT,
            "source_id": source["source_id"],
            "connector": source["connector"],
            "endpoint": source["endpoint"],
            "operations": list(source["operations"]),
            "source_hash": source["source_hash"],
            "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"],
            "limits": source["budgets"],
            "labour_statistics": {
                "provider": self.declared["provider"],
                "format": self.declared["format"],
                "keyed": bool(secret),
                "live_verification": LIVE_VERIFICATION[self.declared["provider"]]["status"],
            },
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "labour runs fetch the declared documents only")

    def _get(self, url: str) -> tuple[bytes, str]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        parts = urlsplit(url)
        if (parts.hostname or "").casefold() != host or parts.scheme != "https":
            raise SourcePackError("network_policy", "declared documents are fetched from the endpoint's host only")
        base, _, query = url.partition("?")
        response = self.transport(
            url=base,
            params=parse_qsl(query, keep_blank_values=True),
            headers={"Accept": "application/json, text/csv"},
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "response was served from another host")
        status = int(response.get("status", 200))
        headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "response exceeds its byte limit")
        if status == 429:
            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(headers.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"request refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        return raw, "fixture" if response.get("origin") == "fixture" else "live"

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        documents = list(self.declared["documents"])
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(documents):
            raise SourcePackError("cursor_drift", "cursor names no declared document")
        document = dict(documents[index])
        fmt = self.declared["format"]
        if fmt == "bls-timeseries-json" and not self.secret:
            raise SourcePackError("authentication_failed", "the BLS Public Data API v2 requires its registration key")
        request_url = document_url(fmt, document, secret=self.secret)
        url = public_url(request_url)
        raw, origin = self._get(request_url)
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        try:
            if fmt == "bls-timeseries-json":
                release = parse_bls(raw, document=document)
            else:
                release = parse_sdmx(raw, fmt=fmt, document=document, url=url)
        except LabourFormatError as exc:
            if exc.code == "rate_limited":
                raise SourcePackError("rate_limited", "BLS daily query threshold reached") from exc
            raise SourcePackError("schema_drift", f"{exc.code}: {exc}") from exc
        if release["item_count"] > limit:
            # Never a truncated release: a missing series would read as a series that was not published.
            raise SourcePackError("budget_exhausted", "release has more series than the run's result budget")
        header = {
            "contract": RELEASE_CONTRACT,
            "provider": release["provider"],
            "format": release["format"],
            "document": document,
            "published_on": release["published_on"],
            "published_at": release["published_at"],
            "release_basis": release["release_basis"],
            "release_label": release["release_label"],
            "file_sha256": release["file_sha256"],
            "content_sha256": release["content_sha256"],
            "item_count": release["item_count"],
            "structure": release["structure"],
            "evidence_origin": origin,
            "live_verification": LIVE_VERIFICATION[release["provider"]]["status"],
            "url": url,
        }
        records = [
            {
                "id": f"{release['file_sha256'][:16]}:{number}",
                "title": f"{document.get('label')} ({release['published_on'] or 'retrieved'})",
                "url": url,
                "language": "en",
                "published_at": release["published_on"],
                "content": json.dumps(item, sort_keys=True, ensure_ascii=False),
                "labour_release": header,
                "labour_item": item,
            }
            for number, item in enumerate(release["items"])
        ]
        receipt = {
            "status": 200,
            "provider": release["provider"],
            "document": document.get("label"),
            "published_on": release["published_on"],
            "release_basis": release["release_basis"],
            "file_sha256": release["file_sha256"],
            "items": len(records),
            "requests": 1,
            "evidence_origin": origin,
            "final_page": index + 1 >= len(documents),
        }
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


FIXTURE_SECRET = "fixture-credential-not-a-real-key"
ADAPTERS = {CONNECTOR: LabourStatisticsAdapter}


def _fixture_key(url: str, params: Any) -> str:
    pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
    pairs = [(k, v) for k, v in pairs if str(k).casefold() != "registrationkey"]
    query = urlencode(sorted(pairs))
    return urlsplit(url).path + ("?" + query if query else "")


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path and sorted query (the registration key is never part of a key)."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        key = _fixture_key(url, params)
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture"}

    return transport


def fixture_request(fmt: str, document: Mapping[str, Any]) -> str:
    """The key :func:`fixture_transport` files a response under."""
    base, _, query = document_url(fmt, document).partition("?")
    return _fixture_key(base, parse_qsl(query, keep_blank_values=True))


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = LabourStatisticsAdapter(
        source, transport=fixture_transport(list(fixture["native_pages"])), secret=FIXTURE_SECRET
    )
    records, cursor = [], None
    while True:
        page = adapter.fetch_page(
            {"operation": min(source["operations"]), "parameters": {}, "limit": int(source["budgets"]["max_results"])},
            cursor=cursor,
        )
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


__all__ = [
    "ADAPTERS",
    "BOUNDED_COVERAGE",
    "CONCEPTS",
    "CONNECTOR",
    "DEFINITION_BASES",
    "EXCLUSIONS",
    "FORMATS",
    "LIVE_VERIFICATION",
    "LabourFormatError",
    "LabourStatisticsAdapter",
    "NEVER_SENTENCE",
    "PROVIDER_CONTRACTS",
    "SEASONAL",
    "decode_bls_series_id",
    "fixture_request",
    "fixture_transport",
    "labour_declaration",
    "parse_bls",
    "parse_sdmx",
    "public_url",
    "replay_native_fixture",
]
