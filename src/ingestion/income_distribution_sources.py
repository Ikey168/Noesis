"""Income, poverty and inequality sources for the Society bundle's ``society.income`` provider (#2583).

IP01 (#2588) source contracts and IP03-IP05 (#2598, #2603, #2608) acquisition. One native source-pack connector,
``income-distribution``, driven by :mod:`src.ingestion.source_pack_runtime` with the ``society-income-distribution``
source pack (``config/source_packs/society.json``). Each source declares one provider and a bounded list of
documents; the adapter fetches one declared document per page and returns one *release* (a header and one item per
series) that :class:`src.kb.income_distribution_store.IncomeDistributionProjector` appends as series vintages.

* **World Bank PIP** (``pip``, format ``pip-json``) - the Poverty and Inequality Platform API (``/pip`` country
  estimates, ``/pip-grp`` regional aggregates). Each document pins a ``release_version``
  (``YYYYMMDD_<PPP year>_<revision>_PROD``) and a ``ppp_version``; a series is keyed by country or region,
  reporting level, welfare type (income or consumption), poverty line and its PPP base year. Survey-year estimates
  and PIP's interpolated or extrapolated reference-year (line-up) estimates are distinguished per value exactly as PIP
  labels them (``estimation_type``, ``is_interpolated``, ``survey_year``); nothing is filled here.
* **Eurostat EU-SILC** (``eurostat-silc``, format ``eurostat-silc-sdmx-csv``) - EU-SILC datasets (``ilc_li02``
  at-risk-of-poverty rate, ``ilc_di12`` Gini coefficient, ``ilc_di11`` S80/S20, ``ilc_di03`` mean and median
  equivalised net income, ``ilc_li01`` at-risk-of-poverty thresholds) through the existing SDMX connector's ESTAT
  SDMX-CSV path. The dataflow version and ``OBS_FLAG`` letters (``b`` break, ``p`` provisional, ``e`` estimated,
  ``u`` low reliability, ``c`` confidential) are stored per value; the at-risk-of-poverty threshold and the modified
  OECD equivalence scale are recorded as definitions; the survey year (``TIME_PERIOD``) and the income reference year
  (the declared offset Eurostat documents, with stated country exceptions) are kept distinct.
* **OECD Income Distribution Database** (``oecd-idd``, format ``oecd-idd-sdmx-csv``) - the IDD dataflow on the OECD
  Data Explorer through the same connector (provider ``OECD``). The income definition and the methodology version
  (the IDD terms of reference, e.g. the 2012 income definition) are part of every series key and definition; series
  breaks (``OBS_STATUS`` ``B``) become source-stated comparability notes. OECD series are never mixed with EU-SILC
  series even where the underlying survey is the same: provider and key differ, and the declared underlying survey is
  recorded as a note only.

Every provider is ``unverified-live`` until a dated live run (``docs/development/income-distribution-evidence/``);
endpoint, parameter and field names marked *verify* come from the providers' public documentation, not from a live
response. Nothing here nowcasts, fills a year, sets a poverty line of its own, blends PIP, EU-SILC and OECD figures
or re-harmonises welfare concepts.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

CONNECTOR = "income-distribution"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-income-distribution-release-v1"
AUDIT = "docs/development/income-distribution-evidence/source-audit.md"
FIXTURE_SECRET = None
PROVIDERS = ("pip", "eurostat-silc", "oecd-idd")
FORMATS = {
    "pip-json": {"provider": "pip"},
    "eurostat-silc-sdmx-csv": {"provider": "eurostat-silc"},
    "oecd-idd-sdmx-csv": {"provider": "oecd-idd"},
}
PROVIDER_HOSTS = {"pip": "api.worldbank.org", "eurostat-silc": "ec.europa.eu", "oecd-idd": "sdmx.oecd.org"}
SDMX_PROVIDERS = {"eurostat-silc": "ESTAT", "oecd-idd": "OECD"}
NEVER_SENTENCE = (
    "Published income, poverty and inequality figures as each source released them: PIP, EU-SILC and OECD side by "
    "side with their own definitions, never blended or re-harmonised; no nowcast, no filled year and no poverty line "
    "of our own."
)
EXCLUSIONS = (
    "nowcasting income, poverty or inequality figures",
    "filling years a source did not publish",
    "poverty lines of our own (only the lines a source publishes)",
    "blending PIP, EU-SILC and OECD figures into one series",
    "re-harmonising welfare concepts, equivalence scales or income definitions",
    "deriving indicators (rates, ratios, shares or counts) that no source published",
    "person-level or household-level microdata",
)
CONCEPTS = (
    "poverty_headcount",
    "poverty_gap",
    "gini",
    "quintile_share_ratio",
    "income_share",
    "mean_income",
    "median_income",
    "poverty_threshold",
)
WELFARE_CONCEPTS = ("income", "consumption", "mixed")
EQUIVALENCE_SCALES = ("per-capita", "modified-oecd", "square-root")
LINE_BASES = ("absolute-ppp", "relative-median", "relative-mean")
ESTIMATION_TYPES = ("survey", "interpolation", "extrapolation", "regional-line-up", "not-stated")
STATUSES = ("reported", "confidential", "not_published")
# Hard ceilings the adapter enforces on top of the source-pack budgets.
CAPS = {"documents": 12, "pip_rows": 400, "sdmx_series": 60, "years": 40}
BOUNDED_COVERAGE = {
    "pip": "Germany (DEU) national estimates and the Europe and Central Asia (ECA) regional aggregate at the "
           "international poverty line of the pinned PPP round, one release per document, every year PIP returns "
           "without fill_gaps beyond PIP's own line-up (fill_gaps is declared per document)",
    "eurostat-silc": "Germany (DE) country figures for ilc_li02 (60 % of median threshold, total population), "
                     "ilc_di12 (Gini), ilc_di11 (S80/S20), ilc_di03 (median equivalised net income) and ilc_li01 "
                     "(threshold, single person), from a declared start period",
    "oecd-idd": "Germany (DEU) Gini of disposable income and the relative poverty rate (50 % of median) under the "
                "current income definition and methodology, from a declared start period",
}
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "pip": {
        "publisher": "World Bank, Poverty and Inequality Platform (PIP)",
        "delivers": "country and regional poverty headcounts, poverty gaps, Gini, mean and median welfare and "
                    "decile shares at stated poverty lines and PPP rounds",
        "access": "api (PIP API v1: /pip/v1/pip country estimates, /pip/v1/pip-grp regional aggregates, "
                  "/pip/v1/versions release list; JSON via format=json) - verify",
        "entry_points": [
            ("https://api.worldbank.org/pip/v1/pip?country={ISO3}&year=all&povline={line}&ppp_version={2017|2021}"
             "&release_version={version}&fill_gaps={true|false}&format=json (verify)"),
            ("https://api.worldbank.org/pip/v1/pip-grp?country={REGION}&year=all&povline={line}&group_by=wb"
             "&ppp_version={...}&release_version={...}&format=json (verify)"),
            "https://api.worldbank.org/pip/v1/versions (verify)",
        ],
        "authentication": "none",
        "key_handling": "no key; nothing secret is stored or sent",
        "licence": "CC BY 4.0 (World Bank open data terms; verify) with the PIP citation and release version",
        "redistribution": "attribution-required; the release version is cited with every value",
        "rate_limits": "no published per-user quota (verify); each run requests only the declared documents, at "
                       "most one request per document",
        "identifiers": "country_code (ISO 3166-1 alpha-3), region_code (World Bank regions), reporting_level "
                       "(national/urban/rural), welfare_type, survey_acronym, survey_year, reporting_year",
        "revision_model": "a release_version (YYYYMMDD_<PPP year>_<PPP revision>_<adaptation>_PROD) is one vintage; "
                          "a release restating past values (new survey data, a PPP revision within the round) is a "
                          "new vintage, never an overwrite; a PPP round change is a different series (the poverty "
                          "line's PPP base year is part of the key)",
        "updates_corrections_removals": "PIP publishes complete releases; a series a later complete release no "
                                        "longer contains is recorded as removed in that release (a revision)",
        "personal_data": "none: aggregate estimates only; survey microdata are never requested",
        "status": "unverified-live",
    },
    "eurostat-silc": {
        "publisher": "Eurostat (EU statistics on income and living conditions, EU-SILC)",
        "delivers": "at-risk-of-poverty rates and thresholds, Gini coefficient, S80/S20 income quintile share ratio, "
                    "mean and median equivalised net income",
        "access": "api (Eurostat SDMX 2.1 dissemination API, SDMX-CSV via format=SDMX-CSV) through the SDMX connector",
        "entry_points": [
            ("https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{dataset}/{key}?format=SDMX-CSV"
             "&startPeriod={year} (verify dataset codes and key order)"),
        ],
        "authentication": "none",
        "key_handling": "no key",
        "licence": "Eurostat copyright and reuse policy (Commission Decision 2011/833/EU): reuse with "
                   "acknowledgement of the source (verify)",
        "redistribution": "attribution-required",
        "rate_limits": "no published per-user quota for the dissemination API (verify); asynchronous responses for "
                       "large extractions are out of scope (the declared keys are small)",
        "identifiers": "dataset code, SDMX key (freq, indic_il/statinfo, unit, age, sex, geo), geo as Eurostat GEO "
                       "codes (ISO alpha-2 except EL and UK, NUTS for regions)",
        "revision_model": "the SDMX-CSV LAST UPDATE column dates each release; a changed dataset is a new vintage, "
                          "the dataflow version and OBS_FLAG letters are kept per value",
        "updates_corrections_removals": "corrections are new releases (new LAST UPDATE); a series a complete later "
                                        "release no longer contains is recorded as removed in that release",
        "personal_data": "none: published aggregates; EU-SILC user microdata (UDB) are excluded",
        "status": "unverified-live",
    },
    "oecd-idd": {
        "publisher": "OECD (Income Distribution Database, IDD)",
        "delivers": "Gini coefficients, relative poverty rates at 50 % and 60 % of the median, income levels and "
                    "shares per income definition and methodology",
        "access": "api (OECD Data Explorer SDMX REST API, SDMX-CSV via format=csvfile) through the SDMX connector",
        "entry_points": [
            ("https://sdmx.oecd.org/public/rest/data/OECD.WISE.INE,DSD_WISE_IDD@DF_IDD,{version}/{key}"
             "?format=csvfile&startPeriod={year} (verify the dataflow id, version and dimension order)"),
        ],
        "authentication": "none",
        "key_handling": "no key",
        "licence": "OECD terms and conditions; OECD data are licensed under CC BY 4.0 since July 2024 (verify)",
        "redistribution": "attribution-required",
        "rate_limits": "about 20 data queries per minute per IP address (verify); each run requests only the "
                       "declared documents",
        "identifiers": "REF_AREA (ISO 3166-1 alpha-3), MEASURE, METHODOLOGY (income-definition terms of reference), "
                       "DEFINITION (current/previous), POVERTY_LINE, AGE (verify the dimension ids)",
        "revision_model": "the response carries no update stamp; a changed response is a new release dated by the "
                          "declared release date or the retrieval time (labelled); breaks (OBS_STATUS B) are "
                          "source-stated comparability notes",
        "updates_corrections_removals": "as Eurostat; a methodology change is a different series (the methodology "
                                        "is part of the key), a revised value is a new vintage",
        "personal_data": "none: published aggregates",
        "status": "unverified-live",
    },
}
LIVE_VERIFICATION = {
    provider: {"status": "unverified-live", "checked": None, "evidence": None,
               "note": "no dated live run yet (IP13, #2648); offline fixtures only"}
    for provider in PROVIDERS
}
# EU-SILC OBS_FLAG letters (verify against the live code list); OECD OBS_STATUS codes.
FLAG_ATTRIBUTES = ("OBS_FLAG", "OBS_STATUS", "CONF_STATUS")
BREAK_FLAGS = {"b", "B"}
CONFIDENTIAL_FLAGS = {"c", "C"}
FLAG_MEANINGS = {
    "b": "break in time series", "p": "provisional", "e": "estimated", "u": "low reliability", "c": "confidential",
    "d": "definition differs", "n": "not significant", "s": "Eurostat estimate", "z": "not applicable",
    "A": "normal value", "B": "break", "E": "estimated value", "P": "provisional value", "M": "missing value",
}
PIP_MEASURES = {
    "headcount": ("poverty_headcount", "share of population below the poverty line (0-1)"),
    "poverty_gap": ("poverty_gap", "poverty gap index (0-1)"),
    "gini": ("gini", "Gini index (0-1)"),
    "mean": ("mean_income", "mean welfare, PPP dollars per person per day"),
    "median": ("median_income", "median welfare, PPP dollars per person per day"),
    "decile10": ("income_share", "share of welfare held by the top decile (0-1)"),
    "decile1": ("income_share", "share of welfare held by the bottom decile (0-1)"),
}
LINE_DEPENDENT = {"poverty_headcount", "poverty_gap"}
MONETARY = {"mean_income", "median_income", "poverty_threshold"}
PIP_RELEASE = re.compile(r"^(\d{8})_(\d{4})_(\d{2})_(\d{2})_[A-Z]+$")


class IncomeFormatError(ValueError):
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
    if value is None:
        return None
    text = str(value).strip()
    if text in {"", ":", "NaN", "nan", "None", "null"}:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return format(number.normalize(), "f") if number == number.to_integral() else str(number)


def parse_pip_release(version: str) -> dict[str, Any]:
    """``20980915_2017_01_02_PROD`` -> release date, PPP base year and the PPP revision label (format: verify)."""
    match = PIP_RELEASE.fullmatch(str(version or ""))
    if not match:
        raise IncomeFormatError("invalid_release", f"PIP release_version {version!r} is not YYYYMMDD_PPP_RR_AA_TAG")
    day, ppp_year, revision, adaptation = match.groups()
    published = date(int(day[:4]), int(day[4:6]), int(day[6:8])).isoformat()
    return {"published_on": published, "ppp_base_year": int(ppp_year), "ppp_revision": f"{revision}_{adaptation}"}


# ------------------------------------------------------------------ declaration


def income_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    config = dict(source.get("income_distribution") or {})
    provider, fmt = config.get("provider"), config.get("format")
    if provider not in PROVIDERS or FORMATS.get(str(fmt), {}).get("provider") != provider:
        raise SourcePackError("invalid_source", "income-distribution sources declare a known provider and its format")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host != PROVIDER_HOSTS[provider]:
        raise SourcePackError("unsafe_endpoint", f"{provider} documents are fetched from {PROVIDER_HOSTS[provider]}")
    documents = [dict(d) for d in config.get("documents") or []]
    if not 1 <= len(documents) <= CAPS["documents"]:
        raise SourcePackError("unbounded_source", f"declare 1-{CAPS['documents']} documents")
    for document in documents:
        try:
            check_document(str(fmt), document)
        except IncomeFormatError as exc:
            raise SourcePackError("invalid_source", f"{document.get('label')}: {exc}") from exc
    return {"provider": provider, "format": fmt, "namespace": config.get("namespace") or "global",
            "documents": documents, "live_verification": config.get("live_verification") or "unverified-live"}


def check_document(fmt: str, document: Mapping[str, Any]) -> None:
    for key in ("label", "welfare_concept", "equivalence_scale", "definition", "references"):
        if not document.get(key):
            raise IncomeFormatError("invalid_document", f"a document states its {key}")
    if document["welfare_concept"] not in WELFARE_CONCEPTS:
        raise IncomeFormatError("invalid_document", f"welfare_concept is one of {WELFARE_CONCEPTS}")
    if document["equivalence_scale"] not in EQUIVALENCE_SCALES:
        raise IncomeFormatError("invalid_document", f"equivalence_scale is one of {EQUIVALENCE_SCALES}")
    if fmt == "pip-json":
        params = dict(document.get("params") or {})
        if document.get("endpoint") not in {"pip", "pip-grp"}:
            raise IncomeFormatError("invalid_document", "PIP documents name the pip or pip-grp endpoint")
        for key in ("country", "povline", "ppp_version", "release_version"):
            if not params.get(key):
                raise IncomeFormatError("unbounded_document", f"PIP documents pin {key}")
        if str(params["country"]).casefold() == "all":
            raise IncomeFormatError("unbounded_document", "PIP documents name countries or regions, never all")
        if parse_pip_release(params["release_version"])["ppp_base_year"] != int(params["ppp_version"]):
            raise IncomeFormatError("invalid_document", "release_version and ppp_version name different PPP rounds")
        if not set(document.get("measures") or []) <= set(PIP_MEASURES) or not document.get("measures"):
            raise IncomeFormatError("invalid_document", f"PIP measures are among {sorted(PIP_MEASURES)}")
    else:
        if not document.get("flow") or not document.get("key"):
            raise IncomeFormatError("unbounded_document", "SDMX documents name a dataflow and a series key")
        if not dict(document.get("params") or {}).get("startPeriod"):
            raise IncomeFormatError("unbounded_document", "SDMX documents pin a start period")
        indicator = dict(document.get("indicator") or {})
        if indicator.get("concept") not in CONCEPTS and not document.get("measure_dimension"):
            raise IncomeFormatError("invalid_document", f"indicator concept is one of {CONCEPTS}")
        if fmt == "oecd-idd-sdmx-csv" and not (document.get("methodology") and document.get("income_definition")):
            raise IncomeFormatError("invalid_document", "OECD IDD documents state the methodology and income definition")
        if fmt == "eurostat-silc-sdmx-csv" and not document.get("income_reference"):
            raise IncomeFormatError("invalid_document", "EU-SILC documents state the income reference period rule")
    line = document.get("poverty_line")
    if line is not None and dict(line).get("basis") not in LINE_BASES:
        raise IncomeFormatError("invalid_document", f"a poverty line basis is one of {LINE_BASES}")


def document_key(provider: str, document: Mapping[str, Any]) -> str:
    """Identity of a declared document across releases (removal detection compares releases of one document)."""
    if provider == "pip":
        params = dict(document["params"])
        return f"pip:{document['endpoint']}:{params['country']}:{params['povline']}:{params['ppp_version']}"
    return f"{provider}:{document['flow']}:{document['key']}"


def document_url(fmt: str, document: Mapping[str, Any]) -> str:
    if fmt == "pip-json":
        params = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in dict(document["params"]).items()}
        params.setdefault("year", "all")
        params["format"] = "json"
        if document["endpoint"] == "pip-grp":
            params.setdefault("group_by", "wb")
        return f"https://api.worldbank.org/pip/v1/{document['endpoint']}?" + urlencode(sorted(params.items()))
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector

    provider = SDMX_PROVIDERS[FORMATS[fmt]["provider"]]
    try:
        url, query = SDMXConnector(provider).csv_url(str(document["flow"]), str(document["key"]),
                                                     dict(document.get("params") or {}))
    except ValueError as exc:
        raise IncomeFormatError("invalid_document", str(exc)) from exc
    return url + "?" + urlencode(sorted(query.items()))


# ------------------------------------------------------------------ parsing


def _definition(document: Mapping[str, Any], provider: str, **extra: Any) -> dict[str, Any]:
    definition = dict(document["definition"])
    return {
        "provider": provider,
        "source_text": definition.get("source_text"),
        "welfare_concept": document["welfare_concept"],
        "equivalence_scale": document["equivalence_scale"],
        "threshold": definition.get("threshold"),
        "income_definition": document.get("income_definition"),
        "methodology": document.get("methodology"),
        "income_reference": document.get("income_reference"),
        "methodology_notes": list(definition.get("methodology_notes") or []),
        "references": [dict(r) for r in document.get("references") or []],
        **extra,
    }


def _line(spec: Mapping[str, Any] | None, *, ppp_base_year: int | None = None) -> dict[str, Any] | None:
    if not spec:
        return None
    line = {k: spec.get(k) for k in ("basis", "amount", "unit", "label")}
    line["amount"] = decimal_text(line["amount"]) if line.get("amount") is not None else None
    line["ppp_base_year"] = ppp_base_year if spec.get("basis") == "absolute-ppp" else None
    return line


def series_key(item: Mapping[str, Any]) -> dict[str, Any]:
    """The fields that make a series: never a release, so each release of the same key is a vintage."""
    return {
        "provider": item["provider"],
        "concept": item["indicator"]["concept"],
        "measure": item["indicator"]["measure"],
        "welfare_concept": item["welfare_concept"],
        "equivalence_scale": item["equivalence_scale"],
        "poverty_line": item.get("poverty_line"),
        "ppp_base_year": item.get("ppp_base_year"),
        "area": {"scheme": item["area"]["scheme"], "code": item["area"]["code"]},
        "coverage": item.get("coverage") or {},
        "survey": item.get("survey"),
        "income_definition": item.get("income_definition"),
        "methodology": item.get("methodology"),
    }


def parse_pip(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    """One PIP response as series items: one per (place, reporting level, welfare type, measure)."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise IncomeFormatError("schema_drift", "PIP response is not JSON") from exc
    rows = payload if isinstance(payload, list) else (payload or {}).get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise IncomeFormatError("schema_drift", "PIP response is not a list of estimates")
    if len(rows) > CAPS["pip_rows"]:
        raise IncomeFormatError("budget_exhausted", "PIP response has more rows than the declared cap")
    params = dict(document["params"])
    release = parse_pip_release(params["release_version"])
    regional = document["endpoint"] == "pip-grp"
    line_spec = {"basis": "absolute-ppp", "amount": params["povline"],
                 "unit": f"{release['ppp_base_year']} PPP dollars per person per day",
                 "label": dict(document.get("poverty_line") or {}).get("label")}
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise IncomeFormatError("schema_drift", "PIP rows are objects")
        code = row.get("region_code") if regional else row.get("country_code")
        year = row.get("reporting_year")
        if not code or year is None:
            raise IncomeFormatError("schema_drift", "PIP rows state a country or region code and a reporting year")
        stated_line = decimal_text(row.get("poverty_line"))
        if stated_line is not None and stated_line != decimal_text(params["povline"]):
            raise IncomeFormatError("schema_drift", "PIP answered for another poverty line than requested")
        welfare = "mixed" if regional else str(row.get("welfare_type") or "").casefold()
        if welfare not in WELFARE_CONCEPTS:
            raise IncomeFormatError("schema_drift", f"PIP welfare_type {row.get('welfare_type')!r} is not stated")
        level = "regional" if regional else str(row.get("reporting_level") or "national")
        estimation = ("regional-line-up" if regional else str(row.get("estimation_type") or "")).casefold() or (
            "interpolation" if row.get("is_interpolated") else "not-stated")
        if estimation not in ESTIMATION_TYPES:
            raise IncomeFormatError("schema_drift", f"PIP estimation_type {estimation!r} is not known")
        for field in document["measures"]:
            if field not in row:
                continue
            concept, unit = PIP_MEASURES[field]
            key = canonical([code, level, welfare, field])
            entry = groups.setdefault(key, {
                "provider": "pip",
                "native_key": f"{document['endpoint']}:{code}:{level}:{welfare}:{field}",
                "indicator": {"concept": concept, "measure": field,
                              "label": f"PIP {field}" + (" (regional aggregate)" if regional else "")},
                "welfare_concept": welfare,
                "equivalence_scale": document["equivalence_scale"],
                "poverty_line": _line(line_spec, ppp_base_year=release["ppp_base_year"])
                if concept in LINE_DEPENDENT else None,
                "ppp_base_year": release["ppp_base_year"],
                "area": {"scheme": "wb-region" if regional else "iso3166-1-alpha3", "code": str(code),
                         "label": row.get("region_name") if regional else row.get("country_name")},
                "coverage": {"reporting_level": level},
                "survey": "PIP household surveys (survey acronym per value)" if not regional
                else "PIP regional line-up aggregate",
                "income_definition": None,
                "methodology": f"PIP {release['ppp_base_year']} PPP round",
                "unit": {"code": field, "label": unit},
                "frequency": "annual",
                "definition": _definition(document, "pip", poverty_line=line_spec if concept in LINE_DEPENDENT
                                          else None, ppp_base_year=release["ppp_base_year"]),
                "references": [dict(r) for r in document.get("references") or []],
                "source_notes": [],
                "observations": [],
            })
            value_text = None if row.get(field) is None else str(row.get(field))
            value = decimal_text(value_text)
            period = str(int(float(year)))
            if any(o["period"] == period for o in entry["observations"]):
                raise IncomeFormatError("schema_drift", f"PIP states reporting year {period} twice for one series")
            entry["observations"].append({
                "period": period,
                "value_text": value_text,
                "value": value,
                "status": "reported" if value is not None else "not_published",
                "estimation_type": estimation,
                "survey_year": None if regional or row.get("survey_year") is None else str(row.get("survey_year")),
                "income_reference_year": None,
                "welfare_type": welfare,
                "flags": {},
                "attributes": {k: row.get(k) for k in ("survey_acronym", "survey_coverage", "survey_comparability",
                                                       "comparable_spell", "distribution_type", "is_interpolated")
                               if row.get(k) is not None},
            })
    items = []
    for entry in groups.values():
        entry["observations"].sort(key=lambda o: o["period"])
        comparability = [(o["period"], o["attributes"].get("survey_comparability")) for o in entry["observations"]]
        for (before, left), (after, right) in pairwise(comparability):
            if left is not None and right is not None and left != right:
                entry["source_notes"].append({
                    "kind": "break", "attribute": "survey_comparability", "value": f"{left} -> {right}",
                    "periods": [after], "statement": "PIP marks the surveys of these years as not comparable "
                                                     f"(survey_comparability changes between {before} and {after})"})
        items.append(entry)
    items.sort(key=lambda i: i["native_key"])
    return {"items": items, "published_on": release["published_on"], "published_at": None,
            "release_basis": "pip_release_version", "release_label": params["release_version"],
            "ppp": {"base_year": release["ppp_base_year"], "revision": release["ppp_revision"]},
            "dataflow_version": None}


def _flag_letters(attributes: Mapping[str, Any]) -> set[str]:
    letters: set[str] = set()
    for key in FLAG_ATTRIBUTES:
        value = str(attributes.get(key) or "").strip()
        letters |= set(value) if key == "OBS_FLAG" else ({value} if value else set())
    return letters


def _flow_version(reference: str | None) -> str | None:
    if not reference:
        return None
    match = re.search(r"\((\d+(?:\.\d+)*)\)\s*$", reference) or re.search(r",(\d+(?:\.\d+)*)\s*$", reference)
    return match.group(1) if match else None


def parse_sdmx(raw: bytes, *, fmt: str, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    """Declared EU-SILC or OECD IDD SDMX-CSV series as items; flags and dataflow version kept per value."""
    from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector
    from src.integrations.common import IntegrationError

    provider = FORMATS[fmt]["provider"]
    connector = SDMXConnector(SDMX_PROVIDERS[provider])
    ref = SeriesRef(locator=f"{document['flow']}/{document['key']}", metadata={"flow": str(document["flow"])},
                    title=document.get("label"))
    try:
        records = connector.parse_csv(RawSeries(ref, raw, content_type="text/csv", source_url=url, fetched_at=0))
    except IntegrationError as exc:
        raise IncomeFormatError("schema_drift", f"{exc.code}: {exc}") from exc
    if not records:
        raise IncomeFormatError("schema_drift", "the response states no series")
    if len(records) > CAPS["sdmx_series"]:
        raise IncomeFormatError("budget_exhausted", "the response has more series than the declared cap")
    dims_spec = dict(document.get("dimensions") or {})
    area_dim = dims_spec.get("area") or ("geo" if provider == "eurostat-silc" else "REF_AREA")
    coverage_dims = list(dims_spec.get("coverage") or [])
    measures = dict(document.get("measure_dimension") or {})
    lines = dict(document.get("poverty_line_dimension") or {})
    reference = dict(document.get("income_reference") or {})
    items, stated_flows, last_update = [], set(), None
    for record in records:
        meta = record.metadata
        dims = {str(k): str(v) for k, v in dict(meta["dimensions"]).items()}
        stated_flows.add(meta.get("dataflow"))
        last_update = meta.get("provider_last_update_at") or last_update
        area_code = dims.get(area_dim)
        if area_code is None:
            raise IncomeFormatError("schema_drift", "the response lacks the declared area dimension")
        if measures:
            code = dims.get(measures["dimension"])
            spec = dict(measures["codes"]).get(code)
            if spec is None:
                raise IncomeFormatError("schema_drift", f"unmapped measure code {code!r}")
            indicator = {"concept": spec["concept"], "measure": code, "label": spec.get("label") or code}
            unit = dict(spec.get("unit") or document.get("unit") or {"code": "", "label": ""})
        else:
            indicator = dict(document["indicator"])
            unit = dict(document.get("unit") or {"code": "", "label": ""})
        if indicator["concept"] not in CONCEPTS:
            raise IncomeFormatError("schema_drift", f"unknown concept {indicator['concept']!r}")
        line = document.get("poverty_line")
        if lines:
            code = dims.get(lines["dimension"])
            line = dict(lines["codes"]).get(code)
            if line is None and indicator["concept"] in LINE_DEPENDENT:
                raise IncomeFormatError("schema_drift", f"unmapped poverty-line code {code!r}")
        if indicator["concept"] not in LINE_DEPENDENT:
            line = None
        observations, breaks = [], []
        for observation in sorted(record.observations, key=lambda o: o.period):
            attributes = {str(k): str(v) for k, v in dict(meta["observation_attributes"].get(observation.period)
                                                         or {}).items()}
            value_text = meta["original_values"].get(observation.period)
            value = decimal_text(value_text)
            letters = _flag_letters(attributes)
            status = "reported" if value is not None else (
                "confidential" if letters & CONFIDENTIAL_FLAGS else "not_published")
            period = str(observation.period)[:4]
            if letters & BREAK_FLAGS:
                breaks.append(period)
            income_year = None
            if reference:
                offset = int(dict(reference.get("exceptions") or {}).get(area_code, reference.get("offset_years", 0)))
                income_year = str(int(period) + offset)
            observations.append({
                "period": period,
                "value_text": value_text if value_text not in (None, "") else None,
                "value": value,
                "status": status,
                "estimation_type": "survey",
                "survey_year": period if provider == "eurostat-silc" else None,
                "income_reference_year": income_year,
                "welfare_type": document["welfare_concept"],
                "flags": {k: attributes[k] for k in sorted(attributes) if k in FLAG_ATTRIBUTES},
                "flag_meanings": sorted({FLAG_MEANINGS.get(letter, letter) for letter in letters}),
                "attributes": {k: attributes[k] for k in sorted(attributes) if k not in FLAG_ATTRIBUTES},
            })
        notes = []
        if breaks:
            notes.append({"kind": "break", "attribute": "OBS_FLAG" if provider == "eurostat-silc" else "OBS_STATUS",
                          "value": "break in series", "periods": breaks,
                          "statement": f"{document['label']}: the source flags a break in series"})
        methodology = document.get("methodology")
        definition_code = None
        if dims_spec.get("methodology"):
            methodology = dims.get(dims_spec["methodology"]) or methodology
        if dims_spec.get("definition"):
            definition_code = dims.get(dims_spec["definition"])
        coverage = {k: dims.get(k) for k in coverage_dims if dims.get(k) is not None}
        items.append({
            "provider": provider,
            "native_key": ".".join(dims[k] for k in dims),
            "dataflow": {"reference": str(document["flow"]), "stated": meta.get("dataflow")},
            "indicator": indicator,
            "welfare_concept": document["welfare_concept"],
            "equivalence_scale": document["equivalence_scale"],
            "poverty_line": _line(line),
            "ppp_base_year": None,
            "area": {"scheme": dims_spec.get("area_scheme") or ("eurostat-geo" if provider == "eurostat-silc"
                                                               else "iso3166-1-alpha3"),
                     "code": area_code, **({"label": dict(document.get("area_labels") or {})[area_code]}
                                           if area_code in dict(document.get("area_labels") or {}) else {})},
            "coverage": coverage,
            "survey": document.get("survey") or ("EU-SILC" if provider == "eurostat-silc" else "OECD IDD"),
            "income_definition": (f"{document.get('income_definition')} ({definition_code})" if definition_code
                                  else document.get("income_definition")),
            "methodology": methodology,
            "unit": unit,
            "frequency": "annual",
            "definition": _definition(document, provider, poverty_line=line, methodology=methodology,
                                      underlying_survey=document.get("underlying_survey")),
            "references": [dict(r) for r in document.get("references") or []],
            "source_notes": notes,
            "observations": observations,
        })
    items.sort(key=lambda i: i["native_key"])
    stated = sorted(f for f in stated_flows if f)
    flow_version = _flow_version(stated[0] if stated else str(document["flow"]))
    declared = dict(document.get("release") or {})
    if last_update:
        published_on, published_at, basis = last_update[:10], last_update, "provider_last_update"
    elif declared.get("published_on"):
        published_on, published_at, basis = str(declared["published_on"]), None, "declared_release"
    else:
        published_on, published_at, basis = None, None, "retrieval_time"
    return {"items": items, "published_on": published_on, "published_at": published_at, "release_basis": basis,
            "release_label": declared.get("label") or last_update or published_on, "ppp": None,
            "dataflow_version": flow_version}


# ------------------------------------------------------------------ adapter


class IncomeDistributionAdapter:
    """Fetch the declared documents; one page (one release) per document."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        del secret  # every income-distribution source is open; nothing secret is ever sent
        self.source = json.loads(json.dumps(source))
        self.declared = income_declaration(self.source)
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "income_distribution": {"provider": self.declared["provider"], "format": self.declared["format"],
                                    "documents": len(self.declared["documents"]), "caps": CAPS,
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
            raise SourcePackError("parameter_forbidden", "income-distribution runs fetch the declared documents only")

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
        fmt = self.declared["format"]
        provider = self.declared["provider"]
        try:
            url = document_url(fmt, document)
        except IncomeFormatError as exc:
            raise SourcePackError("invalid_source", str(exc)) from exc
        raw, origin = self._get(url)
        try:
            release = parse_pip(raw, document=document) if fmt == "pip-json" else parse_sdmx(
                raw, fmt=fmt, document=document, url=url)
        except IncomeFormatError as exc:
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
            "release_label": release["release_label"], "ppp": release["ppp"],
            "dataflow_version": release["dataflow_version"], "file_sha256": hashlib.sha256(raw).hexdigest(),
            "content_sha256": digest(items), "item_count": len(items), "complete": True,
            "evidence_origin": origin, "live_verification": LIVE_VERIFICATION[provider]["status"], "url": url,
        }
        records = [{
            "id": f"{header['file_sha256'][:16]}:{number}",
            "title": f"{document['label']} ({release['release_label'] or 'retrieved'})",
            "url": url, "language": "en", "published_at": release["published_on"],
            "content": canonical(item), "income_release": header, "income_item": item,
        } for number, item in enumerate(items)]
        receipt = {"status": 200, "provider": provider, "document": document["label"],
                   "release_label": release["release_label"], "release_basis": release["release_basis"],
                   "file_sha256": header["file_sha256"], "items": len(records), "requests": 1,
                   "evidence_origin": origin, "final_page": index + 1 >= len(documents)}
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


ADAPTERS = {CONNECTOR: IncomeDistributionAdapter}


def _fixture_key(url: str, params: Any) -> str:
    pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
    query = urlencode(sorted((str(k), str(v)) for k, v in pairs))
    return urlsplit(url).path + ("?" + query if query else "")


def fixture_request(fmt: str, document: Mapping[str, Any]) -> str:
    """The key :func:`fixture_transport` files a response under."""
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
    adapter = IncomeDistributionAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
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
    "CONCEPTS",
    "CONNECTOR",
    "EQUIVALENCE_SCALES",
    "EXCLUSIONS",
    "FORMATS",
    "LIVE_VERIFICATION",
    "NEVER_SENTENCE",
    "PROVIDERS",
    "PROVIDER_CONTRACTS",
    "WELFARE_CONCEPTS",
    "IncomeDistributionAdapter",
    "IncomeFormatError",
    "document_key",
    "document_url",
    "fixture_request",
    "fixture_transport",
    "income_declaration",
    "parse_pip",
    "parse_pip_release",
    "parse_sdmx",
    "replay_native_fixture",
    "series_key",
]
