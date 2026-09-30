"""Competition case, case-stage, party, decision-document and state-aid-award records (#2217, CS02).

``noesis-competition-record-v1`` records sit beside the ownership record model
(:mod:`src.kb.ownership_records`): the same ``source`` object, the same
``record_key``/revision semantics and the same store
(:class:`src.kb.ownership_store.OwnershipStore`, which validates each record
by its contract). Every record is *what one authority published*:

* ``competition_case`` - keyed by authority and native case number (``M.``,
  ``AT.``, ``SA.`` numbers, a CMA slug, an FTC matter number, a DOJ case page),
  with the instrument (merger, antitrust, state aid, market investigation) and
  the instrument, title, state and sector exactly as published;
* ``case_stage`` - one published event (stage name as published, stage date,
  source document); stages are **append-only**: a stage record is never
  removed, and a later page that omits it does not delete it;
* ``case_party`` - a party's name and role exactly as published (notifying
  party, target, addressee, beneficiary, complainant, respondent, defendant),
  with any published identifiers; **no identity is resolved here**;
* ``decision_document`` - document type, date, language, URL and citation
  (CELEX, OJ reference, decision number) as published, plus the legal
  references the authority cites for it; documents are linked, not mirrored;
* ``state_aid_award`` - beneficiary as published, granting authority, aid
  instrument, amount (or range) and currency as published, award date and the
  SA measure reference; nothing is converted or aggregated.

No record carries an outcome prediction, a market-power or market-definition
assessment, an aid compatibility or legality assessment, or legal advice:
such fields are rejected outright. Absent values stay ``None`` and are listed
in ``unknowns``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src.kb.ownership_records import canonical, digest

CONTRACT = "noesis-competition-record-v1"
SCHEMA_NAME = "competition-record"
SCHEMA_VERSION = "1.0.0"
KINDS = ("competition_case", "case_stage", "case_party", "decision_document", "state_aid_award")
AUTHORITIES = ("ec", "uk-cma", "us-ftc", "us-doj")
PROVIDERS = ("ec-competition", "eu-tam", "uk-cma", "us-ftc", "us-doj")
PROVIDER_AUTHORITY = {"ec-competition": "ec", "eu-tam": "ec", "uk-cma": "uk-cma", "us-ftc": "us-ftc",
                      "us-doj": "us-doj"}
INSTRUMENTS = ("merger", "antitrust", "state_aid", "market_investigation")
PARTY_ROLES = ("notifying_party", "target", "addressee", "beneficiary", "complainant", "respondent", "defendant",
               "named_in_title", "other")
DATE_STATUS = ("stated", "unknown")
AWARD_STATUS = ("published", "corrected", "withdrawn")
FORBIDDEN_FIELDS = ("outcome_prediction", "prediction", "predicted_outcome", "market_power", "market_share_estimate",
                    "dominance_assessment", "compatibility_assessment", "legality_assessment", "legal_advice",
                    "risk_score", "determination")
_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_SOURCE_FIELDS = {"provider", "provider_record_id", "url", "locator", "publisher", "license", "revision", "note",
                  "retrieved_at_ms", "raw_sha256", "evidence_origin"}
_COMMON = {"contract", "kind", "record_key", "source", "native", "unknowns"}
_KIND_FIELDS = {
    "competition_case": {"authority", "case_number", "instrument", "instrument_as_published", "title",
                         "state_as_published", "sectors", "opened_on", "closed_on", "member_state", "case_url",
                         "related_case_numbers", "court_dockets", "legal_references", "page_revision"},
    "case_stage": {"case_key", "authority", "case_number", "ordinal", "stage_as_published", "stage_date",
                   "date_status", "document", "page_revision"},
    "case_party": {"case_key", "authority", "case_number", "name_as_published", "role", "role_as_published",
                   "country_as_published", "country", "identifiers"},
    "decision_document": {"case_key", "authority", "case_number", "document_type_as_published", "document_date",
                          "language", "url", "citation", "legal_references"},
    "state_aid_award": {"award_id", "member_state", "sa_number", "beneficiary_name_as_published",
                        "beneficiary_identifiers", "beneficiary_type_as_published", "granting_authority",
                        "aid_instrument_as_published", "aid_objective_as_published", "amount_as_published",
                        "amount_range_as_published", "currency", "award_date", "region_as_published",
                        "sector_as_published", "status", "status_as_published"},
}
SCHEMA_DIR = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema"


class CompetitionRecordError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _fail(message: str, code: str = "invalid_competition_record") -> None:
    raise CompetitionRecordError(code, message)


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


def _identifiers(values: Any, field: str) -> None:
    if values is None:
        return
    if not isinstance(values, list):
        _fail(f"{field} must be a list")
    for item in values:
        if not isinstance(item, dict) or set(item) - {"scheme", "value", "type_as_published"} or not item.get("scheme"):
            _fail(f"{field} items use scheme/value/type_as_published")
        _text(item.get("value"), f"{field}.value", limit=500)


def _forbidden(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(k).lower() in FORBIDDEN_FIELDS or _forbidden(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_forbidden(v) for v in value)
    return False


def compute_unknowns(record: dict[str, Any]) -> list[str]:
    """Explicitly unknown fields; never defaults."""
    kind, unknowns = record["kind"], []
    if kind == "competition_case":
        for field in ("title", "opened_on", "state_as_published"):
            if record.get(field) in (None, ""):
                unknowns.append(field)
    elif kind == "case_stage":
        if record.get("date_status") != "stated":
            unknowns.append("stage_date")
        if not record.get("document"):
            unknowns.append("document")
    elif kind == "case_party":
        if not record.get("identifiers"):
            unknowns.append("identifiers")
        if not record.get("country"):
            unknowns.append("country")
    elif kind == "decision_document":
        for field in ("document_date", "language"):
            if record.get(field) in (None, ""):
                unknowns.append(field)
        if not any((record.get("citation") or {}).values()):
            unknowns.append("citation")
    elif kind == "state_aid_award":
        if record.get("amount_as_published") in (None, "") and not record.get("amount_range_as_published"):
            unknowns.append("amount")
        for field in ("currency", "award_date", "sa_number"):
            if record.get(field) in (None, ""):
                unknowns.append(field)
        if not record.get("beneficiary_identifiers"):
            unknowns.append("beneficiary_identifiers")
    return sorted(set(unknowns))


def validate_record(record: Any) -> dict[str, Any]:
    """Validate and return a canonical copy with ``unknowns`` recomputed."""
    if not isinstance(record, dict):
        _fail("competition record must be an object")
    if _forbidden({k: v for k, v in record.items() if k != "native"}):
        _fail("competition records hold what an authority published; no outcome prediction, market-power, aid "
              "compatibility or legality assessment, or legal advice is stored", "assessment_forbidden")
    if record.get("contract") != CONTRACT:
        _fail("unsupported competition record contract", "schema_drift")
    kind = _enum(record.get("kind"), KINDS, "kind")
    extra = set(record) - _COMMON - _KIND_FIELDS[kind]
    if extra:
        _fail(f"unsupported {kind} field: " + ", ".join(sorted(extra)))
    key = _text(record.get("record_key"), "record_key", limit=1000)
    if not key.startswith("competition:"):
        _fail("competition record keys start with 'competition:'")
    source = record.get("source")
    if not isinstance(source, dict) or set(source) - _SOURCE_FIELDS:
        _fail("source uses " + "/".join(sorted(_SOURCE_FIELDS)))
    _enum(source.get("provider"), PROVIDERS, "source.provider")
    _text(source.get("provider_record_id"), "source.provider_record_id", limit=1000)
    _https(source.get("url"), "source.url")
    result = json.loads(canonical(record))
    if result.get("native") is not None and not isinstance(result["native"], dict):
        _fail("native preserves the published wording as an object")
    if kind in {"competition_case", "case_stage", "case_party", "decision_document"}:
        _enum(result.get("authority"), AUTHORITIES, "authority")
        _text(result.get("case_number"), "case_number", limit=200)
        if kind != "competition_case":
            _text(result.get("case_key"), "case_key", limit=1000)
    if kind == "competition_case":
        _enum(result.get("instrument"), INSTRUMENTS, "instrument")
        _text(result.get("instrument_as_published"), "instrument_as_published", limit=500)
        _date(result.get("opened_on"), "opened_on")
        _date(result.get("closed_on"), "closed_on")
        _https(result.get("case_url"), "case_url")
        for field in ("sectors", "related_case_numbers", "legal_references"):
            if result.get(field) is not None and not isinstance(result[field], list):
                _fail(f"{field} must be a list")
        for docket in result.get("court_dockets") or []:
            if not isinstance(docket, dict) or set(docket) - {"court", "docket_number", "note"} \
                    or not docket.get("docket_number"):
                _fail("court_dockets are citations: court/docket_number/note")
    elif kind == "case_stage":
        _text(result.get("stage_as_published"), "stage_as_published", limit=2000)
        _date(result.get("stage_date"), "stage_date")
        result["date_status"] = result.get("date_status") or ("stated" if result.get("stage_date") else "unknown")
        _enum(result["date_status"], DATE_STATUS, "date_status")
        if result["date_status"] == "unknown" and result.get("stage_date"):
            _fail("an unknown stage date carries no date")
        document = result.get("document")
        if document is not None and (not isinstance(document, dict) or set(document) - {"title", "url", "type"}):
            _fail("stage document uses title/url/type")
        if document:
            _https(document.get("url"), "document.url")
    elif kind == "case_party":
        _text(result.get("name_as_published"), "name_as_published", limit=1000)
        _enum(result.get("role"), PARTY_ROLES, "role")
        _text(result.get("role_as_published"), "role_as_published", limit=500)
        _identifiers(result.get("identifiers"), "identifiers")
        if result.get("country") is not None and not re.fullmatch(r"[A-Z]{2}", str(result["country"])):
            _fail("country is an ISO 3166-1 alpha-2 code derived from the published country, or null")
    elif kind == "decision_document":
        _text(result.get("document_type_as_published"), "document_type_as_published", limit=500)
        _date(result.get("document_date"), "document_date")
        _https(result.get("url"), "url")
        citation = result.get("citation")
        if citation is not None and (not isinstance(citation, dict) or set(citation) - {
                "celex", "oj_reference", "decision_number", "eli", "docket_number"}):
            _fail("citation uses celex/oj_reference/decision_number/eli/docket_number")
        if result.get("legal_references") is not None and not isinstance(result["legal_references"], list):
            _fail("legal_references must be a list of references as published")
    elif kind == "state_aid_award":
        _text(result.get("award_id"), "award_id", limit=200)
        if not re.fullmatch(r"[A-Z]{2}", str(result.get("member_state") or "")):
            _fail("member_state is the published two-letter country code")
        _text(result.get("beneficiary_name_as_published"), "beneficiary_name_as_published", limit=1000)
        _identifiers(result.get("beneficiary_identifiers"), "beneficiary_identifiers")
        if result.get("sa_number") is not None and not re.fullmatch(r"SA\.\d{1,6}", str(result["sa_number"])):
            _fail("sa_number is an SA measure reference as published (SA.nnnnn)")
        for field in ("amount_as_published", "amount_range_as_published"):
            if result.get(field) is not None and not isinstance(result[field], str):
                _fail(f"{field} is kept as the published text, never a converted number")
        _date(result.get("award_date"), "award_date")
        _enum(result.get("status"), AWARD_STATUS, "status")
    result["unknowns"] = compute_unknowns(result)
    return result


def record(kind: str, record_key: str, source: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """Build and validate a record; convenient for adapters and fixtures."""
    return validate_record({"contract": CONTRACT, "kind": kind, "record_key": record_key, "source": source, **fields})


def case_key(authority: str, case_number: str) -> str:
    return f"competition:case:{authority}:{str(case_number).strip()}"


def child_key(kind: str, case: str, *parts: Any) -> str:
    """Stable key of a stage, party or document under its case (a digest of its published identity)."""
    authority, number = case.split(":", 3)[2:4]
    return f"competition:{kind}:{authority}:{number}:{digest([case, *parts])[:16]}"


def award_key(member_state: str, award_id: str) -> str:
    return f"competition:award:eu-tam:{member_state}:{str(award_id).strip()}"


def schema() -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / "noesis-competition-record-v1.json").read_text())


def register_schemas(conn: Any, *, principal_id: str, scopes: Any) -> list[dict[str, Any]]:
    """Register ``noesis-competition-record-v1`` as a module in the shared schema registry."""
    from src.kb.schema_registry import SchemaRegistry

    definition = {
        "contract": "noesis-schema-module-v1", "name": SCHEMA_NAME, "kind": "schema",
        "semantic_version": SCHEMA_VERSION, "content": schema(), "owner": "ownership.competition", "dependencies": [],
        "compatibility_policy": "backward",
        "provenance": {"kind": "imported", "source": "contracts/schemas/jsonschema/noesis-competition-record-v1.json"},
        "actor": {"principal_id": principal_id, "kind": "service"},
    }
    return [SchemaRegistry(conn).register(definition, f"competition-schema:{SCHEMA_NAME}:{SCHEMA_VERSION}",
                                          principal_id=principal_id, scopes=scopes)]
