"""Public-health surveillance acquisition for the Clinical Evidence ``surveillance`` feature (#1917, I01/I03-I05).

One native connector, ``surveillance``, fetches a bounded, declared list of
publications per source - one publication per page, one *release* (source
revision) per publication - and parses each into surveillance series whose
values are exactly as published. Every value keeps its **reference date or
period** and its **reporting date** as two separate fields; a value that
carries only one of them says which one is unknown. Supported documented
formats:

* ``rki-github-csv`` - Robert Koch-Institut open-data releases on GitHub
  (``raw.githubusercontent.com/robert-koch-institut/<repository>/<tag>/<path>``),
  pinned by release tag: a declared column layout names the reporting date
  (``Meldedatum``), the reference date (``Refdatum``), the district code
  (``IdLandkreis``) and the value column. Where the dataset flags that its
  reference date is only the reporting date substituted (``IstErkrankungsbeginn
  = 0``), the reference date is recorded as unknown with the source's text kept.
  Case-definition editions (Falldefinitionen) are declared with their
  valid-from dates and locators and never inferred.
* ``who-gho-odata`` - WHO Global Health Observatory OData API
  (``/api/Indicator`` for the indicator name, ``/api/{IndicatorCode}`` for the
  values): ``TimeDim`` is the reference period, ``Date`` the reporting date,
  ``Low``/``High`` bounds make a value an ``estimate`` (never an observation).
* ``eurostat-sdmx-csv`` - Eurostat health datasets (``hlth_*``) through the
  existing :class:`src.ingestion.connectors.dataset.sdmx.SDMXConnector`
  (SDMX-CSV 1.0, no second SDMX client): ``OBS_FLAG`` letters (``p``
  provisional, ``e`` estimated, ``b`` break in series, ``d`` definition
  differs, ``c`` confidential, ``u`` low reliability) are kept on each value
  and the dataset's ``LAST UPDATE`` is the release clock.
* ``destatis-genesis-ffcsv`` - Destatis GENESIS-Online health tables through
  the existing :class:`src.ingestion.connectors.dataset.genesis.GenesisConnector`
  (credentialed); the table's ``Updated`` stamp is the release clock and every
  GENESIS sign is an absent value with its published meaning.
* ``ecdc-atlas-csv`` - ECDC Surveillance Atlas CSV exports. The Atlas has no
  documented machine interface, so it is never fetched or scraped here: an
  operator supplies the export and :func:`parse_ecdc_export` reads it.

SurvStat@RKI has no documented public machine interface either and is not
acquired (``not-implemented``). Nothing is recomputed, completed, nowcast or
predicted: missing or suppressed markers (empty, ``.``, ``-``, ``x``, ``:``,
``NA``...) are absent values with their text kept and are never compared as
numbers; a column, unit, dimension, code system or row shape the declaration
does not name is refused as schema drift rather than read partially, and a
release is all-or-nothing.

``PROVIDER_CONTRACTS`` records the I01 access decisions; they are merged into
:data:`src.ingestion.clinical_providers.PROVIDER_CONTRACTS`. Live claims marked
*verify* still need checking against the providers' live terms and responses.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-surveillance-release-v1"
EXPORT_CONTRACT = "noesis-surveillance-export-v1"
CONNECTOR = "surveillance"
EXTRACTOR = "surveillance-sources:1.0.0"
KINDS = ("observation", "estimate", "model-output")
INTERVALS = ("day", "week", "month", "quarter", "year")
GEOGRAPHY_SYSTEMS = (
    "ags",
    "rki-landkreis",
    "nuts",
    "eu-country",
    "iso3166-1-alpha2",
    "iso3166-1-alpha3",
    "who-region",
    "who-global",
    "ecdc-aggregate",
    "eurostat-aggregate",
    # OECD area aggregates (OECD, OECDE, EU27_2020 ...) published beside countries by OECD Health Statistics (#2215).
    "oecd-aggregate",
)
# Country groupings Eurostat and ECDC publish beside countries (EU27_2020, EU28, EA20, EEA, EFTA, EU_EEA31...).
# None is a country or a NUTS region: they have no single boundary and are never read as NUTS codes.
AGGREGATE_CODE = re.compile(r"^(?:EU|EA|EEA|EFTA)(?:\d{1,2})?(?:_[A-Z0-9]+)*$")
NUTS_CODE = re.compile(r"^[A-Z]{2}[A-Z0-9]{1,3}$")
COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")
CONDITION_SCHEMES = (
    "rki-meldekategorie",
    "ecdc-health-topic",
    "gho-indicator",
    "eurostat-icd10",
    "destatis-icd10",
    # Health-system capacity indicators (#2215): the condition is the capacity domain (beds, workforce, expenditure);
    # the source's own indicator or measure code is the series' indicator code.
    "health-capacity",
)
CITATION_KINDS = (
    "doi",
    "rki-release",
    "gho-indicator",
    "eurostat-dataset",
    "genesis-table",
    "ecdc-indicator",
    "dataset-accession",
    "oecd-dataflow",
)
# Published unit labels: (pint unit, exact factor to that unit, kind). A count is never turned into a rate and a
# rate is never turned into a count; no denominator is inferred.
UNITS: dict[str, tuple[str, Decimal, str]] = {
    "cases": ("count", Decimal(1), "count"),
    "deaths": ("count", Decimal(1), "count"),
    "persons": ("count", Decimal(1), "count"),
    "thousand cases": ("count", Decimal(1000), "count"),
    "per 100 000 population": ("ppm", Decimal(10), "rate"),
    "per 1 000 000 population": ("ppm", Decimal(1), "rate"),
    "per 1000 population": ("permille", Decimal(1), "rate"),
    "percent": ("percent", Decimal(1), "rate"),
    # Health-system capacity units (#2215): densities and shares as published; a density is never turned into a
    # headcount (no population denominator is inferred) and a share never into an amount.
    "beds": ("count", Decimal(1), "count"),
    "per 10 000 population": ("ppm", Decimal(100), "rate"),
    "percent of GDP": ("percent", Decimal(1), "rate"),
    "percent of current health expenditure": ("percent", Decimal(1), "rate"),
}
# Markers every source uses for a suppressed, confidential or missing value; they are never numbers.
MISSING_MARKERS = frozenset(
    {"", ".", "..", "...", "x", "X", "-", ":", "/", "NA", "N/A", "n/a", "na", "null"}
)
EUROSTAT_FLAGS = {
    "p": "provisional",
    "e": "estimated",
    "b": "break in time series",
    "d": "definition differs",
    "c": "confidential",
    "u": "low reliability",
    "n": "not significant",
    "s": "Eurostat estimate",
    "f": "forecast (as published by the provider)",
    "r": "revised",
    "z": "not applicable",
}
FORMATS: dict[str, dict[str, Any]] = {
    "rki-github-csv": {"provider": "rki-open-data", "jurisdiction": "DE"},
    "who-gho-odata": {"provider": "who-gho", "jurisdiction": "INT"},
    "eurostat-sdmx-csv": {"provider": "eurostat-health", "jurisdiction": "EU"},
    "destatis-genesis-ffcsv": {"provider": "destatis-health", "jurisdiction": "DE"},
    "ecdc-atlas-csv": {"provider": "ecdc-atlas", "jurisdiction": "EU"},
    # OECD Health Statistics through the existing SDMX connector (health-system capacity, #2215).
    "oecd-sdmx-csv": {"provider": "oecd-health", "jurisdiction": "INT"},
}
FETCHED_FORMATS = (
    "rki-github-csv",
    "who-gho-odata",
    "eurostat-sdmx-csv",
    "destatis-genesis-ffcsv",
    "oecd-sdmx-csv",
)
MAX_ROWS = 200_000
MAX_GHO_PAGES = 10
BOUNDARY = (
    "Published surveillance values only, each with its reporting date and reference date kept apart, its kind "
    "(observation, estimate or model output), unit, interval, case-definition revision and source revision. No "
    "outbreak prediction, nowcast, completeness estimate, threshold suggestion or health advice is produced; a "
    "case-definition change is a marked break, never a corrected value; sources stay side by side, never merged."
)

# I01 access decisions. Endpoints, layouts, identifiers and terms are recorded from the providers' published
# documentation as known without network access; every item marked ``verify`` must be checked against the live
# terms and a published response before a dated live run is accepted (#2034).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "rki-open-data": {
        "documentation": "https://github.com/robert-koch-institut",
        "publisher": "Robert Koch-Institut (open-data repositories on GitHub, mirrored on Zenodo)",
        "access": "raw files of a pinned release tag or commit: raw.githubusercontent.com/robert-koch-institut/"
        "<repository>/<tag>/<path>.csv; the release date comes from the GitHub release or Zenodo record and is "
        "declared with the document (verify per repository)",
        "authentication": "none",
        "rate_limits": "GitHub raw-content limits apply (undocumented for raw files; verify); one request per "
        "declared file per run",
        "pagination": "none (one CSV per declared file)",
        "cadence": "per repository: daily, weekly or on release (verify); every release tag is a new vintage",
        "terms": "CC BY 4.0 as stated in each repository's licence file (verify per repository)",
        "retained_evidence": "raw CSV digest, repository, tag and path, declared column layout, row numbers",
        "identifiers": [
            "repository and release tag or commit",
            "Zenodo DOI of the release (where published)",
            "IdLandkreis (AGS Kreis codes; Berlin districts use RKI's own 11001-11012 codes)",
            "Meldekategorie / disease name as the dataset writes it",
        ],
        "cross_references": "a publication citing the release DOI or repository@tag links by explicit citation",
        "delivers": ["observation"],
        "dates": "Meldedatum (reporting date: when the health office learned of the case) and Refdatum (reference "
        "date: onset where known, otherwise the reporting date substituted, flagged by IstErkrankungsbeginn) as "
        "separate columns (verify per dataset)",
        "case_definitions": "Falldefinitionen editions published by the RKI (PDF, by edition year); the edition "
        "in force is declared per document with its valid-from date and locator, never inferred",
        "geography_codes": "AGS Kreis codes (IdLandkreis) and Bundesland codes; RKI's own Berlin district codes",
        "condition_identifiers": "disease names per dataset (Meldekategorie); no ICD code in the data",
        "units": "cases (counts); some datasets publish incidences per 100 000 population",
        "status": "implemented",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser and connector; repository names, column names, tags and the release "
        "date source need a dated live run",
        "unavailable_fallback": "record the provider failure, keep every stored vintage and mark the source stale",
    },
    "rki-survstat": {
        "documentation": "https://survstat.rki.de/",
        "publisher": "Robert Koch-Institut (SurvStat@RKI 2.0)",
        "access": "interactive web application with manual CSV export; no documented public API or bulk export "
        "was identified (a web-service endpoint used by the application is not documented for reuse; verify)",
        "authentication": "n/a",
        "rate_limits": "n/a",
        "pagination": "n/a",
        "cadence": "weekly (Meldewoche)",
        "terms": "RKI terms of use for SurvStat (verify)",
        "retained_evidence": "n/a",
        "identifiers": [
            "Meldewoche (ISO reporting week)",
            "Kreis and Bundesland names as displayed",
        ],
        "cross_references": "none",
        "delivers": ["observation"],
        "dates": "reporting week only (Meldewoche); no reference date in the export",
        "case_definitions": "counts follow the RKI reference definition (Referenzdefinition) of the edition in force",
        "geography_codes": "names in the export, not codes (names are never matched to boundaries)",
        "condition_identifiers": "Meldekategorie names",
        "units": "cases; incidences per 100 000",
        "status": "not-implemented",
        "access_decision": "not-implemented",
        "reason": "no documented machine access; the portal is not scraped. Notifiable-disease series come from the "
        "RKI GitHub open-data releases instead",
        "unavailable_fallback": "use the RKI open-data releases; SurvStat values stay unknown",
    },
    "ecdc-atlas": {
        "documentation": "https://atlas.ecdc.europa.eu/public/index.aspx",
        "publisher": "European Centre for Disease Prevention and Control (Surveillance Atlas of Infectious Diseases)",
        "access": "interactive atlas with a CSV export per selection; no documented API (verify). Exports enter only "
        "as operator-supplied files (import_surveillance_export) in the export's column layout: HealthTopic, "
        "Population, Indicator, Unit, Time, RegionCode, RegionName, NumValue, TxtValue (verify)",
        "authentication": "n/a",
        "rate_limits": "n/a (no automated requests)",
        "pagination": "n/a",
        "cadence": "annual for most diseases, weekly or monthly for some (verify)",
        "terms": "ECDC copyright notice: reuse with attribution (verify)",
        "retained_evidence": "the supplied export's digest, the declared extraction date and selection, row numbers",
        "identifiers": [
            "HealthTopic (disease)",
            "Indicator and Population names",
            "RegionCode (country and NUTS codes, EU/EEA aggregates)",
        ],
        "cross_references": "a publication citing the Atlas indicator links by explicit citation",
        "delivers": ["observation"],
        "dates": "reference period (Time: year or month); the export's data-extraction date is the release clock; "
        "values carry no reporting date (recorded as unknown)",
        "case_definitions": "EU case definitions (Commission Implementing Decision (EU) 2018/945 and later; verify) "
        "are declared per export when known",
        "geography_codes": "country codes (EL for Greece, EU convention), NUTS codes, EU/EEA aggregate codes",
        "condition_identifiers": "ECDC health-topic names",
        "units": "N (cases), N/100000 (notification rate) (verify)",
        "status": "not-implemented",
        "access_decision": "not-implemented",
        "reason": "no documented machine access; the atlas is never fetched or scraped. Operator-supplied exports "
        "are read by parse_ecdc_export and marked evidence_origin=operator",
        "unavailable_fallback": "import an export supplied by the operator; otherwise ECDC values stay unknown",
    },
    "who-gho": {
        "documentation": "https://www.who.int/data/gho/info/gho-odata-api",
        "publisher": "World Health Organization, Global Health Observatory",
        "access": "OData API ghoapi.azureedge.net/api: /Indicator (codes and names), /DIMENSION and "
        "/DIMENSION/{code}/DimensionValues (value sets), /{IndicatorCode}?$filter=... (values). WHO has announced a "
        "successor data API; the OData host's continued service must be verified",
        "authentication": "none",
        "rate_limits": "undocumented (verify); at most 10 pages per declared indicator per run",
        "pagination": "@odata.nextLink (followed on the same host; a longer answer is refused, never truncated)",
        "cadence": "irregular per indicator (verify)",
        "terms": "WHO terms of use for data (CC BY-NC-SA 3.0 IGO for most GHO data; verify)",
        "retained_evidence": "raw JSON digests per request, indicator name, row Ids",
        "identifiers": [
            "IndicatorCode",
            "SpatialDim (ISO 3166-1 alpha-3 countries, WHO region codes)",
            "Dim1/Dim2/Dim3 value codes (SEX_*, AGEGROUP_*...)",
        ],
        "cross_references": "a publication citing the IndicatorCode links by explicit citation",
        "delivers": ["observation", "estimate", "model-output"],
        "dates": "TimeDim (reference year) and Date (the value's publication or update date, used as its reporting "
        "date; verify semantics); a row without Date has an unknown reporting date",
        "case_definitions": "indicator metadata pages (not machine-readable here); declared per document when known",
        "geography_codes": "ISO 3166-1 alpha-3 (COUNTRY), WHO region codes (REGION), GLOBAL",
        "condition_identifiers": "indicator codes (a condition label is declared per document)",
        "units": "per indicator (rates per 100 000 population, counts, percent)",
        "status": "implemented",
        "access_decision": "unverified-live",
        "reason": "fixture-verified parser; the host, the Date semantics and the kind of each indicator (reported, "
        "estimated or modelled) need a dated live run",
        "unavailable_fallback": "record the provider failure, keep every stored vintage and mark the source stale",
    },
    "eurostat-health": {
        "documentation": "https://ec.europa.eu/eurostat/web/health/database",
        "publisher": "Eurostat (health statistics: causes of death, hlth_cd_*)",
        "access": "SDMX 2.1 dissemination API (ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{dataset}/"
        "{key}?format=SDMX-CSV) through the existing SDMX dataset connector; the same API family as the "
        "eurostat-dissemination source of the economic-statistics-and-filings pack (verify the SDMX-CSV column set)",
        "authentication": "none",
        "rate_limits": "fair use; large extractions are delivered asynchronously (verify); one request per declared "
        "dataset key per run",
        "pagination": "none; bounded by the declared series key",
        "cadence": "annual (causes of death)",
        "terms": "Eurostat reuse policy (Commission Decision 2011/833/EU), attribution required",
        "retained_evidence": "raw SDMX-CSV digest, dataflow, LAST UPDATE stamp, row numbers, OBS_FLAG per value",
        "identifiers": [
            "dataset codes hlth_cd_aro (deaths by cause, NUTS 2), hlth_cd_asdr2 (standardised death "
            "rates)",
            "icd10 dimension codes (grouped ICD-10 chapters and blocks)",
            "geo (country codes, NUTS)",
        ],
        "cross_references": "a publication citing the dataset code links by explicit citation",
        "delivers": ["observation"],
        "dates": "TIME_PERIOD (reference year); LAST UPDATE is the release clock; no reporting date per value",
        "case_definitions": "ICD-10 based cause-of-death lists (European shortlist); the icd10 code is the condition",
        "geography_codes": "country codes (EU convention) and NUTS (version per dataset; verify)",
        "condition_identifiers": "icd10 dimension codes",
        "units": "NR (number), RT (rate per 100 000 inhabitants)",
        "status": "implemented",
        "access_decision": "unverified-live",
        "reason": "fixture-verified through the SDMX connector's SDMX-CSV reader; dataset keys, the LAST UPDATE "
        "column format and flag letters need a dated live run",
        "unavailable_fallback": "record the provider failure, keep every stored vintage and mark the source stale",
    },
    "destatis-health": {
        "documentation": "https://www.destatis.de/",
        "publisher": "Statistisches Bundesamt (Destatis), GENESIS-Online health tables (23211 causes of death)",
        "access": "GENESIS-Online REST API 2020: metadata/table (Updated stamp) and data/tablefile?format=ffcsv "
        "through the existing GENESIS dataset connector; credentials in request headers (verify header names)",
        "authentication": "registered user or token (required-secret NOESIS_DESTATIS_GENESIS_TOKEN)",
        "rate_limits": "undocumented; large tables are background jobs (verify); one table per declared document",
        "pagination": "none",
        "cadence": "annual",
        "terms": "Datenlizenz Deutschland - Namensnennung 2.0 (verify)",
        "retained_evidence": "values with GENESIS signs, table code and Updated stamp, response digests",
        "identifiers": [
            "table codes 23211-* (verify the Land-level table code)",
            "ICD-10 cause codes (TODUR* Merkmal; verify)",
            "DLAND (AGS Land codes)",
        ],
        "cross_references": "a publication citing the table code links by explicit citation",
        "delivers": ["observation"],
        "dates": "Zeit (reference year); the table's Updated stamp is the release clock; no reporting date per value",
        "case_definitions": "ICD-10 WHO cause-of-death coding; the cause code is the condition",
        "geography_codes": "AGS Land codes",
        "condition_identifiers": "ICD-10 cause codes as GENESIS publishes them",
        "units": "Anzahl (deaths)",
        "status": "implemented",
        "access_decision": "unverified-live",
        "reason": "credentialed API; fixture-verified through the GENESIS connector only, the table code, Merkmal "
        "codes and header names need a dated live run",
        "unavailable_fallback": "record the provider failure, keep every stored vintage and mark the source stale",
    },
}
PROVIDER_HOSTS = {
    "rki-open-data": {"raw.githubusercontent.com"},
    "who-gho": {"ghoapi.azureedge.net"},
    "eurostat-health": {"ec.europa.eu"},
    "destatis-health": {"www-genesis.destatis.de"},
    "oecd-health": {"sdmx.oecd.org"},
}


class SurveillanceFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        ).encode()
    ).hexdigest()


def _clean(value: Any) -> str | None:
    text = " ".join(str(value if value is not None else "").split())
    return text or None


# ---------------------------------------------------------------------- dates and values

_WEEK = re.compile(r"^(\d{4})-W(\d{2})$")
_QUARTER = re.compile(r"^(\d{4})-Q([1-4])$")


def normalise_period(value: Any) -> str | None:
    """A date or period in ISO form (``2099``, ``2099-Q1``, ``2099-03``, ``2099-W10``, ``2099-03-02``); else None."""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    if (
        re.fullmatch(r"\d{4}", text)
        or _QUARTER.fullmatch(text)
        or _WEEK.fullmatch(text)
    ):
        return text
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return text if 1 <= int(text[5:]) <= 12 else None
    if re.fullmatch(r"\d{4}Q[1-4]", text):
        return f"{text[:4]}-Q{text[5]}"
    if re.fullmatch(r"\d{4}M\d{2}", text):
        return f"{text[:4]}-{text[5:]}"
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T ].*)?$", text)
    if match:
        try:
            return date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            return None
    match = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    if match:
        try:
            return date(
                int(match.group(3)), int(match.group(2)), int(match.group(1))
            ).isoformat()
        except ValueError:
            return None
    return None


def period_bounds(period: str) -> tuple[date, date]:
    """First and last day a date or period covers."""
    if re.fullmatch(r"\d{4}", period):
        year = int(period)
        return date(year, 1, 1), date(year, 12, 31)
    quarter = _QUARTER.fullmatch(period)
    if quarter:
        year, q = int(quarter.group(1)), int(quarter.group(2))
        first = date(year, 3 * q - 2, 1)
        last = (
            date(year + 1, 1, 1) if q == 4 else date(year, 3 * q + 1, 1)
        ) - timedelta(days=1)
        return first, last
    week = _WEEK.fullmatch(period)
    if week:
        first = date.fromisocalendar(int(week.group(1)), int(week.group(2)), 1)
        return first, first + timedelta(days=6)
    if re.fullmatch(r"\d{4}-\d{2}", period):
        year, month = int(period[:4]), int(period[5:])
        first = date(year, month, 1)
        last = (
            date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        ) - timedelta(days=1)
        return first, last
    day = date.fromisoformat(period)
    return day, day


def number_key(value: Any) -> str | None:
    """A canonical plain-decimal text for comparing published numbers (never shown in place of the published text)."""
    if value is None:
        return None
    text = format(Decimal(str(value)).normalize(), "f")
    return "0" if text in {"-0", ""} else text


def parse_decimal(text: Any) -> str | None:
    """The published number as exact decimal text; a missing or suppressed marker is ``None``."""
    raw = str(text if text is not None else "").strip()
    if raw in MISSING_MARKERS:
        return None
    candidate = raw.replace(" ", "").replace(" ", "")
    try:
        number = Decimal(candidate)
    except InvalidOperation as exc:
        raise SurveillanceFormatError(
            "schema_drift", f"{raw!r} is not a published number"
        ) from exc
    if not number.is_finite():
        raise SurveillanceFormatError("schema_drift", f"{raw!r} is not a finite number")
    return str(number)


# ---------------------------------------------------------------------- validation (shared with the store)


def check_unit(label: Any) -> str:
    if label not in UNITS:
        raise SurveillanceFormatError(
            "schema_drift", f"unit {label!r} is not a declared surveillance unit"
        )
    return str(label)


def check_geography(geography: Mapping[str, Any]) -> None:
    if geography.get("system") not in GEOGRAPHY_SYSTEMS or not _clean(
        geography.get("code")
    ):
        raise SurveillanceFormatError(
            "invalid_geography", "a geography names its code system and code"
        )


def case_definition_dates(revision: Mapping[str, Any]) -> tuple[str, str | None]:
    """The validity of a case-definition revision as ISO dates.

    A date in any accepted notation (``2019-01-01``, ``01.01.2019``) is that day; a bare year or month starts on
    its first day (valid-from) or ends on its last day (valid-to). The raw declaration is never compared as text.
    """
    valid_from = normalise_period(revision.get("valid_from"))
    if valid_from is None:
        raise SurveillanceFormatError(
            "invalid_case_definition", "a case definition states its valid-from date"
        )
    start = period_bounds(valid_from)[0].isoformat()
    end = None
    if revision.get("valid_to") is not None:
        valid_to = normalise_period(revision["valid_to"])
        if valid_to is None:
            raise SurveillanceFormatError(
                "invalid_case_definition", "valid-to is a date or period"
            )
        end = period_bounds(valid_to)[1].isoformat()
        if end < start:
            raise SurveillanceFormatError(
                "invalid_case_definition", "valid-to is a date after valid-from"
            )
    return start, end


def normalise_case_definition(revision: Mapping[str, Any]) -> dict[str, Any]:
    """A revision with ISO valid-from/valid-to; a declaration in another notation is kept as declared_validity."""
    start, end = case_definition_dates(revision)
    out = {
        k: revision.get(k) for k in ("key", "version", "text", "locator", "icd_scope")
    }
    out.update(valid_from=start, valid_to=end)
    declared = {
        "valid_from": revision.get("valid_from"),
        "valid_to": revision.get("valid_to"),
    }
    if revision.get("declared_validity"):
        out["declared_validity"] = dict(revision["declared_validity"])
    elif declared != {"valid_from": start, "valid_to": end}:
        out["declared_validity"] = declared
    return out


def check_case_definition(revision: Mapping[str, Any]) -> None:
    if not _clean(revision.get("key")) or not _clean(revision.get("version")):
        raise SurveillanceFormatError(
            "invalid_case_definition", "a case definition names its key and version"
        )
    case_definition_dates(revision)
    if not _clean(revision.get("text")) and not str(
        revision.get("locator") or ""
    ).startswith("https://"):
        raise SurveillanceFormatError(
            "invalid_case_definition",
            "a case definition carries its text or an HTTPS locator",
        )


def check_item(item: Mapping[str, Any]) -> None:
    """One series as parsed: identity, kind, unit, interval and values with their two dates."""
    if item.get("kind") not in KINDS:
        raise SurveillanceFormatError(
            "missing_kind",
            "a surveillance series states its kind: observation, estimate "
            "or model-output",
        )
    condition = dict(item.get("condition") or {})
    if condition.get("scheme") not in CONDITION_SCHEMES or not _clean(
        condition.get("code")
    ):
        raise SurveillanceFormatError(
            "invalid_condition", "a series names its condition scheme and code"
        )
    check_geography(dict(item.get("geography") or {}))
    check_unit(dict(item.get("unit") or {}).get("label"))
    if item.get("interval") not in INTERVALS:
        raise SurveillanceFormatError(
            "invalid_interval", f"interval is one of {INTERVALS}"
        )
    if not _clean(dict(item.get("indicator") or {}).get("code")):
        raise SurveillanceFormatError(
            "invalid_indicator", "a series names its indicator code"
        )
    for citation in item.get("citations") or []:
        if citation.get("kind") not in CITATION_KINDS or not _clean(
            citation.get("identifier")
        ):
            raise SurveillanceFormatError(
                "invalid_citation", "a citable identifier names a known kind and value"
            )
    for revision in (item.get("case_definition") or {}).get("revisions") or []:
        check_case_definition(revision)
    keys = set()
    for value in item.get("values") or []:
        reference, reporting = (
            value.get("reference_period"),
            value.get("reporting_date"),
        )
        if reference is None and reporting is None:
            raise SurveillanceFormatError(
                "invalid_value",
                "a value has a reference period, a reporting date or both",
            )
        for label, period in (
            ("reference_period", reference),
            ("reporting_date", reporting),
        ):
            if period is not None and normalise_period(period) != period:
                raise SurveillanceFormatError(
                    "invalid_value", f"{label} {period!r} is not an ISO date or period"
                )
        for field in ("value", "lower", "upper"):
            if value.get(field) is not None:
                try:
                    number = Decimal(str(value[field]))
                except InvalidOperation as exc:
                    raise SurveillanceFormatError(
                        "invalid_value", f"{field} {value[field]!r} is not a number"
                    ) from exc
                if not number.is_finite():
                    raise SurveillanceFormatError(
                        "invalid_value", f"{field} is not a finite number"
                    )
        if item["kind"] == "observation" and (
            value.get("lower") is not None or value.get("upper") is not None
        ):
            raise SurveillanceFormatError(
                "kind_mismatch",
                "an observation carries no bounds; bounded values are estimates",
            )
        key = (reference or "", reporting or "")
        if key in keys:
            raise SurveillanceFormatError(
                "duplicate_value",
                "a series states the same reference and reporting date twice",
            )
        keys.add(key)


# ---------------------------------------------------------------------- shared item assembly


def _case_definition(
    document: Mapping[str, Any], condition_code: str
) -> dict[str, Any] | None:
    revisions = [
        dict(r)
        for r in document.get("case_definitions") or []
        if r.get("condition") in (None, condition_code)
    ]
    if not revisions:
        return None
    keys = {r["key"] for r in revisions}
    if len(keys) != 1:
        raise SurveillanceFormatError(
            "invalid_case_definition", "a condition has one case-definition key"
        )
    return {
        "key": keys.pop(),
        "revisions": sorted(
            (normalise_case_definition(r) for r in revisions),
            key=lambda r: (r["valid_from"], r["version"]),
        ),
    }


def _item(
    document: Mapping[str, Any],
    *,
    condition,
    indicator,
    geography,
    unit,
    interval,
    kind,
    dimensions,
    values,
    citations,
    locator,
    denominator=None,
    definition_code=None,
) -> dict[str, Any]:
    item = {
        "condition": condition,
        "indicator": indicator,
        "geography": geography,
        "unit": unit,
        "interval": interval,
        "kind": kind,
        "dimensions": dict(sorted(dimensions.items())),
        "denominator": denominator,
        "citations": sorted(
            (
                {"kind": c["kind"], "identifier": str(c["identifier"])}
                for c in citations
            ),
            key=lambda c: (c["kind"], c["identifier"]),
        ),
        # Definitions are declared per condition code, or per the source's own indicator/measure code for
        # health-capacity series (#2215), whose condition is the capacity domain.
        "case_definition": _case_definition(document, definition_code or condition["code"]),
        "delay_note": dict(document["delay_note"])
        if document.get("delay_note")
        else None,
        "values": sorted(
            values,
            key=lambda v: (
                v.get("reference_period") or "",
                v.get("reporting_date") or "",
            ),
        ),
        "locator": locator,
    }
    check_item(item)
    return item


def _geography(
    document: Mapping[str, Any],
    code: str,
    label: Any = None,
    *,
    system: str | None = None,
):
    declared = dict(document.get("geography") or {})
    return {
        "system": system or declared.get("system"),
        "code": str(code).strip(),
        "label": _clean(label),
        "code_list_version": declared.get("code_list_version"),
    }


def _unit(document: Mapping[str, Any], published: str) -> dict[str, str]:
    units = dict(document.get("units") or {})
    label = units.get(published) if units else document.get("unit")
    if label is None:
        raise SurveillanceFormatError(
            "schema_drift", f"unit {published!r} is not declared for this document"
        )
    return {"label": check_unit(label), "published": published}


def _csv_rows(raw: bytes, delimiter: str = ",") -> list[dict[str, str]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SurveillanceFormatError("schema_drift", "CSV is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    rows = []
    for row in reader:
        if None in row or any(v is None for v in row.values()):
            raise SurveillanceFormatError(
                "schema_drift", f"CSV row {reader.line_num} does not match the header"
            )
        rows.append(row)
        if len(rows) > MAX_ROWS:
            raise SurveillanceFormatError("input_limit", "CSV exceeds the row budget")
    if reader.fieldnames is None:
        raise SurveillanceFormatError("schema_drift", "CSV has no header")
    return rows


# ---------------------------------------------------------------------- RKI GitHub open data


def rki_url(document: Mapping[str, Any]) -> str:
    repository, tag, path = (
        str(document.get(k) or "") for k in ("repository", "tag", "path")
    )
    if not re.fullmatch(r"robert-koch-institut/[A-Za-z0-9_.-]+", repository):
        raise SurveillanceFormatError(
            "invalid_declaration",
            "RKI documents name a robert-koch-institut repository",
        )
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+", tag)
        or not re.fullmatch(r"[A-Za-z0-9_./-]+\.csv", path)
        or ".." in path
    ):
        raise SurveillanceFormatError(
            "invalid_declaration", "RKI documents pin a release tag and a CSV path"
        )
    return f"https://raw.githubusercontent.com/{repository}/{tag}/{quote(path)}"


def parse_rki(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    columns = dict(document.get("columns") or {})
    for role in ("reporting_date", "reference_date", "geography", "figure"):
        if not columns.get(role):
            raise SurveillanceFormatError(
                "invalid_declaration", f"the RKI layout names its {role} column"
            )
    rows = _csv_rows(raw, str(document.get("delimiter") or ","))
    if not rows:
        raise SurveillanceFormatError("schema_drift", "the release has no rows")
    dimension_columns = dict(columns.get("dimensions") or {})
    known = columns.get("reference_known")
    needed = [
        columns[r] for r in ("reporting_date", "reference_date", "geography", "figure")
    ]
    needed += list(dimension_columns) + ([known] if known else [])
    missing = [c for c in needed if c not in rows[0]]
    if missing:
        raise SurveillanceFormatError(
            "schema_drift", f"declared columns {missing} are not in the release"
        )
    condition = dict(document["condition"])
    indicator = dict(document["indicator"])
    unit = {
        "label": check_unit(document.get("unit")),
        "published": str(document.get("unit")),
    }
    citations = list(document.get("citations") or []) + [
        {
            "kind": "rki-release",
            "identifier": f"{document['repository']}@{document['tag']}",
        }
    ]
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for number, row in enumerate(rows, start=2):
        geo = str(row[columns["geography"]]).strip()
        if not geo:
            raise SurveillanceFormatError(
                "schema_drift", f"row {number} has no geography code"
            )
        dims = {
            name: str(row[column]).strip()
            for column, name in sorted(dimension_columns.items())
        }
        reporting_text = str(row[columns["reporting_date"]]).strip()
        reference_text = str(row[columns["reference_date"]]).strip()
        reporting = normalise_period(reporting_text)
        reference = normalise_period(reference_text)
        if reporting_text and reporting is None or reference_text and reference is None:
            raise SurveillanceFormatError(
                "schema_drift", f"row {number} has a date that is not ISO"
            )
        flags = []
        if known:
            marker = str(row[known]).strip()
            if marker not in {"0", "1"}:
                raise SurveillanceFormatError(
                    "schema_drift", f"row {number}: {known} is 0 or 1"
                )
            if marker == "0":
                # The source filled its reference date with the reporting date; that is not a reference date.
                reference = None
                flags.append("reference-date-substituted-by-source")
        text = str(row[columns["figure"]]).strip()
        value = {
            "reference_period": reference,
            "reporting_date": reporting,
            "value_text": text,
            "value": parse_decimal(text),
            "lower": None,
            "upper": None,
            "flags": flags,
            "source_reference_text": reference_text or None,
            "locator": {"row": number},
        }
        if value["value"] is None:
            value["flags"] = [*flags, "missing-as-published"]
        groups.setdefault((geo, tuple(sorted(dims.items()))), []).append(value)
    series = []
    for (geo, dims), values in sorted(groups.items()):
        series.append(
            _item(
                document,
                condition=condition,
                indicator=indicator,
                geography=_geography(document, geo),
                unit=unit,
                interval=str(document.get("interval") or "day"),
                kind=str(document.get("kind") or ""),
                dimensions=dict(dims),
                values=values,
                citations=citations,
                locator={
                    "repository": document["repository"],
                    "tag": document["tag"],
                    "path": document["path"],
                },
            )
        )
    return {
        "series": series,
        "native_revision": f"{document['repository']}@{document['tag']}",
        "published_on": normalise_period(document.get("published_on")),
        "published_at": None,
        "release_basis": "declared_release" if document.get("published_on") else None,
        "structure": {"columns": list(rows[0]), "layout": columns},
    }


# ---------------------------------------------------------------------- health-system capacity (#2215)

CAPACITY_DOMAINS = ("beds", "workforce", "expenditure")


def capacity_condition(document: Mapping[str, Any]) -> dict[str, Any] | None:
    """The condition of a health-system capacity document: its declared capacity domain (else ``None``).

    A capacity series names the domain (beds, workforce, expenditure) under the ``health-capacity`` scheme; the
    source's own indicator or measure code stays the series' indicator code and keys its declared definitions.
    """
    domain = document.get("capacity_domain")
    if domain is None:
        return None
    if domain not in CAPACITY_DOMAINS:
        raise SurveillanceFormatError(
            "invalid_declaration", f"capacity_domain is one of {CAPACITY_DOMAINS}"
        )
    return {
        "scheme": "health-capacity",
        "code": str(domain),
        "label": _clean(document.get("condition_label")) or str(domain),
    }


def capacity_indicator(
    document: Mapping[str, Any], code: str, dimension: str, measure: str
) -> dict[str, Any]:
    """A capacity series' indicator: the source's code, its published label and notes, all verbatim."""
    indicator = {
        "code": code,
        "label": _clean(dict(document.get("measure_labels") or {}).get(measure)),
        "measure_dimension": dimension,
        "measure": measure,
    }
    for key in ("dataset", "dataflow", "source_note", "definition_locator"):
        if document.get(key):
            indicator[key] = document[key]
    return indicator


# ---------------------------------------------------------------------- WHO GHO OData

_GHO_SPATIAL = {
    "COUNTRY": "iso3166-1-alpha3",
    "REGION": "who-region",
    "GLOBAL": "who-global",
}
_GHO_CODE = re.compile(r"^[A-Za-z0-9_.-]{2,80}$")
_GHO_FILTER = re.compile(
    r"^(?:(?:SpatialDim|TimeDim|Dim1|Dim2|Dim3) eq '[A-Za-z0-9_.-]{1,40}'(?: and |$))+$"
)


def gho_urls(document: Mapping[str, Any], endpoint: str) -> dict[str, str]:
    code = str(document.get("indicator") or "")
    if not _GHO_CODE.fullmatch(code):
        raise SurveillanceFormatError(
            "invalid_declaration", "GHO documents name an IndicatorCode"
        )
    base = endpoint.rstrip("/")
    data = f"{base}/{code}"
    selection = str(document.get("filter") or "")
    if selection:
        if not _GHO_FILTER.fullmatch(selection):
            raise SurveillanceFormatError(
                "invalid_declaration",
                "GHO filters are equality on SpatialDim, TimeDim "
                "or Dim1-3 joined by 'and'",
            )
        data += "?" + urlencode({"$filter": selection})
    return {
        "metadata": f"{base}/Indicator?"
        + urlencode({"$filter": f"IndicatorCode eq '{code}'"}),
        "data": data,
    }


def parse_gho(
    pages: Sequence[bytes], metadata: bytes, *, document: Mapping[str, Any]
) -> dict[str, Any]:
    code = str(document["indicator"])
    try:
        meta = json.loads(metadata)
        payloads = [json.loads(page) for page in pages]
    except (ValueError, UnicodeDecodeError) as exc:
        raise SurveillanceFormatError(
            "schema_drift", "GHO returned non-JSON content"
        ) from exc
    names = [
        m
        for m in (meta.get("value") if isinstance(meta, Mapping) else None) or []
        if isinstance(m, Mapping) and m.get("IndicatorCode") == code
    ]
    if len(names) != 1:
        raise SurveillanceFormatError(
            "schema_drift", "the GHO Indicator list does not name this indicator once"
        )
    indicator = {
        "code": code,
        "label": _clean(names[0].get("IndicatorName")),
        "definition_locator": document.get("definition_locator"),
    }
    declared_kind = str(document.get("kind") or "")
    condition = capacity_condition(document) or {
        "scheme": "gho-indicator",
        "code": code,
        "label": _clean(document.get("condition_label")),
    }
    groups: dict[tuple, list[dict[str, Any]]] = {}
    reporting_dates = []
    count = 0
    for page_no, payload in enumerate(payloads):
        rows = payload.get("value") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise SurveillanceFormatError(
                "schema_drift", "a GHO page has no value list"
            )
        for index, row in enumerate(rows):
            count += 1
            if count > MAX_ROWS:
                raise SurveillanceFormatError(
                    "input_limit", "GHO answer exceeds the row budget"
                )
            if not isinstance(row, Mapping) or row.get("IndicatorCode") != code:
                raise SurveillanceFormatError(
                    "schema_drift", "a GHO row belongs to another indicator"
                )
            system = _GHO_SPATIAL.get(str(row.get("SpatialDimType") or ""))
            if system is None:
                raise SurveillanceFormatError(
                    "schema_drift",
                    f"SpatialDimType {row.get('SpatialDimType')!r} is not "
                    "a supported code system",
                )
            if (
                row.get("TimeDimType") != "YEAR"
                or normalise_period(row.get("TimeDim")) is None
            ):
                raise SurveillanceFormatError(
                    "schema_drift", "GHO rows are read by YEAR TimeDim only"
                )
            dims = {}
            for n in (1, 2, 3):
                kind, value = row.get(f"Dim{n}Type"), row.get(f"Dim{n}")
                if kind and value:
                    dims[str(kind)] = str(value)
                elif kind or value:
                    raise SurveillanceFormatError(
                        "schema_drift", f"Dim{n} names a type without a value or back"
                    )
            if row.get("PublishState"):
                # A row's publish state (where the answer states one) is part of the series key, kept verbatim.
                dims["PublishState"] = str(row["PublishState"])
            numeric = row.get("NumericValue")
            text = str(row.get("Value") if row.get("Value") is not None else "")
            low, high = row.get("Low"), row.get("High")
            bounded = low is not None or high is not None
            kind = (
                "estimate"
                if bounded and declared_kind == "observation"
                else declared_kind
            )
            reporting = normalise_period(row.get("Date")) if row.get("Date") else None
            if row.get("Date") and reporting is None:
                raise SurveillanceFormatError(
                    "schema_drift", "a GHO Date is not an ISO timestamp"
                )
            if reporting:
                reporting_dates.append(reporting)
            value = None if numeric is None else parse_decimal(str(numeric))
            flags = [] if value is not None else ["missing-as-published"]
            if row.get("Comments"):
                flags.append("comment: " + str(_clean(row["Comments"]))[:300])
            groups.setdefault(
                (system, str(row.get("SpatialDim")), kind, tuple(sorted(dims.items()))),
                [],
            ).append(
                {
                    "reference_period": normalise_period(row.get("TimeDim")),
                    "reporting_date": reporting,
                    "value_text": text,
                    "value": value,
                    "lower": None if low is None else parse_decimal(str(low)),
                    "upper": None if high is None else parse_decimal(str(high)),
                    "flags": flags,
                    "locator": {"page": page_no, "index": index, "id": row.get("Id")},
                }
            )
    series = []
    unit = {
        "label": check_unit(document.get("unit")),
        "published": str(document.get("unit")),
    }
    for (system, spatial, kind, dims), values in sorted(groups.items()):
        series.append(
            _item(
                document,
                condition=condition,
                indicator=indicator,
                geography={
                    "system": system,
                    "code": spatial,
                    "label": None,
                    "code_list_version": None,
                },
                unit=unit,
                interval="year",
                kind=kind,
                dimensions=dict(dims),
                values=values,
                citations=[
                    {"kind": "gho-indicator", "identifier": code},
                    *list(document.get("citations") or []),
                ],
                locator={"indicator": code},
                denominator=document.get("denominator"),
                definition_code=code,
            )
        )
    latest = max(reporting_dates) if reporting_dates else None
    return {
        "series": series,
        "native_revision": f"gho:{code}:{latest or 'undated'}",
        "published_on": normalise_period(document.get("published_on")),
        "published_at": None,
        "release_basis": "declared_publication"
        if document.get("published_on")
        else None,
        "fallback_release": latest,
        "structure": {"indicator": indicator, "pages": len(payloads), "rows": count},
    }


# ---------------------------------------------------------------------- Eurostat through the SDMX connector


def eurostat_url(document: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector

    dataset = str(document.get("dataset") or "")
    if not re.fullmatch(r"hlth_[a-z0-9_]+", dataset):
        raise SurveillanceFormatError(
            "invalid_declaration",
            "Eurostat surveillance documents name an hlth_* dataset",
        )
    try:
        return SDMXConnector("ESTAT").csv_url(
            dataset, str(document.get("key") or ""), dict(document.get("params") or {})
        )
    except ValueError as exc:
        raise SurveillanceFormatError("invalid_declaration", str(exc)) from exc


def parse_eurostat(
    raw: bytes,
    *,
    document: Mapping[str, Any],
    url: str = "",
    max_bytes: int = 20_000_000,
):
    from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector
    from src.integrations.common import IntegrationError

    dataset = str(document["dataset"])
    try:
        records = SDMXConnector(
            "ESTAT", max_bytes=max_bytes, max_observations=100000
        ).parse_csv(
            RawSeries(
                SeriesRef(dataset, metadata={"flow": dataset}),
                raw,
                source_url=url or None,
            )
        )
    except IntegrationError as exc:
        raise SurveillanceFormatError("schema_drift", f"{exc.code}: {exc}") from exc
    if not records:
        raise SurveillanceFormatError(
            "schema_drift", "the dataset answer has no series"
        )
    capacity = capacity_condition(document)
    if capacity is not None and not document.get("measure_dimension"):
        raise SurveillanceFormatError(
            "invalid_declaration",
            "capacity documents name the dimension that carries the measure",
        )
    condition_dimension = str(
        document.get("measure_dimension")
        if capacity is not None
        else document.get("condition_dimension") or "icd10"
    )
    frequencies = {"A": "year", "Q": "quarter", "M": "month", "W": "week", "D": "day"}
    series = []
    updates = set()
    for record in records:
        meta = record.metadata
        dims = dict(meta["dimensions"])
        for required in (condition_dimension, "geo", "unit", "freq"):
            if required not in dims:
                raise SurveillanceFormatError(
                    "schema_drift", f"the dataset has no {required} dimension"
                )
        condition_code = dims.pop(condition_dimension)
        geo, unit_code, freq = dims.pop("geo"), dims.pop("unit"), dims.pop("freq")
        if freq not in frequencies:
            raise SurveillanceFormatError(
                "schema_drift", f"frequency {freq!r} is not supported"
            )
        updates.add(meta.get("provider_last_update_at"))
        values = []
        for obs in record.observations:
            text = meta["original_values"][obs.period]
            attributes = dict(meta["observation_attributes"].get(obs.period) or {})
            flag_text = str(attributes.pop("OBS_FLAG", "") or "")
            unknown = [c for c in flag_text if c.strip() and c not in EUROSTAT_FLAGS]
            if unknown:
                raise SurveillanceFormatError(
                    "schema_drift", f"flag letters {unknown} are not documented"
                )
            flags = [f"{c}: {EUROSTAT_FLAGS[c]}" for c in flag_text if c.strip()]
            value = parse_decimal(text)
            if value is None:
                flags.append("missing-as-published")
            period = normalise_period(obs.period)
            if period is None:
                raise SurveillanceFormatError(
                    "schema_drift", f"period {obs.period!r} is not ISO"
                )
            values.append(
                {
                    "reference_period": period,
                    "reporting_date": None,
                    "value_text": text,
                    "value": value,
                    "lower": None,
                    "upper": None,
                    "flags": flags,
                    "attributes": attributes,
                    "locator": {"line": meta["row_lines"][obs.period]},
                }
            )
        if capacity is not None:
            condition = capacity
            indicator = capacity_indicator(
                document, f"{dataset}:{condition_code}", condition_dimension, condition_code
            )
        else:
            condition = {
                "scheme": "eurostat-icd10",
                "code": condition_code,
                "label": dict(document.get("condition_labels") or {}).get(
                    condition_code
                ),
            }
            indicator = dict(document["indicator"])
        series.append(
            _item(
                document,
                condition=condition,
                indicator=indicator,
                geography=_geography(
                    document,
                    geo,
                    system=region_system(geo, document, aggregate="eurostat-aggregate"),
                ),
                unit=_unit(document, unit_code),
                interval=frequencies[freq],
                kind=str(document.get("kind") or ""),
                dimensions=dims,
                values=values,
                citations=[
                    {"kind": "eurostat-dataset", "identifier": dataset},
                    *list(document.get("citations") or []),
                ],
                locator={"dataflow": meta.get("dataflow"), "dataset": dataset},
                denominator=document.get("denominator"),
                definition_code=condition_code,
            )
        )
    if len(updates) != 1:
        raise SurveillanceFormatError(
            "schema_drift", "the dataset answer states no single LAST UPDATE"
        )
    stamp = updates.pop()
    return {
        "series": series,
        "native_revision": f"{dataset}@{stamp}" if stamp else None,
        "published_on": stamp[:10]
        if stamp
        else normalise_period(document.get("published_on")),
        "published_at": stamp,
        "release_basis": "eurostat_last_update"
        if stamp
        else ("declared_publication" if document.get("published_on") else None),
        "structure": {
            "dataflow": records[0].metadata.get("dataflow"),
            "dataset": dataset,
            "sdmx_format": "SDMX-CSV 1.0",
        },
    }


# ---------------------------------------------------------------------- OECD Health Statistics through the SDMX connector

# SDMX CL_OBS_STATUS codes as the OECD publishes them (verify the code list against the live dataflow). ``A`` is a
# normal value; every other status is kept on the value as a flag, ``B`` (break) marks a series break.
OECD_OBS_STATUS = {
    "A": "normal value",
    "B": "break in time series",
    "D": "definition differs",
    "E": "estimated value",
    "F": "forecast value (as published by the provider)",
    "G": "experimental value",
    "I": "imputed value (as published by the provider)",
    "L": "missing value; data exist but were not collected",
    "M": "missing value; data cannot exist",
    "N": "not significant",
    "O": "missing value",
    "P": "provisional value",
    "Q": "missing value; suppressed",
    "S": "strike or other special circumstance",
    "U": "low reliability",
    "V": "unvalidated value",
}
# OECD area aggregates published beside countries; none is a country and none has a single boundary.
OECD_AGGREGATES = frozenset(
    {"OECD", "OECDE", "OECDAM", "OECDAO", "OECDSO", "EU27_2020", "EA20", "G7", "G20", "WLD"}
)
_OECD_FLOW = re.compile(r"^([A-Za-z0-9_.]+),([A-Za-z0-9_.@]+),([0-9]+(?:\.[0-9]+)*)$")
_OECD_SERVED_FLOW = re.compile(r"^([A-Za-z0-9_.]+):([A-Za-z0-9_.@]+)\(([0-9]+(?:\.[0-9]+)*)\)$")


def oecd_flow(document: Mapping[str, Any]) -> tuple[str, str, str]:
    """(agency, dataflow id, version) of a declared ``AGENCY,DSD@DATAFLOW,VERSION`` reference."""
    match = _OECD_FLOW.fullmatch(str(document.get("dataflow") or ""))
    if match is None:
        raise SurveillanceFormatError(
            "invalid_declaration",
            "OECD documents name a dataflow as AGENCY,DSD@DATAFLOW,VERSION",
        )
    return match.group(1), match.group(2), match.group(3)


def oecd_url(document: Mapping[str, Any]) -> tuple[str, dict[str, str]]:
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector

    oecd_flow(document)
    try:
        return SDMXConnector("OECD").csv_url(
            str(document["dataflow"]),
            str(document.get("key") or ""),
            dict(document.get("params") or {}),
        )
    except ValueError as exc:
        raise SurveillanceFormatError("invalid_declaration", str(exc)) from exc


def oecd_region_system(code: str, document: Mapping[str, Any]) -> str:
    """An OECD ``REF_AREA``: a declared or known OECD aggregate, or an ISO 3166-1 alpha-3 country; else refused."""
    code = str(code).strip().upper()
    declared = {str(k).upper() for k in dict(document.get("aggregate_codes") or {})}
    if code in declared or code in OECD_AGGREGATES or AGGREGATE_CODE.fullmatch(code):
        return "oecd-aggregate"
    if re.fullmatch(r"[A-Z]{3}", code):
        return "iso3166-1-alpha3"
    raise SurveillanceFormatError(
        "schema_drift",
        f"REF_AREA {code!r} is neither an ISO 3166-1 alpha-3 country nor a declared or recognised aggregate",
    )


def parse_oecd(
    raw: bytes,
    *,
    document: Mapping[str, Any],
    url: str = "",
    max_bytes: int = 20_000_000,
) -> dict[str, Any]:
    """OECD Health Statistics SDMX-CSV through the existing SDMX connector (health-system capacity, #2215).

    Keyed by dataflow, version, dimension key and period. ``OBS_STATUS`` and every other observation attribute are
    kept verbatim; the declared source note and per-country notes (the OECD's country-specific deviations) are kept
    verbatim on the indicator. A missing value stays missing: nothing is re-estimated. The dataflow version is part
    of the declared document, so a version change is a new release and a new vintage.
    """
    from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector
    from src.integrations.common import IntegrationError

    capacity = capacity_condition(document)
    if capacity is None:
        raise SurveillanceFormatError(
            "invalid_declaration",
            "OECD Health Statistics documents declare their capacity domain",
        )
    agency, flow_id, version = oecd_flow(document)
    flow = str(document["dataflow"])
    try:
        records = SDMXConnector(
            "OECD", max_bytes=max_bytes, max_observations=100000
        ).parse_csv(
            RawSeries(
                SeriesRef(flow, metadata={"flow": flow}), raw, source_url=url or None
            )
        )
    except IntegrationError as exc:
        raise SurveillanceFormatError("schema_drift", f"{exc.code}: {exc}") from exc
    if not records:
        raise SurveillanceFormatError(
            "schema_drift", "the dataflow answer has no series"
        )
    served = _OECD_SERVED_FLOW.fullmatch(str(records[0].metadata.get("dataflow") or ""))
    if served is None or served.groups() != (agency, flow_id, version):
        # A different dataflow or version would pose as the declared one.
        raise SurveillanceFormatError(
            "schema_drift", "the answer names another dataflow or version than declared"
        )
    names = {
        "measure": str(document.get("measure_dimension") or "MEASURE"),
        "geo": str(document.get("geo_dimension") or "REF_AREA"),
        "unit": str(document.get("unit_dimension") or "UNIT_MEASURE"),
        "freq": str(document.get("freq_dimension") or "FREQ"),
    }
    frequencies = {"A": "year", "Q": "quarter", "M": "month"}
    country_notes = {
        str(k).upper(): v for k, v in dict(document.get("country_notes") or {}).items()
    }
    series = []
    for record in records:
        meta = record.metadata
        dims = dict(meta["dimensions"])
        for required in names.values():
            if required not in dims:
                raise SurveillanceFormatError(
                    "schema_drift", f"the dataflow has no {required} dimension"
                )
        measure = dims.pop(names["measure"])
        geo, unit_code, freq = (
            dims.pop(names["geo"]),
            dims.pop(names["unit"]),
            dims.pop(names["freq"]),
        )
        if freq not in frequencies:
            raise SurveillanceFormatError(
                "schema_drift", f"frequency {freq!r} is not supported"
            )
        values = []
        for obs in record.observations:
            text = meta["original_values"][obs.period]
            attributes = dict(meta["observation_attributes"].get(obs.period) or {})
            status = str(attributes.get("OBS_STATUS", "") or "").strip()
            if status and status not in OECD_OBS_STATUS:
                raise SurveillanceFormatError(
                    "schema_drift", f"OBS_STATUS {status!r} is not a documented status"
                )
            flags = (
                [f"{status.lower()}: {OECD_OBS_STATUS[status]} (OBS_STATUS {status})"]
                if status and status != "A"
                else []
            )
            value = parse_decimal(text)
            if value is None:
                flags.append("missing-as-published")
            period = normalise_period(obs.period)
            if period is None:
                raise SurveillanceFormatError(
                    "schema_drift", f"period {obs.period!r} is not ISO"
                )
            values.append(
                {
                    "reference_period": period,
                    "reporting_date": None,
                    "value_text": text,
                    "value": value,
                    "lower": None,
                    "upper": None,
                    "flags": flags,
                    "attributes": attributes,
                    "locator": {"line": meta["row_lines"][obs.period]},
                }
            )
        indicator = capacity_indicator(
            document, f"{flow_id}:{measure}", names["measure"], measure
        )
        indicator["version"] = version
        if geo.upper() in country_notes:
            indicator["country_note"] = country_notes[geo.upper()]
        series.append(
            _item(
                document,
                condition=capacity,
                indicator=indicator,
                geography=_geography(
                    document, geo, system=oecd_region_system(geo, document)
                ),
                unit=_unit(document, unit_code),
                interval=frequencies[freq],
                kind=str(document.get("kind") or ""),
                dimensions=dims,
                values=values,
                citations=[
                    {"kind": "oecd-dataflow", "identifier": flow},
                    *list(document.get("citations") or []),
                ],
                locator={"dataflow": flow},
                denominator=document.get("denominator"),
                definition_code=measure,
            )
        )
    published = normalise_period(document.get("published_on"))
    return {
        "series": series,
        "native_revision": flow,
        "published_on": published,
        "published_at": None,
        "release_basis": "declared_publication" if published else None,
        "structure": {
            "dataflow": flow,
            "agency": agency,
            "dataflow_id": flow_id,
            "version": version,
            "sdmx_format": "SDMX-CSV 1.0 (OECD csvfile)",
        },
    }


# ---------------------------------------------------------------------- Destatis through the GENESIS connector


def parse_genesis(
    raw: bytes, metadata: bytes, *, document: Mapping[str, Any], url: str = ""
) -> dict[str, Any]:
    from src.ingestion.connectors.dataset.genesis import (
        GenesisConnector,
        GenesisFormatError,
    )

    condition_attribute = str(document.get("condition_attribute") or "")
    if not condition_attribute:
        raise SurveillanceFormatError(
            "invalid_declaration", "GENESIS documents name the cause-code Merkmal"
        )
    table = {
        "table": document.get("table"),
        "geography_attribute": document.get("geography_attribute"),
        "measures": dict(document.get("measures") or {}),
        "label": document.get("label"),
        "frequency": "annual",
    }
    try:
        records = GenesisConnector.parse_table(raw, metadata, table, source_url=url)
    except (GenesisFormatError, KeyError) as exc:
        raise SurveillanceFormatError("schema_drift", str(exc)) from exc
    series = []
    published_on = published_at = None
    for record in records:
        meta = record.metadata
        dims = dict(meta["dimensions"])
        if condition_attribute not in dims:
            raise SurveillanceFormatError(
                "schema_drift", f"a series has no {condition_attribute} cause code"
            )
        condition_code = dims.pop(condition_attribute)
        published_on, published_at = meta["published_on"], meta["published_at"]
        values = []
        for obs in record.observations:
            period = normalise_period(obs.period)
            if period is None:
                raise SurveillanceFormatError(
                    "schema_drift", f"period {obs.period!r} is not ISO"
                )
            text = meta["value_texts"].get(obs.period, "")
            sign = meta["signs"].get(obs.period)
            flags = []
            if sign:
                # Every GENESIS sign is an absent value, also '-' (published as "exactly zero"): its meaning is kept
                # as a flag and it is never compared as a number.
                flags.append(
                    f"genesis sign {sign['sign'] or 'empty'!r}: {sign['meaning']}"
                )
                value = None
            else:
                value = parse_decimal(text)
            values.append(
                {
                    "reference_period": period,
                    "reporting_date": None,
                    "value_text": text,
                    "value": value,
                    "lower": None,
                    "upper": None,
                    "flags": flags,
                    "locator": {"period": obs.period},
                }
            )
        series.append(
            _item(
                document,
                condition={
                    "scheme": "destatis-icd10",
                    "code": condition_code,
                    "label": dict(document.get("condition_labels") or {}).get(
                        condition_code
                    ),
                },
                indicator={"code": meta["value_code"], "label": meta["measure"]},
                geography=_geography(
                    document, record.geography, meta.get("geography_label")
                ),
                unit=_unit(document, meta["published_unit"]),
                interval="year",
                kind=str(document.get("kind") or ""),
                dimensions=dims,
                values=values,
                citations=[
                    {"kind": "genesis-table", "identifier": str(document["table"])},
                    *list(document.get("citations") or []),
                ],
                locator={"table": document["table"], "value_code": meta["value_code"]},
            )
        )
    return {
        "series": series,
        "native_revision": f"{document['table']}@{published_at}",
        "published_on": published_on,
        "published_at": published_at,
        "release_basis": "genesis_table_updated",
        "structure": {"table": document["table"], "measures": table["measures"]},
    }


# ---------------------------------------------------------------------- ECDC Atlas exports (operator-supplied)

ECDC_COLUMNS = (
    "HealthTopic",
    "Population",
    "Indicator",
    "Unit",
    "Time",
    "RegionCode",
    "RegionName",
    "NumValue",
    "TxtValue",
)


def region_system(code: str, document: Mapping[str, Any], *, aggregate: str) -> str:
    """The code system of a Eurostat or ECDC region code: an aggregate (declared, or by the aggregate pattern), a
    country, or a NUTS region (two letters followed by one to three alphanumerics); anything else is refused."""
    code = str(code).strip().upper()
    declared = {str(k).upper() for k in dict(document.get("aggregate_codes") or {})}
    if code in declared or AGGREGATE_CODE.fullmatch(code):
        return aggregate
    if COUNTRY_CODE.fullmatch(code):
        return "eu-country"
    if NUTS_CODE.fullmatch(code):
        return "nuts"
    raise SurveillanceFormatError(
        "schema_drift",
        f"region code {code!r} is neither a country, a NUTS code nor a declared or recognised aggregate",
    )


def _ecdc_system(code: str, document: Mapping[str, Any]) -> str:
    return region_system(code, document, aggregate="ecdc-aggregate")


def parse_ecdc_export(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    rows = _csv_rows(raw, str(document.get("delimiter") or ","))
    if not rows:
        raise SurveillanceFormatError("schema_drift", "the export has no rows")
    missing = [c for c in ECDC_COLUMNS if c not in rows[0]]
    if missing:
        raise SurveillanceFormatError(
            "schema_drift", f"export columns {missing} are missing"
        )
    extra = [c for c in rows[0] if c not in ECDC_COLUMNS]
    if extra:
        raise SurveillanceFormatError(
            "schema_drift", f"undeclared export columns {extra}"
        )
    interval = str(document.get("interval") or "year")
    groups: dict[tuple, list[dict[str, Any]]] = {}
    labels: dict[tuple, str | None] = {}
    for number, row in enumerate(rows, start=2):
        key = (
            row["HealthTopic"].strip(),
            row["Population"].strip(),
            row["Indicator"].strip(),
            row["Unit"].strip(),
            row["RegionCode"].strip(),
        )
        if not all(key):
            raise SurveillanceFormatError(
                "schema_drift",
                f"row {number} lacks a topic, population, indicator, unit or region",
            )
        period = normalise_period(row["Time"])
        if period is None:
            raise SurveillanceFormatError(
                "schema_drift", f"row {number} has a Time that is not ISO"
            )
        text = row["NumValue"].strip()
        value = parse_decimal(text)
        flags = [] if value is not None else ["missing-as-published"]
        if row["TxtValue"].strip() and row["TxtValue"].strip() != text:
            flags.append("text value: " + row["TxtValue"].strip()[:200])
        labels[key] = _clean(row["RegionName"])
        groups.setdefault(key, []).append(
            {
                "reference_period": period,
                "reporting_date": None,
                "value_text": text or row["TxtValue"].strip(),
                "value": value,
                "lower": None,
                "upper": None,
                "flags": flags,
                "locator": {"row": number},
            }
        )
    series = []
    for key, values in sorted(groups.items()):
        topic, population, indicator, unit, region = key
        series.append(
            _item(
                document,
                condition={
                    "scheme": "ecdc-health-topic",
                    "code": topic,
                    "label": topic,
                },
                indicator={
                    "code": indicator,
                    "label": indicator,
                    "population": population,
                },
                geography=_geography(
                    document, region, labels[key], system=_ecdc_system(region, document)
                ),
                unit=_unit(document, unit),
                interval=interval,
                kind=str(document.get("kind") or "observation"),
                dimensions={"population": population},
                values=values,
                citations=[
                    {"kind": "ecdc-indicator", "identifier": f"{topic}/{indicator}"},
                    *list(document.get("citations") or []),
                ],
                locator={"export": document.get("label")},
            )
        )
    extraction = normalise_period(document.get("extraction_date"))
    return {
        "series": series,
        "native_revision": f"ecdc-export@{extraction}",
        "published_on": extraction,
        "published_at": None,
        "release_basis": "declared_extraction" if extraction else None,
        "structure": {"columns": list(ECDC_COLUMNS)},
    }


# ---------------------------------------------------------------------- releases


def _last_modified(headers: Mapping[str, Any]) -> tuple[str | None, str | None]:
    value = headers.get("last-modified")
    if not value:
        return None, None
    try:
        stamp = parsedate_to_datetime(str(value))
    except (TypeError, ValueError):
        return None, None
    return stamp.date().isoformat(), stamp.astimezone(timezone.utc).replace(
        tzinfo=None
    ).isoformat(timespec="seconds")


def finish_release(
    format_id: str,
    release: dict[str, Any],
    raw: bytes,
    *,
    headers: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if release.get("published_on") is None:
        on, at = _last_modified(
            {str(k).casefold(): v for k, v in dict(headers or {}).items()}
        )
        if on:
            release.update(
                published_on=on, published_at=at, release_basis="http_last_modified"
            )
    if release.get("published_on") is None and release.get("fallback_release"):
        # GHO states no dataset release time; the latest value date is the only clock the source gives, labelled so.
        release.update(
            published_on=release["fallback_release"][:10],
            published_at=None,
            release_basis="latest_value_date",
        )
    if release.get("published_on") is None:
        # A retrieval time would pose as a publication; the response is refused instead.
        raise SurveillanceFormatError(
            "schema_drift", "the publication states no release date"
        )
    release.pop("fallback_release", None)
    release.update(
        contract=RELEASE_CONTRACT,
        provider=FORMATS[format_id]["provider"],
        format=format_id,
        jurisdiction=FORMATS[format_id]["jurisdiction"],
        file_sha256=hashlib.sha256(raw).hexdigest(),
        content_sha256=_digest(release["series"]),
        item_count=len(release["series"]),
    )
    return release


def document_url(
    document: Mapping[str, Any], endpoint: str, fmt: str, *, part: str = "data"
) -> str:
    """The declared URL of one document on the source's endpoint (no network access)."""
    if fmt == "rki-github-csv":
        return rki_url(document)
    if fmt == "who-gho-odata":
        return gho_urls(document, endpoint)[part]
    if fmt == "eurostat-sdmx-csv":
        url, query = eurostat_url(document)
        return url + "?" + urlencode(sorted(query.items()))
    if fmt == "oecd-sdmx-csv":
        url, query = oecd_url(document)
        return url + "?" + urlencode(sorted(query.items()))
    if fmt == "destatis-genesis-ffcsv":
        from src.ingestion.connectors.dataset.genesis import TABLE_PATTERN, table_urls

        if not TABLE_PATTERN.fullmatch(str(document.get("table") or "")):
            raise SurveillanceFormatError(
                "invalid_declaration", "GENESIS documents name a table code NNNNN-NNNN"
            )
        return table_urls(endpoint, str(document["table"]))[part]
    raise SurveillanceFormatError(
        "invalid_declaration", f"format {fmt!r} is not fetched"
    )


def surveillance_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("surveillance") or {})
    fmt = declared.get("format")
    if fmt not in FETCHED_FORMATS or FORMATS[fmt]["provider"] != declared.get(
        "provider"
    ):
        raise SourcePackError(
            "invalid_manifest",
            "surveillance sources declare a fetched provider and its format",
        )
    if not str(declared.get("namespace") or "").strip():
        raise SourcePackError(
            "invalid_manifest",
            "surveillance sources name the namespace their records belong to",
        )
    documents = list(declared.get("documents") or [])
    if not documents:
        raise SourcePackError(
            "invalid_manifest", "a surveillance source declares its documents"
        )
    if len(documents) > int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError(
            "invalid_manifest", "more declared documents than the source's page budget"
        )
    host = (urlsplit(source["endpoint"]).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[declared["provider"]]:
        raise SourcePackError(
            "invalid_manifest", "the endpoint is not the provider's documented host"
        )
    urls = []
    for document in documents:
        try:
            url = document_url(document, source["endpoint"], fmt)
            if document.get("unit") is not None:
                check_unit(document["unit"])
            for unit in dict(document.get("units") or {}).values():
                check_unit(unit)
            for revision in document.get("case_definitions") or []:
                check_case_definition(revision)
            if document.get("kind") not in KINDS:
                raise SurveillanceFormatError(
                    "missing_kind", "each document declares the kind of its values"
                )
        except SurveillanceFormatError as exc:
            raise SourcePackError("invalid_manifest", str(exc)) from exc
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError(
                "invalid_manifest",
                "declared documents are HTTPS resources on the endpoint's host",
            )
        urls.append(url)
    if len(set(urls)) != len(urls):
        raise SourcePackError(
            "invalid_manifest", "each declared document is a distinct publication"
        )
    return declared


def unverified(provider: str) -> bool:
    return (
        PROVIDER_CONTRACTS.get(provider, {}).get("access_decision") != "verified-live"
    )


# ---------------------------------------------------------------------- native connector


class SurveillanceAdapter:
    """Fetch the declared publications on the runtime's default transport; one page (one release) per document."""

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
        self.declared = surveillance_declaration(self.source)
        self.secret = secret
        if transport is None:
            from functools import partial

            # The runtime's default transport: same-host public redirects only, a byte ceiling and the timeout.
            transport = partial(
                HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"])
            )
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
            "surveillance": {
                "provider": self.declared["provider"],
                "format": self.declared["format"],
                "namespace": self.declared["namespace"],
                "keyed": bool(secret),
            },
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError(
                "operation_forbidden", "operation is not declared by the source"
            )
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError(
                "parameter_forbidden", "runtime adapter received undeclared controls"
            )
        if dict(request.get("parameters") or {}):
            raise SourcePackError(
                "parameter_forbidden",
                "surveillance runs fetch the declared documents only",
            )

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, text/csv, text/plain"}
        if self.declared["format"] == "destatis-genesis-ffcsv":
            if not self.secret:
                raise SourcePackError(
                    "authentication_failed", "this provider requires its credential"
                )
            # GENESIS token login: the token as user name, an empty password (verify).
            headers["username"] = self.secret
            headers["password"] = ""
        return headers

    def _get(
        self, url: str, headers: Mapping[str, str]
    ) -> tuple[bytes, dict[str, Any], str]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        if (urlsplit(url).hostname or "").casefold() != host or urlsplit(
            url
        ).scheme != "https":
            raise SourcePackError(
                "network_policy",
                "declared documents are fetched from the endpoint's host only",
            )
        base, _, query = url.partition("?")
        response = self.transport(
            url=base,
            params=dict(parse_qsl(query, keep_blank_values=True)),
            headers=dict(headers),
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        final_host = (
            urlsplit(str(response.get("final_url") or url)).hostname or ""
        ).casefold()
        if final_host != host:
            raise SourcePackError(
                "network_policy", "response was served from another host"
            )
        status = int(response.get("status", 200))
        response_headers = {
            str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()
        }
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError(
                "response_too_large", "response exceeds its byte limit"
            )
        if self.secret and len(self.secret) >= 8 and self.secret.encode() in raw:
            raise SourcePackError(
                "authentication_failed",
                "provider echoed the credential; response discarded",
            )
        if status == 429:
            raise SourcePackError(
                "rate_limited",
                "provider quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(response_headers.get("retry-after")),
            )
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed", f"request refused (HTTP {status})"
            )
        if status >= 500:
            raise SourcePackError(
                "source_unavailable", f"provider returned HTTP {status}"
            )
        if status >= 400:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        origin = "fixture" if response.get("origin") == "fixture" else "live"
        return raw, response_headers, origin

    def _gho_pages(
        self, url: str, headers: Mapping[str, str]
    ) -> tuple[list[bytes], dict[str, Any], list[str]]:
        pages, origins, response_headers = [], [], {}
        next_url: str | None = url
        while next_url:
            if len(pages) >= MAX_GHO_PAGES:
                raise SourcePackError(
                    "budget_exhausted",
                    "the indicator has more pages than the declared bound; "
                    "a truncated answer is never stored",
                )
            raw, response_headers, origin = self._get(next_url, headers)
            pages.append(raw)
            origins.append(origin)
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise SourcePackError(
                    "schema_drift", "GHO returned non-JSON content"
                ) from exc
            link = body.get("@odata.nextLink") if isinstance(body, Mapping) else None
            next_url = str(link) if link else None
        return pages, response_headers, origins

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        documents = list(self.declared["documents"])
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(documents):
            raise SourcePackError("cursor_drift", "cursor names no declared document")
        document = dict(documents[index])
        fmt = self.declared["format"]
        endpoint = self.source["endpoint"]
        headers = self._headers()
        origins: list[str] = []
        url = document_url(document, endpoint, fmt)
        try:
            if fmt == "who-gho-odata":
                metadata, _, origin = self._get(
                    document_url(document, endpoint, fmt, part="metadata"), headers
                )
                pages, response_headers, page_origins = self._gho_pages(url, headers)
                origins += [origin, *page_origins]
                raw = b"".join(pages)
                release = parse_gho(pages, metadata, document=document)
                raw_all = metadata + raw
            elif fmt == "destatis-genesis-ffcsv":
                metadata, _, origin = self._get(
                    document_url(document, endpoint, fmt, part="metadata"), headers
                )
                raw, response_headers, data_origin = self._get(url, headers)
                origins += [origin, data_origin]
                release = parse_genesis(raw, metadata, document=document, url=url)
                raw_all = metadata + raw
            else:
                raw, response_headers, origin = self._get(url, headers)
                origins.append(origin)
                if fmt == "rki-github-csv":
                    release = parse_rki(raw, document=document)
                elif fmt == "oecd-sdmx-csv":
                    release = parse_oecd(
                        raw,
                        document=document,
                        url=url,
                        max_bytes=int(self.definition["limits"]["max_bytes"]),
                    )
                else:
                    release = parse_eurostat(
                        raw,
                        document=document,
                        url=url,
                        max_bytes=int(self.definition["limits"]["max_bytes"]),
                    )
                raw_all = raw
            release = finish_release(fmt, release, raw_all, headers=response_headers)
        except SurveillanceFormatError as exc:
            raise SourcePackError(
                "response_too_large" if exc.code == "input_limit" else "schema_drift",
                f"{exc.code}: {exc}",
            ) from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if release["item_count"] > limit:
            # Never a truncated release: a missing series would read as a series without values.
            raise SourcePackError(
                "budget_exhausted",
                "release has more series than the run's result budget",
            )
        # Only a fixture transport says so; the runtime's HTTPS transport is live evidence.
        evidence_origin = "fixture" if set(origins) == {"fixture"} else "live"
        header = {
            "contract": RELEASE_CONTRACT,
            "provider": release["provider"],
            "format": release["format"],
            "jurisdiction": release["jurisdiction"],
            "document": document,
            "native_revision": release["native_revision"],
            "published_on": release["published_on"],
            "published_at": release.get("published_at"),
            "release_basis": release["release_basis"],
            "file_sha256": release["file_sha256"],
            "content_sha256": release["content_sha256"],
            "item_count": release["item_count"],
            "structure": release.get("structure") or {},
            "evidence_origin": evidence_origin,
            "url": url,
        }
        records = [
            {
                "id": f"{release['file_sha256'][:16]}:{number}",
                "title": f"{document.get('label') or release['provider']} ({release['published_on']})",
                "url": url,
                "language": "de" if release["jurisdiction"] == "DE" else "en",
                "published_at": release["published_on"],
                "content": json.dumps(item, sort_keys=True, ensure_ascii=False),
                "surveillance_release": header,
                "surveillance_series": item,
                "surveillance_namespace": self.declared["namespace"],
            }
            for number, item in enumerate(release["series"])
        ]
        receipt = {
            "status": 200,
            "provider": release["provider"],
            "document": document.get("label"),
            "published_on": release["published_on"],
            "release_basis": release["release_basis"],
            "file_sha256": release["file_sha256"],
            "items": len(records),
            "evidence_origin": evidence_origin,
            "execution": "network" if evidence_origin == "live" else "fixture",
            "final_page": index + 1 >= len(documents),
        }
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw_all), receipt=receipt)


FIXTURE_SECRET = "fixture-credential-not-a-real-key"
ADAPTERS = {CONNECTOR: SurveillanceAdapter}


def fixture_request(url: str) -> str:
    """The key :func:`fixture_transport` files a response under (path and sorted query)."""
    parts = urlsplit(url)
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return parts.path + ("?" + query if query else "")


def fixture_transport(
    pages: Sequence[Mapping[str, Any]],
) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path (+ sorted query); responses are marked as fixture evidence."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout, **_):
        del headers, timeout
        key = fixture_request(
            url
            + ("?" + urlencode(sorted(dict(params or {}).items())) if params else "")
        )
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = (
            body.encode()
            if isinstance(body, str)
            else b""
            if body is None
            else json.dumps(body).encode()
        )
        return {
            "status": int(page.get("status", 200)),
            "headers": dict(page.get("headers") or {}),
            "content": content,
            "origin": "fixture",
            **({"final_url": page["final_url"]} if page.get("final_url") else {}),
        }

    return transport


def replay_native_fixture(
    source: Mapping[str, Any], fixture: Mapping[str, Any]
) -> list[dict[str, Any]]:
    adapter = SurveillanceAdapter(
        source,
        transport=fixture_transport(list(fixture["native_pages"])),
        secret=FIXTURE_SECRET,
    )
    records, cursor = [], None
    while True:
        page = adapter.fetch_page(
            {
                "operation": min(source["operations"]),
                "parameters": {},
                "limit": int(source["budgets"]["max_results"]),
            },
            cursor=cursor,
        )
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


def day_ms(day: str) -> int:
    return int(
        datetime.combine(
            date.fromisoformat(day), datetime.min.time(), tzinfo=timezone.utc
        ).timestamp()
        * 1000
    )
