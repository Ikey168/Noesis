"""Regulatory enforcement records: authorities, actions, respondents, notices, penalties and appeals (#2651, EN02).

``noesis-enforcement-record-v1`` records are *what one regulator published*
about an enforcement action outside competition law (competition cases live in
:mod:`src.kb.competition_records`). They are persisted as immutable revisions
by :class:`src.kb.enforcement.EnforcementStore` (one stable record per
``record_key``, a new revision only when the published content changes, every
revision with its run and observation time):

* ``authority`` - the regulator as a source identity (SEC, FCA, EPA, a
  supervisory authority named in the EDPB Article 60 register);
* ``enforcement_action`` - keyed by authority and native identifier (SEC
  release or file number, FCA notice reference, ECHO case number, EDPB register
  entry): action type, legal bases and charges as cited, key dates (initiated,
  decided, published), the outcome **as published** (a settlement keeps the
  published admission wording, e.g. "without admitting or denying"), court
  cases as citations, facilities by published FRS identifier, lead and
  concerned authorities and corrective measures as published;
* ``respondent`` - an **organisational** respondent's name and role exactly as
  published, with published identifiers (CIK, FRN, LEI, company number); no
  identity is resolved here;
* ``enforcement_decision`` - the notice, release, order or decision document
  as a versioned document (type, date, URL, content digest); a corrected or
  superseded notice is a new revision of the same record;
* ``penalty`` - one published monetary sanction or remedy: type as published,
  amount as the published text and currency; an undisclosed amount stays
  ``not_published``; nothing is converted or summed;
* ``appeal`` - a published appeal or reference (Upper Tribunal, court of
  appeals, national court), forum, reference and status as published.

Removals and corrections by the source are revisions (``source_status``
``corrected`` / ``removed_by_source``), never deletions.

**Data minimisation (EN01).** Natural persons are never stored by name: a
``respondent`` record is organisational only, an action keeps the *count* of
individual respondents, and adapters replace the individual's published name
by ``[individual]`` in titles and outcome texts. Personal attributes (date of
birth, home address, nationality, personal registration numbers) are rejected
at write time. No record carries a risk or compliance score, a finding of
wrongdoing inferred from an initiated action, or a merged "finding" for a
settled matter: such fields are rejected outright.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

CONTRACT = "noesis-enforcement-record-v1"
SCHEMA_NAME = "enforcement-record"
SCHEMA_VERSION = "1.0.0"
KINDS = ("authority", "enforcement_action", "respondent", "enforcement_decision", "penalty", "appeal")
CHILD_KINDS = ("respondent", "enforcement_decision", "penalty", "appeal")
PROVIDERS = ("us-sec", "uk-fca", "us-epa-echo", "edpb-art60")
ACTION_TYPES = ("civil_action", "administrative_proceeding", "final_notice", "civil_judicial", "administrative_formal",
                "criminal", "art60_final_decision", "other")
PENALTY_TYPES = ("civil_penalty", "disgorgement", "prejudgment_interest", "financial_penalty",
                 "penalty_before_settlement_discount", "federal_penalty", "state_local_penalty",
                 "supplemental_environmental_project", "cost_recovery", "compliance_action_cost", "administrative_fine",
                 "other")
AMOUNT_STATUS = ("stated", "not_published")
SOURCE_STATUS = ("published", "corrected", "removed_by_source")
RESPONDENT_ROLES = ("respondent", "defendant", "firm", "controller", "processor", "other")
# Fields no record may carry (#2651 exclusions and the EN01 minimisation decision).
FORBIDDEN_FIELDS = ("risk_score", "compliance_score", "risk_rating", "compliance_rating", "wrongdoing",
                    "finding_of_wrongdoing", "violation_found", "guilty", "culpability", "inferred_outcome",
                    "prediction", "predicted_outcome", "legal_advice", "profile", "person_profile")
PERSONAL_FIELDS = ("date_of_birth", "birth_date", "dob", "home_address", "residential_address", "nationality",
                   "personal_id", "national_id_number", "passport_number", "ssn", "age", "individual_name",
                   "natural_person_name", "natural_person_names")
_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_AUTHORITY = re.compile(r"^[a-z]{2}-[a-z0-9-]{2,40}$")
_SOURCE_FIELDS = {"provider", "provider_record_id", "url", "locator", "publisher", "license", "revision", "note",
                  "retrieved_at_ms", "raw_sha256", "evidence_origin"}
_COMMON = {"contract", "kind", "record_key", "source", "native", "unknowns"}
_KIND_FIELDS = {
    "authority": {"authority", "name_as_published", "jurisdiction", "url", "identifiers"},
    "enforcement_action": {"authority", "action_number", "action_type", "action_type_as_published", "title",
                           "status_as_published", "source_status", "initiated_on", "decided_on", "published_on",
                           "legal_bases", "charges_as_published", "outcome_as_published", "settlement",
                           "related_identifiers", "court_cases", "related_references", "natural_person_respondents",
                           "facilities", "lead_authority", "concerned_authorities",
                           "corrective_measures_as_published", "page_revision", "action_url"},
    "respondent": {"action_key", "authority", "action_number", "name_as_published", "respondent_type", "role",
                   "role_as_published", "identifiers", "country"},
    "enforcement_decision": {"action_key", "authority", "action_number", "document_type_as_published", "title",
                             "document_date", "url", "content_sha256", "language", "amended_on",
                             "correction_as_published", "source_status"},
    "penalty": {"action_key", "authority", "action_number", "penalty_type", "penalty_type_as_published",
                "amount_as_published", "currency", "amount_status", "respondent_key", "imposed_on", "note"},
    "appeal": {"action_key", "authority", "action_number", "forum_as_published", "reference", "status_as_published",
               "lodged_on", "decided_on", "court_docket"},
}
SCHEMA_DIR = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema"


class EnforcementRecordError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _fail(message: str, code: str = "invalid_enforcement_record") -> None:
    raise EnforcementRecordError(code, message)


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
        _fail(f"{field} must be YYYY, YYYY-MM or YYYY-MM-DD")
    return value


def _enum(value: Any, allowed: tuple[str, ...], field: str) -> Any:
    if value not in allowed:
        _fail(f"{field} must be one of {', '.join(allowed)}")
    return value


def _https(value: Any, field: str) -> Any:
    if value is not None and not str(value).startswith("https://"):
        _fail(f"{field} must be an HTTPS locator")
    return value


def _list(value: Any, field: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        _fail(f"{field} must be a list")
    return value


def _identifiers(values: Any, field: str) -> None:
    for item in _list(values, field):
        if not isinstance(item, dict) or set(item) - {"scheme", "value", "type_as_published"} or not item.get("scheme"):
            _fail(f"{field} items use scheme/value/type_as_published")
        _text(item.get("value"), f"{field}.value", limit=500)


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        out = {str(k).lower() for k in value}
        for item in value.values():
            out |= _keys(item)
        return out
    if isinstance(value, list):
        out: set[str] = set()
        for item in value:
            out |= _keys(item)
        return out
    return set()


def compute_unknowns(record: dict[str, Any]) -> list[str]:
    """Explicitly unknown fields; never defaults."""
    kind, unknowns = record["kind"], []
    if kind == "enforcement_action":
        for field in ("initiated_on", "decided_on", "published_on", "outcome_as_published"):
            if record.get(field) in (None, ""):
                unknowns.append(field)
        if not record.get("legal_bases"):
            unknowns.append("legal_bases")
    elif kind == "respondent":
        if not record.get("identifiers"):
            unknowns.append("identifiers")
        if not record.get("country"):
            unknowns.append("country")
    elif kind == "enforcement_decision":
        for field in ("document_date", "content_sha256"):
            if record.get(field) in (None, ""):
                unknowns.append(field)
    elif kind == "penalty":
        if record.get("amount_status") != "stated":
            unknowns.append("amount")
        if record.get("amount_status") == "stated" and not record.get("currency"):
            unknowns.append("currency")
    elif kind == "appeal":
        if not record.get("status_as_published"):
            unknowns.append("status_as_published")
        if not record.get("lodged_on"):
            unknowns.append("lodged_on")
    return sorted(set(unknowns))


def validate_record(record: Any) -> dict[str, Any]:
    """Validate and return a canonical copy with ``unknowns`` recomputed; enforces the minimisation decision."""
    if not isinstance(record, dict):
        _fail("enforcement record must be an object")
    keys = _keys({k: v for k, v in record.items() if k != "native"}) | _keys(record.get("native") or {})
    if keys & set(FORBIDDEN_FIELDS):
        _fail("enforcement records hold what a regulator published; no risk or compliance score, inferred "
              "wrongdoing, merged settlement finding, profile or legal advice is stored", "assessment_forbidden")
    if keys & set(PERSONAL_FIELDS):
        _fail("personal attributes of natural persons are not stored (EN01 minimisation decision)",
              "minimisation_violation")
    if record.get("contract") != CONTRACT:
        _fail("unsupported enforcement record contract", "schema_drift")
    kind = _enum(record.get("kind"), KINDS, "kind")
    extra = set(record) - _COMMON - _KIND_FIELDS[kind]
    if extra:
        _fail(f"unsupported {kind} field: " + ", ".join(sorted(extra)))
    key = _text(record.get("record_key"), "record_key", limit=1000)
    if not key.startswith("enforcement:"):
        _fail("enforcement record keys start with 'enforcement:'")
    source = record.get("source")
    if not isinstance(source, dict) or set(source) - _SOURCE_FIELDS:
        _fail("source uses " + "/".join(sorted(_SOURCE_FIELDS)))
    _enum(source.get("provider"), PROVIDERS, "source.provider")
    _text(source.get("provider_record_id"), "source.provider_record_id", limit=1000)
    _https(source.get("url"), "source.url")
    result = json.loads(canonical(record))
    if result.get("native") is not None and not isinstance(result["native"], dict):
        _fail("native preserves the published wording as an object")
    if not _AUTHORITY.fullmatch(str(result.get("authority") or "")):
        _fail("authority is a lower-case code such as us-sec, uk-fca, us-epa or eu-dpa-nl")
    if kind in CHILD_KINDS:
        _text(result.get("action_key"), "action_key", limit=1000)
        _text(result.get("action_number"), "action_number", limit=200)
    if kind == "authority":
        _text(result.get("name_as_published"), "name_as_published", limit=500)
        _https(result.get("url"), "url")
        _identifiers(result.get("identifiers"), "identifiers")
    elif kind == "enforcement_action":
        _text(result.get("action_number"), "action_number", limit=200)
        _enum(result.get("action_type"), ACTION_TYPES, "action_type")
        _text(result.get("action_type_as_published"), "action_type_as_published", limit=500)
        result["source_status"] = result.get("source_status") or "published"
        _enum(result["source_status"], SOURCE_STATUS, "source_status")
        for field in ("initiated_on", "decided_on", "published_on"):
            _date(result.get(field), field)
        for field in ("legal_bases", "charges_as_published", "related_references", "corrective_measures_as_published"):
            for item in _list(result.get(field), field):
                _text(item, field, limit=2000)
        settlement = result.get("settlement")
        if settlement is not None:
            if not isinstance(settlement, dict) or set(settlement) - {"settled_as_published", "admission_as_published"}:
                _fail("settlement uses settled_as_published/admission_as_published (the published wording only)")
            if settlement.get("settled_as_published") not in (True, False, None):
                _fail("settled_as_published is true, false or null as the source states it")
        _identifiers(result.get("related_identifiers"), "related_identifiers")
        for case in _list(result.get("court_cases"), "court_cases"):
            if not isinstance(case, dict) or set(case) - {"court", "docket_number", "caption"} \
                    or not case.get("docket_number"):
                _fail("court_cases are citations: court/docket_number/caption")
        count = result.get("natural_person_respondents")
        if count is not None and (not isinstance(count, int) or count < 0):
            _fail("natural_person_respondents is a count, never a list of names")
        for facility in _list(result.get("facilities"), "facilities"):
            if not isinstance(facility, dict) or set(facility) - {"frs_id", "name_as_published", "latitude",
                                                                  "longitude", "coordinate_source", "state"}:
                _fail("facilities use frs_id/name_as_published/latitude/longitude/coordinate_source/state")
            if (facility.get("latitude") is None) != (facility.get("longitude") is None):
                _fail("facility coordinates are published as a pair or not at all")
            if facility.get("latitude") is not None and not facility.get("coordinate_source"):
                _fail("facility coordinates name their published source")
        lead = result.get("lead_authority")
        if lead is not None and (not isinstance(lead, dict) or set(lead) - {"name_as_published", "country", "code"}):
            _fail("lead_authority uses name_as_published/country/code")
        for item in _list(result.get("concerned_authorities"), "concerned_authorities"):
            if not isinstance(item, dict) or set(item) - {"name_as_published", "country", "code"}:
                _fail("concerned_authorities use name_as_published/country/code")
        _https(result.get("action_url"), "action_url")
    elif kind == "respondent":
        _text(result.get("name_as_published"), "name_as_published", limit=1000)
        if result.get("respondent_type") != "organisation":
            _fail("only organisational respondents are recorded; natural persons are counted on the action, never "
                  "named (EN01 minimisation decision)", "minimisation_violation")
        _enum(result.get("role"), RESPONDENT_ROLES, "role")
        _text(result.get("role_as_published"), "role_as_published", limit=500)
        _identifiers(result.get("identifiers"), "identifiers")
        if result.get("country") is not None and not re.fullmatch(r"[A-Z]{2}", str(result["country"])):
            _fail("country is an ISO 3166-1 alpha-2 code derived from the published country, or null")
    elif kind == "enforcement_decision":
        _text(result.get("document_type_as_published"), "document_type_as_published", limit=500)
        _date(result.get("document_date"), "document_date")
        _date(result.get("amended_on"), "amended_on")
        _https(result.get("url"), "url")
        if result.get("content_sha256") is not None and not re.fullmatch(r"[0-9a-f]{64}",
                                                                          str(result["content_sha256"])):
            _fail("content_sha256 is the SHA-256 of the published document bytes")
        result["source_status"] = result.get("source_status") or "published"
        _enum(result["source_status"], SOURCE_STATUS, "source_status")
    elif kind == "penalty":
        _enum(result.get("penalty_type"), PENALTY_TYPES, "penalty_type")
        _text(result.get("penalty_type_as_published"), "penalty_type_as_published", limit=500)
        _enum(result.get("amount_status"), AMOUNT_STATUS, "amount_status")
        amount = result.get("amount_as_published")
        if amount is not None and not isinstance(amount, str):
            _fail("amount_as_published is kept as the published text, never a converted number")
        if result["amount_status"] == "stated" and not amount:
            _fail("a stated amount carries its published text")
        if result["amount_status"] == "not_published" and amount:
            _fail("an unpublished amount carries no figure")
        if result.get("currency") is not None and not re.fullmatch(r"[A-Z]{3}", str(result["currency"])):
            _fail("currency is the ISO 4217 code of the published currency")
        _date(result.get("imposed_on"), "imposed_on")
    elif kind == "appeal":
        _text(result.get("forum_as_published"), "forum_as_published", limit=500)
        _date(result.get("lodged_on"), "lodged_on")
        _date(result.get("decided_on"), "decided_on")
        docket = result.get("court_docket")
        if docket is not None and (not isinstance(docket, dict) or set(docket) - {"court", "docket_number"}):
            _fail("court_docket is a citation: court/docket_number")
    result["unknowns"] = compute_unknowns(result)
    return result


def record(kind: str, record_key: str, source: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """Build and validate a record; convenient for adapters and fixtures."""
    return validate_record({"contract": CONTRACT, "kind": kind, "record_key": record_key, "source": source, **fields})


def authority_key(authority: str) -> str:
    return f"enforcement:authority:{authority}"


def action_key(authority: str, number: str) -> str:
    return f"enforcement:action:{authority}:{str(number).strip()}"


def child_key(kind: str, action: str, *parts: Any) -> str:
    """Stable key of a respondent, notice, penalty or appeal under its action (a digest of its published identity)."""
    authority, number = action.split(":", 3)[2:4]
    short = {"enforcement_decision": "decision"}.get(kind, kind)
    return f"enforcement:{short}:{authority}:{number}:{digest([action, *parts])[:16]}"


def schema() -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / "noesis-enforcement-record-v1.json").read_text())


def register_schemas(conn: Any, *, principal_id: str, scopes: Any) -> list[dict[str, Any]]:
    """Register ``noesis-enforcement-record-v1`` as a module in the shared schema registry."""
    from src.kb.schema_registry import SchemaRegistry

    definition = {
        "contract": "noesis-schema-module-v1", "name": SCHEMA_NAME, "kind": "schema",
        "semantic_version": SCHEMA_VERSION, "content": schema(), "owner": "legal.enforcement", "dependencies": [],
        "compatibility_policy": "backward",
        "provenance": {"kind": "imported", "source": "contracts/schemas/jsonschema/noesis-enforcement-record-v1.json"},
        "actor": {"principal_id": principal_id, "kind": "service"},
    }
    return [SchemaRegistry(conn).register(definition, f"enforcement-schema:{SCHEMA_NAME}:{SCHEMA_VERSION}",
                                          principal_id=principal_id, scopes=scopes)]
