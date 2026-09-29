"""Development-finance sources for the Funding & Grants ``development-finance`` feature (#1932, D01 and D03-D05).

Three providers are implemented, each under a recorded access contract
(:data:`PROVIDER_CONTRACTS`); two portals are recorded as ``not-implemented``:

* **IATI Datastore** (``iati-datastore``) - publisher-reported activities in
  the IATI activity standard 2.03 (XML). Fetched through
  :class:`~src.ingestion.provider_execution.DurableHTTP` (exact host, explicit
  budget, no redirects, durable replayable captures) for explicitly selected
  publishers, recipient countries or sectors only, with a subscription key.
  Each activity is parsed exactly as reported: the IATI identifier and the
  reporting-org reference are kept verbatim, transactions keep value, currency
  (stated or the activity default, marked which), value date, type and the
  provider and receiver organisations, and anything absent stays unknown.
* **OECD CRS** (``oecd-crs``) - statistical aggregates of the Creditor
  Reporting System through the existing SDMX dataset connector
  (:class:`~src.ingestion.connectors.dataset.sdmx.SDMXConnector`, provider
  ``OECD``, SDMX-CSV) as a source of the ``economic-statistics-and-filings``
  source pack (connector ``development-finance``). Aggregates are never
  decomposed into activities and never summed with IATI transactions.
* **World Bank Projects API** (``world-bank-projects``) - the World Bank's own
  project records (JSON), fetched through ``DurableHTTP`` for explicitly
  selected countries or project IDs. They stay World Bank records; a link to
  an IATI activity needs a stated identifier (:func:`world_bank_link_evidence`).

``transparenzportal-bund`` and ``eu-aid-explorer`` are portal views over data
their publishers also report to IATI; neither documents machine access this
module could rely on, so both are ``not-implemented`` (never scraped) and the
bounded coverage reaches their publishers through the IATI Datastore.

Every provider is ``unverified-live`` until a dated live run; the endpoint,
parameter, header and field names marked *verify* come from the providers'
public documentation and must be checked against the live terms and responses
before a live run. See ``docs/roadmaps/development-finance-source-audit.md``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree

from src.ingestion.provider_execution import (
    DurableHTTP,
    ProviderError,
    canonical,
    digest,
)
from src.ingestion.source_packs import SourcePackError

CONNECTOR = "development-finance"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-development-finance-release-v1"
IATI_VERSION = "2.03"
# The World Bank's IATI publisher (reporting-org) reference, as used in its own IATI activity identifiers
# ("44000-P123456"). Verify against the IATI Registry before a live run.
WORLD_BANK_IATI_REF = "44000"
WORLD_BANK_PROJECT_ID = re.compile(r"^P\d{6}$")
NEVER_SENTENCE = (
    "Published aid activities and statistics as each publisher reported them: no effectiveness or impact "
    "judgement, no total across publishers, no activity inferred from a statistic and no currency conversion "
    "without a cited rate and date."
)
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
MAX_ACTIVITIES_PER_PAGE = 1000

PROVIDER_HOSTS = {
    "iati-datastore": {"api.iatistandard.org"},
    "world-bank-projects": {"search.worldbank.org"},
    "oecd-crs": {"sdmx.oecd.org"},
}

PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "iati-datastore": {
        "delivers": "publisher-reported activities",
        "access_decision": "unverified-live",
        "reason": "documented JSON/XML API with a free subscription key; not yet run live from this runtime",
        "access": "api (IATI Datastore Search API, IATI XML output)",
        "entry_points": [
            "https://api.iatistandard.org/datastore/activity/iati (verify path and output format)"
        ],
        "authentication": "free subscription key sent as the Ocp-Apim-Subscription-Key header (verify); a secret, "
        "never stored in request metadata",
        "rate_limits": "per-key quota published on developer.iatistandard.org (verify the current figure); one "
        "DurableHTTP budget per run bounds requests and bytes",
        "pagination": "Solr-style start/rows, bounded by max_pages; a short page ends the selection (verify)",
        "identifiers": {
            "activity": "iati-identifier as reported (reporting-org ref + publisher's own id, e.g. XM-DAC-..., "
            "GB-COH-...), stored verbatim",
            "publisher": "reporting-org/@ref as reported (the IATI organisation identifier)",
            "organisations": "participating-org/@ref and provider-org/receiver-org/@ref as reported (org-id.guide prefixes)",
        },
        "update_cadence": "publishers refresh on their own schedule; the Datastore re-indexes the IATI Registry "
        "several times a day (verify)",
        "temporal_semantics": "iati-activity/@last-updated-datetime is the publisher's own revision stamp; the "
        "retrieval time is recorded separately",
        "terms": "IATI data is published under the licence each publisher declares in the IATI Registry "
        "(commonly open licences; verify per publisher); the Datastore terms of use apply (verify)",
        "retained_evidence": "raw XML capture per page (DurableHTTP blob digest), one document per activity with "
        "its capture digest and activity index as locator",
        "coverage": "explicitly selected publishers (reporting-org refs), recipient countries (ISO 3166-1 alpha-2) "
        "or DAC sectors only; never all of IATI",
        "unavailable_fallback": "record the failed or partial page as stale publisher coverage; the last "
        "revision stays current and nothing is marked ended",
        "verify": [
            "endpoint path and wt/format parameter",
            "subscription-key header name",
            "query field names "
            "(reporting_org_ref, recipient_country_code, sector_code)",
            "rows ceiling",
            "quota",
        ],
    },
    "oecd-crs": {
        "delivers": "statistical aggregates",
        "access_decision": "unverified-live",
        "reason": "documented SDMX REST API without authentication; dataflow id and CSV shape not yet run live",
        "access": "sdmx (OECD SDMX REST API, SDMX-CSV through the existing SDMX connector)",
        "entry_points": [
            "https://sdmx.oecd.org/public/rest/data/{agency},{dataflow},{version}/{key}?format=csvfile "
            "(verify)"
        ],
        "authentication": "none",
        "rate_limits": "the OECD API states a per-hour request limit for anonymous users (verify the figure)",
        "pagination": "none; a request is bounded by its series key and startPeriod/endPeriod",
        "identifiers": {
            "dataflow": "OECD.DCD.FSD,DSD_CRS@DF_CRS,<version> (verify the current dataflow id and version)",
            "dimensions": "DONOR, RECIPIENT, SECTOR, MEASURE, CHANNEL, FLOW_TYPE, PRICE_BASE, UNIT_MEASURE "
            "(verify the dimension ids in the data structure definition)",
            "donor_agency": "DAC donor and agency codes as the dimension codes state them",
        },
        "update_cadence": "annual CRS release with in-year revisions (verify the release calendar)",
        "temporal_semantics": "a CRS release is a vintage: the provider's LAST UPDATE column when the CSV carries "
        "it, else the release the source declares, else the retrieval time (labelled as such)",
        "terms": "OECD terms and conditions for data reuse with attribution (verify)",
        "retained_evidence": "raw SDMX-CSV per declared series key; row numbers per observation",
        "coverage": "declared donor/recipient/sector series keys and period windows only",
        "unavailable_fallback": "a failed run leaves earlier vintages current and records stale coverage",
        "verify": [
            "dataflow id and version",
            "format=csvfile column order",
            "PRICE_BASE codes (V current, Q constant)",
            "base-year attribute",
            "recipient aggregate codes",
        ],
    },
    "world-bank-projects": {
        "delivers": "publisher-reported project records (World Bank as publisher)",
        "access_decision": "unverified-live",
        "reason": "documented public JSON API without authentication; not yet run live from this runtime",
        "access": "api (World Bank Projects API v2, JSON)",
        "entry_points": [
            "https://search.worldbank.org/api/v2/projects?format=json (verify)"
        ],
        "authentication": "none",
        "rate_limits": "not documented (verify); bounded by the run's DurableHTTP budget",
        "pagination": "rows/os offset, bounded by max_pages (verify parameter names)",
        "identifiers": {
            "project": "World Bank project ID (P + six digits)",
            "country": "countrycode as reported",
        },
        "update_cadence": "continuous; board approvals and restructurings update records (verify)",
        "temporal_semantics": "no per-record update stamp is relied on; a changed record is a new revision by "
        "retrieval time",
        "terms": "World Bank Terms of Use for Datasets (CC BY 4.0 for most datasets; verify for the Projects API)",
        "retained_evidence": "raw JSON capture per page, one document per project",
        "coverage": "explicitly selected countries or project IDs",
        "unavailable_fallback": "record stale coverage; earlier project revisions stay current",
        "verify": [
            "parameter names (countrycode_exact, rows, os)",
            "field names (boardapprovaldate, closingdate, "
            "totalcommamt, idacommamt, ibrdcommamt, sector, theme, projectfinancialtype)",
            "commitment amount currency (reported as US dollars)",
        ],
    },
    "transparenzportal-bund": {
        "delivers": "portal view",
        "access_decision": "not-implemented",
        "reason": "a portal view over German federal development cooperation data that the ministries and "
        "implementing agencies also publish to IATI; no documented machine access is relied on, pages are never "
        "scraped, and the publishers are reached through the IATI Datastore (verify whether a documented export "
        "exists)",
        "access": "none (portal)",
        "coverage": "reach the German federal publishers through iati-datastore selections",
    },
    "eu-aid-explorer": {
        "delivers": "portal view",
        "access_decision": "not-implemented",
        "reason": "a portal combining EU institutions' IATI data and OECD statistics; no documented machine access "
        "is relied on and nothing is scraped; EU publishers are reached through the IATI Datastore and CRS through "
        "the OECD SDMX API (verify whether a documented export exists)",
        "access": "none (portal)",
        "coverage": "reach EU institutional publishers through iati-datastore selections",
    },
}
LIVE_VERIFICATION = {
    provider: {
        "status": "unverified-live"
        if contract["access_decision"] == "unverified-live"
        else "not-implemented",
        "note": "no dated live run from this runtime; offline fixtures only"
        if contract["access_decision"] == "unverified-live"
        else contract["reason"],
    }
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# Bounded coverage: no record set implies complete coverage of any provider.
BOUNDED_COVERAGE = {
    "iati-datastore": "the publishers, recipient countries and sectors named in each selection; at most "
    "max_pages x rows activities per selection",
    "oecd-crs": "the series keys (donor, recipient, sector) and period windows declared in the source pack",
    "world-bank-projects": "the countries or project IDs named in each selection",
}


class DevelopmentFinanceFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def unverified(provider: str) -> bool:
    return (
        PROVIDER_CONTRACTS.get(provider, {}).get("access_decision") != "verified-live"
    )


# ------------------------------------------------------------------- parsing helpers


def text(value: Any) -> str | None:
    """Stripped text, or ``None`` for a missing or blank value (never the string 'None')."""
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None


def iso_day(value: Any) -> str | None:
    """An ISO date (``YYYY-MM-DD``) from an xs:date, an xs:dateTime or ``M/D/YYYY``; else ``None``."""
    raw = text(value)
    if raw is None:
        return None
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if match:
        try:
            return date(*(int(p) for p in match.groups())).isoformat()
        except ValueError:
            return None
    match = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if match:
        month, day, year = (int(p) for p in match.groups())
        try:
            return date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


def iso_instant(value: Any) -> str | None:
    """An xs:dateTime as a UTC ISO timestamp (``YYYY-MM-DDTHH:MM:SS+00:00``); a date alone is midnight UTC."""
    raw = text(value)
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        day = iso_day(raw)
        if day is None:
            return None
        parsed = datetime.fromisoformat(day)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def decimal_text(value: Any) -> str | None:
    """A reported number as a canonical decimal string; ``None`` if absent or not a finite number."""
    raw = text(value)
    if raw is None:
        return None
    try:
        number = (
            Decimal(raw.replace(",", ""))
            if re.fullmatch(r"-?[\d,]+(\.\d+)?", raw)
            else Decimal(raw)
        )
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    normalized = number.normalize()
    return (
        format(normalized, "f")
        if normalized != normalized.to_integral_value()
        else str(number.to_integral_value())
    )


def identifier_key(value: Any) -> str:
    """One comparison key for IATI and project identifiers on both sides of every match.

    Case and surrounding or inner whitespace are ignored; separators (``-``, ``/``, ``.``, ``_``, ``:``) are kept,
    so ``XM-DAC-1-2`` and ``XM-DAC-12`` stay different identifiers.
    """
    return re.sub(r"\s+", "", str(value or "")).upper()


def _safe_xml(raw: bytes, *, max_bytes: int) -> ElementTree.Element:
    if len(raw) > max_bytes:
        raise DevelopmentFinanceFormatError(
            "input_limit", "IATI document exceeds its byte limit"
        )
    head = raw[:4096].upper()
    if b"<!DOCTYPE" in head or b"<!ENTITY" in raw.upper():
        raise DevelopmentFinanceFormatError(
            "invalid_xml", "XML document type and entity declarations are refused"
        )
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise DevelopmentFinanceFormatError(
            "schema_drift", f"not well-formed XML: {exc}"
        ) from exc


def _narratives(element: ElementTree.Element | None) -> list[dict[str, Any]]:
    if element is None:
        return []
    out = []
    for narrative in element.findall("narrative"):
        value = text("".join(narrative.itertext()))
        if value is not None:
            out.append({"lang": narrative.get(_XML_LANG), "text": value})
    return out


def _first(narratives: Sequence[Mapping[str, Any]]) -> str | None:
    return narratives[0]["text"] if narratives else None


def _org(
    element: ElementTree.Element | None, activity_attr: str | None = None
) -> dict[str, Any] | None:
    if element is None:
        return None
    narratives = _narratives(element)
    org = {
        "ref": text(element.get("ref")),
        "type": text(element.get("type")),
        "name": _first(narratives),
        "narratives": narratives,
    }
    if activity_attr:
        org["activity_id"] = text(element.get(activity_attr))
    return org


def _percentage(element: ElementTree.Element) -> tuple[str | None, str | None]:
    raw = text(element.get("percentage"))
    return raw, decimal_text(raw)


def _allocation(element: ElementTree.Element, kind: str) -> dict[str, Any]:
    raw, value = _percentage(element)
    return {
        "kind": kind,
        "code": text(element.get("code")),
        "vocabulary": text(element.get("vocabulary")),
        "percentage_text": raw,
        "percentage": value,
        "narrative": _first(_narratives(element)),
    }


def _code(
    element: ElementTree.Element | None, *, vocabulary: bool = False
) -> dict[str, Any] | None:
    if element is None:
        return None
    code = text(element.get("code"))
    if code is None:
        return None
    return {
        "code": code,
        **({"vocabulary": text(element.get("vocabulary"))} if vocabulary else {}),
    }


def _child_attr(element: ElementTree.Element, path: str, attr: str) -> Any:
    # An Element without children is falsy, so a found child is tested against None, never by truth value.
    child = element.find(path)
    return None if child is None else child.get(attr)


def _child_text(element: ElementTree.Element, path: str) -> str | None:
    child = element.find(path)
    return None if child is None else text("".join(child.itertext()))


def _value(element: ElementTree.Element) -> dict[str, Any]:
    raw = text(element.get("value"))
    return {
        "value_text": raw,
        "value": decimal_text(raw),
        "iso_date": iso_day(element.get("iso-date")),
        "year": text(element.get("year")),
        "comment": _first(_narratives(element.find("comment"))),
    }


def _transaction(
    element: ElementTree.Element, ordinal: int, default_currency: str | None
) -> dict[str, Any]:
    value = element.find("value")
    value_text = text("".join(value.itertext())) if value is not None else None
    stated_currency = text(value.get("currency")) if value is not None else None
    currency = stated_currency or default_currency
    return {
        "ordinal": ordinal,
        "ref": text(element.get("ref")),
        "humanitarian": text(element.get("humanitarian")),
        "type": (_code(element.find("transaction-type")) or {}).get("code"),
        "date": iso_day(_child_attr(element, "transaction-date", "iso-date")),
        "value_text": value_text,
        "value": decimal_text(value_text),
        "currency": currency,
        "currency_source": "stated"
        if stated_currency
        else "activity-default"
        if currency
        else None,
        "value_date": iso_day(value.get("value-date")) if value is not None else None,
        "description": _first(_narratives(element.find("description"))),
        "provider_org": _org(element.find("provider-org"), "provider-activity-id"),
        "receiver_org": _org(element.find("receiver-org"), "receiver-activity-id"),
        "flow_type": (_code(element.find("flow-type")) or {}).get("code"),
        "aid_type": _code(element.find("aid-type"), vocabulary=True),
        "finance_type": (_code(element.find("finance-type")) or {}).get("code"),
        "sectors": [_allocation(s, "sector") for s in element.findall("sector")],
        "recipient_countries": [
            _allocation(c, "recipient-country")
            for c in element.findall("recipient-country")
        ],
        "recipient_regions": [
            _allocation(r, "recipient-region")
            for r in element.findall("recipient-region")
        ],
    }


def _result(element: ElementTree.Element, ordinal: int) -> dict[str, Any]:
    indicators = []
    for number, indicator in enumerate(element.findall("indicator")):
        periods = []
        for period in indicator.findall("period"):
            periods.append(
                {
                    "start": iso_day(_child_attr(period, "period-start", "iso-date")),
                    "end": iso_day(_child_attr(period, "period-end", "iso-date")),
                    "targets": [_value(t) for t in period.findall("target")],
                    "actuals": [_value(a) for a in period.findall("actual")],
                }
            )
        indicators.append(
            {
                "ordinal": number,
                "ref": text(indicator.get("ref")),
                "measure": text(indicator.get("measure")),
                "ascending": text(indicator.get("ascending")),
                "title": _first(_narratives(indicator.find("title"))),
                "baselines": [_value(b) for b in indicator.findall("baseline")],
                "periods": periods,
            }
        )
    return {
        "ordinal": ordinal,
        "type": text(element.get("type")),
        "aggregation_status": text(element.get("aggregation-status")),
        "title": _first(_narratives(element.find("title"))),
        "indicators": indicators,
    }


def parse_activity(element: ElementTree.Element, index: int) -> dict[str, Any]:
    identifier = _child_text(element, "iati-identifier")
    if identifier is None:
        raise DevelopmentFinanceFormatError(
            "schema_drift", f"activity {index} has no iati-identifier"
        )
    default_currency = text(element.get("default-currency"))
    stamp_text = text(element.get("last-updated-datetime"))
    reporting = _org(element.find("reporting-org")) or {
        "ref": None,
        "type": None,
        "name": None,
        "narratives": [],
    }
    other_identifiers = []
    for other in element.findall("other-identifier"):
        owner = other.find("owner-org")
        other_identifiers.append(
            {
                "ref": text(other.get("ref")),
                "type": text(other.get("type")),
                "owner_ref": text(owner.get("ref")) if owner is not None else None,
                "owner_name": _first(_narratives(owner)),
            }
        )
    locations = []
    for location in element.findall("location"):
        pos = location.find("point/pos")
        locations.append(
            {
                "ref": text(location.get("ref")),
                "name": _first(_narratives(location.find("name"))),
                "description": _first(_narratives(location.find("description"))),
                "point": text(pos.text) if pos is not None else None,
                "administrative": [
                    {
                        "vocabulary": text(a.get("vocabulary")),
                        "level": text(a.get("level")),
                        "code": text(a.get("code")),
                    }
                    for a in location.findall("administrative")
                ],
            }
        )
    return {
        "iati_identifier": identifier,
        "last_updated_text": stamp_text,
        "last_updated_at": iso_instant(stamp_text),
        "default_currency": default_currency,
        "language": text(element.get(_XML_LANG)),
        "humanitarian": text(element.get("humanitarian")),
        "reporting_org": reporting,
        "title": _first(_narratives(element.find("title"))),
        "description": _first(_narratives(element.find("description"))),
        "activity_status": (_code(element.find("activity-status")) or {}).get("code"),
        "dates": [
            {"type": text(d.get("type")), "iso_date": iso_day(d.get("iso-date"))}
            for d in element.findall("activity-date")
        ],
        "participating_orgs": [
            {
                **(_org(org, "activity-id") or {}),
                "ordinal": n,
                "role": text(org.get("role")),
                "crs_channel_code": text(org.get("crs-channel-code")),
            }
            for n, org in enumerate(element.findall("participating-org"))
        ],
        "other_identifiers": other_identifiers,
        "related_activities": [
            {"ref": text(r.get("ref")), "type": text(r.get("type"))}
            for r in element.findall("related-activity")
        ],
        "recipient_countries": [
            _allocation(c, "recipient-country")
            for c in element.findall("recipient-country")
        ],
        "recipient_regions": [
            _allocation(r, "recipient-region")
            for r in element.findall("recipient-region")
        ],
        "sectors": [_allocation(s, "sector") for s in element.findall("sector")],
        "locations": locations,
        "default_flow_type": (_code(element.find("default-flow-type")) or {}).get(
            "code"
        ),
        "default_aid_type": _code(element.find("default-aid-type"), vocabulary=True),
        "default_finance_type": (_code(element.find("default-finance-type")) or {}).get(
            "code"
        ),
        "transactions": [
            _transaction(t, n, default_currency)
            for n, t in enumerate(element.findall("transaction"))
        ],
        "results": [_result(r, n) for n, r in enumerate(element.findall("result"))],
        "locator": {"activity_index": index},
    }


def parse_iati_activities(
    raw: bytes,
    *,
    max_bytes: int = 20_000_000,
    max_activities: int = MAX_ACTIVITIES_PER_PAGE,
) -> dict[str, Any]:
    """An ``iati-activities`` document (standard 2.03) into activities exactly as reported."""
    root = _safe_xml(raw, max_bytes=max_bytes)
    if root.tag != "iati-activities":
        raise DevelopmentFinanceFormatError(
            "schema_drift", "the document root is not iati-activities"
        )
    version = text(root.get("version"))
    if version is not None and not version.startswith("2."):
        raise DevelopmentFinanceFormatError(
            "schema_drift", f"IATI version {version} is not a 2.0x document"
        )
    elements = root.findall("iati-activity")
    if len(elements) > max_activities:
        raise DevelopmentFinanceFormatError(
            "input_limit", "more activities than the page limit"
        )
    activities, rejected = [], []
    for index, element in enumerate(elements):
        try:
            activities.append(parse_activity(element, index))
        except DevelopmentFinanceFormatError as exc:
            # One unusable activity never drops the rest of the page; it is recorded in the coverage instead.
            rejected.append({"activity_index": index, "reason": str(exc)})
    return {
        "version": version,
        "generated_datetime": iso_instant(root.get("generated-datetime")),
        "activities": activities,
        "rejected": rejected,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


# ------------------------------------------------------------------ World Bank


def _wb_amount(value: Any) -> dict[str, Any] | None:
    raw = text(value)
    if raw is None:
        return None
    return {"text": raw, "value": decimal_text(raw)}


def _wb_names(
    value: Any, *, keys: Sequence[str] = ("Name", "name")
) -> list[dict[str, Any]]:
    items = (
        value
        if isinstance(value, list)
        else [value]
        if isinstance(value, Mapping)
        else []
    )
    out = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = next((text(item.get(k)) for k in keys if text(item.get(k))), None)
        entry = {
            "name": name,
            "code": text(item.get("code")),
            "percent": decimal_text(item.get("Percent")),
        }
        if name or entry["code"]:
            out.append(entry)
    return out


def parse_world_bank_project(
    project_id: str, body: Mapping[str, Any]
) -> dict[str, Any]:
    pid = text(body.get("id")) or text(project_id)
    if pid is None or not WORLD_BANK_PROJECT_ID.fullmatch(pid):
        raise DevelopmentFinanceFormatError(
            "schema_drift", f"not a World Bank project id: {pid!r}"
        )
    countries = body.get("countrycode")
    countries = (
        countries if isinstance(countries, list) else [countries] if countries else []
    )
    sectors = _wb_names(body.get("sector"))
    for number in range(1, 6):
        sectors += _wb_names(body.get(f"sector{number}"))
    themes = _wb_names(body.get("theme_list") or body.get("theme"))
    for number in range(1, 6):
        themes += _wb_names(body.get(f"theme{number}"))
    return {
        "project_id": pid,
        "name": text(body.get("project_name")),
        "status": text(body.get("status")),
        "approval_date": iso_day(body.get("boardapprovaldate")),
        "closing_date": iso_day(body.get("closingdate")),
        "countries": sorted({str(c).strip().upper() for c in countries if text(c)}),
        "country_names": text(body.get("countryshortname"))
        or text(body.get("countryname")),
        "region": text(body.get("regionname")),
        "lending_instrument": text(body.get("lendinginstr")),
        "financier": text(body.get("projectfinancialtype")),
        "commitments": {
            key: amount
            for key, amount in (
                ("total", _wb_amount(body.get("totalcommamt"))),
                ("ibrd", _wb_amount(body.get("ibrdcommamt"))),
                ("ida", _wb_amount(body.get("idacommamt"))),
                ("grant", _wb_amount(body.get("grantamt"))),
            )
            if amount is not None
        },
        "currency": "USD"
        if any(
            body.get(k) not in (None, "")
            for k in ("totalcommamt", "ibrdcommamt", "idacommamt", "grantamt")
        )
        else None,
        "currency_basis": "the Projects API reports commitment amounts in US dollars (verify)",
        "sectors": sectors,
        "themes": themes,
        "url": text(body.get("url")),
    }


def parse_world_bank_projects(payload: Mapping[str, Any]) -> dict[str, Any]:
    projects = payload.get("projects")
    if not isinstance(projects, Mapping):
        raise DevelopmentFinanceFormatError(
            "schema_drift", "the Projects API response has no projects object"
        )
    parsed, rejected = [], []
    for key, body in sorted(projects.items()):
        try:
            if not isinstance(body, Mapping):
                raise DevelopmentFinanceFormatError(
                    "schema_drift", "a project entry is not an object"
                )
            parsed.append(parse_world_bank_project(key, body))
        except DevelopmentFinanceFormatError as exc:
            rejected.append({"key": str(key), "reason": str(exc)})

    def number(value):
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return None

    return {
        "projects": parsed,
        "rejected": rejected,
        "total": number(payload.get("total")),
        "rows": number(payload.get("rows")),
        "offset": number(payload.get("os")),
    }


def world_bank_link_evidence(
    project_id: str, activity: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Stated identifiers that make an explicit link between a World Bank project and an IATI activity.

    Only the World Bank's own IATI publication (identifier ``44000-<project id>`` from reporting-org 44000), a
    ``related-activity`` naming that identifier, or an ``other-identifier`` carrying the project ID count; a shared
    title, country or amount never does.
    """
    pid = identifier_key(project_id)
    own = identifier_key(f"{WORLD_BANK_IATI_REF}-{project_id}")
    evidence = []
    if identifier_key(activity.get("iati_identifier")) == own and identifier_key(
        (activity.get("reporting_org") or {}).get("ref")
    ) == identifier_key(WORLD_BANK_IATI_REF):
        evidence.append(
            {
                "kind": "world-bank-iati-publication",
                "field": "iati-identifier",
                "value": activity["iati_identifier"],
            }
        )
    for related in activity.get("related_activities") or []:
        if identifier_key(related.get("ref")) == own:
            evidence.append(
                {
                    "kind": "related-activity",
                    "field": "related-activity/@ref",
                    "value": related["ref"],
                    "type": related.get("type"),
                }
            )
    for other in activity.get("other_identifiers") or []:
        if identifier_key(other.get("ref")) in {pid, own}:
            evidence.append(
                {
                    "kind": "other-identifier",
                    "field": "other-identifier/@ref",
                    "value": other["ref"],
                    "type": other.get("type"),
                    "owner_ref": other.get("owner_ref"),
                }
            )
    return evidence


# ------------------------------------------------------------------ clients (DurableHTTP)

_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_SECTOR = re.compile(r"^\d{3,5}$")


def iati_selection(selection: Mapping[str, Any]) -> dict[str, list[str]]:
    """A bounded, explicit IATI selection: publishers, recipient countries and/or DAC sectors."""
    allowed = {"publishers", "recipient_countries", "sectors"}
    if not isinstance(selection, Mapping) or set(selection) - allowed:
        raise ValueError(f"an IATI selection names only {sorted(allowed)}")
    clean = {}
    for key, pattern in (
        ("publishers", _REF),
        ("recipient_countries", _COUNTRY),
        ("sectors", _SECTOR),
    ):
        values = [str(v).strip() for v in selection.get(key) or []]
        if len(values) > 20 or any(not pattern.fullmatch(v) for v in values):
            raise ValueError(f"{key} must be at most 20 well-formed codes")
        if values:
            clean[key] = sorted(set(values))
    if not clean:
        raise ValueError(
            "select at least one publisher, recipient country or sector; never all of IATI"
        )
    return clean


def iati_query(selection: Mapping[str, list[str]]) -> str:
    fields = {
        "publishers": "reporting_org_ref",
        "recipient_countries": "recipient_country_code",
        "sectors": "sector_code",
    }
    clauses = []
    for key in ("publishers", "recipient_countries", "sectors"):
        if selection.get(key):
            clauses.append(
                f"{fields[key]}:(" + " OR ".join(f'"{v}"' for v in selection[key]) + ")"
            )
    return " AND ".join(clauses)


class IATIDatastoreClient:
    """Bounded IATI Datastore access through one explicit DurableHTTP budget (verify the endpoint and fields)."""

    ENDPOINT = "https://api.iatistandard.org/datastore/activity/iati"
    KEY_HEADER = "Ocp-Apim-Subscription-Key"

    def __init__(
        self,
        http: DurableHTTP,
        *,
        principal_id: str,
        subscription_key: str | None = None,
    ) -> None:
        if (
            http.provider != "iati-datastore"
            or not http.hosts <= PROVIDER_HOSTS["iati-datastore"]
        ):
            raise ValueError("the IATI client requires the iati-datastore host policy")
        self.http, self.principal_id, self.key = http, principal_id, subscription_key

    def page(
        self,
        selection: Mapping[str, Any],
        observation: str,
        *,
        start: int = 0,
        rows: int = 100,
    ):
        clean = iati_selection(selection)
        if not 0 <= start <= 100_000 or not 1 <= rows <= MAX_ACTIVITIES_PER_PAGE:
            raise ValueError("bounded start/rows required")
        captured = self.http.request(
            f"{observation}:iati:{digest(clean)[:16]}:{start}:{rows}",
            self.ENDPOINT,
            principal_id=self.principal_id,
            params={"q": iati_query(clean), "start": str(start), "rows": str(rows)},
            headers={"Accept": "application/xml"},
            secret_headers={self.KEY_HEADER: self.key} if self.key else None,
            max_bytes=20_000_000,
            timeout_s=30,
        )
        try:
            parsed = parse_iati_activities(captured.content)
        except DevelopmentFinanceFormatError as exc:
            raise ProviderError(exc.code, str(exc)) from exc
        return parsed, captured

    def collect(
        self,
        selection: Mapping[str, Any],
        observation: str,
        *,
        rows: int = 100,
        max_pages: int = 5,
    ):
        """Follow pages within an explicit bound; the selection is complete only if a short page ended it."""
        clean = iati_selection(selection)
        if not 1 <= max_pages <= 50:
            raise ValueError("max_pages must be between 1 and 50")
        pages, captures, start, stop, complete = [], [], 0, None, False
        for number in range(max_pages):
            try:
                parsed, captured = self.page(clean, observation, start=start, rows=rows)
            except ProviderError as exc:
                stop = f"failed on page {number + 1}: {exc.code}"
                break
            pages.append(parsed)
            captures.append(captured)
            if len(parsed["activities"]) + len(parsed["rejected"]) < rows:
                complete, stop = True, "short page (end of results)"
                break
            start += rows
        else:
            stop = "page budget reached"
        return {
            "selection": clean,
            "pages": pages,
            "captures": captures,
            "coverage": {
                "complete": complete,
                "pages_read": len(pages),
                "stop_reason": stop,
                "requested": clean,
                "rows": rows,
                "max_pages": max_pages,
                "rejected": [
                    {**r, "page": n} for n, p in enumerate(pages) for r in p["rejected"]
                ],
            },
        }


def world_bank_selection(selection: Mapping[str, Any]) -> dict[str, list[str]]:
    allowed = {"countries", "project_ids"}
    if not isinstance(selection, Mapping) or set(selection) - allowed:
        raise ValueError(f"a World Bank selection names only {sorted(allowed)}")
    countries = sorted(
        {str(v).strip().upper() for v in selection.get("countries") or []}
    )
    projects = sorted(
        {str(v).strip().upper() for v in selection.get("project_ids") or []}
    )
    if (
        len(countries) > 20
        or len(projects) > 50
        or any(not _COUNTRY.fullmatch(c) for c in countries)
        or any(not WORLD_BANK_PROJECT_ID.fullmatch(p) for p in projects)
    ):
        raise ValueError(
            "countries are ISO 3166-1 alpha-2 codes and project ids are P + six digits"
        )
    if not countries and not projects:
        raise ValueError("select countries or project ids; never all projects")
    return {k: v for k, v in (("countries", countries), ("project_ids", projects)) if v}


class WorldBankProjectsClient:
    ENDPOINT = "https://search.worldbank.org/api/v2/projects"

    def __init__(self, http: DurableHTTP, *, principal_id: str) -> None:
        if (
            http.provider != "world-bank-projects"
            or not http.hosts <= PROVIDER_HOSTS["world-bank-projects"]
        ):
            raise ValueError(
                "the World Bank client requires the world-bank-projects host policy"
            )
        self.http, self.principal_id = http, principal_id

    def page(
        self,
        selection: Mapping[str, Any],
        observation: str,
        *,
        offset: int = 0,
        rows: int = 50,
    ):
        clean = world_bank_selection(selection)
        if not 0 <= offset <= 10_000 or not 1 <= rows <= 500:
            raise ValueError("bounded os/rows required")
        params = {"format": "json", "rows": str(rows), "os": str(offset)}
        if clean.get("countries"):
            params["countrycode_exact"] = "^".join(clean["countries"])
        if clean.get("project_ids"):
            params["id"] = "^".join(clean["project_ids"])
        captured = self.http.request(
            f"{observation}:wb:{digest(clean)[:16]}:{offset}:{rows}",
            self.ENDPOINT,
            principal_id=self.principal_id,
            params=params,
            max_bytes=10_000_000,
            timeout_s=30,
        )
        try:
            parsed = parse_world_bank_projects(captured.json())
        except DevelopmentFinanceFormatError as exc:
            raise ProviderError(exc.code, str(exc)) from exc
        return parsed, captured

    def collect(
        self,
        selection: Mapping[str, Any],
        observation: str,
        *,
        rows: int = 50,
        max_pages: int = 5,
    ):
        clean = world_bank_selection(selection)
        if not 1 <= max_pages <= 50:
            raise ValueError("max_pages must be between 1 and 50")
        pages, captures, offset, stop, complete = [], [], 0, None, False
        for number in range(max_pages):
            try:
                parsed, captured = self.page(
                    clean, observation, offset=offset, rows=rows
                )
            except ProviderError as exc:
                stop = f"failed on page {number + 1}: {exc.code}"
                break
            pages.append(parsed)
            captures.append(captured)
            returned = len(parsed["projects"]) + len(parsed["rejected"])
            offset += returned
            if returned < rows or (
                parsed["total"] is not None and offset >= parsed["total"]
            ):
                complete, stop = True, "all results read"
                break
        else:
            stop = "page budget reached"
        return {
            "selection": clean,
            "pages": pages,
            "captures": captures,
            "coverage": {
                "complete": complete,
                "pages_read": len(pages),
                "stop_reason": stop,
                "requested": clean,
                "rows": rows,
                "max_pages": max_pages,
                "rejected": [
                    {**r, "page": n} for n, p in enumerate(pages) for r in p["rejected"]
                ],
            },
        }


# ------------------------------------------------------------------ OECD CRS (source-pack connector)

# PRICE_BASE codes of the DAC dataflows (verify): V = current prices, Q = constant prices.
PRICE_BASES = {"V": "current", "Q": "constant"}
# Attributes that may state the constant-price base year (verify against the data structure definition).
BASE_YEAR_ATTRIBUTES = ("BASE_PER", "BASE_YEAR", "REF_YEAR_PRICE")
FORMATS = ("oecd-sdmx-csv",)


AGGREGATE_NAME_MARKERS = (
    "regional",
    "unspecified",
    "unallocated",
    "total",
    "multilateral",
)


def split_code_label(value: Any) -> tuple[str | None, str | None]:
    """An SDMX-CSV cell that may carry a label (``KEN: Kenya`` with labels output) as (code, label)."""
    raw = text(value)
    if raw is None:
        return None, None
    code, sep, label = raw.partition(": ")
    return (text(code), text(label)) if sep else (raw, None)


def classify_recipient(code: Any, name: Any = None) -> dict[str, Any]:
    """A CRS recipient code by its documented structure: a regional or unallocated aggregate, a country, or unknown.

    * a label that names a region or an unspecified/unallocated/total recipient marks an aggregate, whatever the
      code;
    * numeric DAC recipient codes: the bundled regional and unspecified codes, every three-digit code ending in
      ``89`` or ``98`` (the DAC "..., regional" pattern) and every code of four or more digits (the newer
      regional codes such as ``1027`` "Eastern Africa, regional") are aggregates. Any other number is ``unknown``:
      without the DAC recipient list it is never assumed to be a single country;
    * alphabetic codes: an underscore (``AFR_X``) or a known total (``DPGC``) is an aggregate, three letters are an
      ISO 3166-1 alpha-3 country;
    * anything else is ``unknown``.

    An aggregate is never resolved to a country or apportioned. (Verify the ranges against the DAC recipient list.)
    """
    from src.ingestion.development_finance_codes import CODELISTS

    raw, label = text(code), text(name)
    if raw is None:
        return {
            "code": None,
            "kind": "unknown",
            "scheme": None,
            "reason": "no recipient code",
        }
    base = {"code": raw, **({"label": label} if label else {})}
    if label and any(marker in label.casefold() for marker in AGGREGATE_NAME_MARKERS):
        return {
            **base,
            "kind": "aggregate",
            "scheme": "named",
            "reason": "the label names an aggregate",
        }
    regions = CODELISTS["dac-recipient-region"]["codes"]
    if raw.isdigit():
        if raw in regions:
            return {
                **base,
                "label": label or regions[raw],
                "kind": "aggregate",
                "scheme": "dac-recipient",
                "reason": "a bundled DAC regional or unspecified code",
            }
        number = raw.lstrip("0") or "0"
        if len(number) >= 4 or (len(number) == 3 and number[-2:] in {"89", "98"}):
            return {
                **base,
                "kind": "aggregate",
                "scheme": "dac-recipient",
                "reason": "the DAC code structure of regional and unspecified recipients",
            }
        return {
            **base,
            "kind": "unknown",
            "scheme": "dac-recipient",
            "reason": "a numeric DAC code outside the aggregate structure; not assumed to be a single country "
            "without the DAC recipient list",
        }
    upper = raw.upper()
    if "_" in upper or upper in {"DPGC", "ALLD", "W", "WXOECD"}:
        return {
            **base,
            "kind": "aggregate",
            "scheme": "oecd-recipient",
            "reason": "an aggregate code shape",
        }
    if re.fullmatch(r"[A-Z]{3}", upper):
        return {**base, "kind": "country", "scheme": "iso3166-1-alpha3"}
    return {
        **base,
        "kind": "unknown",
        "scheme": None,
        "reason": "an unrecognised code shape",
    }


def development_finance_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("development_finance") or {})
    if declared.get("provider") != "oecd-crs" or declared.get("format") not in FORMATS:
        raise SourcePackError(
            "invalid_manifest",
            "development-finance sources declare provider oecd-crs and "
            "format oecd-sdmx-csv",
        )
    documents = list(declared.get("documents") or [])
    if not documents:
        raise SourcePackError(
            "invalid_manifest",
            "a development-finance source declares its series documents",
        )
    if len(documents) > int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError(
            "invalid_manifest", "more declared documents than the source's page budget"
        )
    host = (urlsplit(source["endpoint"]).hostname or "").casefold()
    for document in documents:
        try:
            parts = urlsplit(document_url(document))
        except ValueError as exc:
            raise SourcePackError("invalid_manifest", str(exc)) from exc
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError(
                "invalid_manifest",
                "declared series are fetched from the endpoint's host",
            )
        release = document.get("release")
        if release is not None and iso_day(dict(release).get("published_on")) is None:
            raise SourcePackError(
                "invalid_manifest", "a declared release states its publication date"
            )
    return declared


def document_url(document: Mapping[str, Any]) -> str:
    """The series URL, built by the SDMX connector's own SDMX-CSV URL builder."""
    from urllib.parse import urlencode

    from src.ingestion.connectors.dataset.sdmx import SDMXConnector

    url, query = SDMXConnector("OECD").csv_url(
        str(document.get("flow") or ""),
        str(document.get("key") or ""),
        dict(document.get("params") or {}),
    )
    return url + "?" + urlencode(sorted(query.items()))


def parse_crs(raw: bytes, *, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    """Declared CRS series through the SDMX connector's SDMX-CSV reader, one cell per dimension combination."""
    from src.ingestion.connectors.dataset.base import RawSeries, SeriesRef
    from src.ingestion.connectors.dataset.sdmx import SDMXConnector
    from src.integrations.common import IntegrationError

    connector = SDMXConnector("OECD")
    ref = SeriesRef(
        locator=str(document["flow"]) + "/" + str(document.get("key") or ""),
        metadata={"flow": str(document["flow"])},
        title=document.get("label"),
    )
    try:
        records = connector.parse_csv(
            RawSeries(ref, raw, content_type="text/csv", source_url=url, fetched_at=0)
        )
    except IntegrationError as exc:
        raise DevelopmentFinanceFormatError(
            "schema_drift", f"{exc.code}: {exc}"
        ) from exc
    if not records:
        raise DevelopmentFinanceFormatError(
            "schema_drift", "the response states no CRS cell"
        )
    cells = []
    for record in records:
        meta = record.metadata
        dims = dict(meta["dimensions"])
        upper = {k.upper(): v for k, v in dims.items()}
        observations = []
        base_years = set()
        for observation in record.observations:
            attributes = dict(
                meta["observation_attributes"].get(observation.period) or {}
            )
            for key in BASE_YEAR_ATTRIBUTES:
                if text(attributes.get(key)):
                    base_years.add(text(attributes[key]))
            value_text = meta["original_values"].get(observation.period)
            observations.append(
                {
                    "period": observation.period,
                    "value_text": text(value_text),
                    "value": decimal_text(value_text)
                    if text(value_text) not in (None, ":")
                    else None,
                    "attributes": attributes,
                    "row": meta["row_lines"].get(observation.period),
                }
            )
        price_code = text(upper.get("PRICE_BASE"))
        price_basis = PRICE_BASES.get((price_code or "").upper(), "unknown")
        unit_mult = {text(o["attributes"].get("UNIT_MULT")) for o in observations} - {
            None
        }
        cells.append(
            {
                "dataflow": meta.get("dataflow"),
                "dataflow_id": meta.get("dataflow_id"),
                "dimensions": dims,
                "donor": text(upper.get("DONOR")),
                "recipient": classify_recipient(
                    *split_code_label(upper.get("RECIPIENT"))
                ),
                "sector": text(upper.get("SECTOR")),
                "flow_type": text(upper.get("FLOW_TYPE")),
                "channel": text(upper.get("CHANNEL")),
                "measure": text(upper.get("MEASURE")),
                "price_basis": price_basis,
                "price_base_code": price_code,
                "base_year": sorted(base_years)[0] if len(base_years) == 1 else None,
                "base_year_state": "stated"
                if len(base_years) == 1
                else "conflicting"
                if base_years
                else ("not-applicable" if price_basis == "current" else "not-stated"),
                "unit": text(upper.get("UNIT_MEASURE")) or text(record.unit),
                "unit_mult": sorted(unit_mult)[0] if len(unit_mult) == 1 else None,
                "observations": observations,
                "provider_last_update_at": meta.get("provider_last_update_at"),
            }
        )
    stamps = {c["provider_last_update_at"] for c in cells} - {None}
    release = dict(document.get("release") or {})
    if stamps:
        published_on, basis = iso_day(sorted(stamps)[-1]), "provider_last_update"
    elif release.get("published_on"):
        published_on, basis = iso_day(release["published_on"]), "declared_release"
    else:
        published_on, basis = None, "retrieval_time"
    return {
        "cells": cells,
        "published_on": published_on,
        "release_label": text(release.get("label")),
        "vintage_basis": basis,
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "item_count": len(cells),
    }


class DevelopmentFinanceAdapter:
    """Fetch declared CRS series on the runtime's default transport; one page per declared series key."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(
        self,
        source: Mapping[str, Any],
        *,
        transport: Callable[..., Mapping[str, Any]] | None = None,
        secret: str | None = None,
    ) -> None:
        del secret
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = development_finance_declaration(self.source)
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
            "development_finance": {
                "provider": self.declared["provider"],
                "format": self.declared["format"],
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
                "parameter_forbidden", "CRS runs fetch the declared series only"
            )

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage, _retry_after_ms

        self._check(request)
        documents = list(self.declared["documents"])
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(documents):
            raise SourcePackError("cursor_drift", "cursor names no declared series")
        document = dict(documents[index])
        url = document_url(document)
        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        if (urlsplit(url).hostname or "").casefold() != host or urlsplit(
            url
        ).scheme != "https":
            raise SourcePackError(
                "network_policy",
                "declared series are fetched from the endpoint's host only",
            )
        base, _, query = url.partition("?")
        from urllib.parse import parse_qsl

        response = self.transport(
            url=base,
            params=dict(parse_qsl(query)),
            headers={"Accept": "application/vnd.sdmx.data+csv;version=1.0.0, text/csv"},
            timeout=int(self.definition["limits"]["timeout_ms"]) / 1000,
        )
        final_host = (
            urlsplit(str(response.get("final_url") or url)).hostname or ""
        ).casefold()
        if final_host != host:
            raise SourcePackError(
                "network_policy", "series was served from another host"
            )
        status = int(response.get("status", 200))
        headers = {
            str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()
        }
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "series exceeds its byte limit")
        if status == 429:
            raise SourcePackError(
                "rate_limited",
                "provider quota is temporarily exhausted",
                retry_after_ms=_retry_after_ms(headers.get("retry-after")),
            )
        if status in {401, 403}:
            raise SourcePackError(
                "authentication_failed", f"download refused (HTTP {status})"
            )
        if status >= 500:
            raise SourcePackError(
                "source_unavailable", f"provider returned HTTP {status}"
            )
        if status >= 400:
            raise SourcePackError("schema_drift", f"download returned HTTP {status}")
        try:
            release = parse_crs(raw, document=document, url=url)
        except DevelopmentFinanceFormatError as exc:
            raise SourcePackError("schema_drift", f"{exc.code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if release["item_count"] > limit:
            raise SourcePackError(
                "budget_exhausted", "series has more cells than the run's result budget"
            )
        header = {
            "contract": RELEASE_CONTRACT,
            "provider": "oecd-crs",
            "format": self.declared["format"],
            "document": {k: v for k, v in document.items()},
            "published_on": release["published_on"],
            "release_label": release["release_label"],
            "vintage_basis": release["vintage_basis"],
            "file_sha256": release["file_sha256"],
            "item_count": release["item_count"],
            # Only a fixture transport says so; the runtime's HTTPS transport is live evidence.
            "evidence_origin": "fixture"
            if response.get("origin") == "fixture"
            else "live",
            "url": url,
        }
        records = [
            {
                "id": f"{release['file_sha256'][:16]}:{number}",
                "title": f"{document.get('label') or 'OECD CRS'} ({release['published_on'] or 'retrieved'})",
                "url": url,
                "language": "en",
                "published_at": release["published_on"],
                "content": json.dumps(cell, sort_keys=True, ensure_ascii=False),
                "development_finance_release": header,
                "development_finance_item": cell,
            }
            for number, cell in enumerate(release["cells"])
        ]
        receipt = {
            "status": status,
            "provider": "oecd-crs",
            "document": document.get("label"),
            "published_on": release["published_on"],
            "file_sha256": release["file_sha256"],
            "items": len(records),
            "evidence_origin": header["evidence_origin"],
            "final_page": index + 1 >= len(documents),
        }
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: DevelopmentFinanceAdapter}


def fixture_transport(
    pages: Sequence[Mapping[str, Any]],
) -> Callable[..., Mapping[str, Any]]:
    """Replay authored series keyed by URL path and sorted query; responses are marked as fixture evidence."""
    from urllib.parse import urlencode

    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        parts = urlsplit(url)
        key = parts.path + (
            "?" + urlencode(sorted(dict(params or {}).items())) if params else ""
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


def fixture_request(document: Mapping[str, Any]) -> str:
    parts = urlsplit(document_url(document))
    return parts.path + ("?" + parts.query if parts.query else "")


def replay_native_fixture(
    source: Mapping[str, Any], fixture: Mapping[str, Any]
) -> list[dict[str, Any]]:
    adapter = DevelopmentFinanceAdapter(
        source, transport=fixture_transport(list(fixture["native_pages"]))
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


__all__ = [
    "ADAPTERS",
    "BOUNDED_COVERAGE",
    "CONNECTOR",
    "DevelopmentFinanceAdapter",
    "DevelopmentFinanceFormatError",
    "IATIDatastoreClient",
    "LIVE_VERIFICATION",
    "NEVER_SENTENCE",
    "PROVIDER_CONTRACTS",
    "PROVIDER_HOSTS",
    "WorldBankProjectsClient",
    "canonical",
    "classify_recipient",
    "decimal_text",
    "identifier_key",
    "iso_day",
    "iso_instant",
    "parse_crs",
    "parse_iati_activities",
    "parse_world_bank_projects",
    "world_bank_link_evidence",
]
