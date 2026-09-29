"""Versioned public-procurement notice, lot, party, CPV, deadline, award and contract records.

``noesis-procurement-record-v1`` is the provider-neutral shape every
procurement acquisition adapter (TED eForms, UK OCDS, SAM.gov) emits. One
record is one *notice* as the source published it. A notice has a stage:

* ``prior-information`` — planned procurement; never an open procedure;
* ``contract-notice`` — a call for competition;
* ``corrigendum`` — a change notice; it is a *revision* of the notice it
  changes (same ``procedure_id``), never a separate opportunity;
* ``cancellation`` — the procedure (or some lots) was cancelled;
* ``award`` — a contract award; history and context, never an opportunity;
* ``modification`` — a change to an awarded contract.

Values keep what sources blur: an *estimated* value is never an *awarded*
value, every amount carries an explicit ISO currency and a VAT basis
(``excluded``/``included``/``unknown``), and absent values stay ``None`` and
are listed in ``unknowns``. Deadlines keep the source's original text and
timezone; a date without a stated offset is never turned into an instant.
Buyers and suppliers keep their source names and identifiers; links to
``canonical_entities`` and LEI records are separate, reviewable identity
decisions (:mod:`src.kb.procurement_identity`) and never part of the source
record. Private supplier/buyer profile data can never enter a record.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from src.kb.funding_records import canonical, digest

CONTRACT = "noesis-procurement-record-v1"
READ_SCOPE = "knowledge:procurement:read"
WRITE_SCOPE = "knowledge:procurement:write"
REVIEW_SCOPE = "knowledge:procurement:review"
PROVIDERS = ("ted", "uk-fts", "uk-cf", "sam-gov")
STAGES = ("prior-information", "contract-notice", "corrigendum", "cancellation", "award", "modification")
OPPORTUNITY_STAGES = frozenset({"prior-information", "contract-notice", "corrigendum", "cancellation"})
HISTORY_STAGES = frozenset({"award", "modification"})
PROCEDURE_TYPES = (
    "open", "restricted", "negotiated-with-call", "negotiated-without-call", "competitive-dialogue",
    "innovation-partnership", "direct-award", "competitive-flexible", "small-business-set-aside",
    "other", "unknown",
)
STATUS_VALUES = ("planned", "active", "cancelled", "unsuccessful", "complete", "unknown")
DEADLINE_KINDS = ("submission", "clarification", "opening", "participation-request", "tender-validity")
VALUE_KINDS = ("estimated", "awarded", "contract", "framework-maximum")
VAT_BASES = ("excluded", "included", "unknown")
CLASSIFICATION_SCHEMES = ("CPV", "NAICS", "PSC", "UNSPSC")
REQUIREMENT_CATEGORIES = (
    "exclusion", "economic-financial", "technical-professional", "suitability", "certification",
    "set-aside", "threshold", "document", "submission-route", "other",
)
# Procedural requirements shape the bid checklist; they are not eligibility.
PROCEDURAL = frozenset({"document", "submission-route"})
DOCUMENT_KINDS = ("procurement-documents", "tender-form", "espd", "specification", "contract-terms", "clarification", "other")
PARTY_ROLES = ("buyer", "supplier")
IDENTIFIER_SCHEMES = ("LEI", "VAT", "national", "GB-COH", "GB-CHC", "UEI", "CAGE", "DUNS", "eu-org", "other")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_ISO2 = re.compile(r"^[A-Z]{2}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(Z|[+-]\d{2}:\d{2})$")
_CPV = re.compile(r"^\d{8}$")
_LOCATOR_KEYS = {"json_pointer", "field", "language", "quote", "url", "section"}
_FIELDS = {
    "contract", "record_kind", "provider", "notice_id", "notice_version", "procedure_id", "stage",
    "changes", "title", "titles", "language", "source_url", "published", "buyer", "procedure",
    "classifications", "estimated_value", "lots", "deadlines", "requirements", "documents",
    "place_of_performance", "set_aside", "awards", "contracts", "status", "references", "unknowns",
}
# Keys that would indicate private supplier/buyer profile state leaking into a
# public source record.
PRIVATE_MARKERS = ("profile", "owner", "self_declaration", "applicant_fact")


class ProcurementRecordError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _fail(message, code="invalid_procurement_record"):
    raise ProcurementRecordError(code, message)


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


def amount(value, field="amount"):
    """A decimal string; floats are rejected so no precision is invented."""
    if value is None:
        return None
    if not isinstance(value, str):
        _fail(f"{field} must be a decimal string, not a float or integer")
    try:
        number = Decimal(value)
    except InvalidOperation:
        _fail(f"{field} is not a decimal amount")
    if not number.is_finite() or number < 0:
        _fail(f"{field} must be a finite nonnegative amount")
    return value


def decimal_text(value):
    """Convert a JSON number or numeric string to an exact decimal string (no float rounding)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        _fail("boolean is not an amount")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        _fail(f"{value!r} is not an amount")
    return format(number.normalize(), "f") if number == number.to_integral_value() else format(number, "f")


def _locator(value, field):
    if value is None:
        return None
    if not isinstance(value, dict) or not value or set(value) - _LOCATOR_KEYS:
        _fail(f"{field} locator uses {'/'.join(sorted(_LOCATOR_KEYS))}")
    return value


def cpv_code(value):
    """Normalise a CPV code to eight digits; the check digit (``-5``) is dropped, the native value kept."""
    match = re.fullmatch(r"\s*(\d{8})(?:-\d)?\s*", str(value or ""))
    return match.group(1) if match else None


def cpv_relation(interest, code):
    """How a CPV interest relates to a notice code.

    ``covers``: the code lies inside the interest's hierarchy branch (division
    ``72000000`` covers ``72212000``); ``same-division``: only the first two
    digits agree; ``none`` otherwise.
    """
    interest, code = cpv_code(interest), cpv_code(code)
    if not interest or not code:
        return "none"
    prefix = interest.rstrip("0")
    prefix = prefix if len(prefix) >= 2 else interest[:2]
    if code.startswith(prefix):
        return "covers"
    return "same-division" if code[:2] == interest[:2] else "none"


def value(amount_text, currency, kind, *, vat="unknown", locator=None, native=None):
    """Build a value object; the kind keeps estimated and awarded values apart."""
    return {"amount": amount_text, "currency": currency, "kind": kind, "vat": vat,
            **({"locator": locator} if locator else {}), **({"native": native} if native else {})}


def _value(item, field, *, kinds):
    if item is None:
        return None
    if not isinstance(item, dict) or set(item) - {"amount", "currency", "kind", "vat", "locator", "native"}:
        _fail(f"{field} uses amount/currency/kind/vat/locator/native")
    _enum(item.get("kind"), kinds, f"{field}.kind")
    _enum(item.get("vat", "unknown"), VAT_BASES, f"{field}.vat")
    amount(item.get("amount"), f"{field}.amount")
    if item.get("amount") is not None and not _CURRENCY.fullmatch(str(item.get("currency") or "")):
        _fail(f"{field} amounts require an explicit ISO-4217 currency")
    if item.get("currency") is not None and not _CURRENCY.fullmatch(str(item["currency"])):
        _fail(f"{field}.currency must be an ISO-4217 code")
    _locator(item.get("locator"), field)
    return item


def _party(item, field, role):
    if not isinstance(item, dict) or set(item) - {"role", "name", "names", "identifiers", "country", "native_id"}:
        _fail(f"{field} uses role/name/names/identifiers/country/native_id")
    if item.get("role") != role:
        _fail(f"{field}.role must be {role}")
    _text(item.get("name"), f"{field}.name (source string)", limit=2000)
    for identifier in item.get("identifiers") or []:
        if not isinstance(identifier, dict) or set(identifier) - {"scheme", "id", "native_scheme"}:
            _fail(f"{field} identifiers use scheme/id/native_scheme")
        _enum(identifier.get("scheme"), IDENTIFIER_SCHEMES, f"{field}.identifier.scheme")
        _text(identifier.get("id"), f"{field}.identifier.id", limit=200)
    if item.get("country") is not None and not _ISO2.fullmatch(str(item["country"])):
        _fail(f"{field}.country must be ISO 3166-1 alpha-2")
    if item.get("names") is not None and not (isinstance(item["names"], dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in item["names"].items())):
        _fail(f"{field}.names maps language tags to names")
    return item


def _classifications(items, field):
    if not isinstance(items, list):
        _fail(f"{field} must be a list")
    for item in items:
        if not isinstance(item, dict) or set(item) - {"scheme", "code", "version", "primary", "native", "description"}:
            _fail(f"{field} uses scheme/code/version/primary/native/description")
        _enum(item.get("scheme"), CLASSIFICATION_SCHEMES, f"{field}.scheme")
        _text(item.get("code"), f"{field}.code", limit=50)
        if item["scheme"] == "CPV" and not _CPV.fullmatch(item["code"]):
            _fail(f"{field}: CPV codes are normalised to eight digits (native value kept)")
    return items


def _deadline(item):
    if not isinstance(item, dict) or set(item) - {"kind", "text", "timezone", "instant", "date", "lot_id", "locator"}:
        _fail("deadline uses kind/text/timezone/instant/date/lot_id/locator")
    _enum(item.get("kind"), DEADLINE_KINDS, "deadline.kind")
    _text(item.get("text"), "deadline.text (original source text)")
    if item.get("instant") is not None:
        if not _INSTANT.fullmatch(str(item["instant"])):
            _fail("deadline.instant must be an ISO instant with an explicit offset")
        if not item.get("timezone"):
            _fail("a normalised deadline instant requires the source timezone/offset")
    if item.get("date") is not None and not _DATE.fullmatch(str(item["date"])):
        _fail("deadline.date must be YYYY-MM-DD")
    _locator(item.get("locator"), "deadline")
    return item


def _requirement(item, lot_ids):
    if not isinstance(item, dict) or set(item) - {
        "requirement_id", "category", "text", "language", "hard", "locator", "machine_rule", "lot_ids", "native_code",
    }:
        _fail("requirement uses requirement_id/category/text/language/hard/locator/machine_rule/lot_ids/native_code")
    _text(item.get("requirement_id"), "requirement_id", limit=200)
    _enum(item.get("category"), REQUIREMENT_CATEGORIES, "requirement.category")
    _text(item.get("text"), "requirement.text")
    if item.get("hard") not in (True, False, None):
        _fail("requirement.hard is true, false or unknown (null)")
    if not item.get("locator"):
        _fail("every requirement cites an exact notice passage", "missing_locator")
    _locator(item["locator"], "requirement")
    if item.get("lot_ids") is not None and (not isinstance(item["lot_ids"], list) or set(item["lot_ids"]) - set(lot_ids)):
        _fail("requirement.lot_ids must name lots of this notice")
    if item.get("machine_rule") is not None:
        from src.kb.funding_eligibility import validate_rule

        validate_rule(item["machine_rule"])
    return item


def _lot(item):
    if not isinstance(item, dict) or set(item) - {
        "lot_id", "title", "titles", "description", "classifications", "estimated_value", "status", "place_of_performance",
    }:
        _fail("lot uses lot_id/title/titles/description/classifications/estimated_value/status/place_of_performance")
    _text(item.get("lot_id"), "lot_id", limit=200)
    _text(item.get("title"), "lot.title", optional=True, limit=2000)
    _classifications(item.get("classifications") or [], "lot.classifications")
    _value(item.get("estimated_value"), "lot.estimated_value", kinds=("estimated", "framework-maximum"))
    if item.get("status") is not None:
        _enum(item["status"], STATUS_VALUES, "lot.status")
    return item


def _award(item, field="award"):
    if not isinstance(item, dict) or set(item) - {
        "award_id", "lot_ids", "date", "status", "suppliers", "awarded_value", "locator", "native_status",
    }:
        _fail(f"{field} uses award_id/lot_ids/date/status/suppliers/awarded_value/locator/native_status")
    _text(item.get("award_id"), f"{field}.award_id", limit=500)
    if item.get("date") is not None and not (_DATE.fullmatch(str(item["date"])) or _INSTANT.fullmatch(str(item["date"]))):
        _fail(f"{field}.date must be a date or ISO instant")
    for supplier in item.get("suppliers") or []:
        _party(supplier, f"{field}.supplier", "supplier")
    _value(item.get("awarded_value"), f"{field}.awarded_value", kinds=("awarded", "framework-maximum"))
    if item.get("status") is not None:
        _enum(item["status"], ("active", "pending", "cancelled", "unsuccessful", "unknown"), f"{field}.status")
    return item


def _contract(item):
    if not isinstance(item, dict) or set(item) - {
        "contract_id", "award_id", "date_signed", "status", "value", "period", "amendments", "locator",
    }:
        _fail("contract uses contract_id/award_id/date_signed/status/value/period/amendments/locator")
    _text(item.get("contract_id"), "contract.contract_id", limit=500)
    _value(item.get("value"), "contract.value", kinds=("contract",))
    for amendment in item.get("amendments") or []:
        if not isinstance(amendment, dict) or set(amendment) - {"id", "date", "rationale", "description"}:
            _fail("contract amendments use id/date/rationale/description")
    return item


def compute_unknowns(record):
    """List explicitly unknown fields rather than inventing defaults."""
    unknowns = []
    if record["stage"] in OPPORTUNITY_STAGES:
        if not record.get("classifications") and not any(lot.get("classifications") for lot in record.get("lots") or []):
            unknowns.append("classifications")
        if (record.get("procedure") or {}).get("type", "unknown") == "unknown":
            unknowns.append("procedure.type")
        estimated = [record.get("estimated_value")] + [lot.get("estimated_value") for lot in record.get("lots") or []]
        if not any(v and v.get("amount") is not None for v in estimated):
            unknowns.append("estimated_value")
        elif any(v and v.get("amount") is not None and v.get("vat", "unknown") == "unknown" for v in estimated):
            unknowns.append("estimated_value.vat")
        if record["stage"] != "cancellation":
            submissions = [d for d in record.get("deadlines") or [] if d["kind"] == "submission"]
            if not submissions and record["stage"] != "prior-information":
                unknowns.append("deadlines.submission")
            elif any(d.get("instant") is None for d in submissions):
                unknowns.append("deadlines.submission.instant")
        if (record.get("status") or {}).get("asserted", "unknown") == "unknown":
            unknowns.append("status")
    if record["stage"] in HISTORY_STAGES:
        for award in record.get("awards") or []:
            if not award.get("awarded_value") or award["awarded_value"].get("amount") is None:
                unknowns.append("awards.awarded_value")
            elif award["awarded_value"].get("vat", "unknown") == "unknown":
                unknowns.append("awards.awarded_value.vat")
            if not award.get("suppliers"):
                unknowns.append("awards.suppliers")
    buyer = record.get("buyer") or {}
    if not buyer.get("identifiers"):
        unknowns.append("buyer.identifiers")
    return sorted(set(unknowns))


def validate_record(record):
    """Validate and return a canonical copy with ``unknowns`` recomputed."""
    if not isinstance(record, dict):
        _fail("procurement record must be an object")
    if any(marker in key for key in record for marker in PRIVATE_MARKERS):
        _fail("private profile state cannot enter a public source record", "private_leak")
    if set(record) - _FIELDS:
        _fail("unsupported procurement record field: " + ", ".join(sorted(set(record) - _FIELDS)))
    if record.get("contract") != CONTRACT:
        _fail("unsupported procurement record contract", "schema_drift")
    if record.get("record_kind") != "notice":
        _fail("record_kind must be notice")
    _enum(record.get("provider"), PROVIDERS, "provider")
    _text(record.get("notice_id"), "notice_id (source-native notice identifier)", limit=500)
    _text(record.get("procedure_id"), "procedure_id (cross-stage key: eForms BT-04, OCDS ocid, SAM solicitation)", limit=500)
    stage = _enum(record.get("stage"), STAGES, "stage")
    _text(record.get("title"), "title", limit=4000)
    _text(record.get("language"), "language", limit=20)
    _text(record.get("source_url"), "source_url", limit=4000)
    if not str(record["source_url"]).startswith("https://"):
        _fail("source_url must be an exact HTTPS source locator")
    if record.get("titles") is not None and not (isinstance(record["titles"], dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in record["titles"].items())):
        _fail("titles maps language tags to titles")
    if record.get("published") is not None and not (_DATE.fullmatch(str(record["published"])) or _INSTANT.fullmatch(str(record["published"]))):
        _fail("published must be a date or ISO instant")
    if stage == "corrigendum":
        changes = record.get("changes")
        if not isinstance(changes, dict) or not changes.get("changes_notice_id"):
            _fail("a corrigendum names the notice it changes (changes.changes_notice_id)")
    _party(record.get("buyer"), "buyer", "buyer")
    procedure = record.get("procedure") or {"type": "unknown"}
    if not isinstance(procedure, dict) or set(procedure) - {"type", "native", "locator"}:
        _fail("procedure uses type/native/locator")
    _enum(procedure.get("type", "unknown"), PROCEDURE_TYPES, "procedure.type")
    _classifications(record.get("classifications") or [], "classifications")
    _value(record.get("estimated_value"), "estimated_value", kinds=("estimated", "framework-maximum"))
    lots = record.get("lots") or []
    lot_ids = [lot.get("lot_id") for lot in lots]
    if len(set(lot_ids)) != len(lot_ids):
        _fail("duplicate lot_id")
    for lot in lots:
        _lot(lot)
    for deadline in record.get("deadlines") or []:
        _deadline(deadline)
        if deadline.get("lot_id") is not None and deadline["lot_id"] not in lot_ids:
            _fail("deadline.lot_id must name a lot of this notice")
    seen = set()
    for requirement in record.get("requirements") or []:
        _requirement(requirement, lot_ids)
        if requirement["requirement_id"] in seen:
            _fail("duplicate requirement_id")
        seen.add(requirement["requirement_id"])
    for document in record.get("documents") or []:
        if not isinstance(document, dict) or set(document) - {"url", "title", "kind", "language", "locator"}:
            _fail("documents use url/title/kind/language/locator")
        _enum(document.get("kind"), DOCUMENT_KINDS, "document.kind")
        _text(document.get("url"), "document.url")
    awards = record.get("awards") or []
    if stage in OPPORTUNITY_STAGES and awards:
        _fail("award results are recorded from award notices, not calls for competition", "not_an_opportunity")
    if stage == "award" and not awards:
        _fail("an award notice needs at least one award entry")
    for award in awards:
        _award(award)
    for contract in record.get("contracts") or []:
        _contract(contract)
    if stage in HISTORY_STAGES and (record.get("deadlines") or []):
        _fail("award history carries no bid deadlines; it is never evidence that a procedure is open", "not_an_opportunity")
    status = record.get("status")
    if status is not None:
        if not isinstance(status, dict) or set(status) - {"native", "asserted", "locator"}:
            _fail("status uses native/asserted/locator")
        _enum(status.get("asserted", "unknown"), STATUS_VALUES, "status.asserted")
        if stage in HISTORY_STAGES and status.get("asserted") in {"active", "planned"}:
            _fail("an award or modification cannot assert an open procedure", "not_an_opportunity")
    place = record.get("place_of_performance")
    if place is not None and (not isinstance(place, dict) or set(place) - {"countries", "nuts", "text"}
                              or any(not _ISO2.fullmatch(str(c)) for c in place.get("countries") or [])):
        _fail("place_of_performance uses countries (ISO2)/nuts/text")
    set_aside = record.get("set_aside")
    if set_aside is not None and (not isinstance(set_aside, dict) or set(set_aside) - {"code", "description", "statuses", "locator"}):
        _fail("set_aside uses code/description/statuses/locator")
    for reference in record.get("references") or []:
        if not isinstance(reference, dict) or set(reference) - {"kind", "notice_id", "url"}:
            _fail("references use kind/notice_id/url")
        _enum(reference.get("kind"), ("changes", "previous-notice", "award-of", "related"), "reference.kind")
    result = json.loads(canonical({**record, "procedure": procedure}))
    result["unknowns"] = compute_unknowns(result)
    return result


def record(provider, stage, notice_id, procedure_id, title, *, source_url, buyer, language="en", **fields):
    """Build and validate one notice record; convenient for adapters and fixtures."""
    return validate_record({
        "contract": CONTRACT, "record_kind": "notice", "provider": provider, "notice_id": notice_id,
        "procedure_id": procedure_id, "stage": stage, "title": " ".join(str(title).split()),
        "language": language, "source_url": source_url, "buyer": buyer, **fields,
    })


def party(role, name, *, identifiers=None, country=None, names=None, native_id=None):
    return {"role": role, "name": " ".join(str(name).split()), "identifiers": identifiers or [],
            **({"country": country} if country else {}), **({"names": names} if names else {}),
            **({"native_id": native_id} if native_id else {})}


def lei_of(party_item):
    """The LEI a source states for a party, if any (known from the source, not inferred)."""
    return next((i["id"] for i in party_item.get("identifiers") or [] if i["scheme"] == "LEI"), None)


# ------------------------------------------------------------ schema registry

SCHEMAS = ("noesis-procurement-record-v1", "noesis-procurement-profile-v1", "noesis-procurement-shortlist-v1")
_ROOT = Path(__file__).resolve().parents[2]


def register_schemas(registry, *, principal_id, scopes):
    """Register the procurement JSON Schemas as pack-owned modules (owner ``procurement.core``)."""
    results = []
    for contract in SCHEMAS:
        content = json.loads((_ROOT / f"contracts/schemas/jsonschema/{contract}.json").read_text())
        name = contract.removeprefix("noesis-").removesuffix("-v1")
        results.append(registry.register({
            "contract": "noesis-schema-module-v1", "name": name, "kind": "schema", "semantic_version": "1.0.0",
            "content": content, "owner": "procurement.core", "dependencies": [], "compatibility_policy": "backward",
            "provenance": {"kind": "user", "source": "packs/procurement (Public Procurement pack)"},
            "actor": {"principal_id": principal_id, "kind": "user"},
        }, f"procurement-schema:{contract}:{digest(content)[:16]}", principal_id=principal_id, scopes=scopes))
    return results
