"""Medical-device records: classifications, clearances, approvals with supplements, recalls, adverse-event reports and
report counts, GUDID device identifiers and EUDAMED actors, devices and certificates, versioned as the regulators
published them (#2654, MD02).

Acquired ``noesis-medical-device-record-v1`` records (from
:mod:`src.ingestion.medical_devices_sources`) are kept per source and record
key with an append-only revision log, following the
:mod:`src.kb.entity_history` pattern of never rewriting what was recorded:

* every record carries its source, its record revision and the time it was
  observed; a changed payload for the same record key is a **new revision**
  linked to its predecessor (a recall status change, a supplement newly listed
  on an approval, a new GUDID version, a certificate suspension). A source's
  correction or removal is also a revision, never a deletion; a record that
  disappears from a later response stays on record;
* an older payload delivered later (by the source's own version order) is
  logged as ``older-observation`` and never becomes current; a replayed response
  already on record adds nothing;
* **as-of lookup** (:meth:`MedicalDevicesStore.as_of`) selects the revision in
  force at a date by the source's own date (``effective_on``: decision,
  termination, status or version date), or by the time it was observed
  (``basis="observed"``), and lists the later revisions.

**Minimisation (MD01) is enforced at write time.** A record still carrying a
contact person, a street address, a telephone number, an e-mail address or a
MAUDE patient block is refused with ``minimisation_violation`` before anything
is written, and narrative text is accepted only on adverse-event reports.
Nothing here detects safety signals, infers causality or gives clinical advice.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from src.ingestion.medical_devices_sources import (
    FEATURE_FOR_PROVIDER,
    MINIMISATION,
    RECORD_CONTRACT,
    RECORD_KINDS,
    REVIEW_BOUNDARY,
    minimisation_violations,
)

CONTRACT = RECORD_CONTRACT
READ_SCOPE = "knowledge:clinical:read"
WRITE_SCOPE = "knowledge:clinical:write"
REVIEW_SCOPE = "knowledge:clinical:review"
NARRATIVE_SCOPE = "knowledge:clinical:devices:narratives:read"
DEFAULT_NAMESPACE = "clinical"
BUNDLE = "clinical-evidence"
FEATURES = ("medical-devices-fda", "medical-devices-gudid", "medical-devices-eudamed")
CHANGES = ("new", "revised", "unchanged", "older-observation")
# Stored before the records that refer to them, so a page's references resolve to their current revision.
_ORDER = {kind: i for i, kind in enumerate(("classification", "actor", "clearance", "approval", "supplement",
                                            "device-identifier", "eudamed-device", "certificate", "recall",
                                            "adverse-event-count", "adverse-event-report"))}
EXCLUSIONS = ("safety-signal detection", "causality from adverse-event reports", "clinical advice",
              "patient data beyond what regulators publish")
# Keys that would carry a safety reading, a rate or advice; no answer may contain them.
FORBIDDEN_ANSWER_KEYS = frozenset({
    "rate", "rates", "incidence", "risk", "risk_score", "score", "signal", "safety_signal", "signal_score",
    "disproportionality", "prr", "ror", "causal", "causality", "causation", "verdict", "recommendation", "advice",
    "clinical_advice", "safe", "unsafe", "per_patient", "per_device_rate",
})

_DDL = """
CREATE TABLE IF NOT EXISTS medical_device_records (
  namespace TEXT NOT NULL, source_id TEXT NOT NULL, record_key TEXT NOT NULL, provider TEXT NOT NULL,
  jurisdiction TEXT NOT NULL, record_kind TEXT NOT NULL, parent_key TEXT, current_revision_id TEXT NOT NULL,
  revision_count INTEGER NOT NULL, first_run_id TEXT NOT NULL, first_observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, source_id, record_key)
);
CREATE TABLE IF NOT EXISTS medical_device_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, source_id TEXT NOT NULL, record_key TEXT NOT NULL,
  revision_no INTEGER NOT NULL, previous_revision_id TEXT, change TEXT NOT NULL, content_hash TEXT NOT NULL,
  native_revision TEXT, revision_order TEXT NOT NULL, effective_on TEXT, record_json TEXT NOT NULL,
  narratives INTEGER NOT NULL, evidence_origin TEXT NOT NULL, run_id TEXT NOT NULL, receipt_id TEXT,
  observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS medical_device_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL,
  receipt_json TEXT NOT NULL, outcome_json TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, receipt_id)
);
"""
_REVISION_COLUMNS = ("revision_id", "source_id", "record_key", "revision_no", "previous_revision_id", "change",
                     "content_hash", "native_revision", "revision_order", "effective_on", "record_json",
                     "narratives", "evidence_origin", "run_id", "receipt_id", "observed_at_ms")


class MedicalDevicesError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def observed_at(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat()


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = ({f"namespace:{namespace}:write"} if write
              else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"})
    if required not in scopes or not needed & scopes:
        raise MedicalDevicesError("unauthorized", f"{required} and namespace access are required")


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def feature_enabled(conn: Any, feature: str) -> bool:
    """Whether the Clinical Evidence bundle's optional medical-devices feature is selected (default off).

    Reads the active composition plan only; ``feature`` is one of :data:`FEATURES`.
    """
    try:
        if not all(table_exists(conn, t) for t in ("composition_authority", "composition_active",
                                                    "composition_generations", "composition_plans")):
            return False
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle=?", [BUNDLE]).fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return feature in ((plan.get("features") or {}).get(BUNDLE) or [])


def validate(record: Mapping[str, Any]) -> dict[str, Any]:
    """Structural checks plus the MD01 minimisation guard; returns a canonical copy."""
    record = json.loads(canonical(record))
    if record.get("contract") != CONTRACT or record.get("record_kind") not in RECORD_KINDS:
        raise MedicalDevicesError("invalid_record", "not a medical-device record")
    if not str(record.get("record_key") or "").startswith("medical-devices:"):
        raise MedicalDevicesError("invalid_record", "record keys are medical-devices:* keys")
    if not str(record.get("locator") or "").startswith("https://"):
        raise MedicalDevicesError("invalid_record", "every record cites an HTTPS locator")
    if record.get("provider") == "openfda-device" and not (record.get("disclaimer") or {}).get("text"):
        raise MedicalDevicesError("invalid_record", "an openFDA record carries the openFDA disclaimer")
    if record["record_kind"] in {"adverse-event-report", "adverse-event-count"} and not record.get("caveats"):
        raise MedicalDevicesError("invalid_record", "adverse-event reports and counts carry the source's caveats")
    if record["record_kind"] == "supplement" and not record.get("parent_key"):
        raise MedicalDevicesError("invalid_record", "a supplement names the approval it supplements")
    violations = minimisation_violations(record)
    if violations:
        raise MedicalDevicesError("minimisation_violation", "withheld personal fields may not be stored (MD01)",
                                  paths=violations)
    return record


def _view(row: Sequence[Any], head: Mapping[str, Any]) -> dict[str, Any]:
    revision = dict(zip(_REVISION_COLUMNS, row))
    record = json.loads(revision.pop("record_json"))
    return {
        **head, **revision, "narratives": bool(revision["narratives"]), "record": record,
        "citation": {
            "source_id": revision["source_id"], "provider": head.get("provider") or record.get("provider"),
            "record_key": revision["record_key"], "revision_id": revision["revision_id"],
            "revision_no": revision["revision_no"], "native_revision": revision["native_revision"],
            "effective_on": revision["effective_on"], "locator": record.get("locator"),
            "observed_at_ms": revision["observed_at_ms"], "observed_at": observed_at(revision["observed_at_ms"]),
            "evidence_origin": revision["evidence_origin"],
        },
    }


def redact(row: Mapping[str, Any], scopes: Iterable[str]) -> dict[str, Any]:
    """A record view without MAUDE narrative text unless the principal holds the narrative scope."""
    scopes = set(scopes)
    fields = (row.get("record") or {}).get("fields") or {}
    if not fields.get("narratives") or NARRATIVE_SCOPE in scopes or "operator" in scopes:
        return dict(row)
    record = {**row["record"], "fields": {**fields, "narratives": None,
                                          "narratives_withheld": len(fields["narratives"])}}
    return {**row, "record": record}


class MedicalDevicesStore:
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
        """Append revisions for what changed; idempotent (re-projecting an unchanged record adds nothing)."""
        checked = [validate(r) for r in records]  # refuse the whole page before writing anything
        keys = [(r["record_key"], r.get("native_revision"), r.get("revision_order")) for r in checked]
        if len(set(keys)) != len(keys):
            raise MedicalDevicesError("invalid_record", "a page repeats a record revision")
        observed = int(observed_at_ms if observed_at_ms is not None else self.now())
        receipt = dict(receipt or {})
        receipt_id = "md-receipt:" + digest([namespace, source_id, run_id, receipt, [k[0] for k in keys]])[:24]
        counts = dict.fromkeys(CHANGES, 0)
        self.conn.execute("BEGIN")
        try:
            for record in sorted(checked, key=lambda r: (_ORDER[r["record_kind"]], r["record_key"],
                                                         r.get("revision_order") or "")):
                counts[self._observe(namespace, record, source_id, run_id, receipt_id, observed)] += 1
            self.conn.execute("INSERT OR IGNORE INTO medical_device_receipts VALUES (?,?,?,?,?,?,?)",
                              [namespace, receipt_id, run_id, source_id, canonical(receipt), canonical(counts),
                               observed])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"receipt_id": receipt_id, "counts": counts, "records": len(checked)}

    def _observe(self, namespace, record, source_id, run_id, receipt_id, observed) -> str:
        key = record["record_key"]
        origin = "fixture" if record.get("evidence_origin") == "fixture" else "live"
        body = {k: v for k, v in record.items() if k != "evidence_origin"}
        content_hash = digest(body)
        head = self.conn.execute(
            "SELECT r.current_revision_id, r.revision_count, v.content_hash, v.revision_order "
            "FROM medical_device_records r JOIN medical_device_revisions v ON v.namespace=r.namespace AND "
            "v.revision_id=r.current_revision_id WHERE r.namespace=? AND r.source_id=? AND r.record_key=?",
            [namespace, source_id, key]).fetchone()
        order = str(record.get("revision_order") or "")
        if head is not None:
            if head[2] == content_hash:
                return "unchanged"
            if self.conn.execute(
                    "SELECT 1 FROM medical_device_revisions WHERE namespace=? AND source_id=? AND record_key=? AND "
                    "content_hash=?", [namespace, source_id, key, content_hash]).fetchone():
                return "unchanged"  # a replayed older response: already on record, never re-applied
            change = "older-observation" if order < str(head[3] or "") else "revised"
        else:
            change = "new"
        revision_no = 1 if head is None else int(head[1]) + 1
        revision_id = "md-rev:" + digest([namespace, source_id, key, revision_no, content_hash])[:24]
        narratives = len(((record.get("fields") or {}).get("narratives")) or [])
        self.conn.execute(
            "INSERT INTO medical_device_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, source_id, key, revision_no, None if head is None else head[0], change,
             content_hash, record.get("native_revision"), order, record.get("effective_on"), canonical(record),
             narratives, origin, run_id, receipt_id, observed])
        if head is None:
            self.conn.execute("INSERT INTO medical_device_records VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              [namespace, source_id, key, record["provider"], record["jurisdiction"],
                               record["record_kind"], record.get("parent_key"), revision_id, 1, run_id, observed])
        elif change == "older-observation":
            self.conn.execute("UPDATE medical_device_records SET revision_count=? WHERE namespace=? AND source_id=? "
                              "AND record_key=?", [revision_no, namespace, source_id, key])
        else:
            self.conn.execute("UPDATE medical_device_records SET current_revision_id=?, revision_count=?, "
                              "parent_key=? WHERE namespace=? AND source_id=? AND record_key=?",
                              [revision_id, revision_no, record.get("parent_key"), namespace, source_id, key])
        return change

    # ------------------------------------------------------------------ reads

    def records(self, namespace: str, *, scopes: Iterable[str], kinds: Iterable[str] | None = None,
                record_keys: Iterable[str] | None = None, providers: Iterable[str] | None = None,
                parent_key: str | None = None) -> list[dict[str, Any]]:
        """Current revision of every matching record (per source); narratives only with the narrative scope."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not self.ready():
            return []
        where, params = [], []
        if parent_key is not None:
            where.append("AND r.parent_key=?")
            params.append(parent_key)
        for column, values in (("r.record_kind", kinds), ("r.record_key", record_keys), ("r.provider", providers)):
            if values is not None:
                values = sorted(set(values))
                if not values:
                    return []
                where.append(f"AND {column} IN (" + ",".join("?" * len(values)) + ")")
                params += values
        rows = self.conn.execute(
            "SELECT r.provider, r.jurisdiction, r.record_kind, r.parent_key, r.revision_count, r.first_observed_at_ms, "
            + ", ".join(f"v.{c}" for c in _REVISION_COLUMNS) +
            " FROM medical_device_records r JOIN medical_device_revisions v ON v.namespace=r.namespace AND "
            "v.revision_id=r.current_revision_id WHERE r.namespace=? " + " ".join(where) +
            " ORDER BY r.record_kind, r.record_key, r.source_id", [namespace, *params]).fetchall()
        out = []
        for row in rows:
            head = dict(zip(("provider", "jurisdiction", "record_kind", "parent_key", "revision_count",
                             "first_observed_at_ms"), row[:6]))
            out.append(redact(_view(row[6:], head), scopes))
        return out

    def history(self, namespace: str, record_key: str, *, scopes: Iterable[str], source_id: str | None = None
                ) -> list[dict[str, Any]]:
        """Every revision of a record in arrival order, including older observations that never became current."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT v." + ", v.".join(_REVISION_COLUMNS) + ", r.provider, r.jurisdiction, r.record_kind "
            "FROM medical_device_revisions v JOIN medical_device_records r ON r.namespace=v.namespace AND "
            "r.source_id=v.source_id AND r.record_key=v.record_key WHERE v.namespace=? AND v.record_key=? AND "
            "(? IS NULL OR v.source_id=?) ORDER BY v.source_id, v.revision_no",
            [namespace, record_key, source_id, source_id]).fetchall()
        n = len(_REVISION_COLUMNS)
        return [redact(_view(row[:n], dict(zip(("provider", "jurisdiction", "record_kind"), row[n:]))), scopes)
                for row in rows]

    def revision(self, namespace: str, revision_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        row = self.conn.execute("SELECT " + ", ".join(_REVISION_COLUMNS) + " FROM medical_device_revisions "
                                "WHERE namespace=? AND revision_id=?", [namespace, revision_id]).fetchone()
        if row is None:
            raise MedicalDevicesError("not_found", "revision is not visible in this namespace")
        return redact(_view(row, {}), scopes)

    def as_of(self, namespace: str, record_key: str, as_of: str, *, scopes: Iterable[str],
              source_id: str | None = None, basis: str = "published") -> dict[str, Any]:
        """The revision in force at a date per source, and the revisions after it.

        ``basis="published"`` uses the source's own date (``effective_on``; a revision without a published date,
        such as a classification, is in force at any date and marked ``dated: false``); ``basis="observed"`` uses the observation time (what was on record at that date).
        Older observations never become current. Nothing in force yet is ``not_yet_published``.
        """
        if basis not in {"published", "observed"}:
            raise MedicalDevicesError("invalid_request", "basis is published or observed")
        day = str(as_of)[:10]
        by_source: dict[str, list[dict[str, Any]]] = {}
        for revision in self.history(namespace, record_key, scopes=scopes, source_id=source_id):
            if revision["change"] != "older-observation":
                by_source.setdefault(revision["source_id"], []).append(revision)
        out = []
        for source, revisions in sorted(by_source.items()):
            def when(r: Mapping[str, Any]) -> str:
                seen = (observed_at(r["observed_at_ms"]) or "")[:10]
                return seen if basis == "observed" else (r["effective_on"] or "")

            eligible = [r for r in revisions if when(r) <= day]
            chosen = max(eligible, key=lambda r: (when(r), r["revision_no"])) if eligible else None
            out.append({"source_id": source, "as_of": day, "basis": basis,
                        "status": "in_force" if chosen else "not_yet_published",
                        "dated": bool(chosen and (basis == "observed" or chosen["effective_on"])),
                        "revision": chosen, "later_revisions": [
                            {"revision_id": r["revision_id"], "revision_no": r["revision_no"], "date": when(r),
                             "citation": r["citation"]} for r in revisions if chosen is None or
                            r["revision_no"] > chosen["revision_no"]]})
        return {"record_key": record_key, "as_of": day, "basis": basis, "sources": out,
                "status": "answered" if out else "none_on_record"}

    def receipts(self, namespace: str, run_id: str | None = None, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT receipt_id, run_id, source_id, receipt_json, outcome_json, recorded_at_ms FROM "
            "medical_device_receipts WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY recorded_at_ms, "
            "receipt_id", [namespace, run_id, run_id]).fetchall()
        return [{"receipt_id": r[0], "run_id": r[1], "source_id": r[2], "receipt": json.loads(r[3]),
                 "counts": json.loads(r[4]), "recorded_at_ms": r[5]} for r in rows]


class MedicalDevicesProjector:
    """Source-pack runtime projector for ``noesis-medical-device-record-v1`` pages (one selection unit per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = MedicalDevicesStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("medical_devices") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, principal_id
        items = []
        for item in records:
            record = item.get("medical_device_record")
            if not isinstance(record, Mapping):
                raise MedicalDevicesError("invalid_record", "page record is not a medical-device record")
            items.append(dict(record))
        return [self.store.project(self._namespace(source), items, run_id=run_id, source_id=source["source_id"],
                                   receipt=dict(page_receipt or {}))]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        rows = self.store.conn.execute(
            "SELECT count(*) FROM medical_device_receipts WHERE namespace=? AND run_id=? AND source_id=?",
            [self._namespace(source), run_id, source["source_id"]]).fetchone()
        return {"status": status, "units": int(rows[0])}


def readiness(conn: Any) -> dict[str, Any]:
    from src.ingestion.medical_devices_sources import (
        BOUNDED_COVERAGE,
        EUDAMED_MODULES,
        LIVE_VERIFICATION,
        PROVIDER_CONTRACTS,
    )

    store = MedicalDevicesStore(conn, initialize=False)
    counts: dict[str, int] = {}
    origins: dict[str, list[str]] = {}
    if store.ready():
        counts = dict(conn.execute("SELECT provider, count(*) FROM medical_device_records GROUP BY provider"
                                   ).fetchall())
        for provider, origin in conn.execute(
                "SELECT DISTINCT r.provider, v.evidence_origin FROM medical_device_records r JOIN "
                "medical_device_revisions v ON v.namespace=r.namespace AND v.source_id=r.source_id AND "
                "v.record_key=r.record_key ORDER BY 1, 2").fetchall():
            origins.setdefault(provider, []).append(origin)
    return {
        "feature": "clinical.devices",
        "enabled": {feature: feature_enabled(conn, feature) for feature in FEATURES},
        "store_ready": store.ready(),
        "providers": {p: {"access_decision": c["access_decision"], "live": LIVE_VERIFICATION[p]["status"],
                          "feature": FEATURE_FOR_PROVIDER.get(p), "records": int(counts.get(p, 0)),
                          "evidence_origins": origins.get(p, [])} for p, c in PROVIDER_CONTRACTS.items()},
        "eudamed_modules": EUDAMED_MODULES,
        "bounded_coverage": BOUNDED_COVERAGE,
        "minimisation": {"policy": MINIMISATION["policy"], "narratives": MINIMISATION["narratives"]},
        "review_boundary": REVIEW_BOUNDARY,
        "note": "offline (fixture) and live evidence are reported per revision (evidence_origin); no provider is "
                "live until a dated run verifies it (MD14, #2723)",
    }


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in an answer that would carry a rate, a signal, a causal reading or advice."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_ANSWER_KEYS:
                found.append(f"{path}.{key}")
            found += forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}[{index}]")
    return found
