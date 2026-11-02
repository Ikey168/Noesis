"""Tax transparency sources for the Economics ``economics.tax`` provider (#2889).

The machine-readable copy of ``docs/development/tax-transparency-evidence/source-audit.md`` (TX01). One native
source-pack connector, ``tax-transparency``, driven by :mod:`src.ingestion.source_pack_runtime` with the four sources of
the ``economic-tax-transparency`` pack (``config/source_packs/economic-tax-transparency.json``). Each source declares one
provider and a bounded list of documents; the adapter fetches one declared document per page and returns one *release*
(an edition: a header and one item per series) that :class:`src.kb.tax_store.TaxProjector` appends as series vintages.

* **OECD Corporate Tax Statistics, CbCR aggregates** (``oecd-cts-cbcr``) - anonymised and aggregated country-by-country
  reporting statistics by ultimate parent jurisdiction and partner-jurisdiction grouping (``DSD_CBCR@DF_CBCRI``,
  *verify*), published statistics.
* **OECD Corporate Tax Statistics, corporate tax rates** (``oecd-cts-rates``) - statutory corporate income tax rates
  (``DSD_TAX_CIT@DF_CIT``, published statistics) and forward-looking effective tax rates (``DSD_ETR@DF_ETR``,
  *verify*), which are **model estimates** of a hypothetical investment and are stored as such.
* **EU Tax Observatory, offshore wealth** (``eutax-offshore-wealth``) - Atlas of the Offshore World estimates of
  offshore financial wealth per country, model estimates per edition (licence not established, *verify*).
* **EU Tax Observatory, billionaire effective tax rates** (``eutax-billionaire-etr``) - Global Tax Evasion Report
  estimates per country, model estimates per report edition (licence not established, *verify*).

Every document is a CSV with declared column roles (SDMX-CSV for the OECD, the publisher's CSV for the EU Tax
Observatory; long or wide form). Each release is an **edition** with a declared label and publication date. Status
letters are kept verbatim; a confidential cell is a status, never a value.

**Tax Justice Network** indices (Corporate Tax Haven Index, Financial Secrecy Index) are ``not-implemented``: the data
portal is registration-gated, free only for non-commercial use, and commercial use needs a paid licence; nothing is
acquired or scraped. **Public CbCR** reports under Directive (EU) 2021/2101 are ``reference-only``: no machine-readable
source exists, so operators record links (:meth:`src.kb.tax_store.TaxStore.record_company_report`), never figures.

Every acquired source is ``unverified-live`` until a dated live run; endpoints, dataflow ids, column names and code lists
marked *verify* come from documentation and catalogue mirrors, not from a live response. Nothing here computes an
effective tax rate, a ratio, a share, a rank or a score, blends sources, fills a jurisdiction or year, converts a
currency or forecasts.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

CONNECTOR = "tax-transparency"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-tax-transparency-release-v1"
AUDIT = "docs/development/tax-transparency-evidence/source-audit.md"
# None of the acquired sources needs a credential, so the fixtures expect none (native-connector contract).
FIXTURE_SECRET = None
PROVIDERS = ("oecd-cts-cbcr", "oecd-cts-rates", "eutax-offshore-wealth", "eutax-billionaire-etr")
NOT_IMPLEMENTED = ("tjn-cthi", "tjn-fsi")
REFERENCE_ONLY = ("public-cbcr",)
FORMATS = {
    "oecd-cts-cbcr-csv": {"provider": "oecd-cts-cbcr"},
    "oecd-cts-rates-csv": {"provider": "oecd-cts-rates"},
    "eutax-offshore-wealth-csv": {"provider": "eutax-offshore-wealth"},
    "eutax-billionaire-etr-csv": {"provider": "eutax-billionaire-etr"},
}
PROVIDER_HOSTS = {
    "oecd-cts-cbcr": "sdmx.oecd.org",
    "oecd-cts-rates": "sdmx.oecd.org",
    "eutax-offshore-wealth": "atlas-offshore.world",
    "eutax-billionaire-etr": "www.taxobservatory.eu",
}
NATURES = ("published_statistic", "model_estimate")
# The audited datasets: provider, nature of the values, whether a counterpart grouping is part of the key, and the
# concepts each may deliver (verify every id against the live catalogue).
DATASETS: dict[str, dict[str, Any]] = {
    "DSD_CBCR@DF_CBCRI": {
        "provider": "oecd-cts-cbcr", "nature": "published_statistic", "counterpart": True,
        "concepts": ("profit_before_income_tax", "income_tax_accrued", "income_tax_paid", "employees",
                     "revenues_total", "tangible_assets"),
    },
    "DSD_TAX_CIT@DF_CIT": {
        "provider": "oecd-cts-rates", "nature": "published_statistic", "counterpart": False,
        "concepts": ("statutory_cit_rate",),
    },
    "DSD_ETR@DF_ETR": {
        "provider": "oecd-cts-rates", "nature": "model_estimate", "counterpart": False,
        "concepts": ("effective_average_tax_rate", "effective_marginal_tax_rate"),
    },
    "atlas-offshore-financial-wealth": {
        "provider": "eutax-offshore-wealth", "nature": "model_estimate", "counterpart": False,
        "concepts": ("offshore_financial_wealth", "offshore_financial_wealth_share_of_gdp"),
    },
    "gter-billionaire-effective-tax-rates": {
        "provider": "eutax-billionaire-etr", "nature": "model_estimate", "counterpart": False,
        "concepts": ("billionaire_effective_tax_rate_wealth", "billionaire_effective_tax_rate_income"),
    },
}
CONCEPTS = tuple(sorted({c for d in DATASETS.values() for c in d["concepts"]}))
# Index concepts that would come from the Tax Justice Network and are not implemented.
NOT_IMPLEMENTED_CONCEPTS = {"corporate_tax_haven_index": "tjn-cthi", "haven_score": "tjn-cthi",
                            "financial_secrecy_index": "tjn-fsi", "secrecy_score": "tjn-fsi"}
JURISDICTION_SCHEMES = ("iso3166-alpha3",)
STATUSES = ("reported", "estimated", "confidential", "not_published")
FREQUENCIES = ("annual",)
NEVER_SENTENCE = (
    "Published tax statistics and model estimates as their publishers released them, edition by edition: OECD CbCR "
    "aggregates and statutory rates as published statistics, OECD forward-looking effective tax rates and EU Tax "
    "Observatory offshore-wealth and billionaire tax-rate figures as model estimates with their method and edition, "
    "never presented as observations; no own effective tax rate, ratio, share, rank or score, no blending of sources, "
    "no filled jurisdiction or year, no currency conversion and no forecast."
)
EXCLUSIONS = (
    "own effective tax rates, profit-shifting or tax-gap estimates",
    "ratios, shares, per-capita or per-employee figures computed from published values",
    "blending OECD, EU Tax Observatory or company figures",
    "presenting model estimates as observations",
    "ranking or risk-scoring jurisdictions or companies",
    "filling jurisdictions or years a source did not publish",
    "currency conversion",
    "forecasts",
    "company-level figures from public CbCR reports (link-only references)",
    "Tax Justice Network index scores and ranks (not-implemented: licence)",
    "taxpayer-level or person-level data, including the names of individual wealth holders",
    "values for confidential cells (stored as their status only)",
)
# Hard ceilings the adapter enforces on top of the source-pack budgets (TX01 bounded first coverage).
CAPS = {
    "oecd-cts-cbcr": {"documents": 1, "series_per_response": 40, "years": 5},
    "oecd-cts-rates": {"documents": 2, "series_per_response": 20, "years": 5},
    "eutax-offshore-wealth": {"documents": 1, "series_per_response": 20, "years": 5},
    "eutax-billionaire-etr": {"documents": 1, "series_per_response": 10, "years": 5},
}
REFERENCE_CAP = 50
BOUNDED_COVERAGE = {
    "oecd-cts-cbcr": {
        "jurisdictions": {"Germany": "DEU", "Netherlands": "NLD"},
        "series": "DF_CBCRI: profit (loss) before income tax, income tax accrued and employees by ultimate parent "
                  "jurisdiction, partner groupings total, domestic and foreign (verify codes)",
        "periods": "at most 5 years from a declared start year",
        "caps": "1 document, 40 series",
    },
    "oecd-cts-rates": {
        "jurisdictions": {"Germany": "DEU", "Netherlands": "NLD", "Ireland": "IRL"},
        "series": "DF_CIT combined statutory CIT rate (published statistic) and DF_ETR forward-looking effective "
                  "average tax rate (model estimate)",
        "periods": "at most 5 years",
        "caps": "2 documents, 20 series each",
    },
    "eutax-offshore-wealth": {
        "jurisdictions": {"Germany": "DEU", "Netherlands": "NLD"},
        "series": "offshore financial wealth, amount and share of GDP (model estimates)",
        "periods": "at most 5 years",
        "caps": "1 document, 20 series",
    },
    "eutax-billionaire-etr": {
        "jurisdictions": {"France": "FRA", "Netherlands": "NLD"},
        "series": "billionaires' effective tax rate on wealth and on income (model estimates; verify the measures)",
        "periods": "the report's reference year",
        "caps": "1 document, 10 series",
    },
    "tjn-cthi": {"status": "not-implemented", "reason": "registration-gated portal; free only for non-commercial use; "
                                                        "commercial use needs a paid licence"},
    "tjn-fsi": {"status": "not-implemented", "reason": "registration-gated portal; free only for non-commercial use; "
                                                       "commercial use needs a paid licence"},
    "public-cbcr": {"status": "reference-only", "reason": "no machine-readable source; operator-declared links only",
                    "cap": f"{REFERENCE_CAP} references per call"},
    "record_cap": "max_results series per response and max_pages documents per run; a larger release is refused, "
                  "never truncated; every further jurisdiction or dataset is a source-pack version bump",
}
_OECD_ENTRY = ("https://sdmx.oecd.org/public/rest/data/OECD.CTP.TPS,{structure}@{dataflow},{version}/{key}"
               "?startPeriod=...&endPeriod=...&format=csvfilewithlabels (verify)")
_OECD_LICENCE = {"id": "oecd-terms-cc-by-4.0", "text": "OECD Terms and Conditions; OECD data are stated to be "
                 "available under CC BY 4.0 from 1 July 2024 (verify)", "terms_url": "https://www.oecd.org/en/about/terms-conditions.html",
                 "attribution": "OECD, Corporate Tax Statistics database"}
_EUTAX_LICENCE = {"id": "eu-tax-observatory-terms-not-established", "text": "not established (verify): no licence "
                  "statement was found; a live run needs an operator to verify and accept the terms first",
                  "terms_url": "https://www.taxobservatory.eu/", "attribution": "EU Tax Observatory"}
_OECD_REVISION = (
    "annual editions of the Corporate Tax Statistics database; each edition is a release vintage dated by its declared "
    "publication date; a value changed in a later edition is a new vintage; a series a later complete edition no "
    "longer states becomes a removed_by_source vintage; confidential cells are a status; OBS_STATUS letters verbatim "
    "(verify the code list)"
)
_EUTAX_REVISION = ("each update or report is an edition; a changed estimate in a later edition is a new vintage; the "
                   "method is recorded per edition")
_FALLBACK = ("a failed document (HTTP error, redirect to another host, a missing declared column, a response larger "
             "than the budget) fails that source's run with its code and a receipt; earlier vintages stay current and "
             "nothing is marked removed or revised because of a failure; readiness reports the source as stale")
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "oecd-cts-cbcr": {
        "publisher": "OECD, Centre for Tax Policy and Administration: Corporate Tax Statistics database",
        "delivers": "anonymised and aggregated CbCR statistics (profit before income tax, income tax accrued and paid, "
                    "employees, revenues, tangible assets) by ultimate parent jurisdiction and partner grouping",
        "access": "api (OECD Data Explorer SDMX REST, CSV); OECD.CTP.TPS DSD_CBCR@DF_CBCRI (verify id, version and "
                  "dimension order)",
        "entry_points": [_OECD_ENTRY],
        "authentication": "none",
        "key_handling": "no key; nothing secret is sent or stored",
        "licence": _OECD_LICENCE,
        "redistribution": "attribution-required (verify)",
        "rate_limits": "OECD Data Explorer per-IP request limits (verify the current figure); one request per document",
        "revision_model": _OECD_REVISION,
        "nature": "published_statistic",
        "definitions": "CbCR aggregates describe groups of multinational groups by parent jurisdiction, never a "
                       "company; the OECD caveats (double counting of intra-group dividends, differing accounting "
                       "standards, confidentiality; verify the wording per edition) travel as definition notes",
        "personal_data": "none: published aggregates only",
        "unavailable_fallback": _FALLBACK,
        "status": "unverified-live",
        "verify": ["dataflow id and version", "dimension order and column names", "partner grouping codes",
                   "OBS_STATUS and CONF_STATUS code lists", "the CC BY 4.0 statement", "rate limits",
                   "edition publication dates"],
    },
    "oecd-cts-rates": {
        "publisher": "OECD, Centre for Tax Policy and Administration: Corporate Tax Statistics database",
        "delivers": "statutory corporate income tax rates (published statistics) and forward-looking effective tax "
                    "rates (model estimates)",
        "access": "api (same SDMX REST path); DSD_TAX_CIT@DF_CIT and DSD_ETR@DF_ETR (verify)",
        "entry_points": [_OECD_ENTRY],
        "authentication": "none",
        "key_handling": "no key; nothing secret is sent or stored",
        "licence": _OECD_LICENCE,
        "redistribution": "attribution-required (verify)",
        "rate_limits": "as oecd-cts-cbcr (verify)",
        "revision_model": _OECD_REVISION,
        "nature": "statutory rates: published_statistic; forward-looking effective tax rates: model_estimate",
        "definitions": "statutory combined (central plus sub-central) CIT rate as published; forward-looking effective "
                       "average and marginal tax rates come from the OECD model of a hypothetical investment and keep "
                       "the method and edition",
        "personal_data": "none: published aggregates only",
        "unavailable_fallback": _FALLBACK,
        "status": "unverified-live",
        "verify": ["DF_CIT and DF_ETR ids and versions", "measure codes", "the ETR methodology reference",
                   "the CC BY 4.0 statement"],
    },
    "eutax-offshore-wealth": {
        "publisher": "EU Tax Observatory: Atlas of the Offshore World",
        "delivers": "offshore financial wealth per country as model estimates (amount and share of GDP), per edition",
        "access": "download (Atlas 'Download Data' and its GitHub repository per search results; verify path and "
                  "format)",
        "entry_points": ["https://atlas-offshore.world/... (verify)"],
        "authentication": "none",
        "key_handling": "no key; nothing secret is sent or stored",
        "licence": _EUTAX_LICENCE,
        "redistribution": "not established (verify)",
        "rate_limits": "unknown (verify); one request per declared document",
        "revision_model": _EUTAX_REVISION,
        "nature": "model_estimate",
        "definitions": "offshore financial wealth held by a country's households as estimated by the Atlas method "
                       "(verify the data sources and model per edition)",
        "personal_data": "none: country estimates only",
        "unavailable_fallback": _FALLBACK,
        "status": "unverified-live",
        "verify": ["the licence", "the download path, format and columns", "the method per edition"],
    },
    "eutax-billionaire-etr": {
        "publisher": "EU Tax Observatory: Global Tax Evasion Report",
        "delivers": "billionaires' effective tax rates per country as model estimates, per report edition",
        "access": "download (verify that a machine-readable appendix exists; the PDF is never parsed)",
        "entry_points": ["https://www.taxobservatory.eu/... (verify)"],
        "authentication": "none",
        "key_handling": "no key; nothing secret is sent or stored",
        "licence": _EUTAX_LICENCE,
        "redistribution": "not established (verify)",
        "rate_limits": "unknown (verify); one request per declared document",
        "revision_model": _EUTAX_REVISION,
        "nature": "model_estimate",
        "definitions": "taxes paid by billionaires relative to their wealth or income as estimated in the report "
                       "(verify the measures); country figures only, never named individuals",
        "personal_data": "none: country estimates only; names of wealth holders are never stored",
        "unavailable_fallback": _FALLBACK,
        "status": "unverified-live",
        "verify": ["the licence", "whether a machine-readable appendix exists", "the measures and their method"],
    },
    "tjn-cthi": {
        "publisher": "Tax Justice Network: Corporate Tax Haven Index",
        "delivers": "haven scores, global scale weights and ranks per jurisdiction, per edition",
        "access": "none acquired: TJN data portal, registration required; no documented machine API (verify)",
        "entry_points": [],
        "authentication": "-",
        "key_handling": "-",
        "licence": {"id": "tjn-tiered", "text": "free for non-commercial users after registration; commercial users pay "
                    "a licensing fee or subscription; inclusion in a product or service provided to third parties is "
                    "commercial (per search results; full terms not read)"},
        "redistribution": "not permitted for commercial use without a paid licence",
        "rate_limits": "-",
        "revision_model": "-",
        "nature": "-",
        "definitions": "-",
        "personal_data": "none acquired",
        "unavailable_fallback": "queries report not-implemented, never empty",
        "status": "not-implemented",
        "verify": ["the full licence terms", "whether an open-licensed release exists"],
    },
    "tjn-fsi": {
        "publisher": "Tax Justice Network: Financial Secrecy Index",
        "delivers": "secrecy scores, global scale weights and ranks per jurisdiction, per edition",
        "access": "none acquired: TJN data portal, registration required; no documented machine API (verify)",
        "entry_points": [],
        "authentication": "-",
        "key_handling": "-",
        "licence": {"id": "tjn-tiered", "text": "free for non-commercial users after registration; commercial users pay "
                    "a licensing fee (per search results; full terms not read)"},
        "redistribution": "not permitted for commercial use without a paid licence",
        "rate_limits": "-",
        "revision_model": "-",
        "nature": "-",
        "definitions": "-",
        "personal_data": "none acquired",
        "unavailable_fallback": "queries report not-implemented, never empty",
        "status": "not-implemented",
        "verify": ["the full licence terms", "whether an open-licensed release exists"],
    },
    "public-cbcr": {
        "publisher": "each in-scope multinational group (Directive (EU) 2021/2101), on its website or a business "
                     "register",
        "delivers": "per-company income tax information reports, financial years starting on or after 2024-06-22 "
                    "(verify)",
        "access": "none acquired: no central register or machine-readable collection; operator-declared links",
        "entry_points": [],
        "authentication": "-",
        "key_handling": "-",
        "licence": {"id": "per-report", "text": "each report's own terms; only links and metadata are stored"},
        "redistribution": "links and metadata only",
        "rate_limits": "-",
        "revision_model": "a corrected report is a new revision of its reference; earlier revisions stay listed",
        "nature": "-",
        "definitions": "Commission Implementing Regulation (EU) 2024/2952 common template and machine-readable formats "
                       "(verify)",
        "personal_data": "none: company names as published and links",
        "unavailable_fallback": "-",
        "status": "reference-only",
        "verify": ["the directive's application date", "the implementing regulation", "a machine-readable source"],
    },
}
LIVE_VERIFICATION = {
    **{provider: {"status": "unverified-live", "checked": None, "evidence": None,
                  "intended": "verified-live after a dated bounded run",
                  "note": "no dated live run from this runtime; authored offline fixtures only"}
       for provider in PROVIDERS},
    **{provider: {"status": "not-implemented", "checked": None, "evidence": None,
                  "intended": "stays not-implemented unless an amended audit finds terms that allow the intended use",
                  "note": "registration-gated portal, non-commercial-only free licence, commercial fee"}
       for provider in NOT_IMPLEMENTED},
    "public-cbcr": {"status": "reference-only", "checked": None, "evidence": None,
                    "intended": "a machine-readable source would need its own audit",
                    "note": "operator-declared links only; no figures"},
}
NOT_IMPLEMENTED_ANSWER = {
    "providers": list(NOT_IMPLEMENTED), "status": "not-implemented",
    "reason": "the Tax Justice Network's Corporate Tax Haven Index and Financial Secrecy Index are free only for "
              "non-commercial use after registration and need a paid licence otherwise; nothing is acquired, so this "
              "is not an empty answer",
    "audit": AUDIT,
}
# OECD SDMX OBS_STATUS letters (verify against the live code list) and CONF_STATUS letters.
FLAG_MEANINGS = {"A": "normal value", "B": "break in time series", "E": "estimated value", "P": "provisional value",
                 "M": "missing value", "L": "missing value; data exist but were not collected", "C": "confidential",
                 "F": "free (not confidential)"}
CONFIDENTIAL = {"C"}
MISSING = {"M", "L"}


class TaxFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def unverified(provider: str) -> bool:
    return LIVE_VERIFICATION.get(provider, {}).get("status") != "verified-live"


def text(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    return raw or None


def decimal_text(value: Any) -> str | None:
    """A published number as exact decimal text; ``None`` for a missing or non-numeric value (never zero)."""
    raw = text(value)
    if raw is None or raw in {":", "..", "NaN", "nan", "null", "None", "NA", "n.a."}:
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


def period_matches(period: str, frequency: str = "annual") -> bool:
    return frequency == "annual" and re.fullmatch(r"\d{4}", str(period)) is not None


def series_key(item: Mapping[str, Any]) -> dict[str, Any]:
    """The fields that make a series (never an edition, so each edition of the same key is a vintage)."""
    jurisdiction = dict(item["jurisdiction"])
    unit = dict(item.get("unit") or {})
    return {
        "provider": item["provider"],
        "dataset": item["dataset"],
        "indicator": dict(item["indicator"]).get("code"),
        "concept": dict(item["indicator"]).get("concept"),
        "jurisdiction": {"scheme": jurisdiction["scheme"], "code": str(jurisdiction["code"])},
        "counterpart": dict(item.get("counterpart") or {}).get("code"),
        "unit": unit.get("code"),
        "unit_multiplier": unit.get("multiplier"),
        "frequency": item.get("frequency"),
        "nature": item.get("nature"),
    }


# ------------------------------------------------------------------ declarations


def tax_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    config = dict(source.get("tax_transparency") or {})
    provider, fmt = config.get("provider"), config.get("format")
    if provider not in PROVIDERS or FORMATS.get(str(fmt), {}).get("provider") != provider:
        raise SourcePackError("invalid_manifest", "tax-transparency sources declare a known provider and its format")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host != PROVIDER_HOSTS[provider]:
        raise SourcePackError("invalid_manifest", f"{provider} documents are fetched from {PROVIDER_HOSTS[provider]}")
    if dict(source.get("auth") or {}).get("kind") != "none":
        raise SourcePackError("invalid_manifest", "tax-transparency requests carry no credential")
    documents = [dict(d) for d in config.get("documents") or []]
    if not documents:
        raise SourcePackError("invalid_manifest", f"{provider} declares at least one document")
    if len(documents) > int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "more declared documents than the source's page budget")
    if len(documents) > CAPS[provider]["documents"]:
        raise SourcePackError("invalid_manifest", f"{provider} declares at most {CAPS[provider]['documents']} documents")
    urls = []
    for document in documents:
        try:
            check_document(provider, document)
        except TaxFormatError as exc:
            raise SourcePackError("invalid_manifest", f"{document.get('label')}: {exc}") from exc
        parts = urlsplit(str(document["url"]))
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError("invalid_manifest", "declared documents are HTTPS resources on the endpoint's host")
        urls.append(str(document["url"]))
    if len(set(urls)) != len(urls):
        raise SourcePackError("invalid_manifest", "each declared document is a distinct request")
    return {"provider": provider, "format": fmt, "namespace": config.get("namespace") or "global",
            "documents": documents, "live_verification": config.get("live_verification") or "unverified-live"}


def check_document(provider: str, document: Mapping[str, Any]) -> None:
    for key in ("label", "dataset", "url", "release", "columns", "jurisdictions", "indicators", "period",
                "definition", "references"):
        if not document.get(key):
            raise TaxFormatError("invalid_document", f"a document states its {key}")
    dataset = DATASETS.get(str(document["dataset"]))
    if dataset is None or dataset["provider"] != provider:
        raise TaxFormatError("unbounded_document", f"{document['dataset']} is not an audited dataset of {provider}")
    release = dict(document["release"])
    if iso_day(release.get("published_on")) is None or not text(release.get("edition")):
        raise TaxFormatError("invalid_document", "a document declares its edition and its publication date")
    for ref in document.get("references") or []:
        if not text(dict(ref).get("identifier")) or not text(dict(ref).get("kind")):
            raise TaxFormatError("invalid_document", "each reference states its kind and identifier as published")
    period = dict(document["period"])
    start, end = str(period.get("start") or ""), str(period.get("end") or "")
    if not (period_matches(start) and period_matches(end)) or not 1 <= int(end) - int(start) + 1 <= \
            CAPS[provider]["years"]:
        raise TaxFormatError("unbounded_document", f"documents pin at most {CAPS[provider]['years']} annual periods")
    columns = dict(document["columns"])
    for role in ("jurisdiction", "period"):
        if not columns.get(role):
            raise TaxFormatError("invalid_document", f"a document names its {role} column")
    wide = bool(document.get("value_columns"))
    if not wide and not (columns.get("indicator") and columns.get("obs_value")):
        raise TaxFormatError("invalid_document", "a long table names its indicator and obs_value columns; a wide table "
                                                 "declares value_columns")
    if dataset["counterpart"] and (not columns.get("counterpart") or not dict(document.get("counterparts") or {})):
        raise TaxFormatError("invalid_document", "a CbCR document names its counterpart column and grouping codes")
    if document.get("jurisdiction_scheme") not in JURISDICTION_SCHEMES:
        raise TaxFormatError("invalid_document", f"jurisdictions use one of {JURISDICTION_SCHEMES}")
    for code in dict(document["jurisdictions"]):
        if not re.fullmatch(r"[A-Z]{3}", str(code)):
            raise TaxFormatError("invalid_document", "jurisdiction codes are ISO 3166 alpha-3")
    indicators = dict(document["indicators"])
    for code, spec in indicators.items():
        spec = dict(spec)
        if spec.get("concept") not in dataset["concepts"] or not text(spec.get("label")):
            raise TaxFormatError("invalid_document", f"indicator {code} maps to one of {dataset['concepts']} with a "
                                                     "label")
        if not columns.get("unit") and not text(dict(spec.get("unit") or {}).get("code")):
            raise TaxFormatError("invalid_document", f"indicator {code} states its unit")
    for column in document.get("value_columns") or []:
        if dict(column).get("indicator") not in indicators or not text(dict(column).get("column")):
            raise TaxFormatError("invalid_document", "each value column names its column and a declared indicator")
    if dataset["nature"] == "model_estimate":
        method = dict(document.get("method") or {})
        if not text(method.get("label")) or not text(method.get("description")) or \
                not str(method.get("reference") or "").startswith("https://"):
            raise TaxFormatError("invalid_document", "a model-estimate document states its method (label, description "
                                                     "and an https reference)")
    if not text(dict(document["definition"]).get("source_text")):
        raise TaxFormatError("invalid_document", "a document records the definition its source states")


def document_key(provider: str, document: Mapping[str, Any]) -> str:
    """Identity of a declared document across editions (removal detection compares editions of one document)."""
    return f"{provider}:{document['dataset']}:" + digest(sorted(dict(document["jurisdictions"])))[:12]


def document_url(document: Mapping[str, Any]) -> str:
    return str(document["url"])


# ------------------------------------------------------------------ parsing


def _read_csv(raw: bytes, document: Mapping[str, Any]) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    try:
        body = raw.decode(str(document.get("encoding") or "utf-8-sig"))
    except (UnicodeDecodeError, LookupError) as exc:
        raise TaxFormatError("schema_drift", "the CSV is not in the declared encoding") from exc
    reader = csv.DictReader(io.StringIO(body))
    header = [h.strip() for h in reader.fieldnames or []]
    rows = []
    for number, row in enumerate(reader, start=2):
        if number > 20_000:
            raise TaxFormatError("input_limit", "the table has more rows than allowed")
        rows.append((number, {str(k).strip(): v for k, v in row.items() if k is not None}))
    return header, rows


def _unit(document: Mapping[str, Any], spec: Mapping[str, Any], row: Mapping[str, str]) -> dict[str, Any]:
    columns = dict(document["columns"])
    labels = dict(document.get("units") or {})
    if columns.get("unit"):
        code = text(row.get(columns["unit"])) or "unstated"
    else:
        code = str(dict(spec["unit"])["code"])
    multiplier = text(row.get(columns["unit_mult"])) if columns.get("unit_mult") else \
        text(dict(spec.get("unit") or {}).get("multiplier"))
    return {"code": code, "label": labels.get(code) or dict(spec.get("unit") or {}).get("label") or code,
            "multiplier": multiplier or "0"}


def _cells(document: Mapping[str, Any], row: Mapping[str, str]) -> list[tuple[str, str | None, dict[str, str]]]:
    """``(indicator code, value text, flags)`` for one row, long or wide form."""
    columns = dict(document["columns"])
    flags: dict[str, str] = {}
    for role in ("status", "confidentiality"):
        if columns.get(role) and text(row.get(columns[role])):
            flags[columns[role]] = str(text(row.get(columns[role])))
    if document.get("value_columns"):
        return [(str(c["indicator"]), text(row.get(str(c["column"]))), dict(flags))
                for c in document["value_columns"]]
    return [(str(text(row.get(columns["indicator"])) or ""), text(row.get(columns["obs_value"])), flags)]


def parse_table(raw: bytes, *, fmt: str, document: Mapping[str, Any]) -> dict[str, Any]:
    """Declared tax CSV as series items: one per indicator, jurisdiction, counterpart and unit; status letters
    verbatim; confidential cells as a status; model estimates carry their method and edition."""
    provider = FORMATS[fmt]["provider"]
    dataset_id = str(document["dataset"])
    dataset = DATASETS[dataset_id]
    header, rows = _read_csv(raw, document)
    columns = {k: v for k, v in dict(document["columns"]).items() if v}
    needed = [c for role, c in columns.items() if role not in {"status", "confidentiality", "unit_mult"}]
    needed += [str(c["column"]) for c in document.get("value_columns") or []]
    missing = sorted({c for c in needed if c not in header})
    if missing:
        raise TaxFormatError("schema_drift", f"the table lacks declared columns {missing}")
    jurisdictions = dict(document["jurisdictions"])
    counterparts = dict(document.get("counterparts") or {})
    indicators = {str(k): dict(v) for k, v in dict(document["indicators"]).items()}
    period = dict(document["period"])
    edition = str(dict(document["release"])["edition"])
    method = dict(document["method"]) if dataset["nature"] == "model_estimate" else None
    series: dict[str, dict[str, Any]] = {}
    for number, row in rows:
        code = text(row.get(columns["jurisdiction"]))
        year = text(row.get(columns["period"]))
        if code is None or year is None:
            continue
        if code not in jurisdictions:
            raise TaxFormatError("schema_drift", f"row {number} states an undeclared jurisdiction {code!r}")
        if not period_matches(year):
            raise TaxFormatError("schema_drift", f"row {number}: {year!r} is not an annual period")
        if not int(period["start"]) <= int(year) <= int(period["end"]):
            raise TaxFormatError("schema_drift", f"row {number}: {year} is outside the declared periods")
        counterpart = None
        if dataset["counterpart"]:
            counterpart_code = text(row.get(columns["counterpart"]))
            if counterpart_code not in counterparts:
                raise TaxFormatError("schema_drift", f"row {number} states an undeclared counterpart "
                                                     f"{counterpart_code!r}")
            counterpart = {"code": counterpart_code, "label": counterparts[counterpart_code]}
        for indicator_code, value_text, flags in _cells(document, row):
            spec = indicators.get(indicator_code)
            if spec is None:
                raise TaxFormatError("schema_drift", f"row {number} states an unmapped indicator {indicator_code!r}")
            unit = _unit(document, spec, row)
            native_key = ".".join([dataset_id, code, (counterpart or {}).get("code") or "_Z", indicator_code,
                                   unit["code"], unit["multiplier"]])
            entry = series.setdefault(native_key, {
                "provider": provider,
                "dataset": dataset_id,
                "nature": dataset["nature"],
                "native_key": native_key,
                "indicator": {"code": indicator_code, "concept": spec["concept"], "label": spec["label"]},
                "jurisdiction": {"scheme": str(document["jurisdiction_scheme"]), "code": code,
                                 "label": jurisdictions[code]},
                "counterpart": counterpart or {"code": "not_applicable",
                                               "label": "no partner grouping for this dataset"},
                "unit": unit,
                "frequency": "annual",
                "edition": edition,
                "method": method,
                "definition": {"provider": provider, "source_text": str(document["definition"]["source_text"]),
                               "notes": [str(n) for n in document["definition"].get("notes") or []],
                               "caveats": [str(n) for n in document["definition"].get("caveats") or []]},
                "references": [dict(r) for r in document.get("references") or []],
                "observations": {},
            })
            letters = set("".join(flags.values()))
            value = decimal_text(value_text)
            if flags.get(columns.get("confidentiality") or "") in CONFIDENTIAL:
                status = "confidential"
            elif value is None or set(flags.get(columns.get("status") or "", "")) & MISSING:
                status = "not_published"
            else:
                status = "estimated" if dataset["nature"] == "model_estimate" else "reported"
            if year in entry["observations"]:
                raise TaxFormatError("schema_drift", f"{native_key} {year} appears twice")
            entry["observations"][year] = {
                "period": year,
                "value_text": value_text,
                "value": value if status in {"reported", "estimated"} else None,
                "status": status,
                "flags": dict(sorted(flags.items())),
                "flag_meanings": sorted({FLAG_MEANINGS.get(letter, letter) for letter in letters}),
                "row": number,
            }
    if not series:
        raise TaxFormatError("schema_drift", "the table states no row of the declared selection")
    if len(series) > CAPS[provider]["series_per_response"]:
        raise TaxFormatError("budget_exhausted", "the response has more series than the audited cap")
    items = []
    for key in sorted(series):
        entry = series[key]
        observations = [entry["observations"][p] for p in sorted(entry["observations"])]
        notes = []
        breaks = [o["period"] for o in observations if "B" in "".join(o["flags"].values())]
        if breaks:
            notes.append({"kind": "break", "periods": breaks, "statement": f"{document['label']}: the source flags a "
                                                                           "break in series (B)"})
        provisional = [o["period"] for o in observations if "P" in o["flags"].get(columns.get("status", ""), "")]
        if provisional:
            notes.append({"kind": "provisional", "periods": provisional,
                          "statement": f"{document['label']}: the source marks these periods provisional (P)"})
        items.append({**entry, "observations": observations, "source_notes": notes})
    release = dict(document["release"])
    return {"items": items, "published_on": iso_day(release["published_on"]), "release_basis": "declared_release",
            "release_label": text(release.get("label")) or edition, "edition": edition,
            "structure": {"header": header, "dataset": dataset_id}}


# ------------------------------------------------------------------ adapter


class TaxTransparencyAdapter:
    """Fetch the declared documents on the runtime's default transport; one page (one edition) per document."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        del secret  # no acquired tax source carries a credential
        self.source = json.loads(json.dumps(source))
        self.declared = tax_declaration(self.source)
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        provider = self.declared["provider"]
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "tax_transparency": {"provider": provider, "format": self.declared["format"],
                                 "documents": len(self.declared["documents"]), "caps": CAPS[provider],
                                 "live_verification": LIVE_VERIFICATION[provider]["status"]},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "tax-transparency runs fetch the declared documents only")

    def _get(self, url: str) -> tuple[bytes, str]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        host = PROVIDER_HOSTS[self.declared["provider"]]
        parts = urlsplit(url)
        if (parts.hostname or "").casefold() != host or parts.scheme != "https":
            raise SourcePackError("network_policy", "declared documents are fetched from the provider's host only")
        base, _, query = url.partition("?")
        response = self.transport(url=base, params=parse_qsl(query, keep_blank_values=True),
                                  headers={"Accept": "text/csv"},
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
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
        documents = self.declared["documents"]
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(documents):
            raise SourcePackError("cursor_drift", "cursor names no declared document")
        document = dict(documents[index])
        fmt, provider = self.declared["format"], self.declared["provider"]
        url = document_url(document)
        raw, origin = self._get(url)
        try:
            release = parse_table(raw, fmt=fmt, document=document)
        except TaxFormatError as exc:
            code = {"budget_exhausted": "budget_exhausted", "input_limit": "response_too_large"}.get(exc.code,
                                                                                                     "schema_drift")
            raise SourcePackError(code, f"{exc.code}: {exc}") from exc
        items = release["items"]
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(items) > limit:
            # Never a truncated edition: a missing series would read as a series the source removed.
            raise SourcePackError("budget_exhausted", "release has more series than the run's result budget")
        header = {
            "contract": RELEASE_CONTRACT, "provider": provider, "format": fmt, "document": document,
            "document_key": document_key(provider, document), "published_on": release["published_on"],
            "published_at": None, "release_basis": release["release_basis"], "release_label": release["release_label"],
            "edition": release["edition"], "file_sha256": hashlib.sha256(raw).hexdigest(),
            "content_sha256": digest(items), "item_count": len(items), "complete": True, "evidence_origin": origin,
            "live_verification": LIVE_VERIFICATION[provider]["status"], "url": url,
        }
        records = [{
            "id": f"{header['file_sha256'][:16]}:{number}",
            "title": f"{document['label']} ({release['edition']})",
            "url": url, "language": "en", "published_at": release["published_on"],
            "content": canonical(item), "tax_release": header, "tax_item": item,
        } for number, item in enumerate(items)]
        receipt = {"status": 200, "provider": provider, "document": document["label"], "edition": release["edition"],
                   "published_on": release["published_on"], "file_sha256": header["file_sha256"],
                   "items": len(records), "requests": 1, "evidence_origin": origin,
                   "final_page": index + 1 >= len(documents)}
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


ADAPTERS = {CONNECTOR: TaxTransparencyAdapter}


def _fixture_key(url: str, params: Any) -> str:
    pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
    query = urlencode(sorted((str(k), str(v)) for k, v in pairs))
    return urlsplit(url).path + ("?" + query if query else "")


def fixture_request(document: Mapping[str, Any]) -> str:
    """The key :func:`fixture_transport` files a response under (path and sorted query)."""
    base, _, query = document_url(document).partition("?")
    return _fixture_key(base, parse_qsl(query, keep_blank_values=True))


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path and sorted query."""
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
                "content": content, "origin": "fixture",
                **({"final_url": page["final_url"]} if page.get("final_url") else {})}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = TaxTransparencyAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
    records, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


def coverage_report() -> dict[str, Any]:
    return {
        "providers": {p: {"status": c["status"], "delivers": c["delivers"], "nature": c["nature"],
                          "personal_data": c["personal_data"]} for p, c in PROVIDER_CONTRACTS.items()},
        "bounded_coverage": BOUNDED_COVERAGE,
        "not_implemented": list(NOT_IMPLEMENTED),
        "reference_only": list(REFERENCE_ONLY),
    }


__all__ = [
    "ADAPTERS",
    "BOUNDED_COVERAGE",
    "CAPS",
    "CONCEPTS",
    "CONNECTOR",
    "DATASETS",
    "EXCLUSIONS",
    "FIXTURE_SECRET",
    "FORMATS",
    "FREQUENCIES",
    "LIVE_VERIFICATION",
    "NATURES",
    "NEVER_SENTENCE",
    "NOT_IMPLEMENTED",
    "NOT_IMPLEMENTED_ANSWER",
    "NOT_IMPLEMENTED_CONCEPTS",
    "PROVIDERS",
    "PROVIDER_CONTRACTS",
    "REFERENCE_CAP",
    "REFERENCE_ONLY",
    "STATUSES",
    "TaxFormatError",
    "TaxTransparencyAdapter",
    "check_document",
    "coverage_report",
    "document_key",
    "document_url",
    "fixture_request",
    "fixture_transport",
    "parse_table",
    "period_matches",
    "replay_native_fixture",
    "series_key",
    "tax_declaration",
    "unverified",
]
