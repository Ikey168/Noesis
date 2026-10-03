"""Waste and circular-economy sources for the Climate and Environment ``environment.waste`` provider (#2740, WC03-WC05).

The machine-readable copy of ``docs/development/waste-evidence/source-audit.md`` (WC01). One native source-pack
connector, ``waste``, driven by :mod:`src.ingestion.source_pack_runtime` with the four sources of the
``climate-environment-waste`` pack (``packs/climate-environment/source_packs/climate-environment-waste.json``). Each
source declares one provider and a bounded list of documents; the adapter fetches one declared document per page and
returns one *release* (a header and one item per series or facility transfer row) that
:class:`src.kb.waste_store.WasteProjector` appends as vintages.

* **Eurostat waste statistics** (``eurostat-waste``, format ``eurostat-waste-sdmx-csv``) - waste generated
  (``env_wasgen``) and waste treated by treatment operation (``env_wastrt``) through the existing
  :class:`~src.ingestion.connectors.dataset.sdmx.SDMXConnector` (provider ``ESTAT``, SDMX-CSV). ``LAST UPDATE`` dates
  the release, ``OBS_FLAG`` letters are kept verbatim per value, and the series key carries the waste category
  (EWC-Stat ``waste``), hazardousness (``hazard``), NACE activity or households (``nace_r2``), the treatment operation
  (``wst_oper``) and the unit as published. Both datasets are biennial: odd years are absent and stay absent.
* **Eurostat circular economy** (``eurostat-circular-economy``, format ``eurostat-cei-sdmx-csv``) - ``cei_wm011`` and
  ``cei_srm030`` stored exactly as Eurostat publishes them; no rate is computed from tonnages.
* **EEA Industrial Reporting waste transfers** (``eea-industry-waste-transfers``, format ``eea-waste-transfers-json``) -
  off-site transfers of hazardous and non-hazardous waste per facility and reporting year from the EEA Discodata SQL
  endpoint, with the same endpoint and pinned-query pattern as the ``eea-industry`` adapter in
  :mod:`src.ingestion.environment_providers`, bounded to the Berlin facility selection of ``environment.core`` and
  one reporting year. The pinned query selects the INSPIRE id, reporting year, hazardousness, recovery or disposal,
  domestic or transboundary, quantity and method code only: no operator, parent-company, address, contact or
  authority column. A full page (``nrOfHits`` rows) is recorded as truncated (``eea_truncated``), never as complete.
* **OECD municipal waste** (``oecd-municipal-waste``, format ``oecd-waste-csv``) - municipal waste generated, total,
  for DEU and FRA through the SDMX connector's OECD path (``format=csvfile``). The dataflow agency, id, version and
  dimensions are *verify* until the live run (WC13). With no update stamp, a changed response is a new release dated
  by the declared release date, else the retrieval time (``retrieval_time``); ``OBS_STATUS`` ``B`` is a source-stated
  break. Requests are paced at the stricter of the recorded OECD limits (one request per 60 seconds).

Every provider is ``unverified-live`` until a dated live run (WC13). Nothing here nowcasts, fills a year, blends
Eurostat, OECD and EEA figures, sums facility transfers into national totals or computes a recycling rate, per-capita
or material-flow figure of its own.
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

CONNECTOR = "waste"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-waste-release-v1"
AUDIT = "docs/development/waste-evidence/source-audit.md"
TRACKER = "#2740"
# No source carries a credential; the runtime still passes a fixture secret to native adapters.
FIXTURE_SECRET = None
PROVIDERS = ("eurostat-waste", "eurostat-circular-economy", "eea-industry-waste-transfers", "oecd-municipal-waste")
SERIES_PROVIDERS = ("eurostat-waste", "eurostat-circular-economy", "oecd-municipal-waste")
TRANSFER_PROVIDER = "eea-industry-waste-transfers"
FORMATS = {
    "eurostat-waste-sdmx-csv": {"provider": "eurostat-waste"},
    "eurostat-cei-sdmx-csv": {"provider": "eurostat-circular-economy"},
    "eea-waste-transfers-json": {"provider": "eea-industry-waste-transfers"},
    "oecd-waste-csv": {"provider": "oecd-municipal-waste"},
}
PROVIDER_HOSTS = {"eurostat-waste": "ec.europa.eu", "eurostat-circular-economy": "ec.europa.eu",
                  "eea-industry-waste-transfers": "discodata.eea.europa.eu", "oecd-municipal-waste": "sdmx.oecd.org"}
SOURCE_IDS = {"eurostat-waste": "eurostat-waste", "eurostat-circular-economy": "eurostat-circular-economy",
              "eea-industry-waste-transfers": "eea-industry-waste-transfers",
              "oecd-municipal-waste": "oecd-municipal-waste"}
NEVER_SENTENCE = (
    "Published waste and circular-economy figures as each source released them: Eurostat, OECD and EEA figures side "
    "by side with their own definitions, never blended; biennial gaps stay absent; facility transfers are never "
    "summed into national totals; no nowcast, no filled year, no recycling rate, per-capita or material-flow figure "
    "of our own, no derived indicator and no forecast."
)
EXCLUSIONS = (
    "nowcasting waste or circularity indicators",
    "filling years a source did not publish (biennial odd years stay absent)",
    "blending Eurostat, OECD and EEA figures",
    "summing facility transfers into national totals",
    "recycling rates, per-capita or material-flow figures of our own",
    "derived indicators",
    "forecasts",
    "operator, parent-company, address, contact and competent-authority fields",
    "a second facility register",
    "waste-shipment notifications, permit documents and inspection records",
)
CONCEPTS = (
    "waste_generated",
    "waste_treated",
    "municipal_waste_recycling_rate",
    "circular_material_use_rate",
    "municipal_waste_generated",
)
STATUSES = ("reported", "confidential", "not_published")
PERIODICITIES = ("annual", "biennial")
HAZARD_CODES = {"HAZ_NHAZ": "hazardous and non-hazardous", "HAZ": "hazardous", "NHAZ": "non-hazardous",
                "not_applicable": "not broken down by hazardousness"}
TRANSFER_HAZARD = {"HW": "hazardous", "NONHW": "non-hazardous"}
TRANSFER_TREATMENT = {"R": "recovery", "D": "disposal"}
TRANSFER_DESTINATION = {"DOMESTIC": "domestic", "TRANSBOUNDARY": "transboundary"}
METHOD_CODES = {"M": "measured", "C": "calculated", "E": "estimated"}
# Hard ceilings the adapter enforces on top of the source-pack budgets (WC01 bounded first coverage).
CAPS = {
    "eurostat-waste": {"documents": 2, "documents_per_dataset": 1, "series_per_response": 60},
    # One SDMX request names one dataflow, so the audit's "1 document" is one document per dataset (cei_wm011,
    # cei_srm030); recorded as a clarification in the audit.
    "eurostat-circular-economy": {"documents": 2, "documents_per_dataset": 1, "series_per_response": 10},
    "eea-industry-waste-transfers": {"documents": 1, "rows_per_response": 500, "reporting_years": 1},
    "oecd-municipal-waste": {"documents": 1, "series_per_response": 10, "min_request_interval_s": 60},
}
BOUNDED_COVERAGE = {
    "eurostat-waste": {
        "places": {"Germany": "geo DE", "France": "geo FR"},
        "series": "env_wasgen total waste, all NACE activities and households, hazardous and non-hazardous; env_wastrt "
                  "total by treatment operation",
        "periods": "from a declared start year (biennial)",
        "caps": "2 documents, 60 series per response",
    },
    "eurostat-circular-economy": {
        "places": {"Germany": "geo DE", "France": "geo FR"},
        "series": "cei_wm011 and cei_srm030 as Eurostat publishes them",
        "periods": "from a declared start year",
        "caps": "1 document per dataset (one SDMX request names one dataflow), 10 series per response",
    },
    "eea-industry-waste-transfers": {
        "places": {"Berlin, DE": "the environment.core eea-industry facility selection (countryCode DE, city Berlin)"},
        "series": "hazardous and non-hazardous off-site transfers per facility, recovery or disposal, domestic or "
                  "transboundary",
        "periods": "one reporting year",
        "caps": "1 document, 500 rows (nrOfHits); a full page is recorded as truncated (eea_truncated)",
    },
    "oecd-municipal-waste": {
        "places": {"Germany": "REF_AREA DEU", "France": "REF_AREA FRA"},
        "series": "municipal waste generated, total",
        "periods": "from a declared start year",
        "caps": "1 document, 10 series",
    },
    "justification": "two countries in all three statistical sources show the same concept side by side without "
                     "blending; Berlin reuses the facility selection environment.core already acquires, so the "
                     "transfers attach to known facilities",
    "record_cap": "max_results series per response and max_pages documents per run; a larger statistical release is "
                  "refused, never truncated; an EEA page that fills nrOfHits is stored and labelled truncated",
}
_EUROSTAT_ENTRY = ("https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/{dataset}/{key}"
                   "?format=SDMX-CSV&startPeriod=... (verify)")
_EUROSTAT_TERMS = {
    "authentication": "none",
    "key_handling": "no key; nothing secret is sent or stored",
    "licence": "Eurostat reuse policy (Commission Decision 2011/833/EU): reuse with acknowledgement (verify)",
    "redistribution": "attribution-required",
    "rate_limits": "no published quota (verify); one request per declared document",
    "personal_data": "none: published aggregates only; confidential cells are stored as their status only",
    "unavailable_fallback": "a failed document (HTTP error, redirect to another host, schema drift, a response over "
                            "the budget) fails the source's run with its code and a receipt; earlier vintages stay "
                            "current, nothing is marked revised or removed, and readiness reports the source stale",
}
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "eurostat-waste": {
        "publisher": "Eurostat, waste statistics (Waste Statistics Regulation (EC) No 2150/2002, verify amendments)",
        "delivers": "waste generated by waste category, hazardousness and NACE activity (env_wasgen); waste treated by "
                    "category and treatment operation (env_wastrt)",
        "access": "api (Eurostat SDMX 2.1 dissemination API, SDMX-CSV through the existing SDMX connector, ESTAT "
                  "path; verify codes and dimension order)",
        "entry_points": [_EUROSTAT_ENTRY],
        **_EUROSTAT_TERMS,
        "revision_model": "LAST UPDATE dates each release; countries resubmit earlier reference years and each changed "
                          "release is a new vintage; OBS_FLAG (p, e, b, c, d, verify) kept per value; a series a "
                          "later complete release no longer states becomes a removed_by_source vintage",
        "definitions": "waste category (EWC-Stat, waste), hazardousness (hazard), NACE activity or households "
                       "(nace_r2), treatment operation (wst_oper: recovery, recycling, backfilling, energy recovery, "
                       "incineration, landfill; verify codes) and unit (tonnes, kg per capita) as Eurostat publishes "
                       "them; biennial reference years",
        "status": "unverified-live",
        "verify": ["dataset codes and dimension order", "wst_oper codes", "OBS_FLAG letters", "LAST UPDATE format",
                   "the Waste Statistics Regulation amendments", "reuse terms"],
    },
    "eurostat-circular-economy": {
        "publisher": "Eurostat, circular economy monitoring framework",
        "delivers": "Eurostat-published indicators such as the recycling rate of municipal waste (cei_wm011) and the "
                    "circular material use rate (cei_srm030)",
        "access": "api (same SDMX-CSV path; verify codes)",
        "entry_points": [_EUROSTAT_ENTRY],
        **_EUROSTAT_TERMS,
        "revision_model": "as Eurostat waste: LAST UPDATE per release, each changed release a new vintage, flags per "
                          "value, removals as removed_by_source vintages",
        "definitions": "the indicator as Eurostat defines it in the monitoring framework, stored as published; Noesis "
                       "computes no rate from waste tonnages",
        "status": "unverified-live",
        "verify": ["cei_wm011 and cei_srm030 codes", "unit codes", "OBS_FLAG letters"],
    },
    "eea-industry-waste-transfers": {
        "publisher": "European Environment Agency, Industrial Reporting under the IED and E-PRTR (the E-PRTR "
                     "successor)",
        "delivers": "off-site transfers of hazardous and non-hazardous waste per facility and reporting year, by "
                    "recovery or disposal and domestic or transboundary",
        "access": "api (EEA Discodata SQL endpoint, JSON; the same endpoint and pinned-query pattern as the "
                  "eea-industry adapter in src/ingestion/environment_providers.py; a waste-transfer table in the "
                  "[IED].[latest] schema, verify table and column names)",
        "entry_points": ["https://discodata.eea.europa.eu/sql?query={pinned SQL}&p=1&nrOfHits={cap} (verify)"],
        "authentication": "none",
        "key_handling": "no key; nothing secret is sent or stored",
        "licence": "EEA standard reuse policy, CC BY 4.0, citing the dataset version (as recorded for eea-industry, "
                   "verify)",
        "redistribution": "attribution-required (cite the EEA dataset version)",
        "rate_limits": "none documented (verify); one bounded page per declared document; a full page is recorded "
                       "as truncated (eea_truncated), never as complete",
        "revision_model": "the latest schema is a moving view: each acquisition records the dataset version the EEA "
                          "states (verify where it is exposed), else the retrieval time (labelled); member states "
                          "correct past reporting years, so a changed row for a past year is a new vintage, never an "
                          "overwrite; a row absent from a later complete response is removed_by_source, not deleted",
        "definitions": "facility INSPIRE id, reporting year, hazardous or non-hazardous, recovery (R) or disposal "
                       "(D), domestic or transboundary, quantity in tonnes as published and the method code "
                       "(measured, calculated, estimated); facilities report only above the E-PRTR thresholds "
                       "(verify), so absence is not zero",
        "personal_data": "the pinned query selects no operator, parent-company, address, contact or authority column; "
                         "operator names stay where environment.core already holds them",
        "unavailable_fallback": _EUROSTAT_TERMS["unavailable_fallback"],
        "status": "unverified-live",
        "verify": ["the waste-transfer table and column names", "the codes for hazardousness, R/D and destination",
                   "where the dataset version is exposed", "the E-PRTR reporting thresholds",
                   "whether any contact-person field exists", "reuse terms"],
    },
    "oecd-municipal-waste": {
        "publisher": "OECD, Environment statistics",
        "delivers": "municipal waste generation and treatment per country",
        "access": "api (OECD Data Explorer SDMX REST API, format=csvfile, through the SDMX connector's OECD path; "
                  "the municipal-waste dataflow id is not known with confidence: verify agency, id, version and "
                  "dimensions)",
        "entry_points": [("https://sdmx.oecd.org/public/rest/data/{agency},{dataflow},{version}/{key}"
                          "?format=csvfile&startPeriod=... (verify)")],
        "authentication": "none",
        "key_handling": "no key; nothing secret is sent or stored",
        "licence": "OECD terms; OECD data under CC BY 4.0 since 2024 (verify)",
        "redistribution": "attribution-required",
        "rate_limits": "OECD API limits (IP01 records about 20 data queries a minute, ED01 about 60 an hour; the "
                       "stricter applies until verified, so one request per 60 seconds, verify)",
        "revision_model": "no update stamp in the response (verify): a changed response is a new release dated by "
                          "the declared release date, else the retrieval time (labelled retrieval_time); OBS_STATUS B "
                          "becomes a source-stated break",
        "definitions": "the municipal-waste definition and treatment categories as the OECD states them; for EU "
                       "members the OECD and Eurostat figures share the joint questionnaire (verify) yet stay two "
                       "series; a difference is shown, never reconciled",
        "personal_data": "none: published aggregates only",
        "unavailable_fallback": _EUROSTAT_TERMS["unavailable_fallback"],
        "status": "unverified-live",
        "verify": ["dataflow agency, id and version", "dimension names and order", "OBS_STATUS codes",
                   "whether a release stamp is exposed", "rate limits", "licence"],
    },
}
LIVE_VERIFICATION = {
    provider: {"status": "unverified-live", "checked": None, "evidence": None,
               "intended": "verified-live after a dated bounded run (WC13, #2810)",
               "note": "no dated live run: the publisher hosts are blocked by this runtime's egress proxy; authored "
                       "offline fixtures only"}
    for provider in PROVIDERS
}
# Eurostat OBS_FLAG letters (verify against the live code list).
FLAG_MEANINGS = {
    "b": "break in time series", "p": "provisional", "e": "estimated", "c": "confidential", "u": "low reliability",
    "d": "definition differs", "s": "Eurostat estimate", "z": "not applicable", "n": "not significant",
}
BREAK_FLAGS = {"b"}
CONFIDENTIAL_FLAGS = {"c"}
# OECD OBS_STATUS codes (SDMX cross-domain code list; verify).
OECD_STATUS_MEANINGS = {"A": "normal value", "B": "break in time series", "E": "estimated value",
                        "P": "provisional value", "M": "missing value", "L": "missing value; data exist but were not "
                                                                               "collected"}
# The pinned EEA columns: identity and transfer fields only. Nothing names, locates or contacts an operator.
EEA_COLUMNS = ("FacilityInspireId", "reportingYear", "wasteClassification", "wasteTreatment", "transboundary",
               "totalWasteQuantityTNE", "methodCode")
FORBIDDEN_COLUMN_WORDS = ("operator", "parent", "company", "address", "street", "postal", "postcode", "city",
                          "contact", "email", "phone", "telephone", "fax", "authority", "person", "name")


class WasteFormatError(ValueError):
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


def series_key(item: Mapping[str, Any]) -> dict[str, Any]:
    """The fields that make a statistical series (never a release, so each release of the same key is a vintage)."""
    def code(field: str) -> Any:
        return dict(item.get(field) or {}).get("code")

    return {
        "provider": item["provider"],
        "dataset": item["dataset"],
        "indicator": dict(item["indicator"]).get("code"),
        "concept": dict(item["indicator"]).get("concept"),
        "waste_category": code("waste_category"),
        "hazard": code("hazard"),
        "activity": code("activity"),
        "operation": code("operation"),
        "unit": code("unit"),
        "area": {"scheme": item["area"]["scheme"], "code": str(item["area"]["code"])},
        "periodicity": item.get("periodicity"),
    }


def transfer_key(item: Mapping[str, Any]) -> dict[str, Any]:
    """A facility transfer row: INSPIRE id, reporting year, hazardousness, R/D and destination."""
    return {"inspire_id": item["inspire_id"], "reporting_year": int(item["reporting_year"]),
            "hazardous": item["hazardous"]["code"], "treatment": item["treatment"]["code"],
            "destination": item["destination"]["code"]}


def transfer_native_id(item: Mapping[str, Any]) -> str:
    key = transfer_key(item)
    return (f"waste-transfer:{key['inspire_id']}:{key['reporting_year']}:{key['hazardous']}:{key['treatment']}:"
            f"{key['destination']}")


# ------------------------------------------------------------------ declarations


def eea_query(country: str, city: str, year: int) -> str:
    """The pinned Discodata SQL: transfer columns only, bounded to the environment.core facility selection."""
    if not re.fullmatch(r"[A-Z]{2}", str(country)) or not re.fullmatch(r"[A-Za-z .-]{1,60}", str(city)):
        raise WasteFormatError("unbounded_document", "the EEA selection names one country code and one city")
    columns = ", ".join(f"w.{c}" for c in EEA_COLUMNS)
    return (f"SELECT {columns} FROM [IED].[latest].[OffsiteWasteTransfer] w JOIN [IED].[latest].[Facility] f "
            "ON f.FacilityInspireId = w.FacilityInspireId "
            f"WHERE f.countryCode = '{country}' AND f.city = '{city}' AND w.reportingYear = {int(year)} "
            "ORDER BY w.FacilityInspireId, w.wasteClassification, w.wasteTreatment, w.transboundary")


def selected_columns(query: str) -> list[str]:
    """The column list of a pinned SELECT (for the minimisation check)."""
    match = re.match(r"\s*SELECT\s+(.*?)\s+FROM\s", query, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    return [c.strip().split(".")[-1] for c in match.group(1).split(",")]


def forbidden_columns(query: str) -> list[str]:
    return [c for c in selected_columns(query) if any(w in c.casefold() for w in FORBIDDEN_COLUMN_WORDS)]


def waste_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    config = dict(source.get("waste") or {})
    provider, fmt = config.get("provider"), config.get("format")
    if provider not in PROVIDERS or FORMATS.get(str(fmt), {}).get("provider") != provider:
        raise SourcePackError("invalid_manifest", "waste sources declare a known provider and its runtime format")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host != PROVIDER_HOSTS[provider]:
        raise SourcePackError("invalid_manifest", f"{provider} documents are fetched from {PROVIDER_HOSTS[provider]}")
    if dict(source.get("auth") or {}).get("kind") != "none":
        raise SourcePackError("invalid_manifest", "waste sources carry no credential")
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
        except (WasteFormatError, ValueError) as exc:
            raise SourcePackError("invalid_manifest", f"{document.get('label')}: {exc}") from exc
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError("invalid_manifest", "declared documents are HTTPS resources on the endpoint's host")
        urls.append(url)
    if len(set(urls)) != len(urls):
        raise SourcePackError("invalid_manifest", "each declared document is a distinct request")
    flows = [str(d.get("flow")) for d in documents if d.get("flow")]
    if len(set(flows)) != len(flows):
        raise SourcePackError("invalid_manifest", "one document per dataset")
    return {"provider": provider, "format": fmt, "namespace": config.get("namespace") or "environment",
            "documents": documents, "live_verification": config.get("live_verification") or "unverified-live"}


def check_document(fmt: str, document: Mapping[str, Any]) -> None:
    for key in ("label", "definition", "references"):
        if not document.get(key):
            raise WasteFormatError("invalid_document", f"a document states its {key}")
    release = document.get("release")
    if release is not None and iso_day(dict(release).get("published_on")) is None:
        raise WasteFormatError("invalid_document", "a declared release states its publication date")
    for ref in document.get("references") or []:
        if not text(dict(ref).get("identifier")) or not text(dict(ref).get("kind")):
            raise WasteFormatError("invalid_document", "each reference states its kind and identifier as published")
    if fmt == "eea-waste-transfers-json":
        selection = dict(document.get("selection") or {})
        if not re.fullmatch(r"\d{4}", str(selection.get("reporting_year") or "")):
            raise WasteFormatError("unbounded_document", "an EEA document names one reporting year")
        if int(selection.get("limit") or 0) != CAPS[TRANSFER_PROVIDER]["rows_per_response"]:
            raise WasteFormatError("unbounded_document", "an EEA document pins nrOfHits at the audited cap (500)")
        if selection.get("facility_selection") != "environment.core eea-industry":
            raise WasteFormatError("invalid_document", "the EEA selection reuses the environment.core facility "
                                                       "selection")
        query = eea_query(str(selection.get("country")), str(selection.get("city")), int(selection["reporting_year"]))
        if forbidden_columns(query):
            raise WasteFormatError("minimisation", "the pinned query selects a personal or operator column")
        return
    if not document.get("flow") or not document.get("key"):
        raise WasteFormatError("unbounded_document", "SDMX documents name a dataflow and a series key")
    params = dict(document.get("params") or {})
    if not re.fullmatch(r"\d{4}", str(params.get("startPeriod") or "")):
        raise WasteFormatError("unbounded_document", "SDMX documents pin a declared start year")
    if document.get("periodicity") not in PERIODICITIES:
        raise WasteFormatError("invalid_document", f"a document states its periodicity ({PERIODICITIES})")
    indicators = dict(dict(document.get("indicator") or {}).get("codes") or {})
    if not indicators:
        raise WasteFormatError("invalid_document", "SDMX documents map the indicator codes")
    for spec in indicators.values():
        if dict(spec).get("concept") not in CONCEPTS:
            raise WasteFormatError("invalid_document", f"indicator concept is one of {CONCEPTS}")
    area = dict(document.get("area") or {})
    scheme = "iso3166-1-alpha3" if fmt == "oecd-waste-csv" else "eurostat-geo"
    if not area.get("dimension") or area.get("scheme") != scheme:
        raise WasteFormatError("invalid_document", f"the area dimension is declared with scheme {scheme}")
    if fmt == "oecd-waste-csv" and not dict(document.get("dataflow") or {}).get("verify"):
        raise WasteFormatError("invalid_document", "the OECD dataflow agency, id, version and dimensions stay "
                                                   "marked verify until the live run")


def document_key(provider: str, document: Mapping[str, Any]) -> str:
    """Identity of a declared document across releases (removal detection compares releases of one document)."""
    if provider == TRANSFER_PROVIDER:
        selection = dict(document["selection"])
        return (f"eea-waste-transfers:{selection['country']}:{selection['city']}:{selection['reporting_year']}")
    return f"{provider}:{document['flow']}:{document['key']}"


def document_url(fmt: str, document: Mapping[str, Any]) -> str:
    if fmt == "eea-waste-transfers-json":
        selection = dict(document["selection"])
        query = eea_query(str(selection["country"]), str(selection["city"]), int(selection["reporting_year"]))
        return "https://discodata.eea.europa.eu/sql?" + urlencode(
            sorted({"query": query, "p": "1", "nrOfHits": str(int(selection["limit"]))}.items()))
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector

    agency = "OECD" if fmt == "oecd-waste-csv" else "ESTAT"
    url, query = SDMXConnector(agency).csv_url(str(document["flow"]), str(document["key"]),
                                               dict(document.get("params") or {}))
    return url + "?" + urlencode(sorted(query.items()))


# ------------------------------------------------------------------ parsing


def _definition(document: Mapping[str, Any], provider: str, **extra: Any) -> dict[str, Any]:
    definition = dict(document["definition"])
    return {
        "provider": provider,
        "source_text": text(definition.get("source_text")),
        "scope": text(definition.get("scope")),
        "methodology_notes": [str(n) for n in definition.get("methodology_notes") or []],
        "references": [dict(r) for r in document.get("references") or []],
        **extra,
    }


def _declared_release(document: Mapping[str, Any]) -> tuple[str | None, str | None]:
    release = dict(document.get("release") or {})
    return iso_day(release.get("published_on")), text(release.get("label"))


def _labelled(spec: Mapping[str, Any], code: str) -> dict[str, Any]:
    label = dict(spec.get("labels") or {}).get(code)
    return {"label": label} if label else {}


def _dimension(spec: Mapping[str, Any] | None, dims: Mapping[str, str], role: str) -> dict[str, Any]:
    """A key part (waste category, hazardousness, activity, operation, unit) as published, or not applicable."""
    spec = dict(spec or {})
    if spec.get("dimension"):
        value = dims.get(spec["dimension"])
        if value is None:
            raise WasteFormatError("schema_drift", f"the response lacks the declared {role} dimension")
        return {"code": value, **({"scheme": spec["scheme"]} if spec.get("scheme") else {}), **_labelled(spec, value)}
    if spec.get("code"):
        return {"code": str(spec["code"]), **({"scheme": spec["scheme"]} if spec.get("scheme") else {}),
                **({"label": spec["label"]} if spec.get("label") else {})}
    return {"code": "not_applicable"}


def parse_sdmx(raw: bytes, *, fmt: str, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    """Declared Eurostat or OECD SDMX-CSV series as items; flags verbatim, key parts as published."""
    from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector
    from src.integrations.common import IntegrationError

    provider = FORMATS[fmt]["provider"]
    oecd = fmt == "oecd-waste-csv"
    connector = SDMXConnector("OECD" if oecd else "ESTAT")
    ref = SeriesRef(locator=f"{document['flow']}/{document['key']}", metadata={"flow": str(document["flow"])},
                    title=document.get("label"))
    try:
        records = connector.parse_csv(RawSeries(ref, raw, content_type="text/csv", source_url=url, fetched_at=0))
    except IntegrationError as exc:
        raise WasteFormatError("schema_drift", f"{exc.code}: {exc}") from exc
    if not records:
        raise WasteFormatError("schema_drift", "the response states no series")
    if len(records) > CAPS[provider]["series_per_response"]:
        raise WasteFormatError("budget_exhausted", "the response has more series than the audited cap")
    indicator_spec = dict(document["indicator"])
    area_spec = dict(document["area"])
    items, flows, last_update = [], set(), None
    for record in records:
        meta = record.metadata
        dims = {str(k): str(v) for k, v in dict(meta["dimensions"]).items()}
        flows.add(meta.get("dataflow"))
        last_update = meta.get("provider_last_update_at") or last_update
        if area_spec["dimension"] not in dims:
            raise WasteFormatError("schema_drift", "the response lacks the declared area dimension")
        if indicator_spec.get("dimension"):
            if indicator_spec["dimension"] not in dims:
                raise WasteFormatError("schema_drift", "the response lacks the declared indicator dimension")
            indicator_code = dims[indicator_spec["dimension"]]
        else:
            indicator_code = str(document["flow"])
        indicator = dict(indicator_spec["codes"]).get(indicator_code)
        if indicator is None:
            raise WasteFormatError("schema_drift", f"unmapped indicator code {indicator_code!r}")
        area_code = dims[area_spec["dimension"]]
        observations, breaks = [], []
        for observation in sorted(record.observations, key=lambda o: o.period):
            attributes = {str(k): str(v) for k, v in dict(meta["observation_attributes"].get(observation.period)
                                                         or {}).items()}
            value_text = meta["original_values"].get(observation.period)
            value = decimal_text(value_text)
            if oecd:
                flag = attributes.get("OBS_STATUS") or ""
                flags = {"OBS_STATUS": flag} if flag else {}
                meanings = [OECD_STATUS_MEANINGS.get(flag, f"OBS_STATUS {flag} as published")] if flag else []
                status = "reported" if value is not None else "not_published"
                is_break = flag == "B"
            else:
                flag = attributes.get("OBS_FLAG") or ""
                letters = set(flag)
                flags = {"OBS_FLAG": flag} if flag else {}
                meanings = sorted({FLAG_MEANINGS.get(letter, letter) for letter in letters})
                status = "reported" if value is not None else (
                    "confidential" if letters & CONFIDENTIAL_FLAGS else "not_published")
                is_break = bool(letters & BREAK_FLAGS)
            period = str(observation.period).strip()
            if not re.fullmatch(r"\d{4}", period):
                raise WasteFormatError("schema_drift", f"period {period!r} is not a reference year")
            if is_break:
                breaks.append(period)
            observations.append({
                "period": period,
                "value_text": value_text if value_text not in (None, "") else None,
                "value": value if status == "reported" else None,
                "status": status,
                "flags": flags,
                "flag_meanings": meanings,
                "attributes": {k: attributes[k] for k in sorted(attributes) if k not in {"OBS_FLAG", "OBS_STATUS"}},
            })
        notes = []
        if breaks:
            notes.append({"kind": "break", "attribute": "OBS_STATUS" if oecd else "OBS_FLAG",
                          "value": "B" if oecd else "b", "periods": breaks,
                          "statement": f"{document['label']}: the source flags a break in series"})
        provisional = [o["period"] for o in observations
                       if "p" in o["flags"].get("OBS_FLAG", "") or o["flags"].get("OBS_STATUS") == "P"]
        if provisional:
            notes.append({"kind": "provisional", "attribute": "OBS_STATUS" if oecd else "OBS_FLAG",
                          "value": "P" if oecd else "p", "periods": provisional,
                          "statement": f"{document['label']}: the source marks these periods provisional"})
        unit = _dimension(document.get("unit"), dims, "unit")
        items.append({
            "kind": "series",
            "provider": provider,
            "dataset": str(document["flow"]),
            "native_key": ".".join(dims[k] for k in dims),
            "dataflow": {"reference": str(document["flow"]), "stated": meta.get("dataflow"),
                         **({"verify": dict(document.get("dataflow") or {}).get("verify")} if oecd else {})},
            "indicator": {"code": indicator_code, "concept": indicator["concept"],
                          "label": indicator.get("label") or indicator_code},
            "waste_category": _dimension(document.get("waste_category"), dims, "waste category"),
            "hazard": _dimension(document.get("hazard"), dims, "hazardousness"),
            "activity": _dimension(document.get("activity"), dims, "activity"),
            "operation": _dimension(document.get("operation"), dims, "treatment operation"),
            "unit": unit,
            "area": {"scheme": area_spec["scheme"], "code": area_code, **_labelled(area_spec, area_code)},
            "periodicity": document["periodicity"],
            "frequency": record.frequency if record.frequency != "irregular" else "annual",
            "dimensions": dims,
            "definition": _definition(document, provider, indicator=indicator_code, unit=unit.get("code"),
                                      periodicity=document["periodicity"]),
            "references": [dict(r) for r in document.get("references") or []],
            "source_notes": notes,
            "observations": observations,
        })
    items.sort(key=lambda i: i["native_key"])
    stated = sorted(f for f in flows if f)
    flow_version = None
    if stated:
        match = re.search(r"\((\d+(?:\.\d+)*)\)\s*$", stated[0]) or re.search(r",(\d+(?:\.\d+)*)$", stated[0])
        flow_version = match.group(1) if match else None
    declared_on, declared_label = _declared_release(document)
    if last_update and not oecd:
        stamp = datetime.fromisoformat(last_update)
        stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
        published_on, published_at, basis = stamp.date().isoformat(), stamp.isoformat(), "provider_last_update"
    elif declared_on:
        published_on, published_at, basis = declared_on, None, "declared_release"
    else:
        published_on, published_at, basis = None, None, "retrieval_time"
    return {"items": items, "published_on": published_on, "published_at": published_at, "release_basis": basis,
            "release_label": declared_label or (f"LAST UPDATE {last_update}" if last_update and not oecd else None),
            "dataflow_version": flow_version, "complete": True, "truncated": False}


def parse_eea(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    """One bounded Discodata page of waste transfers as transfer items; a full page is labelled truncated."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WasteFormatError("schema_drift", "Discodata response is not JSON") from exc
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise WasteFormatError("schema_drift", "Discodata response has no results list")
    selection = dict(document["selection"])
    limit = int(selection["limit"])
    if len(rows) > limit:
        raise WasteFormatError("budget_exhausted", "Discodata returned more rows than nrOfHits")
    year = int(selection["reporting_year"])
    items, seen = [], set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(EEA_COLUMNS) - set(row):
            raise WasteFormatError("schema_drift", f"Discodata row {index} lacks the pinned transfer columns")
        extra = sorted(set(row) - set(EEA_COLUMNS))
        if extra:
            raise WasteFormatError("schema_drift", f"Discodata row {index} carries unpinned columns {extra}")
        if int(row["reportingYear"]) != year:
            raise WasteFormatError("schema_drift", "Discodata answered for another reporting year")
        hazard, treatment = str(row["wasteClassification"]), str(row["wasteTreatment"])
        destination = str(row["transboundary"]).upper()
        if hazard not in TRANSFER_HAZARD or treatment not in TRANSFER_TREATMENT or \
                destination not in TRANSFER_DESTINATION:
            raise WasteFormatError("schema_drift", f"Discodata row {index} uses unaudited transfer codes")
        method = text(row.get("methodCode"))
        if method is not None and method not in METHOD_CODES:
            raise WasteFormatError("schema_drift", f"Discodata row {index} uses an unaudited method code")
        quantity = decimal_text(row.get("totalWasteQuantityTNE"))
        if quantity is None:
            raise WasteFormatError("schema_drift", f"Discodata row {index} states no quantity")
        item = {
            "kind": "transfer",
            "provider": TRANSFER_PROVIDER,
            "inspire_id": str(row["FacilityInspireId"]),
            "reporting_year": year,
            "hazardous": {"code": hazard, "label": TRANSFER_HAZARD[hazard]},
            "treatment": {"code": treatment, "label": TRANSFER_TREATMENT[treatment]},
            "destination": {"code": destination, "label": TRANSFER_DESTINATION[destination]},
            "quantity": quantity,
            "quantity_text": str(row["totalWasteQuantityTNE"]),
            "unit": {"code": "t", "label": "tonnes (as published)"},
            "method": {"code": method, "label": METHOD_CODES.get(method or "", "not stated")},
            "facility_ref": f"eea-industry:{row['FacilityInspireId']}",
            "definition": _definition(document, TRANSFER_PROVIDER, threshold_note=(
                "facilities report only above the E-PRTR thresholds (verify), so an absent row is not zero")),
        }
        native = transfer_native_id(item)
        if native in seen:
            raise WasteFormatError("schema_drift", "Discodata states the same transfer row twice")
        seen.add(native)
        items.append(item)
    items.sort(key=transfer_native_id)
    truncated = len(rows) >= limit
    version = text(payload.get("datasetVersion")) if isinstance(payload, dict) else None
    published = iso_day(payload.get("datasetPublished")) if isinstance(payload, dict) else None
    if published:
        basis = "provider_dataset_version"
    else:
        basis = "retrieval_time"
    return {"items": items, "published_on": published, "published_at": None, "release_basis": basis,
            "release_label": f"EEA dataset version {version}" if version else None,
            "dataflow_version": version or None, "complete": not truncated, "truncated": truncated,
            "dataset_version": {"stated": version, "published_on": published,
                                "basis": "stated by the EEA in the response" if version else
                                "not stated; the retrieval time dates this acquisition (retrieval_time)"}}


# ------------------------------------------------------------------ adapter


class WasteAdapter:
    """Fetch the declared documents on the runtime's default transport; one page (one release) per document."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        del secret  # no waste source carries a credential
        self.source = json.loads(json.dumps(source))
        self.declared = waste_declaration(self.source)
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
            "waste": {"provider": provider, "format": self.declared["format"],
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
            raise SourcePackError("parameter_forbidden", "waste runs fetch the declared documents only")

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
        url = document_url(fmt, document)
        raw, origin = self._get(url)
        try:
            release = parse_eea(raw, document=document) if fmt == "eea-waste-transfers-json" else parse_sdmx(
                raw, fmt=fmt, document=document, url=url)
        except WasteFormatError as exc:
            raise SourcePackError("budget_exhausted" if exc.code == "budget_exhausted" else "schema_drift",
                                  f"{exc.code}: {exc}") from exc
        items = release["items"]
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(items) > limit:
            # Never a truncated statistical release: a missing series would read as a series the source removed.
            raise SourcePackError("budget_exhausted", "release has more items than the run's result budget")
        header = {
            "contract": RELEASE_CONTRACT, "provider": provider, "format": fmt, "document": document,
            "document_key": document_key(provider, document), "published_on": release["published_on"],
            "published_at": release["published_at"], "release_basis": release["release_basis"],
            "release_label": release["release_label"], "dataflow_version": release["dataflow_version"],
            "file_sha256": hashlib.sha256(raw).hexdigest(), "content_sha256": digest(items),
            "item_count": len(items), "complete": release["complete"], "truncated": release["truncated"],
            "evidence_origin": origin, "live_verification": LIVE_VERIFICATION[provider]["status"], "url": url,
            **({"dataset_version": release["dataset_version"]} if "dataset_version" in release else {}),
            **({"eea_truncated": True} if release["truncated"] else {}),
        }
        records = [{
            "id": f"{header['file_sha256'][:16]}:{number}",
            "title": f"{document['label']} ({release['published_on'] or 'retrieved'})",
            "url": url, "language": "en", "published_at": release["published_on"],
            "content": canonical(item), "waste_release": header, "waste_item": item,
        } for number, item in enumerate(items)]
        if not records:
            # An empty bounded page still records the release (no row was reported for the selection).
            records = [{"id": f"{header['file_sha256'][:16]}:empty", "title": f"{document['label']} (no rows)",
                        "url": url, "language": "en", "published_at": release["published_on"],
                        "content": canonical({"empty": True}), "waste_release": header, "waste_item": None}]
        receipt = {"status": 200, "provider": provider, "document": document["label"],
                   "published_on": release["published_on"], "release_basis": release["release_basis"],
                   "file_sha256": header["file_sha256"], "items": len(items), "requests": 1,
                   "truncated": release["truncated"], "evidence_origin": origin,
                   "final_page": index + 1 >= len(documents)}
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


ADAPTERS = {CONNECTOR: WasteAdapter}


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
    adapter = WasteAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
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
    "EEA_COLUMNS",
    "EXCLUSIONS",
    "FIXTURE_SECRET",
    "FORMATS",
    "LIVE_VERIFICATION",
    "NEVER_SENTENCE",
    "PROVIDERS",
    "PROVIDER_CONTRACTS",
    "SERIES_PROVIDERS",
    "STATUSES",
    "TRANSFER_PROVIDER",
    "WasteAdapter",
    "WasteFormatError",
    "document_key",
    "document_url",
    "eea_query",
    "fixture_request",
    "fixture_transport",
    "forbidden_columns",
    "parse_eea",
    "parse_sdmx",
    "replay_native_fixture",
    "selected_columns",
    "series_key",
    "transfer_key",
    "transfer_native_id",
    "unverified",
]
