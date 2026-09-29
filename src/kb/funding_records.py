"""Versioned funding programme, call, deadline and financial-term records.

``noesis-funding-record-v1`` is the provider-neutral shape every funding
acquisition adapter emits. It deliberately keeps apart things that funding
sources blur together:

* a *programme* (a standing instrument such as EXIST-Gründungsstipendium),
* a *call* or *round* (a time-bounded application window for a programme),
* a *directory entry* (an aggregator listing, e.g. Förderdatenbank, which is
  never evidence of an open application window), and
* an *award* (a grant that was made; contextual history, never an
  opportunity).

Absent values stay ``None`` and are listed in ``unknowns``; nothing is
defaulted. Money is a decimal string plus an explicit ISO currency and a
*basis*, so a programme or topic budget can never be read as the size of one
applicant's award. The existing ``config/extraction_schemas/eu-funding.json``
models award metadata only; :func:`award_from_extraction` maps it onto the
``award`` record kind rather than onto an opportunity.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

CONTRACT = "noesis-funding-record-v1"
READ_SCOPE = "knowledge:funding:read"
WRITE_SCOPE = "knowledge:funding:write"
PROVIDERS = ("nlnet", "eu-ft", "foerderdatenbank", "exist")
RECORD_KINDS = ("programme", "call", "directory_entry", "award")
AUTHORITY_KINDS = ("funder", "administering-body", "directory")
# Cash-equivalent kinds may be compared with a cash funding need. Loans,
# guarantees, equity and credits are support, but never counted as cash grants.
INSTRUMENT_KINDS = (
    "grant", "stipend", "prize", "loan", "guarantee", "equity", "credit",
    "in-kind", "procurement", "unknown",
)
CASH_EQUIVALENT = frozenset({"grant", "stipend", "prize"})
STATUS_VALUES = ("open", "forthcoming", "rolling", "closed", "unknown")
AWARD_BASES = ("per-project", "per-applicant", "per-person-month", "unknown")
BUDGET_BASES = ("call-total", "topic-total", "programme-total", "unknown")
DEADLINE_KINDS = ("opening", "submission", "cut-off")
REIMBURSEMENT = ("advance", "milestone", "arrears", "unknown")
REQUIREMENT_CATEGORIES = (
    "applicant-type", "residence", "establishment", "affiliation",
    "consortium", "project-stage", "licensing", "co-financing", "theme",
    "document", "submission-route", "duration", "other",
)
DOCUMENT_KINDS = ("call-text", "guideline", "template", "faq", "form", "other")
REFERENCE_KINDS = ("administering-body", "authoritative-call", "programme")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})$")
_FIELDS = {
    "contract", "record_kind", "provider", "provider_id", "round_id", "title",
    "language", "source_url", "authority", "funder", "programme", "instrument",
    "financial_terms", "status", "deadlines", "requirements", "themes",
    "documents", "references", "sections", "unknowns",
}
# Top-level keys that would indicate private applicant state leaking into a
# public source record.
PRIVATE_MARKERS = ("profile", "applicant_fact", "owner")


class FundingRecordError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _fail(message, code="invalid_funding_record"):
    raise FundingRecordError(code, message)


def _text(value, field, *, optional=False, limit=20000):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _fail(f"{field} must be nonempty text")
    return value


def _enum(value, allowed, field):
    if value not in allowed:
        _fail(f"{field} must be one of {', '.join(allowed)}")
    return value


def money(value, field="amount"):
    """Validate a decimal amount string; floats are rejected to keep exactness."""
    if value is None:
        return None
    if not isinstance(value, str):
        _fail(f"{field} must be a decimal string, not a float or integer")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        _fail(f"{field} is not a decimal amount")
    if not amount.is_finite() or amount < 0:
        _fail(f"{field} must be a finite nonnegative amount")
    return value


def _locator(value, field):
    if value is None:
        return None
    if not isinstance(value, dict) or not value or set(value) - {
        "section", "selector", "json_pointer", "page", "paragraph", "quote", "url",
    }:
        _fail(f"{field} locator uses section/selector/json_pointer/page/paragraph/quote/url")
    return value


def _financial_terms(terms):
    if not isinstance(terms, dict):
        _fail("financial_terms must be an object")
    allowed = {
        "currency", "award_range", "programme_budget", "funding_rate",
        "co_financing", "eligible_costs", "reimbursement", "repayable", "notes",
    }
    if set(terms) - allowed:
        _fail("unsupported financial term")
    currency = terms.get("currency")
    if currency is not None and not _CURRENCY.fullmatch(str(currency)):
        _fail("currency must be an ISO-4217 code")
    award = terms.get("award_range")
    if award is not None:
        if not isinstance(award, dict) or set(award) - {"min", "max", "basis", "locator"}:
            _fail("award_range requires min/max/basis/locator")
        money(award.get("min"), "award_range.min")
        money(award.get("max"), "award_range.max")
        _enum(award.get("basis"), AWARD_BASES, "award_range.basis")
        _locator(award.get("locator"), "award_range")
        if award.get("min") is not None and award.get("max") is not None and Decimal(award["min"]) > Decimal(award["max"]):
            _fail("award_range.min exceeds max")
        if currency is None and (award.get("min") or award.get("max")):
            _fail("amounts require an explicit currency")
    budget = terms.get("programme_budget")
    if budget is not None:
        if not isinstance(budget, dict) or set(budget) - {"amount", "basis", "expected_awards", "locator"}:
            _fail("programme_budget requires amount/basis/expected_awards/locator")
        money(budget.get("amount"), "programme_budget.amount")
        _enum(budget.get("basis"), BUDGET_BASES, "programme_budget.basis")
        if budget.get("expected_awards") is not None and (
            type(budget["expected_awards"]) is not int or budget["expected_awards"] < 1
        ):
            _fail("expected_awards must be a positive integer")
        if currency is None and budget.get("amount"):
            _fail("amounts require an explicit currency")
    rate = terms.get("funding_rate")
    if rate is not None:
        if not isinstance(rate, dict) or set(rate) - {"max_percent", "min_percent", "locator"}:
            _fail("funding_rate uses min_percent/max_percent/locator")
        for key in ("min_percent", "max_percent"):
            if rate.get(key) is not None:
                money(rate[key], "funding_rate." + key)
                if Decimal(rate[key]) > 100:
                    _fail("funding rates are percentages up to 100")
    co = terms.get("co_financing")
    if co is not None:
        if not isinstance(co, dict) or set(co) - {"required", "min_percent", "locator"}:
            _fail("co_financing uses required/min_percent/locator")
        if co.get("required") not in (True, False, None):
            _fail("co_financing.required is true, false or unknown (null)")
        if co.get("min_percent") is not None:
            money(co["min_percent"], "co_financing.min_percent")
    costs = terms.get("eligible_costs")
    if costs is not None and (
        not isinstance(costs, list) or any(not isinstance(c, str) or not c for c in costs)
    ):
        _fail("eligible_costs is a list of cost categories or null when unknown")
    if terms.get("reimbursement") is not None:
        _enum(terms["reimbursement"], REIMBURSEMENT, "reimbursement")
    if terms.get("repayable") not in (True, False, None):
        _fail("repayable is true, false or unknown (null)")
    return terms


def _deadline(value):
    if not isinstance(value, dict) or set(value) - {
        "stage", "kind", "text", "timezone", "instant", "date", "locator",
    }:
        _fail("deadline uses stage/kind/text/timezone/instant/date/locator")
    _enum(value.get("kind"), DEADLINE_KINDS, "deadline.kind")
    _text(value.get("text"), "deadline.text (original source text)")
    if value.get("instant") is not None:
        if not _INSTANT.fullmatch(str(value["instant"])):
            _fail("deadline.instant must be an ISO instant with an explicit offset")
        if not value.get("timezone"):
            _fail("a normalized deadline instant requires the source timezone")
    if value.get("date") is not None and not _DATE.fullmatch(str(value["date"])):
        _fail("deadline.date must be YYYY-MM-DD")
    _locator(value.get("locator"), "deadline")
    return value


def _requirement(value):
    if not isinstance(value, dict) or set(value) - {
        "requirement_id", "category", "text", "hard", "locator", "machine_rule",
    }:
        _fail("requirement uses requirement_id/category/text/hard/locator/machine_rule")
    _text(value.get("requirement_id"), "requirement_id", limit=200)
    _enum(value.get("category"), REQUIREMENT_CATEGORIES, "requirement.category")
    _text(value.get("text"), "requirement.text")
    if value.get("hard") not in (True, False, None):
        _fail("requirement.hard is true, false or unknown (null)")
    if not value.get("locator"):
        _fail("every requirement needs an exact source locator", "missing_locator")
    _locator(value["locator"], "requirement")
    if value.get("machine_rule") is not None:
        from src.kb.funding_eligibility import validate_rule

        validate_rule(value["machine_rule"])
    return value


def compute_unknowns(record):
    """List explicitly unknown fields rather than inventing defaults."""
    unknowns = []
    terms = record.get("financial_terms") or {}
    kinds = (record.get("instrument") or {}).get("kinds") or ["unknown"]
    if "unknown" in kinds:
        unknowns.append("instrument.kinds")
    if record["record_kind"] in {"programme", "call"}:
        if not terms.get("currency"):
            unknowns.append("financial_terms.currency")
        if not terms.get("award_range") or (
            terms["award_range"].get("min") is None and terms["award_range"].get("max") is None
        ):
            unknowns.append("financial_terms.award_range")
        if terms.get("eligible_costs") is None:
            unknowns.append("financial_terms.eligible_costs")
        if (terms.get("co_financing") or {}).get("required") is None:
            unknowns.append("financial_terms.co_financing")
        if terms.get("reimbursement") in (None, "unknown"):
            unknowns.append("financial_terms.reimbursement")
    if record["record_kind"] == "call":
        if (record.get("status") or {}).get("asserted", "unknown") == "unknown":
            unknowns.append("status")
        if not record.get("deadlines"):
            unknowns.append("deadlines")
        elif any(d.get("instant") is None for d in record["deadlines"] if d["kind"] != "opening"):
            unknowns.append("deadlines.instant")
    return sorted(set(unknowns))


def validate_record(record):
    """Validate and return a canonical copy with ``unknowns`` recomputed."""
    if not isinstance(record, dict):
        _fail("funding record must be an object")
    if any(marker in key for key in record for marker in PRIVATE_MARKERS):
        _fail("private applicant state cannot enter a public source record", "private_leak")
    if set(record) - _FIELDS:
        _fail("unsupported funding record field: " + ", ".join(sorted(set(record) - _FIELDS)))
    if record.get("contract") != CONTRACT:
        _fail("unsupported funding record contract", "schema_drift")
    kind = _enum(record.get("record_kind"), RECORD_KINDS, "record_kind")
    _enum(record.get("provider"), PROVIDERS, "provider")
    _text(record.get("provider_id"), "provider_id (source-native identifier)", limit=500)
    _text(record.get("round_id"), "round_id", optional=True, limit=500)
    _text(record.get("title"), "title", limit=2000)
    _text(record.get("language"), "language", limit=20)
    _text(record.get("source_url"), "source_url", limit=4000)
    if not str(record["source_url"]).startswith("https://"):
        _fail("source_url must be an exact HTTPS source locator")
    authority = record.get("authority")
    if not isinstance(authority, dict) or set(authority) != {"kind", "name"}:
        _fail("authority requires kind and name")
    _enum(authority["kind"], AUTHORITY_KINDS, "authority.kind")
    if kind == "directory_entry" and authority["kind"] != "directory":
        _fail("directory entries carry directory authority, not funder authority")
    instrument = record.get("instrument") or {}
    if not isinstance(instrument, dict) or set(instrument) - {"kinds", "native_label", "locator"}:
        _fail("instrument uses kinds/native_label/locator")
    kinds = instrument.get("kinds") or ["unknown"]
    for value in kinds:
        _enum(value, INSTRUMENT_KINDS, "instrument.kinds")
    _financial_terms(record.get("financial_terms") or {})
    status = record.get("status")
    deadlines = record.get("deadlines") or []
    if kind in {"award", "programme", "directory_entry"} and deadlines:
        _fail(f"{kind} records do not carry application deadlines; model a call", "not_an_opportunity")
    if kind == "award" and status is not None:
        _fail("award history is not an opportunity and has no open/closed status", "not_an_opportunity")
    if kind == "directory_entry" and status and status.get("asserted") not in (None, "unknown"):
        _fail("a directory listing cannot assert an open application window", "not_an_opportunity")
    if status is not None:
        if not isinstance(status, dict) or set(status) - {"native", "asserted", "locator"}:
            _fail("status uses native/asserted/locator")
        _enum(status.get("asserted", "unknown"), STATUS_VALUES, "status.asserted")
    for deadline in deadlines:
        _deadline(deadline)
    seen = set()
    for requirement in record.get("requirements") or []:
        _requirement(requirement)
        if requirement["requirement_id"] in seen:
            _fail("duplicate requirement_id")
        seen.add(requirement["requirement_id"])
    for document in record.get("documents") or []:
        if not isinstance(document, dict) or set(document) - {"url", "title", "kind", "locator", "sha256"}:
            _fail("documents use url/title/kind/locator/sha256")
        _enum(document.get("kind"), DOCUMENT_KINDS, "document.kind")
        _text(document.get("url"), "document.url")
    for reference in record.get("references") or []:
        if not isinstance(reference, dict) or set(reference) - {"kind", "url", "provider", "provider_id", "name"}:
            _fail("references use kind/url/provider/provider_id/name")
        _enum(reference.get("kind"), REFERENCE_KINDS, "reference.kind")
    themes = record.get("themes") or []
    if not isinstance(themes, list) or any(not isinstance(t, str) or not t for t in themes):
        _fail("themes must be a list of strings")
    result = json.loads(canonical({**record, "instrument": {**instrument, "kinds": kinds}}))
    result["unknowns"] = compute_unknowns(result)
    return result


def record(provider, record_kind, provider_id, title, *, source_url, authority, language="en", **fields):
    """Build and validate a record; convenient for adapters and fixtures."""
    return validate_record({
        "contract": CONTRACT, "record_kind": record_kind, "provider": provider,
        "provider_id": provider_id, "title": " ".join(str(title).split()),
        "language": language, "source_url": source_url, "authority": authority,
        **fields,
    })


def is_cash_equivalent(record):
    kinds = set((record.get("instrument") or {}).get("kinds") or ["unknown"])
    return bool(kinds) and kinds <= CASH_EQUIVALENT


def award_from_extraction(structure, *, provider, provider_id, source_url, language="en"):
    """Map an ``eu-funding`` extraction ``award`` structure onto award history.

    The extraction schema's ``amount`` is free text including currency; it is
    retained verbatim as a note because parsing it would invent precision.
    """
    if not isinstance(structure, dict) or not structure.get("programme"):
        _fail("award extraction requires a programme")
    return record(
        provider, "award", provider_id,
        f"Award: {structure.get('beneficiary') or 'unknown beneficiary'} — {structure['programme']}",
        source_url=source_url, language=language,
        authority={"kind": "funder", "name": structure["programme"]},
        programme={"id": None, "title": structure["programme"]},
        financial_terms={"notes": "extracted award amount text: " + str(structure.get("amount") or "unknown")},
    )
