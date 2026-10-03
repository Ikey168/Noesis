"""Social protection sources for the Society bundle's ``society.social-protection`` provider (#2741, SS03-SS05).

The machine-readable copy of ``docs/development/social-protection-evidence/source-audit.md`` (SS01). One native
source-pack connector, ``social-protection``, driven by :mod:`src.ingestion.source_pack_runtime` with the three
sources of the ``society-social-protection`` source pack (``config/source_packs/society-social-protection.json``).
Each source declares one provider and a bounded list of documents; the adapter fetches one declared document per page
and returns one *release* (a header and one item per series) that
:class:`src.kb.social_protection_store.SocialProtectionProjector` appends as series vintages.

* **Eurostat ESSPROS** (``eurostat-esspros``, format ``esspros-sdmx-csv``) - ``spr_exp_sum`` total expenditure,
  ``spr_exp_func`` expenditure by function (``spfunc``) and ``spr_pns_ben`` pension beneficiaries through the existing
  :class:`~src.ingestion.connectors.dataset.sdmx.SDMXConnector` (provider ``ESTAT``, SDMX-CSV), one request per
  declared document. ``LAST UPDATE`` dates the release; ``OBS_FLAG`` letters (``b`` break, ``p`` provisional, ``e``
  estimated, ``d`` definition differs, ``c`` confidential) are kept per value and ``c`` is a status, never a value.
  The ESSPROS manual edition in force is recorded as part of the definition.
* **OECD SOCX** (``oecd-socx``, format ``socx-sdmx-csv``) - public social expenditure, total and old age, % of GDP,
  through the same connector (provider ``OECD``, ``format=csvfile``). The dataflow agency, id, version and dimensions
  are _verify_ until the live validation (SS13). The response has no update stamp: a changed response is a new release
  dated by the declared release date, else the retrieval time (``retrieval_time``). ``OBS_STATUS`` ``B`` is a
  source-stated break; estimate and projection years keep that status and are never treated as final. Requests are
  paced at the stricter of the recorded OECD limits (about 20 data queries a minute, about 60 an hour) until verified.
* **ILOSTAT SDG 1.3.1** (``ilo-social-protection-coverage``, format ``ilo-sdmx-csv``) - dataflow
  ``ILO,DF_SDG_0131_SEX_SOC_RT,1.0`` (_verify_), total and old-age coverage, both sexes, through the same connector
  (provider ``ILO``). The population denominator ILO states, the contingency, sex and the ``NOTE_SOURCE``,
  ``NOTE_INDICATOR`` and ``NOTE_CLASSIF`` attributes (verbatim) are stored per series. A World Social Protection Report
  edition that restates earlier years is a new vintage (the declared ``edition``). ILO regional and global modelled
  estimates are separate series and not in first coverage; the World Social Protection Data Dashboards are
  ``not-implemented`` and never scraped.

Every provider is ``unverified-live`` until a dated live run (SS13); endpoint, dataset, dimension and attribute names
marked _verify_ come from the publishers' documentation as the audit records it, not from a live response. ESSPROS,
SOCX, ILO and COFOG figures are never blended; one publisher's functions are never re-classified into another's.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

CONNECTOR = "social-protection"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-social-protection-release-v1"
AUDIT = "docs/development/social-protection-evidence/source-audit.md"
PACK_ID = "society-social-protection"
FIXTURE_SECRET = None
PROVIDERS = ("eurostat-esspros", "oecd-socx", "ilo-social-protection-coverage")
NOT_IMPLEMENTED = ("ilo-world-social-protection-dashboards",)
FORMATS = {
    "esspros-sdmx-csv": {"provider": "eurostat-esspros"},
    "socx-sdmx-csv": {"provider": "oecd-socx"},
    "ilo-sdmx-csv": {"provider": "ilo-social-protection-coverage"},
}
PROVIDER_HOSTS = {"eurostat-esspros": "ec.europa.eu", "oecd-socx": "sdmx.oecd.org",
                  "ilo-social-protection-coverage": "sdmx.ilo.org"}
SDMX_PROVIDERS = {"eurostat-esspros": "ESTAT", "oecd-socx": "OECD", "ilo-social-protection-coverage": "ILO"}
# Each publisher's own classification of functions, branches or contingencies (never re-classified into another's).
FUNCTION_SCHEMES = {
    "eurostat-esspros": ("esspros-spfunc", "esspros-pension-category"),
    "oecd-socx": ("socx-branch",),
    "ilo-social-protection-coverage": ("ilo-contingency",),
}
# COFOG "social protection" (GF10) belongs to economics.public-finance: a third, distinct concept.
COFOG_SCHEME = "cofog"
MEASURES = ("expenditure", "beneficiaries", "coverage")
MEASURE_MEANINGS = {
    "expenditure": "money spent on social protection as the publisher scopes it (ESSPROS schemes or SOCX branches)",
    "beneficiaries": "a count of benefit recipients as the publisher counts them",
    "coverage": "a share of a population group covered by at least one benefit, as ILO defines the group",
}
KEY_FIELDS = ("function", "scheme_type", "financing", "cash_or_kind", "basis", "sex", "unit")
STATUSES = ("reported", "confidential", "not_published")
PUBLICATION_STATUSES = ("not-stated", "normal", "provisional", "estimated", "projected")
NEVER_SENTENCE = (
    "Published social protection figures as each source released them: Eurostat ESSPROS, OECD SOCX and ILOSTAT side "
    "by side with their own definitions and classifications, never blended or re-classified; COFOG stays a separate "
    "concept; no nowcast, no filled year, no per-capita, per-beneficiary or share-of-GDP figure of our own."
)
EXCLUSIONS = (
    "nowcasting social protection figures",
    "filling years a source did not publish",
    "blending ESSPROS, SOCX and ILO figures into one series",
    "re-classifying one publisher's functions, branches or contingencies into another's",
    "combining ESSPROS or SOCX expenditure with COFOG social-protection expenditure",
    "per-capita, per-beneficiary or share-of-GDP figures of our own",
    "deriving indicators (rates, ratios, shares or counts) that no source published",
    "forecasts of our own (OECD estimates and projections are kept with that status, never as final)",
    "person-level or household-level data (benefit registers, microdata, Social Security Inquiry returns)",
    "scraping the World Social Protection Data Dashboards",
)
CAPS = {
    "eurostat-esspros": {"documents": 3, "series_per_response": 60},
    "oecd-socx": {"documents": 1, "series_per_response": 20},
    "ilo-social-protection-coverage": {"documents": 1, "series_per_response": 20},
}
# Request pacing: the stricter of the recorded OECD limits (IP01 ~20 data queries a minute, ED01 ~60 an hour) applies
# until SS13 verifies the real limit; ESSPROS and ILOSTAT publish none, so one request per declared document.
PACING = {
    "eurostat-esspros": {"min_interval_s": 0, "basis": "no published quota (verify); one request per document"},
    "oecd-socx": {"min_interval_s": 60, "basis": "stricter of about 20 data queries a minute (IP01) and about 60 an "
                                                  "hour (ED01): at most one request a minute until verified"},
    "ilo-social-protection-coverage": {"min_interval_s": 0,
                                       "basis": "no published quota (verify); one request per document"},
}
BOUNDED_COVERAGE = {
    "eurostat-esspros": {
        "places": {"Germany": "geo DE", "France": "geo FR"},
        "series": "spr_exp_sum total expenditure (MIO_EUR, PC_GDP); spr_exp_func old age and sickness/health care; "
                  "spr_pns_ben pension beneficiaries, total",
        "periods": "from a declared start period",
        "caps": "3 documents, 60 series per response",
    },
    "oecd-socx": {
        "places": {"Germany": "REF_AREA DEU", "France": "REF_AREA FRA"},
        "series": "public social expenditure, total and old age, % of GDP (net social expenditure is a separate OECD "
                  "series, not in first coverage)",
        "periods": "from a declared start period",
        "caps": "1 document, 20 series",
    },
    "ilo-social-protection-coverage": {
        "places": {"Germany": "REF_AREA DEU", "France": "REF_AREA FRA"},
        "series": "SDG 1.3.1 total and old-age coverage, both sexes (regional and global modelled estimates are "
                  "separate series, not in first coverage)",
        "periods": "from a declared start period",
        "caps": "1 document, 20 series",
    },
}
UNAVAILABLE_FALLBACK = (
    "A failed document (HTTP error, redirect to another host, schema drift, a response larger than the budget) fails "
    "that source's run with its code and a receipt; earlier vintages stay current and nothing is marked removed or "
    "revised because of a failure. Readiness reports the source as stale."
)
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "eurostat-esspros": {
        "publisher": "Eurostat, European System of integrated Social PROtection Statistics (ESSPROS)",
        "delivers": "social-protection expenditure and receipts by function and scheme type; pension beneficiaries",
        "access": "api (Eurostat SDMX 2.1 dissemination API, SDMX-CSV) through the SDMX connector (ESTAT path); "
                  "datasets spr_exp_sum, spr_exp_func, spr_pns_ben (verify codes and dimension order)",
        "entry_points": [
            ("https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{dataset}/{key}?format=SDMX-CSV"
             "&startPeriod={year} (verify)"),
        ],
        "authentication": "none",
        "key_handling": "no key; nothing secret is stored or sent",
        "licence": "Eurostat reuse policy (Commission Decision 2011/833/EU): reuse with acknowledgement (verify)",
        "redistribution": "attribution-required",
        "rate_limits": "no published quota (verify); one request per declared document",
        "identifiers": "dataset code, SDMX key (freq, spfunc, scheme and benefit type, unit, geo), geo as Eurostat GEO "
                       "codes (ISO alpha-2 except EL and UK) (verify)",
        "revision_model": "LAST UPDATE dates each release; the dataflow version and OBS_FLAG letters (b break, p "
                          "provisional, e estimated, d definition differs, c confidential; verify the list) are kept "
                          "per value; a changed dataset is a new vintage, an unchanged re-publication adds nothing; a "
                          "series a later complete release no longer states becomes a removed_by_source vintage",
        "definitions": "the function (spfunc), scheme-type and benefit-type dimensions (means-tested or not, cash or "
                       "in kind), the unit as published (MIO_EUR, PC_GDP, EUR_HAB, PPS_HAB; verify) and the ESSPROS "
                       "manual edition in force; published percentages and per-inhabitant values stored as published",
        "personal_data": "none: published aggregates; benefit-recipient registers and microdata are excluded",
        "unavailable_fallback": UNAVAILABLE_FALLBACK,
        "status": "unverified-live",
    },
    "oecd-socx": {
        "publisher": "OECD, Social Expenditure Database (SOCX)",
        "delivers": "public and mandatory private social expenditure by policy area and type (cash, in kind)",
        "access": "api (OECD Data Explorer SDMX REST API, format=csvfile) through the SDMX connector (OECD path); the "
                  "aggregate SOCX dataflow OECD.ELS.SPD,DSD_SOCX_AGG@DF_SOCX_AGG,1.0 is the author's recollection "
                  "(verify agency, id, version and dimensions)",
        "entry_points": [
            ("https://sdmx.oecd.org/public/rest/data/{agency},{dataflow},{version}/{key}?format=csvfile"
             "&startPeriod={year} (verify)"),
        ],
        "authentication": "none",
        "key_handling": "no key",
        "licence": "OECD terms; OECD data under CC BY 4.0 since 2024 (verify)",
        "redistribution": "attribution-required",
        "rate_limits": "OECD API limits (IP01 records about 20 data queries a minute, ED01 about 60 an hour); the "
                       "stricter applies until verified (verify)",
        "identifiers": "REF_AREA (ISO 3166-1 alpha-3), PROGRAMME_TYPE (SOCX branch), EXPEND_SOURCE (financing), "
                       "SPENDING_TYPE (cash or in kind), UNIT_MEASURE (verify the dimension ids)",
        "revision_model": "no update stamp in the response (verify): a changed response is a new release dated by the "
                          "declared release date, else the retrieval time (labelled retrieval_time); the most recent "
                          "years are OECD estimates or projections from national budget data (verify the attribute "
                          "that marks them) and keep that status, never treated as final; OBS_STATUS B is a "
                          "source-stated break",
        "definitions": "the policy area (nine SOCX branches including health and active labour-market programmes), "
                       "the source of financing (public, mandatory private, voluntary private), type (cash, in kind), "
                       "gross or net basis and unit; net social expenditure is a separate series, not in first "
                       "coverage",
        "personal_data": "none: published aggregates",
        "unavailable_fallback": UNAVAILABLE_FALLBACK,
        "status": "unverified-live",
    },
    "ilo-social-protection-coverage": {
        "publisher": "ILO, ILOSTAT (SDG indicator 1.3.1, fed by the Social Security Inquiry)",
        "delivers": "share of the population covered by at least one benefit and by contingency (children, old age, "
                    "unemployment, disability, ...)",
        "access": "api (ILOSTAT SDMX REST API, SDMX-CSV) through the SDMX connector (ILO path); dataflow "
                  "ILO,DF_SDG_0131_SEX_SOC_RT,1.0 (verify id and the contingency dimension)",
        "entry_points": [
            "https://sdmx.ilo.org/rest/data/ILO,DF_SDG_0131_SEX_SOC_RT,1.0/{key}?format=csv&startPeriod={year} (verify)",
        ],
        "authentication": "none",
        "key_handling": "no key",
        "licence": "ILOSTAT terms of use, reuse with attribution (verify the licence; CC BY 4.0 as the author "
                   "understands it)",
        "redistribution": "attribution-required",
        "rate_limits": "none published (verify); one request per declared document",
        "identifiers": "REF_AREA (ISO 3166-1 alpha-3), SEX, the contingency dimension (verify), MEASURE",
        "revision_model": "per-indicator update date (verify where the response states it), else the declared "
                          "release date, else retrieval time; World Social Protection Report editions restate earlier "
                          "years and are new vintages; NOTE_SOURCE, NOTE_INDICATOR and NOTE_CLASSIF kept verbatim; "
                          "ILO regional and global modelled estimates are separate series, not in first coverage",
        "definitions": "the contingency, sex, the population denominator ILO states in its metadata, the reference "
                       "year and the note attributes; coverage is a share of a population group and is never compared "
                       "with an expenditure series as if it measured the same thing",
        "personal_data": "none: published aggregates; Social Security Inquiry questionnaire returns are excluded",
        "unavailable_fallback": UNAVAILABLE_FALLBACK,
        "status": "unverified-live",
    },
    "ilo-world-social-protection-dashboards": {
        "publisher": "ILO, World Social Protection Data Dashboards",
        "delivers": "the same coverage and expenditure figures as interactive dashboards and report tables",
        "access": "no documented machine API known to the author (verify)",
        "entry_points": [],
        "authentication": "not applicable",
        "key_handling": "not applicable",
        "licence": "not assessed (not acquired)",
        "redistribution": "not applicable",
        "rate_limits": "not applicable",
        "identifiers": "not applicable",
        "revision_model": "not applicable",
        "definitions": "not applicable: the ILOSTAT SDMX route carries the coverage series",
        "personal_data": "none",
        "unavailable_fallback": "never scraped; reported as not-implemented",
        "status": "not-implemented",
    },
}
LIVE_VERIFICATION = {
    **{provider: {"status": "unverified-live", "checked": None, "evidence": None,
                  "note": "no dated live run yet (SS13, #2808); offline fixtures only"} for provider in PROVIDERS},
    "ilo-world-social-protection-dashboards": {"status": "not-implemented", "checked": None, "evidence": None,
                                               "note": "no stable machine access (verify); dashboards are never "
                                                       "scraped"},
}
FLAG_ATTRIBUTES = ("OBS_FLAG", "OBS_STATUS", "CONF_STATUS")
BREAK_FLAGS = {"b", "B"}
CONFIDENTIAL_FLAGS = {"c", "C"}
DEFINITION_FLAGS = {"d"}
FLAG_MEANINGS = {
    "b": "break in time series", "p": "provisional", "e": "estimated", "d": "definition differs", "c": "confidential",
    "u": "low reliability", "z": "not applicable", "A": "normal value", "B": "break in time series",
    "E": "estimated value", "P": "provisional value", "F": "forecast value (OECD projection)", "M": "missing value",
}
# Source-stated publication status per flag (the source's own status, never ours).
FLAG_STATUS = {"p": "provisional", "e": "estimated", "A": "normal", "B": "normal", "E": "estimated",
               "P": "provisional", "F": "projected"}
NOTE_ATTRIBUTES = ("NOTE_SOURCE", "NOTE_INDICATOR", "NOTE_CLASSIF")


class SocialProtectionFormatError(ValueError):
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
    if text in {"", ":", "NaN", "nan", "None", "null"}:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return format(number.normalize(), "f") if number == number.to_integral() else str(number)


# ------------------------------------------------------------------ declaration


def social_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    config = dict(source.get("social_protection") or {})
    provider, fmt = config.get("provider"), config.get("format")
    if provider not in PROVIDERS or FORMATS.get(str(fmt), {}).get("provider") != provider:
        raise SourcePackError("invalid_source", "social-protection sources declare a known provider and its format")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host != PROVIDER_HOSTS[provider]:
        raise SourcePackError("unsafe_endpoint", f"{provider} documents are fetched from {PROVIDER_HOSTS[provider]}")
    documents = [dict(d) for d in config.get("documents") or []]
    if not 1 <= len(documents) <= CAPS[provider]["documents"]:
        raise SourcePackError("unbounded_source", f"declare 1-{CAPS[provider]['documents']} documents")
    for document in documents:
        try:
            check_document(provider, document)
        except SocialProtectionFormatError as exc:
            raise SourcePackError("invalid_source", f"{document.get('label')}: {exc}") from exc
    return {"provider": provider, "format": fmt, "namespace": config.get("namespace") or "global",
            "documents": documents, "live_verification": config.get("live_verification") or "unverified-live"}


def check_document(provider: str, document: Mapping[str, Any]) -> None:
    for key in ("label", "flow", "key", "measure", "function_scheme", "key_fields", "dimensions", "definition",
                "references"):
        if not document.get(key):
            raise SocialProtectionFormatError("invalid_document", f"a document states its {key}")
    if not dict(document.get("params") or {}).get("startPeriod"):
        raise SocialProtectionFormatError("unbounded_document", "documents pin a start period")
    if "*" in str(document["key"]) or str(document["key"]).startswith(".") and str(document["key"]).endswith("."):
        raise SocialProtectionFormatError("unbounded_document", "documents name their series key")
    if dict(document["measure"]).get("concept") not in MEASURES:
        raise SocialProtectionFormatError("invalid_document", f"measure concept is one of {MEASURES}")
    if document["function_scheme"] not in FUNCTION_SCHEMES[provider]:
        raise SocialProtectionFormatError(
            "invalid_document", f"{provider} states its own classification ({FUNCTION_SCHEMES[provider]}); "
                                "another publisher's functions are never used here")
    fields = dict(document["key_fields"])
    if set(fields) != set(KEY_FIELDS):
        raise SocialProtectionFormatError("invalid_document", f"key_fields declare exactly {KEY_FIELDS}")
    for name, spec in fields.items():
        spec = dict(spec)
        if bool(spec.get("dimension")) == bool(spec.get("fixed")):
            raise SocialProtectionFormatError("invalid_document", f"{name} names a dimension or a fixed value")
        if spec.get("dimension") and not spec.get("codes"):
            raise SocialProtectionFormatError("invalid_document", f"{name} lists the codes it reads")
    if not dict(document["dimensions"]).get("area"):
        raise SocialProtectionFormatError("invalid_document", "documents name the area dimension")
    if provider == "eurostat-esspros" and not document.get("manual_edition"):
        raise SocialProtectionFormatError("invalid_document", "ESSPROS documents state the manual edition in force")
    if provider == "oecd-socx" and not document.get("methodology"):
        raise SocialProtectionFormatError("invalid_document", "SOCX documents state the SOCX methodology")
    if provider == "ilo-social-protection-coverage":
        if not document.get("population"):
            raise SocialProtectionFormatError("invalid_document", "ILO documents state the population denominator")
        if dict(document.get("scope") or {}).get("modelled_estimates"):
            raise SocialProtectionFormatError("unbounded_document", "ILO regional and global modelled estimates are "
                                                                    "not in first coverage")


def document_key(provider: str, document: Mapping[str, Any]) -> str:
    """Identity of a declared document across releases (removal detection compares releases of one document)."""
    return f"{provider}:{document['flow']}:{document['key']}"


def document_url(provider: str, document: Mapping[str, Any]) -> str:
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector

    try:
        url, query = SDMXConnector(SDMX_PROVIDERS[provider]).csv_url(str(document["flow"]), str(document["key"]),
                                                                     dict(document.get("params") or {}))
    except ValueError as exc:
        raise SocialProtectionFormatError("invalid_document", str(exc)) from exc
    return url + "?" + urlencode(sorted(query.items()))


# ------------------------------------------------------------------ parsing


def series_key(item: Mapping[str, Any]) -> dict[str, Any]:
    """The fields that make a series: never a release, so each release of the same key is a vintage."""
    return {
        "provider": item["provider"],
        "dataset": item["dataset"],
        "measure": item["measure"]["concept"],
        "function": {k: item["function"].get(k) for k in ("scheme", "code")},
        **{field: dict(item[field]).get("code") for field in KEY_FIELDS if field != "function"},
        "area": {"scheme": item["area"]["scheme"], "code": item["area"]["code"]},
        "frequency": item["frequency"],
    }


def _flow_version(reference: str | None) -> str | None:
    if not reference:
        return None
    match = re.search(r"\((\d+(?:\.\d+)*)\)\s*$", reference) or re.search(r",(\d+(?:\.\d+)*)\s*$", reference)
    return match.group(1) if match else None


def _letters(attributes: Mapping[str, Any]) -> list[str]:
    letters: list[str] = []
    for key in FLAG_ATTRIBUTES:
        value = str(attributes.get(key) or "").strip()
        letters += list(value) if key == "OBS_FLAG" else ([value] if value else [])
    return letters


def _field(name: str, spec: Mapping[str, Any], dims: Mapping[str, str]) -> dict[str, Any]:
    if spec.get("fixed"):
        return dict(spec["fixed"])
    code = dims.get(spec["dimension"])
    if code is None:
        raise SocialProtectionFormatError("schema_drift", f"the response lacks the {spec['dimension']} dimension")
    known = dict(spec["codes"]).get(code)
    if known is None:
        raise SocialProtectionFormatError("schema_drift", f"unmapped {name} code {code!r}")
    return {"code": code, **({"label": known} if isinstance(known, str) else dict(known))}


def parse_sdmx(raw: bytes, *, provider: str, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    """Declared ESSPROS, SOCX or ILOSTAT SDMX-CSV series as items; flags, notes and dataflow version per value."""
    from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector
    from src.integrations.common import IntegrationError

    connector = SDMXConnector(SDMX_PROVIDERS[provider])
    ref = SeriesRef(locator=f"{document['flow']}/{document['key']}", metadata={"flow": str(document["flow"])},
                    title=document.get("label"))
    try:
        records = connector.parse_csv(RawSeries(ref, raw, content_type="text/csv", source_url=url, fetched_at=0))
    except IntegrationError as exc:
        raise SocialProtectionFormatError("schema_drift", f"{exc.code}: {exc}") from exc
    if not records:
        raise SocialProtectionFormatError("schema_drift", "the response states no series")
    if len(records) > CAPS[provider]["series_per_response"]:
        raise SocialProtectionFormatError("budget_exhausted", "the response has more series than the declared cap")
    dims_spec = dict(document["dimensions"])
    fields = {k: dict(v) for k, v in dict(document["key_fields"]).items()}
    area_dim = dims_spec["area"]
    area_scheme = dims_spec.get("area_scheme") or ("eurostat-geo" if provider == "eurostat-esspros"
                                                   else "iso3166-1-alpha3")
    labels = dict(document.get("area_labels") or {})
    population = dict(document.get("population") or {})
    items, stated_flows, last_update = [], set(), None
    for record in records:
        meta = record.metadata
        dims = {str(k): str(v) for k, v in dict(meta["dimensions"]).items()}
        stated_flows.add(meta.get("dataflow"))
        last_update = meta.get("provider_last_update_at") or last_update
        area_code = dims.get(area_dim)
        if area_code is None:
            raise SocialProtectionFormatError("schema_drift", "the response lacks the declared area dimension")
        if labels and area_code not in labels:
            raise SocialProtectionFormatError("schema_drift", f"the response states an undeclared place {area_code!r}")
        values = {name: _field(name, spec, dims) for name, spec in fields.items()}
        function = {"scheme": document["function_scheme"], **values["function"]}
        observations, breaks, definition_differs, notes = [], [], [], {k: set() for k in NOTE_ATTRIBUTES}
        for observation in sorted(record.observations, key=lambda o: o.period):
            attributes = {str(k): str(v) for k, v in dict(meta["observation_attributes"].get(observation.period)
                                                         or {}).items()}
            value_text = meta["original_values"].get(observation.period)
            value_text = value_text if value_text not in (None, "") else None
            letters = _letters(attributes)
            confidential = bool(set(letters) & CONFIDENTIAL_FLAGS)
            value = decimal_text(value_text)
            if confidential and value is not None:
                raise SocialProtectionFormatError("schema_drift", "a cell flagged confidential carries a value; "
                                                                  "'c' is a status, never a value")
            status = "reported" if value is not None else ("confidential" if confidential else "not_published")
            period = str(observation.period)
            if set(letters) & BREAK_FLAGS:
                breaks.append(period)
            if set(letters) & DEFINITION_FLAGS:
                definition_differs.append(period)
            stated = [FLAG_STATUS[letter] for letter in letters if letter in FLAG_STATUS]
            publication = next((s for s in ("projected", "estimated", "provisional") if s in stated),
                               "normal" if "normal" in stated else "not-stated")
            for name in NOTE_ATTRIBUTES:
                if attributes.get(name):
                    notes[name].add(attributes[name])
            observations.append({
                "period": period,
                "value_text": value_text,
                "value": value,
                "status": status,
                "publication_status": publication,
                "origin": "published",
                "flags": {k: attributes[k] for k in sorted(attributes) if k in FLAG_ATTRIBUTES},
                "flag_meanings": sorted({FLAG_MEANINGS.get(letter, letter) for letter in letters}),
                "attributes": {k: attributes[k] for k in sorted(attributes) if k not in FLAG_ATTRIBUTES},
            })
        source_notes = []
        attribute = "OBS_FLAG" if provider == "eurostat-esspros" else "OBS_STATUS"
        if breaks:
            source_notes.append({"kind": "break", "attribute": attribute, "value": "break in series",
                                 "periods": breaks, "statement": f"{document['label']}: the source flags a break in "
                                                                 "series"})
        if definition_differs:
            source_notes.append({"kind": "definition_differs", "attribute": attribute, "value": "d",
                                 "periods": definition_differs,
                                 "statement": f"{document['label']}: the source flags that the definition differs"})
        for note in document.get("scope_notes") or []:
            source_notes.append({"kind": "scope_difference", "attribute": "declared", "value": note.get("relates_to"),
                                 "periods": [], "statement": str(note["statement"]),
                                 "cites": [dict(c) for c in note.get("cites") or []]})
        stated_population = dict(population.get(values["function"]["code"]) or population.get("*") or {})
        definition = dict(document["definition"])
        items.append({
            "provider": provider,
            "dataset": str(document.get("dataset") or document["flow"]),
            "native_key": ".".join(dims[k] for k in dims),
            "dataflow": {"reference": str(document["flow"]), "stated": meta.get("dataflow")},
            "measure": dict(document["measure"]),
            "function": function,
            **{name: values[name] for name in KEY_FIELDS if name != "function"},
            "area": {"scheme": area_scheme, "code": area_code, **({"label": labels[area_code]}
                                                                  if area_code in labels else {})},
            "frequency": str(document.get("frequency") or "annual"),
            "population": stated_population or None,
            "definition": {
                "provider": provider,
                "source_text": definition.get("source_text"),
                "measure_meaning": MEASURE_MEANINGS[document["measure"]["concept"]],
                "function": function,
                "manual_edition": document.get("manual_edition"),
                "methodology": document.get("methodology"),
                "population": stated_population or None,
                "notes": {k: sorted(v) for k, v in notes.items() if v},
                "methodology_notes": list(definition.get("methodology_notes") or []),
                "references": [dict(r) for r in document.get("references") or []],
            },
            "references": [dict(r) for r in document.get("references") or []],
            "source_notes": source_notes,
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
            "release_label": declared.get("label") or last_update or published_on,
            "edition": declared.get("edition"), "dataflow_version": flow_version}


# ------------------------------------------------------------------ adapter

_LAST_REQUEST: dict[str, float] = {}


class SocialProtectionAdapter:
    """Fetch the declared documents; one page (one release) per document, paced per provider."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None, sleep: Callable[[float], None] | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        del secret  # every social-protection source is open; nothing secret is ever sent
        self.source = json.loads(json.dumps(source))
        self.declared = social_declaration(self.source)
        self.paced = transport is None
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.sleep, self.clock = sleep or time.sleep, clock or time.monotonic
        provider = self.declared["provider"]
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "social_protection": {"provider": provider, "format": self.declared["format"],
                                  "documents": len(self.declared["documents"]), "caps": CAPS[provider],
                                  "pacing": PACING[provider],
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
            raise SourcePackError("parameter_forbidden", "social-protection runs fetch the declared documents only")

    def _pace(self, host: str) -> None:
        interval = PACING[self.declared["provider"]]["min_interval_s"]
        if not self.paced or not interval:
            return
        last = _LAST_REQUEST.get(host)
        if last is not None and self.clock() - last < interval:
            self.sleep(interval - (self.clock() - last))
        _LAST_REQUEST[host] = self.clock()

    def _get(self, url: str) -> tuple[bytes, str]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        host = PROVIDER_HOSTS[self.declared["provider"]]
        parts = urlsplit(url)
        if (parts.hostname or "").casefold() != host or parts.scheme != "https":
            raise SourcePackError("network_policy", "declared documents are fetched from the provider's host only")
        self._pace(host)
        base, _, query = url.partition("?")
        response = self.transport(url=base, params=parse_qsl(query, keep_blank_values=True),
                                  headers={"Accept": "text/csv, application/vnd.sdmx.data+csv"},
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
        try:
            url = document_url(provider, document)
        except SocialProtectionFormatError as exc:
            raise SourcePackError("invalid_source", str(exc)) from exc
        raw, origin = self._get(url)
        try:
            release = parse_sdmx(raw, provider=provider, document=document, url=url)
        except SocialProtectionFormatError as exc:
            raise SourcePackError("budget_exhausted" if exc.code == "budget_exhausted" else "schema_drift",
                                  f"{exc.code}: {exc}") from exc
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
            "edition": release["edition"], "dataflow_version": release["dataflow_version"],
            "file_sha256": hashlib.sha256(raw).hexdigest(), "content_sha256": digest(items),
            "item_count": len(items), "complete": True, "evidence_origin": origin,
            "live_verification": LIVE_VERIFICATION[provider]["status"], "url": url,
        }
        records = [{
            "id": f"{header['file_sha256'][:16]}:{number}",
            "title": f"{document['label']} ({release['release_label'] or 'retrieved'})",
            "url": url, "language": "en", "published_at": release["published_on"],
            "content": canonical(item), "social_release": header, "social_item": item,
        } for number, item in enumerate(items)]
        receipt = {"status": 200, "provider": provider, "document": document["label"],
                   "release_label": release["release_label"], "release_basis": release["release_basis"],
                   "file_sha256": header["file_sha256"], "items": len(records), "requests": 1,
                   "evidence_origin": origin, "final_page": index + 1 >= len(documents)}
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


ADAPTERS = {CONNECTOR: SocialProtectionAdapter}


def _fixture_key(url: str, params: Any) -> str:
    pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
    query = urlencode(sorted((str(k), str(v)) for k, v in pairs))
    return urlsplit(url).path + ("?" + query if query else "")


def fixture_request(provider: str, document: Mapping[str, Any]) -> str:
    """The key :func:`fixture_transport` files a response under."""
    base, _, query = document_url(provider, document).partition("?")
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
    adapter = SocialProtectionAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
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
    "COFOG_SCHEME",
    "CONNECTOR",
    "EXCLUSIONS",
    "FORMATS",
    "FUNCTION_SCHEMES",
    "KEY_FIELDS",
    "LIVE_VERIFICATION",
    "MEASURES",
    "MEASURE_MEANINGS",
    "NEVER_SENTENCE",
    "NOT_IMPLEMENTED",
    "PACING",
    "PROVIDERS",
    "PROVIDER_CONTRACTS",
    "PUBLICATION_STATUSES",
    "STATUSES",
    "SocialProtectionAdapter",
    "SocialProtectionFormatError",
    "document_key",
    "document_url",
    "fixture_request",
    "fixture_transport",
    "parse_sdmx",
    "replay_native_fixture",
    "series_key",
    "social_declaration",
    "unverified",
]
