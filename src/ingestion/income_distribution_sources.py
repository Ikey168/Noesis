"""Income, poverty and inequality sources for the Society bundle's ``society.income`` provider (#2583, IP01, IP03-IP05).

Three providers are recorded under an access contract (:data:`PROVIDER_CONTRACTS`), each a format of the
``income-distribution`` source-pack connector:

* **World Bank PIP** (``pip``, format ``pip-json``) - the Poverty and Inequality Platform API (``/pip`` country
  estimates, ``/pip-grp`` regional aggregates), JSON. A document names one country or region, one poverty line and
  the PPP round (``ppp_version``) and pins the PIP release (``version``, e.g. ``20260801_2017_01_02_PROD``). Each PIP
  release and each PPP round is a separate vintage; the welfare type (income or consumption), the survey year, the
  survey acronym and PIP's own estimation label (survey-year estimate or interpolated/extrapolated reference-year
  "lineup" estimate) are stored per value exactly as PIP labels them.
* **Eurostat EU-SILC** (``eu-silc``, format ``eurostat-sdmx-csv``) - EU-SILC datasets (``ilc_*``) through the
  existing :class:`~src.ingestion.connectors.dataset.sdmx.SDMXConnector` ESTAT SDMX-CSV path; ``LAST UPDATE`` dates
  the release, ``OBS_FLAG`` letters are stored verbatim per value, the survey year and the income reference year
  are kept distinct, and the at-risk-of-poverty threshold rule and the modified OECD equivalence scale are recorded
  as definitions.
* **OECD Income Distribution Database** (``oecd-idd``, format ``oecd-sdmx-csv``) - the OECD Data Explorer IDD
  dataflow through the same connector (provider ``OECD``); the income definition, methodology (terms of reference)
  and dataflow version are stored per series, and breaks become comparability notes. OECD values are never mixed
  with EU-SILC values, even where the OECD figure rests on the same survey.

Every provider is ``unverified-live`` until a dated live run (``docs/development/income-distribution-evidence/``);
endpoint, parameter and field names marked *verify* come from the providers' documentation as recorded in the source
audit. The sources publish aggregate statistics only; the data-minimisation decision (:data:`MINIMISATION`) keeps
any person- or household-level field out of every record. Nothing here nowcasts, fills a year, sets its own poverty
line, blends PIP, EU-SILC and OECD figures into one series or re-harmonises a welfare concept.
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

CONNECTOR = "income-distribution"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-income-distribution-release-v1"
NEVER_SENTENCE = (
    "Published income, poverty and inequality figures as each source released them: PIP, EU-SILC and OECD side by "
    "side with their welfare concept, equivalence scale, poverty line and PPP round, never blended or re-harmonised; "
    "no nowcast, no filled year and no poverty line of our own."
)
EXCLUSIONS = (
    "nowcasting poverty or inequality",
    "filling years a source did not publish",
    "setting or applying poverty lines no source published",
    "blending PIP, EU-SILC and OECD figures into one series",
    "re-harmonising welfare concepts, equivalence scales or PPP rounds",
    "deriving indicators (rates, shares or ratios) that no source published",
    "person- or household-level microdata",
)
# IP01 data-minimisation decision: aggregate published statistics only.
MINIMISATION = {
    "decision": "store aggregate published statistics only; no person-, household- or respondent-level field is "
    "stored, logged or returned",
    "stored": [
        "published aggregate values with their flags, labels and notes",
        "survey metadata as published (survey acronym, survey year, coverage, welfare type, estimation label)",
        "release, retrieval and file digests",
    ],
    "excluded": [
        "EU-SILC, LIS or national survey microdata (research-contract access; out of scope)",
        "PIP percentile or distribution files beyond the declared aggregate indicators",
        "any field naming or identifying a person, household or respondent",
    ],
    "redacted": "nothing: no personal field is admitted, so nothing needs redaction; a record carrying one is "
    "refused at write time (personal_data_refused)",
    "retention": "every retained vintage is kept as published provenance; removals by the source are recorded as "
    "revisions, never deletions",
    "who_may_query": "principals with knowledge:income:read and namespace read access; writes need "
    "knowledge:income:write, reviews knowledge:income:review",
}
PERSONAL_KEYS = frozenset({
    "person_id", "personal_id", "household_id", "hh_id", "respondent", "respondent_id", "name", "first_name",
    "last_name", "surname", "full_name", "address", "street", "postcode", "zip", "email", "phone", "birth_date",
    "date_of_birth", "national_id", "microdata", "individual_income", "household_income_record",
})

PROVIDER_HOSTS = {"pip": {"api.worldbank.org"}, "eu-silc": {"ec.europa.eu"}, "oecd-idd": {"sdmx.oecd.org"}}
SDMX_PROVIDERS = {"eu-silc": "ESTAT", "oecd-idd": "OECD"}
PIP_PATHS = {"pip": "country estimates", "pip-grp": "regional and global aggregates"}
PIP_PARAMETERS = ("country", "year", "povline", "fill_gaps", "ppp_version", "version", "welfare_type",
                  "reporting_level", "group_by", "format")
PIP_INDICATORS = {
    "headcount": {"concept": "poverty_headcount_ratio", "measure": "share",
                  "label": "Poverty headcount ratio at the poverty line (share of population)",
                  "unit": {"code": "share", "label": "share of population (0-1)"}},
    "poverty_gap": {"concept": "poverty_gap_index", "measure": "index",
                    "label": "Poverty gap index at the poverty line", "unit": {"code": "share", "label": "index (0-1)"}},
    "gini": {"concept": "gini_index", "measure": "index", "label": "Gini index of the welfare distribution",
             "unit": {"code": "index01", "label": "index (0-1)"}},
    "mean": {"concept": "mean_welfare", "measure": "mean", "label": "Mean welfare per person per day",
             "unit": {"code": "usd_ppp_day", "label": "PPP dollars per person per day"}},
    "median": {"concept": "median_welfare", "measure": "median", "label": "Median welfare per person per day",
               "unit": {"code": "usd_ppp_day", "label": "PPP dollars per person per day"}},
    "decile10": {"concept": "income_share", "measure": "share", "label": "Welfare share held by the top decile",
                 "unit": {"code": "share", "label": "share of total welfare (0-1)"}},
}
# PIP fields kept per value (as PIP labels them; verify names against a live response).
PIP_VALUE_ATTRIBUTES = ("survey_year", "survey_acronym", "survey_coverage", "welfare_type", "estimation_type",
                        "is_interpolated", "survey_comparability", "comparable_spell", "distribution_type", "ppp",
                        "cpi", "reporting_level")
MONETARY_UNITS = {"usd_ppp_day"}

PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "pip": {
        "delivers": "World Bank PIP country poverty and inequality estimates (survey-year and interpolated "
        "reference-year estimates) and regional aggregates at stated poverty lines and PPP rounds",
        "access_decision": "unverified-live",
        "reason": "documented public JSON API without authentication; not yet run live from this runtime (the audit "
        "could not fetch pip.worldbank.org/api from this environment)",
        "access": "api (PIP API v1, JSON; /pip country estimates and /pip-grp aggregates; verify)",
        "entry_points": [
            ("https://api.worldbank.org/pip/v1/pip?country={ISO3}&year=all&povline={line}&fill_gaps={bool}"
             "&ppp_version={YYYY}&version={release}&format=json (verify)"),
            ("https://api.worldbank.org/pip/v1/pip-grp?country={REGION}&year=all&povline={line}&group_by=wb"
             "&ppp_version={YYYY}&version={release}&format=json (verify)"),
            "https://api.worldbank.org/pip/v1/versions (release list; verify)",
        ],
        "authentication": "none",
        "rate_limits": "no published quota found (verify); each run is bounded by the declared documents, max_pages "
        "and max_results",
        "identifiers": {
            "countries": "country_code as ISO 3166-1 alpha-3",
            "regions": "region_code as World Bank region codes (e.g. SSF, EAP, WLD; verify)",
            "release": "version string {YYYYMMDD}_{PPP year}_{PPP revision}_{data revision}_{PROD} (verify); the "
            "release date and PPP round are read from it",
        },
        "indicators": {k: v["label"] for k, v in PIP_INDICATORS.items()},
        "welfare_concepts": "welfare_type per survey: income or consumption, as PIP states; never converted",
        "equivalence_scale": "per-capita welfare (no equivalence scale), as PIP documents (verify)",
        "poverty_lines": "the declared povline in PPP dollars per person per day of the declared PPP round "
        "(e.g. 2.15 in 2017 PPP); no other line is ever computed",
        "estimation_labels": "estimation_type / is_interpolated per value: survey-year estimate versus interpolated "
        "or extrapolated reference-year estimate, stored as PIP labels them (verify field names)",
        "update_cadence": "PIP releases (typically March and September; verify) and PPP-round changes",
        "temporal_semantics": "the pinned release version dates the vintage; reporting_year is the reference year and "
        "survey_year the survey year, kept distinct",
        "release_signals": "/versions endpoint and the version string of each release (verify)",
        "revision_model": "each PIP release and each PPP round or PPP revision is a new vintage; a PPP revision "
        "restating past values is never an overwrite",
        "terms": "CC BY 4.0 (World Bank dataset terms of use; verify on the PIP site)",
        "retained_evidence": "raw JSON per request (file digest) with every kept field verbatim",
        "coverage": "declared countries, regions, poverty lines, PPP rounds and releases only",
        "unavailable_fallback": "a failed request leaves earlier vintages current and is reported in the receipt",
        "verify": ["API base path and version", "parameter names", "response field names", "version string layout",
                   "estimation labels", "terms"],
    },
    "eu-silc": {
        "delivers": "Eurostat EU-SILC at-risk-of-poverty rates, thresholds and inequality indicators by country and "
        "NUTS region",
        "access_decision": "unverified-live",
        "reason": "documented dissemination API without authentication, read through the SDMX connector's ESTAT "
        "SDMX-CSV path; not yet run live from this runtime",
        "access": "api (Eurostat SDMX 2.1 dissemination API, SDMX-CSV with LAST UPDATE and OBS_FLAG)",
        "entry_points": [("https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{dataset}/{key}"
                          "?format=SDMX-CSV (verify)")],
        "sdmx_version": "SDMX 2.1 REST",
        "authentication": "none",
        "rate_limits": "no published per-user limit (verify); asynchronous bulk extractions are out of scope",
        "identifiers": {
            "datasets": "ilc_li02 (at-risk-of-poverty rate by poverty threshold, age and sex), ilc_di12 (Gini "
            "coefficient of equivalised disposable income), ilc_li01 (at-risk-of-poverty thresholds), ilc_li41 "
            "(at-risk-of-poverty rate by NUTS region) - verify dimension order",
            "areas": "geo as Eurostat GEO codes (country = ISO alpha-2 except EL; NUTS 1/2 codes)",
        },
        "welfare_concepts": "equivalised disposable income (income concept only)",
        "equivalence_scale": "modified OECD scale (1.0 first adult, 0.5 other persons aged 14 and over, 0.3 children "
        "under 14), recorded as definition",
        "poverty_lines": "at-risk-of-poverty threshold: 60 % of the national median equivalised disposable income "
        "after social transfers (other shares where declared); thresholds acquired as published series (ilc_li01)",
        "reference_years": "TIME_PERIOD is the survey year; the income reference year is the previous calendar year "
        "for most countries (declared per document; verify per country)",
        "flags": "OBS_FLAG letters stored verbatim: b break in time series, p provisional, e estimated (by the "
        "publisher), u low reliability, c confidential, d definition differs",
        "update_cadence": "annual survey waves with revisions of earlier years",
        "temporal_semantics": "the SDMX-CSV LAST UPDATE column dates the release",
        "release_signals": "LAST UPDATE per dataset; Eurostat release calendar",
        "revision_model": "a changed dataset with a new LAST UPDATE is a new vintage; earlier vintages stay",
        "terms": "Eurostat reuse policy (Commission Decision 2011/833/EU): reuse with attribution",
        "retained_evidence": "raw SDMX-CSV per request (file digest), OBS_FLAG letters and the LAST UPDATE stamp",
        "coverage": "declared datasets, keys and periods only",
        "unavailable_fallback": "a failed request leaves earlier vintages current",
        "verify": ["dataset codes and key order", "LAST UPDATE format", "OBS_FLAG letters",
                   "income reference year rule per country"],
    },
    "oecd-idd": {
        "delivers": "OECD Income Distribution Database indicators (Gini, poverty rates at 50 % and 60 % of the "
        "median, income levels) by country, methodology and income definition",
        "access_decision": "unverified-live",
        "reason": "documented SDMX REST API (.Stat Suite) without authentication; the IDD dataflow reference not yet "
        "run live from this runtime",
        "access": "api (OECD Data Explorer SDMX REST API, SDMX-CSV via format=csvfile)",
        "entry_points": [("https://sdmx.oecd.org/public/rest/data/OECD.WISE.INE,DSD_WISE_IDD@DF_IDD,{version}/{key}"
                          "?format=csvfile (verify)")],
        "sdmx_version": "SDMX 2.1 REST (.Stat Suite)",
        "authentication": "none",
        "rate_limits": "about 20 data queries per minute per IP address (as recorded by the labour audit; verify)",
        "identifiers": {
            "dataflow": "OECD.WISE.INE,DSD_WISE_IDD@DF_IDD (verify version)",
            "areas": "REF_AREA as ISO 3166-1 alpha-3 (OECD aggregates stay distinct)",
            "dimensions": "REF_AREA, FREQ, MEASURE, STATISTICAL_OPERATION, UNIT_MEASURE, AGE, METHODOLOGY, "
            "DEFINITION, POVERTY_LINE (verify order and codes)",
        },
        "welfare_concepts": "household disposable income (cash income, 2012 terms of reference from 2012 onwards)",
        "equivalence_scale": "square root of household size, as the IDD terms of reference state",
        "methodology": "METHODOLOGY dimension (2011 and 2012 terms of reference) and DEFINITION dimension stored per "
        "series; a change is a break, never a continuation",
        "update_cadence": "rolling updates, two to three times a year (OECD dataset page; verify)",
        "temporal_semantics": "a changed response or dataflow version is a new release dated by the declared "
        "release date, else the retrieval time (labelled)",
        "release_signals": "dataflow version increments; IDD metadata version",
        "revision_model": "data updates and dataflow version changes are new vintages; earlier vintages stay",
        "terms": "OECD terms and conditions: reuse with attribution (verify)",
        "retained_evidence": "raw SDMX-CSV per request (file digest) with the DATAFLOW column",
        "coverage": "declared keys and periods only",
        "unavailable_fallback": "a failed or rate-limited request leaves earlier vintages current",
        "verify": ["dataflow reference and version", "dimension order and codes", "break flags", "rate limit"],
    },
}
LIVE_VERIFICATION = {
    provider: {
        "status": contract["access_decision"],
        "intended": "verified-live after a dated bounded run (IP13, #2648)",
        "note": "no dated live run from this runtime; offline fixtures only",
    }
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# Each provider is a separate optional feature of the Society bundle (IP11).
SOURCE_FEATURES = {"pip": "pip", "eu-silc": "eu-silc", "oecd-idd": "oecd-idd"}
BOUNDED_COVERAGE = {
    "places": {
        "Germany": {"pip": "DEU", "eu-silc": "DE", "oecd-idd": "DEU"},
        "Austria": {"eu-silc": "AT"},
        "Berlin (NUTS 2 DE30)": {"eu-silc": "DE30"},
        "Indonesia": {"pip": "IDN (consumption; interpolated reference-year estimates)"},
        "United States": {"oecd-idd": "USA"},
        "Sub-Saharan Africa": {"pip": "SSF (regional aggregate)"},
    },
    "indicators": ["poverty headcount ratio at a stated line", "poverty gap", "Gini", "mean and median welfare",
                   "top-decile share", "at-risk-of-poverty rate and threshold", "relative poverty rate (OECD)"],
    "poverty_lines": {"pip": "2.15 PPP$ per day (2017 PPP)", "eu-silc": "60 % of national median equivalised "
                      "disposable income", "oecd-idd": "50 % of median equivalised disposable income"},
    "periods": "two to four most recent reference years per declared series",
    "record_cap": "max_results series per response and max_pages requests per run",
    "justification": "Germany is covered by all three sources (the side-by-side journey), a consumption-based PIP "
    "country and a PIP region cover welfare-type and aggregate handling, Berlin covers NUTS matching, the United "
    "States covers a non-EU OECD country; the caps keep each run within the documented budgets",
}
FORMATS = {
    "pip-json": {"provider": "pip"},
    "eurostat-sdmx-csv": {"provider": "eu-silc"},
    "oecd-sdmx-csv": {"provider": "oecd-idd"},
}
CONCEPTS = (
    "poverty_headcount_ratio",
    "poverty_gap_index",
    "gini_index",
    "mean_welfare",
    "median_welfare",
    "income_share",
    "income_quintile_share_ratio",
    "poverty_threshold",
)
MEASURES = ("share", "rate", "index", "ratio", "mean", "median", "level")
WELFARE_CONCEPTS = ("income", "consumption")
# PIP's regional aggregates combine income- and consumption-based country estimates as PIP computes them.
AGGREGATE_WELFARE = "mixed-aggregate"
EQUIVALENCE_SCALES = ("per-capita", "modified-oecd", "square-root", "none")
REFERENCE_YEAR_BASES = ("survey-year", "lineup-year", "income-year")
STATUSES = ("reported", "confidential", "not_published")
CONFIDENTIAL_FLAGS = {"c", "C"}
BREAK_FLAGS = {"b", "B"}
FLAG_ATTRIBUTES = ("OBS_FLAG", "OBS_STATUS", "CONF_STATUS")
NOTE_ATTRIBUTES = ("NOTE", "COMMENT_OBS", "NOTE_SOURCE")
UNIT_MULT_ATTRIBUTES = ("UNIT_MULT",)


class IncomeFormatError(ValueError):
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
    return format(number, "f") if number.is_finite() else None


def iso_day(value: Any) -> str | None:
    raw = text(value)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return None


def personal_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in a value that would carry person- or household-level data (IP01 minimisation)."""
    found = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in PERSONAL_KEYS:
                found.append(f"{path}.{key}")
            found += personal_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += personal_keys(item, f"{path}[{index}]")
    return found


def pip_version(version: str | None) -> dict[str, Any] | None:
    """The PIP release version string read as release date, PPP year and revisions (layout to verify live)."""
    raw = text(version)
    if raw is None:
        return None
    match = re.fullmatch(r"(\d{8})_(\d{4})_(\d{2})_(\d{2})_([A-Z]+)", raw)
    if not match:
        raise IncomeFormatError("invalid_document", f"not a PIP release version: {raw!r}")
    day = match.group(1)
    try:
        released = date(int(day[:4]), int(day[4:6]), int(day[6:])).isoformat()
    except ValueError as exc:
        raise IncomeFormatError("invalid_document", "the PIP version's release date is not a date") from exc
    return {"version": raw, "released_on": released, "ppp_version": match.group(2),
            "ppp_revision": match.group(3), "data_revision": match.group(4), "identity": match.group(5),
            "layout": "{YYYYMMDD}_{PPP year}_{PPP revision}_{data revision}_{identity} (verify)"}


# ------------------------------------------------------------------ declarations


def income_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("income_distribution") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError(
            "invalid_manifest", "income-distribution sources declare a known provider and its runtime format"
        )
    documents = list(declared.get("documents") or [])
    if not documents:
        raise SourcePackError("invalid_manifest", "an income-distribution source declares its documents")
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
        except (IncomeFormatError, ValueError) as exc:
            raise SourcePackError("invalid_manifest", str(exc)) from exc
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError("invalid_manifest", "declared documents are HTTPS resources on the endpoint's host")
        urls.append(url)
    if len(set(urls)) != len(urls):
        raise SourcePackError("invalid_manifest", "each declared document is a distinct request")
    return declared


def _check_indicator(indicator: Mapping[str, Any]) -> None:
    if indicator.get("concept") not in CONCEPTS or indicator.get("measure") not in MEASURES:
        raise IncomeFormatError("invalid_document", f"an indicator states its concept ({CONCEPTS}) and measure")


def _check_line(line: Any) -> None:
    if line is None:
        return
    line = dict(line)
    if line.get("kind") == "absolute":
        if decimal_text(line.get("value")) is None or not text(line.get("ppp_base_year")) or not text(
                line.get("unit")):
            raise IncomeFormatError("invalid_document", "an absolute poverty line states its value, unit and PPP base "
                                    "year as published")
    elif line.get("kind") == "relative":
        if decimal_text(line.get("share")) is None or not text(line.get("of")):
            raise IncomeFormatError("invalid_document", "a relative poverty line states its share and reference")
    else:
        raise IncomeFormatError("invalid_document", "a poverty line is absolute or relative, as the source states")


def check_document(fmt: str, document: Mapping[str, Any]) -> None:
    if not text(document.get("label")):
        raise IncomeFormatError("invalid_document", "a document has a label")
    if personal_keys(dict(document)):
        raise IncomeFormatError("personal_data_refused", "a document declares no person- or household-level field")
    definition = dict(document.get("definition") or {})
    if document.get("welfare_concept") not in WELFARE_CONCEPTS + (AGGREGATE_WELFARE,):
        raise IncomeFormatError("invalid_document", f"a document states its welfare concept ({WELFARE_CONCEPTS})")
    if dict(document.get("equivalence_scale") or {}).get("code") not in EQUIVALENCE_SCALES:
        raise IncomeFormatError("invalid_document", f"a document states its equivalence scale ({EQUIVALENCE_SCALES})")
    if document.get("reference_year_basis") not in REFERENCE_YEAR_BASES:
        raise IncomeFormatError("invalid_document", f"a document states its reference-year basis "
                                f"({REFERENCE_YEAR_BASES})")
    if not text(document.get("survey")) or not text(document.get("coverage")):
        raise IncomeFormatError("invalid_document", "a document states its survey and coverage")
    if not text(definition.get("income_definition")):
        raise IncomeFormatError("invalid_document", "a document states the income definition the source uses")
    for ref in document.get("references") or []:
        if not text(dict(ref).get("identifier")) or not text(dict(ref).get("kind")):
            raise IncomeFormatError("invalid_document", "each reference states its kind and identifier as published")
    release = document.get("release")
    if release is not None and iso_day(dict(release).get("published_on")) is None:
        raise IncomeFormatError("invalid_document", "a declared release states its publication date")
    if fmt == "pip-json":
        if document.get("path") not in PIP_PATHS:
            raise IncomeFormatError("invalid_document", f"a PIP document requests one of {sorted(PIP_PATHS)}")
        params = dict(document.get("params") or {})
        if set(params) - set(PIP_PARAMETERS):
            raise IncomeFormatError("invalid_document", f"PIP parameters are {PIP_PARAMETERS}")
        for key in ("country", "povline", "ppp_version"):
            if not text(params.get(key)):
                raise IncomeFormatError("invalid_document", f"a PIP document states {key}")
        if decimal_text(params["povline"]) is None:
            raise IncomeFormatError("invalid_document", "povline is the published poverty line value")
        version = pip_version(params.get("version"))
        if version is not None and version["ppp_version"] != str(params["ppp_version"]):
            raise IncomeFormatError("invalid_document", "the pinned PIP release and ppp_version name the same round")
        indicators = list(document.get("indicators") or [])
        if not indicators or set(indicators) - set(PIP_INDICATORS):
            raise IncomeFormatError("invalid_document", f"a PIP document selects indicators from {sorted(PIP_INDICATORS)}")
        if not dict(document.get("area") or {}).get("scheme"):
            raise IncomeFormatError("invalid_document", "a PIP document names its area code scheme")
        return
    area = dict(document.get("area") or {})
    if not area.get("dimension") or not area.get("scheme"):
        raise IncomeFormatError("invalid_document", "an SDMX document names its area dimension and code scheme")
    if document.get("indicator_dimension"):
        spec = dict(document["indicator_dimension"])
        if not spec.get("dimension") or not spec.get("codes"):
            raise IncomeFormatError("invalid_document", "an indicator dimension maps its codes to indicators")
        for indicator in dict(spec["codes"]).values():
            _check_indicator(dict(indicator))
            if not dict(dict(indicator).get("unit") or {}).get("label"):
                raise IncomeFormatError("invalid_document", "each mapped indicator states its unit")
    else:
        _check_indicator(dict(document.get("indicator") or {}))
        if not dict(document.get("unit") or {}).get("label"):
            raise IncomeFormatError("invalid_document", "a document states its unit")
    if document.get("poverty_line_dimension"):
        spec = dict(document["poverty_line_dimension"])
        if not spec.get("dimension") or "codes" not in spec:
            raise IncomeFormatError("invalid_document", "a poverty-line dimension maps its codes to lines")
        for line in dict(spec["codes"]).values():
            _check_line(line)
    else:
        _check_line(document.get("poverty_line"))
    if document.get("methodology_dimension"):
        spec = dict(document["methodology_dimension"])
        if not spec.get("dimension") or not spec.get("codes"):
            raise IncomeFormatError("invalid_document", "a methodology dimension maps its codes to descriptions")


def document_url(fmt: str, document: Mapping[str, Any]) -> str:
    if fmt == "pip-json":
        params = {k: str(v) for k, v in dict(document.get("params") or {}).items()}
        params.setdefault("format", "json")
        return f"https://api.worldbank.org/pip/v1/{document['path']}?" + urlencode(sorted(params.items()))
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector

    provider = SDMX_PROVIDERS[FORMATS[fmt]["provider"]]
    url, query = SDMXConnector(provider).csv_url(
        str(document.get("flow") or ""), str(document.get("key") or ""), dict(document.get("params") or {})
    )
    return url + "?" + urlencode(sorted(query.items()))


def document_key(provider: str, document: Mapping[str, Any]) -> str:
    """One declared request independent of its pinned release (a later PIP release of it is the same document)."""
    params = {k: v for k, v in dict(document.get("params") or {}).items() if k != "version"}
    return "inc-document:" + digest([provider, document.get("path"), document.get("flow"), document.get("key"),
                                     params])[:24]


# ------------------------------------------------------------------ parsing


def _declared_release(document: Mapping[str, Any]) -> tuple[str | None, str | None]:
    release = dict(document.get("release") or {})
    return iso_day(release.get("published_on")), text(release.get("label"))


def _definition(document: Mapping[str, Any], provider: str, indicator: Mapping[str, Any], *,
                welfare: str, line: Mapping[str, Any] | None, methodology: str | None) -> dict[str, Any]:
    definition = dict(document.get("definition") or {})
    return {
        "provider": provider,
        "indicator_code": text(indicator.get("code")),
        "concept": indicator["concept"],
        "measure": indicator["measure"],
        "welfare_concept": welfare,
        "income_definition": text(definition.get("income_definition")),
        "equivalence_scale": dict(document["equivalence_scale"]),
        "poverty_line": None if line is None else dict(line),
        "reference_year_basis": document["reference_year_basis"],
        "income_reference_rule": text(definition.get("income_reference_rule")),
        "survey": text(document.get("survey")),
        "coverage": text(document.get("coverage")),
        "methodology_version": methodology or text(definition.get("methodology_version")),
        "source_text": text(definition.get("source_text")),
        "methodology_notes": [str(n) for n in definition.get("methodology_notes") or []],
    }


def _item(provider: str, document: Mapping[str, Any], *, native_key: str, dataflow: Mapping[str, Any],
          indicator: Mapping[str, Any], unit: Mapping[str, Any], welfare: str, line: Mapping[str, Any] | None,
          ppp_base_year: str | None, survey: str, coverage: str, area: Mapping[str, Any], dimensions: Mapping[str, Any],
          observations: list[dict[str, Any]], source_notes: list[dict[str, Any]], methodology: str | None,
          unit_multiplier: str | None = None) -> dict[str, Any]:
    return {
        "provider": provider,
        "native_key": native_key,
        "document_key": document_key(provider, document),
        "dataflow": dict(dataflow),
        "indicator": dict(indicator),
        "definition": _definition(document, provider, indicator, welfare=welfare, line=line, methodology=methodology),
        "welfare_concept": welfare,
        "equivalence_scale": dict(document["equivalence_scale"]),
        "poverty_line": None if line is None else dict(line),
        "ppp_base_year": ppp_base_year,
        "reference_year_basis": document["reference_year_basis"],
        "survey": survey,
        "coverage": coverage,
        "methodology_version": methodology,
        "frequency": "annual",
        "unit": dict(unit),
        "unit_multiplier": unit_multiplier,
        "area": dict(area),
        "dimensions": dict(dimensions),
        "source_notes": source_notes,
        "references": [dict(r) for r in document.get("references") or []],
        "denominator": dict(document["denominator"]) if document.get("denominator") else None,
        "observations": sorted(observations, key=lambda o: o["period"]),
    }


def _pip_rows(raw: bytes) -> list[dict[str, Any]]:
    try:
        body = json.loads(raw.decode("utf-8"), parse_float=str, parse_int=str)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IncomeFormatError("schema_drift", "PIP response is not JSON") from exc
    if isinstance(body, Mapping) and isinstance(body.get("error"), (str, Mapping)):
        raise IncomeFormatError("schema_drift", f"PIP answered with an error: {body.get('error')}")
    rows = body if isinstance(body, list) else None
    if rows is None or not all(isinstance(r, Mapping) for r in rows):
        raise IncomeFormatError("schema_drift", "PIP response is not a list of rows")
    return [dict(r) for r in rows]


def parse_pip(raw: bytes, *, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    """PIP rows as income items: one series per area, reporting level, welfare type, survey and indicator."""
    del url
    rows = _pip_rows(raw)
    if not rows:
        raise IncomeFormatError("schema_drift", "the response states no estimate")
    if personal_keys(rows):
        raise IncomeFormatError("personal_data_refused", "the response carries a person- or household-level field")
    params = dict(document["params"])
    version = pip_version(params.get("version"))
    ppp = str(params["ppp_version"])
    line = {"kind": "absolute", "value": decimal_text(params["povline"]), "unit": "PPP$ per person per day",
            "ppp_base_year": ppp, "set_by": "World Bank (declared request parameter)"}
    regional = document["path"] == "pip-grp"
    area_spec = dict(document["area"])
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for row in rows:
        code = text(row.get("region_code" if regional else "country_code"))
        year = text(row.get("reporting_year"))
        if code is None or year is None or not re.fullmatch(r"\d{4}", year):
            raise IncomeFormatError("schema_drift", "a PIP row lacks its area code or reporting year")
        stated_line = decimal_text(row.get("poverty_line"))
        if stated_line is not None and Decimal(stated_line) != Decimal(line["value"]):
            raise IncomeFormatError("schema_drift", "a PIP row states another poverty line than requested")
        welfare = text(row.get("welfare_type")) or (document["welfare_concept"] if regional else None)
        if not regional and welfare not in WELFARE_CONCEPTS:
            raise IncomeFormatError("schema_drift", "a PIP country row states its welfare type (income or consumption)")
        level = text(row.get("reporting_level")) or ("regional" if regional else "national")
        survey = text(row.get("survey_acronym")) or ("PIP regional aggregate" if regional else None)
        if survey is None:
            raise IncomeFormatError("schema_drift", "a PIP country row states its survey")
        label = text(row.get("region_name" if regional else "country_name"))
        groups.setdefault((code, level, welfare, survey, label), []).append(row)
    items = []
    for (code, level, welfare, survey, label), members in sorted(groups.items(), key=lambda g: g[0][:4]):
        years = [str(r["reporting_year"]) for r in members]
        if len(set(years)) != len(years):
            raise IncomeFormatError("schema_drift", "a PIP series repeats a reporting year")
        for name in document["indicators"]:
            spec = PIP_INDICATORS[name]
            indicator = {"code": name, "concept": spec["concept"], "measure": spec["measure"], "label": spec["label"]}
            observations, spells = [], []
            for row in sorted(members, key=lambda r: str(r["reporting_year"])):
                value_text = text(row.get(name))
                value = decimal_text(value_text)
                attributes = {k: str(row[k]) for k in PIP_VALUE_ATTRIBUTES if row.get(k) not in (None, "")}
                attributes.setdefault("welfare_type", welfare)
                observations.append({
                    "period": str(row["reporting_year"]),
                    "value_text": value_text,
                    "value": value,
                    "status": "reported" if value is not None else "not_published",
                    "flags": {},
                    "attributes": attributes,
                    "footnotes": [],
                })
                spells.append((str(row["reporting_year"]), attributes.get("survey_comparability")))
            notes = []
            breaks = [year for (year, spell), (_, before) in zip(spells[1:], spells[:-1])
                      if spell is not None and before is not None and spell != before]
            if breaks:
                notes.append({"kind": "break", "attribute": "survey_comparability",
                              "value": "PIP states a new survey comparability spell", "periods": breaks})
            items.append(_item(
                "pip", document,
                native_key=f"{document['path']}:{code}:{level}:{welfare}:{survey}:{name}:{line['value']}@{ppp}",
                dataflow={"reference": document["path"], "stated": PIP_PATHS[document["path"]],
                          "version": None if version is None else version["version"]},
                indicator=indicator, unit=spec["unit"], welfare=welfare, line=line if name in {
                    "headcount", "poverty_gap"} else None,
                ppp_base_year=ppp if spec["unit"]["code"] in MONETARY_UNITS or name in {"headcount", "poverty_gap"}
                else None,
                survey=survey, coverage=f"{level} ({document['coverage']})",
                area={"scheme": area_spec["scheme"], "code": code, **({"label": label} if label else {})},
                dimensions={"path": document["path"], "area": code, "reporting_level": level,
                            "welfare_type": welfare, "survey": survey, "povline": line["value"], "ppp_version": ppp},
                observations=observations, source_notes=notes, methodology=None,
            ))
    items.sort(key=lambda i: i["native_key"])
    declared_on, declared_label = _declared_release(document)
    if version is not None:
        published_on, basis, label = version["released_on"], "pip_release_version", version["version"]
    elif declared_on:
        published_on, basis, label = declared_on, "declared_release", declared_label
    else:
        published_on, basis, label = None, "retrieval_time", None
    return {
        "provider": "pip",
        "format": "pip-json",
        "items": items,
        "item_count": len(items),
        "published_on": published_on,
        "published_at": None,
        "release_basis": basis,
        "release_label": label,
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": digest(items),
        "structure": {"path": document["path"], "release_version": version, "ppp_version": ppp,
                      "dataflow_version": None if version is None else version["version"], "rows": len(rows),
                      "series": len(items)},
    }


def _flag_letters(attributes: Mapping[str, Any]) -> set[str]:
    letters: set[str] = set()
    for key in FLAG_ATTRIBUTES:
        value = text(attributes.get(key))
        if value:
            letters |= set(value)
    return letters


def _flow_version(reference: str) -> str | None:
    match = re.search(r"\((\d+(?:\.\d+)*)\)\s*$", reference) or re.search(r",(\d+(?:\.\d+)*)\s*$", reference)
    return match.group(1) if match else None


def _mapped(document: Mapping[str, Any], key: str, dims: Mapping[str, str]) -> tuple[bool, Any]:
    spec = document.get(key)
    if not spec:
        return False, None
    spec = dict(spec)
    code = dims.get(spec["dimension"])
    codes = dict(spec["codes"])
    if code not in codes:
        raise IncomeFormatError("schema_drift", f"unmapped {spec['dimension']} code {code!r}")
    return True, codes[code]


def parse_sdmx(raw: bytes, *, fmt: str, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    """Declared SDMX-CSV series as income items: one per dimension combination, flags and notes verbatim."""
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
        raise IncomeFormatError("schema_drift", f"{exc.code}: {exc}") from exc
    if not records:
        raise IncomeFormatError("schema_drift", "the response states no series")
    area_spec = dict(document["area"])
    offset = document.get("income_reference_offset_years")
    items, dataflows, last_update = [], set(), None
    for record in records:
        meta = record.metadata
        dims = {str(k): str(v) for k, v in dict(meta["dimensions"]).items()}
        if personal_keys(dims):
            raise IncomeFormatError("personal_data_refused", "the response carries a person-level dimension")
        dataflows.add(meta.get("dataflow"))
        last_update = meta.get("provider_last_update_at") or last_update
        area_code = dims.get(area_spec["dimension"])
        if area_code is None:
            raise IncomeFormatError("schema_drift", "the response lacks the declared area dimension")
        mapped, indicator = _mapped(document, "indicator_dimension", dims)
        if mapped:
            indicator = dict(indicator)
            unit = dict(indicator.pop("unit"))
        else:
            indicator, unit = dict(document["indicator"]), dict(document["unit"])
        mapped, line = _mapped(document, "poverty_line_dimension", dims)
        if not mapped:
            line = document.get("poverty_line")
        _, methodology = _mapped(document, "methodology_dimension", dims)
        observations, notes, breaks, multipliers = [], {}, [], set()
        for observation in sorted(record.observations, key=lambda o: o.period):
            attributes = {str(k): str(v) for k, v in dict(meta["observation_attributes"].get(observation.period)
                                                         or {}).items()}
            value_text = meta["original_values"].get(observation.period)
            value = decimal_text(value_text)
            letters = _flag_letters(attributes)
            status = "reported" if value is not None else (
                "confidential" if letters & CONFIDENTIAL_FLAGS else "not_published")
            period = str(observation.period)
            if letters & BREAK_FLAGS:
                breaks.append(period)
            for key in NOTE_ATTRIBUTES:
                if attributes.get(key):
                    notes.setdefault((key, attributes[key]), []).append(period)
            multiplier = next((attributes[k] for k in UNIT_MULT_ATTRIBUTES if attributes.get(k)), None)
            if multiplier is not None:
                multipliers.add(multiplier)
            kept = {k: attributes[k] for k in sorted(attributes) if k not in FLAG_ATTRIBUTES}
            kept["welfare_type"] = document["welfare_concept"]
            if document["reference_year_basis"] == "survey-year":
                kept["survey_year"] = period
                if offset is not None and re.fullmatch(r"\d{4}", period):
                    kept["income_reference_year"] = str(int(period) + int(offset))
                    kept["income_reference_basis"] = "declared rule: " + str(
                        dict(document.get("definition") or {}).get("income_reference_rule") or "")
            elif document["reference_year_basis"] == "income-year":
                kept["income_reference_year"] = period
            observations.append({
                "period": period,
                "value_text": value_text if value_text not in (None, "") else None,
                "value": value,
                "status": status,
                "flags": {k: attributes[k] for k in sorted(attributes) if k in FLAG_ATTRIBUTES},
                "attributes": kept,
                "footnotes": [],
            })
        source_notes = [
            {"kind": "source-attribute", "attribute": key, "value": value, "periods": periods}
            for (key, value), periods in sorted(notes.items())
        ]
        if breaks:
            source_notes.append({"kind": "break", "attribute": "flag", "value": "break in series", "periods": breaks})
        for declared in document.get("declared_breaks") or []:
            declared = dict(declared)
            if declared.get("area") in (None, area_code):
                source_notes.append({"kind": "break", "attribute": "declared", "value": str(declared["statement"]),
                                     "periods": [str(p) for p in declared.get("periods") or []]})
        items.append(_item(
            provider, document,
            native_key=".".join(dims[k] for k in dims),
            dataflow={"reference": str(document["flow"]), "stated": meta.get("dataflow")},
            indicator=indicator, unit=unit, welfare=document["welfare_concept"], line=line,
            ppp_base_year=text(document.get("ppp_base_year")), survey=str(document["survey"]),
            coverage=str(document["coverage"]),
            area={"scheme": area_spec["scheme"], "code": area_code,
                  **({"label": dict(area_spec.get("labels") or {}).get(area_code)}
                     if dict(area_spec.get("labels") or {}).get(area_code) else {})},
            dimensions=dims, observations=observations, source_notes=source_notes,
            methodology=None if methodology is None else str(methodology),
            unit_multiplier=min(multipliers) if len(multipliers) == 1 else None,
        ))
    items.sort(key=lambda i: i["native_key"])
    declared_on, declared_label = _declared_release(document)
    if last_update:
        stamp = datetime.fromisoformat(last_update)
        stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
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


# ------------------------------------------------------------------ adapter


class IncomeDistributionAdapter:
    """Fetch the declared income documents on the runtime's default transport; one page (one release) per document."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        del secret  # no provider needs a credential (IP01)
        self.source = json.loads(json.dumps(source))
        self.declared = income_declaration(self.source)
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
            "income_distribution": {
                "provider": self.declared["provider"],
                "format": self.declared["format"],
                "feature": SOURCE_FEATURES[self.declared["provider"]],
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
            raise SourcePackError("parameter_forbidden", "income runs fetch the declared documents only")

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
        url = document_url(fmt, document)
        raw, origin = self._get(url)
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        try:
            release = (parse_pip(raw, document=document, url=url) if fmt == "pip-json"
                       else parse_sdmx(raw, fmt=fmt, document=document, url=url))
        except IncomeFormatError as exc:
            code = "personal_data_refused" if exc.code == "personal_data_refused" else "schema_drift"
            raise SourcePackError(code, f"{exc.code}: {exc}") from exc
        if release["item_count"] > limit:
            # Never a truncated release: a missing series would read as a series that was not published.
            raise SourcePackError("budget_exhausted", "release has more series than the run's result budget")
        header = {
            "contract": RELEASE_CONTRACT,
            "provider": release["provider"],
            "format": release["format"],
            "document": document,
            "document_key": document_key(release["provider"], document),
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
                "income_release": header,
                "income_item": item,
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


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: IncomeDistributionAdapter}


def _fixture_key(url: str, params: Any) -> str:
    pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
    query = urlencode(sorted(pairs))
    return urlsplit(url).path + ("?" + query if query else "")


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


def fixture_request(fmt: str, document: Mapping[str, Any]) -> str:
    """The key :func:`fixture_transport` files a response under."""
    base, _, query = document_url(fmt, document).partition("?")
    return _fixture_key(base, parse_qsl(query, keep_blank_values=True))


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = IncomeDistributionAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
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
    "EXCLUSIONS",
    "FORMATS",
    "LIVE_VERIFICATION",
    "MINIMISATION",
    "NEVER_SENTENCE",
    "PROVIDER_CONTRACTS",
    "SOURCE_FEATURES",
    "IncomeDistributionAdapter",
    "IncomeFormatError",
    "document_key",
    "fixture_request",
    "fixture_transport",
    "income_declaration",
    "parse_pip",
    "parse_sdmx",
    "personal_keys",
    "pip_version",
    "replay_native_fixture",
]
