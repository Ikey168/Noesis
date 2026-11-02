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

``noesis-ownership-record-v2`` (#2883) is a new contract version beside v1,
which stays unchanged and accepted. A v2 record may also carry what structured
SEC filings state: the ``reported_holding`` assertion kind (a Form 13F
information-table position with no percentage and no control), Schedule
13D/13G voting and dispositive ``powers`` and ``group`` membership, the
``filing`` an assertion comes from with its place on the amendment chain, and
the ``person_policy`` a natural-person statement was stored under (owner-scoped
by default; the opt-in ``NOESIS_PERSON_RESEARCH`` mode stores it unscoped and
such records are refused with ``minimisation_violation`` when the mode is off).
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from src.config.env import person_research_enabled

CONTRACT = "noesis-ownership-record-v1"
PART_CONTRACT = "noesis-ownership-part-v1"
DOSSIER_CONTRACT = "noesis-ownership-dossier-v1"
CONTRACT_V2 = "noesis-ownership-record-v2"  # #2883; v1 stays accepted and unchanged
CONTRACTS = (CONTRACT, CONTRACT_V2)
READ_SCOPE = "knowledge:ownership:read"
WRITE_SCOPE = "knowledge:ownership:write"
REVIEW_SCOPE = "knowledge:ownership:review"
PROVIDERS = (
    "gleif", "companies-house", "sec-edgar", "open-ownership", "opencorporates",
    "handelsregister", "unternehmensregister", "bris", "market-instruments", "bafin",
)
# ICIJ Offshore Leaks Database (#2888): leak-dataset records, quoted with ICIJ's caveat; never a verdict.
PROVIDERS = PROVIDERS + ("icij-offshore-leaks",)
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
# Natural persons named by a register (PSC individuals, ROE beneficial owners,
# BODS person statements) are owner-scoped by default. With the opt-in
# person-research mode (NOESIS_PERSON_RESEARCH, docs/security/person-research-mode.md)
# they are stored unscoped and carry this policy; writing such a record with the
# switch off is refused (``minimisation_violation``).
PERSON_RESEARCH_POLICY = "ownership-person-research-v1"
SCHEMA_DIR = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema"
SCHEMAS = {
    "ownership-record": "noesis-ownership-record-v1",
    "ownership-part": "noesis-ownership-part-v1",
    "ownership-dossier": "noesis-ownership-dossier-v1",
}
# Further registered versions of a module: (name, semantic version) -> contract file (#2883).
SCHEMA_VERSIONS = {("ownership-record", "2.0.0"): CONTRACT_V2}


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
    if record.get("contract") == CONTRACT_V2:
        unknowns.extend(v2_unknowns(record))
    return sorted(set(unknowns))


def validate_record(record: Any) -> dict[str, Any]:
    """Validate and return a canonical copy with ``unknowns`` recomputed."""
    if not isinstance(record, dict):
        _fail("ownership record must be an object")
    lowered = {str(k).lower() for k in record}
    if any(marker in lowered for marker in FORBIDDEN_FIELDS):
        _fail("ownership records hold what a source states; no beneficial-ownership, sanctions or AML "
              "determination is stored", "determination_forbidden")
    if record.get("contract") not in CONTRACTS:
        _fail("unsupported ownership record contract", "schema_drift")
    v2 = record["contract"] == CONTRACT_V2
    kind = _enum(record.get("kind"), KINDS, "kind")
    extra = set(record) - _COMMON - _KIND_FIELDS[kind] - (V2_KIND_FIELDS.get(kind, set()) if v2 else set())
    if extra:
        _fail(f"unsupported {kind} field: " + ", ".join(sorted(extra)))
    _text(record.get("record_key"), "record_key", limit=1000)
    _source(record.get("source"))
    if record.get("owner_scoped") not in (None, True, False):
        _fail("owner_scoped is a boolean")
    native = record.get("native")
    if isinstance(native, dict) and native.get("policy") == PERSON_RESEARCH_POLICY and not person_research_enabled():
        _fail("this record was produced under person-research mode (NOESIS_PERSON_RESEARCH); it is refused while "
              "the switch is off", "minimisation_violation")
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
        assertion_kind = _enum(result.get("assertion_kind"), ASSERTION_KINDS + (V2_ASSERTION_KINDS if v2 else ()),
                               "assertion_kind")
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
    if v2:
        validate_v2_fields(result)
    result["unknowns"] = compute_unknowns(result)
    return result


def record(kind: str, record_key: str, source: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """Build and validate a record; convenient for adapters and fixtures."""
    return validate_record({"contract": CONTRACT, "kind": kind, "record_key": record_key, "source": source, **fields})


def entity_key(scheme: str, value: str) -> str:
    """Stable per-source identity key, e.g. ``lei:5299...`` or ``gb-coh:09990002``."""
    return f"{scheme.lower()}:{str(value).strip()}"


def schema(name: str, version: str = "1.0.0") -> dict[str, Any]:
    contract = SCHEMAS[name] if version == "1.0.0" else SCHEMA_VERSIONS[(name, version)]
    return json.loads((SCHEMA_DIR / f"{contract}.json").read_text())


def register_schemas(conn: Any, *, principal_id: str, scopes: Any) -> list[dict[str, Any]]:
    """Register the ownership contracts as modules in the shared schema registry."""
    from src.kb.schema_registry import SchemaRegistry

    registry = SchemaRegistry(conn)
    results = []
    versions = [(name, contract, "1.0.0") for name, contract in SCHEMAS.items()]
    versions += [(name, contract, version) for (name, version), contract in SCHEMA_VERSIONS.items()]
    for name, contract, version in versions:
        definition = {
            "contract": "noesis-schema-module-v1", "name": name, "kind": "schema", "semantic_version": version,
            "content": schema(name, version), "owner": "ownership.core", "dependencies": [],
            "compatibility_policy": "backward",
            "provenance": {"kind": "imported", "source": f"contracts/schemas/jsonschema/{contract}.json"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(registry.register(definition, f"ownership-schema:{name}:{version}", principal_id=principal_id,
                                         scopes=scopes))
    return results


# ------------------------------------------------- noesis-ownership-record-v2 (#2883)

V2_ASSERTION_KINDS = ("reported_holding",)
PERSON_POLICY_DEFAULT = "ownership-person-owner-scoped-v1"
PERSON_POLICY_RESEARCH = "ownership-person-research-v1"
PERSON_POLICIES = (PERSON_POLICY_DEFAULT, PERSON_POLICY_RESEARCH)
V2_KIND_FIELDS = {
    "ownership_assertion": {"filing", "position", "powers", "group", "person_policy"},
    "filing_reference": {"amendment"},
    "person": {"person_policy"},
}
AMENDMENT_TYPES = ("restatement", "new_holdings")
AMOUNT_TYPES = ("SH", "PRN")
PUT_CALL = ("Put", "Call")
INVESTMENT_DISCRETION = ("SOLE", "DFND", "OTR")
VALUE_UNITS = ("USD", "USD-thousands")
POWER_FIELDS = ("sole_voting", "shared_voting", "sole_dispositive", "shared_dispositive", "aggregate_amount")
_DECIMAL_TEXT = re.compile(r"^\d+(\.\d+)?$")
_ACCESSION = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_CUSIP = re.compile(r"^[0-9A-Z*@#]{9}$")


def record_v2(kind: str, record_key: str, source: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """Build and validate a ``noesis-ownership-record-v2`` record."""
    return validate_record({"contract": CONTRACT_V2, "kind": kind, "record_key": record_key, "source": source, **fields})


def _decimal_text(value: Any, field: str) -> Any:
    if value is None:
        return None
    if not isinstance(value, str) or not _DECIMAL_TEXT.fullmatch(value):
        _fail(f"{field} is a decimal string as stated, never a float")
    return value


def _object(value: Any, allowed: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - allowed:
        _fail(f"{field} uses " + "/".join(sorted(allowed)))
    return value


def _amendment(value: Any, field: str) -> None:
    amendment = _object(value, {"is_amendment", "number", "type", "amends_accession", "chain_key", "chain_basis"}, field)
    if amendment.get("is_amendment") not in (True, False):
        _fail(f"{field}.is_amendment states whether the filing amends an earlier one")
    _text(amendment.get("chain_key"), f"{field}.chain_key", limit=500)
    if amendment.get("number") is not None and not re.fullmatch(r"\d+", str(amendment["number"])):
        _fail(f"{field}.number is the amendment number as stated")
    if amendment.get("type") is not None:
        _enum(amendment["type"], AMENDMENT_TYPES, f"{field}.type")
    if amendment.get("amends_accession") is not None and not _ACCESSION.fullmatch(str(amendment["amends_accession"])):
        _fail(f"{field}.amends_accession is an accession number")
    if not amendment["is_amendment"] and (amendment.get("number") or amendment.get("type") or amendment.get("amends_accession")):
        _fail(f"{field}: an original filing carries no amendment number, type or amended accession")


def _filing(value: Any) -> None:
    filing = _object(value, {"accession_number", "form_type", "filing_date", "period_of_report", "event_date",
                             "document_url", "amendment"}, "filing")
    if not _ACCESSION.fullmatch(str(filing.get("accession_number") or "")):
        _fail("filing.accession_number is an EDGAR accession number")
    _text(filing.get("form_type"), "filing.form_type", limit=100)
    for key in ("filing_date", "period_of_report", "event_date"):
        _date(filing.get(key), f"filing.{key}")
    if filing.get("document_url") is not None and not str(filing["document_url"]).startswith("https://"):
        _fail("filing.document_url must be an HTTPS locator")
    _amendment(filing.get("amendment"), "filing.amendment")


def _position(value: Any) -> None:
    position = _object(value, {"issuer_name", "title_of_class", "cusip", "figi", "amount", "amount_type", "value",
                               "value_unit", "put_call", "investment_discretion", "other_managers",
                               "voting_authority"}, "position")
    if not _CUSIP.fullmatch(str(position.get("cusip") or "")):
        _fail("position.cusip is the nine-character CUSIP as reported")
    if position.get("amount") is None:
        _fail("position.amount (shares or principal) is required")
    _decimal_text(position.get("amount"), "position.amount")
    _enum(position.get("amount_type"), AMOUNT_TYPES, "position.amount_type")
    _decimal_text(position.get("value"), "position.value")
    for key, allowed in (("value_unit", VALUE_UNITS), ("put_call", PUT_CALL),
                         ("investment_discretion", INVESTMENT_DISCRETION)):
        if position.get(key) is not None:
            _enum(position[key], allowed, f"position.{key}")
    managers = position.get("other_managers")
    if managers is not None and (not isinstance(managers, list) or not all(isinstance(m, str) for m in managers)):
        _fail("position.other_managers lists the other-manager numbers as stated")
    voting = position.get("voting_authority")
    if voting is not None:
        for key, amount in _object(voting, {"sole", "shared", "none"}, "position.voting_authority").items():
            _decimal_text(amount, f"position.voting_authority.{key}")


def _person_policy(result: dict[str, Any], *, natural_person: bool) -> None:
    policy = result.get("person_policy")
    if not natural_person:
        if policy is not None:
            _fail("person_policy applies to natural-person statements only")
        return
    _enum(policy, PERSON_POLICIES, "person_policy")
    if policy == PERSON_POLICY_DEFAULT and result.get("owner_scoped") is not True:
        _fail("a natural-person statement stored under the default policy is owner-scoped", "minimisation_violation")
    if policy == PERSON_POLICY_RESEARCH:
        from src.config.env import person_research_enabled

        if not person_research_enabled():
            _fail("person-research records are refused while NOESIS_PERSON_RESEARCH is off", "minimisation_violation")


def validate_v2_fields(result: dict[str, Any]) -> None:
    """Rules for the fields only ``noesis-ownership-record-v2`` carries."""
    kind = result["kind"]
    if kind == "person":
        _person_policy(result, natural_person=True)
    elif kind == "filing_reference":
        if result.get("amendment") is not None:
            _amendment(result["amendment"], "amendment")
    elif kind == "ownership_assertion":
        assertion_kind = result["assertion_kind"]
        if result.get("filing") is not None:
            _filing(result["filing"])
        if assertion_kind == "reported_holding":
            if result.get("position") is None or result.get("filing") is None:
                _fail("a reported holding carries the position and the filing that reports it")
            if result.get("share") is not None:
                _fail("a reported holding states shares or principal, not a percentage")
        elif result.get("position") is not None:
            _fail("only reported_holding assertions carry a 13F position")
        if result.get("position") is not None:
            _position(result["position"])
        if (result.get("powers") is not None or result.get("group") is not None) and assertion_kind not in {
                "shareholding", "voting_rights"}:
            _fail("voting/dispositive powers and group membership belong to shareholding or voting-rights assertions")
        if result.get("powers") is not None:
            for key, amount in _object(result["powers"], set(POWER_FIELDS), "powers").items():
                _decimal_text(amount, f"powers.{key}")
        natural = (result.get("holder") or {}).get("kind") == "person"
        group = result.get("group")
        if group is not None:
            _object(group, {"member_of_group", "members", "basis"}, "group")
            if group.get("member_of_group") not in ("a", "b", None):
                _fail("group.member_of_group is the cover-page box as stated: a, b or unknown")
            if not isinstance(group.get("members") or [], list):
                _fail("group.members is a list")
            from src.config.env import person_research_enabled

            for member in group.get("members") or []:
                _object(member, {"key", "name", "kind", "withheld"}, "group.members[]")
                _enum(member.get("kind"), HOLDER_KINDS, "group.members[].kind")
                if member["kind"] == "person" and not member.get("withheld") and not person_research_enabled():
                    _fail("a natural-person group member is withheld unless person-research mode is on",
                          "minimisation_violation")
                if member.get("withheld") and (member.get("name") or member.get("key")):
                    _fail("a withheld group member carries no name or key")
        _person_policy(result, natural_person=natural)


def v2_unknowns(record: dict[str, Any]) -> list[str]:
    unknowns = []
    position = record.get("position") or {}
    if record.get("kind") == "ownership_assertion" and record.get("assertion_kind") == "reported_holding":
        if position.get("value") is None:
            unknowns.append("position.value")
        elif position.get("value_unit") is None:
            unknowns.append("position.value_unit")
        if position.get("voting_authority") is None:
            unknowns.append("position.voting_authority")
        if position.get("investment_discretion") is None:
            unknowns.append("position.investment_discretion")
    filing = record.get("filing") or {}
    if record.get("kind") == "ownership_assertion" and record.get("assertion_kind") in {"shareholding", "voting_rights"} \
            and filing:
        if record.get("powers") is None:
            unknowns.append("powers")
        if (record.get("group") or {}).get("member_of_group") is None:
            unknowns.append("group.member_of_group")
    amendment = filing.get("amendment") or record.get("amendment") or {}
    if amendment.get("is_amendment") and not amendment.get("amends_accession"):
        unknowns.append("amendment.amends_accession")
    return unknowns
