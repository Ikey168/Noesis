"""Extractives and mineral-resource sources for the Economics ``extractives`` features (#2653, EX01 and EX03-EX05).

Three providers are recorded under an access contract (:data:`PROVIDER_CONTRACTS`), each implemented as a format
of the ``extractives`` source-pack connector:

* **EITI summary data** (``eiti``, format ``eiti-summary-json``) - one country's summary data for one fiscal period
  (the structured summary of an EITI report) read from the EITI open-data API as JSON. Government-reported
  revenues, company-reported and government-reported payments per company, revenue stream (with the GFS code the
  report states), government agency and project as reported, and the report's own reconciliation discrepancies
  are kept as separate lines, each amount with the currency the report states. Nothing is converted, summed or
  reconciled further. Contact persons and any field naming a natural person are dropped by the parser and refused
  by the store (EX01 minimisation decision); a company the report flags as a natural person is redacted.
* **USGS Mineral Commodity Summaries** (``usgs-mcs``, format ``usgs-mcs-world-csv``) - the annual MCS data
  release's world production, capacity and reserves table (a CSV on ScienceBase). Each annual release is one
  vintage; the document declares the header names (verify against the release's metadata XML) and which columns
  carry each statistic, year and estimated flag. ``W`` (withheld) and ``NA`` (not available) stay statuses without
  a value; a trailing ``e`` marks a published estimate and ``r`` a published revision.
* **BGS World Mineral Statistics** (``bgs-wms``, format ``bgs-ogcapi-json``) - production, import and export
  figures from the BGS OGC API Features collection ``world-mineral-statistics`` (GeoJSON). Each BGS publication
  (World Mineral Production edition) is a vintage declared per document. BGS and USGS figures for the same
  commodity and country are always different series.

Every provider is ``unverified-live`` until a dated live run (``docs/development/extractives-evidence/``); endpoint,
parameter and field names marked *verify* come from search-result summaries of the providers' documentation (the
official pages could not be fetched from this runtime on 2026-09-30). Nothing here scores corruption or governance
risk, estimates reserves, forecasts prices or reconciles payment discrepancies beyond what EITI publishes.
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

CONNECTOR = "extractives"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-extractives-release-v1"
NEVER_SENTENCE = (
    "Published extractive-sector payments and mineral statistics as each source released them: government- and "
    "company-reported figures and EITI's own discrepancies side by side, USGS and BGS series never blended, "
    "withheld values never filled, amounts never converted or summed across reports; no risk scoring, no reserve "
    "estimate and no forecast."
)
EXCLUSIONS = (
    "reconciling payment discrepancies beyond those the EITI report publishes",
    "own reserve or resource estimates",
    "corruption or governance risk scoring",
    "price or production forecasts",
    "converting currencies or summing amounts across reports",
    "blending USGS and BGS series or filling withheld values",
    "inferring project ownership",
)

FEATURES = ("extractives-eiti", "extractives-usgs", "extractives-bgs")
PROVIDER_FEATURES = {"eiti": "extractives-eiti", "usgs-mcs": "extractives-usgs", "bgs-wms": "extractives-bgs"}
PROVIDER_HOSTS = {
    "eiti": {"eiti.org"},
    "usgs-mcs": {"www.sciencebase.gov"},
    "bgs-wms": {"ogcapi.bgs.ac.uk"},
}
READ_ON = "2026-09-30"
UNFETCHED = ("the official page could not be fetched from this runtime on 2026-09-30 (egress blocked); recorded "
             "from search-result summaries only - unverified")

PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "eiti": {
        "delivers": "EITI summary data per country and fiscal period: government revenues by GFS-classified revenue "
        "stream and agency, company payments (company- and government-reported) by company and project as "
        "reported, and the report's reconciliation discrepancies",
        "access_decision": "unverified-live",
        "reason": "the EITI states that summary data is downloadable as CSV and served through an API; the API "
        "endpoint, its JSON field names and the reuse licence could not be read from the official pages",
        "access": "api (EITI open-data API, JSON; bulk CSV per country page also served)",
        "entry_points": [("https://eiti.org/api/v1.0/summary_data (search-result summary; verify path, filters "
                          "and paging)")],
        "authentication": "none stated (verify)",
        "rate_limits": "none published (verify); each run is bounded by the declared documents, the source's "
                       "max_pages and max_results",
        "identifiers": {
            "report": "summary-data id and the report label and version the summary states (verify field names)",
            "country": "ISO 3166-1 alpha-3 as the summary states",
            "revenue_stream": "IMF GFS classification code and label as the report states (EITI summary data "
                              "classifies revenue streams by GFS)",
            "company": "name as reported plus any identifier the report states (registry number, LEI; verify)",
            "project": "project name and any identifier as reported (project-level reporting, Requirement 4.7)",
            "commodity": "HS codes as the summary data states (EITI summary data uses HS codes for commodities)",
        },
        "update_cadence": "per report: each implementing country publishes annually; summary data files are "
                          "imported to eiti.org when submitted",
        "temporal_semantics": "a declared report publication date dates the revision; else the summary's change "
                              "stamp, else the retrieval time (labelled); the fiscal period is the reference period",
        "revision_model": "a re-published summary for the same country and fiscal period with changed content is a "
                          "new report revision; a withdrawn summary is a revision stating its withdrawal, never a "
                          "deletion",
        "discrepancies": "only the discrepancies the report publishes (government minus company figures, with the "
                         "report's explanation); nothing is reconciled further",
        "personal_data": "contact persons (name, e-mail, telephone) and any natural-person field are excluded; a "
                         "company flagged as a natural person is stored redacted",
        "terms": "EITI open-data policy expects open licensing of EITI data; the exact licence of eiti.org summary "
                 "data is unverified - the operator must confirm reuse and attribution terms before a live run",
        "retained_evidence": "raw JSON per request (file digest), report label and version, excluded field names",
        "coverage": "declared countries and fiscal periods only",
        "unavailable_fallback": "a failed request leaves earlier report revisions current and is reported",
        "read": {"https://eiti.org/open-data": UNFETCHED,
                 "https://eiti.org/how-we-collect-and-publish-eiti-summary-data": UNFETCHED,
                 "https://eiti.org/guidance-notes/eiti-summary-data-template": UNFETCHED},
        "verify": ["API path, filters and paging",
                   ("JSON field names (revenue_government, revenue_company, reconciliation)"),
                   "licence and attribution", "how a corrected summary is signalled"],
    },
    "usgs-mcs": {
        "delivers": "USGS Mineral Commodity Summaries world production, capacity and reserves by commodity and "
                    "country, one vintage per annual release",
        "access_decision": "unverified-live",
        "reason": "annual data releases on ScienceBase (MCS2025_World_Data.csv published 2025-01-31 per search "
                  "results); headers and value conventions not yet read from the release metadata",
        "access": "download (ScienceBase data release file, CSV with a metadata XML)",
        "entry_points": [
            "https://www.sciencebase.gov/catalog/item/677eaf95d34e760b392c4970 (MCS 2025 data release; verify)",
            ("https://www.sciencebase.gov/catalog/item/6798fd34d34ea8c18376e8ee (world production, capacity and "
             "reserves; verify)"),
            "https://www.sciencebase.gov/catalog/file/get/{item}?name=MCS{year}_World_Data.csv (verify)",
        ],
        "authentication": "none",
        "rate_limits": "none published (verify); one file request per declared document",
        "identifiers": {"commodity": "COMMODITY and TYPE columns as published (verify header names)",
                        "country": "country names as published (aggregates such as World total stay distinct)"},
        "update_cadence": "annual (late January / early February)",
        "temporal_semantics": "the declared data-release publication date dates the vintage; the year columns are "
                              "the reference periods",
        "revision_model": "each annual release is a new vintage; a year re-stated in a later release is a revision; "
                          "earlier vintages stay",
        "value_conventions": "W withheld (company proprietary data), NA not available, e estimated, r revised "
                             "(verify against the release metadata); withheld values are never filled",
        "terms": "USGS-authored data are generally US public domain; USGS asks for citation of the data release "
                 "(verify on the release page)",
        "retained_evidence": "raw CSV per request (file digest) with the declared header mapping",
        "coverage": "declared commodities and countries only",
        "unavailable_fallback": "a failed request leaves earlier vintages current",
        "read": {"https://www.usgs.gov/centers/national-minerals-information-center/mineral-commodity-summaries":
                 UNFETCHED,
                 "https://www.sciencebase.gov/catalog/item/6798fd34d34ea8c18376e8ee": UNFETCHED},
        "verify": ["CSV header names per release", "estimated/revised markers", "file URL pattern", "citation text"],
    },
    "bgs-wms": {
        "delivers": "BGS World Mineral Statistics production, imports and exports by commodity, country and year, "
                    "one vintage per BGS publication",
        "access_decision": "unverified-live",
        "reason": "OGC API Features collection world-mineral-statistics (1970 onwards) and an OGC WFS are "
                  "documented per search results; property names not yet read from a live response",
        "access": "api (BGS OGC API Features, GeoJSON items; WFS 2.0 also served at ogc2.bgs.ac.uk)",
        "entry_points": ["https://ogcapi.bgs.ac.uk/collections/world-mineral-statistics/items?f=json (verify)"],
        "authentication": "none",
        "rate_limits": "none published (verify); one page per declared document, never truncated",
        "identifiers": {"commodity": "bgs_commodity_trans and erml_commodity as published",
                        "country": "country_trans as published (verify whether an ISO code property is served)",
                        "statistic": "statistic type (production, imports, exports) as published (verify)"},
        "update_cadence": "annual (World Mineral Production)",
        "temporal_semantics": "the declared publication (edition) date dates the vintage; the year property is the "
                              "reference period",
        "revision_model": "each BGS publication is a new vintage; a figure changed in a later edition is a "
                          "revision; earlier vintages stay",
        "terms": "Open Government Licence; acknowledgement 'Contains British Geological Survey materials (c) UKRI "
                 "[year]' and a link to the OGL where possible (search-result summary of the data.gov.uk record; "
                 "verify)",
        "attribution": "Contains British Geological Survey materials (c) UKRI {year}",
        "retained_evidence": "raw GeoJSON per request (file digest), numberMatched/numberReturned",
        "coverage": "declared commodities, countries and years only",
        "unavailable_fallback": "a failed request leaves earlier vintages current",
        "read": {"https://www.bgs.ac.uk/mineralsuk/statistics/world-mineral-statistics/": UNFETCHED,
                 "https://ogcapi.bgs.ac.uk/collections/world-mineral-statistics": UNFETCHED},
        "verify": ["property names", "statistic type labels", "unit labels", "paging (limit/offset)", "licence"],
    },
}
LIVE_VERIFICATION = {
    provider: {
        "status": contract["access_decision"],
        "intended": "verified-live after a dated bounded run (EX13, #2717)",
        "note": "no dated live run from this runtime; offline fixtures only",
    }
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# EX01 personal-data minimisation decision, enforced by the parser and again by the store at write time.
MINIMISATION = {
    "stored": "legal-entity names and identifiers as reported, government agencies, revenue streams, projects, "
              "amounts with currency, discrepancies and explanations as published",
    "excluded": "contact persons, e-mail addresses, telephone numbers, signatories and beneficial-owner names or "
                "any other natural-person field of a summary-data file",
    "redacted": "a reporting entity the report flags as a natural person keeps its payments, with the name "
                "replaced by a redaction marker and no identifiers",
    "retention": "as long as the report revision it came from is retained",
    "access": "knowledge:extractives:read with namespace access; no personal field is stored, so no answer can "
              "return one",
}
PERSONAL_KEYS = frozenset({
    "contact", "contact_person", "contact_name", "contact_email", "email", "e_mail", "phone", "telephone",
    "person", "person_name", "signatory", "beneficial_owner", "beneficial_owners", "date_of_birth",
    "nationality", "home_address", "passport",
})
REDACTED_NAME = "[natural person - redacted]"
# The bounded first coverage (EX01). No record set implies complete coverage of any provider.
BOUNDED_COVERAGE = {
    "countries": {"Chile (CHL)": ["eiti (not an EITI implementing country - none expected)", "usgs-mcs", "bgs-wms"],
                  "Peru (PER)": ["eiti", "usgs-mcs", "bgs-wms"],
                  "Norway (NOR)": ["eiti", "bgs-wms (crude petroleum, natural gas)"]},
    "commodities": ["copper (mine production, reserves)", "lithium", "crude petroleum and natural gas (BGS)"],
    "companies": "the reporting companies of the declared EITI summaries; matched to ownership entities only by "
                 "review",
    "periods": "the two most recent fiscal periods per EITI country; the two most recent MCS releases and BGS "
               "editions",
    "record_cap": "max_results items per response and max_pages requests per run",
}
FORMATS = {
    "eiti-summary-json": {"provider": "eiti", "kind": "eiti_report"},
    "usgs-mcs-world-csv": {"provider": "usgs-mcs", "kind": "commodity_series"},
    "bgs-ogcapi-json": {"provider": "bgs-wms", "kind": "commodity_series"},
}
STATISTICS = ("production", "reserves", "capacity", "imports", "exports")
STATUSES = ("reported", "withheld", "not_available", "symbol_only")
REPORTED_BY = ("government", "company")
USGS_SYMBOLS = {"W": "withheld", "NA": "not_available", "XX": "not_available"}


class ExtractivesFormatError(ValueError):
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
    """A published number as exact decimal text (thousands separators dropped); ``None`` when not a number."""
    raw = text(value)
    if raw is None:
        return None
    raw = raw.replace(",", "").replace(" ", "").replace(" ", "")
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


def personal_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in a value that would carry a natural person's data (EX01 minimisation decision)."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in PERSONAL_KEYS:
                found.append(f"{path}.{key}")
            found += personal_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += personal_keys(item, f"{path}[{index}]")
    return found


def strip_personal(value: Any, path: str = "$") -> tuple[Any, list[str]]:
    """The value without natural-person keys, and the paths removed (recorded as excluded field names only)."""
    if isinstance(value, dict):
        out, removed = {}, []
        for key, item in value.items():
            if str(key).casefold() in PERSONAL_KEYS:
                removed.append(f"{path}.{key}")
                continue
            out[key], sub = strip_personal(item, f"{path}.{key}")
            removed += sub
        return out, removed
    if isinstance(value, list):
        out_list, removed = [], []
        for index, item in enumerate(value):
            kept, sub = strip_personal(item, f"{path}[{index}]")
            out_list.append(kept)
            removed += sub
        return out_list, removed
    return value, []


# ------------------------------------------------------------------ declarations


def extractives_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("extractives") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError("invalid_manifest", "extractives sources declare a known provider and its runtime format")
    documents = list(declared.get("documents") or [])
    if not documents:
        raise SourcePackError("invalid_manifest", "an extractives source declares its documents")
    if len(documents) > int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "more declared requests than the source's page budget")
    host = (urlsplit(source["endpoint"]).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[declared["provider"]]:
        raise SourcePackError("invalid_manifest", "the endpoint is not the provider's documented host")
    urls = []
    for document in documents:
        try:
            check_document(fmt, document)
            url = document_url(document)
        except (ExtractivesFormatError, ValueError) as exc:
            raise SourcePackError("invalid_manifest", str(exc)) from exc
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError("invalid_manifest", "declared documents are HTTPS resources on the endpoint's host")
        urls.append(url)
    if len(set(urls)) != len(urls):
        raise SourcePackError("invalid_manifest", "each declared document is a distinct request")
    return declared


def check_document(fmt: str, document: Mapping[str, Any]) -> None:
    if not text(document.get("label")) or not text(document.get("url")):
        raise ExtractivesFormatError("invalid_document", "a document has a label and a URL")
    release = document.get("release")
    if release is not None and iso_day(dict(release).get("published_on")) is None:
        raise ExtractivesFormatError("invalid_document", "a declared release states its publication date")
    if fmt == "eiti-summary-json":
        country = dict(document.get("country") or {})
        if country.get("scheme") != "iso3166-1-alpha3" or not re.fullmatch(r"[A-Z]{3}", str(country.get("code"))):
            raise ExtractivesFormatError("invalid_document", "an EITI document names its country (ISO alpha-3)")
        period = dict(document.get("fiscal_period") or {})
        if not iso_day(period.get("start")) or not iso_day(period.get("end")):
            raise ExtractivesFormatError("invalid_document", "an EITI document states its fiscal period")
        return
    if release is None:
        raise ExtractivesFormatError("invalid_document", "a USGS or BGS document declares its publication (vintage)")
    if fmt == "usgs-mcs-world-csv":
        columns = dict(document.get("columns") or {})
        for key in ("commodity", "country", "unit"):
            if not text(columns.get(key)):
                raise ExtractivesFormatError("invalid_document", f"a USGS document names its {key} column")
        statistics = list(document.get("statistics") or [])
        if not statistics:
            raise ExtractivesFormatError("invalid_document", "a USGS document maps its statistic columns")
        for stat in statistics:
            stat = dict(stat)
            if stat.get("statistic") not in STATISTICS or not re.fullmatch(r"\d{4}", str(stat.get("year"))) \
                    or not text(stat.get("column")):
                raise ExtractivesFormatError("invalid_document", "each statistic column states statistic, year and "
                                                                 "header")
        return
    properties = dict(document.get("properties") or {})
    for key in ("commodity", "country", "year", "statistic", "quantity", "unit"):
        if not text(properties.get(key)):
            raise ExtractivesFormatError("invalid_document", f"a BGS document names its {key} property")
    for label, statistic in dict(document.get("statistic_labels") or {}).items():
        if statistic not in STATISTICS:
            raise ExtractivesFormatError("invalid_document", f"statistic label {label!r} maps to one of {STATISTICS}")


def document_url(document: Mapping[str, Any]) -> str:
    base = str(document["url"])
    params = {str(k): str(v) for k, v in dict(document.get("params") or {}).items()}
    return base + ("?" + urlencode(sorted(params.items())) if params else "")


# ------------------------------------------------------------------ parsing: EITI


def _declared_release(document: Mapping[str, Any]) -> tuple[str | None, str | None]:
    release = dict(document.get("release") or {})
    return iso_day(release.get("published_on")), text(release.get("label"))


def _company(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    raw = dict(raw) if isinstance(raw, Mapping) else {"name": raw}
    natural = str(raw.get("entity_type") or "").casefold() in {"individual", "natural person", "natural_person"}
    identifiers = []
    for item in raw.get("identifiers") or []:
        item = dict(item)
        if text(item.get("scheme")) and text(item.get("value")):
            identifiers.append({"scheme": str(item["scheme"]).strip().casefold(), "value": str(item["value"]).strip()})
    if text(raw.get("identification")) and text(raw.get("identification_type")):
        identifiers.append({"scheme": str(raw["identification_type"]).strip().casefold(),
                            "value": str(raw["identification"]).strip()})
    return {
        "name_as_reported": REDACTED_NAME if natural else text(raw.get("name")),
        "identifiers": [] if natural else sorted({(i["scheme"], i["value"]): i for i in identifiers}.values(),
                                                 key=lambda i: (i["scheme"], i["value"])),
        "sector": text(raw.get("sector")),
        "commodities": [] if natural else [
            {"name": text(dict(c).get("name")), "hs_code": text(dict(c).get("hs_code"))}
            for c in raw.get("commodities") or [] if isinstance(c, Mapping)],
        "natural_person": natural,
        "redacted": natural,
    }


def _project(raw: Any) -> dict[str, Any] | None:
    if not raw:
        return None
    raw = dict(raw) if isinstance(raw, Mapping) else {"name": raw}
    coordinates = raw.get("coordinates")
    point = None
    if isinstance(coordinates, Sequence) and not isinstance(coordinates, str) and len(coordinates) == 2:
        lon, lat = decimal_text(coordinates[0]), decimal_text(coordinates[1])
        if lon is not None and lat is not None:
            point = {"lon": lon, "lat": lat, "crs": "EPSG:4326 as published (verify)"}
    identifiers = [{"scheme": str(dict(i)["scheme"]).strip().casefold(), "value": str(dict(i)["value"]).strip()}
                   for i in raw.get("identifiers") or [] if text(dict(i).get("scheme")) and text(dict(i).get("value"))]
    if text(raw.get("id")):
        identifiers.append({"scheme": "eiti-project-id", "value": str(raw["id"]).strip()})
    return {"name_as_reported": text(raw.get("name")), "identifiers": sorted(
        {(i["scheme"], i["value"]): i for i in identifiers}.values(), key=lambda i: (i["scheme"], i["value"])),
        "coordinates": point, "commodities": [text(c) for c in raw.get("commodities") or [] if text(c)]}


def _stream(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {"name_as_reported": text(raw.get("revenue_stream") or raw.get("name")),
            "gfs_code": text(raw.get("gfs_code")), "gfs_label": text(raw.get("gfs_label"))}


def _amount(raw: Mapping[str, Any], key: str, default_currency: str | None) -> dict[str, Any]:
    value_text = text(raw.get(key))
    return {"amount_text": value_text, "amount": decimal_text(value_text),
            "currency": text(raw.get("currency")) or default_currency}


def parse_eiti(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    """One EITI summary (country, fiscal period): report, revenue lines, company payments and discrepancies."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtractivesFormatError("schema_drift", "EITI response is not JSON") from exc
    rows = body.get("data") if isinstance(body, Mapping) else None
    if not isinstance(rows, list):
        raise ExtractivesFormatError("schema_drift", "EITI response states no data array")
    country = dict(document["country"])
    period = dict(document["fiscal_period"])
    matching = [r for r in rows if isinstance(r, Mapping)
                and str(dict(r.get("country") or {}).get("iso3") or "").upper() == country["code"]
                and iso_day(r.get("year_start")) == iso_day(period["start"])
                and iso_day(r.get("year_end")) == iso_day(period["end"])]
    if len(matching) != 1:
        raise ExtractivesFormatError("schema_drift", "the response does not state exactly one summary for the "
                                                     "declared country and fiscal period")
    summary, excluded = strip_personal(dict(matching[0]))
    currency = text(summary.get("currency_code"))
    report = dict(summary.get("report") or {})
    status = "withdrawn" if str(summary.get("status") or "").casefold() == "withdrawn" else "published"
    government, company_lines, discrepancies = [], [], []
    for number, line in enumerate(summary.get("revenue_government") or [], start=1):
        line = dict(line)
        government.append({
            "line": number, "reported_by": "government", "level": "government-revenue",
            "agency": {"name_as_reported": text(line.get("government_entity"))},
            "revenue_stream": _stream(line), **_amount(line, "value", currency),
            "in_kind": bool(line.get("in_kind")),
            "budget_reference": dict(line["budget_reference"]) if line.get("budget_reference") else None,
        })
    for number, line in enumerate(summary.get("revenue_company") or [], start=1):
        line = dict(line)
        reported_by = str(line.get("reported_by") or "company").casefold()
        if reported_by not in REPORTED_BY:
            raise ExtractivesFormatError("schema_drift", "a payment line states who reported it")
        company_lines.append({
            "line": number, "reported_by": reported_by, "level": "company-payment",
            "company": _company(line.get("company")), "project": _project(line.get("project")),
            "agency": {"name_as_reported": text(line.get("government_entity"))},
            "revenue_stream": _stream(line), **_amount(line, "value", currency),
            "in_kind": bool(line.get("in_kind")),
            "budget_reference": dict(line["budget_reference"]) if line.get("budget_reference") else None,
        })
    for number, line in enumerate(summary.get("reconciliation") or [], start=1):
        line = dict(line)
        discrepancies.append({
            "line": number, "company": _company(line.get("company")), "revenue_stream": _stream(line),
            "government_amount_text": text(line.get("government_value")),
            "company_amount_text": text(line.get("company_value")),
            "discrepancy_text": text(line.get("discrepancy")),
            "government_amount": decimal_text(line.get("government_value")),
            "company_amount": decimal_text(line.get("company_value")),
            "discrepancy": decimal_text(line.get("discrepancy")),
            "currency": text(line.get("currency")) or currency,
            "explanation": text(line.get("explanation")),
            "basis": "as published by the EITI report (government minus company, as stated)",
        })
    item = {
        "kind": "eiti_report",
        "provider": "eiti",
        "report": {"summary_id": text(summary.get("id")), "label": text(report.get("label")) or document["label"],
                   "version": text(report.get("version")), "published_on": iso_day(report.get("published")),
                   "url": text(report.get("url")), "status": status},
        "country": {"scheme": "iso3166-1-alpha3", "code": country["code"],
                    "label": text(dict(summary.get("country") or {}).get("label"))},
        "fiscal_period": {"start": iso_day(period["start"]), "end": iso_day(period["end"])},
        "currency": currency,
        "government_revenues": government,
        "company_payments": company_lines,
        "discrepancies": discrepancies,
        "commodities": [{"name": text(dict(c).get("name")), "hs_code": text(dict(c).get("hs_code"))}
                        for c in summary.get("commodities") or [] if isinstance(c, Mapping)],
        "excluded_fields": sorted({re.sub(r"\[\d+\]", "[]", p) for p in excluded}),
        "minimisation": "EX01: natural-person fields excluded, natural-person entities redacted",
    }
    declared_on, declared_label = _declared_release(document)
    published_on = declared_on or item["report"]["published_on"]
    changed = text(summary.get("changed"))
    return {
        "provider": "eiti",
        "format": "eiti-summary-json",
        "items": [item],
        "item_count": 1,
        "published_on": published_on,
        "published_at": None if published_on else changed,
        "release_basis": "declared_release" if declared_on else "report_publication" if published_on else (
            "provider_change_stamp" if changed else "retrieval_time"),
        "release_label": declared_label or item["report"]["label"],
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": digest([item]),
        "structure": {"summaries_in_response": len(rows), "summary_id": item["report"]["summary_id"],
                      "excluded_fields": item["excluded_fields"]},
    }


# ------------------------------------------------------------------ parsing: commodity series


def _observation(raw_value: Any, *, estimated: bool = False, revised: bool = False,
                 note: str | None = None) -> dict[str, Any]:
    value_text = text(raw_value)
    flags: dict[str, Any] = {}
    stripped = value_text
    if stripped and re.fullmatch(r".*\d\s*[eE]", stripped):
        estimated, stripped = True, stripped[:-1].strip()
        flags["marker"] = "e"
    if stripped and re.fullmatch(r".*\d\s*r", stripped):
        revised, stripped = True, stripped[:-1].strip()
        flags["marker"] = flags.get("marker", "") + "r"
    if value_text is None:
        status, value = "not_available", None
    elif stripped.upper() in USGS_SYMBOLS:
        status, value = USGS_SYMBOLS[stripped.upper()], None
    else:
        value = decimal_text(stripped)
        status = "reported" if value is not None else "symbol_only"
    return {"value_text": value_text, "value": value, "status": status, "estimated": bool(estimated),
            "revised": bool(revised), "flags": flags, "notes": [note] if note else []}


def _hydrocarbon(document: Mapping[str, Any], commodity: str) -> bool:
    return commodity.casefold() in {str(c).casefold() for c in document.get("hydrocarbons") or []}


def parse_usgs(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    """The MCS world table as series per commodity, type, country, statistic and unit; values as published."""
    try:
        body = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        body = raw.decode("latin-1")
    reader = csv.DictReader(io.StringIO(body))
    headers = set(reader.fieldnames or [])
    columns = dict(document["columns"])
    needed = {columns["commodity"], columns["country"], columns["unit"]} | {
        dict(s)["column"] for s in document["statistics"]}
    missing = sorted(h for h in needed if h not in headers)
    if missing:
        raise ExtractivesFormatError("schema_drift", f"declared USGS headers missing from the file: {missing}")
    wanted_commodities = {str(c).casefold() for c in document.get("commodities") or []}
    wanted_countries = {str(c).casefold() for c in document.get("countries") or []}
    series: dict[str, dict[str, Any]] = {}
    for row in reader:
        commodity = text(row.get(columns["commodity"]))
        country = text(row.get(columns["country"]))
        if not commodity or not country:
            continue
        if wanted_commodities and commodity.casefold() not in wanted_commodities:
            continue
        if wanted_countries and country.casefold() not in wanted_countries:
            continue
        form = text(row.get(columns.get("type") or "")) if columns.get("type") else None
        unit = text(row.get(columns["unit"]))
        for stat in document["statistics"]:
            stat = dict(stat)
            flag_column = stat.get("estimated_column")
            estimated = bool(flag_column and str(row.get(flag_column) or "").strip().casefold()
                             in {"e", "estimated", "true", "yes", "1"})
            note_column = stat.get("notes_column")
            obs = {"period": str(stat["year"]), **_observation(
                row.get(stat["column"]), estimated=estimated,
                note=text(row.get(note_column)) if note_column else None)}
            key = canonical([commodity, form, stat["statistic"], unit, country])
            entry = series.setdefault(key, {
                "kind": "commodity_series", "provider": "usgs-mcs",
                "commodity": {"name": commodity, "form": form, "code": None,
                              "hydrocarbon": _hydrocarbon(document, commodity)},
                "statistic": stat["statistic"], "unit": {"label": unit},
                "country": {"scheme": "usgs-country-name", "name": country, "code": None},
                "definition": {"commodity": commodity, "form": form, "source_notes": []},
                "references": [dict(r) for r in document.get("references") or []],
                "observations": [],
            })
            if any(o["period"] == obs["period"] for o in entry["observations"]):
                raise ExtractivesFormatError("schema_drift", "the file states a series-year twice")
            entry["observations"].append(obs)
    items = [series[k] for k in sorted(series)]
    for item in items:
        item["observations"].sort(key=lambda o: o["period"])
    if not items:
        raise ExtractivesFormatError("schema_drift", "the file states none of the declared commodities")
    declared_on, declared_label = _declared_release(document)
    return {
        "provider": "usgs-mcs", "format": "usgs-mcs-world-csv", "items": items, "item_count": len(items),
        "published_on": declared_on, "published_at": None, "release_basis": "declared_release",
        "release_label": declared_label, "file_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": digest(items),
        "structure": {"headers": sorted(headers), "columns": columns,
                      "publication": dict(document.get("release") or {})},
    }


def parse_bgs(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    """One OGC API Features page of World Mineral Statistics: series per commodity, statistic, unit and country."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtractivesFormatError("schema_drift", "BGS response is not JSON") from exc
    features = body.get("features") if isinstance(body, Mapping) else None
    if not isinstance(features, list):
        raise ExtractivesFormatError("schema_drift", "BGS response states no features")
    matched, returned = body.get("numberMatched"), body.get("numberReturned", len(features))
    if matched is not None and int(matched) > int(returned):
        raise ExtractivesFormatError("truncated", "the page does not hold every matched feature; narrow the request")
    props = dict(document["properties"])
    labels = {str(k).casefold(): v for k, v in dict(document.get("statistic_labels") or {}).items()}
    series: dict[str, dict[str, Any]] = {}
    for feature in features:
        values = dict(dict(feature).get("properties") or {})
        commodity = text(values.get(props["commodity"]))
        country = text(values.get(props["country"]))
        label = text(values.get(props["statistic"]))
        year = text(values.get(props["year"]))
        if not commodity or not country or not label or not year:
            raise ExtractivesFormatError("schema_drift", "a feature lacks a declared property")
        statistic = labels.get(label.casefold()) or label.casefold()
        if statistic not in STATISTICS:
            raise ExtractivesFormatError("schema_drift", f"unmapped statistic type {label!r}")
        unit = text(values.get(props["unit"]))
        code = text(values.get(props["country_code"])) if props.get("country_code") else None
        note = text(values.get(props["note"])) if props.get("note") else None
        key = canonical([commodity, statistic, unit, country])
        entry = series.setdefault(key, {
            "kind": "commodity_series", "provider": "bgs-wms",
            "commodity": {"name": commodity, "form": text(values.get(props.get("commodity_detail") or "")),
                          "code": text(values.get(props.get("commodity_code") or "")),
                          "hydrocarbon": _hydrocarbon(document, commodity)},
            "statistic": statistic, "unit": {"label": unit},
            "country": {"scheme": "bgs-country", "name": country, "code": code,
                        "code_scheme": props.get("country_code_scheme") if code else None},
            "definition": {"commodity": commodity, "form": text(values.get(props.get("commodity_detail") or "")),
                           "source_notes": []},
            "references": [dict(r) for r in document.get("references") or []],
            "observations": [],
        })
        period = year[:4]
        if any(o["period"] == period for o in entry["observations"]):
            raise ExtractivesFormatError("schema_drift", "the page states a series-year twice")
        entry["observations"].append({"period": period, **_observation(values.get(props["quantity"]), note=note)})
    items = [series[k] for k in sorted(series)]
    for item in items:
        item["observations"].sort(key=lambda o: o["period"])
    if not items:
        raise ExtractivesFormatError("schema_drift", "the page states no feature")
    declared_on, declared_label = _declared_release(document)
    return {
        "provider": "bgs-wms", "format": "bgs-ogcapi-json", "items": items, "item_count": len(items),
        "published_on": declared_on, "published_at": None, "release_basis": "declared_release",
        "release_label": declared_label, "file_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": digest(items),
        "structure": {"number_matched": matched, "number_returned": returned, "properties": props,
                      "publication": dict(document.get("release") or {}),
                      "attribution": PROVIDER_CONTRACTS["bgs-wms"]["attribution"].format(
                          year=(declared_on or "")[:4] or "[year]")},
    }


# ------------------------------------------------------------------ adapter


class ExtractivesAdapter:
    """Fetch the declared extractives documents on the runtime's transport; one page (one release) per document."""

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

        del secret  # no extractives provider needs a key
        self.source = json.loads(json.dumps(source))
        self.declared = extractives_declaration(self.source)
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
            "extractives": {
                "provider": self.declared["provider"],
                "format": self.declared["format"],
                "feature": PROVIDER_FEATURES[self.declared["provider"]],
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
            raise SourcePackError("parameter_forbidden", "extractives runs fetch the declared documents only")

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
            headers={"Accept": "application/json, application/geo+json, text/csv"},
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
            raise SourcePackError("rate_limited", "provider asked the client to slow down",
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
        url = document_url(document)
        raw, origin = self._get(url)
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        try:
            if fmt == "eiti-summary-json":
                release = parse_eiti(raw, document=document)
            elif fmt == "usgs-mcs-world-csv":
                release = parse_usgs(raw, document=document)
            else:
                release = parse_bgs(raw, document=document)
        except ExtractivesFormatError as exc:
            if exc.code == "truncated":
                raise SourcePackError("budget_exhausted", str(exc)) from exc
            raise SourcePackError("schema_drift", f"{exc.code}: {exc}") from exc
        if release["item_count"] > limit:
            # Never a truncated release: a missing series would read as a series that was not published.
            raise SourcePackError("budget_exhausted", "release has more items than the run's result budget")
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
            "licence": {k: PROVIDER_CONTRACTS[release["provider"]].get(k) for k in ("terms", "attribution")},
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
                "extractives_release": header,
                "extractives_item": item,
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
ADAPTERS = {CONNECTOR: ExtractivesAdapter}


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


def fixture_request(document: Mapping[str, Any]) -> str:
    """The key :func:`fixture_transport` files a response under."""
    base, _, query = document_url(document).partition("?")
    return _fixture_key(base, parse_qsl(query, keep_blank_values=True))


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = ExtractivesAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
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
    "CONNECTOR",
    "EXCLUSIONS",
    "FEATURES",
    "FORMATS",
    "LIVE_VERIFICATION",
    "MINIMISATION",
    "NEVER_SENTENCE",
    "PROVIDER_CONTRACTS",
    "PROVIDER_FEATURES",
    "STATISTICS",
    "STATUSES",
    "ExtractivesAdapter",
    "ExtractivesFormatError",
    "extractives_declaration",
    "fixture_request",
    "fixture_transport",
    "parse_bgs",
    "parse_eiti",
    "parse_usgs",
    "personal_keys",
    "replay_native_fixture",
]
