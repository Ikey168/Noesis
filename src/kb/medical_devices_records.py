"""Medical device, clearance, approval, recall and adverse-event report records with revisions (#2654, MD02).

``noesis-medical-device-record-v1`` records (from
:mod:`src.ingestion.medical_devices_sources`) are kept per namespace and
record key with an append-only revision log, following the
:mod:`src.kb.entity_history` pattern of never rewriting what was recorded.
Every revision carries its **source** (provider, locator, disclaimer,
attribution, licence), its **record revision** (native revision, revision
number and id) and its **as-of time** (the source's published date and the
observation time). Record kinds:

* ``classification`` - an FDA product code with device class and regulation;
* ``clearance`` - a 510(k) keyed by K number with decision code, decision date
  and product code as published;
* ``approval`` / ``approval-supplement`` - a PMA keyed by P number; each
  supplement is its own record keyed by P number and supplement number and
  chained to the original through ``approval_key`` (a supplement never
  rewrites the approval);
* ``recall`` - keyed by recall number, with class, status and reason as
  published; a class or status change is a new revision;
* ``adverse-event-report`` - a MAUDE report keyed by report number with event
  type and dates as published and FDA's caveats attached;
* ``report-count`` - the published MAUDE tally for a product code and window,
  with caveats; counts are reports, never incidence;
* ``device-identifier`` - an AccessGUDID record keyed by primary DI with package
  DIs and its public version as the revision;
* ``eudamed-actor`` / ``eudamed-device`` / ``eudamed-certificate`` - keyed by
  SRN, Basic UDI-DI and notified body + certificate number; a certificate
  status change is a new revision.

Revisions are immutable. A changed payload is a ``revised`` revision; an older
version observed later is an ``older-observation`` that never becomes current;
a replay adds nothing; a unit the publisher no longer answers becomes a
``not-published`` revision (removals and corrections are revisions, never
deletions). **The MD01 minimisation decision is enforced at write time**: a
record carrying a personal field (patient, reporter, contact person, street
address, phone, email) is refused with ``minimisation_violation`` before
anything is written, and MAUDE narratives are returned only to principals
holding the narrative scope. No record or answer carries a safety signal,
causality, incidence, rate or clinical advice.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.ingestion.medical_devices_sources import (
    MINIMISATION,
    MINIMISATION_ID,
    PROVIDERS,
    RECORD_CONTRACT,
    REVIEW_BOUNDARY,
)

CONTRACT = RECORD_CONTRACT
READ_SCOPE = "knowledge:clinical:read"
WRITE_SCOPE = "knowledge:clinical:write"
REVIEW_SCOPE = "knowledge:clinical:review"
NARRATIVE_SCOPE = "knowledge:clinical:devices:narratives:read"
DEFAULT_NAMESPACE = "clinical"
BUNDLE = "clinical-evidence"
RECORD_KINDS = ("classification", "clearance", "approval", "approval-supplement", "recall", "adverse-event-report",
                "report-count", "device-identifier", "eudamed-actor", "eudamed-device", "eudamed-certificate")
CHANGES = ("new", "revised", "unchanged", "older-observation")
STATES = ("published", "not-published")
EXCLUSIONS = ("safety-signal detection", "causality from adverse-event reports", "clinical advice",
              "patient data beyond what regulators publish")
# Keys that would carry an assessment; no record or answer may contain them.
FORBIDDEN_ANSWER_KEYS = frozenset({
    "signal", "safety_signal", "signal_score", "disproportionality", "prr", "ror", "causality", "causal",
    "caused_by", "incidence", "rate", "reporting_rate", "risk", "risk_score", "safety_verdict", "verdict",
    "recommendation", "clinical_advice", "advice", "ranking", "score",
})
# Personal fields refused at write time (MD01 minimisation), matched as keys anywhere in a record.
PERSONAL_KEYS = frozenset({
    "patient", "patients", "patient_age", "patient_sex", "patient_weight", "date_of_birth", "age", "sex", "gender",
    "weight", "ethnicity", "race", "sequence_number_outcome", "sequence_number_treatment", "reporter",
    "reporter_name", "reporter_occupation_code", "contact", "contact_person", "contact_persons", "contacts",
    "customer_contacts", "prrc", "prrcs", "first_name", "last_name", "phone", "telephone", "email", "fax",
    "address", "address_1", "address_2", "street", "street_address", "zip_code", "postal_code", "postcode",
    "manufacturer_contact_f_name", "manufacturer_contact_l_name", "manufacturer_contact_phone_number",
    "manufacturer_contact_email", "manufacturer_contact_address_1", "distributor_address_1",
})
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ALLOWED = {"contract", "record_kind", "record_key", "provider", "jurisdiction", "authority", "native_id",
            "native_revision", "revision_order", "as_of", "locator", "publication_state", "fields", "identifiers",
            "links_as_published", "caveats", "disclaimer", "attribution", "license", "minimisation", "evidence_origin",
            "unknowns"}
_KEY_FIELD = {"classification": "product_code", "clearance": "k_number", "approval": "pma_number",
              "approval-supplement": "supplement_number", "recall": "recall_number",
              "adverse-event-report": "report_number", "report-count": "product_code",
              "device-identifier": "primary_di", "eudamed-actor": "srn", "eudamed-device": "basic_udi_di",
              "eudamed-certificate": "certificate_number"}
_DATE_FIELDS = ("decision_date", "date_received", "event_date_initiated", "event_date_posted", "event_date_terminated",
                "center_classification_date", "date_of_event", "date_report", "public_version_date", "publish_date",
                "issue_date", "starting_validity_date", "expiry_date", "last_update_date")
_UNKNOWN_FIELDS = {
    "clearance": ("decision_code", "decision_date", "product_code"),
    "approval": ("decision_code", "decision_date", "product_code"),
    "approval-supplement": ("decision_code", "decision_date", "supplement_type"),
    "recall": ("recall_class", "status_as_published", "event_date_initiated", "product_code"),
    "adverse-event-report": ("event_type", "date_received", "date_of_event"),
    "device-identifier": ("brand_name", "company_name", "public_version_number"),
    "eudamed-certificate": ("status_as_published", "issue_date", "expiry_date"),
    "eudamed-device": ("manufacturer_srn", "risk_class"),
    "eudamed-actor": ("name", "country"),
    "classification": ("device_class",),
    "report-count": (),
}
_DDL = """
CREATE TABLE IF NOT EXISTS medical_device_records (
  namespace TEXT NOT NULL, record_key TEXT NOT NULL, provider TEXT NOT NULL, jurisdiction TEXT NOT NULL,
  record_kind TEXT NOT NULL, current_revision_id TEXT NOT NULL, revision_count INTEGER NOT NULL,
  first_run_id TEXT NOT NULL, first_observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, record_key)
);
CREATE TABLE IF NOT EXISTS medical_device_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, record_key TEXT NOT NULL, revision_no INTEGER NOT NULL,
  previous_revision_id TEXT, change TEXT NOT NULL, content_hash TEXT NOT NULL, native_revision TEXT,
  revision_order TEXT NOT NULL, as_of TEXT, publication_state TEXT NOT NULL, record_json TEXT NOT NULL,
  evidence_origin TEXT NOT NULL, source_id TEXT NOT NULL, run_id TEXT NOT NULL, receipt_id TEXT,
  observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS medical_device_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL,
  receipt_json TEXT NOT NULL, outcome_json TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, receipt_id)
);
"""
_REVISION_COLUMNS = ("revision_id", "record_key", "revision_no", "previous_revision_id", "change", "content_hash",
                     "native_revision", "revision_order", "as_of", "publication_state", "record_json",
                     "evidence_origin", "source_id", "run_id", "receipt_id", "observed_at_ms")


class MedicalDeviceError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = ({f"namespace:{namespace}:write"} if write
              else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"})
    if required not in scopes or not needed & scopes:
        raise MedicalDeviceError("unauthorized", f"{required} and namespace access are required")


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def selected_features(conn: Any) -> list[str]:
    """The Clinical Evidence optional features selected in the active composition plan (read only; default none)."""
    try:
        if not all(table_exists(conn, t) for t in ("composition_authority", "composition_active",
                                                    "composition_generations", "composition_plans")):
            return []
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle=?", [BUNDLE]).fetchone()
        if not managed or managed[0] != "composition":
            return []
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return []
    return list((plan.get("features") or {}).get(BUNDLE) or [])


def feature_enabled(conn: Any, feature: str) -> bool:
    return feature in selected_features(conn)


def personal_fields(value: Any, path: str = "$") -> list[str]:
    """Paths of any personal field (MD01) anywhere in a record."""
    found = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in PERSONAL_KEYS:
                found.append(f"{path}.{key}")
            found += personal_fields(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += personal_fields(item, f"{path}[{index}]")
    return found


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in a record or answer that would carry a signal, causality, rate or advice."""
    found = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_ANSWER_KEYS:
                found.append(f"{path}.{key}")
            found += forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}[{index}]")
    return found


def _unknowns(record: Mapping[str, Any]) -> list[str]:
    if record["publication_state"] != "published":
        return ["publication_state"]
    fields = record["fields"]
    unknowns = [f for f in _UNKNOWN_FIELDS.get(record["record_kind"], ()) if fields.get(f) in (None, "", [])]
    if record.get("as_of") is None:
        unknowns.append("as_of")
    return sorted(set(unknowns))


def validate(record: Mapping[str, Any]) -> dict[str, Any]:
    """Structural checks, the exclusions and the MD01 minimisation guard; returns a canonical copy."""
    if not isinstance(record, Mapping):
        raise MedicalDeviceError("invalid_record", "a medical-device record is an object")
    record = json.loads(canonical(record))
    if record.get("contract") != CONTRACT or record.get("record_kind") not in RECORD_KINDS:
        raise MedicalDeviceError("invalid_record", "not a medical-device record")
    extra = set(record) - _ALLOWED
    if extra:
        raise MedicalDeviceError("invalid_record", "unsupported field: " + ", ".join(sorted(extra)))
    violations = personal_fields(record)
    if violations:
        raise MedicalDeviceError("minimisation_violation", "personal fields may not be stored (MD01 "
                                 f"{MINIMISATION_ID})", paths=violations)
    assessed = forbidden_keys({k: v for k, v in record.items() if k not in {"caveats", "disclaimer"}})
    if assessed:
        raise MedicalDeviceError("assessment_forbidden", "records hold what regulators published; no signal, "
                                 "causality, rate or advice is stored", paths=assessed)
    if not str(record.get("record_key") or "").startswith("medical-devices:"):
        raise MedicalDeviceError("invalid_record", "record keys are medical-devices:* keys")
    if record.get("provider") not in PROVIDERS:
        raise MedicalDeviceError("invalid_record", "unknown medical-devices provider")
    if not str(record.get("locator") or "").startswith("https://"):
        raise MedicalDeviceError("invalid_record", "every record cites an HTTPS locator")
    if record.get("publication_state") not in STATES:
        raise MedicalDeviceError("invalid_record", "publication_state is published or not-published")
    if record.get("minimisation") != MINIMISATION_ID:
        raise MedicalDeviceError("invalid_record", f"records declare the {MINIMISATION_ID} policy")
    fields = record.get("fields")
    if not isinstance(fields, dict):
        raise MedicalDeviceError("invalid_record", "fields is an object")
    kind = record["record_kind"]
    if not fields.get(_KEY_FIELD[kind]) and not (kind == "approval" and fields.get("pma_number")):
        raise MedicalDeviceError("invalid_record", f"a {kind} record carries its {_KEY_FIELD[kind]}")
    for field in _DATE_FIELDS:
        if fields.get(field) is not None and not _DATE.fullmatch(str(fields[field])):
            raise MedicalDeviceError("invalid_record", f"{field} is YYYY-MM-DD or null (never guessed)")
    if record.get("as_of") is not None and not _DATE.fullmatch(str(record["as_of"])):
        raise MedicalDeviceError("invalid_record", "as_of is the source's published date (YYYY-MM-DD) or null")
    if record["publication_state"] == "published":
        if record["provider"] == "openfda-device" and not str((record.get("disclaimer") or {}).get("text") or ""):
            raise MedicalDeviceError("missing_disclaimer", "openFDA records keep openFDA's disclaimer")
        if kind in {"adverse-event-report", "report-count"} and not record.get("caveats"):
            raise MedicalDeviceError("missing_caveats", "adverse-event reports and counts carry FDA's caveats")
    if kind == "approval-supplement" and fields.get("approval_key") != (
            f"medical-devices:fda:pma:{fields.get('pma_number')}"):
        raise MedicalDeviceError("invalid_record", "a supplement names the approval it supplements")
    if kind == "report-count":
        for item in fields.get("counts_as_published") or []:
            if set(item) != {"term", "count"} or not isinstance(item["count"], int):
                raise MedicalDeviceError("invalid_record", "published counts are term/count pairs, kept verbatim")
    for item in (record.get("identifiers") or []) + (record.get("links_as_published") or []):
        if not isinstance(item, dict) or set(item) != {"scheme", "value"}:
            raise MedicalDeviceError("invalid_record", "identifiers and links use scheme/value")
    record["unknowns"] = _unknowns(record)
    return record


def present(view: Mapping[str, Any], scopes: Iterable[str]) -> dict[str, Any]:
    """A view as returned to a principal: MAUDE narratives only with the narrative scope (MD01)."""
    scopes = set(scopes)
    record = view["record"]
    if record.get("record_kind") != "adverse-event-report" or NARRATIVE_SCOPE in scopes or "operator" in scopes:
        return dict(view)
    fields = dict(record["fields"])
    narratives = fields.get("narratives") or []
    fields["narratives"] = [{"text_type_code": n.get("text_type_code"), "text": None, "withheld": True}
                            for n in narratives]
    return {**view, "record": {**record, "fields": fields},
            "narratives_withheld": {"count": len(narratives), "scope": NARRATIVE_SCOPE,
                                    "reason": MINIMISATION["restricted"]["note"]}}


def content_digest(record: Mapping[str, Any]) -> str:
    """The published content of a record: a dataset-wide revision stamp alone (openFDA ``meta.last_updated``, a
    version number without changed content) is not a new revision; the first revision keeps its as-of date."""
    body = {k: v for k, v in record.items()
            if k not in {"evidence_origin", "native_revision", "revision_order", "as_of", "unknowns"}}
    if isinstance(body.get("disclaimer"), Mapping):
        body["disclaimer"] = {k: v for k, v in body["disclaimer"].items() if k != "last_updated"}
    fields = dict(body.get("fields") or {})
    for stamp in ("enforcement_report_revision", "version_number", "public_version_number", "last_update_date",
                  "public_version_date"):
        fields.pop(stamp, None)
    body["fields"] = fields
    return digest(body)


def _view(row: Sequence[Any], head: Mapping[str, Any]) -> dict[str, Any]:
    revision = dict(zip(_REVISION_COLUMNS, row))
    record = json.loads(revision.pop("record_json"))
    return {
        **head, **revision, "record": record,
        "citation": {
            "record_key": revision["record_key"], "provider": record["provider"],
            "jurisdiction": record["jurisdiction"], "authority": record["authority"],
            "revision_id": revision["revision_id"], "revision_no": revision["revision_no"],
            "native_revision": revision["native_revision"], "as_of": revision["as_of"],
            "observed_at_ms": revision["observed_at_ms"], "locator": record["locator"],
            "source_id": revision["source_id"], "evidence_origin": revision["evidence_origin"],
            "attribution": record.get("attribution"), "license": record.get("license"),
            "disclaimer": (record.get("disclaimer") or {}).get("text"),
        },
    }


class MedicalDeviceStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "medical_device_revisions")

    # ------------------------------------------------------------------ writes

    def project(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str, source_id: str,
                receipt: Mapping[str, Any] | None = None, observed_at_ms: int | None = None) -> dict[str, Any]:
        """Append revisions for what changed; idempotent. The whole page is refused if any record is invalid."""
        checked = [validate(r) for r in records]
        keys = [r["record_key"] for r in checked]
        if len(set(keys)) != len(keys):
            raise MedicalDeviceError("invalid_record", "a page repeats a record key")
        if not self.ready():
            self.conn.execute(_DDL)
        observed = int(observed_at_ms if observed_at_ms is not None else self.now())
        receipt = dict(receipt or {})
        receipt_id = "md-receipt:" + digest([namespace, source_id, run_id, receipt, keys])[:24]
        counts = dict.fromkeys(CHANGES, 0)
        changes = {}
        self.conn.execute("BEGIN")
        try:
            for record in sorted(checked, key=lambda r: r["record_key"]):
                change = self._observe(namespace, record, source_id, run_id, receipt_id, observed)
                counts[change] += 1
                changes[record["record_key"]] = change
            self.conn.execute("INSERT OR IGNORE INTO medical_device_receipts VALUES (?,?,?,?,?,?,?)",
                              [namespace, receipt_id, run_id, source_id, canonical(receipt), canonical(counts),
                               observed])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"receipt_id": receipt_id, "counts": counts, "records": len(checked), "changes": changes}

    def _observe(self, namespace, record, source_id, run_id, receipt_id, observed) -> str:
        key = record["record_key"]
        origin = "fixture" if record.get("evidence_origin") == "fixture" else "live"
        content_hash = content_digest(record)
        head = self.conn.execute(
            "SELECT r.current_revision_id, r.revision_count, v.content_hash, v.revision_order FROM "
            "medical_device_records r JOIN medical_device_revisions v ON v.namespace=r.namespace AND "
            "v.revision_id=r.current_revision_id WHERE r.namespace=? AND r.record_key=?", [namespace, key]).fetchone()
        order = str(record.get("revision_order") or "")
        if head is not None:
            if head[2] == content_hash:
                return "unchanged"
            if self.conn.execute("SELECT 1 FROM medical_device_revisions WHERE namespace=? AND record_key=? AND "
                                 "content_hash=?", [namespace, key, content_hash]).fetchone():
                return "unchanged"  # a replayed older response: already on record, never re-applied
            change = "older-observation" if order and order < str(head[3] or "") else "revised"
        else:
            change = "new"
        revision_no = 1 if head is None else int(head[1]) + 1
        revision_id = "md-rev:" + digest([namespace, key, revision_no, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO medical_device_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, key, revision_no, None if head is None else head[0], change, content_hash,
             record.get("native_revision"), order, record.get("as_of"), record["publication_state"],
             canonical(record), origin, source_id, run_id, receipt_id, observed])
        if head is None:
            self.conn.execute("INSERT INTO medical_device_records VALUES (?,?,?,?,?,?,?,?,?)",
                              [namespace, key, record["provider"], record["jurisdiction"], record["record_kind"],
                               revision_id, 1, run_id, observed])
        elif change == "older-observation":
            self.conn.execute("UPDATE medical_device_records SET revision_count=? WHERE namespace=? AND "
                              "record_key=?", [revision_no, namespace, key])
        else:
            self.conn.execute("UPDATE medical_device_records SET current_revision_id=?, revision_count=? WHERE "
                              "namespace=? AND record_key=?", [revision_id, revision_no, namespace, key])
        return change

    # ------------------------------------------------------------------ reads

    def _heads(self, namespace: str, where: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT r.provider, r.jurisdiction, r.record_kind, r.revision_count, r.first_observed_at_ms, "
            + ", ".join(f"v.{c}" for c in _REVISION_COLUMNS) +
            " FROM medical_device_records r JOIN medical_device_revisions v ON v.namespace=r.namespace AND "
            "v.revision_id=r.current_revision_id WHERE r.namespace=? " + where +
            " ORDER BY r.record_kind, r.record_key", [namespace, *params]).fetchall()
        return [_view(row[5:], dict(zip(("provider", "jurisdiction", "record_kind", "revision_count",
                                          "first_observed_at_ms"), row[:5]))) for row in rows]

    def records(self, namespace: str, *, scopes: Iterable[str], kinds: Iterable[str] | None = None,
                record_keys: Iterable[str] | None = None, provider: str | None = None,
                include_unpublished: bool = True) -> list[dict[str, Any]]:
        """Current revision of every matching record, as presented to the caller."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not self.ready():
            return []
        where, params = [], []
        if provider is not None:
            where.append("AND r.provider=?")
            params.append(provider)
        for column, values in (("r.record_kind", kinds), ("r.record_key", record_keys)):
            if values is not None:
                values = sorted(set(values))
                if not values:
                    return []
                where.append(f"AND {column} IN (" + ",".join("?" * len(values)) + ")")
                params += values
        rows = self._heads(namespace, " ".join(where), params)
        if not include_unpublished:
            rows = [r for r in rows if r["publication_state"] == "published"]
        return [present(r, scopes) for r in rows]

    def history(self, namespace: str, record_key: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Every revision of a record in arrival order, including older observations that never became current."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT " + ", ".join(_REVISION_COLUMNS) + " FROM medical_device_revisions WHERE namespace=? AND "
            "record_key=? ORDER BY revision_no", [namespace, record_key]).fetchall()
        return [present(_view(row, {}), scopes) for row in rows]

    def revision(self, namespace: str, revision_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        row = self.conn.execute("SELECT " + ", ".join(_REVISION_COLUMNS) + " FROM medical_device_revisions "
                                "WHERE namespace=? AND revision_id=?", [namespace, revision_id]).fetchone() \
            if self.ready() else None
        if row is None:
            raise MedicalDeviceError("not_found", "revision is not visible in this namespace")
        return present(_view(row, {}), scopes)

    def as_of(self, namespace: str, record_key: str, *, scopes: Iterable[str], published_by: str | None = None,
              known_at_ms: int | None = None) -> dict[str, Any] | None:
        """The revision in force for a record at a date.

        ``published_by`` (YYYY-MM-DD) selects the latest revision the source had published by that date (its
        as-of date, then revision order); ``known_at_ms`` the revision that was current in this store at that
        record time. Older observations never count as current. ``None`` when no revision qualifies.
        """
        views = [v for v in self.history(namespace, record_key, scopes=scopes)
                 if known_at_ms is None or v["observed_at_ms"] <= known_at_ms]
        if published_by is not None:
            views = [v for v in views if v["as_of"] is not None and v["as_of"] <= published_by]
            return max(views, key=lambda v: (v["as_of"], v["revision_order"], v["revision_no"]), default=None)
        views = [v for v in views if v["change"] != "older-observation"]
        return views[-1] if views else None

    def receipts(self, namespace: str, run_id: str | None = None, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "medical_device_receipts"):
            return []
        rows = self.conn.execute(
            "SELECT receipt_id, run_id, source_id, receipt_json, outcome_json, recorded_at_ms FROM "
            "medical_device_receipts WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY recorded_at_ms, "
            "receipt_id", [namespace, run_id, run_id]).fetchall()
        return [{"receipt_id": r[0], "run_id": r[1], "source_id": r[2], "receipt": json.loads(r[3]),
                 "counts": json.loads(r[4]), "recorded_at_ms": r[5]} for r in rows]


class MedicalDeviceProjector:
    """Source-pack runtime projector for ``noesis-medical-device-record-v1`` pages (one selection unit per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = MedicalDeviceStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("medical_devices") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, principal_id
        from src.ingestion.source_packs import SourcePackError

        items = []
        for item in records:
            record = item.get("medical_device_record")
            if not isinstance(record, Mapping):
                raise SourcePackError("mapping_failed", "page record is not a medical-device record")
            items.append(dict(record))
        try:
            return [self.store.project(self._namespace(source), items, run_id=run_id, source_id=source["source_id"],
                                       receipt=dict(page_receipt or {}))]
        except MedicalDeviceError as exc:
            raise SourcePackError("mapping_failed", f"{exc.code}: {exc}") from exc

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        rows = self.store.conn.execute(
            "SELECT count(*) FROM medical_device_receipts WHERE namespace=? AND run_id=? AND source_id=?",
            [self._namespace(source), run_id, source["source_id"]]).fetchone()
        return {"status": status, "units": int(rows[0])}


def readiness(conn: Any, namespace: str = DEFAULT_NAMESPACE) -> dict[str, Any]:
    """Selected features, records per provider, live-verification state and EUDAMED module gaps."""
    from src.ingestion.medical_devices_sources import (
        EUDAMED_MODULES,
        FEATURES,
        LIVE_VERIFICATION,
        PROVIDER_CONTRACTS,
    )

    store = MedicalDeviceStore(conn, initialize=False)
    counts: dict[str, int] = {}
    if store.ready():
        counts = dict(conn.execute("SELECT provider, count(*) FROM medical_device_records WHERE namespace=? "
                                   "GROUP BY provider", [namespace]).fetchall())
    selected = selected_features(conn)
    return {
        "bundle": BUNDLE, "provider": "clinical.devices", "namespace": namespace, "store_ready": store.ready(),
        "features": {FEATURES[p]: FEATURES[p] in selected for p in PROVIDERS},
        "sources": {p: {"feature": FEATURES[p], "selected": FEATURES[p] in selected,
                        "access_decision": c["access_decision"], "live": LIVE_VERIFICATION[p]["status"],
                        "records": int(counts.get(p, 0))} for p, c in PROVIDER_CONTRACTS.items()},
        "eudamed_modules": EUDAMED_MODULES,
        "minimisation": {"id": MINIMISATION_ID, "policy": MINIMISATION["policy"],
                         "restricted": MINIMISATION["restricted"]},
        "review_boundary": REVIEW_BOUNDARY,
        "notice": "unverified-live providers have fixture evidence only; a dated live run is outstanding (#2723)",
    }


__all__ = ["CONTRACT", "EXCLUSIONS", "NARRATIVE_SCOPE", "READ_SCOPE", "RECORD_KINDS", "REVIEW_SCOPE", "WRITE_SCOPE",
           "MedicalDeviceError", "MedicalDeviceProjector", "MedicalDeviceStore", "authorize", "feature_enabled",
           "forbidden_keys", "personal_fields", "present", "readiness", "selected_features", "validate"]
