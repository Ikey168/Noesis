"""Industry and business statistics sources for the Economics ``economics.business`` provider (#2738, IB03-IB05).

The machine-readable copy of ``docs/development/business-statistics-evidence/source-audit.md`` (IB01). One native
source-pack connector, ``business-statistics``, driven by :mod:`src.ingestion.source_pack_runtime` with three sources
of the ``economic-statistics-and-filings`` pack (``config/source_packs/economic.json``). Each source declares one
provider and a bounded list of documents; the adapter fetches one declared document per page and returns one
*release* (a header and one item per series) that :class:`src.kb.business_statistics_store.BusinessStatisticsProjector`
appends as series vintages.

* **Eurostat short-term business statistics** (``eurostat-sts``, format ``eurostat-sts-sdmx-csv``) - monthly
  production indices (``sts_inpr_m``) through the existing :class:`~src.ingestion.connectors.dataset.sdmx.SDMXConnector`
  (provider ``ESTAT``, SDMX-CSV). The ``LAST UPDATE`` column dates the release; ``OBS_FLAG`` letters are kept verbatim
  per value; the seasonal and calendar adjustment (``s_adj``: ``NSA``, ``CA``, ``SCA``) and the index base year
  (``unit``: ``I21`` = 2021=100) are part of the series key, so adjusted and unadjusted series and a rebased index are
  different series. Nothing is re-based or adjusted here.
* **Eurostat business demography** (``eurostat-business-demography``, format ``eurostat-bd-sdmx-csv``) - active
  enterprises, births and deaths by NACE Rev.2 and size class through the same connector. Deaths of the latest years
  are provisional until confirmed; each later release is a new vintage.
* **US Census County Business Patterns** (``us-census-cbp``, format ``census-cbp-json``) - establishments, mid-March
  employment and annual payroll by NAICS for a state from the Census Data API (JSON array of arrays), one document per
  reference year. The optional key ``NOESIS_CENSUS_API_KEY`` is sent as ``key`` and never recorded in a URL, receipt,
  header or record. Noise flags (``EMP_N``, ``PAYANN_N``) are kept verbatim per value and a noise-infused value is
  never exact; a withheld cell keeps its status and carries no value (never a zero); nothing is reconstructed from
  other cells. The NAICS variable of each year (``NAICS2017``, ``NAICS2022``) is the classification vintage.

Every provider is ``unverified-live`` until a dated live run (IB13); endpoints, dataset codes, dimension orders,
variable names and flag letters marked *verify* come from the publishers' documentation, not from a live response.
Nothing here nowcasts, fills a period, re-bases an index, adjusts a series, blends Eurostat and Census figures,
reconstructs a suppressed cell or derives a rate, share or per-establishment figure.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

CONNECTOR = "business-statistics"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-business-statistics-release-v1"
AUDIT = "docs/development/business-statistics-evidence/source-audit.md"
SECRET_REF = "NOESIS_CENSUS_API_KEY"
# A placeholder the offline fixtures send so the key path is exercised; it must never appear in any record.
FIXTURE_SECRET = "fixture-census-key-not-a-real-key"
PROVIDERS = ("eurostat-sts", "eurostat-business-demography", "us-census-cbp")
FORMATS = {
    "eurostat-sts-sdmx-csv": {"provider": "eurostat-sts"},
    "eurostat-bd-sdmx-csv": {"provider": "eurostat-business-demography"},
    "census-cbp-json": {"provider": "us-census-cbp"},
}
PROVIDER_HOSTS = {"eurostat-sts": "ec.europa.eu", "eurostat-business-demography": "ec.europa.eu",
                  "us-census-cbp": "api.census.gov"}
SDMX_FORMATS = ("eurostat-sts-sdmx-csv", "eurostat-bd-sdmx-csv")
NEVER_SENTENCE = (
    "Published industry and business statistics as each source released them: Eurostat and Census figures side by "
    "side with their own statistical units, classifications and definitions, never blended; no nowcast, no filled "
    "period, no re-based index, no own seasonal adjustment, no reconstructed suppressed cell and no derived figure."
)
EXCLUSIONS = (
    "nowcasting business indicators",
    "filling periods a source did not publish",
    "re-basing indices to another base year",
    "seasonal or calendar adjustment of our own",
    "blending Eurostat and Census figures",
    "reconstructing suppressed, withheld or noise-infused cells",
    "rates, shares or per-establishment figures of our own",
    "derived indicators",
    "forecasts",
    "business-register or survey microdata and single-business figures",
)
CONCEPTS = (
    "production_index",
    "turnover_index",
    "active_enterprises",
    "enterprise_births",
    "enterprise_deaths",
    "enterprise_survivals",
    "birth_rate",
    "death_rate",
    "establishments",
    "employment",
    "annual_payroll",
    "first_quarter_payroll",
)
ADJUSTMENTS = ("NSA", "CA", "SCA", "SA", "not_applicable")
STATUSES = ("reported", "confidential", "withheld", "not_published")
STATISTICAL_UNITS = ("kind-of-activity-unit", "enterprise", "establishment")
CLASSIFICATION_SCHEMES = {"NACE": ("Rev.2",), "NAICS": ("2017", "2022")}
# Hard ceilings the adapter enforces on top of the source-pack budgets (IB01 bounded first coverage).
CAPS = {
    "eurostat-sts": {"documents": 1, "series_per_response": 10, "months": 36},
    "eurostat-business-demography": {"documents": 1, "series_per_response": 20, "years": 10},
    "us-census-cbp": {"documents": 2, "rows_per_response": 50, "years": 2},
}
BOUNDED_COVERAGE = {
    "eurostat-sts": {
        "places": {"Germany": "geo DE"},
        "series": "sts_inpr_m production (indic_bt PROD), NACE Rev.2 B-D and C, s_adj SCA and NSA, current base year "
                  "(I21; the key's unit position is open so a rebase is seen, never re-based)",
        "periods": "at most 36 months from a declared start period",
        "caps": "1 document, 10 series per response",
    },
    "eurostat-business-demography": {
        "places": {"Germany": "geo DE"},
        "series": "active enterprises, births and deaths, total business economy (B-S_X_O_S94), all size classes",
        "periods": "from a declared start year",
        "caps": "1 document, 20 series per response",
    },
    "us-census-cbp": {
        "places": {"California": "state 06"},
        "series": "ESTAB, EMP and PAYANN with their noise flags for NAICS 00 (total) and 31-33 (manufacturing), all "
                  "establishment sizes (EMPSZES 001)",
        "periods": "the two most recent reference years, one document per year",
        "caps": "2 documents, 50 rows per response",
    },
    "justification": "Germany and California are the labour track's places, so economics.labour links and the labour "
                     "track's operator concordance import (LB07) apply unchanged; manufacturing appears in all three "
                     "sources; economics.trade flows for the same place are linked by place and citation only",
    "record_cap": "max_results series per response and max_pages documents per run; a larger release is refused, "
                  "never truncated",
}
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "eurostat-sts": {
        "publisher": "Eurostat, short-term business statistics (European Business Statistics Regulation (EU) "
                     "2019/2152, verify)",
        "delivers": "monthly production and turnover indices for industry, construction, trade and services by NACE "
                    "Rev.2",
        "access": "api (Eurostat SDMX 2.1 dissemination API, SDMX-CSV through the existing SDMX connector, ESTAT "
                  "path); sts_inpr_m industrial production, sts_intv_m industry turnover (verify codes and dimension "
                  "order)",
        "entry_points": [("https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{dataset}/{key}"
                          "?format=SDMX-CSV&startPeriod=... (verify)")],
        "authentication": "none",
        "key_handling": "no key; nothing secret is sent or stored",
        "licence": "Eurostat reuse policy (Commission Decision 2011/833/EU): reuse with acknowledgement (verify)",
        "redistribution": "attribution-required",
        "rate_limits": "no published quota (verify); one request per declared document",
        "revision_model": "LAST UPDATE dates each release; every monthly release may revise earlier months and each "
                          "changed release is a new vintage; a new index base year (unit I21 replacing I15, verify) "
                          "is a different series, never re-based by us; OBS_FLAG (p, e, b, c, verify) kept per value",
        "definitions": "indicator (indic_bt), NACE Rev.2 aggregate (B-D, C), unit and index base year, seasonal and "
                       "calendar adjustment (s_adj NSA, CA, SCA) as published; adjusted and unadjusted series are "
                       "different series",
        "statistical_unit": "kind-of-activity unit as the STS methodology states (verify)",
        "personal_data": "none: published aggregates only",
        "unavailable_fallback": "a failed document fails the run with its code and a receipt; earlier vintages stay "
                                "current and nothing is marked removed or revised; readiness reports the source stale",
        "status": "unverified-live",
        "verify": ["dataset codes and dimension order", "OBS_FLAG letters", "the current base-year unit code",
                   "LAST UPDATE format", "the EBS Regulation reference"],
    },
    "eurostat-business-demography": {
        "publisher": "Eurostat, business demography",
        "delivers": "active enterprises, births, deaths and survivals, with Eurostat-published rates, by NACE Rev.2 "
                    "and size class",
        "access": "api (same SDMX-CSV path); bd_9bd_sz_cl_r2 or its EBS successor (the dataset codes changed with "
                  "the EBS Regulation, verify the current code)",
        "entry_points": [("https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{dataset}/{key}"
                          "?format=SDMX-CSV&startPeriod=... (verify)")],
        "authentication": "none",
        "key_handling": "no key; nothing secret is sent or stored",
        "licence": "Eurostat reuse policy (Commission Decision 2011/833/EU): reuse with acknowledgement (verify)",
        "redistribution": "attribution-required",
        "rate_limits": "no published quota (verify); one request per declared document",
        "revision_model": "annual, published about two years after the reference year (verify); deaths are confirmed "
                          "only after two years without reactivation, so the latest years' deaths are provisional "
                          "and revised in later releases, each a new vintage; flags as STS",
        "definitions": "Eurostat-OECD business demography definitions of active enterprise, birth, death and "
                       "survival; employer or all enterprises; size class; Eurostat-published rates stored as "
                       "published",
        "statistical_unit": "enterprise",
        "personal_data": "none: published aggregates only",
        "unavailable_fallback": "as Eurostat STS",
        "status": "unverified-live",
        "verify": ["the current dataset code", "indic_sb codes", "dimension order", "publication lag"],
    },
    "us-census-cbp": {
        "publisher": "US Census Bureau, County Business Patterns",
        "delivers": "establishments, mid-March employment, first-quarter and annual payroll by NAICS for nation, "
                    "state and county",
        "access": "api (Census Data API, JSON array of arrays, /data/{year}/cbp; verify path, variables and NAICS "
                  "vintage per year)",
        "entry_points": [("https://api.census.gov/data/{year}/cbp?get=NAME,ESTAB,EMP,EMP_N,PAYANN,PAYANN_N"
                          "&for=state:06&NAICS2017=31-33&key=... (verify variable names, the noise and flag "
                          "variables and the NAICS variable for each year)")],
        "authentication": f"optional API key: optional secret {SECRET_REF}, resolved by the source-pack runtime's "
                          "secret resolver and sent as key",
        "key_handling": "the key is never recorded in evidence, URLs, receipts or records; without it a lower daily "
                        "quota applies (verify)",
        "licence": "US government works (public domain, verify); the Data API terms of service ask applications to "
                   "state that they use the Census Bureau Data API without endorsement (verify the wording)",
        "redistribution": "public domain with the Data API non-endorsement statement",
        "rate_limits": "about 500 queries per IP per day without a key (verify); one request per declared document",
        "revision_model": "one annual release per reference year, dated by the declared release date, else the "
                          "retrieval time (labelled retrieval_time); a later correction of a published year is a new "
                          "vintage; a NAICS revision (2017 to 2022, verify the first year) is a different "
                          "classification key, linked only through Census's published concordance",
        "disclosure_protection": "since reference year 2017 employment and payroll are protected by noise infusion "
                                 "with a noise flag per value (EMP_N, PAYANN_N: low, moderate or high noise, verify "
                                 "letters); cells with too few establishments are withheld (verify the rule and the "
                                 "marker); earlier years used cell suppression with employment-size range flags "
                                 "(EMP_F, verify). Every flag is stored verbatim; a withheld cell is never a zero; a "
                                 "flagged value is never exact; nothing is reconstructed from other cells",
        "definitions": "establishment (not enterprise) as the unit; employment in the pay period including March 12; "
                       "payroll in thousands of dollars (verify); the NAICS vintage; CBP's scope exclusions "
                       "(self-employed, private households, government, most agriculture, verify) as a definition "
                       "note",
        "statistical_unit": "establishment",
        "personal_data": "none: published aggregates only; ZIP-code Business Patterns and Nonemployer Statistics are "
                         "not audited and not acquired",
        "unavailable_fallback": "a failed document (HTTP error, redirect to another host, schema drift, an "
                                "over-budget response, a missing optional key with the keyless quota exhausted) "
                                "fails the run with its code and a receipt; earlier vintages stay current",
        "status": "unverified-live",
        "verify": ["path and variables per year", "noise-flag letters", "withheld marker",
                   "EMPSZES code for all establishments", "repeated NAICS predicate", "keyless quota",
                   "terms wording"],
    },
}
LIVE_VERIFICATION = {
    provider: {"status": "unverified-live", "checked": None, "evidence": None,
               "intended": "verified-live after a dated bounded run (IB13, #2738)",
               "note": "no dated live run from this runtime; authored offline fixtures only"}
    for provider in PROVIDERS
}
DEFINITION_NOTES = {
    "comparability": "Eurostat enterprises (and STS kind-of-activity units) and Census establishments are different "
                     "statistical units; NACE and NAICS are mapped only through published concordances, each mapping "
                     "marked exact, partial or one-to-many; CBP employment and BLS or Eurostat employment series in "
                     "economics.labour are different series, linked by place and citation only",
}
# Eurostat OBS_FLAG letters (verify against the live code list).
FLAG_MEANINGS = {
    "b": "break in time series", "p": "provisional", "e": "estimated", "c": "confidential", "u": "low reliability",
    "d": "definition differs", "s": "Eurostat estimate", "z": "not applicable", "n": "not significant",
}
BREAK_FLAGS = {"b"}
CONFIDENTIAL_FLAGS = {"c"}
# CBP noise flags and withheld markers (Census CBP flag documentation; verify the letters before a live run).
CBP_NOISE_FLAGS = {"G": "low noise (less than 2 %)", "H": "moderate noise (2 % to less than 5 %)",
                   "J": "high noise (5 % or more)"}
CBP_WITHHELD_FLAGS = {"D": "withheld to avoid disclosing data for individual companies",
                      "S": "withheld because the estimate did not meet publication standards",
                      "N": "not available or not comparable"}
CBP_VARIABLES = {
    "ESTAB": {"concept": "establishments", "label": "Number of establishments", "flag": None,
              "unit": {"code": "ESTAB", "label": "establishments"}},
    "EMP": {"concept": "employment", "label": "Number of employees (pay period including March 12)",
            "flag": "EMP_N", "unit": {"code": "PERSONS", "label": "employees"}},
    "PAYANN": {"concept": "annual_payroll", "label": "Annual payroll", "flag": "PAYANN_N",
               "unit": {"code": "USD_THS", "label": "thousands of US dollars (verify)"}},
}
NAICS_VARIABLES = {"NAICS2017": "2017", "NAICS2022": "2022"}
_INDEX_UNIT = re.compile(r"^I(\d{2})$")


class BusinessFormatError(ValueError):
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
    if raw is None or raw in {":", "NaN", "nan", "null", "None"}:
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


def normalise_period(period: str) -> str:
    """SDMX period codes in the ``dataset-series-v1`` forms (``2096``, ``2096-01``)."""
    raw = str(period).strip()
    match = re.fullmatch(r"(\d{4})-?M(\d{2})", raw)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return raw


def index_base_year(unit_code: str | None) -> str | None:
    """``I21`` -> ``2021`` (Eurostat index units are I + two-digit base year; verify)."""
    match = _INDEX_UNIT.fullmatch(str(unit_code or ""))
    return f"20{match.group(1)}" if match else None


def series_key(item: Mapping[str, Any]) -> dict[str, Any]:
    """The fields that make a series (never a release, so each release of the same key is a vintage)."""
    classification = dict(item.get("classification") or {})
    unit = dict(item.get("unit") or {})
    return {
        "provider": item["provider"],
        "dataset": item["dataset"],
        "indicator": dict(item["indicator"]).get("code"),
        "concept": dict(item["indicator"]).get("concept"),
        "classification": {k: classification.get(k) for k in ("scheme", "version", "code")},
        "size_class": dict(item.get("size_class") or {}).get("code"),
        "area": {"scheme": item["area"]["scheme"], "code": str(item["area"]["code"])},
        "adjustment": item.get("adjustment"),
        "unit": {"code": unit.get("code"), "base_year": unit.get("base_year")},
        "frequency": item.get("frequency"),
    }


# ------------------------------------------------------------------ declarations


def business_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    config = dict(source.get("business_statistics") or {})
    provider, fmt = config.get("provider"), config.get("format")
    if provider not in PROVIDERS or FORMATS.get(str(fmt), {}).get("provider") != provider:
        raise SourcePackError("invalid_manifest",
                              "business-statistics sources declare a known provider and its runtime format")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host != PROVIDER_HOSTS[provider]:
        raise SourcePackError("invalid_manifest", f"{provider} documents are fetched from {PROVIDER_HOSTS[provider]}")
    auth = dict(source.get("auth") or {})
    if provider == "us-census-cbp":
        if auth.get("kind") not in {"none", "optional-secret"} or (
                auth.get("kind") == "optional-secret" and auth.get("secret_ref") != SECRET_REF):
            raise SourcePackError("invalid_manifest", f"CBP uses the optional secret {SECRET_REF} or no key")
    elif auth.get("kind") != "none":
        raise SourcePackError("invalid_manifest", "Eurostat dissemination requests carry no credential")
    documents = [dict(d) for d in config.get("documents") or []]
    cap = CAPS[provider]["documents"]
    if not 1 <= len(documents) <= cap:
        raise SourcePackError("invalid_manifest", f"{provider} declares 1-{cap} documents")
    if len(documents) > int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "more declared requests than the source's page budget")
    urls = []
    for document in documents:
        try:
            check_document(str(fmt), document)
            url = document_url(str(fmt), document)
        except (BusinessFormatError, ValueError) as exc:
            raise SourcePackError("invalid_manifest", f"{document.get('label')}: {exc}") from exc
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError("invalid_manifest", "declared documents are HTTPS resources on the endpoint's host")
        urls.append(url)
    if len(set(urls)) != len(urls):
        raise SourcePackError("invalid_manifest", "each declared document is a distinct request")
    if provider == "us-census-cbp" and len({d["year"] for d in documents}) != len(documents):
        raise SourcePackError("invalid_manifest", "CBP declares one document per reference year")
    return {"provider": provider, "format": fmt, "namespace": config.get("namespace") or "global",
            "documents": documents, "live_verification": config.get("live_verification") or "unverified-live"}


def _months_between(start: str, end: str) -> int:
    first, last = (int(start[:4]) * 12 + int(start[5:7]), int(end[:4]) * 12 + int(end[5:7]))
    return last - first + 1


def check_document(fmt: str, document: Mapping[str, Any]) -> None:
    for key in ("label", "definition", "references", "statistical_unit"):
        if not document.get(key):
            raise BusinessFormatError("invalid_document", f"a document states its {key}")
    if document["statistical_unit"] not in STATISTICAL_UNITS:
        raise BusinessFormatError("invalid_document", f"statistical_unit is one of {STATISTICAL_UNITS}")
    release = document.get("release")
    if release is not None and iso_day(dict(release).get("published_on")) is None:
        raise BusinessFormatError("invalid_document", "a declared release states its publication date")
    for ref in document.get("references") or []:
        if not text(dict(ref).get("identifier")) or not text(dict(ref).get("kind")):
            raise BusinessFormatError("invalid_document", "each reference states its kind and identifier as published")
    if fmt == "census-cbp-json":
        if not re.fullmatch(r"\d{4}", str(document.get("year") or "")):
            raise BusinessFormatError("invalid_document", "a CBP document names its reference year")
        if document.get("naics_variable") not in NAICS_VARIABLES:
            raise BusinessFormatError("invalid_document", f"the NAICS variable is one of {sorted(NAICS_VARIABLES)}")
        codes = list(document.get("naics_codes") or [])
        if not codes or "*" in codes or len(set(codes)) != len(codes):
            raise BusinessFormatError("unbounded_document", "a CBP document names its NAICS codes, never all")
        geography = dict(document.get("geography") or {})
        if not re.fullmatch(r"state:\d{2}", str(geography.get("for") or "")) or geography.get("scheme") != \
                "us-fips-state":
            raise BusinessFormatError("unbounded_document", "a CBP document names one state (for=state:NN)")
        if not set(document.get("variables") or []) <= set(CBP_VARIABLES) or not document.get("variables"):
            raise BusinessFormatError("invalid_document", f"CBP variables are among {sorted(CBP_VARIABLES)}")
        size = dict(document.get("size_class") or {})
        if size.get("variable") != "EMPSZES" or not size.get("code"):
            raise BusinessFormatError("invalid_document", "a CBP document pins the establishment size class (EMPSZES)")
        return
    if not document.get("flow") or not document.get("key"):
        raise BusinessFormatError("unbounded_document", "SDMX documents name a dataset and a series key")
    params = dict(document.get("params") or {})
    if not params.get("startPeriod"):
        raise BusinessFormatError("unbounded_document", "SDMX documents pin a start period")
    if fmt == "eurostat-sts-sdmx-csv":
        if not params.get("endPeriod") or not re.fullmatch(r"\d{4}-\d{2}", str(params["startPeriod"])) or \
                not re.fullmatch(r"\d{4}-\d{2}", str(params["endPeriod"])):
            raise BusinessFormatError("unbounded_document", "STS documents pin monthly start and end periods")
        if not 1 <= _months_between(str(params["startPeriod"]), str(params["endPeriod"])) <= \
                CAPS["eurostat-sts"]["months"]:
            raise BusinessFormatError("unbounded_document", "STS documents request at most 36 months")
        if not dict(document.get("adjustment") or {}).get("dimension"):
            raise BusinessFormatError("invalid_document", "STS documents name the s_adj dimension")
    indicators = dict(dict(document.get("indicator") or {}).get("codes") or {})
    if not dict(document.get("indicator") or {}).get("dimension") or not indicators:
        raise BusinessFormatError("invalid_document", "SDMX documents map the indicator dimension's codes")
    for spec in indicators.values():
        if dict(spec).get("concept") not in CONCEPTS:
            raise BusinessFormatError("invalid_document", f"indicator concept is one of {CONCEPTS}")
    classification = dict(document.get("classification") or {})
    if not classification.get("dimension") or classification.get("scheme") != "NACE" or \
            classification.get("version") != "Rev.2":
        raise BusinessFormatError("invalid_document", "Eurostat documents classify by NACE Rev.2")
    area = dict(document.get("area") or {})
    if not area.get("dimension") or area.get("scheme") != "eurostat-geo":
        raise BusinessFormatError("invalid_document", "Eurostat documents name the geo dimension")


def document_key(provider: str, document: Mapping[str, Any]) -> str:
    """Identity of a declared document across releases (removal detection compares releases of one document)."""
    if provider == "us-census-cbp":
        return (f"cbp:{document['year']}:{document['geography']['for']}:{document['naics_variable']}:"
                f"{','.join(sorted(document['naics_codes']))}:{document['size_class']['code']}")
    return f"{provider}:{document['flow']}:{document['key']}"


def _cbp_params(document: Mapping[str, Any], secret: str | None) -> list[tuple[str, str]]:
    variables = ["NAME", *[v for v in CBP_VARIABLES if v in document["variables"]]]
    variables += [CBP_VARIABLES[v]["flag"] for v in CBP_VARIABLES if v in document["variables"]
                  and CBP_VARIABLES[v]["flag"]]
    pairs = [("get", ",".join(variables)), ("for", str(document["geography"]["for"])),
             (str(document["size_class"]["variable"]), str(document["size_class"]["code"]))]
    pairs += [(str(document["naics_variable"]), str(code)) for code in document["naics_codes"]]
    if secret:
        pairs.append(("key", secret))
    return sorted(pairs)


def document_url(fmt: str, document: Mapping[str, Any], *, secret: str | None = None) -> str:
    """The request URL; the Census key is only added for the transport (never recorded)."""
    if fmt == "census-cbp-json":
        return f"https://api.census.gov/data/{document['year']}/cbp?" + urlencode(_cbp_params(document, secret))
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector

    url, query = SDMXConnector("ESTAT").csv_url(str(document["flow"]), str(document["key"]),
                                                dict(document.get("params") or {}))
    return url + "?" + urlencode(sorted(query.items()))


def public_url(url: str) -> str:
    """A request URL without the Census key."""
    base, _, query = url.partition("?")
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k.casefold() != "key"]
    return base + ("?" + urlencode(kept) if kept else "")


# ------------------------------------------------------------------ parsing


def _definition(document: Mapping[str, Any], provider: str, **extra: Any) -> dict[str, Any]:
    definition = dict(document["definition"])
    return {
        "provider": provider,
        "statistical_unit": document["statistical_unit"],
        "source_text": text(definition.get("source_text")),
        "scope": text(definition.get("scope")),
        "methodology_notes": [str(n) for n in definition.get("methodology_notes") or []],
        "comparability": DEFINITION_NOTES["comparability"],
        "references": [dict(r) for r in document.get("references") or []],
        **extra,
    }


def _declared_release(document: Mapping[str, Any]) -> tuple[str | None, str | None]:
    release = dict(document.get("release") or {})
    return iso_day(release.get("published_on")), text(release.get("label"))


def _labelled(spec: Mapping[str, Any], code: str) -> dict[str, Any]:
    label = dict(spec.get("labels") or {}).get(code)
    return {"label": label} if label else {}


def parse_sdmx(raw: bytes, *, fmt: str, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    """Declared Eurostat SDMX-CSV series as items; flags verbatim, adjustment and base year in the key."""
    from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector
    from src.integrations.common import IntegrationError

    provider = FORMATS[fmt]["provider"]
    connector = SDMXConnector("ESTAT")
    ref = SeriesRef(locator=f"{document['flow']}/{document['key']}", metadata={"flow": str(document["flow"])},
                    title=document.get("label"))
    try:
        records = connector.parse_csv(RawSeries(ref, raw, content_type="text/csv", source_url=url, fetched_at=0))
    except IntegrationError as exc:
        raise BusinessFormatError("schema_drift", f"{exc.code}: {exc}") from exc
    if not records:
        raise BusinessFormatError("schema_drift", "the response states no series")
    if len(records) > CAPS[provider]["series_per_response"]:
        raise BusinessFormatError("budget_exhausted", "the response has more series than the audited cap")
    indicator_spec = dict(document["indicator"])
    classification_spec = dict(document["classification"])
    area_spec = dict(document["area"])
    adjustment_spec = dict(document.get("adjustment") or {})
    size_spec = dict(document.get("size_class") or {})
    unit_spec = dict(document.get("unit") or {})
    items, flows, last_update = [], set(), None
    for record in records:
        meta = record.metadata
        dims = {str(k): str(v) for k, v in dict(meta["dimensions"]).items()}
        flows.add(meta.get("dataflow"))
        last_update = meta.get("provider_last_update_at") or last_update
        for role, spec in (("indicator", indicator_spec), ("classification", classification_spec),
                           ("area", area_spec)):
            if spec["dimension"] not in dims:
                raise BusinessFormatError("schema_drift", f"the response lacks the declared {role} dimension")
        indicator_code = dims[indicator_spec["dimension"]]
        indicator = dict(indicator_spec["codes"]).get(indicator_code)
        if indicator is None:
            raise BusinessFormatError("schema_drift", f"unmapped indicator code {indicator_code!r}")
        if adjustment_spec.get("dimension"):
            adjustment = dims.get(adjustment_spec["dimension"])
            if adjustment not in ADJUSTMENTS:
                raise BusinessFormatError("schema_drift", f"unmapped adjustment code {adjustment!r}")
        else:
            adjustment = "not_applicable"
        if size_spec.get("dimension"):
            size_code = dims.get(size_spec["dimension"])
            if size_code is None:
                raise BusinessFormatError("schema_drift", "the response lacks the declared size-class dimension")
            size_class = {"code": size_code, **_labelled(size_spec, size_code)}
        else:
            size_class = {"code": str(size_spec.get("code") or "all"),
                          "label": size_spec.get("label") or "not broken down by size class"}
        if unit_spec.get("dimension"):
            unit_code = dims.get(unit_spec["dimension"])
            if unit_code is None:
                raise BusinessFormatError("schema_drift", "the response lacks the declared unit dimension")
            base_year = index_base_year(unit_code)
            unit = {"code": unit_code, "base_year": base_year,
                    "label": dict(unit_spec.get("labels") or {}).get(unit_code)
                    or (f"Index, {base_year}=100" if base_year else unit_code)}
        else:
            unit = {"code": str(dict(indicator.get("unit") or {}).get("code") or "NR"), "base_year": None,
                    "label": str(dict(indicator.get("unit") or {}).get("label") or "number")}
        class_code = dims[classification_spec["dimension"]]
        area_code = dims[area_spec["dimension"]]
        observations, breaks = [], []
        for observation in sorted(record.observations, key=lambda o: o.period):
            attributes = {str(k): str(v) for k, v in dict(meta["observation_attributes"].get(observation.period)
                                                         or {}).items()}
            value_text = meta["original_values"].get(observation.period)
            value = decimal_text(value_text)
            flag = attributes.get("OBS_FLAG") or ""
            letters = set(flag)
            status = "reported" if value is not None else (
                "confidential" if letters & CONFIDENTIAL_FLAGS else "not_published")
            period = normalise_period(observation.period)
            if letters & BREAK_FLAGS:
                breaks.append(period)
            observations.append({
                "period": period,
                "value_text": value_text if value_text not in (None, "") else None,
                "value": value if status == "reported" else None,
                "status": status,
                "flags": {"OBS_FLAG": flag} if flag else {},
                "flag_meanings": sorted({FLAG_MEANINGS.get(letter, letter) for letter in letters}),
                "attributes": {k: attributes[k] for k in sorted(attributes) if k != "OBS_FLAG"},
            })
        notes = []
        if breaks:
            notes.append({"kind": "break", "attribute": "OBS_FLAG", "value": "b", "periods": breaks,
                          "statement": f"{document['label']}: Eurostat flags a break in series (OBS_FLAG b)"})
        provisional = [o["period"] for o in observations if "p" in o["flags"].get("OBS_FLAG", "")]
        if provisional:
            notes.append({"kind": "provisional", "attribute": "OBS_FLAG", "value": "p", "periods": provisional,
                          "statement": f"{document['label']}: Eurostat marks these periods provisional"})
        items.append({
            "provider": provider,
            "dataset": str(document["flow"]),
            "native_key": ".".join(dims[k] for k in dims),
            "dataflow": {"reference": str(document["flow"]), "stated": meta.get("dataflow")},
            "indicator": {"code": indicator_code, "concept": indicator["concept"],
                          "label": indicator.get("label") or indicator_code},
            "classification": {"scheme": "NACE", "version": "Rev.2", "code": class_code, "native": class_code,
                               **_labelled(classification_spec, class_code)},
            "size_class": size_class,
            "area": {"scheme": "eurostat-geo", "code": area_code, **_labelled(area_spec, area_code)},
            "adjustment": adjustment,
            "unit": unit,
            "statistical_unit": document["statistical_unit"],
            "frequency": record.frequency,
            "dimensions": dims,
            "definition": _definition(document, provider, indicator=indicator_code, adjustment=adjustment,
                                      unit=unit, classification="NACE Rev.2"),
            "references": [dict(r) for r in document.get("references") or []],
            "source_notes": notes,
            "observations": observations,
        })
    items.sort(key=lambda i: i["native_key"])
    stated = sorted(f for f in flows if f)
    flow_version = None
    if stated:
        match = re.search(r"\((\d+(?:\.\d+)*)\)\s*$", stated[0])
        flow_version = match.group(1) if match else None
    declared_on, declared_label = _declared_release(document)
    if last_update:
        stamp = datetime.fromisoformat(last_update)
        stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
        published_on, published_at, basis = stamp.date().isoformat(), stamp.isoformat(), "provider_last_update"
    elif declared_on:
        published_on, published_at, basis = declared_on, None, "declared_release"
    else:
        published_on, published_at, basis = None, None, "retrieval_time"
    return {"items": items, "published_on": published_on, "published_at": published_at, "release_basis": basis,
            "release_label": declared_label or (f"LAST UPDATE {last_update}" if last_update else None),
            "dataflow_version": flow_version}


def parse_cbp(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    """One CBP year response (JSON array of arrays) as series items: flags verbatim, withheld cells without value."""
    try:
        rows = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BusinessFormatError("schema_drift", "CBP response is not JSON") from exc
    if not isinstance(rows, list) or not rows or not all(isinstance(r, list) for r in rows):
        raise BusinessFormatError("schema_drift", "CBP response is not an array of arrays")
    header, body = [str(c) for c in rows[0]], rows[1:]
    if len(body) > CAPS["us-census-cbp"]["rows_per_response"]:
        raise BusinessFormatError("budget_exhausted", "CBP response has more rows than the audited cap")
    naics_variable = str(document["naics_variable"])
    size_variable = str(document["size_class"]["variable"])
    required = {naics_variable, "state", *document["variables"]}
    required |= {CBP_VARIABLES[v]["flag"] for v in document["variables"] if CBP_VARIABLES[v]["flag"]}
    missing = sorted(required - set(header))
    if missing:
        raise BusinessFormatError("schema_drift", f"CBP response lacks columns {missing}")
    if len(set(header)) != len(header):
        raise BusinessFormatError("schema_drift", "CBP response repeats a column")
    geography = dict(document["geography"])
    year = str(document["year"])
    groups: dict[str, dict[str, Any]] = {}
    seen = set()
    for row in body:
        if len(row) != len(header):
            raise BusinessFormatError("schema_drift", "a CBP row has another shape than the header")
        cells = dict(zip(header, [None if c is None else str(c) for c in row]))
        if cells["state"] != geography["code"]:
            raise BusinessFormatError("schema_drift", "CBP answered for another state than requested")
        naics = str(cells[naics_variable])
        if naics not in document["naics_codes"]:
            raise BusinessFormatError("schema_drift", f"CBP answered for an undeclared NAICS code {naics!r}")
        size_code = cells.get(size_variable) or str(document["size_class"]["code"])
        if size_code != str(document["size_class"]["code"]):
            raise BusinessFormatError("schema_drift", "CBP answered for another establishment size class")
        if (naics, size_code) in seen:
            raise BusinessFormatError("schema_drift", "CBP states a NAICS code twice")
        seen.add((naics, size_code))
        for variable in document["variables"]:
            spec = CBP_VARIABLES[variable]
            value_text = cells.get(variable)
            flag_variable = spec["flag"]
            flag = text(cells.get(flag_variable)) if flag_variable else None
            flags = {flag_variable: flag} if flag_variable and flag else {}
            meanings = []
            if flag in CBP_WITHHELD_FLAGS:
                status, value = "withheld", None
                meanings.append(CBP_WITHHELD_FLAGS[flag])
            else:
                value = decimal_text(value_text)
                status = "reported" if value is not None else "not_published"
                if flag in CBP_NOISE_FLAGS:
                    meanings.append(CBP_NOISE_FLAGS[flag])
                elif flag:
                    meanings.append(f"flag {flag} as published (meaning not in the audited code list)")
            attributes: dict[str, Any] = {}
            if flag_variable and status == "reported":
                attributes = {"noise_infused": True, "exact": False,
                              "precision": "noise-infused value; never exact"} if int(year) >= 2017 else {}
            key = canonical([naics, size_code, variable])
            entry = groups.setdefault(key, {
                "provider": "us-census-cbp",
                "dataset": "cbp",
                "native_key": f"cbp:{geography['for']}:{naics_variable}={naics}:{size_variable}={size_code}:{variable}",
                "dataflow": {"reference": "cbp", "stated": f"/data/{year}/cbp"},
                "indicator": {"code": variable, "concept": spec["concept"], "label": spec["label"]},
                "classification": {"scheme": "NAICS", "version": NAICS_VARIABLES[naics_variable], "code": naics,
                                   "native": naics, "variable": naics_variable,
                                   **({"label": dict(document.get("naics_labels") or {})[naics]}
                                      if naics in dict(document.get("naics_labels") or {}) else {})},
                "size_class": {"code": size_code, "label": document["size_class"].get("label")},
                "area": {"scheme": "us-fips-state", "code": geography["code"],
                         "label": geography.get("label") or cells.get("NAME")},
                "adjustment": "not_applicable",
                "unit": {**spec["unit"], "base_year": None},
                "statistical_unit": document["statistical_unit"],
                "frequency": "annual",
                "dimensions": {"year": year, "for": geography["for"], naics_variable: naics,
                               size_variable: size_code, "variable": variable},
                "definition": _definition(document, "us-census-cbp", indicator=variable,
                                          classification=f"NAICS {NAICS_VARIABLES[naics_variable]}",
                                          disclosure_protection=PROVIDER_CONTRACTS["us-census-cbp"]
                                          ["disclosure_protection"]),
                "references": [dict(r) for r in document.get("references") or []],
                "source_notes": [],
                "observations": [],
            })
            entry["observations"].append({
                "period": year,
                "value_text": value_text,
                "value": value,
                "status": status,
                "flags": flags,
                "flag_meanings": meanings,
                "attributes": attributes,
            })
    items = sorted(groups.values(), key=lambda i: i["native_key"])
    if not items:
        raise BusinessFormatError("schema_drift", "CBP response states no rows")
    declared_on, declared_label = _declared_release(document)
    return {"items": items, "published_on": declared_on, "published_at": None,
            "release_basis": "declared_release" if declared_on else "retrieval_time",
            "release_label": declared_label, "dataflow_version": f"{naics_variable} ({year})"}


# ------------------------------------------------------------------ adapter


class BusinessStatisticsAdapter:
    """Fetch the declared documents on the runtime's default transport; one page (one release) per document."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = business_declaration(self.source)
        # Only the CBP source may carry the optional Census key; Eurostat requests never send a credential.
        self.secret = secret if self.declared["provider"] == "us-census-cbp" else None
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "business_statistics": {"provider": self.declared["provider"], "format": self.declared["format"],
                                    "documents": len(self.declared["documents"]), "caps": CAPS[
                                        self.declared["provider"]], "keyed": bool(self.secret),
                                    "live_verification": LIVE_VERIFICATION[self.declared["provider"]]["status"]},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "business-statistics runs fetch the declared documents only")

    def _get(self, url: str) -> tuple[bytes, str]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        host = PROVIDER_HOSTS[self.declared["provider"]]
        parts = urlsplit(url)
        if (parts.hostname or "").casefold() != host or parts.scheme != "https":
            raise SourcePackError("network_policy", "declared documents are fetched from the provider's host only")
        base, _, query = url.partition("?")
        response = self.transport(url=base, params=parse_qsl(query, keep_blank_values=True),
                                  headers={"Accept": "application/json, text/csv"},
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
        request_url = document_url(fmt, document, secret=self.secret)
        url = public_url(request_url)
        raw, origin = self._get(request_url)
        try:
            release = parse_cbp(raw, document=document) if fmt == "census-cbp-json" else parse_sdmx(
                raw, fmt=fmt, document=document, url=url)
        except BusinessFormatError as exc:
            raise SourcePackError("budget_exhausted" if exc.code == "budget_exhausted" else "schema_drift",
                                  f"{exc.code}: {exc}") from exc
        items = release["items"]
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(items) > limit:
            # Never a truncated release: a missing series would read as a series the source removed.
            raise SourcePackError("budget_exhausted", "release has more series than the run's result budget")
        header = {
            "contract": RELEASE_CONTRACT, "provider": provider, "format": fmt, "document": document,
            "document_key": document_key(provider, document), "published_on": release["published_on"],
            "published_at": release["published_at"], "release_basis": release["release_basis"],
            "release_label": release["release_label"], "dataflow_version": release["dataflow_version"],
            "file_sha256": hashlib.sha256(raw).hexdigest(), "content_sha256": digest(items),
            "item_count": len(items), "complete": True, "evidence_origin": origin,
            "live_verification": LIVE_VERIFICATION[provider]["status"], "url": url,
        }
        records = [{
            "id": f"{header['file_sha256'][:16]}:{number}",
            "title": f"{document['label']} ({release['published_on'] or 'retrieved'})",
            "url": url, "language": "en", "published_at": release["published_on"],
            "content": canonical(item), "business_release": header, "business_item": item,
        } for number, item in enumerate(items)]
        receipt = {"status": 200, "provider": provider, "document": document["label"],
                   "published_on": release["published_on"], "release_basis": release["release_basis"],
                   "file_sha256": header["file_sha256"], "items": len(records), "requests": 1,
                   "keyed": bool(self.secret), "evidence_origin": origin, "final_page": index + 1 >= len(documents)}
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


ADAPTERS = {CONNECTOR: BusinessStatisticsAdapter}


def _fixture_key(url: str, params: Any) -> str:
    pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
    pairs = [(str(k), str(v)) for k, v in pairs if str(k).casefold() != "key"]
    query = urlencode(sorted(pairs))
    return urlsplit(url).path + ("?" + query if query else "")


def fixture_request(fmt: str, document: Mapping[str, Any]) -> str:
    """The key :func:`fixture_transport` files a response under (the Census key is never part of it)."""
    base, _, query = document_url(fmt, document).partition("?")
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
                "content": content, "origin": "fixture"}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = BusinessStatisticsAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
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
    "BOUNDED_COVERAGE",
    "CAPS",
    "CBP_NOISE_FLAGS",
    "CBP_WITHHELD_FLAGS",
    "CONCEPTS",
    "CONNECTOR",
    "EXCLUSIONS",
    "FIXTURE_SECRET",
    "FORMATS",
    "LIVE_VERIFICATION",
    "NEVER_SENTENCE",
    "PROVIDERS",
    "PROVIDER_CONTRACTS",
    "SECRET_REF",
    "STATUSES",
    "BusinessFormatError",
    "BusinessStatisticsAdapter",
    "business_declaration",
    "document_key",
    "document_url",
    "fixture_request",
    "fixture_transport",
    "index_base_year",
    "parse_cbp",
    "parse_sdmx",
    "public_url",
    "replay_native_fixture",
    "series_key",
    "unverified",
]
