"""Regulatory enforcement action, respondent, decision, penalty, appeal and notice-document records (#2651, EN02).

``noesis-enforcement-record-v1`` records are *what one regulator published*
about one enforcement action outside competition law (SEC litigation releases
and administrative proceedings, FCA final notices, EPA ECHO enforcement cases,
EDPB Article 60 register entries). They are persisted by
:class:`src.kb.enforcement.EnforcementStore` as immutable revisions: one stable
record per ``record_key``, a new revision only when the published content
changes, and a correction, supersession or removal by the source is a new
revision, never a deletion.

* ``enforcement_action`` - keyed by provider and native release, notice, case
  or register-entry identifier; the authority (a source identity), the action
  type as published, the title, the legal bases cited as published, key dates
  (initiated, decided, published), the outcome **as published** with any
  settlement and the stated admission wording kept verbatim (``settled
  without admitting or denying`` is never turned into a finding), the appeal
  status as published, related court cases as citations only, facilities (EPA)
  and the publication status (published, corrected, removed by the source);
* ``respondent`` - a respondent or defendant as named: organisations keep the
  name and the identifiers the source published (CIK, FRN, FRS, LEI, company
  numbers); natural persons follow the EN01 minimisation decision (an
  action-scoped pseudonym and the role only - see ``MINIMISATION``);
* ``decision`` - one published decision, order, judgment or notice: type as
  published, date, outcome and corrective measures as published;
* ``penalty`` - one published monetary sanction: type, amount text, amount and
  currency as published, the stage (before or after a settlement discount) and
  whether the figure was published at all; nothing is converted or summed;
* ``appeal`` - a published appeal or referral: forum, reference and status as
  published;
* ``notice_document`` - a versioned notice document: URL, title, type, date and
  the digest of the bytes retrieved; a corrected notice is a new revision.

No record carries a risk or compliance score, an inference of wrongdoing, a
finding merged from a settled outcome or a personal profile: such fields are
rejected outright, and personal fields (dates of birth, addresses, individual
reference numbers) are rejected at write time. Absent values stay ``None`` and
are listed in ``unknowns``.
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
KINDS = ("enforcement_action", "respondent", "decision", "penalty", "appeal", "notice_document")
PROVIDERS = ("us-sec", "uk-fca", "us-epa-echo", "edpb")
# Authorities are source identities: the three national regulators, and EU supervisory authorities by the lead
# authority code the EDPB register publishes (``eu-sa-ie``).
FIXED_AUTHORITIES = ("us-sec", "uk-fca", "us-epa")
_EU_SA = re.compile(r"^eu-sa-[a-z]{2}$")
ACTION_TYPES = ("civil_action", "administrative_proceeding", "final_notice", "civil_judicial_case",
                "administrative_case", "criminal_case", "one_stop_shop_decision")
PARTY_TYPES = ("organisation", "natural_person")
PENALTY_TYPES = ("civil_penalty", "disgorgement", "prejudgment_interest", "financial_penalty", "fine",
                 "federal_penalty", "state_local_penalty", "sep_cost", "cost_recovery", "compliance_action_cost",
                 "restitution", "other")
PENALTY_STATUS = ("stated", "not_published")
PENALTY_STAGES = ("as_imposed", "after_settlement_discount", "before_settlement_discount")
PUBLICATION_STATUS = ("published", "corrected", "removed_by_source")
# Keys no record may carry (#2651 exclusions): scores, inferred wrongdoing, findings merged from settlements and
# profiles of named individuals.
FORBIDDEN_FIELDS = ("risk_score", "compliance_score", "risk_rating", "severity_score", "wrongdoing",
                    "inferred_wrongdoing", "finding_of_wrongdoing", "guilty", "found_liable", "liability_finding",
                    "violation_found", "culpability", "recidivism", "person_profile", "profile", "prediction",
                    "legal_advice")
# Personal fields never stored for anyone (EN01 minimisation decision).
PERSONAL_FIELDS = ("date_of_birth", "birth_date", "dob", "age", "address", "home_address", "email", "phone",
                   "nationality", "individual_reference_number", "irn", "crd_number")
MINIMISATION = {
    "organisations": "name as published and the identifiers the source published (CIK, FRN, FRS registry id, LEI, "
                     "company number); matched to entities only through reviewable identity (EN07)",
    "natural_persons": "an action-scoped pseudonym ('natural person N') and the role as published only; the name, "
                       "individual reference numbers, CRD numbers, addresses, ages and dates of birth are never "
                       "stored; names are replaced by the pseudonym in every quoted text; never matched, "
                       "aggregated across actions, used as a query key or used as a monitor target",
    "individual_only_actions": "an action whose only respondents are natural persons is not recorded; the "
                               "acquisition receipt counts it as withheld",
    "retention": "pseudonyms are not linkable across actions (derived from the action and ordinal, never the "
                 "name); records are retained as immutable revisions like every other record",
    "who_may_query": "knowledge:legal:read holders see organisations and pseudonyms; nobody can query or monitor a "
                     "natural person",
}
_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_PSEUDONYM = re.compile(r"^natural person \d{1,3}$")
_AMOUNT = re.compile(r"^-?\d+(\.\d+)?$")
_SOURCE_FIELDS = {"provider", "provider_record_id", "url", "locator", "publisher", "license", "revision", "note",
                  "retrieved_at_ms", "raw_sha256", "evidence_origin", "format"}
_COMMON = {"contract", "kind", "record_key", "source", "native", "unknowns"}
_CHILD = {"action_key", "authority"}
_KIND_FIELDS = {
    "enforcement_action": {"authority", "authority_as_published", "native_id", "identifiers", "action_type",
                           "action_type_as_published", "title", "legal_bases", "initiated_on", "decided_on",
                           "published_on", "outcome_as_published", "settled", "admission_wording",
                           "appeal_status_as_published", "court_cases", "related_references", "concerned_authorities",
                           "facilities", "url", "page_revision", "publication_status", "withheld_natural_persons"},
    "respondent": _CHILD | {"ordinal", "party_type", "name_as_published", "pseudonym", "role_as_published",
                            "identifiers", "country"},
    "decision": _CHILD | {"decision_type_as_published", "decided_on", "outcome_as_published", "settled",
                          "admission_wording", "corrective_measures", "legal_provisions", "document_url"},
    "penalty": _CHILD | {"decision_key", "respondent_key", "penalty_type", "penalty_type_as_published",
                         "amount_as_published", "amount", "currency", "status", "stage", "discount_as_published"},
    "appeal": _CHILD | {"forum_as_published", "reference", "status_as_published", "stated_on"},
    "notice_document": _CHILD | {"url", "title", "document_type_as_published", "published_on", "content_sha256",
                                 "bytes", "media_type", "language"},
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


def _text(value: Any, field: str, *, optional: bool = False, limit: int = 8000) -> Any:
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


def _list(value: Any, field: str) -> None:
    if value is not None and not isinstance(value, list):
        _fail(f"{field} must be a list")


def _identifiers(values: Any, field: str) -> None:
    if values is None:
        return
    _list(values, field)
    for item in values:
        if not isinstance(item, dict) or set(item) - {"scheme", "value", "type_as_published"} or not item.get("scheme"):
            _fail(f"{field} items use scheme/value/type_as_published")
        _text(item.get("value"), f"{field}.value", limit=500)


def authority_valid(value: Any) -> bool:
    return value in FIXED_AUTHORITIES or bool(_EU_SA.fullmatch(str(value or "")))


def _keys(value: Any, names: tuple[str, ...]) -> list[str]:
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in names:
                found.append(str(key))
            found += _keys(item, names)
    elif isinstance(value, list):
        for item in value:
            found += _keys(item, names)
    return found


def compute_unknowns(record: dict[str, Any]) -> list[str]:
    """Explicitly unknown fields; never defaults."""
    kind, unknowns = record["kind"], []
    if kind == "enforcement_action":
        for field in ("title", "outcome_as_published"):
            if record.get(field) in (None, ""):
                unknowns.append(field)
        if not any(record.get(f) for f in ("initiated_on", "decided_on", "published_on")):
            unknowns.append("dates")
        if not record.get("legal_bases"):
            unknowns.append("legal_bases")
        if record.get("settled") is None:
            unknowns.append("settled")
    elif kind == "respondent":
        if record.get("party_type") == "organisation" and not record.get("identifiers"):
            unknowns.append("identifiers")
    elif kind == "decision":
        for field in ("decided_on", "outcome_as_published"):
            if record.get(field) in (None, ""):
                unknowns.append(field)
    elif kind == "penalty":
        if record.get("status") == "not_published":
            unknowns.append("amount")
        elif record.get("amount") is None:
            unknowns.append("amount_figure")
        if record.get("status") == "stated" and not record.get("currency"):
            unknowns.append("currency")
    elif kind == "appeal":
        for field in ("reference", "stated_on"):
            if record.get(field) in (None, ""):
                unknowns.append(field)
    elif kind == "notice_document":
        for field in ("published_on", "content_sha256"):
            if record.get(field) in (None, ""):
                unknowns.append(field)
    return sorted(set(unknowns))


def validate_record(record: Any) -> dict[str, Any]:
    """Validate and return a canonical copy with ``unknowns`` recomputed; minimisation is enforced here."""
    if not isinstance(record, dict):
        _fail("enforcement record must be an object")
    if _keys({k: v for k, v in record.items() if k != "native"}, FORBIDDEN_FIELDS):
        _fail("enforcement records hold what a regulator published; no risk or compliance score, inference of "
              "wrongdoing, finding merged from a settlement or personal profile is stored", "assessment_forbidden")
    if _keys(record, PERSONAL_FIELDS):
        _fail("personal fields (dates of birth, addresses, individual reference numbers) are never stored",
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
    if not authority_valid(result.get("authority")):
        _fail("authority is us-sec, uk-fca, us-epa or an EU supervisory authority code (eu-sa-xx)")
    if kind != "enforcement_action":
        _text(result.get("action_key"), "action_key", limit=1000)
        if not result["action_key"].startswith("enforcement:action:"):
            _fail("action_key names an enforcement action")
    for field in ("settled",):
        if field in result and result[field] is not None and not isinstance(result[field], bool):
            _fail("settled is true only when the source states a settlement or consent, otherwise null")
    if kind == "enforcement_action":
        _text(result.get("native_id"), "native_id", limit=300)
        _enum(result.get("action_type"), ACTION_TYPES, "action_type")
        _text(result.get("action_type_as_published"), "action_type_as_published", limit=500)
        _identifiers(result.get("identifiers"), "identifiers")
        for field in ("initiated_on", "decided_on", "published_on"):
            _date(result.get(field), field)
        for field in ("legal_bases", "related_references", "concerned_authorities", "facilities"):
            _list(result.get(field), field)
        for docket in result.get("court_cases") or []:
            if not isinstance(docket, dict) or set(docket) - {"court", "docket_number", "note"} \
                    or not docket.get("docket_number"):
                _fail("court_cases are citations: court/docket_number/note")
        for facility in result.get("facilities") or []:
            if not isinstance(facility, dict) or set(facility) - {"frs_registry_id", "name_as_published", "city",
                                                                   "state", "latitude_as_published",
                                                                   "longitude_as_published"}:
                _fail("facilities keep frs_registry_id/name_as_published/city/state and published coordinates")
        _https(result.get("url"), "url")
        result["publication_status"] = result.get("publication_status") or "published"
        _enum(result["publication_status"], PUBLICATION_STATUS, "publication_status")
        if result.get("admission_wording") is not None:
            _text(result["admission_wording"], "admission_wording", limit=1000)
        if result.get("withheld_natural_persons") is not None and \
                not isinstance(result["withheld_natural_persons"], int):
            _fail("withheld_natural_persons is a count")
    elif kind == "respondent":
        _enum(result.get("party_type"), PARTY_TYPES, "party_type")
        _text(result.get("role_as_published"), "role_as_published", limit=500)
        if not isinstance(result.get("ordinal"), int):
            _fail("ordinal is the respondent's position in the action")
        if result["party_type"] == "natural_person":
            if result.get("name_as_published") or result.get("identifiers"):
                _fail("a natural-person respondent keeps a pseudonym and role only", "minimisation_violation")
            if not _PSEUDONYM.fullmatch(str(result.get("pseudonym") or "")):
                _fail("a natural-person respondent carries an action-scoped pseudonym 'natural person N'",
                      "minimisation_violation")
        else:
            _text(result.get("name_as_published"), "name_as_published", limit=1000)
            if result.get("pseudonym"):
                _fail("organisations are named as published, not pseudonymised")
            _identifiers(result.get("identifiers"), "identifiers")
        if result.get("country") is not None and not re.fullmatch(r"[A-Z]{2}", str(result["country"])):
            _fail("country is an ISO 3166-1 alpha-2 code derived from the published country, or null")
    elif kind == "decision":
        _text(result.get("decision_type_as_published"), "decision_type_as_published", limit=500)
        _date(result.get("decided_on"), "decided_on")
        for field in ("corrective_measures", "legal_provisions"):
            _list(result.get(field), field)
        _https(result.get("document_url"), "document_url")
    elif kind == "penalty":
        _enum(result.get("penalty_type"), PENALTY_TYPES, "penalty_type")
        _text(result.get("penalty_type_as_published"), "penalty_type_as_published", limit=500)
        _enum(result.get("status"), PENALTY_STATUS, "status")
        if result.get("stage") is not None:
            _enum(result["stage"], PENALTY_STAGES, "stage")
        if result.get("amount") is not None and (not isinstance(result["amount"], str)
                                                 or not _AMOUNT.fullmatch(result["amount"])):
            _fail("amount is the published figure as a decimal string, never a converted value")
        if result.get("amount_as_published") is not None and not isinstance(result["amount_as_published"], str):
            _fail("amount_as_published is kept as the published text")
        if result["status"] == "not_published" and (result.get("amount") or result.get("amount_as_published")):
            _fail("a penalty whose figure is not published carries no amount")
        if result.get("currency") is not None and not re.fullmatch(r"[A-Z]{3}", str(result["currency"])):
            _fail("currency is the ISO 4217 code of the published currency")
    elif kind == "appeal":
        _text(result.get("forum_as_published"), "forum_as_published", limit=500)
        _text(result.get("status_as_published"), "status_as_published", limit=2000)
        _date(result.get("stated_on"), "stated_on")
    elif kind == "notice_document":
        _https(result.get("url"), "url")
        if not result.get("url"):
            _fail("a notice document has a URL")
        _date(result.get("published_on"), "published_on")
        if result.get("content_sha256") is not None and not re.fullmatch(r"[0-9a-f]{64}",
                                                                         str(result["content_sha256"])):
            _fail("content_sha256 is the SHA-256 of the retrieved bytes")
    result["unknowns"] = compute_unknowns(result)
    return result


def record(kind: str, record_key: str, source: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """Build and validate a record; convenient for adapters and fixtures."""
    return validate_record({"contract": CONTRACT, "kind": kind, "record_key": record_key, "source": source, **fields})


def action_key(provider: str, native_id: str) -> str:
    return f"enforcement:action:{provider}:{str(native_id).strip()}"


def child_key(kind: str, action: str, *parts: Any) -> str:
    """Stable key of a respondent, decision, penalty, appeal or document under its action."""
    provider, native = action.split(":", 3)[2:4]
    return f"enforcement:{kind}:{provider}:{native}:{digest([action, *parts])[:16]}"


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
