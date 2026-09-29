"""Versioned registered-trial, arm, outcome, result, regulatory and link records.

``noesis-clinical-record-v1`` is the provider-neutral shape every clinical
adapter emits (``src/ingestion/clinical_providers.py``). It keeps apart what
clinical sources blur together:

* a *registration* (``registered-trial``: what a registry says a trial is and
  plans, one revision per registry version),
* a *results posting* (``result-posting``: results a registry published, as
  reported, never inferred from a paper), and
* a *publication* (never a record here: a ``registry-link`` to an existing
  ``documents`` revision harvested by the Science providers).

The three never stand in for each other: a trial with a linked paper still
shows "results not posted" until a registry posts them, and a posting is not a
publication. Arms and outcome measures are their own records so an outcome's
role and time frame keep a change history across registry versions.
Regulatory records (approvals, label revisions, safety communications,
adverse-event report summaries, authorisation status) carry the provider's own
disclaimer; adverse-event figures are *reporting counts*, never incidence,
risk or causation.

Records are revisioned per the C01.2 store rules: one authoritative store per
record type (this module), stable content-derived ids, immutable revisions
addressable by number, a native-revision link (the registry version, label
version or EMA revision number) on every revision, and namespace scoping on
every row. The same trial in two registries is two records joined by a
``registry-link`` with the identifier evidence; it is never merged.

Absent values stay ``None`` and are listed in ``unknowns``; nothing is
defaulted. No record carries medical advice, dosing or a treatment
recommendation.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date
from typing import Any

CONTRACT = "noesis-clinical-record-v1"
READ_SCOPE = "knowledge:clinical:read"
WRITE_SCOPE = "knowledge:clinical:write"
REVIEW_SCOPE = "knowledge:clinical:review"
INGEST_SCOPE = "knowledge:ingestion:execute"

REGISTRIES = ("ctgov", "ctis", "euctr", "prospero")
REGULATORS = ("openfda", "ema")
PROVIDERS = REGISTRIES + REGULATORS + ("noesis",)
RECORD_KINDS = (
    "registered-trial", "trial-arm", "outcome-measure", "result-posting",
    "regulatory-record", "registry-link", "review-registration",
)
TRIAL_STATUSES = (
    "not-yet-recruiting", "recruiting", "enrolling-by-invitation", "active-not-recruiting",
    "suspended", "terminated", "completed", "withdrawn", "authorised", "ongoing", "ended",
    "halted", "not-authorised", "unknown",
)
PHASES = ("early-phase-1", "phase-1", "phase-2", "phase-3", "phase-4", "not-applicable", "unknown")
ALLOCATIONS = ("randomized", "non-randomized", "not-applicable", "unknown")
MASKINGS = ("none", "single", "double", "triple", "quadruple", "unknown")
OUTCOME_ROLES = ("primary", "secondary", "other")
REGULATORY_KINDS = ("approval", "label-revision", "safety-communication", "adverse-event-summary",
                    "authorisation-status")
# ``series-*`` links join a surveillance series (src/kb/surveillance.py, from_record provider ``surveillance``) to a
# publication or a registered trial by explicit dataset citation only (#1917, I09).
# ``medicine-*`` links join a medicines-regulation record (src/kb/clinical_medicines.py) to a trial or publication it
# explicitly cites, or to FAERS reporting counts through a reviewed substance identity (#2214, MR09).
LINK_KINDS = ("registry-registry", "registry-publication", "registry-review", "series-publication", "series-trial",
              "medicine-trial", "medicine-publication", "medicine-faers")
EVIDENCE_KINDS = (
    "registry-declared-secondary-id", "registry-declared-reference", "secondary-source-identifier",
    "paper-family", "abstract-mention", "user-supplied", "dataset-citation", "trial-declared-dataset",
    "regulator-cited-reference", "reviewed-substance-identity",
)
LINK_STATUSES = ("accepted", "candidate", "rejected", "target-not-acquired")
IDENTIFIER_KINDS = ("nct", "eudract", "eu-ct", "isrctn", "prospero", "pmid", "doi", "fda-application",
                    "ema-product", "sponsor-protocol", "who-utn", "other")
VERSION_BASES = ("registry-history", "current-record", "label-version", "revision-number", "observation",
                 "registration")
IDENTIFIER_PATTERNS = {
    "nct": re.compile(r"^NCT\d{8}$"),
    "eudract": re.compile(r"^\d{4}-\d{6}-\d{2}$"),
    "eu-ct": re.compile(r"^\d{4}-\d{6}-\d{2}-\d{2}$"),
    "isrctn": re.compile(r"^ISRCTN\d{8}$"),
    "prospero": re.compile(r"^CRD\d{8,14}$"),
    "pmid": re.compile(r"^\d{1,9}$"),
    "doi": re.compile(r"^10\.\d{4,9}/\S+$"),
    "fda-application": re.compile(r"^(NDA|ANDA|BLA)\d{6}$"),
    "ema-product": re.compile(r"^EMEA/H/C/\d{6}$"),
}
# Which registry assigns an identifier kind (for cross-registry linking).
REGISTRY_FOR_IDENTIFIER = {"nct": "ctgov", "eudract": "euctr", "eu-ct": "ctis", "prospero": "prospero"}
PRIMARY_IDENTIFIER = {"ctgov": "nct", "euctr": "eudract", "ctis": "eu-ct", "prospero": "prospero"}
# Keys that would turn a report count into an effect or a record into advice.
FORBIDDEN_KEYS = frozenset({
    "incidence", "rate", "risk", "relative_risk", "odds_ratio", "causation", "causal", "causality",
    "dose", "dosing", "dosage", "recommendation", "recommended_dose", "advice", "treatment_recommendation",
    "grade", "verdict",
})
COUNT_SEMANTICS = ("Number of adverse-event reports in FAERS that mention the term for the queried product. "
                   "Reporting counts only: not incidence, not a rate, and not evidence that the product "
                   "caused the event.")
_DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
_KIND_FIELDS = {
    "registered-trial": {
        "registry", "identifier", "title", "source_url", "native_version", "status", "phase", "sponsor",
        "secondary_identifiers", "conditions", "interventions", "design", "registration", "member_states",
        "declared_references", "results_indicator", "arm_keys", "outcome_keys", "brief_summary",
    },
    "trial-arm": {"registry", "identifier", "arm_key", "label", "arm_type", "description", "interventions",
                  "source_url", "native_version", "present"},
    "outcome-measure": {"registry", "identifier", "outcome_key", "measure", "role", "time_frame", "description",
                        "source_url", "native_version", "present", "locator"},
    "result-posting": {"registry", "identifier", "source_url", "native_version", "posted", "outcome_results",
                       "adverse_events", "results_summary_url", "content_acquired", "locator", "participants"},
    "regulatory-record": {"authority", "provider", "regulatory_kind", "native_id", "title", "source_url",
                          "native_version", "product", "dates", "status", "sections", "omitted_sections",
                          "documents", "submissions", "counts", "count_query", "reports_total",
                          "count_semantics", "disclaimer", "mesh_terms"},
    "registry-link": {"link_kind", "from_record", "to", "evidence_kind", "evidence", "status", "native_version",
                      "review"},
    "review-registration": {"registry", "identifier", "title", "source_url", "native_version", "review_question",
                            "condition", "population", "interventions", "comparators", "outcomes",
                            "registration_date", "status", "source", "protocol_link"},
}
_COMMON = {"contract", "record_kind", "unknowns"}
# Record families that extend this record model under their own contract and share this store (C01.2): the module
# provides ``validate_extension_record``, ``extension_identity`` and ``extension_amendments``.
EXTENSION_CONTRACTS = {"noesis-clinical-medicines-record-v1": "src.kb.clinical_medicines"}


def _extension(record):
    import importlib

    module = EXTENSION_CONTRACTS.get(record.get("contract")) if isinstance(record, dict) else None
    return importlib.import_module(module) if module else None


class ClinicalRecordError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _fail(message, code="invalid_clinical_record"):
    raise ClinicalRecordError(code, message)


def _text(value, field, *, optional=False, limit=20000):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _fail(f"{field} must be nonempty bounded text")
    return value


def _enum(value, allowed, field):
    if value not in allowed:
        _fail(f"{field} must be one of {', '.join(allowed)}")
    return value


def _date(value, field):
    if value is not None and (not isinstance(value, str) or not _DATE.fullmatch(value)):
        _fail(f"{field} must be YYYY-MM or YYYY-MM-DD")
    return value


def identifier(kind, value):
    """Validate one identifier; unknown kinds are kept as ``other`` verbatim."""
    _enum(kind, IDENTIFIER_KINDS, "identifier kind")
    value = _text(value, "identifier value", limit=500).strip()
    if kind == "doi":
        value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I).lower()
    pattern = IDENTIFIER_PATTERNS.get(kind)
    if pattern and not pattern.fullmatch(value):
        _fail(f"{value!r} is not a valid {kind} identifier", "invalid_identifier")
    return value


def _forbidden(value, path="record"):
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                _fail(f"{path}.{key}: clinical records carry no incidence, causation, dosing, grade or advice",
                      "outside_boundary")
            _forbidden(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _forbidden(child, f"{path}[{index}]")


def _native_version(value, *, required=True):
    if value is None:
        if required:
            _fail("native_version is required (registry version, label version or observation)")
        return None
    if not isinstance(value, dict) or set(value) - {"version", "date", "basis"}:
        _fail("native_version uses version/date/basis")
    if value.get("version") is not None and not isinstance(value["version"], str):
        _fail("native_version.version is a string")
    _date(value.get("date"), "native_version.date")
    _enum(value.get("basis"), VERSION_BASES, "native_version.basis")
    return value


def _trial(record):
    _enum(record.get("registry"), ("ctgov", "ctis", "euctr"), "registry")
    identifier(PRIMARY_IDENTIFIER[record["registry"]], record.get("identifier"))
    _text(record.get("title"), "title", optional=True, limit=4000)
    status = record.get("status") or {}
    if not isinstance(status, dict) or set(status) - {"native", "normalized", "locator", "as_of"}:
        _fail("status uses native/normalized/locator/as_of")
    _enum(status.get("normalized", "unknown"), TRIAL_STATUSES, "status.normalized")
    phase = record.get("phase") or {}
    if not isinstance(phase, dict) or set(phase) - {"native", "normalized", "locator"}:
        _fail("phase uses native/normalized/locator")
    for value in phase.get("normalized") or ["unknown"]:
        _enum(value, PHASES, "phase.normalized")
    design = record.get("design") or {}
    if not isinstance(design, dict) or set(design) - {
        "study_type", "allocation", "masking", "masked_roles", "intervention_model", "primary_purpose",
        "enrollment_planned", "enrollment_actual", "controlled", "locator", "source_member_state",
    }:
        _fail("unsupported design field")
    _enum(design.get("allocation", "unknown"), ALLOCATIONS, "design.allocation")
    _enum(design.get("masking", "unknown"), MASKINGS, "design.masking")
    for key in ("enrollment_planned", "enrollment_actual"):
        if design.get(key) is not None and (type(design[key]) is not int or design[key] < 0):
            _fail(f"design.{key} is a nonnegative integer or unknown (null)")
    for item in record.get("secondary_identifiers") or []:
        if not isinstance(item, dict) or set(item) - {"kind", "value", "declared_by", "locator", "domain"}:
            _fail("secondary identifiers use kind/value/declared_by/locator/domain")
        item["value"] = identifier(item["kind"], item.get("value"))
    registration = record.get("registration") or {}
    if not isinstance(registration, dict) or set(registration) - {
        "first_submitted", "first_posted", "start_date", "primary_completion_date", "completion_date",
        "prospective", "locator", "results_first_posted", "last_update_posted",
    }:
        _fail("unsupported registration field")
    for key, value in registration.items():
        if key not in {"prospective", "locator"}:
            _date(value, f"registration.{key}")
    if registration.get("prospective") not in (True, False, None):
        _fail("registration.prospective is true, false or unknown (null)")
    for state in record.get("member_states") or []:
        if not isinstance(state, dict) or set(state) - {"state", "authority", "status", "decision", "decision_date",
                                                         "locator"}:
            _fail("member_states use state/authority/status/decision/decision_date/locator")
        _date(state.get("decision_date"), "member_states.decision_date")
    for reference in record.get("declared_references") or []:
        if not isinstance(reference, dict) or set(reference) - {"pmid", "doi", "type", "citation", "locator"}:
            _fail("declared references use pmid/doi/type/citation/locator")
        if reference.get("pmid"):
            reference["pmid"] = identifier("pmid", reference["pmid"])
        if reference.get("doi"):
            reference["doi"] = identifier("doi", reference["doi"])
    indicator = record.get("results_indicator") or {}
    if indicator.get("has_results") not in (True, False, None):
        _fail("results_indicator.has_results is true, false or unknown")
    for key in ("outcome_results", "adverse_events", "publications", "publication"):
        if key in record:
            _fail(f"a registration never carries {key}; model a result posting or a publication link",
                  "kind_confusion")


def _arm(record):
    _enum(record.get("registry"), ("ctgov", "ctis", "euctr"), "registry")
    identifier(PRIMARY_IDENTIFIER[record["registry"]], record.get("identifier"))
    _text(record.get("arm_key"), "arm_key", limit=500)
    _text(record.get("label"), "label", limit=2000)
    if record.get("present") not in (True, False):
        _fail("arm.present states whether the arm is in this registry version")


def _outcome(record):
    _enum(record.get("registry"), ("ctgov", "ctis", "euctr"), "registry")
    identifier(PRIMARY_IDENTIFIER[record["registry"]], record.get("identifier"))
    _text(record.get("outcome_key"), "outcome_key", limit=500)
    _text(record.get("measure"), "measure", limit=4000)
    _enum(record.get("role"), OUTCOME_ROLES, "role")
    _text(record.get("time_frame"), "time_frame", optional=True, limit=2000)
    if record.get("present") not in (True, False):
        _fail("outcome.present states whether the outcome is in this registry version")


def _result(record):
    _enum(record.get("registry"), ("ctgov", "ctis", "euctr"), "registry")
    identifier(PRIMARY_IDENTIFIER[record["registry"]], record.get("identifier"))
    posted = record.get("posted") or {}
    if not isinstance(posted, dict) or set(posted) - {"first_posted", "first_submitted", "last_update", "locator"}:
        _fail("posted uses first_posted/first_submitted/last_update/locator")
    for key in ("first_posted", "first_submitted", "last_update"):
        _date(posted.get(key), f"posted.{key}")
    if record.get("content_acquired") not in (True, False):
        _fail("content_acquired states whether result content was acquired or only its availability")
    for key in ("publication", "doi", "pmid", "journal"):
        if key in record:
            _fail("a result posting is not a publication; link the paper separately", "kind_confusion")


def _regulatory(record):
    _enum(record.get("provider"), REGULATORS, "provider")
    _enum(record.get("authority"), ("FDA", "EMA"), "authority")
    _enum(record.get("regulatory_kind"), REGULATORY_KINDS, "regulatory_kind")
    _text(record.get("native_id"), "native_id", limit=500)
    if record["provider"] == "openfda":
        disclaimer = record.get("disclaimer")
        if not isinstance(disclaimer, dict) or not str(disclaimer.get("text") or "").strip():
            _fail("openFDA records keep openFDA's own disclaimer on the record", "missing_disclaimer")
    if record["regulatory_kind"] == "adverse-event-summary":
        if record.get("count_semantics") != COUNT_SEMANTICS:
            _fail("adverse-event summaries state that counts are reporting counts only", "count_semantics")
        for item in record.get("counts") or []:
            if not isinstance(item, dict) or set(item) != {"term", "reports"} or type(item["reports"]) is not int:
                _fail("adverse-event counts are {term, reports} integer report counts")
        if record.get("reports_total") is not None and type(record["reports_total"]) is not int:
            _fail("reports_total is an integer report count or unknown")


def _link(record):
    _enum(record.get("link_kind"), LINK_KINDS, "link_kind")
    _enum(record.get("evidence_kind"), EVIDENCE_KINDS, "evidence_kind")
    _enum(record.get("status"), LINK_STATUSES, "status")
    source = record.get("from_record")
    if not isinstance(source, dict) or set(source) != {"provider", "identifier"}:
        _fail("from_record names the registry record by provider and identifier")
    target = record.get("to")
    if not isinstance(target, dict):
        _fail("link target is required")
    if record["link_kind"] in {"registry-publication", "series-publication", "medicine-publication"}:
        if set(target) - {"document_id", "revision_id", "identifiers", "title", "family_id"} or not (
            target.get("document_id") and target.get("revision_id")
        ):
            _fail("a publication link references an existing documents revision", "invalid_publication_ref")
    else:
        if set(target) != {"registry", "identifier"}:
            _fail("registry links name the target registry and identifier")
        if record["link_kind"] == "medicine-faers":
            if target["registry"] != "openfda" or not str(target["identifier"]).startswith("faers:"):
                _fail("a FAERS link names an openFDA adverse-event summary (faers:...)")
        else:
            identifier(PRIMARY_IDENTIFIER[target["registry"]], target["identifier"])
    if not isinstance(record.get("evidence"), dict) or not record["evidence"]:
        _fail("every link records its identifier evidence", "missing_evidence")


def _review(record):
    _enum(record.get("registry"), ("prospero",), "registry")
    identifier("prospero", record.get("identifier"))
    _text(record.get("title"), "title", limit=4000)
    source = record.get("source") or {}
    if source.get("kind") not in {"user-supplied-export", "registry-api"}:
        _fail("review registrations record how they were supplied")
    _date(record.get("registration_date"), "registration_date")


_VALIDATORS = {
    "registered-trial": _trial, "trial-arm": _arm, "outcome-measure": _outcome, "result-posting": _result,
    "regulatory-record": _regulatory, "registry-link": _link, "review-registration": _review,
}


def compute_unknowns(record):
    """Explicitly unknown fields; nothing is defaulted."""
    kind, unknowns = record["record_kind"], []
    if kind == "registered-trial":
        if (record.get("status") or {}).get("normalized", "unknown") == "unknown":
            unknowns.append("status")
        if "unknown" in ((record.get("phase") or {}).get("normalized") or ["unknown"]):
            unknowns.append("phase")
        design = record.get("design") or {}
        for key in ("allocation", "masking"):
            if design.get(key, "unknown") == "unknown":
                unknowns.append(f"design.{key}")
        for key in ("enrollment_planned", "enrollment_actual"):
            if design.get(key) is None:
                unknowns.append(f"design.{key}")
        if (record.get("registration") or {}).get("prospective") is None:
            unknowns.append("registration.prospective")
        if not (record.get("sponsor") or {}).get("lead"):
            unknowns.append("sponsor")
        if (record.get("results_indicator") or {}).get("has_results") is None:
            unknowns.append("results_indicator")
    elif kind == "outcome-measure" and not record.get("time_frame"):
        unknowns.append("time_frame")
    elif kind == "result-posting" and not record.get("content_acquired"):
        unknowns.append("result_content")
    elif kind == "regulatory-record":
        if record.get("regulatory_kind") == "adverse-event-summary" and record.get("reports_total") is None:
            unknowns.append("reports_total")
        if not (record.get("dates") or {}):
            unknowns.append("dates")
    return sorted(set(unknowns))


def validate_record(record):
    """Validate and return a canonical copy with ``unknowns`` recomputed."""
    if not isinstance(record, dict):
        _fail("clinical record must be an object")
    extension = _extension(record)
    if extension is not None:
        return extension.validate_extension_record(record)
    if record.get("contract") != CONTRACT:
        _fail("unsupported clinical record contract", "schema_drift")
    kind = _enum(record.get("record_kind"), RECORD_KINDS, "record_kind")
    extra = set(record) - _COMMON - _KIND_FIELDS[kind]
    if extra:
        _fail(f"unsupported {kind} field: " + ", ".join(sorted(extra)))
    _forbidden({k: v for k, v in record.items() if k not in {"sections"}})
    copy = json.loads(canonical(record))
    _native_version(copy.get("native_version"), required=kind not in {"registry-link"})
    source_url = copy.get("source_url")
    if kind not in {"registry-link"}:
        if not isinstance(source_url, str) or not source_url.startswith("https://"):
            _fail("source_url must be an exact HTTPS source locator")
    _VALIDATORS[kind](copy)
    copy["unknowns"] = compute_unknowns(copy)
    return copy


def record(record_kind, **fields):
    """Build and validate a record; convenient for adapters and fixtures."""
    return validate_record({"contract": CONTRACT, "record_kind": record_kind, **fields})


def identity(record):
    """The native identity of a record: provider, native id and sub key."""
    extension = _extension(record)
    if extension is not None:
        return extension.extension_identity(record)
    kind = record["record_kind"]
    if kind == "registered-trial":
        return (record["registry"], record["identifier"], "")
    if kind == "trial-arm":
        return (record["registry"], record["identifier"], "arm:" + record["arm_key"])
    if kind == "outcome-measure":
        return (record["registry"], record["identifier"], "outcome:" + record["outcome_key"])
    if kind == "result-posting":
        return (record["registry"], record["identifier"], "results")
    if kind == "regulatory-record":
        return (record["provider"], record["native_id"], record["regulatory_kind"])
    if kind == "review-registration":
        return (record["registry"], record["identifier"], "")
    source, target = record["from_record"], record["to"]
    to_key = target.get("document_id") or f"{target.get('registry')}:{target.get('identifier')}"
    return (source["provider"], source["identifier"], f"link:{record['link_kind']}:{to_key}:{record['evidence_kind']}")


def record_id(namespace, record):
    return "clinical:" + digest([namespace, record["record_kind"], *identity(record)])[:32]


def normalize_text(value):
    """Comparison key for measures and terms: casefolded words only."""
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def prospective(first_submitted, start_date):
    """Registered before the trial started? ``None`` when either date is unknown."""
    if not first_submitted or not start_date:
        return None
    try:
        submitted = date.fromisoformat(first_submitted if len(first_submitted) == 10 else first_submitted + "-01")
        started = date.fromisoformat(start_date if len(start_date) == 10 else start_date + "-01")
    except ValueError:
        return None
    if len(start_date) == 7 and submitted.strftime("%Y-%m") == start_date:
        return None  # same month, day unknown: cannot tell
    return submitted <= started


# ------------------------------------------------------------------- store

_DDL = """
CREATE TABLE IF NOT EXISTS clinical_records(
 record_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, record_kind TEXT NOT NULL, provider TEXT NOT NULL,
 native_id TEXT NOT NULL, sub_key TEXT NOT NULL, revision BIGINT NOT NULL, first_seen_ms BIGINT NOT NULL,
 last_seen_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS clinical_record_revisions(
 record_id TEXT NOT NULL, revision BIGINT NOT NULL, content_hash TEXT NOT NULL, content_json TEXT NOT NULL,
 version_key TEXT NOT NULL, version_date TEXT, observed_at_ms BIGINT NOT NULL, observation_id TEXT NOT NULL,
 evidence_json TEXT NOT NULL, amendments_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
 PRIMARY KEY(record_id, revision));
CREATE TABLE IF NOT EXISTS clinical_version_conflicts(
 conflict_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, record_id TEXT NOT NULL, version_key TEXT NOT NULL,
 retained_revision BIGINT NOT NULL, conflicting_hash TEXT NOT NULL, conflicting_json TEXT NOT NULL,
 observation_id TEXT NOT NULL, reason TEXT NOT NULL, observed_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS clinical_term_annotations(
 namespace TEXT NOT NULL, record_id TEXT NOT NULL, source TEXT NOT NULL, terms_hash TEXT NOT NULL,
 terms_json TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, record_id, source, terms_hash));
CREATE TABLE IF NOT EXISTS clinical_provider_state(
 namespace TEXT NOT NULL, provider TEXT NOT NULL, last_success_ms BIGINT, last_failure_ms BIGINT,
 last_failure_code TEXT, last_observation_id TEXT, last_execution TEXT, PRIMARY KEY(namespace, provider));
CREATE TABLE IF NOT EXISTS clinical_view_dependencies(
 view_id TEXT NOT NULL, namespace TEXT NOT NULL, owner TEXT NOT NULL, record_id TEXT NOT NULL,
 revision BIGINT NOT NULL, PRIMARY KEY(view_id, record_id));
CREATE TABLE IF NOT EXISTS clinical_view_invalidations(
 view_id TEXT NOT NULL, record_id TEXT NOT NULL, from_revision BIGINT NOT NULL, to_revision BIGINT NOT NULL,
 reasons_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(view_id, record_id, to_revision));
"""


def _version_key(record):
    version = record.get("native_version") or {}
    if version.get("version") is not None:
        return f"{version.get('basis')}:{version['version']}"
    if version.get("date"):
        return f"{version.get('basis')}:{version['date']}"
    return "unversioned"


def classify_amendments(before, after):
    """What changed between two revisions of one record (for monitoring and staleness)."""
    if before is None:
        return [{"kind": "new", "path": "record"}]
    extension = _extension(after)
    if extension is not None:
        return extension.extension_amendments(before, after)
    kind, changes = after["record_kind"], []
    if canonical(before.get("native_version")) != canonical(after.get("native_version")):
        changes.append({"kind": "new_registry_version" if kind in {"registered-trial", "trial-arm", "outcome-measure"}
                        else "new_native_version", "path": "native_version",
                        "before": before.get("native_version"), "after": after.get("native_version")})
    if kind == "registered-trial":
        old, new = (before.get("status") or {}).get("normalized"), (after.get("status") or {}).get("normalized")
        if old != new:
            changes.append({"kind": "status_change", "path": "status", "before": old, "after": new})
        for path in ("design", "phase", "registration", "member_states"):
            if canonical(before.get(path)) != canonical(after.get(path)):
                changes.append({"kind": f"{path}_change", "path": path})
        if canonical(before.get("outcome_keys")) != canonical(after.get("outcome_keys")):
            changes.append({"kind": "outcomes_change", "path": "outcome_keys"})
        old_ref = {canonical(r) for r in before.get("declared_references") or []}
        if any(canonical(r) not in old_ref for r in after.get("declared_references") or []):
            changes.append({"kind": "declared_reference_added", "path": "declared_references"})
        if (before.get("results_indicator") or {}).get("has_results") != (after.get("results_indicator") or {}).get(
                "has_results"):
            changes.append({"kind": "results_indicator_change", "path": "results_indicator"})
    elif kind == "outcome-measure":
        for path in ("role", "time_frame", "present", "measure"):
            if before.get(path) != after.get(path):
                changes.append({"kind": f"outcome_{path}_change", "path": path,
                                "before": before.get(path), "after": after.get(path)})
    elif kind == "result-posting":
        if canonical(before.get("posted")) != canonical(after.get("posted")) or canonical(
                before.get("outcome_results")) != canonical(after.get("outcome_results")):
            changes.append({"kind": "results_updated", "path": "outcome_results"})
    elif kind == "regulatory-record":
        if canonical(before.get("status")) != canonical(after.get("status")):
            changes.append({"kind": "regulatory_status_change", "path": "status"})
        if canonical(before.get("sections")) != canonical(after.get("sections")):
            changes.append({"kind": "label_text_change", "path": "sections"})
        if canonical(before.get("counts")) != canonical(after.get("counts")):
            changes.append({"kind": "report_counts_change", "path": "counts"})
        if canonical(before.get("submissions")) != canonical(after.get("submissions")):
            changes.append({"kind": "submissions_change", "path": "submissions"})
    elif kind == "registry-link" and before.get("status") != after.get("status"):
        changes.append({"kind": "link_status_change", "path": "status", "before": before.get("status"),
                        "after": after.get("status")})
    if not changes:
        changes.append({"kind": "descriptive_change", "path": "record"})
    return changes


def _require_read(namespace, scopes):
    if "operator" in scopes:
        return
    if READ_SCOPE not in scopes or (f"namespace:{namespace}:read" not in scopes
                                    and f"namespace:{namespace}:write" not in scopes):
        raise ClinicalRecordError("unauthorized", "clinical read and namespace access are required")


def _require_write(namespace, scopes, *, ingest=False):
    if "operator" in scopes:
        return
    if WRITE_SCOPE not in scopes or f"namespace:{namespace}:write" not in scopes or (
            ingest and INGEST_SCOPE not in scopes):
        raise ClinicalRecordError("unauthorized", "clinical write and namespace write scopes are required")


class ClinicalRecordStore:
    """The one authoritative store for clinical records (C01.2)."""

    def __init__(self, conn, *, initialize=True, now=None):
        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    # -------------------------------------------------------------- writes

    def ingest(self, namespace, provider, records, *, observation_id, observed_at_ms, scopes, evidence=None,
               execution=None):
        """Apply one provider observation (adapter records, already parsed)."""
        _require_write(namespace, scopes, ingest=True)
        return self._ingest(namespace, provider, records, observation_id=observation_id,
                            observed_at_ms=observed_at_ms, evidence=evidence, execution=execution)

    def _ingest(self, namespace, provider, records, *, observation_id, observed_at_ms, evidence=None,
                execution=None):
        if not isinstance(records, list) or len(records) > 5000:
            raise ClinicalRecordError("invalid_observation", "bounded record list required")
        records = [validate_record(r) for r in records]
        summary = {"observation_id": observation_id, "provider": provider, "created": [], "revised": [],
                   "unchanged": [], "conflicts": [], "invalidated_views": [], "amendments": {}, "links": []}
        # Registry versions are applied in native order so revisions follow the registry.
        records.sort(key=lambda r: (_kind_rank(r["record_kind"]),
                                    _order_key(r.get("native_version"))))
        self.conn.execute("BEGIN")
        try:
            for item in records:
                self._apply(namespace, item, observed_at_ms, observation_id, evidence or {}, summary)
            summary["links"] = self._link_registries(namespace, observed_at_ms, observation_id)
            self.conn.execute(
                """INSERT INTO clinical_provider_state VALUES (?,?,?,NULL,NULL,?,?)
                   ON CONFLICT (namespace, provider) DO UPDATE SET last_success_ms=excluded.last_success_ms,
                   last_observation_id=excluded.last_observation_id, last_execution=excluded.last_execution,
                   last_failure_ms=NULL, last_failure_code=NULL""",
                [namespace, provider, observed_at_ms, observation_id, execution or "unrecorded"])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return summary

    def _apply(self, namespace, item, observed_at_ms, observation_id, evidence, summary):
        rid = record_id(namespace, item)
        provider, native_id, sub_key = identity(item)
        key, content_hash = _version_key(item), digest(item)
        row = self.conn.execute("SELECT revision FROM clinical_records WHERE record_id=?", [rid]).fetchone()
        same_version = self.conn.execute(
            "SELECT revision, content_hash FROM clinical_record_revisions WHERE record_id=? AND version_key=? "
            "ORDER BY revision DESC LIMIT 1", [rid, key]).fetchone() if row else None
        if same_version:
            if same_version[1] == content_hash:
                self.conn.execute("UPDATE clinical_records SET last_seen_ms=greatest(last_seen_ms, ?) WHERE record_id=?",
                                  [observed_at_ms, rid])
                summary["unchanged"].append(rid)
                return
            if key != "unversioned" and not key.startswith("observation:"):
                # The same registry version observed with different content: both are
                # retained and the disagreement is reported, never silently replaced.
                conflict_id = "clinical-conflict:" + digest([rid, key, content_hash])[:24]
                self.conn.execute(
                    "INSERT INTO clinical_version_conflicts VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                    [conflict_id, namespace, rid, key, same_version[0], content_hash, canonical(item), observation_id,
                     "same native version observed with different content", observed_at_ms])
                summary["conflicts"].append({"record_id": rid, "version_key": key, "conflict_id": conflict_id,
                                             "retained_revision": same_version[0]})
                return
        previous = self._content(rid) if row else None
        if previous is not None and key in {"unversioned"} and digest(previous) == content_hash:
            summary["unchanged"].append(rid)
            return
        revision = (row[0] if row else 0) + 1
        amendments = classify_amendments(previous, item)
        if row:
            self.conn.execute(
                "UPDATE clinical_records SET revision=?, last_seen_ms=greatest(last_seen_ms, ?) WHERE record_id=?",
                [revision, observed_at_ms, rid])
            summary["revised"].append(rid)
        else:
            self.conn.execute("INSERT INTO clinical_records VALUES (?,?,?,?,?,?,1,?,?)",
                              [rid, namespace, item["record_kind"], provider, native_id, sub_key, observed_at_ms,
                               observed_at_ms])
            summary["created"].append(rid)
        self.conn.execute(
            "INSERT INTO clinical_record_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [rid, revision, content_hash, canonical(item), key, (item.get("native_version") or {}).get("date"),
             observed_at_ms, observation_id, canonical(evidence), canonical(amendments), self.now()])
        summary["amendments"][rid] = amendments
        if row:
            views = self.conn.execute(
                "SELECT view_id, revision FROM clinical_view_dependencies WHERE record_id=? AND revision<?",
                [rid, revision]).fetchall()
            for view_id, old in views:
                self.conn.execute(
                    "INSERT INTO clinical_view_invalidations VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                    [view_id, rid, old, revision, canonical(sorted({a["kind"] for a in amendments})), self.now()])
                summary["invalidated_views"].append(view_id)
        self._invalidate_new_members(namespace, item, rid, summary)

    def _invalidate_new_members(self, namespace, item, rid, summary):
        """A new link or posting for a trial a view depends on makes that view stale."""
        trial_key = None
        if item["record_kind"] in {"result-posting", "trial-arm", "outcome-measure"}:
            trial_key = (item["registry"], item["identifier"])
        elif item["record_kind"] == "registry-link":
            trial_key = (item["from_record"]["provider"], item["from_record"]["identifier"])
        if trial_key is None:
            return
        trial = self.conn.execute(
            "SELECT record_id, revision FROM clinical_records WHERE namespace=? AND record_kind='registered-trial' "
            "AND provider=? AND native_id=?", [namespace, *trial_key]).fetchone()
        if not trial:
            return
        for (view_id,) in self.conn.execute(
                "SELECT view_id FROM clinical_view_dependencies WHERE record_id=?", [trial[0]]).fetchall():
            known = self.conn.execute("SELECT 1 FROM clinical_view_dependencies WHERE view_id=? AND record_id=?",
                                      [view_id, rid]).fetchone()
            if known:
                continue
            self.conn.execute(
                "INSERT INTO clinical_view_invalidations VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                [view_id, rid, 0, 1, canonical([f"new_{item['record_kind']}"]), self.now()])
            summary["invalidated_views"].append(view_id)

    def _link_registries(self, namespace, observed_at_ms, observation_id):
        """Registry-to-registry links from declared secondary identifiers (never a merge)."""
        created = []
        trials = self.conn.execute(
            "SELECT record_id FROM clinical_records WHERE namespace=? AND record_kind='registered-trial' "
            "ORDER BY record_id", [namespace]).fetchall()
        for (rid,) in trials:
            content = self._content(rid)
            revision = self.conn.execute("SELECT revision FROM clinical_records WHERE record_id=?", [rid]).fetchone()[0]
            for secondary in content.get("secondary_identifiers") or []:
                registry = REGISTRY_FOR_IDENTIFIER.get(secondary["kind"])
                if registry in (None, content["registry"], "prospero"):
                    continue
                target = self.conn.execute(
                    "SELECT record_id FROM clinical_records WHERE namespace=? AND record_kind='registered-trial' "
                    "AND provider=? AND native_id=?", [namespace, registry, secondary["value"]]).fetchone()
                link = validate_record({
                    "contract": CONTRACT, "record_kind": "registry-link", "link_kind": "registry-registry",
                    "from_record": {"provider": content["registry"], "identifier": content["identifier"]},
                    "to": {"registry": registry, "identifier": secondary["value"]},
                    "evidence_kind": "registry-declared-secondary-id",
                    "evidence": {"declaring_record_id": rid, "declaring_revision": revision,
                                 "declared_as": secondary.get("domain") or secondary["kind"],
                                 "locator": secondary.get("locator")},
                    "status": "accepted" if target else "target-not-acquired",
                    "native_version": {"version": None, "date": None, "basis": "observation"},
                })
                lid = record_id(namespace, link)
                existing = self.conn.execute("SELECT revision FROM clinical_records WHERE record_id=?", [lid]).fetchone()
                if existing and digest(self._content(lid)) == digest(link):
                    continue
                summary = {"created": [], "revised": [], "unchanged": [], "conflicts": [], "invalidated_views": [],
                           "amendments": {}}
                self._apply(namespace, link, observed_at_ms, observation_id, {"derived_from": rid}, summary)
                created.append({"link_id": lid, "status": link["status"], "to": link["to"],
                                "from": link["from_record"]})
        return created

    def add_link(self, namespace, link, *, scopes, observation_id, observed_at_ms=None, evidence=None):
        """Record a registry-publication or registry-review link (write scope)."""
        _require_write(namespace, scopes)
        link = validate_record({"contract": CONTRACT, "record_kind": "registry-link",
                                "native_version": {"version": None, "date": None, "basis": "observation"}, **link})
        summary = {"created": [], "revised": [], "unchanged": [], "conflicts": [], "invalidated_views": [],
                   "amendments": {}}
        self.conn.execute("BEGIN")
        try:
            self._apply(namespace, link, observed_at_ms or self.now(), observation_id, evidence or {}, summary)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"link_id": record_id(namespace, link), **summary}

    def annotate_terms(self, namespace, rid, source, terms, *, observed_at_ms):
        """Registry-derived term annotations (e.g. CT.gov MeSH browse terms) kept beside, not in, revisions."""
        encoded = canonical(terms)
        self.conn.execute("INSERT INTO clinical_term_annotations VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [namespace, rid, source, digest(terms), encoded, observed_at_ms])

    def record_success(self, namespace, provider, *, observation_id, observed_at_ms, execution):
        """A successful refresh of a provider whose records another clinical owner keeps (surveillance series)."""
        self.conn.execute(
            """INSERT INTO clinical_provider_state VALUES (?,?,?,NULL,NULL,?,?)
               ON CONFLICT (namespace, provider) DO UPDATE SET last_success_ms=excluded.last_success_ms,
               last_observation_id=excluded.last_observation_id, last_execution=excluded.last_execution,
               last_failure_ms=NULL, last_failure_code=NULL""",
            [namespace, provider, observed_at_ms, observation_id, execution])

    def record_failure(self, namespace, provider, *, observation_id, failure_code, observed_at_ms, scopes=None,
                       internal=False):
        """A failed refresh marks the provider stale; it changes no record."""
        if not internal:
            _require_write(namespace, scopes or set(), ingest=True)
        self.conn.execute(
            """INSERT INTO clinical_provider_state VALUES (?,?,NULL,?,?,?,NULL)
               ON CONFLICT (namespace, provider) DO UPDATE SET last_failure_ms=excluded.last_failure_ms,
               last_failure_code=excluded.last_failure_code""",
            [namespace, provider, observed_at_ms, failure_code, observation_id])
        return {"provider": provider, "state": "stale", "failure_code": failure_code}

    # --------------------------------------------------------------- reads

    def _content(self, rid, revision=None):
        row = self.conn.execute(
            """SELECT r.content_json FROM clinical_records c JOIN clinical_record_revisions r
               ON r.record_id=c.record_id AND r.revision=coalesce(?, c.revision) WHERE c.record_id=?""",
            [revision, rid]).fetchone()
        return json.loads(row[0]) if row else None

    def provider_state(self, namespace, provider):
        row = self.conn.execute(
            "SELECT last_success_ms, last_failure_ms, last_failure_code, last_execution FROM clinical_provider_state "
            "WHERE namespace=? AND provider=?", [namespace, provider]).fetchone()
        if not row:
            return {"provider": provider, "last_success_ms": None, "stale": True, "reason": "never acquired"}
        return {"provider": provider, "last_success_ms": row[0], "last_failure_ms": row[1],
                "last_failure_code": row[2], "last_execution": row[3], "stale": row[1] is not None}

    def get(self, namespace, rid, *, scopes, revision=None):
        _require_read(namespace, scopes)
        row = self.conn.execute(
            "SELECT revision, record_kind, provider, native_id FROM clinical_records WHERE record_id=? AND namespace=?",
            [rid, namespace]).fetchone()
        if not row:
            raise ClinicalRecordError("record_not_found", "clinical record is unavailable")
        meta = self.conn.execute(
            "SELECT revision, version_key, version_date, observed_at_ms, observation_id, evidence_json, amendments_json "
            "FROM clinical_record_revisions WHERE record_id=? AND revision=coalesce(?, ?)",
            [rid, revision, row[0]]).fetchone()
        if not meta:
            raise ClinicalRecordError("record_not_found", "clinical record revision is unavailable")
        return {"record_id": rid, "namespace": namespace, "revision": meta[0], "current_revision": row[0],
                "record": self._content(rid, meta[0]), "version_key": meta[1], "version_date": meta[2],
                "observed_at_ms": meta[3], "observation_id": meta[4], "evidence": json.loads(meta[5]),
                "amendments": json.loads(meta[6])}

    def history(self, namespace, rid, *, scopes):
        _require_read(namespace, scopes)
        if not self.conn.execute("SELECT 1 FROM clinical_records WHERE record_id=? AND namespace=?",
                                 [rid, namespace]).fetchone():
            raise ClinicalRecordError("record_not_found", "clinical record is unavailable")
        rows = self.conn.execute(
            "SELECT revision, version_key, version_date, observed_at_ms, amendments_json, content_json "
            "FROM clinical_record_revisions WHERE record_id=? ORDER BY revision", [rid]).fetchall()
        conflicts = self.conn.execute(
            "SELECT conflict_id, version_key, retained_revision, conflicting_json, reason FROM clinical_version_conflicts "
            "WHERE record_id=? AND namespace=? ORDER BY conflict_id", [rid, namespace]).fetchall()
        return {"record_id": rid, "revisions": [
            {"revision": r[0], "version_key": r[1], "version_date": r[2], "observed_at_ms": r[3],
             "amendments": json.loads(r[4]), "record": json.loads(r[5])} for r in rows],
            "version_conflicts": [{"conflict_id": c[0], "version_key": c[1], "retained_revision": c[2],
                                   "conflicting_record": json.loads(c[3]), "reason": c[4]} for c in conflicts]}

    def find(self, namespace, *, scopes, kinds=None, provider=None, native_id=None, limit=1000):
        _require_read(namespace, scopes)
        rows = self.conn.execute(
            "SELECT record_id, record_kind, provider, native_id FROM clinical_records WHERE namespace=? "
            "ORDER BY record_kind, provider, native_id, record_id LIMIT ?",
            [namespace, min(max(int(limit), 1), 10000)]).fetchall()
        return [{"record_id": r[0], "record_kind": r[1], "provider": r[2], "native_id": r[3],
                 "record": self._content(r[0]),
                 "revision": self.conn.execute("SELECT revision FROM clinical_records WHERE record_id=?",
                                               [r[0]]).fetchone()[0]}
                for r in rows if (not kinds or r[1] in kinds) and (provider is None or r[2] == provider)
                and (native_id is None or r[3] == native_id)]

    def trial_id(self, namespace, registry, identifier_value):
        row = self.conn.execute(
            "SELECT record_id FROM clinical_records WHERE namespace=? AND record_kind='registered-trial' AND provider=? "
            "AND native_id=?", [namespace, registry, identifier_value]).fetchone()
        return row[0] if row else None

    def links(self, namespace, *, provider, identifier_value, direction="from"):
        rows = self.conn.execute(
            "SELECT record_id FROM clinical_records WHERE namespace=? AND record_kind='registry-link' ORDER BY record_id",
            [namespace]).fetchall()
        result = []
        for (rid,) in rows:
            link = self._content(rid)
            source, target = link["from_record"], link["to"]
            if direction == "from" and (source["provider"], source["identifier"]) == (provider, identifier_value):
                result.append({"link_id": rid, **link})
            if direction == "to" and (target.get("registry"), target.get("identifier")) == (provider, identifier_value):
                result.append({"link_id": rid, **link})
        return result

    def trial(self, namespace, rid, *, scopes, as_of_revision=None):
        """The trial as assembled from its distinct records, with links and conflicts, nothing merged."""
        current = self.get(namespace, rid, scopes=scopes, revision=as_of_revision)
        trial = current["record"]
        if trial["record_kind"] != "registered-trial":
            raise ClinicalRecordError("not_a_trial", "record is not a registered trial")
        members = self.find(namespace, scopes=scopes, kinds={"trial-arm", "outcome-measure", "result-posting"},
                            provider=trial["registry"], native_id=trial["identifier"])
        annotations = [json.loads(r[0]) for r in self.conn.execute(
            "SELECT terms_json FROM clinical_term_annotations WHERE namespace=? AND record_id=? ORDER BY observed_at_ms",
            [namespace, rid]).fetchall()]
        outgoing = self.links(namespace, provider=trial["registry"], identifier_value=trial["identifier"])
        incoming = [link for link in self.links(namespace, provider=trial["registry"],
                                                identifier_value=trial["identifier"], direction="to")]
        return {
            "record_id": rid, "revision": current["revision"], "current_revision": current["current_revision"],
            "trial": trial, "version_key": current["version_key"],
            "arms": [m for m in members if m["record_kind"] == "trial-arm"],
            "outcomes": [m for m in members if m["record_kind"] == "outcome-measure"],
            "result_postings": [m for m in members if m["record_kind"] == "result-posting"],
            "links": outgoing, "linked_from": incoming,
            "cross_registry": self.cross_registry(namespace, rid, scopes=scopes),
            "term_annotations": annotations,
            "version_conflicts": self.history(namespace, rid, scopes=scopes)["version_conflicts"],
            "source_freshness": self.provider_state(namespace, trial["registry"]),
        }

    def cross_registry(self, namespace, rid, *, scopes):
        """Linked registry records compared field by field; disagreements are listed, never resolved."""
        _require_read(namespace, scopes)
        trial = self._content(rid)
        own = self.conn.execute("SELECT revision FROM clinical_records WHERE record_id=?", [rid]).fetchone()[0]
        peers = {}
        for direction in ("from", "to"):
            for link in self.links(namespace, provider=trial["registry"], identifier_value=trial["identifier"],
                                   direction=direction):
                if link["link_kind"] != "registry-registry":
                    continue
                other = link["to"] if direction == "from" else {"registry": link["from_record"]["provider"],
                                                                "identifier": link["from_record"]["identifier"]}
                peers.setdefault((other["registry"], other["identifier"]), []).append(
                    {"link_id": link["link_id"], "evidence_kind": link["evidence_kind"], "status": link["status"],
                     "evidence": link["evidence"]})
        comparisons = []
        for (registry, value), evidence in sorted(peers.items()):
            peer_id = self.trial_id(namespace, registry, value)
            entry = {"registry": registry, "identifier": value, "record_id": peer_id, "link_evidence": evidence,
                     "merged": False, "disagreements": []}
            if peer_id:
                peer = self._content(peer_id)
                peer_revision = self.conn.execute("SELECT revision FROM clinical_records WHERE record_id=?",
                                                  [peer_id]).fetchone()[0]
                pairs = _comparable(trial, peer) + [(
                    "primary_outcomes", self.primary_outcomes(namespace, trial["registry"], trial["identifier"]),
                    self.primary_outcomes(namespace, registry, value))]
                for field, left, right in pairs:
                    if left and right and left != right:
                        entry["disagreements"].append({
                            "field": field,
                            "values": [{"registry": trial["registry"], "record_id": rid, "revision": own, "value": left},
                                       {"registry": registry, "record_id": peer_id, "revision": peer_revision,
                                        "value": right}]})
            comparisons.append(entry)
        return comparisons

    def primary_outcomes(self, namespace, registry, identifier_value):
        """Normalized primary outcome measures currently registered (``None`` when none recorded)."""
        rows = self.conn.execute(
            "SELECT record_id FROM clinical_records WHERE namespace=? AND record_kind='outcome-measure' AND provider=? "
            "AND native_id=? ORDER BY record_id", [namespace, registry, identifier_value]).fetchall()
        measures = sorted({normalize_text(c["measure"]) for c in (self._content(r) for (r,) in rows)
                           if c["role"] == "primary" and c["present"]})
        return measures or None

    # ------------------------------------------------------------- views

    def register_view(self, namespace, view_id, owner, pins):
        for rid, revision in pins.items():
            self.conn.execute(
                """INSERT INTO clinical_view_dependencies VALUES (?,?,?,?,?)
                   ON CONFLICT (view_id, record_id) DO UPDATE SET revision=excluded.revision""",
                [view_id, namespace, owner, rid, revision])
        # Recomputing a view clears earlier invalidations it has absorbed.
        self.conn.execute(
            "DELETE FROM clinical_view_invalidations WHERE view_id=? AND to_revision <= coalesce("
            "(SELECT revision FROM clinical_view_dependencies d WHERE d.view_id=clinical_view_invalidations.view_id "
            "AND d.record_id=clinical_view_invalidations.record_id), 0)", [view_id])

    def view_status(self, view_id):
        rows = self.conn.execute(
            "SELECT record_id, from_revision, to_revision, reasons_json FROM clinical_view_invalidations WHERE view_id=? "
            "ORDER BY record_id, to_revision", [view_id]).fetchall()
        stale = [{"record_id": r, "from_revision": f, "to_revision": t, "reasons": json.loads(x)} for r, f, t, x in rows]
        return {"view_id": view_id, "current": not stale, "stale": bool(stale), "invalidations": stale}


def _kind_rank(kind):
    return RECORD_KINDS.index(kind) if kind in RECORD_KINDS else len(RECORD_KINDS)


def _order_key(version):
    version = version or {}
    value = version.get("version")
    try:
        number = int(value) if value is not None else -1
    except (TypeError, ValueError):
        number = -1
    return (number, version.get("date") or "")


def _comparable(left, right):
    """Field pairs compared across registries (normalized values only)."""
    design_l, design_r = left.get("design") or {}, right.get("design") or {}
    phase_l = sorted((left.get("phase") or {}).get("normalized") or [])
    phase_r = sorted((right.get("phase") or {}).get("normalized") or [])
    status_l = (left.get("status") or {}).get("normalized")
    status_r = (right.get("status") or {}).get("normalized")
    return [
        ("status", None if status_l in (None, "unknown") else status_l,
         None if status_r in (None, "unknown") else status_r),
        ("phase", None if phase_l in ([], ["unknown"]) else phase_l, None if phase_r in ([], ["unknown"]) else phase_r),
        ("design.allocation", _known(design_l.get("allocation")), _known(design_r.get("allocation"))),
        ("design.masking", _known(design_l.get("masking")), _known(design_r.get("masking"))),
        ("design.enrollment_planned", design_l.get("enrollment_planned"), design_r.get("enrollment_planned")),
        ("design.enrollment_actual", design_l.get("enrollment_actual"), design_r.get("enrollment_actual")),
        ("sponsor.lead", normalize_text((left.get("sponsor") or {}).get("lead")) or None,
         normalize_text((right.get("sponsor") or {}).get("lead")) or None),
    ]


def _known(value):
    return None if value in (None, "unknown") else value


# -------------------------------------------------------------- projection


class ClinicalProjector:
    """Source-pack projector for ``noesis-clinical-record-v1`` (the runtime calls it per committed page).

    Each page record carries the adapter's parsed H02 records; they are applied
    to the namespace named by the source, with the page's document revision,
    request hashes and source-pack run as evidence. Replayed pages are
    idempotent because unchanged revisions are never appended.
    """

    def __init__(self, conn) -> None:
        self.conn = conn
        self.store = ClinicalRecordStore(conn)

    def _document_revision(self, source_id, raw_id):
        from src.ingestion.source_packs import _digest

        document_id = "spdoc:" + _digest([source_id, str(raw_id)])[:28]
        row = self.conn.execute(
            "SELECT revision_id FROM document_revision_records WHERE document_id=? ORDER BY revision DESC LIMIT 1",
            [document_id]).fetchone() if self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='document_revision_records'").fetchone() else None
        return {"document_id": document_id, "revision_id": row[0] if row else None}

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del documents, principal_id
        from src.ingestion.clinical_providers import CONNECTOR_PROVIDER

        provider = CONNECTOR_PROVIDER[source["connector"]]
        summaries = []
        for item in records:
            clinical = item.get("clinical_records") or []
            if not clinical:
                continue
            namespace = str(item.get("clinical_namespace") or (source.get("clinical") or {}).get("namespace"))
            evidence = {
                "source_pack": {"pack_id": manifest["pack_id"], "version": manifest["version"],
                                "manifest_hash": manifest["manifest_hash"], "source_id": source["source_id"],
                                "run_id": run_id},
                "document": self._document_revision(source["source_id"], item.get("id")),
                "capture": item.get("clinical_capture") or {},
            }
            observed = self.store.now()
            summary = self.store._ingest(
                namespace, provider, list(clinical), observation_id=f"{run_id}:{source['source_id']}:{item.get('id')}",
                observed_at_ms=observed, evidence=evidence,
                execution=(page_receipt or {}).get("execution") or "unrecorded")
            for annotation in item.get("clinical_annotations") or []:
                trial = next((r for r in clinical if r["record_kind"] == "registered-trial"), None)
                if trial:
                    self.store.annotate_terms(namespace, record_id(namespace, validate_record(trial)),
                                              annotation.get("source") or "registry", annotation,
                                              observed_at_ms=observed)
            summaries.append({k: summary[k] for k in ("created", "revised", "unchanged", "conflicts")})
        return {"pages": len(summaries), "records": summaries}

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        from src.ingestion.clinical_providers import CONNECTOR_PROVIDER

        if status != "complete":
            row = self.conn.execute(
                "SELECT failure_json FROM source_pack_source_runs WHERE run_id=? AND source_id=?",
                [run_id, source["source_id"]]).fetchone()
            code = (json.loads(row[0]) or {}).get("code") if row and row[0] else "source_failed"
            self.store.record_failure(str((source.get("clinical") or {}).get("namespace")),
                                      CONNECTOR_PROVIDER[source["connector"]], observation_id=run_id,
                                      failure_code=code, observed_at_ms=self.store.now(), internal=True)
        return {"status": status}


# ------------------------------------------------------------ schema registry

SCHEMA_FILES = {
    "noesis-clinical-record": "noesis-clinical-record-v1.json",
    "noesis-clinical-evidence-map": "noesis-clinical-evidence-map-v1.json",
    "noesis-clinical-medicines-record": "noesis-clinical-medicines-record-v1.json",
}


def register_schemas(conn, *, principal_id, scopes):
    """Register the clinical contracts in the schema registry and declare the pack as their consumer."""
    from pathlib import Path

    from src.kb.schema_registry import SchemaRegistry

    root = Path(__file__).resolve().parents[2] / "contracts" / "schemas" / "jsonschema"
    registry = SchemaRegistry(conn)
    modules = []
    for name, file_name in sorted(SCHEMA_FILES.items()):
        content = json.loads((root / file_name).read_text())
        module = registry.register({
            "contract": "noesis-schema-module-v1", "name": name, "kind": "schema", "semantic_version": "1.0.0",
            "content": content, "owner": "clinical-evidence", "dependencies": [], "compatibility_policy": "backward",
            "provenance": {"kind": "imported", "source": f"contracts/schemas/jsonschema/{file_name}"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }, f"clinical-schema:{name}:1.0.0:{digest(content)[:16]}", principal_id=principal_id, scopes=scopes)
        registry.declare_dependency(module["module_id"], "pack", "clinical-evidence",
                                    {"use": "clinical record contract"}, principal_id=principal_id, scopes=scopes)
        modules.append({"name": name, "version": "1.0.0", "module_id": module["module_id"]})
    return modules
