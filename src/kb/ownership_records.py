"""Corporate-ownership record model (``noesis-ownership-record-v1``).

Every record is *what one source states*: a legal entity, a person, a
registration, an officer role, an ownership or control assertion, a corporate
event or a filing reference. The model keeps apart things registries blur:

* **direct_parent** and **ultimate_parent** are accounting-consolidation
  assertions (GLEIF Level 2); they are distinct kinds from control assertions
  such as **shareholding**, **voting_rights**, **appoint_directors**,
  **significant_influence** and **other_control** (Companies House PSC, BODS,
  Schedule 13D/13G cover pages);
* a **reporting_exception** is a source statement that an owner is not
  reported (GLEIF exception categories, "no PSC" and PSC exemption
  statements, BODS unspecified interested parties); it is never read as
  "no owner exists";
* percentages are an exact decimal string *or* a band with explicit bounds,
  never a band collapsed into a point value;
* absent values stay ``None`` and are listed in ``unknowns``; nothing is
  defaulted or interpolated.

Records never carry a beneficial-ownership, sanctions or AML determination:
``determination`` fields are rejected outright. Conflicting assertions from
different sources are separate records (their ``record_key`` includes the
source) and are never merged.

Entity-like records (``legal_entity``, ``person``) reference the shared
``canonical_entities`` owner (``src/kb/entities.py``) by id; this module does
not create another entity store.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

CONTRACT = "noesis-ownership-record-v1"
PART_CONTRACT = "noesis-ownership-part-v1"
DOSSIER_CONTRACT = "noesis-ownership-dossier-v1"
READ_SCOPE = "knowledge:ownership:read"
WRITE_SCOPE = "knowledge:ownership:write"
REVIEW_SCOPE = "knowledge:ownership:review"
PROVIDERS = (
    "gleif", "companies-house", "sec-edgar", "open-ownership", "opencorporates",
    "handelsregister", "unternehmensregister", "bris", "market-instruments",
)
KINDS = (
    "legal_entity", "person", "registration", "officer_role", "ownership_assertion",
    "corporate_event", "filing_reference",
)
ENTITY_KINDS = ("legal_entity", "person")
PARENT_KINDS = ("direct_parent", "ultimate_parent")
CONTROL_KINDS = ("shareholding", "voting_rights", "appoint_directors", "significant_influence", "other_control")
ASSERTION_KINDS = PARENT_KINDS + CONTROL_KINDS + ("reporting_exception",)
HOLDER_KINDS = ("entity", "person", "unknown")
EVENT_TYPES = (
    "incorporation", "registration", "dissolution", "name_change", "merger", "succession",
    "re_registration", "status_change", "other",
)
DATE_STATUS = ("stated", "before", "unknown")
FROM_STATUS = ("stated", "unknown")
TO_STATUS = ("stated", "open", "unknown")
EXCEPTION_LEVELS = ("direct", "ultimate", "psc", "any")
FORBIDDEN_FIELDS = ("determination", "beneficial_owner_determination", "sanctions", "aml", "risk_score")
_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_ENTITY_FIELDS = {"name", "jurisdiction", "identifiers", "entity_type", "status", "other_names", "founding_date",
                  "dissolution_date", "addresses", "canonical_entity_id"}
_PERSON_FIELDS = {"name", "identifiers", "nationalities", "country_of_residence", "native_kind", "canonical_entity_id"}
_KIND_FIELDS = {
    "legal_entity": _ENTITY_FIELDS,
    "person": _PERSON_FIELDS,
    "registration": {"entity_key", "register", "number", "jurisdiction", "status", "registered_on", "dissolved_on",
                     "document_reference"},
    "officer_role": {"entity_key", "officer", "role", "appointed_on", "resigned_on", "appointed_status"},
    "ownership_assertion": {"subject_key", "holder", "assertion_kind", "share", "native", "validity",
                            "reporting_exception", "relationship_status", "basis", "statement_date"},
    "corporate_event": {"entity_key", "event_type", "related_entity_key", "event_date", "date_status", "description"},
    "filing_reference": {"entity_key", "form_type", "accession_number", "filing_date", "document_url", "description",
                         "parsed", "filers", "period_of_report"},
}
_COMMON = {"contract", "kind", "record_key", "source", "owner_scoped", "unknowns", "native"}
_SOURCE_FIELDS = {"provider", "provider_record_id", "url", "locator", "statement_id", "publisher", "source_type",
                  "retrieved_at_ms", "raw_sha256", "license", "revision", "note"}
SCHEMA_DIR = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema"
SCHEMAS = {
    "ownership-record": "noesis-ownership-record-v1",
    "ownership-part": "noesis-ownership-part-v1",
    "ownership-dossier": "noesis-ownership-dossier-v1",
}


class OwnershipRecordError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _fail(message: str, code: str = "invalid_ownership_record") -> None:
    raise OwnershipRecordError(code, message)


def _text(value: Any, field: str, *, optional: bool = False, limit: int = 4000) -> Any:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _fail(f"{field} must be nonempty text")
    return value


def _date(value: Any, field: str) -> Any:
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        _fail(f"{field} must be YYYY, YYYY-MM or YYYY-MM-DD (partial dates stay partial)")
    return value


def _enum(value: Any, allowed: tuple[str, ...], field: str) -> Any:
    if value not in allowed:
        _fail(f"{field} must be one of {', '.join(allowed)}")
    return value


def _percent(value: Any, field: str) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        _fail(f"{field} must be a decimal string, never a float")
    try:
        number = Decimal(value)
    except InvalidOperation:
        _fail(f"{field} is not a decimal")
    if not number.is_finite() or number < 0 or number > 100:
        _fail(f"{field} must be a percentage between 0 and 100")
    return value


def validate_share(share: Any) -> Any:
    """An exact percentage or an explicit band; bands are never collapsed."""
    if share is None:
        return None
    if not isinstance(share, dict) or set(share) - {"exact", "band", "native", "shares", "class"}:
        _fail("share uses exact/band/native/shares/class")
    if ("exact" in share) == ("band" in share):
        _fail("share states either an exact percentage or a band")
    if "exact" in share:
        _percent(share["exact"], "share.exact")
    else:
        band = share["band"]
        if not isinstance(band, dict) or set(band) - {"min", "max", "min_inclusive", "max_inclusive"}:
            _fail("share.band uses min/max/min_inclusive/max_inclusive")
        _percent(band.get("min"), "share.band.min")
        _percent(band.get("max"), "share.band.max")
        if band.get("min") is None and band.get("max") is None:
            _fail("a share band needs at least one bound")
        if band.get("min") is not None and band.get("max") is not None and Decimal(band["min"]) > Decimal(band["max"]):
            _fail("share.band.min exceeds max")
        for key in ("min_inclusive", "max_inclusive"):
            if band.get(key) not in (True, False, None):
                _fail(f"share.band.{key} is true, false or unknown")
    if share.get("shares") is not None and (not isinstance(share["shares"], str) or not re.fullmatch(r"\d+(\.\d+)?", share["shares"])):
        _fail("share.shares is a decimal string of shares as stated")
    return share


def _identifiers(values: Any, field: str) -> None:
    if values is None:
        return
    if not isinstance(values, list):
        _fail(f"{field} must be a list")
    for item in values:
        if not isinstance(item, dict) or set(item) - {"scheme", "value", "authority", "source"} or not item.get("scheme"):
            _fail(f"{field} items use scheme/value/authority/source")
        _text(item.get("value"), f"{field}.value", limit=500)


def _validity(validity: Any) -> dict[str, Any]:
    if validity is None:
        validity = {}
    if not isinstance(validity, dict) or set(validity) - {"from", "to", "from_status", "to_status"}:
        _fail("validity uses from/to/from_status/to_status")
    result = {
        "from": _date(validity.get("from"), "validity.from"),
        "to": _date(validity.get("to"), "validity.to"),
        "from_status": validity.get("from_status") or ("stated" if validity.get("from") else "unknown"),
        "to_status": validity.get("to_status") or ("stated" if validity.get("to") else "unknown"),
    }
    _enum(result["from_status"], FROM_STATUS, "validity.from_status")
    _enum(result["to_status"], TO_STATUS, "validity.to_status")
    if result["from_status"] == "stated" and not result["from"]:
        _fail("a stated validity start needs a date")
    if result["to_status"] == "stated" and not result["to"]:
        _fail("a stated validity end needs a date")
    if result["to_status"] in {"open", "unknown"} and result["to"]:
        _fail("an open or unknown end carries no date")
    if result["from"] and result["to"] and result["to"] < result["from"]:
        _fail("validity ends before it starts")
    return result


def _source(source: Any) -> dict[str, Any]:
    if not isinstance(source, dict) or set(source) - _SOURCE_FIELDS:
        _fail("source uses " + "/".join(sorted(_SOURCE_FIELDS)))
    _enum(source.get("provider"), PROVIDERS, "source.provider")
    _text(source.get("provider_record_id"), "source.provider_record_id", limit=1000)
    if source.get("url") is not None and not str(source["url"]).startswith("https://"):
        _fail("source.url must be an HTTPS locator")
    if source.get("locator") is not None and (not isinstance(source["locator"], dict) or set(source["locator"]) - {
            "json_pointer", "xpath", "quote", "line", "field"}):
        _fail("source.locator uses json_pointer/xpath/quote/line/field")
    return source


def compute_unknowns(record: dict[str, Any]) -> list[str]:
    """Explicitly unknown fields; never defaults."""
    kind, unknowns = record["kind"], []
    if kind == "legal_entity":
        for field in ("jurisdiction", "status", "founding_date"):
            if record.get(field) in (None, ""):
                unknowns.append(field)
        if not record.get("identifiers"):
            unknowns.append("identifiers")
    elif kind == "person":
        if not record.get("identifiers"):
            unknowns.append("identifiers")
    elif kind == "registration":
        for field in ("status", "registered_on"):
            if record.get(field) in (None, ""):
                unknowns.append(field)
    elif kind == "officer_role":
        if record.get("appointed_status") != "stated":
            unknowns.append("appointed_on")
    elif kind == "ownership_assertion":
        validity = record["validity"]
        if validity["from_status"] == "unknown":
            unknowns.append("validity.from")
        if validity["to_status"] == "unknown":
            unknowns.append("validity.to")
        if record["assertion_kind"] != "reporting_exception":
            if record.get("share") is None and record["assertion_kind"] in {"shareholding", "voting_rights"}:
                unknowns.append("share")
            holder = record.get("holder") or {}
            if holder.get("kind") == "unknown" or not holder.get("key"):
                unknowns.append("holder.identity")
    elif kind == "corporate_event":
        if record.get("date_status") != "stated":
            unknowns.append("event_date")
        if record["event_type"] in {"merger", "succession"} and not record.get("related_entity_key"):
            unknowns.append("related_entity_key")
    elif kind == "filing_reference":
        if not record.get("parsed"):
            unknowns.append("ownership_figures (filing not parsed)")
    return sorted(set(unknowns))


def validate_record(record: Any) -> dict[str, Any]:
    """Validate and return a canonical copy with ``unknowns`` recomputed."""
    if not isinstance(record, dict):
        _fail("ownership record must be an object")
    lowered = {str(k).lower() for k in record}
    if any(marker in lowered for marker in FORBIDDEN_FIELDS):
        _fail("ownership records hold what a source states; no beneficial-ownership, sanctions or AML "
              "determination is stored", "determination_forbidden")
    if record.get("contract") != CONTRACT:
        _fail("unsupported ownership record contract", "schema_drift")
    kind = _enum(record.get("kind"), KINDS, "kind")
    extra = set(record) - _COMMON - _KIND_FIELDS[kind]
    if extra:
        _fail(f"unsupported {kind} field: " + ", ".join(sorted(extra)))
    _text(record.get("record_key"), "record_key", limit=1000)
    _source(record.get("source"))
    if record.get("owner_scoped") not in (None, True, False):
        _fail("owner_scoped is a boolean")
    result = json.loads(canonical(record))
    if result.get("native") is not None and not isinstance(result["native"], dict):
        _fail("native preserves the source's original wording as an object")
    if kind in ENTITY_KINDS:
        _text(result.get("name"), "name", limit=2000)
        _identifiers(result.get("identifiers"), "identifiers")
        if kind == "legal_entity":
            _date(result.get("founding_date"), "founding_date")
            _date(result.get("dissolution_date"), "dissolution_date")
    elif kind == "registration":
        _text(result.get("entity_key"), "entity_key", limit=1000)
        _text(result.get("register"), "register", limit=500)
        _text(result.get("number"), "number", limit=200)
        _date(result.get("registered_on"), "registered_on")
        _date(result.get("dissolved_on"), "dissolved_on")
        document = result.get("document_reference")
        if document is not None and (not isinstance(document, dict) or set(document) - {
                "kind", "reference", "url", "sha256", "retrieved_on", "official"} or not document.get("reference")):
            _fail("document_reference uses kind/reference/url/sha256/retrieved_on/official")
    elif kind == "officer_role":
        _text(result.get("entity_key"), "entity_key", limit=1000)
        officer = result.get("officer")
        if not isinstance(officer, dict) or set(officer) - {"name", "key", "kind"}:
            _fail("officer uses name/key/kind")
        _text(officer.get("name"), "officer.name", limit=1000)
        _text(result.get("role"), "role", limit=200)
        _date(result.get("appointed_on"), "appointed_on")
        _date(result.get("resigned_on"), "resigned_on")
        result["appointed_status"] = result.get("appointed_status") or ("stated" if result.get("appointed_on") else "unknown")
        _enum(result["appointed_status"], DATE_STATUS, "appointed_status")
    elif kind == "ownership_assertion":
        _text(result.get("subject_key"), "subject_key", limit=1000)
        assertion_kind = _enum(result.get("assertion_kind"), ASSERTION_KINDS, "assertion_kind")
        holder = result.get("holder")
        exception = result.get("reporting_exception")
        if assertion_kind == "reporting_exception":
            if holder is not None:
                _fail("a reporting exception names no holder; it states that none is reported")
            if not isinstance(exception, dict) or set(exception) - {"level", "category", "reason", "native_text"}:
                _fail("reporting_exception uses level/category/reason/native_text")
            _enum(exception.get("level"), EXCEPTION_LEVELS, "reporting_exception.level")
            _text(exception.get("category"), "reporting_exception.category", limit=200)
        else:
            if exception is not None:
                _fail("only reporting_exception assertions carry a reporting_exception")
            if not isinstance(holder, dict) or set(holder) - {"key", "name", "kind"}:
                _fail("holder uses key/name/kind")
            _enum(holder.get("kind"), HOLDER_KINDS, "holder.kind")
            if holder.get("kind") != "unknown" and not (holder.get("name") or holder.get("key")):
                _fail("a known holder carries a name or a source key")
        if assertion_kind in PARENT_KINDS and result.get("share") is not None:
            _fail("consolidation-parent assertions state no percentage; model a control assertion")
        validate_share(result.get("share"))
        result["validity"] = _validity(result.get("validity"))
        _date(result.get("statement_date"), "statement_date")
    elif kind == "corporate_event":
        _text(result.get("entity_key"), "entity_key", limit=1000)
        _enum(result.get("event_type"), EVENT_TYPES, "event_type")
        _date(result.get("event_date"), "event_date")
        result["date_status"] = result.get("date_status") or ("stated" if result.get("event_date") else "unknown")
        _enum(result["date_status"], DATE_STATUS, "date_status")
        if result["date_status"] == "unknown" and result.get("event_date"):
            _fail("an unknown event date carries no date")
    elif kind == "filing_reference":
        _text(result.get("entity_key"), "entity_key", limit=1000)
        _text(result.get("form_type"), "form_type", limit=100)
        _text(result.get("accession_number"), "accession_number", limit=200)
        _date(result.get("filing_date"), "filing_date")
        if result.get("document_url") is not None and not str(result["document_url"]).startswith("https://"):
            _fail("document_url must be an HTTPS locator")
        if result.get("parsed") not in (True, False):
            _fail("filing_reference.parsed states whether ownership figures were extracted")
    result["unknowns"] = compute_unknowns(result)
    return result


def record(kind: str, record_key: str, source: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """Build and validate a record; convenient for adapters and fixtures."""
    return validate_record({"contract": CONTRACT, "kind": kind, "record_key": record_key, "source": source, **fields})


def entity_key(scheme: str, value: str) -> str:
    """Stable per-source identity key, e.g. ``lei:5299...`` or ``gb-coh:09990002``."""
    return f"{scheme.lower()}:{str(value).strip()}"


def schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / f"{SCHEMAS[name]}.json").read_text())


def register_schemas(conn: Any, *, principal_id: str, scopes: Any) -> list[dict[str, Any]]:
    """Register the ownership contracts as modules in the shared schema registry."""
    from src.kb.schema_registry import SchemaRegistry

    registry = SchemaRegistry(conn)
    results = []
    for name, contract in SCHEMAS.items():
        definition = {
            "contract": "noesis-schema-module-v1", "name": name, "kind": "schema", "semantic_version": "1.0.0",
            "content": schema(name), "owner": "ownership.core", "dependencies": [], "compatibility_policy": "backward",
            "provenance": {"kind": "imported", "source": f"contracts/schemas/jsonschema/{contract}.json"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(registry.register(definition, f"ownership-schema:{name}:1.0.0", principal_id=principal_id,
                                         scopes=scopes))
    return results
