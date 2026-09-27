"""Bounded acquisition of public-procurement notices through the source-pack runtime.

Sources are selected per recorded access contract (:data:`PROVIDER_CONTRACTS`,
P01). Implemented providers are native source-pack connectors; the runtime
(:class:`~src.ingestion.source_pack_runtime.SourcePackRuntime`) owns budgets,
cursors, retries, license acceptance, credentials, receipts and watermarks,
and projects every committed page into
:class:`~src.kb.procurement_notices.ProcurementNoticeStore`:

* ``ted`` — TED API v3 notice search (eForms business terms, POST JSON,
  iteration-token pagination). The eForms-to-record mapping is
  :data:`EFORMS_MAPPING`. Corrigenda (BT-758) are revisions of the notice they
  change; result notices become award history or cancellations.
* ``ocds`` — UK Find a Tender and Contracts Finder OCDS release packages. The
  OCID is the cross-stage key; ``tenderAmendment`` releases are revisions,
  award/contract stages link to their tender, contract amendments are
  modifications.
* ``sam-gov`` — SAM.gov Get Opportunities v2 (credentialed: the API key is a
  ``NOESIS_SAM_API_KEY`` secret reference, never stored). NAICS, PSC and
  set-aside codes are preserved; SAM carries no CPV.

service.bund.de, the Berlin Vergabeplattform and OpenTender are recorded as
``not-implemented`` with reasons; nothing is scraped. Parsers are fail-closed:
a response that does not have the documented shape raises ``schema_drift``
and nothing is ingested from it. Nothing here submits a bid or contacts a
buyer; only public notice data is read.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from src.ingestion.source_packs import SourcePackError
from src.kb.procurement_records import (
    ProcurementRecordError,
    cpv_code,
    decimal_text,
    party,
    record,
    value,
)

ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_KEY = "procurement_record"
FIXTURE_SECRET = "fixture-sam-api-key-not-a-real-credential"
MAX_PAGE_SIZE = 100
PROVIDER_HOSTS = {
    "ted": {"api.ted.europa.eu"},
    "uk-fts": {"www.find-tender.service.gov.uk"},
    "uk-cf": {"www.contractsfinder.service.gov.uk"},
    "sam-gov": {"api.sam.gov"},
}
CONNECTOR_PROVIDERS = {"ted": ("ted",), "ocds": ("uk-fts", "uk-cf"), "sam-gov": ("sam-gov",)}
PROVIDER_CONTRACTS = {
    "ted": {
        "status": "implemented",
        "connector": "ted",
        "documentation": "https://docs.ted.europa.eu/api/latest/index.html",
        "access": "TED API v3 notice search (POST /v3/notices/search, JSON) over published eForms notices",
        "authentication": "none for search of published notices (API key only for submission, which is never used)",
        "terms": "TED reuse: EU Publications Office legal notice / Commission Decision 2011/833/EU; cite the notice URL",
        "rate_limits": "fair use; HTTP 429 is honoured as rate_limited with Retry-After",
        "pagination": "iterationNextToken (paginationMode=ITERATION), bounded by max_pages",
        "cadence": "notices publish daily (Mon-Fri); refresh at most daily",
        "identifiers": {"notice": "publication number (e.g. 00612345-2026) plus BT-701 notice UUID and BT-757 version",
                        "procedure": "BT-04 procedure identifier (stable across PIN, CN, corrigendum and award)",
                        "lot": "BT-137 lot identifier (LOT-0001)"},
        "schema_versions": "eForms SDK 1.x business terms (BT-xx); legacy TED XML (R2.0.9) notices before 2023-10 are not mapped",
        "cpv_version": "CPV 2008 (Regulation (EC) 213/2008)",
        "cross_references": "corrigendum: BT-758 names the changed notice; result notices share BT-04 with their contract notice",
        "retained_evidence": "raw response SHA-256 per page, JSON pointer and field locators per mapped value, full notice link",
        "coverage": "explicitly selected expert-search queries only; not all of TED",
        "unavailable_fallback": "failed refresh marks the provider stale; notices keep their last revision and are never closed",
    },
    "uk-fts": {
        "status": "implemented",
        "connector": "ocds",
        "documentation": "https://www.find-tender.service.gov.uk/Search/Api",
        "access": "Find a Tender OCDS release packages (GET /api/1.0/ocdsReleasePackages)",
        "authentication": "none for reading published notices",
        "terms": "Open Government Licence v3.0",
        "rate_limits": "undocumented fair use; HTTP 429 honoured as rate_limited",
        "pagination": "cursor via links.next (same host only), bounded by max_pages",
        "cadence": "continuous; refresh at most hourly",
        "identifiers": {"notice": "OCDS release id", "procedure": "OCID (ocds-h6vhtk-...)", "lot": "tender.lots[].id"},
        "schema_versions": "OCDS 1.1 with UK profile extensions (lots, selection criteria, amountGross)",
        "cpv_version": "CPV 2008 via items[].classification (scheme CPV)",
        "cross_references": "all stages share the OCID; tenderAmendment and contractAmendment releases carry amendments[]",
        "retained_evidence": "raw release package SHA-256 per page, JSON pointers per mapped value",
        "coverage": "explicitly selected date windows / stages only",
        "unavailable_fallback": "failed refresh marks the provider stale; nothing is closed",
    },
    "uk-cf": {
        "status": "implemented",
        "connector": "ocds",
        "documentation": "https://www.contractsfinder.service.gov.uk/apidocumentation",
        "access": "Contracts Finder OCDS search (GET /Published/Notices/OCDS/Search)",
        "authentication": "none for reading published notices",
        "terms": "Open Government Licence v3.0",
        "rate_limits": "undocumented fair use; HTTP 429 honoured as rate_limited",
        "pagination": "cursor via links.next (same host only), bounded by max_pages",
        "cadence": "continuous; refresh at most hourly",
        "identifiers": {"notice": "OCDS release id", "procedure": "OCID (ocds-b5fd17-...)", "lot": "rarely lotted"},
        "schema_versions": "OCDS 1.1",
        "cpv_version": "CPV 2008 via items[].classification",
        "cross_references": "stages share the OCID",
        "retained_evidence": "raw release package SHA-256 per page, JSON pointers per mapped value",
        "coverage": "explicitly selected date windows only; mostly below-threshold English notices",
        "unavailable_fallback": "failed refresh marks the provider stale; nothing is closed",
    },
    "sam-gov": {
        "status": "implemented",
        "connector": "sam-gov",
        "documentation": "https://open.gsa.gov/api/get-opportunities-public-api/",
        "access": "SAM.gov Get Opportunities Public API v2 (GET /opportunities/v2/search)",
        "authentication": "required API key (secret reference NOESIS_SAM_API_KEY; sent as api_key, never stored or logged)",
        "terms": "US Government public data; GSA API terms of use apply to the key holder",
        "rate_limits": "per-key daily request quotas set by GSA by account role; HTTP 429 honoured as rate_limited",
        "pagination": "limit/offset against totalRecords, bounded by max_pages; postedFrom/postedTo window of at most one year",
        "cadence": "daily at most",
        "identifiers": {"notice": "noticeId", "procedure": "solicitationNumber (falls back to noticeId)", "lot": "none"},
        "schema_versions": "Opportunities API v2 JSON",
        "cpv_version": "none: NAICS (naicsCode), PSC (classificationCode) and set-aside codes are preserved instead",
        "cross_references": "award notices share the solicitation number with their solicitation",
        "retained_evidence": "raw response SHA-256 per page (request URL recorded without the key)",
        "coverage": "explicitly selected posted-date window and NAICS/set-aside filters",
        "unavailable_fallback": "missing credential blocks the source (credential_missing); nothing is closed",
    },
    "service-bund": {
        "status": "not-implemented",
        "reason": ("service.bund.de publishes federal tender listings as portal pages and an RSS listing feed of titles "
                   "and links only; no documented machine interface for notice content (deadlines, lots, criteria) "
                   "exists, and portal pages are not scraped. Federal notices above the EU thresholds are covered "
                   "through TED."),
        "fallback": "use TED for above-threshold federal notices; below-threshold federal notices are not covered",
    },
    "berlin-vergabe": {
        "status": "not-implemented",
        "reason": ("The Berlin Vergabeplattform offers no documented public API or bulk export for notices; the "
                   "portal requires interactive use and is not scraped. Berlin notices above the EU thresholds are "
                   "covered through TED."),
        "fallback": "use TED for above-threshold Berlin notices; below-threshold Berlin notices are not covered",
    },
    "opentender": {
        "status": "not-implemented",
        "reason": ("OpenTender (DIGIWHIST) offers bulk historical downloads derived from TED and national portals; "
                   "it is award history only (never evidence of an open procedure) and duplicates TED award notices, "
                   "so it is not ingested in this bounded profile."),
        "fallback": "award history comes from TED result notices and OCDS award/contract releases",
    },
}
LIVE_VERIFICATION = {
    provider: {"status": "unverified-live",
               "note": "no successful live run from this runtime; see scripts/procurement_live_check.py and "
                       "docs/development/procurement-evidence/"}
    for provider in ("ted", "uk-fts", "uk-cf", "sam-gov")
}
LIVE_VERIFICATION.update({provider: {"status": "not-implemented", "note": PROVIDER_CONTRACTS[provider]["reason"]}
                          for provider in ("service-bund", "berlin-vergabe", "opentender")})
# Recorded result of the most recent bounded live run (P15); it never upgrades
# the status above without a successful receipt.
LAST_LIVE_CHECK = {
    "date": "2026-09-27",
    "report": "docs/development/procurement-evidence/live-check-2026-09-27.json",
    "result": "blocked",
    "detail": "no provider was reached: the build environment's egress proxy refused CONNECT to every provider host",
    "providers": {
        "ted": {"result": "blocked", "failure_code": "source_unavailable", "transport_detail": "Tunnel connection failed: 403 Forbidden"},
        "uk-fts": {"result": "blocked", "failure_code": "source_unavailable", "transport_detail": "Tunnel connection failed: 403 Forbidden"},
        "uk-cf": {"result": "blocked", "failure_code": "source_unavailable", "transport_detail": "Tunnel connection failed: 403 Forbidden"},
        "sam-gov": {"result": "blocked", "failure_code": "credential_missing", "transport_detail": None,
                    "note": "blocked at preflight: NOESIS_SAM_API_KEY is not configured; no request was sent"},
    },
}
for _provider, _result in LAST_LIVE_CHECK["providers"].items():
    LIVE_VERIFICATION[_provider] = {**LIVE_VERIFICATION[_provider], "last_check": {"date": LAST_LIVE_CHECK["date"], **_result}}

# ------------------------------------------------------------------ eForms map

# eForms business term (as requested in the TED v3 ``fields`` list) -> record
# path. Per-lot fields are arrays aligned with BT-137-Lot; per-result fields
# are arrays aligned with BT-13713-LotResult.
EFORMS_MAPPING = {
    "publication-number": "notice_id (and source_url https://ted.europa.eu/en/notice/-/detail/<id>)",
    "notice-type": "stage (pin-* -> prior-information, cn-* -> contract-notice, can-* -> award/cancellation, can-modif -> modification)",
    "BT-701-notice": "references[kind=related] notice UUID (retained)",
    "BT-757-notice": "notice_version",
    "BT-04-procedure": "procedure_id (cross-stage key)",
    "BT-758-notice": "stage=corrigendum; changes.changes_notice_id",
    "BT-140-notice": "changes.reason (code retained)",
    "BT-141(a)-notice": "changes.description (language-tagged)",
    "publication-date": "published",
    "notice-title": "title/titles (BT-21-Procedure, every language kept)",
    "buyer-name": "buyer.name/names (BT-500-Organization-Company of the buyer)",
    "buyer-identifier": "buyer.identifiers[scheme=national] (BT-501)",
    "buyer-country": "buyer.country (BT-514, ISO 3166-1 alpha-3 -> alpha-2)",
    "procedure-type": "procedure.type (BT-105 codes normalised; native kept)",
    "classification-cpv": "classifications[scheme=CPV, primary] (BT-262-Procedure)",
    "estimated-value-proc": "estimated_value.amount (BT-27-Procedure; eForms values exclude VAT)",
    "estimated-value-cur-proc": "estimated_value.currency",
    "place-of-performance-country-proc": "place_of_performance.countries (BT-5141)",
    "BT-5071-Procedure": "place_of_performance.nuts",
    "BT-67(a)-Procedure": "requirements[category=exclusion] (code -> exclusion.* self-declaration rule)",
    "BT-67(b)-Procedure": "requirements[category=exclusion].text",
    "BT-137-Lot": "lots[].lot_id",
    "BT-21-Lot": "lots[].title/titles",
    "BT-262-Lot": "lots[].classifications[scheme=CPV]",
    "BT-27-Lot": "lots[].estimated_value.amount (excluding VAT)",
    "BT-27-Lot-Currency": "lots[].estimated_value.currency",
    "BT-131(d)-Lot/BT-131(t)-Lot": "deadlines[kind=submission, lot_id] (text and offset kept)",
    "BT-13(d)-Lot/BT-13(t)-Lot": "deadlines[kind=clarification, lot_id]",
    "BT-132(d)-Lot/BT-132(t)-Lot": "deadlines[kind=opening, lot_id]",
    "BT-747-Lot": "requirements[].category per lot (ef-stand -> economic-financial, tp-abil -> technical-professional, sui-act -> suitability)",
    "BT-750-Lot": "requirements[].text per lot (threshold, reference and certification rules extracted by pattern)",
    "BT-15-Lot": "documents[kind=procurement-documents]",
    "BT-13713-LotResult": "awards[].lot_ids",
    "BT-142-LotResult": "awards[].native_status (selec-w -> active; clos-nw on every lot -> stage cancellation)",
    "BT-720-Tender/BT-720-Tender-Currency": "awards[].awarded_value (excluding VAT)",
    "winner-name/winner-identifier/winner-country": "awards[].suppliers (source strings and national identifiers)",
    "BT-1451-Contract": "awards[].date (contract conclusion date)",
}
_ISO3 = {"DEU": "DE", "FRA": "FR", "NLD": "NL", "AUT": "AT", "BEL": "BE", "ITA": "IT", "ESP": "ES", "POL": "PL",
         "GBR": "GB", "USA": "US", "DNK": "DK", "SWE": "SE", "FIN": "FI", "IRL": "IE", "LUX": "LU", "CZE": "CZ",
         "PRT": "PT", "HUN": "HU", "ROU": "RO", "BGR": "BG", "GRC": "GR", "SVK": "SK", "SVN": "SI", "HRV": "HR",
         "EST": "EE", "LVA": "LV", "LTU": "LT", "CYP": "CY", "MLT": "MT", "NOR": "NO", "CHE": "CH"}
_LANG = {"eng": "en", "deu": "de", "fra": "fr", "nld": "nl", "ita": "it", "spa": "es", "pol": "pl", "dan": "da",
         "swe": "sv", "fin": "fi", "por": "pt", "ces": "cs", "hun": "hu", "ron": "ro", "bul": "bg", "ell": "el",
         "slk": "sk", "slv": "sl", "hrv": "hr", "est": "et", "lav": "lv", "lit": "lt", "gle": "ga", "mlt": "mt"}
_TED_PROCEDURE = {"open": "open", "restricted": "restricted", "neg-w-call": "negotiated-with-call",
                  "neg-wo-call": "negotiated-without-call", "comp-dial": "competitive-dialogue",
                  "innovation": "innovation-partnership", "oth-single": "other", "oth-mult": "other"}
_TED_CRITERION = {"ef-stand": "economic-financial", "tp-abil": "technical-professional", "sui-act": "suitability"}
# eForms exclusion-ground codes (BT-67(a)) -> supplier self-declaration fact.
EXCLUSION_CODES = {
    "crime-org": "exclusion.criminal_conviction", "corruption": "exclusion.criminal_conviction",
    "fraud": "exclusion.criminal_conviction", "terr-offence": "exclusion.criminal_conviction",
    "finan-laund": "exclusion.criminal_conviction", "hum-traff": "exclusion.criminal_conviction",
    "tax-pay": "exclusion.tax_arrears", "socsec-pay": "exclusion.social_security_arrears",
    "bankruptcy": "exclusion.insolvency", "insolvency": "exclusion.insolvency", "liq-admin": "exclusion.insolvency",
    "cred-arran": "exclusion.insolvency", "bankr-nat": "exclusion.insolvency", "prof-misconduct": "exclusion.professional_misconduct",
    "conflict-interest": "exclusion.conflict_of_interest", "distorsion": "exclusion.distortion_of_competition",
    "sanction": "exclusion.prior_termination", "misrepresent": "exclusion.misrepresentation",
    "envir-law": "exclusion.environmental_social_labour", "socsec-law": "exclusion.environmental_social_labour",
    "labour-law": "exclusion.environmental_social_labour",
}
_OCDS_METHOD = {"open": "open", "selective": "restricted", "limited": "negotiated-without-call", "direct": "direct-award"}
_OCDS_CRITERION = {"economic": "economic-financial", "technical": "technical-professional", "suitability": "suitability"}
_SAM_STAGE = {"Presolicitation": "prior-information", "Sources Sought": "prior-information",
              "Special Notice": "prior-information", "Solicitation": "contract-notice",
              "Combined Synopsis/Solicitation": "contract-notice", "Award Notice": "award",
              "Justification": "award", "Sale of Surplus Property": None, "Intent to Bundle Requirements (DoD-Funded)": None}
_SAM_SET_ASIDE = {"SBA": ["small-business"], "SBP": ["small-business"], "8A": ["8a"], "8AN": ["8a"],
                  "HZC": ["hubzone"], "HZS": ["hubzone"], "SDVOSBC": ["sdvosb"], "SDVOSBS": ["sdvosb"],
                  "WOSB": ["wosb", "edwosb"], "WOSBSS": ["wosb", "edwosb"], "EDWOSB": ["edwosb"], "EDWOSBSS": ["edwosb"],
                  "VSA": ["vosb", "sdvosb"], "VSS": ["vosb", "sdvosb"]}


def _drift(message):
    raise SourcePackError("schema_drift", message)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _digest(value_):
    return hashlib.sha256(json.dumps(value_, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


# ----------------------------------------------------------- requirement rules

_MONEY_RE = re.compile(
    r"(?P<cur>EUR|GBP|USD|€|£|\$)\s?(?P<amount>\d{1,3}(?:[ ,.  ]\d{3})+|\d+)(?:\s?(?P<mult>million|m|k)\b)?"
    r"|(?P<amount2>\d{1,3}(?:[ ,.  ]\d{3})+|\d+)\s?(?P<mult2>million|m|k)?\s?(?P<cur2>EUR|GBP|USD|€|£)", re.IGNORECASE)
_CURRENCY_SIGN = {"€": "EUR", "£": "GBP", "$": "USD"}
_WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
                 "drei": 3, "zwei": 2, "vier": 4, "fünf": 5}
_CERTIFICATION_RE = re.compile(r"\bISO(?:/IEC)?\s?(\d{4,5}(?:-\d)?)\b|\bCyber Essentials(?: Plus)?\b|\bFedRAMP\b|\bCMMI\b", re.IGNORECASE)


def _money(text):
    match = _MONEY_RE.search(text)
    if not match:
        return None
    raw = match.group("amount") or match.group("amount2")
    currency = (match.group("cur") or match.group("cur2")).upper()
    currency = _CURRENCY_SIGN.get(currency, currency)
    multiplier = (match.group("mult") or match.group("mult2") or "").lower()
    digits = re.sub(r"[ ,.  ]", "", raw)
    number = int(digits) * {"million": 1_000_000, "m": 1_000_000, "k": 1000}.get(multiplier, 1)
    return {"amount": str(number), "currency": currency, "quote": match.group(0)}


def criterion_rule(category, text):
    """Extract a machine rule from a selection-criterion text, or ``None`` (unparsed, never a pass)."""
    lowered = text.lower()
    if category == "economic-financial" and re.search(r"turnover|umsatz", lowered):
        amount_ = _money(text)
        if amount_:
            return {"fact": f"supplier.annual_turnover.{amount_['currency']}", "op": "gte", "value": amount_["amount"]}
    if category == "economic-financial" and re.search(r"indemnity|insurance|haftpflicht", lowered):
        amount_ = _money(text)
        if amount_:
            return {"fact": f"supplier.insurance_cover.{amount_['currency']}", "op": "gte", "value": amount_["amount"]}
    if category == "technical-professional" and re.search(r"reference|referenz|comparable contracts", lowered):
        match = re.search(r"(?:at least|minimum of|mindestens)\s+(\d+|\w+)", lowered)
        if match:
            count = int(match.group(1)) if match.group(1).isdigit() else _WORD_NUMBERS.get(match.group(1))
            if count:
                return {"fact": "supplier.references_count", "op": "gte", "value": count}
    if category in {"technical-professional", "certification", "suitability"}:
        certificates = _certifications(text)
        # Only rules that name exactly one certificate are extracted; alternatives stay unparsed.
        if len(certificates) == 1 and not re.search(r"\bor equivalent\b|\boder gleichwertig\b", lowered):
            return {"fact": "supplier.certifications", "op": "intersects", "value": certificates}
    return None


def _certifications(text):
    found = []
    for match in _CERTIFICATION_RE.finditer(text):
        if match.group(1):
            standard = match.group(1)
            prefix = "ISO/IEC" if "iec" in match.group(0).lower() else "ISO"
            found.append(f"{prefix} {standard}")
        else:
            found.append(match.group(0).strip())
    return sorted(set(found))


def exclusion_requirement(provider, code, text, *, locator, index):
    fact = EXCLUSION_CODES.get(code)
    return {"requirement_id": f"{provider}:exclusion:{code}", "category": "exclusion", "native_code": code,
            "text": text, "hard": True, "locator": locator,
            **({"machine_rule": {"fact": fact, "op": "is_false"}} if fact else {})}


def _criterion(provider, key, category, text, *, locator, lot_ids, language):
    rule = criterion_rule(category, text)
    if category == "technical-professional" and rule and rule["fact"] == "supplier.certifications":
        category = "certification"
    return {"requirement_id": f"{provider}:{key}", "category": category, "text": text, "language": language,
            "hard": True, "locator": locator, "lot_ids": lot_ids, **({"machine_rule": rule} if rule else {})}


# ------------------------------------------------------------------- dates


def deadline(kind, date_text, time_text=None, *, lot_id=None, locator=None, original=None):
    """eForms/OCDS/SAM date + time with offset -> deadline keeping original text and offset."""
    text = original or " ".join(t for t in (date_text, time_text) if t)
    if not date_text:
        return None
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(Z|[+-]\d{2}:\d{2})?", date_text.strip())
    item = {"kind": kind, "text": text, "date": None, "instant": None, "timezone": None,
            **({"lot_id": lot_id} if lot_id else {}), **({"locator": locator} if locator else {})}
    if match:
        item["date"] = match.group(1)
        offset = match.group(2)
        if time_text:
            clock = re.fullmatch(r"(\d{2}:\d{2}(?::\d{2})?)(Z|[+-]\d{2}:\d{2})?", time_text.strip())
            if not clock:
                _drift(f"unparseable deadline time {time_text!r}")
            zone = clock.group(2) or offset
            if zone:
                zone = "+00:00" if zone == "Z" else zone
                item.update(instant=f"{match.group(1)}T{clock.group(1)}{zone}", timezone=f"{zone} (offset stated by the source)")
        return item
    instant = re.fullmatch(r"(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?(Z|[+-]\d{2}:?\d{2})", date_text.strip())
    if instant:
        zone = instant.group(3)
        zone = "+00:00" if zone == "Z" else (zone if ":" in zone else zone[:3] + ":" + zone[3:])
        item.update(date=instant.group(1), instant=f"{instant.group(1)}T{instant.group(2)}{zone}",
                    timezone=f"{zone} (offset stated by the source)")
        return item
    _drift(f"unparseable deadline {date_text!r}")


# ------------------------------------------------------------------- TED


def _languages(field):
    """TED multilingual value -> {iso639-1: text}; lists join (source order)."""
    if field is None:
        return {}
    if isinstance(field, str):
        return {"und": field}
    if not isinstance(field, dict):
        _drift("multilingual field must be an object keyed by language")
    result = {}
    for lang, text in field.items():
        tag = _LANG.get(lang, lang)
        if isinstance(text, list):
            text = " ".join(str(t) for t in text)
        if not isinstance(text, str):
            _drift("multilingual values are text")
        result[tag] = text
    return result


def _per_item(field, count, name):
    """Language-tagged per-lot (or per-result) texts, aligned by position."""
    if field is None:
        return [{} for _ in range(count)]
    if not isinstance(field, dict):
        _drift(f"{name} must be keyed by language")
    rows = [{} for _ in range(count)]
    for lang, texts in field.items():
        if not isinstance(texts, list) or len(texts) != count:
            _drift(f"{name}[{lang}] is not aligned with its lots/results")
        for index, text in enumerate(texts):
            rows[index][_LANG.get(lang, lang)] = text
    return rows


def _pick(texts, preferred=("en", "de", "fr")):
    for tag in preferred:
        if texts.get(tag):
            return tag, texts[tag]
    if texts:
        tag = sorted(texts)[0]
        return tag, texts[tag]
    return None, None


def _aligned(notice, key, count, *, required=False):
    values = notice.get(key)
    if values is None:
        if required:
            _drift(f"{key} is missing")
        return [None] * count
    if not isinstance(values, list) or len(values) != count:
        _drift(f"{key} is not aligned with BT-137-Lot / BT-13713-LotResult")
    return values


def _first(values):
    if isinstance(values, list):
        return values[0] if values else None
    return values


def _country(code):
    if code is None:
        return None
    code = str(code).upper()
    if len(code) == 2:
        return code
    if code not in _ISO3:
        _drift(f"unmapped country code {code}")
    return _ISO3[code]


def parse_ted_notice(notice, *, index=0):
    """One TED v3 search hit (eForms fields) -> a procurement notice record."""
    if not isinstance(notice, dict):
        _drift("TED notice must be an object")
    pointer = ""  # JSON pointers are relative to the notice object, so page position never changes a record
    number = notice.get("publication-number")
    procedure_id = notice.get("BT-04-procedure")
    notice_type = notice.get("notice-type")
    if not number or not procedure_id or not notice_type:
        _drift("TED notice lacks publication-number, BT-04-procedure or notice-type")
    url = f"https://ted.europa.eu/en/notice/-/detail/{number}"
    titles = _languages(notice.get("notice-title"))
    language, title = _pick(titles)
    if not title:
        _drift("TED notice has no title")
    names = _languages(notice.get("buyer-name"))
    _, buyer_name = _pick(names, ("de", "en", "fr"))
    if not buyer_name:
        _drift("TED notice has no buyer-name")
    buyer_ids = [{"scheme": "national", "id": str(i)} for i in (notice.get("buyer-identifier") or [])]
    buyer = party("buyer", buyer_name, identifiers=buyer_ids, country=_country(_first(notice.get("buyer-country"))),
                  names=names if len(names) > 1 else None)
    procedure_native = notice.get("procedure-type")
    lots = notice.get("BT-137-Lot") or []
    if not isinstance(lots, list):
        _drift("BT-137-Lot must be a list")
    count = len(lots)
    changes = None
    if notice_type.startswith("pin"):
        stage = "prior-information"
    elif notice_type == "can-modif":
        stage = "modification"
    elif notice_type.startswith("can"):
        results = notice.get("BT-142-LotResult") or []
        stage = "cancellation" if results and all(r == "clos-nw" for r in results) else "award"
    elif notice_type.startswith("cn"):
        stage = "contract-notice"
    else:
        _drift(f"unmapped TED notice-type {notice_type}")
    if notice.get("BT-758-notice"):
        if stage != "contract-notice":
            _drift("change notices are only mapped for contract notices in this profile")
        stage = "corrigendum"
        reason = _languages(notice.get("BT-141(a)-notice"))
        changes = {"changes_notice_id": notice["BT-758-notice"], "reason": notice.get("BT-140-notice"),
                   "description": reason}
    fields = {
        "notice_version": notice.get("BT-757-notice"),
        "published": (notice.get("publication-date") or "")[:10] or None,
        "titles": titles, "language": language,
        "references": [{"kind": "related", "notice_id": notice["BT-701-notice"]}] if notice.get("BT-701-notice") else [],
    }
    if changes:
        fields["changes"] = changes
        fields["references"].append({"kind": "changes", "notice_id": changes["changes_notice_id"]})
    cpv_main = notice.get("classification-cpv") or []
    fields["classifications"] = [{"scheme": "CPV", "code": cpv_code(c), "version": "CPV 2008", "primary": i == 0, "native": c}
                                 for i, c in enumerate(cpv_main) if cpv_code(c)]
    if len(fields["classifications"]) != len(cpv_main):
        _drift("unparseable CPV code")
    if stage in {"award", "modification"}:
        fields["awards"] = _ted_awards(notice, url, pointer)
        fields["status"] = {"native": notice_type, "asserted": "complete"}
        return record("ted", stage, number, procedure_id, title, source_url=url, buyer=buyer, **fields)
    fields["procedure"] = {"type": _TED_PROCEDURE.get(procedure_native, "unknown" if not procedure_native else "other"),
                           **({"native": procedure_native} if procedure_native else {}),
                           "locator": {"json_pointer": pointer + "/procedure-type", "field": "BT-105-Procedure"}}
    if notice.get("estimated-value-proc") is not None:
        fields["estimated_value"] = value(decimal_text(notice["estimated-value-proc"]), notice.get("estimated-value-cur-proc"),
                                          "estimated", vat="excluded", native="BT-27-Procedure (eForms: excluding VAT)",
                                          locator={"json_pointer": pointer + "/estimated-value-proc", "field": "BT-27-Procedure"})
    countries = [_country(c) for c in notice.get("place-of-performance-country-proc") or []]
    if countries or notice.get("BT-5071-Procedure"):
        fields["place_of_performance"] = {"countries": countries, "nuts": list(notice.get("BT-5071-Procedure") or [])}
    lot_titles = _per_item(notice.get("BT-21-Lot"), count, "BT-21-Lot")
    lot_cpv = _aligned(notice, "BT-262-Lot", count)
    lot_values = _aligned(notice, "BT-27-Lot", count)
    lot_currency = _aligned(notice, "BT-27-Lot-Currency", count)
    lot_docs = _aligned(notice, "BT-15-Lot", count)
    record_lots, deadlines, documents = [], [], []
    for position, lot_id in enumerate(lots):
        tag, lot_title = _pick(lot_titles[position])
        lot = {"lot_id": lot_id, "title": lot_title, "titles": lot_titles[position] or None,
               "classifications": [{"scheme": "CPV", "code": cpv_code(lot_cpv[position]), "version": "CPV 2008", "primary": True,
                                    "native": lot_cpv[position]}] if lot_cpv[position] else []}
        if lot_cpv[position] and not cpv_code(lot_cpv[position]):
            _drift("unparseable lot CPV code")
        if lot_values[position] is not None:
            lot["estimated_value"] = value(decimal_text(lot_values[position]), lot_currency[position], "estimated", vat="excluded",
                                           native="BT-27-Lot (eForms: excluding VAT)",
                                           locator={"json_pointer": f"{pointer}/BT-27-Lot/{position}", "field": "BT-27-Lot"})
        record_lots.append({k: v for k, v in lot.items() if v is not None})
        for kind, date_key, time_key in (("submission", "BT-131(d)-Lot", "BT-131(t)-Lot"),
                                         ("clarification", "BT-13(d)-Lot", "BT-13(t)-Lot"),
                                         ("opening", "BT-132(d)-Lot", "BT-132(t)-Lot")):
            dates, times = _aligned(notice, date_key, count), _aligned(notice, time_key, count)
            if dates[position]:
                item = deadline(kind, dates[position], times[position], lot_id=lot_id,
                                locator={"json_pointer": f"{pointer}/{date_key}/{position}", "field": date_key})
                deadlines.append(item)
        if lot_docs[position]:
            documents.append({"url": lot_docs[position], "title": "Procurement documents",
                              "kind": "procurement-documents", "locator": {"json_pointer": f"{pointer}/BT-15-Lot/{position}", "field": "BT-15-Lot"}})
    fields.update(lots=record_lots, deadlines=deadlines, documents=list({d["url"]: d for d in reversed(documents)}.values())[::-1])
    requirements = []
    codes = notice.get("BT-67(a)-Procedure") or []
    texts = _per_item(notice.get("BT-67(b)-Procedure"), len(codes), "BT-67(b)-Procedure")
    for position, code in enumerate(codes):
        tag, text = _pick(texts[position])
        requirements.append({**exclusion_requirement("ted", code, text or f"Exclusion ground: {code}",
                                                     locator={"json_pointer": f"{pointer}/BT-67(a)-Procedure/{position}",
                                                              "field": "BT-67(a)-Procedure", **({"quote": text[:400]} if text else {})},
                                                     index=position), **({"language": tag} if tag else {})})
    kinds = _aligned(notice, "BT-747-Lot", count)
    descriptions = notice.get("BT-750-Lot") or {}
    for position, lot_id in enumerate(lots):
        lot_kinds = kinds[position] or []
        per_language = {}
        for lang, rows in descriptions.items():
            if not isinstance(rows, list) or len(rows) != count or not isinstance(rows[position], list) \
                    or len(rows[position]) != len(lot_kinds):
                _drift("BT-750-Lot is not aligned with BT-747-Lot")
            per_language[_LANG.get(lang, lang)] = rows[position]
        for number_, kind in enumerate(lot_kinds):
            category = _TED_CRITERION.get(kind, "other")
            tag, text = _pick({lang: rows[number_] for lang, rows in per_language.items()})
            if not text:
                _drift("selection criterion without description")
            requirements.append(_criterion(
                "ted", f"{lot_id}:criterion:{number_ + 1}", category, text, lot_ids=[lot_id], language=tag,
                locator={"json_pointer": f"{pointer}/BT-750-Lot/{tag}/{position}/{number_}", "field": "BT-750-Lot",
                         "language": tag, "quote": text[:400]}))
    fields["requirements"] = requirements
    fields["status"] = {"native": notice_type, "asserted": "planned" if stage == "prior-information" else
                        "cancelled" if stage == "cancellation" else "active"}
    if stage == "cancellation":
        fields.pop("deadlines", None)
    return record("ted", stage, number, procedure_id, title, source_url=url, buyer=buyer, **fields)


def _ted_awards(notice, url, pointer):
    results = notice.get("BT-13713-LotResult") or []
    count = len(results)
    statuses = _aligned(notice, "BT-142-LotResult", count, required=True)
    amounts = _aligned(notice, "BT-720-Tender", count)
    currencies = _aligned(notice, "BT-720-Tender-Currency", count)
    winners = _per_item(notice.get("winner-name"), count, "winner-name")
    winner_ids = _aligned(notice, "winner-identifier", count)
    winner_countries = _aligned(notice, "winner-country", count)
    dates = _aligned(notice, "BT-1451-Contract", count)
    awards = []
    for position, lot_id in enumerate(results):
        _, name = _pick(winners[position], ("de", "en", "fr"))
        suppliers = [party("supplier", name, country=_country(winner_countries[position]),
                           identifiers=[{"scheme": "national", "id": str(winner_ids[position])}] if winner_ids[position] else [])] if name else []
        awards.append({
            "award_id": f"{notice['publication-number']}:{lot_id}", "lot_ids": [lot_id],
            "date": (dates[position] or "")[:10] or None, "native_status": statuses[position],
            "status": {"selec-w": "active", "clos-nw": "unsuccessful", "open-nw": "pending"}.get(statuses[position], "unknown"),
            "suppliers": suppliers,
            **({"awarded_value": value(decimal_text(amounts[position]), currencies[position], "awarded", vat="excluded",
                                       native="BT-720-Tender (eForms: excluding VAT)",
                                       locator={"json_pointer": f"{pointer}/BT-720-Tender/{position}", "field": "BT-720-Tender"})}
               if amounts[position] is not None else {}),
            "locator": {"json_pointer": f"{pointer}/BT-13713-LotResult/{position}", "field": "BT-13713-LotResult"},
        })
    if not awards:
        _drift("result notice without BT-13713-LotResult")
    return awards


def parse_ted_search(payload):
    """TED v3 search response -> records; each hit is mapped fail-closed."""
    if not isinstance(payload, dict) or not isinstance(payload.get("notices"), list):
        _drift("TED search response lacks notices[]")
    if payload.get("timedOut"):
        raise SourcePackError("source_timeout", "TED search timed out server-side; results are partial")
    records = []
    for index, notice in enumerate(payload["notices"]):
        try:
            records.append(parse_ted_notice(notice, index=index))
        except ProcurementRecordError as exc:
            raise SourcePackError("mapping_failed", f"notice {notice.get('publication-number')}: {exc}") from exc
    return {"records": records, "next": payload.get("iterationNextToken") or None,
            "total": payload.get("totalNoticeCount")}


# ------------------------------------------------------------------- OCDS


def _ocds_value(item, kind, pointer):
    if not item or item.get("amount") is None and item.get("amountGross") is None:
        return None
    currency = item.get("currency")
    if item.get("amount") is not None and item.get("amountGross") is not None:
        # UK profile: ``amount`` is the net value when a gross value is also stated.
        return value(decimal_text(item["amount"]), currency, kind, vat="excluded",
                     native=f"amount (net) with amountGross {decimal_text(item['amountGross'])}",
                     locator={"json_pointer": pointer + "/amount"})
    if item.get("amount") is None:
        return value(decimal_text(item["amountGross"]), currency, kind, vat="included", locator={"json_pointer": pointer + "/amountGross"})
    return value(decimal_text(item["amount"]), currency, kind, vat="unknown", locator={"json_pointer": pointer + "/amount"})


def _ocds_party(entry, role):
    identifiers = []
    for identifier in [entry.get("identifier") or {}] + list(entry.get("additionalIdentifiers") or []):
        scheme, ident = identifier.get("scheme"), identifier.get("id")
        if not scheme or not ident:
            continue
        mapped = {"XI-LEI": "LEI", "GB-COH": "GB-COH", "GB-CHC": "GB-CHC", "GB-VAT": "VAT", "XI-EORI": "other"}.get(scheme)
        identifiers.append({"scheme": mapped or "other", "id": str(ident), "native_scheme": scheme})
    country = (entry.get("address") or {}).get("countryName")
    code = {"United Kingdom": "GB", "England": "GB", "Scotland": "GB", "Wales": "GB", "Northern Ireland": "GB",
            "Germany": "DE", "Ireland": "IE", "France": "FR", "United States": "US"}.get(country)
    return party(role, entry.get("name") or "(unnamed party)", identifiers=identifiers, country=code, native_id=entry.get("id"))


def parse_ocds_release(release, provider, *, index=0):
    if not isinstance(release, dict) or not release.get("ocid") or not release.get("id") or not isinstance(release.get("tag"), list):
        _drift("OCDS release lacks ocid, id or tag")
    pointer = ""  # relative to the release object
    tags = set(release["tag"])
    parties = {p.get("id"): p for p in release.get("parties") or [] if isinstance(p, dict)}
    buyer_ref = (release.get("buyer") or {}).get("id")
    buyer_entry = parties.get(buyer_ref) or release.get("buyer")
    if not buyer_entry or not buyer_entry.get("name"):
        _drift("OCDS release lacks a buyer")
    buyer = _ocds_party({**(parties.get(buyer_ref) or {}), **{k: v for k, v in (release.get("buyer") or {}).items() if v}}, "buyer")
    tender = release.get("tender") or {}
    host = {"uk-fts": "www.find-tender.service.gov.uk", "uk-cf": "www.contractsfinder.service.gov.uk"}[provider]
    url = (f"https://{host}/Notice/{urllib.parse.quote(release['id'], safe='')}" if provider == "uk-fts"
           else f"https://{host}/Notice/{urllib.parse.quote(release['id'], safe='')}")
    title = tender.get("title") or ((release.get("awards") or [{}])[0].get("title")) or release["ocid"]
    language = release.get("language") or "en"
    classifications, seen = [], set()
    for item in tender.get("items") or []:
        classification = item.get("classification") or {}
        if classification.get("scheme") == "CPV" and cpv_code(classification.get("id")) and classification["id"] not in seen:
            seen.add(classification["id"])
            classifications.append({"scheme": "CPV", "code": cpv_code(classification["id"]), "version": "CPV 2008",
                                    "primary": not classifications, "native": classification["id"],
                                    **({"description": classification["description"]} if classification.get("description") else {})})
    common = {"published": (release.get("date") or "")[:10] or None, "classifications": classifications,
              "references": [{"kind": "related", "notice_id": release["id"]}]}
    if tags & {"award", "awardUpdate", "contract", "contractUpdate", "contractAmendment"} and not tags & {"tender", "tenderUpdate", "tenderAmendment"}:
        stage = "modification" if "contractAmendment" in tags else "award"
        awards = []
        for position, award in enumerate(release.get("awards") or []):
            suppliers = [_ocds_party({**(parties.get(s.get("id")) or {}), **{k: v for k, v in s.items() if v}}, "supplier")
                         for s in award.get("suppliers") or []]
            awards.append({"award_id": str(award.get("id") or position), "lot_ids": list(award.get("relatedLots") or []),
                           "date": (award.get("date") or "")[:10] or None, "native_status": award.get("status"),
                           "status": {"active": "active", "pending": "pending", "cancelled": "cancelled",
                                      "unsuccessful": "unsuccessful"}.get(award.get("status"), "unknown"),
                           "suppliers": suppliers,
                           **({"awarded_value": v} if (v := _ocds_value(award.get("value"), "awarded", f"{pointer}/awards/{position}/value")) else {}),
                           "locator": {"json_pointer": f"{pointer}/awards/{position}"}})
        contracts = []
        for position, contract in enumerate(release.get("contracts") or []):
            contracts.append({"contract_id": str(contract.get("id") or position), "award_id": str(contract.get("awardID") or ""),
                              "date_signed": contract.get("dateSigned"), "status": contract.get("status"),
                              **({"value": v} if (v := _ocds_value(contract.get("value"), "contract", f"{pointer}/contracts/{position}/value")) else {}),
                              "period": contract.get("period"),
                              "amendments": [{k: a.get(k) for k in ("id", "date", "rationale", "description") if a.get(k)}
                                             for a in contract.get("amendments") or []],
                              "locator": {"json_pointer": f"{pointer}/contracts/{position}"}})
        if not awards and contracts:
            awards = [{"award_id": c["award_id"] or c["contract_id"], "lot_ids": [], "status": "active", "suppliers": [],
                       "locator": c["locator"]} for c in contracts]
        return record(provider, stage, release["id"], release["ocid"], title, source_url=url, buyer=buyer, language=language,
                      awards=awards, contracts=contracts, status={"native": ",".join(sorted(tags)), "asserted": "complete"}, **common)
    if not tender:
        _drift("OCDS tender release without tender")
    status_native = tender.get("status")
    asserted = {"planning": "planned", "planned": "planned", "active": "active", "cancelled": "cancelled",
                "unsuccessful": "unsuccessful", "complete": "complete", "withdrawn": "cancelled"}.get(status_native, "unknown")
    if "planning" in tags or asserted == "planned":
        stage = "prior-information"
    elif asserted in {"cancelled"}:
        stage = "cancellation"
    elif "tenderAmendment" in tags or tender.get("amendments"):
        stage = "corrigendum"
    else:
        stage = "contract-notice"
    fields = dict(common)
    if stage == "corrigendum":
        amendment = (tender.get("amendments") or [{}])[-1]
        fields["changes"] = {"changes_notice_id": amendment.get("amendsReleaseID") or release["ocid"],
                             "reason": amendment.get("rationale"), "description": {language: amendment.get("description") or ""}}
    method = tender.get("procurementMethod")
    fields["procedure"] = {"type": _OCDS_METHOD.get(method, "unknown"),
                           **({"native": " / ".join(x for x in (method, tender.get("procurementMethodDetails")) if x)} if method else {}),
                           "locator": {"json_pointer": pointer + "/tender/procurementMethod"}}
    if (v := _ocds_value(tender.get("value"), "estimated", pointer + "/tender/value")):
        fields["estimated_value"] = v
    lots, deadlines = [], []
    for position, lot in enumerate(tender.get("lots") or []):
        entry = {"lot_id": str(lot.get("id")), "title": lot.get("title")}
        if (v := _ocds_value(lot.get("value"), "estimated", f"{pointer}/tender/lots/{position}/value")):
            entry["estimated_value"] = v
        if lot.get("status"):
            entry["status"] = {"active": "active", "cancelled": "cancelled", "unsuccessful": "unsuccessful",
                               "complete": "complete"}.get(lot["status"], "unknown")
        lots.append({k: v for k, v in entry.items() if v is not None})
    fields["lots"] = lots
    for kind, key in (("submission", "tenderPeriod"), ("clarification", "enquiryPeriod")):
        end = (tender.get(key) or {}).get("endDate")
        if end:
            deadlines.append(deadline(kind, end, locator={"json_pointer": f"{pointer}/tender/{key}/endDate"}, original=end))
    if stage != "cancellation":
        fields["deadlines"] = deadlines
    requirements = []
    lot_ids = [lot["lot_id"] for lot in lots]
    for position, criterion in enumerate((tender.get("selectionCriteria") or {}).get("criteria") or []):
        text = criterion.get("description")
        if not text:
            _drift("selection criterion without description")
        applies = [str(x) for x in criterion.get("relatedLots") or criterion.get("appliesTo") or [] if str(x) in lot_ids] or None
        requirements.append(_criterion(provider, f"criterion:{position + 1}", _OCDS_CRITERION.get(criterion.get("type"), "other"),
                                       text, lot_ids=applies, language=language,
                                       locator={"json_pointer": f"{pointer}/tender/selectionCriteria/criteria/{position}", "quote": text[:400]}))
    for position, ground in enumerate((tender.get("exclusionGrounds") or {}).get("grounds") or []):
        requirements.append({**exclusion_requirement(provider, ground.get("code") or f"ground-{position}", ground.get("description") or ground.get("code"),
                                                     locator={"json_pointer": f"{pointer}/tender/exclusionGrounds/grounds/{position}"}, index=position),
                             "language": language})
    if tender.get("submissionMethodDetails"):
        requirements.append({"requirement_id": f"{provider}:submission-route", "category": "submission-route",
                             "text": tender["submissionMethodDetails"], "hard": True,
                             "locator": {"json_pointer": pointer + "/tender/submissionMethodDetails"}})
    fields["requirements"] = requirements
    fields["documents"] = [{"url": d["url"], "title": d.get("title") or "Tender document",
                            "kind": {"tenderNotice": "procurement-documents", "technicalSpecifications": "specification",
                                     "contractDraft": "contract-terms", "clarifications": "clarification"}.get(d.get("documentType"), "other")}
                           for d in tender.get("documents") or [] if isinstance(d, dict) and str(d.get("url", "")).startswith("https://")]
    fields["status"] = {"native": status_native, "asserted": asserted, "locator": {"json_pointer": pointer + "/tender/status"}}
    delivery = [a for item in tender.get("items") or [] for a in item.get("deliveryAddresses") or []]
    regions = sorted({r for a in delivery for r in ([a.get("region")] if a.get("region") else [])})
    if regions or buyer.get("country"):
        fields["place_of_performance"] = {"countries": [buyer["country"]] if buyer.get("country") else [], "nuts": regions}
    return record(provider, stage, release["id"], release["ocid"], title, source_url=url, buyer=buyer, language=language, **fields)


def parse_ocds_package(payload, provider):
    if not isinstance(payload, dict) or not isinstance(payload.get("releases"), list):
        _drift("OCDS release package lacks releases[]")
    if payload.get("version") not in ("1.1", None):
        _drift(f"unsupported OCDS version {payload.get('version')}")
    records = []
    for index, release in enumerate(payload["releases"]):
        try:
            records.append(parse_ocds_release(release, provider, index=index))
        except ProcurementRecordError as exc:
            raise SourcePackError("mapping_failed", f"release {release.get('id')}: {exc}") from exc
    return {"records": records, "next": (payload.get("links") or {}).get("next")}


# ------------------------------------------------------------------- SAM.gov


def parse_sam_opportunity(item, *, index=0):
    if not isinstance(item, dict) or not item.get("noticeId") or not item.get("title") or not item.get("type"):
        _drift("SAM opportunity lacks noticeId, title or type")
    pointer = ""  # relative to the opportunity object
    stage = _SAM_STAGE.get(item["type"], "unmapped")
    if stage == "unmapped":
        _drift(f"unmapped SAM notice type {item['type']}")
    if stage is None:
        return None
    department = item.get("fullParentPathName") or item.get("department") or "(unstated agency)"
    office = (department.split(".")[-1] if "." in department else department).strip()
    buyer = party("buyer", office, country="US", identifiers=[{"scheme": "other", "id": str(item["fullParentPathCode"]),
                                                              "native_scheme": "SAM fullParentPathCode"}] if item.get("fullParentPathCode") else [],
                  native_id=department)
    procedure_id = item.get("solicitationNumber") or item["noticeId"]
    url = item.get("uiLink") if str(item.get("uiLink", "")).startswith("https://sam.gov/") else f"https://sam.gov/opp/{item['noticeId']}/view"
    classifications = []
    if item.get("naicsCode"):
        classifications.append({"scheme": "NAICS", "code": str(item["naicsCode"]), "primary": True, "version": "NAICS 2022"})
    if item.get("classificationCode"):
        classifications.append({"scheme": "PSC", "code": str(item["classificationCode"]), "primary": False})
    fields = {"published": (item.get("postedDate") or "")[:10] or None, "classifications": classifications,
              "notice_version": None, "references": []}
    fields = {k: v for k, v in fields.items() if v is not None}
    if stage == "award":
        award = item.get("award") or {}
        awardee = award.get("awardee") or {}
        suppliers = [party("supplier", awardee["name"], country="US",
                           identifiers=[{"scheme": "UEI", "id": awardee["ueiSAM"]}] if awardee.get("ueiSAM") else [])] if awardee.get("name") else []
        awards = [{"award_id": str(award.get("number") or item["noticeId"]), "lot_ids": [], "date": (award.get("date") or "")[:10] or None,
                   "status": "active", "suppliers": suppliers,
                   **({"awarded_value": value(decimal_text(award["amount"]), "USD", "awarded", vat="unknown",
                                              native="SAM award.amount (US federal: no VAT basis stated)",
                                              locator={"json_pointer": pointer + "/award/amount"})} if award.get("amount") not in (None, "") else {}),
                   "locator": {"json_pointer": pointer + "/award"}}]
        return record("sam-gov", "award", item["noticeId"], procedure_id, item["title"], source_url=url, buyer=buyer,
                      awards=awards, status={"native": item["type"], "asserted": "complete"}, **fields)
    fields["procedure"] = {"type": "small-business-set-aside" if item.get("typeOfSetAside") in {"SBA", "SBP"} else "unknown",
                           "native": item.get("baseType") or item["type"], "locator": {"json_pointer": pointer + "/type"}}
    deadlines = []
    if item.get("responseDeadLine"):
        deadlines.append(deadline("submission", item["responseDeadLine"], locator={"json_pointer": pointer + "/responseDeadLine"},
                                  original=item["responseDeadLine"]))
    fields["deadlines"] = deadlines
    requirements = []
    if item.get("typeOfSetAside"):
        statuses = _SAM_SET_ASIDE.get(item["typeOfSetAside"])
        text = item.get("typeOfSetAsideDescription") or item["typeOfSetAside"]
        fields["set_aside"] = {"code": item["typeOfSetAside"], "description": text, "statuses": statuses or [],
                               "locator": {"json_pointer": pointer + "/typeOfSetAside"}}
        requirements.append({"requirement_id": "sam-gov:set-aside", "category": "set-aside", "text": text, "hard": True,
                             "native_code": item["typeOfSetAside"], "locator": {"json_pointer": pointer + "/typeOfSetAsideDescription", "quote": text},
                             **({"machine_rule": {"fact": "supplier.set_aside_statuses", "op": "intersects", "value": statuses}} if statuses else {})})
    fields["requirements"] = requirements
    place = item.get("placeOfPerformance") or {}
    where = ", ".join(str((place.get(k) or {}).get("name")) for k in ("city", "state") if (place.get(k) or {}).get("name"))
    fields["place_of_performance"] = {"countries": ["US"], **({"text": where} if where else {})}
    documents = [{"url": link, "title": "Attachment", "kind": "procurement-documents"}
                 for link in item.get("resourceLinks") or [] if str(link).startswith("https://")]
    if str(item.get("description", "")).startswith("https://api.sam.gov/"):
        documents.append({"url": item["description"], "title": "Notice description (requires API key)", "kind": "specification"})
    fields["documents"] = documents
    active = item.get("active")
    asserted = "planned" if stage == "prior-information" else "active" if active == "Yes" else "unknown"
    fields["status"] = {"native": f"active={active}", "asserted": asserted, "locator": {"json_pointer": pointer + "/active"}}
    return record("sam-gov", stage, item["noticeId"], procedure_id, item["title"], source_url=url, buyer=buyer, **fields)


def parse_sam_search(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("opportunitiesData"), list) or "totalRecords" not in payload:
        _drift("SAM response lacks opportunitiesData[] or totalRecords")
    records = []
    for index, item in enumerate(payload["opportunitiesData"]):
        try:
            mapped = parse_sam_opportunity(item, index=index)
        except ProcurementRecordError as exc:
            raise SourcePackError("mapping_failed", f"notice {item.get('noticeId')}: {exc}") from exc
        if mapped is not None:
            records.append(mapped)
    return {"records": records, "total": int(payload["totalRecords"]), "offset": int(payload.get("offset") or 0),
            "returned": len(payload["opportunitiesData"])}


# ---------------------------------------------------------- runtime adapters


def http_request(*, url, params, headers, timeout, body=None, method="GET", max_bytes=20_000_000):
    """GET/POST over HTTPS; the environment proxy settings apply, redirects are refused."""
    query = urllib.parse.urlencode(params or {})
    target = url + ("?" + query if query else "")
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(target, data=data, headers=dict(headers), method=method)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, response_headers, newurl):
            raise SourcePackError("network_policy", "procurement sources are read without following redirects")

    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            content = response.read(max_bytes + 1)
            if len(content) > max_bytes:
                raise SourcePackError("response_too_large", "source response exceeds its byte limit")
            return {"status": response.status, "headers": dict(response.headers), "content": content}
    except urllib.error.HTTPError as exc:
        try:
            return {"status": exc.code, "headers": dict(exc.headers or {}), "content": b""}
        finally:
            exc.close()
    except TimeoutError as exc:
        raise SourcePackError("source_timeout", "source request exceeded its transport timeout") from exc
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            raise SourcePackError("source_timeout", "source request exceeded its transport timeout") from exc
        detail = str(reason)
        if "Tunnel connection failed" in detail:
            # The environment's egress proxy refused CONNECT to the provider host.
            raise SourcePackError("source_unavailable", "egress to the provider host was refused by the network proxy "
                                  f"(CONNECT {detail.rsplit(' ', 1)[-1][:40]})", transport_detail=detail[:200]) from exc
        raise SourcePackError("source_unavailable", f"source transport is unavailable ({type(reason).__name__}: {detail[:200]})",
                              transport_detail=detail[:200]) from exc


class _ProcurementAdapter:
    accepts_transport = True
    provider = None

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        self.source = json.loads(json.dumps(source))
        self.selection = dict(self.source.get("procurement") or {})
        self.provider = self._provider()
        endpoint = urllib.parse.urlsplit(self.source["endpoint"])
        if endpoint.hostname not in PROVIDER_HOSTS[self.provider]:
            raise SourcePackError("network_policy", f"{self.provider} sources use {sorted(PROVIDER_HOSTS[self.provider])}")
        self.execution = "network" if transport is None else "injected"
        self.transport = transport or (lambda **kw: http_request(max_bytes=int(source["budgets"]["max_bytes"]), **kw))
        self.secret = secret
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]), "source_hash": source["source_hash"],
            "mapping": source["mapping"], "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "procurement": {"provider": self.provider, "selection": {k: v for k, v in self.selection.items() if k != "namespace"}},
        }

    def _provider(self):
        provider = self.selection.get("provider") or CONNECTOR_PROVIDERS[self.source["connector"]][0]
        if provider not in CONNECTOR_PROVIDERS[self.source["connector"]]:
            raise SourcePackError("invalid_mapping", f"{self.source['connector']} serves {CONNECTOR_PROVIDERS[self.source['connector']]}")
        return provider

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _scope(self):
        return _digest({"endpoint": self.source["endpoint"], "selection": self.selection})

    def _check(self, request):
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"} or dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "procurement runs use the pinned selection")

    def _cursor(self, cursor):
        state = {} if cursor is None else json.loads(cursor)
        if cursor is not None and state.get("scope") != self._scope():
            raise SourcePackError("cursor_drift", "cursor belongs to a different selection")
        return state

    def _send(self, **kwargs):
        from src.ingestion.source_pack_runtime import _retry_after_ms

        response = self.transport(timeout=int(self.definition["limits"]["timeout_ms"]) / 1000, **kwargs)
        status = int(response.get("status", 200))
        headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "source response exceeds its byte limit")
        if status == 429:
            raise SourcePackError("rate_limited", f"{self.provider} rate limit reached", retry_after_ms=_retry_after_ms(headers.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed",
                                  f"{self.provider} refused the request (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"{self.provider} returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("permanent_source_failure", f"{self.provider} returned HTTP {status}")
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise SourcePackError("schema_drift", f"{self.provider} did not return JSON") from exc
        return payload, raw, status, headers

    def _page(self, parsed, raw, status, headers, next_cursor, extra):
        from src.ingestion.source_pack_runtime import RuntimePage

        records = []
        for item in parsed["records"]:
            records.append({"id": f"{self.provider}:{item['notice_id']}", "title": item["title"], "language": item["language"],
                            "url": item["source_url"], "published_at": item.get("published"),
                            "content": "\n".join([item["title"]] + [r["text"] for r in item.get("requirements") or []]),
                            RECORD_KEY: item, "raw_sha256": _sha(raw)})
        receipt = {"status": status, "provider": self.provider, "execution": self.execution, "response_sha256": _sha(raw),
                   "records": len(records), "complete": next_cursor is None,
                   **{k: headers[k] for k in ("last-modified", "etag") if k in headers}, **extra}
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


class TedAdapter(_ProcurementAdapter):
    """TED v3 expert search with iteration tokens; POST JSON, no credential."""

    FIELDS = sorted({key.split("/")[0] for key in EFORMS_MAPPING} | {
        "BT-131(t)-Lot", "BT-13(d)-Lot", "BT-13(t)-Lot", "BT-132(d)-Lot", "BT-132(t)-Lot", "BT-27-Lot-Currency",
        "BT-720-Tender-Currency", "winner-name", "winner-identifier", "winner-country", "BT-131(d)-Lot", "BT-720-Tender"})

    def fetch_page(self, request, *, cursor):
        self._check(request)
        state = self._cursor(cursor)
        query = self.selection.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise SourcePackError("unbounded_source", "ted sources pin one expert-search query")
        limit = min(int(self.selection.get("page_size") or 50), MAX_PAGE_SIZE, int(request.get("limit") or MAX_PAGE_SIZE))
        body = {"query": query, "fields": self.FIELDS,
                "limit": limit, "scope": "ALL", "paginationMode": "ITERATION", "checkQuerySyntax": False,
                **({"iterationNextToken": state["token"]} if state.get("token") else {})}
        url = self.source["endpoint"].rstrip("/") + "/notices/search"
        payload, raw, status, headers = self._send(url=url, params={}, method="POST", body=body,
                                                   headers={"Accept": "application/json", "Content-Type": "application/json"})
        parsed = parse_ted_search(payload)
        next_cursor = json.dumps({"scope": self._scope(), "token": parsed["next"]}, sort_keys=True) \
            if parsed["next"] and parsed["records"] else None
        return self._page(parsed, raw, status, headers, next_cursor, {"total": parsed["total"], "request": {"url": url, "query": query}})


class OcdsAdapter(_ProcurementAdapter):
    """UK OCDS release packages; ``links.next`` must stay on the declared host."""

    def fetch_page(self, request, *, cursor):
        self._check(request)
        state = self._cursor(cursor)
        host = urllib.parse.urlsplit(self.source["endpoint"]).hostname
        if state.get("next"):
            target = urllib.parse.urlsplit(state["next"])
            if target.scheme != "https" or target.hostname != host:
                raise SourcePackError("network_policy", "OCDS pagination links stay on the declared host")
            url, params = state["next"], {}
        else:
            url = self.source["endpoint"]
            params = {k: str(v) for k, v in dict(self.selection.get("params") or {}).items()}
            if not params:
                raise SourcePackError("unbounded_source", "ocds sources pin a bounded date window/stage selection")
        payload, raw, status, headers = self._send(url=url, params=params, method="GET", headers={"Accept": "application/json"})
        parsed = parse_ocds_package(payload, self.provider)
        next_cursor = json.dumps({"scope": self._scope(), "next": parsed["next"]}, sort_keys=True) if parsed["next"] else None
        return self._page(parsed, raw, status, headers, next_cursor, {"request": {"url": url, "params": params}})


class SamAdapter(_ProcurementAdapter):
    """SAM.gov Get Opportunities v2; the API key comes from the secret reference only."""

    def fetch_page(self, request, *, cursor):
        self._check(request)
        if not self.secret:
            raise SourcePackError("credential_missing", "SAM.gov requires the NOESIS_SAM_API_KEY secret")
        state = self._cursor(cursor)
        params = {k: str(v) for k, v in dict(self.selection.get("params") or {}).items()}
        if not params.get("postedFrom") or not params.get("postedTo"):
            raise SourcePackError("unbounded_source", "sam-gov sources pin a postedFrom/postedTo window")
        limit = min(int(self.selection.get("page_size") or 100), 1000, int(request.get("limit") or 1000))
        offset = int(state.get("offset", 0))
        params.update(limit=str(limit), offset=str(offset))
        url = self.source["endpoint"]
        payload, raw, status, headers = self._send(url=url, params={**params, "api_key": self.secret}, method="GET",
                                                   headers={"Accept": "application/json"})
        parsed = parse_sam_search(payload)
        following = offset + parsed["returned"]
        next_cursor = json.dumps({"scope": self._scope(), "offset": following}, sort_keys=True) \
            if parsed["returned"] and following < parsed["total"] else None
        # The request is recorded without the key.
        return self._page(parsed, raw, status, headers, next_cursor, {"total": parsed["total"], "request": {"url": url, "params": params}})


ADAPTERS = {"ted": TedAdapter, "ocds": OcdsAdapter, "sam-gov": SamAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Serve authored native pages; each page names the request it answers.

    ``request`` keys: ``token`` (TED iterationNextToken, null for the first
    page), ``url`` (OCDS page URL without query) plus ``params``, or ``offset``
    (SAM). Unmatched requests answer HTTP 404.
    """

    def matches(page, url, params, body):
        wanted = page["request"]
        if "token" in wanted:
            return (body or {}).get("iterationNextToken") == wanted["token"]
        if "offset" in wanted:
            return str(params.get("offset")) == str(wanted["offset"]) and "api_key" in params
        target = urllib.parse.urlsplit(url)
        base = f"{target.scheme}://{target.netloc}{target.path}"
        query = dict(urllib.parse.parse_qsl(target.query)) or dict(params)
        return base == wanted["url"] and all(str(query.get(k)) == str(v) for k, v in (wanted.get("params") or {}).items())

    def transport(*, url, params, headers, timeout, body=None, method="GET"):
        del headers, timeout, method
        page = next((p for p in pages if matches(p, url, params or {}, body)), None)
        if page is None:
            return {"status": 404, "headers": {}, "content": b""}
        content = page.get("body")
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": json.dumps(content).encode() if isinstance(content, (dict, list)) else (content or "").encode()}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = ADAPTERS[source["connector"]](source, transport=fixture_transport(list(fixture["native_pages"])),
                                            secret=FIXTURE_SECRET)
    records: list[dict[str, Any]] = []
    cursor = None
    for _ in range(int(source["budgets"]["max_pages"])):
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {}}, cursor=cursor)
        records.extend(dict(item) for item in page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records
