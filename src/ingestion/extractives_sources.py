"""Extractives sources for the Economics ``economics.extractives`` provider (#2653, EX01 and EX03-EX05).

Three providers are recorded under an access contract (:data:`PROVIDER_CONTRACTS`), each a format of the
``extractives`` source-pack connector and each its own optional Economics feature:

* **EITI summary data** (``eiti``, format ``eiti-summary-json``, feature ``extractives-eiti``) - one country's
  summary data for one fiscal period and one declared report version: the report, government agencies, revenue
  streams with the government-reported totals, companies as reported (with the identifiers the report publishes),
  projects and licences as reported and company payments with the government-reported and company-reported figures
  kept apart and the report's own reconciliation discrepancy as published. Contact persons and any other personal
  field are dropped at parse time and refused at write time (:data:`MINIMISATION`); a payer the report marks as an
  individual keeps its amounts but never its name or identifier.
* **USGS Mineral Commodity Summaries** (``usgs-mcs``, format ``usgs-mcs-csv``, feature ``extractives-usgs``) - a
  commodity's world production and reserves table from the annual MCS data release (a CSV on ScienceBase) with the
  declared column meaning per year: each annual release is a vintage, estimated columns and ``e``/``r`` markers are
  stored as published and ``W`` (withheld) values stay withheld, never filled.
* **BGS World Mineral Statistics** (``bgs-wms``, format ``bgs-wms-csv``, feature ``extractives-bgs``) - production,
  import and export figures from the BGS World Mineral Statistics download for a declared commodity selection:
  each BGS publication is a vintage, BGS series are separate from USGS series of the same commodity and country,
  and the BGS attribution travels with every series.

Every provider is ``unverified-live`` until a dated live run; endpoints, column names and terms marked *verify*
come from the providers' public documentation as known to the author and were **not re-verified live** (the audit
was written without network access to eiti.org, usgs.gov/sciencebase.gov or bgs.ac.uk). See
``docs/development/extractives-evidence/source-audit.md``. Nothing here reconciles payment discrepancies beyond
what the EITI report states, estimates reserves, scores corruption or governance risk or forecasts prices.
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
    "Published extractive-sector payments, production, reserves and trade figures as each publisher released them: "
    "EITI discrepancies only as the report states them, sources side by side and never blended, currencies never "
    "converted or summed across reports, no own reserve estimate, no corruption or governance risk score and no "
    "price forecast."
)
EXCLUSIONS = (
    "reconciling payment discrepancies beyond those the EITI report publishes",
    "own reserve or production estimates, or filling withheld values",
    "corruption, governance or other risk scoring",
    "price forecasts",
    "blending USGS and BGS (or any two sources') series",
    "converting currencies or summing amounts across reports",
    "inferring project ownership from names or locations",
    "storing names or contact details of natural persons",
)

PROVIDER_HOSTS = {
    "eiti": {"eiti.org"},
    "usgs-mcs": {"www.sciencebase.gov"},
    "bgs-wms": {"www2.bgs.ac.uk"},
}
FEATURES = {"eiti": "extractives-eiti", "usgs-mcs": "extractives-usgs", "bgs-wms": "extractives-bgs"}

PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "eiti": {
        "delivers": "EITI summary data per implementing country, fiscal period and report version: report, "
        "government agencies, revenue streams (GFS-classified), companies, projects and licences as reported, company "
        "payments with government- and company-reported figures and the report's reconciliation discrepancies",
        "access_decision": "unverified-live",
        "reason": "open data without authentication; the API path, the JSON field names of the summary data and the "
        "report-version metadata were not checked against a live response",
        "access": "api (EITI open data API, one summary-data document per country and fiscal period; verify)",
        "format": "JSON rendering of the EITI summary data template v2 (report, government agencies, revenue "
        "streams, companies, projects, payments; verify every field name)",
        "entry_points": ["https://eiti.org/api/v2.0/summary_data/{id} (verify)",
                         "https://eiti.org/open-data (summary data files, verify)"],
        "authentication": "none; no key is handled",
        "rate_limits": "not documented; one request per declared report version, bounded by max_pages",
        "identifiers": {
            "report": "ISO 3166-1 alpha-2 country code plus the fiscal period start and end dates",
            "report_version": "the version the operator declares from the report's publication metadata",
            "revenue_stream": "GFS classification code as published (e.g. 1112E1) plus the stream name",
            "company": "company identification number and register scheme as the report publishes them; else the "
            "company name as reported (never resolved here)",
            "project": "project name and licence numbers as reported",
        },
        "update_cadence": "annual reports per implementing country; revised reports published irregularly",
        "temporal_semantics": "the declared publication date of the report version; the fiscal period is the "
        "reference period",
        "revision_model": "a revised report is a new report version: each changed record adds a revision, a record "
        "absent from the new version of the same report becomes a 'removed' revision; nothing is deleted",
        "terms": "EITI open data: free reuse with attribution to EITI and the national report (verify the licence "
        "statement, stated by EITI as open data; not re-verified live)",
        "licence": {"id": "eiti-open-data", "attribution": "Extractive Industries Transparency Initiative (EITI), "
                    "summary data of the national EITI report"},
        "retained_evidence": "raw JSON digest, report URL and version, row identifiers of each payment",
        "personal_data": "contact persons of national secretariats and reporting entities; payers the report marks "
                         "as individuals (see MINIMISATION)",
        "verify": ["API path", "field names", "report version metadata", "licence statement"],
    },
    "usgs-mcs": {
        "delivers": "USGS Mineral Commodity Summaries world mine production and reserves by country per commodity, "
        "one data release per year",
        "access_decision": "unverified-live",
        "reason": "US federal data release on ScienceBase without authentication; file names and column headers of "
        "each year's release differ and were not checked live",
        "access": "file (the MCS data release CSV of one commodity table on ScienceBase; verify the item and file "
        "names per release)",
        "format": "CSV with one row per country; production columns per year (the latest year estimated) and a "
        "reserves column, the meaning of each column declared per document (verify each header)",
        "entry_points": ["https://www.sciencebase.gov/catalog/file/get/{item}?name={file} (verify)"],
        "authentication": "none",
        "rate_limits": "not documented; one download per declared commodity table",
        "identifiers": {
            "commodity": "the MCS commodity name (copper, lithium, ...) with the declared unit and definition",
            "country": "country names as published; ISO 3166-1 and M49 codes are declared by the operator per name "
                       "(basis 'operator-declared'), never inferred",
        },
        "update_cadence": "annually (late January)",
        "temporal_semantics": "the declared publication date of the annual release; the column year is the "
        "reference period",
        "revision_model": "each annual release is a vintage of the series; a year estimated in one release and "
        "revised in the next shows both vintages; W (withheld to avoid disclosing company proprietary data) stays "
        "withheld",
        "terms": "US Geological Survey data are US federal government works in the public domain; cite USGS "
        "(not re-verified live)",
        "licence": {"id": "us-public-domain", "attribution": "U.S. Geological Survey, Mineral Commodity Summaries"},
        "retained_evidence": "CSV digest, header, row numbers and notes",
        "personal_data": "none",
        "verify": ["ScienceBase item ids", "file names", "column headers", "value markers"],
    },
    "bgs-wms": {
        "delivers": "BGS World Mineral Statistics production, imports and exports by country and commodity",
        "access_decision": "unverified-live",
        "reason": "the BGS World Mineral Statistics download (CSV export of a query) needs no account but requires "
        "accepting the terms in the request; the query parameters, the CSV layout and the licence wording were not "
        "checked live",
        "access": "file (the CSV export of a declared World Mineral Statistics query; verify the query parameters)",
        "format": "CSV: country, commodity, sub-commodity, statistic type (production, imports, exports), year, "
        "quantity, units and notes (verify each header)",
        "entry_points": ["https://www2.bgs.ac.uk/mineralsuk/statistics/wms.cfc?method=... (verify)"],
        "authentication": "none (terms accepted in the query as the download form does; verify)",
        "rate_limits": "not documented; one download per declared query",
        "identifiers": {
            "commodity": "the BGS commodity and sub-commodity names as published",
            "country": "country names as published; ISO 3166-1 and M49 codes declared by the operator per name",
        },
        "update_cadence": "annually (the World Mineral Production publication and the updated statistics)",
        "temporal_semantics": "the declared publication date of the BGS release; the year is the reference period",
        "revision_model": "each BGS publication is a vintage; later publications revise earlier years",
        "terms": "BGS World Mineral Statistics: free to download with the acknowledgement 'British Geological "
        "Survey (c) UKRI'; redistribution terms to be confirmed against the current BGS licence page (Open "
        "Government Licence or BGS non-commercial terms; not re-verified live)",
        "licence": {"id": "bgs-wms-terms", "attribution": "Contains British Geological Survey materials (c) UKRI, "
                    "World Mineral Statistics"},
        "retained_evidence": "CSV digest, header, row numbers and notes",
        "personal_data": "none",
        "verify": ["query parameters", "CSV headers", "licence wording", "publication dates"],
    },
}

LIVE_VERIFICATION = {
    provider: {"status": contract["access_decision"], "note": "no dated live run from this runtime; offline "
               "fixtures only (terms not re-verified live)"}
    for provider, contract in PROVIDER_CONTRACTS.items()
}

# The data-minimisation decision of EX01. Enforced at parse time (dropped) and at write time (refused).
MINIMISATION = {
    "stored": ["company names and the company identifiers the report publishes (legal persons)",
               "government agency names", "project names, licence numbers and published coordinates",
               "amounts, currencies and discrepancies as reported"],
    "excluded": [("contact persons of national secretariats, multi-stakeholder groups and reporting entities "
                  "(names, e-mail addresses, telephone numbers)"),
                 ("beneficial owners that are natural persons (beneficial ownership disclosure is out of scope)"),
                 "signatories and attestation officers"],
    "redacted": [("a payer or licence holder the report marks as an individual (natural person): its name and "
                  "identifier are withheld; its payments are kept under a report-local key")],
    "retention": "records are kept as long as the report revision they belong to; redaction applies to every "
                 "revision",
    "access": "records are readable with knowledge:extractives:read in the namespace; no personal field is stored, "
              "so no tool can return one",
    "personal_keys": ["contact", "contact_person", "contacts", "email", "e_mail", "phone", "telephone",
                      "beneficial_owners", "beneficial_owner", "signatory", "person_name", "date_of_birth",
                      "home_address", "nationality"],
}
PERSONAL_KEYS = frozenset(MINIMISATION["personal_keys"])
INDIVIDUAL_TYPES = frozenset({"individual", "natural person", "natural_person", "person", "sole proprietor"})
WITHHELD_NAME = "[natural person - name withheld]"

# The bounded first coverage (EX01). No record set implies complete coverage of any provider.
BOUNDED_COVERAGE = {
    "eiti": {
        "countries": ["NL (Netherlands EITI)", "DE (D-EITI)"],
        "fiscal_periods": "the two most recent reported fiscal years per country",
        "versions": "every published version of those reports",
        "record_cap": "max_results records per report version (a report is never truncated)",
    },
    "usgs-mcs": {
        "commodities": ["copper (mine production, reserves)", "lithium (mine production, reserves)"],
        "countries": "every country row of the declared tables (Chile, Peru, Australia, United States, world total)",
        "releases": "the two most recent annual releases",
        "record_cap": "max_results series per table",
    },
    "bgs-wms": {
        "commodities": ["copper (mine production; ores and concentrates imports and exports)",
                        "crude petroleum (production)"],
        "countries": ["CL", "PE", "DE", "NL"],
        "years": "five reference years per publication",
        "releases": "the two most recent publications",
        "record_cap": "max_results series per query",
    },
    "justification": "Copper is published by all three sources (EITI payments of mining companies, USGS and BGS "
                     "production), so the side-by-side answer and the trade link (HS 2603) are exercised on one "
                     "commodity; crude petroleum and natural gas exercise the Energy link; the Netherlands and "
                     "Germany are EITI implementing countries whose reports name companies of the Corporate "
                     "Ownership fixtures' groups.",
}

FORMATS = {
    "eiti-summary-json": {"provider": "eiti", "kind": "eiti"},
    "usgs-mcs-csv": {"provider": "usgs-mcs", "kind": "series"},
    "bgs-wms-csv": {"provider": "bgs-wms", "kind": "series"},
}
STATISTICS = ("production", "reserves", "imports", "exports")
SERIES_STATUSES = ("reported", "withheld", "not_available", "qualitative")
FREQUENCIES = ("annual",)


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
    """A published number as exact decimal text; ``None`` for a missing or non-numeric value (never zero)."""
    raw = text(value)
    if raw is None:
        return None
    try:
        number = Decimal(raw.replace(",", "").replace(" ", ""))
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return format(number.normalize(), "f") if number == number.to_integral_value() else format(number, "f")


def iso_day(value: Any) -> str | None:
    raw = text(value)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return None


def slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(value or "").casefold()).strip("-") or "unnamed"


def personal_keys(value: Any, path: str = "$") -> list[str]:
    """Paths of keys that would carry personal data under the minimisation decision."""
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


def _release(document: Mapping[str, Any]) -> dict[str, Any]:
    release = dict(document.get("release") or {})
    return {"published_on": iso_day(release.get("published_on")), "label": text(release.get("label")),
            "version": text(release.get("version"))}


def _licence(provider: str) -> dict[str, Any]:
    return dict(PROVIDER_CONTRACTS[provider]["licence"])


# ------------------------------------------------------------------ declarations


def extractives_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("extractives") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError("invalid_manifest", "extractives sources declare a known provider and its format")
    documents = list(declared.get("documents") or [])
    if not documents:
        raise SourcePackError("invalid_manifest", "an extractives source declares its documents")
    if len(documents) > int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "more declared documents than the source's page budget")
    host = (urlsplit(source["endpoint"]).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[declared["provider"]]:
        raise SourcePackError("invalid_manifest", "the endpoint is not the provider's documented host")
    urls = []
    for document in documents:
        try:
            check_document(fmt, document)
        except ExtractivesFormatError as exc:
            raise SourcePackError("invalid_manifest", str(exc)) from exc
        url = str(document["url"])
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError("invalid_manifest", "declared documents are HTTPS resources on the endpoint's host")
        urls.append(url)
    if len(set(urls)) != len(urls):
        raise SourcePackError("invalid_manifest", "each declared document is a distinct request")
    return declared


def check_document(fmt: str, document: Mapping[str, Any]) -> None:
    if not text(document.get("label")) or not str(document.get("url") or "").startswith("https://"):
        raise ExtractivesFormatError("invalid_document", "a document has a label and an https URL")
    release = _release(document)
    if release["published_on"] is None or not release["version"]:
        raise ExtractivesFormatError("invalid_document", "a document declares its release version and publication "
                                                         "date")
    if fmt == "eiti-summary-json":
        if not re.fullmatch(r"[A-Z]{2}", str(document.get("country") or "")):
            raise ExtractivesFormatError("invalid_document", "an EITI document names its ISO alpha-2 country")
        period = dict(document.get("fiscal_period") or {})
        if iso_day(period.get("start")) is None or iso_day(period.get("end")) is None:
            raise ExtractivesFormatError("invalid_document", "an EITI document declares its fiscal period")
        return
    commodity = dict(document.get("commodity") or {})
    if not commodity.get("code") or not commodity.get("label") or \
            not str(dict(commodity.get("definition") or {}).get("reference") or "").startswith("https://"):
        raise ExtractivesFormatError("invalid_document", "a commodity document declares the commodity code, label "
                                                         "and a definition reference")
    columns = dict(document.get("columns") or {})
    countries = dict(document.get("countries") or {})
    if not countries:
        raise ExtractivesFormatError("invalid_document", "a commodity document declares its country code table")
    for name, codes in countries.items():
        if not re.fullmatch(r"[A-Z]{2}", str(dict(codes).get("iso2") or "")) and not dict(codes).get("aggregate"):
            raise ExtractivesFormatError("invalid_document", f"country {name!r} declares an ISO alpha-2 code or is "
                                                             "an aggregate")
    if fmt == "usgs-mcs-csv":
        if not columns.get("country"):
            raise ExtractivesFormatError("invalid_document", "a USGS document names its country column")
        values = list(document.get("value_columns") or [])
        if not values:
            raise ExtractivesFormatError("invalid_document", "a USGS document declares its value columns")
        for column in values:
            column = dict(column)
            if column.get("statistic") not in ("production", "reserves") or not column.get("column") or \
                    not re.fullmatch(r"\d{4}", str(column.get("year") or "")) or not column.get("unit"):
                raise ExtractivesFormatError("invalid_document", "each value column states statistic, column, year "
                                                                 "and unit")
    elif fmt == "bgs-wms-csv":
        for key in ("country", "statistic", "year", "quantity", "unit"):
            if not columns.get(key):
                raise ExtractivesFormatError("invalid_document", f"a BGS document names its {key} column")
        statistics = dict(document.get("statistics") or {})
        if not statistics or not set(statistics.values()) <= set(STATISTICS):
            raise ExtractivesFormatError("invalid_document", "a BGS document maps its published statistic types to "
                                                             f"{STATISTICS}")


def document_url(document: Mapping[str, Any]) -> str:
    return str(document["url"])


# ------------------------------------------------------------------ EITI


def _amount(value: Any, default_currency: str | None) -> dict[str, Any] | None:
    """An amount as reported: value text, exact decimal and currency; ``None`` when nothing is reported."""
    if value is None:
        return None
    if isinstance(value, Mapping):
        raw, currency = value.get("value"), value.get("currency") or default_currency
    else:
        raw, currency = value, default_currency
    value_text = text(raw)
    if value_text is None:
        return None
    return {"value_text": value_text, "value": decimal_text(value_text), "currency": text(currency)}


def _identifiers(values: Any) -> list[dict[str, str]]:
    out = []
    for item in values or []:
        if isinstance(item, Mapping) and text(item.get("scheme")) and text(item.get("value")):
            out.append({"scheme": str(item["scheme"]).strip(), "value": str(item["value"]).strip()})
    return sorted({(i["scheme"], i["value"]): i for i in out}.values(), key=lambda i: (i["scheme"], i["value"]))


def _individual(entry: Mapping[str, Any]) -> bool:
    return str(entry.get("type") or entry.get("entity_type") or "").casefold() in INDIVIDUAL_TYPES


def company_key(country: str, period: str, entry: Mapping[str, Any], report_key: str) -> str:
    """A company as reported in one report (country and fiscal period): published identifier first, then the name
    as reported; individuals get a report-local key without their name."""
    if _individual(entry):
        return f"extractives:eiti:payer:{country}:{period}:individual:" + digest([report_key, entry.get("id")])[:16]
    ids = _identifiers(entry.get("identification") or entry.get("identifiers"))
    if ids:
        return f"extractives:eiti:company:{country}:{period}:{ids[0]['scheme']}:{ids[0]['value']}"
    return f"extractives:eiti:company:{country}:{period}:name:{slug(entry.get('name'))}"


def parse_eiti(raw: bytes, *, document: Mapping[str, Any], max_records: int = 5000) -> dict[str, Any]:
    """One report version: report, agencies, streams, companies, projects and licences, payments as reported."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtractivesFormatError("schema_drift", "the EITI answer is not JSON") from exc
    data = payload.get("summary_data") if isinstance(payload, Mapping) else None
    if not isinstance(data, Mapping):
        raise ExtractivesFormatError("schema_drift", "the answer holds no summary_data object")
    country = dict(data.get("country") or {})
    period = dict(data.get("fiscal_year") or {})
    declared_period = dict(document["fiscal_period"])
    if str(country.get("iso2") or "").upper() != document["country"]:
        raise ExtractivesFormatError("schema_drift", "the summary data is for another country than declared")
    if iso_day(period.get("start")) != iso_day(declared_period["start"]) or \
            iso_day(period.get("end")) != iso_day(declared_period["end"]):
        raise ExtractivesFormatError("schema_drift", "the summary data covers another fiscal period than declared")
    iso2 = document["country"]
    start, end = iso_day(period["start"]), iso_day(period["end"])
    period_key = f"{start}_{end}"
    report_key = f"extractives:eiti:report:{iso2}:{period_key}"
    report = dict(data.get("report") or {})
    currency = text(report.get("currency"))
    release = _release(document)
    dropped = sorted({p.rsplit(".", 1)[1] for p in personal_keys(dict(data))})
    records: list[dict[str, Any]] = [{
        "record_type": "eiti_report", "record_key": report_key, "country": iso2,
        "country_name": text(country.get("name")), "fiscal_period": {"start": start, "end": end},
        "title": text(report.get("title")), "report_url": text(report.get("url")), "currency": currency,
        "unit": text(report.get("unit")), "version_as_published": text(report.get("version")),
        "published_as_stated": iso_day(report.get("published")),
        "reconciliation_note": text(report.get("reconciliation_note")),
    }]
    agencies = {}
    for entry in data.get("government_agencies") or []:
        entry = dict(entry)
        key = f"{report_key}:agency:{slug(entry.get('id') or entry.get('name'))}"
        agencies[str(entry.get("id"))] = key
        records.append({"record_type": "government_agency", "record_key": key, "report_key": report_key,
                        "agency_id_as_published": text(entry.get("id")), "name_as_published": text(entry.get("name")),
                        "identifiers": _identifiers(entry.get("identification"))})
    streams = {}
    for entry in data.get("revenue_streams") or []:
        entry = dict(entry)
        key = f"{report_key}:stream:{slug(entry.get('gfs_code') or '')}:{slug(entry.get('name'))}"
        streams[str(entry.get("id"))] = key
        records.append({"record_type": "revenue_stream", "record_key": key, "report_key": report_key,
                        "stream_id_as_published": text(entry.get("id")), "gfs_code": text(entry.get("gfs_code")),
                        "name_as_published": text(entry.get("name")),
                        "agency_key": agencies.get(str(entry.get("agency"))),
                        "government_reported": _amount(entry.get("government_reported"), currency),
                        "budget_reference": text(entry.get("budget_reference"))})
    companies = {}
    for entry in data.get("companies") or []:
        entry = dict(entry)
        key = company_key(iso2, period_key, entry, report_key)
        companies[str(entry.get("id"))] = key
        individual = _individual(entry)
        records.append({"record_type": "company", "record_key": key, "report_key": report_key,
                        "company_id_as_published": text(entry.get("id")),
                        "name_as_published": WITHHELD_NAME if individual else text(entry.get("name")),
                        "entity_type_as_published": text(entry.get("type")) or "company",
                        "natural_person": individual,
                        "identifiers": [] if individual else _identifiers(entry.get("identification")),
                        "sector": text(entry.get("sector")),
                        "commodities": [str(c) for c in entry.get("commodities") or []],
                        "country": iso2})
    projects = {}
    for entry in data.get("projects") or []:
        entry = dict(entry)
        key = f"{report_key}:project:{slug(entry.get('id') or entry.get('name'))}"
        projects[str(entry.get("id"))] = key
        licences = []
        for licence in entry.get("licences") or []:
            licence = dict(licence)
            holder = companies.get(str(licence.get("holder")))
            licences.append({"number": text(licence.get("number")), "type": text(licence.get("type")),
                             "holder_key": holder, "commodity": text(licence.get("commodity"))})
        records.append({"record_type": "project", "record_key": key, "report_key": report_key,
                        "project_id_as_published": text(entry.get("id")), "name_as_published": text(entry.get("name")),
                        "company_key": companies.get(str(entry.get("company"))),
                        "commodities": [str(c) for c in entry.get("commodities") or []],
                        "identifiers": _identifiers(entry.get("identifiers")), "licences": licences,
                        "coordinates": dict(entry["coordinates"]) if isinstance(entry.get("coordinates"), Mapping)
                        else None})
    for index, entry in enumerate(data.get("payments") or []):
        entry = dict(entry)
        company = companies.get(str(entry.get("company")))
        stream = streams.get(str(entry.get("revenue_stream")))
        if company is None or stream is None:
            raise ExtractivesFormatError("schema_drift", f"payment {index} names an undeclared company or stream")
        project = projects.get(str(entry.get("project"))) if entry.get("project") is not None else None
        key = ":".join([report_key.replace(":report:", ":payment:"), company.split(f":{period_key}:", 1)[1],
                        stream.rsplit(":stream:", 1)[1]] + ([project.rsplit(":project:", 1)[1]] if project else []))
        discrepancy = entry.get("discrepancy")
        records.append({
            "record_type": "company_payment", "record_key": key, "report_key": report_key,
            "company_key": company, "stream_key": stream, "project_key": project,
            "project_as_reported": text(entry.get("project_name")) or (text(entry.get("project")) if project else None),
            "company_reported": _amount(entry.get("company_reported"), currency),
            "government_reported": _amount(entry.get("government_reported"), currency),
            "discrepancy_as_published": None if discrepancy is None else {
                **(_amount(discrepancy, currency) or {}),
                "explanation": text(dict(discrepancy).get("explanation")) if isinstance(discrepancy, Mapping)
                else None},
            "payment_row": text(entry.get("id")) or str(index),
        })
    if len(records) > max_records:
        raise ExtractivesFormatError("input_limit", "the report version has more records than allowed")
    keys = [r["record_key"] for r in records]
    if len(set(keys)) != len(keys):
        raise ExtractivesFormatError("schema_drift", "a record appears twice in one report version")
    for record in records:
        record["kind"] = "eiti"
        record["provider"] = "eiti"
    return {
        "kind": "eiti",
        "provider": "eiti",
        "format": "eiti-summary-json",
        "items": records,
        "item_count": len(records),
        "published_on": release["published_on"],
        "published_at": None,
        "release_basis": "declared_release",
        "release_label": release["label"],
        "release_version": release["version"],
        "report_key": report_key,
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "content_sha256": digest(records),
        "structure": {"country": iso2, "fiscal_period": {"start": start, "end": end},
                      "personal_fields_dropped": dropped,
                      "minimisation": "contact persons and other personal fields are dropped; individuals' names "
                                      "and identifiers are withheld"},
    }


# ------------------------------------------------------------------ USGS and BGS


def _value(raw: Any) -> dict[str, Any]:
    """A published cell: withheld (W), not available (NA, --), qualitative text, or a number with e/r markers."""
    value_text = text(raw)
    if value_text is None or value_text in {"--", "—", "-", "..", "NA", "na", "n.a."}:
        return {"value_text": value_text, "value": None, "status": "not_available", "estimated": False,
                "revised": False}
    if value_text.upper() == "W":
        return {"value_text": value_text, "value": None, "status": "withheld", "estimated": False, "revised": False}
    match = re.fullmatch(r"([-+]?[\d,. ]+?)\s*([er]{1,2})?", value_text)
    if match and decimal_text(match.group(1)) is not None:
        markers = match.group(2) or ""
        return {"value_text": value_text, "value": decimal_text(match.group(1)), "status": "reported",
                "estimated": "e" in markers, "revised": "r" in markers}
    return {"value_text": value_text, "value": None, "status": "qualitative", "estimated": False, "revised": False}


def _country(document: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    declared = dict(document["countries"]).get(name)
    if declared is None:
        return None
    declared = dict(declared)
    if declared.get("aggregate"):
        return {"name": name, "aggregate": True, "iso2": None, "m49": None, "code_basis": "published aggregate row"}
    return {"name": name, "aggregate": False, "iso2": declared["iso2"], "m49": text(declared.get("m49")),
            "code_basis": "operator-declared"}


def _series_head(provider: str, document: Mapping[str, Any], statistic: str, unit: str,
                 country: Mapping[str, Any]) -> dict[str, Any]:
    commodity = dict(document["commodity"])
    code = country["iso2"] or f"agg:{slug(country['name'])}"
    return {
        "kind": "series", "provider": provider,
        "source_series_id": ":".join([provider, str(commodity["code"]), statistic, slug(unit), code]),
        "commodity": {"code": str(commodity["code"]), "label": str(commodity["label"]),
                      "sub_commodity": text(commodity.get("sub_commodity")),
                      "definition": dict(commodity["definition"]),
                      "hydrocarbon": bool(commodity.get("hydrocarbon")), "siec": text(commodity.get("siec"))},
        "statistic": statistic, "unit": unit, "frequency": "annual", "country": dict(country),
        "licence": _licence(provider), "table": text(document.get("table")),
    }


def _read_csv(raw: bytes, document: Mapping[str, Any]) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    try:
        body = raw.decode(str(document.get("encoding") or "utf-8-sig"))
    except (UnicodeDecodeError, LookupError) as exc:
        raise ExtractivesFormatError("schema_drift", "the CSV is not in the declared encoding") from exc
    reader = csv.DictReader(io.StringIO(body))
    header = [h.strip() for h in reader.fieldnames or []]
    rows = []
    for number, row in enumerate(reader, start=2):
        if number > 50_000:
            raise ExtractivesFormatError("input_limit", "the table has more rows than allowed")
        rows.append((number, {str(k).strip(): v for k, v in row.items() if k is not None}))
    return header, rows


def _finish(release: Mapping[str, Any], provider: str, fmt: str, raw: bytes, items: list[dict[str, Any]],
            structure: Mapping[str, Any]) -> dict[str, Any]:
    if not items:
        raise ExtractivesFormatError("schema_drift", "the table states no row of the declared selection")
    return {
        "kind": "series", "provider": provider, "format": fmt, "items": items, "item_count": len(items),
        "published_on": release["published_on"], "published_at": None, "release_basis": "declared_release",
        "release_label": release["label"], "release_version": release["version"], "report_key": None,
        "file_sha256": hashlib.sha256(raw).hexdigest(), "content_sha256": digest(items), "structure": dict(structure),
    }


def parse_usgs(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    """An MCS world table: one production series and one reserves series per country row, markers as published."""
    header, rows = _read_csv(raw, document)
    columns = dict(document.get("columns") or {})
    values = [dict(c) for c in document["value_columns"]]
    missing = [c for c in [columns["country"], *(v["column"] for v in values),
                           *(columns[k] for k in ("notes", "commodity") if columns.get(k))] if c not in header]
    if missing:
        raise ExtractivesFormatError("schema_drift", f"the table lacks declared columns {missing}")
    wanted = text(document.get("row_filter", {}).get("value")) if document.get("row_filter") else None
    series: dict[str, dict[str, Any]] = {}
    unknown = []
    for number, row in rows:
        if wanted is not None and text(row.get(document["row_filter"]["column"])) != wanted:
            continue
        name = text(row.get(columns["country"]))
        if name is None:
            continue
        country = _country(document, name)
        if country is None:
            unknown.append(name)
            continue
        notes = [n for n in [text(row.get(columns.get("notes") or ""))] if n]
        for column in values:
            head = _series_head("usgs-mcs", document, column["statistic"], column["unit"], country)
            entry = series.setdefault(head["source_series_id"], {**head, "observations": {}})
            cell = _value(row.get(column["column"]))
            if column.get("estimated"):
                cell["estimated"] = True
            period = str(column["year"])
            if period in entry["observations"]:
                raise ExtractivesFormatError("schema_drift", f"{name} {column['statistic']} {period} appears twice")
            entry["observations"][period] = {"period": period, "column": column["column"], **cell,
                                             "reference": text(column.get("reference")),
                                             "notes": notes, "row": number}
    if unknown:
        raise ExtractivesFormatError("schema_drift", f"rows without a declared country code: {sorted(unknown)}")
    items = []
    for key in sorted(series):
        entry = series[key]
        entry["observations"] = [entry["observations"][p] for p in sorted(entry["observations"])]
        items.append(entry)
    return _finish(_release(document), "usgs-mcs", "usgs-mcs-csv", raw, items,
                   {"header": header, "table": document.get("table"),
                    "withheld_note": "W = withheld to avoid disclosing company proprietary data; never filled"})


def parse_bgs(raw: bytes, *, document: Mapping[str, Any]) -> dict[str, Any]:
    """A World Mineral Statistics export: one series per country, statistic and unit, notes as published."""
    header, rows = _read_csv(raw, document)
    columns = dict(document["columns"])
    missing = [c for c in columns.values() if c not in header]
    if missing:
        raise ExtractivesFormatError("schema_drift", f"the export lacks declared columns {missing}")
    statistics = {str(k).casefold(): v for k, v in dict(document["statistics"]).items()}
    commodity_filter = text(document.get("commodity_as_published"))
    series: dict[str, dict[str, Any]] = {}
    unknown = []
    for number, row in rows:
        if commodity_filter and columns.get("commodity") and text(row.get(columns["commodity"])) != commodity_filter:
            continue
        name = text(row.get(columns["country"]))
        statistic = statistics.get(str(row.get(columns["statistic"]) or "").strip().casefold())
        year = text(row.get(columns["year"]))
        if name is None or statistic is None or year is None:
            continue
        country = _country(document, name)
        if country is None:
            unknown.append(name)
            continue
        unit = text(row.get(columns["unit"])) or "unstated"
        head = _series_head("bgs-wms", document, statistic, unit, country)
        entry = series.setdefault(head["source_series_id"], {**head, "observations": {}})
        if year in entry["observations"]:
            raise ExtractivesFormatError("schema_drift", f"{name} {statistic} {year} appears twice")
        cell = _value(row.get(columns["quantity"]))
        entry["observations"][year] = {"period": year, "column": columns["quantity"], **cell, "reference": None,
                                       "notes": [n for n in [text(row.get(columns.get("note") or ""))] if n],
                                       "row": number}
    if unknown:
        raise ExtractivesFormatError("schema_drift", f"rows without a declared country code: {sorted(unknown)}")
    items = []
    for key in sorted(series):
        entry = series[key]
        entry["observations"] = [entry["observations"][p] for p in sorted(entry["observations"])]
        items.append(entry)
    return _finish(_release(document), "bgs-wms", "bgs-wms-csv", raw, items,
                   {"header": header, "commodity_as_published": commodity_filter,
                    "attribution": PROVIDER_CONTRACTS["bgs-wms"]["licence"]["attribution"]})


def parse_document(fmt: str, raw: bytes, document: Mapping[str, Any], *, limit: int) -> dict[str, Any]:
    if fmt == "eiti-summary-json":
        return parse_eiti(raw, document=document, max_records=max(limit, 1))
    if fmt == "usgs-mcs-csv":
        return parse_usgs(raw, document=document)
    if fmt == "bgs-wms-csv":
        return parse_bgs(raw, document=document)
    raise ExtractivesFormatError("invalid_document", f"unknown format {fmt}")


def release_records(release: Mapping[str, Any], document: Mapping[str, Any], url: str,
                    evidence_origin: str) -> list[dict[str, Any]]:
    """The page records of one parsed release: the release header and one item each."""
    header = {
        "contract": RELEASE_CONTRACT,
        "provider": release["provider"],
        "format": release["format"],
        "kind": release["kind"],
        "document": dict(document),
        "published_on": release["published_on"],
        "published_at": release["published_at"],
        "release_basis": release["release_basis"],
        "release_label": release["release_label"],
        "release_version": release["release_version"],
        "report_key": release.get("report_key"),
        "file_sha256": release["file_sha256"],
        "content_sha256": release["content_sha256"],
        "item_count": release["item_count"],
        "structure": release["structure"],
        "evidence_origin": evidence_origin,
        "live_verification": LIVE_VERIFICATION[release["provider"]]["status"],
        "url": url,
    }
    return [
        {
            "id": f"{release['file_sha256'][:16]}:{number}",
            "title": f"{document.get('label') or release['provider']} ({release['release_version']})",
            "url": url,
            "language": "en",
            "published_at": release["published_on"],
            "content": json.dumps(item, sort_keys=True, ensure_ascii=False),
            "extractives_release": header,
            "extractives_item": item,
        }
        for number, item in enumerate(release["items"])
    ]


# ------------------------------------------------------------------ runtime adapter


class ExtractivesAdapter:
    """Fetch the declared extractives documents on the runtime's default transport; one page (one release) each."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        del secret  # every extractives provider is read without a credential
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
            "extractives": {"provider": self.declared["provider"], "format": self.declared["format"],
                            "feature": FEATURES[self.declared["provider"]],
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
            raise SourcePackError("parameter_forbidden", "extractives runs fetch the declared documents only")

    def _get(self, url: str) -> tuple[bytes, str]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        parts = urlsplit(url)
        if (parts.hostname or "").casefold() != host or parts.scheme != "https":
            raise SourcePackError("network_policy", "declared documents are fetched from the endpoint's host only")
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
            release = parse_document(fmt, raw, document, limit=limit)
        except ExtractivesFormatError as exc:
            code = "response_too_large" if exc.code == "input_limit" else "schema_drift"
            raise SourcePackError(code, f"{exc.code}: {exc}") from exc
        if release["item_count"] > limit:
            # Never a truncated release: a missing series or payment would read as one that was not published.
            raise SourcePackError("budget_exhausted", "release has more items than the run's result budget")
        records = release_records(release, document, url, origin)
        receipt = {
            "status": 200,
            "provider": release["provider"],
            "document": document.get("label"),
            "release_version": release["release_version"],
            "published_on": release["published_on"],
            "file_sha256": release["file_sha256"],
            "items": len(records),
            "evidence_origin": origin,
            "final_page": index + 1 >= len(documents),
        }
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: ExtractivesAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path (+ sorted query)."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
        query = urlencode(sorted(pairs))
        key = urlsplit(url).path + ("?" + query if query else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture",
                **({"final_url": page["final_url"]} if page.get("final_url") else {})}

    return transport


def fixture_request(document: Mapping[str, Any]) -> str:
    """The key :func:`fixture_transport` files a response under (path and sorted query)."""
    parts = urlsplit(document_url(document))
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return parts.path + ("?" + query if query else "")


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = ExtractivesAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
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
        "providers": {p: {"access_decision": c["access_decision"], "delivers": c["delivers"],
                          "feature": FEATURES[p], "personal_data": c["personal_data"]}
                      for p, c in PROVIDER_CONTRACTS.items()},
        "bounded_coverage": BOUNDED_COVERAGE,
        "minimisation": MINIMISATION,
        "not_implemented": [],
    }


__all__ = [
    "ADAPTERS", "BOUNDED_COVERAGE", "CONNECTOR", "EXCLUSIONS", "FEATURES", "LIVE_VERIFICATION", "MINIMISATION",
    "NEVER_SENTENCE", "PERSONAL_KEYS", "PROVIDER_CONTRACTS", "WITHHELD_NAME", "ExtractivesAdapter",
    "ExtractivesFormatError", "check_document", "coverage_report", "extractives_declaration", "fixture_request",
    "fixture_transport", "parse_bgs", "parse_document", "parse_eiti", "parse_usgs", "personal_keys",
    "release_records", "replay_native_fixture", "unverified",
]
