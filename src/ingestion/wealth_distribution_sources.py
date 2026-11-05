"""Wealth distribution sources for the Society bundle's ``society.wealth`` provider (#2882).

The machine-readable copy of ``docs/development/wealth-distribution-evidence/source-audit.md`` (WD01). One native
source-pack connector, ``wealth-distribution``, driven by :mod:`src.ingestion.source_pack_runtime` with the three
implemented sources of the ``society-wealth-distribution`` source pack
(``config/source_packs/society-wealth-distribution.json``). Each source declares one provider and a bounded list of
documents; the adapter fetches one declared document per page and returns one *release* (a header and one item per
series) that :class:`src.kb.wealth_distribution_store.WealthProjector` appends as series vintages.

* **World Inequality Database** (``wid-wealth-distribution``, format ``wid-api-json``) - top 1%, 0.1% and 0.01% net
  personal wealth shares and national and private wealth-to-income ratios through the WID API its own R and Stata
  tools call (JSON, ``x-api-key`` from the secret reference ``NOESIS_WID_API_KEY``). The variable code is split as WID
  defines it (type, concept, percentile, age, population). Years WID's metadata lists as extrapolated keep the
  publication status ``extrapolated``; WID's method statement is kept verbatim per series.
* **ECB Distributional Wealth Accounts** (``ecb-distributional-wealth-accounts``, format ``ecb-dwa-csv``) - quarterly
  household net wealth shares of the top decile and bottom half and median net wealth from the ECB Data Portal
  (``format=csvdata``). ``OBS_STATUS`` and ``OBS_CONF`` are kept verbatim; ``C`` confidential is a status, never a
  value. DWA are model-based experimental statistics and keep that method.
* **Federal Reserve Distributional Financial Accounts** (``fed-distributional-financial-accounts``, format
  ``dfa-zip-csv``) - quarterly US net worth shares by wealth percentile group read from one declared member of the DFA
  download archive (never another member). DFA periods ``YYYY:Qn`` are stored as ``YYYY-Qn`` with the published text
  kept as an attribute.

* **ECB HFCS statistical tables** (``ecb-hfcs-statistical-tables``, format ``hfcs-pdf-tables``) and **Bundesbank PHF
  aggregates** (``bundesbank-phf-aggregates``, format ``phf-pdf-tables``) - survey-wave aggregates that the publishers
  release only as PDF tables. A document declares the PDF and, per table, an anchor text that must occur exactly once,
  the column codes in printed order and the row labels it reads (:func:`read_pdf_tables`). Text comes from PyMuPDF
  word positions rebuilt into printed lines; a declared row must match exactly one line of its table section and hold
  exactly one number or declared marker per declared column. Anything short of that fails the document with the
  reason: no cell is guessed, re-ordered or transcribed by hand, and no undeclared cell is read. Each value keeps the
  printed token, the quoted line, the table anchor and the PDF page.

Every implemented provider is ``unverified-live`` until a dated live run; host, path, response shape, dimension and
column names marked _verify_ come from the publishers' documentation as the audit records it, not from a live
response. WID, DWA, DFA, HFCS and PHF figures are never blended, re-harmonised or recomputed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

CONNECTOR = "wealth-distribution"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-wealth-distribution-release-v1"
AUDIT = "docs/development/wealth-distribution-evidence/source-audit.md"
PACK_ID = "society-wealth-distribution"
FIXTURE_SECRET = "fixture-wid-key-not-a-real-key"
PROVIDERS = ("wid-wealth-distribution", "ecb-distributional-wealth-accounts", "fed-distributional-financial-accounts",
             "ecb-hfcs-statistical-tables", "bundesbank-phf-aggregates")
NOT_IMPLEMENTED: tuple[str, ...] = ()
FORMATS = {
    "wid-api-json": {"provider": "wid-wealth-distribution"},
    "ecb-dwa-csv": {"provider": "ecb-distributional-wealth-accounts"},
    "dfa-zip-csv": {"provider": "fed-distributional-financial-accounts"},
    "hfcs-pdf-tables": {"provider": "ecb-hfcs-statistical-tables"},
    "phf-pdf-tables": {"provider": "bundesbank-phf-aggregates"},
}
PDF_PROVIDERS = ("ecb-hfcs-statistical-tables", "bundesbank-phf-aggregates")
PROVIDER_HOSTS = {
    "wid-wealth-distribution": "rfap9nitz6.execute-api.eu-west-1.amazonaws.com",
    "ecb-distributional-wealth-accounts": "data-api.ecb.europa.eu",
    "fed-distributional-financial-accounts": "www.federalreserve.gov",
    "ecb-hfcs-statistical-tables": "www.ecb.europa.eu",
    "bundesbank-phf-aggregates": "www.bundesbank.de",
}
INDICATORS = ("wealth_share", "wealth_level", "wealth_average", "wealth_median", "wealth_income_ratio")
INDICATOR_MEANINGS = {
    "wealth_share": "the share of total wealth (as the source defines it) held by a wealth group",
    "wealth_level": "the total wealth held by a wealth group, in the published currency unit",
    "wealth_average": "average wealth within a group, as the source computes it",
    "wealth_median": "median wealth, as the source computes it",
    "wealth_income_ratio": "aggregate wealth as a ratio to national income, as the source defines both",
}
# Series key fields besides provider, dataset, place and frequency; each carries the code as published.
KEY_FIELDS = ("indicator", "wealth_concept", "group", "unit", "population", "method")
STATUSES = ("reported", "confidential", "not_published")
PUBLICATION_STATUSES = ("not-stated", "normal", "provisional", "estimated", "extrapolated", "projected")
AREA_SCHEMES = ("iso3166-1-alpha2",)
FREQUENCIES = ("annual", "quarterly", "irregular")  # irregular: survey waves (HFCS, PHF)
NEVER_SENTENCE = (
    "Published wealth distribution figures as each source released them: WID, ECB DWA and Fed DFA side by side with "
    "their own wealth concepts, groups, population units and methods, never blended or re-harmonised; model-based "
    "estimates keep the publisher's method and flags; no nowcast, no filled period, no share, ratio or average of "
    "our own."
)
EXCLUSIONS = (
    "nowcasting wealth figures",
    "filling or interpolating periods a source did not publish",
    "blending WID, DWA and DFA figures into one series",
    "re-harmonising wealth concepts, population units or percentile groups across sources",
    "top shares, Pareto fits, averages, ratios or other indicators of our own",
    "correcting or re-estimating a publisher's model-based estimates",
    "forecasts of our own",
    "person-level or household-level data (HFCS, PHF or SCF microdata, rich lists, named individuals or families)",
    ("reading an undeclared PDF table cell, transcribing figures by hand, or guessing a cell that cannot be read "
     "exactly (HFCS and PHF PDF tables)"),
)
CAPS = {
    "wid-wealth-distribution": {"documents": 1, "series_per_response": 40},
    "ecb-distributional-wealth-accounts": {"documents": 1, "series_per_response": 20},
    "fed-distributional-financial-accounts": {"documents": 1, "series_per_response": 20},
    "ecb-hfcs-statistical-tables": {"documents": 1, "series_per_response": 80, "tables": 6, "pages": 400},
    "bundesbank-phf-aggregates": {"documents": 1, "series_per_response": 40, "tables": 6, "pages": 120},
}
PACING = {
    provider: {"min_interval_s": 0, "basis": "no published quota (verify); one request per declared document"}
    for provider in PROVIDERS
}
BOUNDED_COVERAGE = {
    "wid-wealth-distribution": {
        "places": {"Germany": "DE", "France": "FR", "United States": "US"},
        "series": "shweal top 1%, 0.1% and 0.01% net personal wealth shares (992_j); wwealn and wwealp "
                  "wealth-to-income ratios (999_i)",
        "periods": "from a declared start year",
        "caps": "1 document, 40 series",
    },
    "ecb-distributional-wealth-accounts": {
        "places": {"Germany": "REF_AREA DE", "France": "REF_AREA FR"},
        "series": "net wealth share of D10 and B50; median net wealth",
        "periods": "from a declared start quarter",
        "caps": "1 document, 20 series",
    },
    "fed-distributional-financial-accounts": {
        "places": {"United States": "US"},
        "series": "net worth share of TopPt1, RemainingTop1, Next9, Next40 and Bottom50",
        "periods": "from a declared start quarter",
        "caps": "1 document, 20 series",
    },
    "ecb-hfcs-statistical-tables": {
        "places": {"Germany": "DE", "France": "FR", "Italy": "IT", "Spain": "ES"},
        "series": "declared rows of the net wealth table (median, mean, top 10% share) per declared country column",
        "periods": "one survey wave per declared table",
        "caps": "1 PDF, 6 tables, 400 pages, 80 series",
    },
    "bundesbank-phf-aggregates": {
        "places": {"Germany": "DE"},
        "series": "declared rows of the net wealth distribution table (mean, median, 90th percentile, top 10% share, "
                  "Gini) per declared survey-year column",
        "periods": "the survey waves the declared columns name",
        "caps": "1 PDF, 6 tables, 120 pages, 40 series",
    },
}
UNAVAILABLE_FALLBACK = (
    "A failed document (HTTP error, redirect to another host, schema drift, a missing archive member, a response "
    "larger than the budget) fails that source's run with its code and a receipt; earlier vintages stay current and "
    "nothing is marked removed or revised because of a failure. Readiness reports the source as stale."
)
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "wid-wealth-distribution": {
        "publisher": "World Inequality Lab, World Inequality Database (WID.world)",
        "delivers": "top 1%, 0.1% and 0.01% net personal wealth shares; national and private wealth-to-income ratios",
        "access": "api (the WID API WID's own R and Stata tools call: countries-variables, JSON, x-api-key header; "
                  "verify host, path, response shape and third-party use of the key); the bulk archive "
                  "wid_all_data.zip exceeds the runtime's 100 MB response budget (verify its size) and is not used",
        "entry_points": [
            ("https://rfap9nitz6.execute-api.eu-west-1.amazonaws.com/prod/countries-variables?countries={areas}"
             "&variables={variables} (verify)"),
        ],
        "authentication": "api key in the x-api-key header (secret reference NOESIS_WID_API_KEY)",
        "key_handling": "the key is resolved at run time from the secret reference; never stored, logged or committed",
        "licence": "WID.world terms of use; reuse with citation of WID.world (verify the licence)",
        "redistribution": "attribution-required (verify)",
        "rate_limits": "none published (verify); one request per declared document",
        "identifiers": "variable code (type letter, five-letter concept, percentile, age code, population code), "
                       "ISO 3166-1 alpha-2 country codes (verify)",
        "revision_model": "no update stamp in the response (verify): a changed response is a new release dated by the "
                          "declared WID update, else retrieval time (labelled retrieval_time); whole histories are "
                          "revised when DINA methods change; WID's method statement is a definition revision; years "
                          "WID lists as extrapolated keep publication status extrapolated (verify the metadata field)",
        "definitions": "variable codes as WID defines them (shweal net personal wealth share, wwealn net national "
                       "wealth to net national income, wwealp net private wealth to net national income; percentile "
                       "groups; 992_j adults 20+ equal-split, 999_i all individuals; verify); shares are fractions of "
                       "1 as published",
        "method": "model-based: distributional national accounts (DINA) combining tax, survey, rich-list and national "
                  "accounts data; kept with WID's per-series method statement",
        "personal_data": "none: published aggregates; rich lists and named individuals are never acquired",
        "unavailable_fallback": UNAVAILABLE_FALLBACK,
        "status": "unverified-live",
    },
    "ecb-distributional-wealth-accounts": {
        "publisher": "European Central Bank, Distributional Wealth Accounts (DWA, experimental statistics)",
        "delivers": "quarterly household net wealth by wealth group (top 10%, bottom 50%) and median net wealth",
        "access": "api (ECB Data Portal SDMX REST API, format=csvdata; dataflow DWA; key positions FREQ.REF_AREA."
                  "REF_SECTOR.ACCOUNTING_ENTRY.STO.INSTR_ASSET.DWA_GRP.UNIT_MEASURE.DWA_EST.DWA_COMP, dimension ids "
                  "are the author's labels, verify)",
        "entry_points": [
            "https://data-api.ecb.europa.eu/service/data/DWA/{key}?format=csvdata&startPeriod={quarter} (verify)",
        ],
        "authentication": "none",
        "key_handling": "no key; nothing secret is stored or sent",
        "licence": "ECB statistics reuse policy: reuse with source acknowledgement (verify)",
        "redistribution": "attribution-required (verify)",
        "rate_limits": "none published (verify); one request per declared document",
        "identifiers": "SDMX series key; REF_AREA as ISO 3166-1 alpha-2 (verify); DWA_GRP wealth groups (D10, B50, "
                       "_Z; verify)",
        "revision_model": "quarterly releases revise earlier quarters; no update stamp in the CSV (verify): declared "
                          "release date, else retrieval time; OBS_STATUS (A normal, E estimated, P provisional; "
                          "verify) and OBS_CONF (C confidential, a status never a value) kept verbatim; a series a "
                          "later complete release no longer states becomes a removed_by_source vintage",
        "definitions": "instrument NWA net wealth, wealth group, unit as published (share of total net wealth, euro "
                       "median), households (S14) as the unit of analysis",
        "method": "model-based: HFCS distributions aligned to household-sector national accounts (experimental "
                  "statistics)",
        "personal_data": "none: published aggregates; HFCS microdata are excluded",
        "unavailable_fallback": UNAVAILABLE_FALLBACK,
        "status": "unverified-live",
    },
    "fed-distributional-financial-accounts": {
        "publisher": "Board of Governors of the Federal Reserve System, Distributional Financial Accounts (DFA)",
        "delivers": "quarterly US household net worth shares of the top 0.1%, the rest of the top 1%, the next 9%, "
                    "the next 40% and the bottom 50%",
        "access": "download (the DFA archive dfa.zip, one declared member dfa-networth-shares.csv; verify path, member "
                  "name, columns and category codes)",
        "entry_points": ["https://www.federalreserve.gov/releases/z1/dataviz/download/zips/dfa.zip (verify)"],
        "authentication": "none",
        "key_handling": "no key",
        "licence": "US federal government work; the Board's website usage policy asks for attribution (verify)",
        "redistribution": "attribution-required (verify)",
        "rate_limits": "none published (verify); one request per declared document",
        "identifiers": "Date (YYYY:Qn), Category (TopPt1, RemainingTop1, Next9, Next40, Bottom50; verify), balance "
                       "sheet columns",
        "revision_model": "released with each quarterly Z.1 release; every release can revise the whole history; the "
                          "declared Z.1 release date dates the vintage, else retrieval time",
        "definitions": "the balance-sheet column (Net worth), the wealth percentile group as published, percent share "
                       "of the aggregate, US households as the unit of analysis",
        "method": "model-based: the triennial Survey of Consumer Finances distributed onto the Financial Accounts "
                  "(Z.1), interpolated between survey years",
        "personal_data": "none: published aggregates; SCF microdata are excluded",
        "unavailable_fallback": UNAVAILABLE_FALLBACK,
        "status": "unverified-live",
    },
    "ecb-hfcs-statistical-tables": {
        "publisher": "European Central Bank, Household Finance and Consumption Survey (HFCS)",
        "delivers": "per-wave aggregate tables: median and mean household net wealth and wealth shares by country",
        "access": "download (the HFCS Statistical Tables PDF per wave and data version, e.g. "
                  "HFCS_Statistical_Tables_Wave_2021_June_2026.pdf; no machine-readable aggregate endpoint known); "
                  "declared tables, rows and country columns only (verify table numbers, titles and row labels)",
        "entry_points": [("https://www.ecb.europa.eu/home/pdf/research/hfcn/"
                          "HFCS_Statistical_Tables_Wave_2021_June_2026.pdf (verify)")],
        "authentication": "none",
        "key_handling": "no key",
        "licence": "ECB copyright; reproduction permitted with the source acknowledged (verify)",
        "redistribution": "attribution-required (verify)",
        "rate_limits": "none published (verify); one request per declared document",
        "identifiers": "table number and title as printed, row label as printed, column country code as printed "
                       "(EA and ISO 3166-1 alpha-2; verify)",
        "revision_model": "each data version of a wave (e.g. July 2023 v3.3, June 2026 v4.1) is a new PDF and a new "
                          "release dated by the declared publication date; a figure that changes between versions is "
                          "a new vintage, never an overwrite",
        "definitions": "household net wealth as the HFCS defines it (real and financial assets minus liabilities), "
                       "households as the unit of analysis, survey reference year per country (verify per wave)",
        "method": "survey (HFCS, multiply imputed, weighted aggregates as published)",
        "personal_data": "none: published aggregates; HFCS microdata are excluded",
        "unavailable_fallback": UNAVAILABLE_FALLBACK + " A declared cell that cannot be read exactly (anchor not "
                                "unique, row missing or repeated, column count differs, an undeclared marker) fails "
                                "the document with the reason; nothing is guessed.",
        "status": "unverified-live",
    },
    "bundesbank-phf-aggregates": {
        "publisher": "Deutsche Bundesbank, Panel on Household Finances (PHF) / household wealth survey",
        "delivers": "German household net wealth distribution per survey wave: mean, median, percentiles, top 10% "
                    "share and Gini coefficient",
        "access": "download (the data annex PDF of the Monthly Report article on each wave, e.g. "
                  "2023-04-vermoegensbefragung-data.pdf); declared tables, rows and survey-year columns only (verify "
                  "path, table titles, row labels and decimal separator)",
        "entry_points": [("https://www.bundesbank.de/resource/blob/908924/3ef9d9a4eaeae8a8779ccec3ac464970/"
                          "472B63F073F071307366337C94F8C870/2023-04-vermoegensbefragung-data.pdf (verify)")],
        "authentication": "none",
        "key_handling": "no key",
        "licence": "Deutsche Bundesbank publications: reproduction permitted provided the source is stated (verify)",
        "redistribution": "attribution-required (verify)",
        "rate_limits": "none published (verify); one request per declared document",
        "identifiers": "table title as printed, row label as printed, survey-year column as printed (verify)",
        "revision_model": "each Monthly Report data annex is a release dated by its declared publication date; a "
                          "later annex that restates an earlier wave is a new vintage, never an overwrite",
        "definitions": "household net wealth as the PHF defines it, households in Germany as the unit of analysis, "
                       "survey year as printed (verify)",
        "method": "survey (PHF, multiply imputed, weighted aggregates as published)",
        "personal_data": "none: published aggregates; PHF microdata (Research Data and Service Centre, scientific use "
                         "only) are excluded",
        "unavailable_fallback": UNAVAILABLE_FALLBACK + " A declared cell that cannot be read exactly fails the "
                                "document with the reason; nothing is guessed.",
        "status": "unverified-live",
    },
}
LIVE_VERIFICATION = {
    provider: {"status": "unverified-live", "checked": None, "evidence": None,
               "note": "no dated live run yet; offline fixtures only"} for provider in PROVIDERS
}
FLAG_ATTRIBUTES = ("OBS_STATUS", "OBS_CONF")
CONFIDENTIAL_FLAGS = {"C"}
FLAG_MEANINGS = {"A": "normal value", "E": "estimated value", "P": "provisional value", "F": "forecast value",
                 "C": "confidential", "F_CONF": "free", "extrapolated": "extrapolated by the source"}
# Source-stated publication status per flag (the source's own status, never ours).
FLAG_STATUS = {"A": "normal", "E": "estimated", "P": "provisional", "F": "projected"}


class WealthFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def unverified(provider: str) -> bool:
    return LIVE_VERIFICATION.get(provider, {}).get("status") != "verified-live"


def decimal_text(value: Any) -> str | None:
    """The published number as text (no rounding, no rescaling); ``None`` for a missing cell."""
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", ":", "NaN", "nan", "None", "null", "NA"}:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return format(number.normalize(), "f") if number == number.to_integral() else str(number)


# ------------------------------------------------------------------ declaration


def wealth_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    config = dict(source.get("wealth") or {})
    provider, fmt = config.get("provider"), config.get("format")
    if provider not in PROVIDERS or FORMATS.get(str(fmt), {}).get("provider") != provider:
        raise SourcePackError("invalid_source", "wealth-distribution sources declare a known provider and its format")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host != PROVIDER_HOSTS[provider]:
        raise SourcePackError("unsafe_endpoint", f"{provider} documents are fetched from {PROVIDER_HOSTS[provider]}")
    documents = [dict(d) for d in config.get("documents") or []]
    if not 1 <= len(documents) <= CAPS[provider]["documents"]:
        raise SourcePackError("unbounded_source", f"declare 1-{CAPS[provider]['documents']} documents")
    for document in documents:
        try:
            check_document(provider, document)
        except WealthFormatError as exc:
            raise SourcePackError("invalid_source", f"{document.get('label')}: {exc}") from exc
    return {"provider": provider, "format": fmt, "namespace": config.get("namespace") or "global",
            "documents": documents, "live_verification": config.get("live_verification") or "unverified-live"}


def check_document(provider: str, document: Mapping[str, Any]) -> None:
    for key in ("label", "dataset", "key_fields", "area", "frequency", "definition", "references"):
        if not document.get(key):
            raise WealthFormatError("invalid_document", f"a document states its {key}")
    if document["frequency"] not in FREQUENCIES:
        raise WealthFormatError("invalid_document", f"frequency is one of {FREQUENCIES}")
    fields = dict(document["key_fields"])
    if set(fields) != set(KEY_FIELDS):
        raise WealthFormatError("invalid_document", f"key_fields declare exactly {KEY_FIELDS}")
    for name, spec in fields.items():
        spec = dict(spec)
        if bool(spec.get("dimension")) == bool(spec.get("fixed")):
            raise WealthFormatError("invalid_document", f"{name} names a dimension or a fixed value")
        if spec.get("dimension") and not spec.get("codes"):
            raise WealthFormatError("invalid_document", f"{name} lists the codes it reads")
        values = [spec["fixed"]] if spec.get("fixed") else list(dict(spec["codes"]).values())
        if name == "indicator" and any(not isinstance(v, Mapping) or v.get("concept") not in INDICATORS
                                       for v in values):
            raise WealthFormatError("invalid_document", f"indicator concepts are one of {INDICATORS}")
    area = dict(document["area"])
    if area.get("scheme") not in AREA_SCHEMES or bool(area.get("dimension")) == bool(area.get("fixed")):
        raise WealthFormatError("invalid_document", "the area names its scheme and a dimension or a fixed code")
    if provider == "wid-wealth-distribution":
        if not document.get("areas") or not document.get("variables"):
            raise WealthFormatError("unbounded_document", "WID documents name their countries and variables")
        if len(document["areas"]) * len(document["variables"]) > CAPS[provider]["series_per_response"]:
            raise WealthFormatError("unbounded_document", "WID documents stay within the series cap")
        for variable in document["variables"]:
            if not re.fullmatch(r"[a-z]{6}_p[0-9.]+p[0-9.]+_[0-9]{3}_[a-z]", str(variable)):
                raise WealthFormatError("invalid_document", f"not a WID variable code: {variable!r}")
    elif provider in PDF_PROVIDERS:
        check_pdf_document(provider, document)
    elif provider == "ecb-distributional-wealth-accounts":
        if not document.get("key") or "*" in str(document["key"]) or not document.get("dimension_columns"):
            raise WealthFormatError("unbounded_document", "DWA documents name their series key and dimension columns")
        if any(c in str(document["key"]) for c in "/?#& "):
            raise WealthFormatError("invalid_document", "DWA series keys use codes separated by '.' and '+'")
        if not dict(document.get("params") or {}).get("startPeriod"):
            raise WealthFormatError("unbounded_document", "documents pin a start period")
    else:
        if not document.get("archive_path") or not document.get("member") or not document.get("value_columns"):
            raise WealthFormatError("unbounded_document", "DFA documents name the archive, one member and the value "
                                                          "columns they read")
        if "/" in str(document["member"]) or "\\" in str(document["member"]):
            raise WealthFormatError("invalid_document", "the archive member is a bare file name")
        if not document.get("start_period"):
            raise WealthFormatError("unbounded_document", "documents pin a start period")


PDF_MARKER_STATUSES = ("not_published", "confidential")
PDF_SEPARATORS = {"decimal": (".", ","), "thousands": (None, ",", ".", "'")}
DEFAULT_HEADING = r"^(Table|Tabelle)\s+[A-Z]?\d+(\.\d+)*\b"


def check_pdf_document(provider: str, document: Mapping[str, Any]) -> None:
    """A PDF document names its file and, per table, the anchor, columns and rows it reads (nothing else is read)."""
    path = str(document.get("pdf_path") or "")
    if not path or not path.lower().endswith(".pdf") or any(c in path for c in "?#\\ ") or ".." in path:
        raise WealthFormatError("invalid_document", "PDF documents name a bare .pdf path on the provider's host")
    tables = list(document.get("tables") or [])
    if not 1 <= len(tables) <= CAPS[provider]["tables"]:
        raise WealthFormatError("unbounded_document", f"declare 1-{CAPS[provider]['tables']} tables")
    cells = 0
    for table in tables:
        table = dict(table)
        by_period = bool(table.get("columns_are_periods"))
        for key in ("anchor", "columns", "rows", *(() if by_period else ("period", "column_dimension"))):
            if not table.get(key):
                raise WealthFormatError("invalid_document", f"a PDF table states its {key}")
        columns = [str(c) for c in table["columns"]]
        if len(set(columns)) != len(columns):
            raise WealthFormatError("invalid_document", "a PDF table lists each printed column once, in order")
        periods = columns if by_period else [str(table["period"])]
        if not all(re.fullmatch(r"\d{4}", p) for p in periods):
            raise WealthFormatError("invalid_document", "a PDF table's periods are the survey years it reports")
        read = [str(c) for c in table.get("read_columns") or columns]
        if not set(read) <= set(columns) or len(set(read)) != len(read):
            raise WealthFormatError("invalid_document", "read_columns are printed columns, each once")
        labels = [str(dict(r).get("label") or "") for r in table["rows"]]
        if not all(labels) or len(set(labels)) != len(labels):
            raise WealthFormatError("invalid_document", "PDF rows state distinct labels as printed")
        for row in table["rows"]:
            if not by_period and table["column_dimension"] in dict(dict(row).get("dims") or {}):
                raise WealthFormatError("invalid_document", "a row's dimensions never repeat the column dimension")
        if table.get("decimal", ".") not in PDF_SEPARATORS["decimal"]:
            raise WealthFormatError("invalid_document", f"decimal separator is one of {PDF_SEPARATORS['decimal']}")
        if table.get("thousands") not in PDF_SEPARATORS["thousands"] or table.get("thousands") == table.get(
                "decimal", "."):
            raise WealthFormatError("invalid_document", "thousands separator is none, ',', '.' or \"'\" and differs "
                                                        "from the decimal separator")
        if any(status not in PDF_MARKER_STATUSES for status in dict(table.get("markers") or {}).values()):
            raise WealthFormatError("invalid_document", f"markers map to {PDF_MARKER_STATUSES}")
        pages = [int(p) for p in table.get("pages") or []]
        if any(p < 1 or p > CAPS[provider]["pages"] for p in pages):
            raise WealthFormatError("invalid_document", "declared pages are 1-based and within the page cap")
        if not 1 <= int(table.get("max_pages") or 1) <= 3:
            raise WealthFormatError("invalid_document", "a PDF table section spans 1-3 pages")
        cells += len(read) * len(labels)
    if cells > CAPS[provider]["series_per_response"]:
        raise WealthFormatError("unbounded_document", "declared PDF cells stay within the series cap")


def document_key(provider: str, document: Mapping[str, Any]) -> str:
    """Identity of a declared document across releases (removal detection compares releases of one document)."""
    fields = ("key", "areas", "variables", "archive_path", "member", "value_columns")
    if provider in PDF_PROVIDERS:  # PDF documents only; the other providers' keys stay as first released
        fields += ("pdf_path", "tables")
    return f"{provider}:{document['dataset']}:" + digest({k: document.get(k) for k in fields})[:16]


def document_url(provider: str, document: Mapping[str, Any], endpoint: str) -> str:
    base = endpoint.rstrip("/")
    if provider == "wid-wealth-distribution":
        query = {"countries": ",".join(document["areas"]), "variables": ",".join(document["variables"])}
        return f"{base}/countries-variables?" + urlencode(sorted(query.items()))
    if provider == "ecb-distributional-wealth-accounts":
        query = {"format": "csvdata", **{k: str(v) for k, v in dict(document.get("params") or {}).items()}}
        return f"{base}/{document['dataset']}/{document['key']}?" + urlencode(sorted(query.items()))
    if provider in PDF_PROVIDERS:
        return f"{base}/{str(document['pdf_path']).lstrip('/')}"
    return f"{base}/{str(document['archive_path']).lstrip('/')}"


# ------------------------------------------------------------------ parsing


def series_key(item: Mapping[str, Any]) -> dict[str, Any]:
    """The fields that make a series: never a release, so each release of the same key is a vintage."""
    return {
        "provider": item["provider"],
        "dataset": item["dataset"],
        **{field: dict(item[field]).get("code") for field in KEY_FIELDS},
        "area": {"scheme": item["area"]["scheme"], "code": item["area"]["code"]},
        "frequency": item["frequency"],
    }


def _field(name: str, spec: Mapping[str, Any], dims: Mapping[str, str]) -> dict[str, Any]:
    if spec.get("fixed"):
        return dict(spec["fixed"])
    code = dims.get(spec["dimension"])
    if code is None:
        raise WealthFormatError("schema_drift", f"the response lacks the {spec['dimension']} dimension")
    known = dict(spec["codes"]).get(code)
    if known is None:
        raise WealthFormatError("schema_drift", f"unmapped {name} code {code!r}")
    return {"code": code, **({"label": known} if isinstance(known, str) else dict(known))}


def _years(text: Any) -> set[str]:
    """Years named by a WID metadata range list such as ``[[2093, 2094]]`` or ``2093-2094`` (verify the field)."""
    years: set[str] = set()
    for start, end in re.findall(r"(\d{4})(?:\s*[,-]\s*(\d{4}))?", str(text or "")):
        years.update(str(y) for y in range(int(start), int(end or start) + 1))
    return years


def _rows_wid(raw: bytes, document: Mapping[str, Any]) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw.decode("utf-8"), parse_float=str, parse_int=str)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WealthFormatError("schema_drift", "the WID response is not JSON") from exc
    if not isinstance(payload, Mapping):
        raise WealthFormatError("schema_drift", "the WID response is not an object keyed by variable")
    rows = []
    for variable, entries in payload.items():
        if variable not in document["variables"]:
            raise WealthFormatError("schema_drift", f"the response states an undeclared variable {variable!r}")
        code, percentile, age, pop = str(variable).split("_")
        for entry in entries if isinstance(entries, list) else [entries]:
            for country, body in dict(entry).items():
                body = dict(body or {})
                meta = {str(k): v for k, v in dict(body.get("meta") or {}).items()}
                extrapolated = _years(meta.get("extrapolation"))
                dims = {"variable": code, "type": code[0], "concept": code[1:], "percentile": percentile,
                        "age": age, "pop": pop, "pop_age": f"{age}_{pop}", "country": str(country)}
                for value in body.get("values") or []:
                    period = str(dict(value).get("y") or "").strip()
                    attributes = {}
                    if period in extrapolated:
                        attributes["extrapolation"] = str(meta.get("extrapolation"))
                    rows.append({"dims": dims, "native": f"{country}.{variable}", "period": period,
                                 "value_text": dict(value).get("v"), "attributes": attributes, "meta": meta,
                                 "stated_status": "extrapolated" if period in extrapolated else None})
    return rows


def _csv_rows(text: str, *, delimiter: str = ",") -> tuple[list[str], list[list[str]]]:
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = [c.strip() for c in next(reader, None) or []]
    if not header or len(set(header)) != len(header):
        raise WealthFormatError("schema_drift", "the CSV has no header or repeats a column")
    rows = []
    for line, row in enumerate(reader, start=2):
        if not row or not any(cell.strip() for cell in row):
            continue
        if len(row) != len(header):
            raise WealthFormatError("schema_drift", f"CSV row {line} has {len(row)} cells, not {len(header)}")
        rows.append([cell.strip() for cell in row])
    return header, rows


def _rows_dwa(raw: bytes, document: Mapping[str, Any]) -> list[dict[str, Any]]:
    try:
        header, body = _csv_rows(raw.decode("utf-8-sig"))
    except UnicodeDecodeError as exc:
        raise WealthFormatError("schema_drift", "the DWA response is not UTF-8") from exc
    dimension_columns = list(document["dimension_columns"])
    missing = [c for c in (*dimension_columns, "TIME_PERIOD", "OBS_VALUE") if c not in header]
    if missing:
        raise WealthFormatError("schema_drift", f"the DWA response lacks the columns {missing}")
    index = {c: i for i, c in enumerate(header)}
    attribute_columns = [c for c in header if c not in {*dimension_columns, "TIME_PERIOD", "OBS_VALUE", "KEY"}]
    rows = []
    for row in body:
        attributes = {c: row[index[c]] for c in attribute_columns if row[index[c]]}
        rows.append({"dims": {c: row[index[c]] for c in dimension_columns}, "period": row[index["TIME_PERIOD"]],
                     "value_text": row[index["OBS_VALUE"]] or None, "attributes": attributes, "meta": {},
                     "stated_status": None})
    return rows


def _rows_dfa(raw: bytes, document: Mapping[str, Any], *, max_bytes: int) -> list[dict[str, Any]]:
    member = str(document["member"])
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise WealthFormatError("schema_drift", "the DFA download is not a zip archive") from exc
    with archive:
        info = next((i for i in archive.infolist() if i.filename == member), None)
        if info is None:
            raise WealthFormatError("schema_drift", f"the archive has no member {member!r}")
        if info.file_size > max_bytes:
            raise WealthFormatError("budget_exhausted", "the archive member exceeds the byte budget")
        try:
            text = archive.read(info).decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise WealthFormatError("schema_drift", "the archive member is not UTF-8") from exc
    header, body = _csv_rows(text)
    columns = list(document["value_columns"])
    missing = [c for c in ("Date", "Category", *columns) if c not in header]
    if missing:
        raise WealthFormatError("schema_drift", f"the DFA member lacks the columns {missing}")
    index = {c: i for i, c in enumerate(header)}
    start = str(document["start_period"])
    rows = []
    for row in body:
        published = row[index["Date"]]
        match = re.fullmatch(r"(\d{4}):Q([1-4])", published)
        if not match:
            raise WealthFormatError("schema_drift", f"unexpected DFA date {published!r}")
        period = f"{match.group(1)}-Q{match.group(2)}"
        if period < start:
            continue
        for column in columns:
            rows.append({"dims": {"Category": row[index["Category"]], "column": column}, "period": period,
                         "value_text": row[index[column]] or None, "attributes": {"period_as_published": published},
                         "meta": {}, "stated_status": None})
    return rows


def _norm(text: Any) -> str:
    """Whitespace-normalised printed text (no case folding: labels are matched as printed)."""
    return " ".join(str(text or "").replace("\u00a0", " ").replace("\u2212", "-").replace("\u2013", "-").split())


def pdf_lines(raw: bytes, *, max_pages: int) -> list[list[str]]:
    """Printed lines per page, rebuilt from PyMuPDF word positions (words sharing a baseline, left to right)."""
    if not raw.startswith(b"%PDF"):
        raise WealthFormatError("schema_drift", "the document is not a PDF")
    try:  # PyMuPDF, the optional-sources extra; its legacy ``fitz`` name prints a warning on stdout from 1.24
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz
        except ImportError as exc:
            raise WealthFormatError("dependency_missing", "reading PDF tables needs PyMuPDF (the optional-sources "
                                                          "extra); no figure is read without it") from exc
    try:
        document = fitz.open(stream=raw, filetype="pdf")
    except Exception as exc:  # any unreadable PDF is schema drift, never a partial read
        raise WealthFormatError("schema_drift", "the PDF cannot be opened") from exc
    with document:
        if document.needs_pass:
            raise WealthFormatError("schema_drift", "the PDF is encrypted")
        if document.page_count > max_pages:
            raise WealthFormatError("budget_exhausted", f"the PDF has {document.page_count} pages, more than the "
                                                        f"cap of {max_pages}")
        pages = []
        for page in document:
            lines: list[dict[str, Any]] = []
            for x0, y0, x1, y1, word, *_ in sorted(page.get_text("words"), key=lambda w: ((w[1] + w[3]) / 2, w[0])):
                middle = (y0 + y1) / 2
                if lines and abs(lines[-1]["middle"] - middle) <= max(1.5, (y1 - y0) * 0.35):
                    lines[-1]["words"].append((x0, word))
                else:
                    lines.append({"middle": middle, "words": [(x0, word)]})
            pages.append([_norm(" ".join(w for _, w in sorted(line["words"]))) for line in lines])
    return pages


def _section(pages: Sequence[Sequence[str]], table: Mapping[str, Any]) -> list[tuple[int, str]]:
    """The printed lines of one declared table: after its unique anchor, up to its end anchor or the next heading."""
    anchor = _norm(table["anchor"])
    allowed = {int(p) for p in table.get("pages") or []}
    hits = [(number, index) for number, lines in enumerate(pages, start=1) for index, line in enumerate(lines)
            if anchor in line and (not allowed or number in allowed)]
    if not hits:
        raise WealthFormatError("schema_drift", f"table anchor {anchor!r} not found")
    if len(hits) > 1:
        raise WealthFormatError("schema_drift", f"table anchor {anchor!r} appears {len(hits)} times (pages "
                                                f"{sorted({p for p, _ in hits})}); not read")
    first, index = hits[0]
    heading = re.compile(str(table.get("heading_pattern") or DEFAULT_HEADING))
    end = _norm(table["end_anchor"]) if table.get("end_anchor") else None
    lines: list[tuple[int, str]] = []
    for number in range(first, min(len(pages), first + int(table.get("max_pages") or 1) - 1) + 1):
        for line in pages[number - 1][index + 1 if number == first else 0:]:
            if (end and end in line) or heading.match(line):
                return lines
            lines.append((number, line))
    return lines


_NUMBER = re.compile(r"-?\d[\d.,']*")


def read_cell(token: str, table: Mapping[str, Any]) -> dict[str, Any] | None:
    """One printed cell: a number in the table's declared notation, or a declared marker; ``None`` if it is neither."""
    markers = {str(k): str(v) for k, v in dict(table.get("markers") or {}).items()}
    if token in markers:
        return {"status": markers[token], "value": None, "printed": token}
    body, flag = token, None
    for suffix in sorted(dict(table.get("flag_suffixes") or {}), key=len, reverse=True):
        if body.endswith(suffix) and len(body) > len(suffix):
            body, flag = body[: -len(suffix)], suffix
            break
    body = body.removesuffix("%")
    if not _NUMBER.fullmatch(body):
        return None
    decimal, thousands = str(table.get("decimal") or "."), table.get("thousands")
    unsigned = body.lstrip("-")
    if thousands:
        whole = unsigned.split(decimal)[0]
        groups = whole.split(str(thousands))
        if len(groups) > 1 and (not groups[0] or len(groups[0]) > 3 or any(len(g) != 3 for g in groups[1:])):
            return None
        body = body.replace(str(thousands), "")
    if body.count(decimal) > 1 or any(sep in body for sep in {".", ",", "'"} - {decimal}):
        return None
    value = decimal_text(body.replace(decimal, ".") if decimal != "." else body)
    if value is None:
        return None
    return {"status": "reported", "value": value, "printed": token, "flag": flag}


def read_pdf_tables(raw: bytes, document: Mapping[str, Any], *, max_pages: int) -> list[dict[str, Any]]:
    """The declared cells of the declared tables; any cell short of one exact reading fails the document."""
    pages = pdf_lines(raw, max_pages=max_pages)
    rows = []
    for table in document["tables"]:
        table = dict(table)
        lines = _section(pages, table)
        columns = [str(c) for c in table["columns"]]
        read = [str(c) for c in table.get("read_columns") or columns]
        by_period = bool(table.get("columns_are_periods"))
        flags = {str(k): str(v) for k, v in dict(table.get("flag_suffixes") or {}).items()}
        for row in table["rows"]:
            label = _norm(dict(row)["label"])
            matches = []
            for number, line in lines:
                if not line.startswith(label + " "):
                    continue
                tokens = line[len(label) + 1:].split()
                cells = [read_cell(t, table) for t in tokens]
                if tokens and all(c is not None for c in cells):
                    matches.append((number, line, cells))
            if not matches:
                raise WealthFormatError("schema_drift", f"row {label!r} not found in table {table['anchor']!r}")
            if len(matches) > 1:
                raise WealthFormatError("schema_drift", f"row {label!r} appears {len(matches)} times in table "
                                                        f"{table['anchor']!r}; not read")
            number, line, cells = matches[0]
            if len(cells) != len(columns):
                raise WealthFormatError("schema_drift", f"row {label!r} holds {len(cells)} cells for {len(columns)} "
                                                        f"declared columns in table {table['anchor']!r}; not read")
            for column, cell in zip(columns, cells, strict=True):
                if column not in read:
                    continue
                attributes = {"pdf_page": str(number), "pdf_table": _norm(table["anchor"]), "row_label": label,
                              "printed": cell["printed"], "quoted_line": line[:500]}
                if cell["status"] == "confidential":
                    attributes["OBS_CONF"] = "C"
                if cell.get("flag"):
                    attributes["pdf_flag"] = cell["flag"]
                    attributes["pdf_flag_meaning"] = flags[cell["flag"]]
                dims = dict(dict(row).get("dims") or {})
                if not by_period:
                    dims[str(table["column_dimension"])] = column
                rows.append({"dims": dims, "period": column if by_period else str(table["period"]),
                             "value_text": cell["value"], "attributes": attributes, "meta": {},
                             "stated_status": None})
    return rows


def parse_release(raw: bytes, *, provider: str, document: Mapping[str, Any], url: str,
                  max_bytes: int = 100_000_000) -> dict[str, Any]:
    """Declared WID, DWA, DFA, HFCS or PHF series as items; flags, statuses and the method statement per series."""
    del url
    if provider == "wid-wealth-distribution":
        rows = _rows_wid(raw, document)
    elif provider in PDF_PROVIDERS:
        rows = read_pdf_tables(raw, document, max_pages=CAPS[provider]["pages"])
    elif provider == "ecb-distributional-wealth-accounts":
        rows = _rows_dwa(raw, document)
    else:
        rows = _rows_dfa(raw, document, max_bytes=max_bytes)
    if not rows:
        raise WealthFormatError("schema_drift", "the response states no series")
    fields = {k: dict(v) for k, v in dict(document["key_fields"]).items()}
    area_spec = dict(document["area"])
    labels = dict(document.get("area_labels") or {})
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        dims = row["dims"]
        if area_spec.get("fixed"):
            area_code = str(area_spec["fixed"])
        else:
            area_code = dims.get(area_spec["dimension"])
            if area_code is None:
                raise WealthFormatError("schema_drift", "the response lacks the declared area dimension")
        if labels and area_code not in labels:
            raise WealthFormatError("schema_drift", f"the response states an undeclared place {area_code!r}")
        group = groups.setdefault(canonical([area_code, dims]), {
            "area": area_code, "dims": dims, "rows": [], "meta": row["meta"],
            "native": row.get("native") or ".".join(str(v) for v in dims.values())})
        group["rows"].append(row)
    if len(groups) > CAPS[provider]["series_per_response"]:
        raise WealthFormatError("budget_exhausted", "the response has more series than the declared cap")
    method_notes = dict(document.get("method_notes") or {})
    items = []
    for group in groups.values():
        dims = group["dims"]
        values = {name: _field(name, spec, dims) for name, spec in fields.items()}
        observations, periods = [], set()
        for row in sorted(group["rows"], key=lambda r: r["period"]):
            period = str(row["period"])
            if not re.fullmatch(r"\d{4}(-Q[1-4])?", period):
                raise WealthFormatError("schema_drift", f"unexpected period {period!r}")
            if period in periods:
                raise WealthFormatError("schema_drift", "a series states a period twice")
            periods.add(period)
            attributes = {str(k): str(v) for k, v in dict(row["attributes"]).items()}
            flags = {k: attributes[k] for k in FLAG_ATTRIBUTES if attributes.get(k)}
            letters = list(flags.values())
            confidential = flags.get("OBS_CONF") in CONFIDENTIAL_FLAGS
            value_text = row["value_text"] if row["value_text"] not in (None, "") else None
            value = decimal_text(value_text)
            if value_text is not None and value is None:
                raise WealthFormatError("schema_drift", f"non-numeric value for {period}")
            if confidential and value is not None:
                raise WealthFormatError("schema_drift", "a cell flagged confidential carries a value; 'C' is a "
                                                        "status, never a value")
            status = "reported" if value is not None else ("confidential" if confidential else "not_published")
            stated = row["stated_status"] or next((FLAG_STATUS[x] for x in letters if x in FLAG_STATUS), None)
            if row["stated_status"] == "extrapolated":
                flags["WID_EXTRAPOLATION"] = attributes.pop("extrapolation", "")
                letters.append("extrapolated")
            observations.append({
                "period": period, "value_text": value_text, "value": value, "status": status,
                "publication_status": stated or "not-stated", "origin": "published", "flags": flags,
                "flag_meanings": sorted({FLAG_MEANINGS.get(x, x) for x in letters}),
                "attributes": {k: v for k, v in sorted(attributes.items()) if k not in FLAG_ATTRIBUTES},
            })
        source_notes = []
        for note in document.get("scope_notes") or []:
            source_notes.append({"kind": "scope_difference", "attribute": "declared", "value": note.get("relates_to"),
                                 "periods": [], "statement": str(note["statement"]),
                                 "cites": [dict(c) for c in note.get("cites") or []]})
        definition = dict(document["definition"])
        stated_method = group["meta"].get("method") or method_notes.get(values["method"]["code"])
        items.append({
            "provider": provider,
            "dataset": str(document["dataset"]),
            "native_key": group["native"],
            **values,
            "area": {"scheme": area_spec["scheme"], "code": group["area"],
                     **({"label": labels[group["area"]]} if group["area"] in labels else {})},
            "frequency": str(document["frequency"]),
            "definition": {
                "provider": provider,
                "source_text": definition.get("source_text"),
                "indicator_meaning": INDICATOR_MEANINGS[values["indicator"]["concept"]],
                "wealth_concept": values["wealth_concept"],
                "population": values["population"],
                "method": values["method"],
                "method_statement": stated_method,
                "methodology_notes": list(definition.get("methodology_notes") or []),
                "references": [dict(r) for r in document.get("references") or []],
            },
            "references": [dict(r) for r in document.get("references") or []],
            "source_notes": source_notes,
            "observations": observations,
        })
    items.sort(key=lambda i: (i["area"]["code"], i["native_key"]))
    declared = dict(document.get("release") or {})
    if declared.get("published_on"):
        published_on, basis = str(declared["published_on"]), "declared_release"
    else:
        published_on, basis = None, "retrieval_time"
    return {"items": items, "published_on": published_on, "published_at": None, "release_basis": basis,
            "release_label": declared.get("label") or published_on, "edition": declared.get("edition")}


# ------------------------------------------------------------------ adapter


class WealthAdapter:
    """Fetch the declared documents; one page (one release) per document."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = wealth_declaration(self.source)
        provider = self.declared["provider"]
        # Only WID takes a key; it is sent in a header and never stored, logged or returned.
        self._secret = secret if provider == "wid-wealth-distribution" else None
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "wealth": {"provider": provider, "format": self.declared["format"],
                       "documents": len(self.declared["documents"]), "caps": CAPS[provider],
                       "pacing": PACING[provider], "live_verification": LIVE_VERIFICATION[provider]["status"]},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "wealth-distribution runs fetch the declared documents only")

    def _get(self, url: str) -> tuple[bytes, str]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        provider = self.declared["provider"]
        host = PROVIDER_HOSTS[provider]
        parts = urlsplit(url)
        if (parts.hostname or "").casefold() != host or parts.scheme != "https":
            raise SourcePackError("network_policy", "declared documents are fetched from the provider's host only")
        headers = {"Accept": "application/json" if provider == "wid-wealth-distribution"
                   else "application/zip" if provider == "fed-distributional-financial-accounts"
                   else "application/pdf" if provider in PDF_PROVIDERS else "text/csv"}
        if provider == "wid-wealth-distribution":
            if not self._secret:
                raise SourcePackError("credential_missing", "WID requests need the key behind NOESIS_WID_API_KEY")
            headers["x-api-key"] = self._secret
        base, _, query = url.partition("?")
        response = self.transport(url=base, params=parse_qsl(query, keep_blank_values=True), headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "response was served from another host")
        status = int(response.get("status", 200))
        response_headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "response exceeds its byte limit")
        if status == 429:
            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(response_headers.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"request refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        if status >= 300:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        return raw, "fixture" if response.get("origin") == "fixture" else "live"

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        documents = self.declared["documents"]
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(documents):
            raise SourcePackError("cursor_drift", "cursor names no declared document")
        document = dict(documents[index])
        provider = self.declared["provider"]
        url = document_url(provider, document, self.source["endpoint"])
        raw, origin = self._get(url)
        try:
            release = parse_release(raw, provider=provider, document=document, url=url,
                                    max_bytes=int(self.definition["limits"]["max_bytes"]))
        except WealthFormatError as exc:
            # A missing PDF reader means the source cannot be read in this installation: never a partial release.
            code = {"budget_exhausted": "budget_exhausted", "dependency_missing": "source_unavailable"}.get(
                exc.code, "schema_drift")
            raise SourcePackError(code, f"{exc.code}: {exc}") from exc
        items = release["items"]
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(items) > limit:
            # Never a truncated release: a missing series would read as a series the source removed.
            raise SourcePackError("budget_exhausted", "release has more series than the run's result budget")
        header = {
            "contract": RELEASE_CONTRACT, "provider": provider, "format": self.declared["format"],
            "document": document, "document_key": document_key(provider, document),
            "published_on": release["published_on"], "published_at": release["published_at"],
            "release_basis": release["release_basis"], "release_label": release["release_label"],
            "edition": release["edition"], "file_sha256": hashlib.sha256(raw).hexdigest(),
            "content_sha256": digest(items), "item_count": len(items), "complete": True, "evidence_origin": origin,
            "live_verification": LIVE_VERIFICATION[provider]["status"], "url": url,
        }
        records = [{
            "id": f"{header['file_sha256'][:16]}:{number}",
            "title": f"{document['label']} ({release['release_label'] or 'retrieved'})",
            "url": url, "language": "en", "published_at": release["published_on"],
            "content": canonical(item), "wealth_release": header, "wealth_item": item,
        } for number, item in enumerate(items)]
        receipt = {"status": 200, "provider": provider, "document": document["label"],
                   "release_label": release["release_label"], "release_basis": release["release_basis"],
                   "file_sha256": header["file_sha256"], "items": len(records), "requests": 1,
                   "evidence_origin": origin, "final_page": index + 1 >= len(documents)}
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


ADAPTERS = {CONNECTOR: WealthAdapter}


def _fixture_key(url: str, params: Any) -> str:
    pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
    query = urlencode(sorted((str(k), str(v)) for k, v in pairs))
    return urlsplit(url).path + ("?" + query if query else "")


def fixture_request(source: Mapping[str, Any], document: Mapping[str, Any]) -> str:
    """The key :func:`fixture_transport` files a response under."""
    provider = dict(source["wealth"])["provider"]
    base, _, query = document_url(provider, document, source["endpoint"]).partition("?")
    return _fixture_key(base, parse_qsl(query, keep_blank_values=True))


def zip_members(members: Mapping[str, str]) -> bytes:
    """A deterministic stored (uncompressed) zip of authored member texts."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        for name in sorted(members):
            info = zipfile.ZipInfo(name, date_time=(2098, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            archive.writestr(info, members[name].encode())
    return buffer.getvalue()


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path and sorted query (``body_zip`` members are zipped on replay;
    ``body_base64`` carries binary bodies such as authored PDFs)."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        key = _fixture_key(url, params)
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        if page.get("body_zip") is not None:
            content = zip_members(dict(page["body_zip"]))
        elif page.get("body_base64") is not None:
            import base64

            content = base64.b64decode(page["body_base64"])
        else:
            body = page.get("body")
            content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture"}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = WealthAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                            secret=FIXTURE_SECRET)
    records, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


__all__ = [
    "ADAPTERS",
    "AREA_SCHEMES",
    "BOUNDED_COVERAGE",
    "CAPS",
    "CONNECTOR",
    "EXCLUSIONS",
    "FIXTURE_SECRET",
    "FORMATS",
    "INDICATORS",
    "INDICATOR_MEANINGS",
    "KEY_FIELDS",
    "LIVE_VERIFICATION",
    "NEVER_SENTENCE",
    "NOT_IMPLEMENTED",
    "PACING",
    "PDF_PROVIDERS",
    "PROVIDERS",
    "PROVIDER_CONTRACTS",
    "PUBLICATION_STATUSES",
    "STATUSES",
    "WealthAdapter",
    "WealthFormatError",
    "decimal_text",
    "document_key",
    "document_url",
    "fixture_request",
    "fixture_transport",
    "parse_release",
    "pdf_lines",
    "read_cell",
    "read_pdf_tables",
    "replay_native_fixture",
    "series_key",
    "unverified",
    "wealth_declaration",
    "zip_members",
]
