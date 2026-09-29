"""US Congress and UK Parliament legislation records: projection, receipts and dossier building (#2208, LT02-LT06).

Acquired ``noesis-legislation-record-v1`` records (from
:mod:`src.ingestion.legislation_sources`) are committed as official-record
document revisions through the shared :class:`DocumentStore`
(``political:<source_id>:<record_key>``; a changed record - a new congress.gov
``updateDate``, a new sitting, a corrected division list - is a new document
revision, an unchanged re-acquisition adds nothing). This module keeps only an
*index* of those revisions per namespace (which record belongs to which bill,
which bill a division or debate is a candidate for, the acquisition receipts
and reviewed candidate links) and builds bill dossiers through the existing
:class:`src.domains.political.legislative_dossiers.LegislativeDossierStore`;
there is no parallel dossier store.

A late-arriving older observation (a provider revision stamp older than the one
already current) is logged as ``older-observation`` and never replaces the
current revision. A division or Hansard reference whose source names no bill
stays an unlinked candidate until a reviewer accepts it; accepted, rejected
and reverted reviews keep their history.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.domains.political.legislation_mapping import (
    LEGISLATION_SOURCES,
    RECORD_CONTRACT,
    REVIEW_BOUNDARY,
    LegislationMappingError,
    bill_jurisdiction,
    document_for,
)

CONTRACT = RECORD_CONTRACT
READ_SCOPE = "knowledge:political:legislation:read"
WRITE_SCOPE = "knowledge:political:legislation:write"
REVIEW_SCOPE = "knowledge:political:legislation:review"
DEFAULT_NAMESPACE = "global"
FEATURES = {"US": "legislation-us", "GB": "legislation-uk"}
CHANGES = ("new", "revised", "unchanged", "older-observation")
# Keys that would carry a prediction, a score or a legal-effect reading; no answer may contain them.
FORBIDDEN_ANSWER_KEYS = frozenset({
    "passage_probability", "probability", "prediction", "predicted_outcome", "likelihood", "score",
    "member_score", "ideology", "ideology_score", "rating", "legal_effect", "effect_summary", "verdict",
})

_DDL = """
CREATE TABLE IF NOT EXISTS legislation_records (
  namespace TEXT NOT NULL, record_key TEXT NOT NULL, source_id TEXT NOT NULL, provider TEXT NOT NULL,
  record_kind TEXT NOT NULL, jurisdiction TEXT NOT NULL, bill_key TEXT, candidate_bill_key TEXT,
  link_basis TEXT NOT NULL, document_id TEXT NOT NULL, revision_id TEXT NOT NULL, revision_no INTEGER NOT NULL,
  native_revision TEXT, content_hash TEXT NOT NULL, evidence_origin TEXT NOT NULL, first_run_id TEXT NOT NULL,
  last_run_id TEXT NOT NULL, first_seen_ms BIGINT NOT NULL, updated_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, record_key, source_id)
);
CREATE TABLE IF NOT EXISTS legislation_record_revisions (
  namespace TEXT NOT NULL, record_key TEXT NOT NULL, source_id TEXT NOT NULL, observation_no INTEGER NOT NULL,
  change TEXT NOT NULL, revision_id TEXT, content_hash TEXT NOT NULL, native_revision TEXT, run_id TEXT NOT NULL,
  observed_at_ms BIGINT NOT NULL, record_json TEXT NOT NULL,
  PRIMARY KEY(namespace, record_key, source_id, observation_no)
);
CREATE TABLE IF NOT EXISTS legislation_receipts (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, unit_index INTEGER NOT NULL,
  receipt_json TEXT NOT NULL, outcome_json TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, run_id, source_id, unit_index)
);
CREATE TABLE IF NOT EXISTS legislation_link_reviews (
  namespace TEXT NOT NULL, review_id TEXT NOT NULL, record_key TEXT NOT NULL, source_id TEXT NOT NULL,
  bill_key TEXT NOT NULL, basis TEXT NOT NULL, state TEXT NOT NULL, evidence_json TEXT NOT NULL,
  history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, review_id)
);
CREATE TABLE IF NOT EXISTS legislation_dossier_index (
  namespace TEXT NOT NULL, dossier_namespace TEXT NOT NULL, bill_key TEXT NOT NULL, owner TEXT NOT NULL,
  dossier_id TEXT NOT NULL, jurisdiction TEXT NOT NULL, revision BIGINT NOT NULL, built_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, dossier_namespace, bill_key, owner)
);
"""
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")


class LegislationError(ValueError):
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
        raise LegislationError("unauthorized", f"{required} and namespace access are required")


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def feature_enabled(conn: Any, feature: str) -> bool:
    """Whether the Political bundle's optional ``legislation-us`` / ``legislation-uk`` feature is selected.

    Reads the active composition plan only; defaults to off.
    """
    try:
        if not all(table_exists(conn, t) for t in ("composition_authority", "composition_active",
                                                    "composition_generations", "composition_plans")):
            return False
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle='political'").fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return feature in ((plan.get("features") or {}).get("political") or [])


def _newer_or_equal(native: str | None, current: str | None) -> bool:
    """Whether an observation's provider stamp is not older than the current one (ISO stamps only)."""
    if not native or not current or not _ISO.match(native) or not _ISO.match(current):
        return True
    return native >= current


class LegislationStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        from src.ingestion.document_store import DocumentStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self._documents = DocumentStore(conn) if initialize else None
        if initialize:
            conn.execute(_DDL)

    @property
    def documents(self):
        if self._documents is None:
            from src.ingestion.document_store import DocumentStore

            self._documents = DocumentStore(self.conn)
        return self._documents

    def ready(self) -> bool:
        return table_exists(self.conn, "legislation_records")

    # ------------------------------------------------------------------ projection

    def project(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str, source_id: str,
                receipt: Mapping[str, Any] | None = None, observed_at_ms: int | None = None) -> dict[str, Any]:
        """Commit each record as an official-record document revision and index it. Idempotent."""
        if source_id not in LEGISLATION_SOURCES:
            raise LegislationError("unsupported_source", f"{source_id!r} is not a legislation source")
        observed = int(observed_at_ms if observed_at_ms is not None else self.now())
        counts = dict.fromkeys(CHANGES, 0)
        changed: list[dict[str, Any]] = []
        for record in records:
            record = json.loads(canonical(record))
            try:
                payload = document_for(record, source_id, observed_at_ms=observed)
            except LegislationMappingError as exc:
                raise LegislationError(exc.code, str(exc)) from exc
            content_hash = digest(record)
            row = self.conn.execute(
                "SELECT revision_no, native_revision, content_hash FROM legislation_records "
                "WHERE namespace=? AND record_key=? AND source_id=?",
                [namespace, record["record_key"], source_id]).fetchone()
            if row and row[2] == content_hash:
                counts["unchanged"] += 1
                continue
            if row and not _newer_or_equal(record.get("native_revision"), row[1]):
                self._log(namespace, record, source_id, "older-observation", None, content_hash, run_id, observed)
                counts["older-observation"] += 1
                continue
            outcome = self.documents.upsert([payload])
            if outcome.invalid:
                raise LegislationError("document_invalid", outcome.dead_letter[0]["error"])
            revision_id = outcome.changes[0]["revision_id"]
            link = dict(record.get("bill_link") or {})
            if row is None:
                self.conn.execute(
                    "INSERT INTO legislation_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [namespace, record["record_key"], source_id, record["provider"], record["record_kind"],
                     record["jurisdiction"], record.get("bill_key"), link.get("candidate_bill_key"),
                     link.get("basis") or "none", payload["document_id"], revision_id, 1,
                     record.get("native_revision"), content_hash, record.get("evidence_origin") or "live", run_id,
                     run_id, observed, observed])
                change = "new"
            else:
                self.conn.execute(
                    "UPDATE legislation_records SET bill_key=?, candidate_bill_key=?, link_basis=?, revision_id=?, "
                    "revision_no=revision_no+1, native_revision=?, content_hash=?, evidence_origin=?, last_run_id=?, "
                    "updated_ms=? WHERE namespace=? AND record_key=? AND source_id=?",
                    [record.get("bill_key"), link.get("candidate_bill_key"), link.get("basis") or "none",
                     revision_id, record.get("native_revision"), content_hash,
                     record.get("evidence_origin") or "live", run_id, observed, namespace, record["record_key"],
                     source_id])
                change = "revised"
            self._log(namespace, record, source_id, change, revision_id, content_hash, run_id, observed)
            counts[change] += 1
            changed.append({"record_key": record["record_key"], "change": change, "revision_id": revision_id})
        if receipt is not None:
            self.conn.execute(
                "INSERT OR IGNORE INTO legislation_receipts VALUES (?,?,?,?,?,?,?)",
                [namespace, run_id, source_id, int(receipt.get("unit_index", 0)), canonical(dict(receipt)),
                 canonical({"counts": counts, "changed": changed}), observed])
        return {"source_id": source_id, "run_id": run_id, "counts": counts, "changed": changed}

    def _log(self, namespace, record, source_id, change, revision_id, content_hash, run_id, observed) -> None:
        number = self.conn.execute(
            "SELECT coalesce(max(observation_no), 0) + 1 FROM legislation_record_revisions WHERE namespace=? "
            "AND record_key=? AND source_id=?", [namespace, record["record_key"], source_id]).fetchone()[0]
        self.conn.execute("INSERT INTO legislation_record_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          [namespace, record["record_key"], source_id, int(number), change, revision_id,
                           content_hash, record.get("native_revision"), run_id, observed, canonical(record)])

    # ------------------------------------------------------------------ reads

    _COLUMNS = ("namespace", "record_key", "source_id", "provider", "record_kind", "jurisdiction", "bill_key", "candidate_bill_key",
                "link_basis", "document_id", "revision_id", "revision_no", "native_revision", "content_hash",
                "evidence_origin", "first_run_id", "last_run_id", "first_seen_ms", "updated_ms")

    def _rows(self, where: str, params: list[Any]) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            f"SELECT {', '.join(self._COLUMNS)} FROM legislation_records WHERE {where} "
            "ORDER BY record_kind, record_key, source_id", params).fetchall()
        return [self._view(dict(zip(self._COLUMNS, row))) for row in rows]

    def _view(self, row: dict[str, Any]) -> dict[str, Any]:
        latest = self.conn.execute(
            "SELECT record_json FROM legislation_record_revisions WHERE namespace=? AND record_key=? "
            "AND source_id=? AND revision_id=? ORDER BY observation_no DESC LIMIT 1",
            [row["namespace"], row["record_key"], row["source_id"], row["revision_id"]]).fetchone()
        row["record"] = json.loads(latest[0]) if latest else None
        row["citation"] = {"document_id": row["document_id"], "revision_id": row["revision_id"],
                           "source_id": row["source_id"], "url": (row["record"] or {}).get("locator"),
                           "record_revision_no": row["revision_no"], "native_revision": row["native_revision"],
                           "evidence_origin": row["evidence_origin"]}
        return row

    def records(self, namespace: str, *, scopes: Iterable[str], kinds: Iterable[str] | None = None
                ) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        rows = self._rows("namespace=?", [namespace])
        wanted = set(kinds or [])
        return [r for r in rows if not wanted or r["record_kind"] in wanted]

    def record(self, namespace: str, record_key: str, *, scopes: Iterable[str], source_id: str | None = None
               ) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        rows = self._rows("namespace=? AND record_key=? AND (? IS NULL OR source_id=?)",
                          [namespace, record_key, source_id, source_id])
        if not rows:
            raise LegislationError("not_found", "no legislation record with that key in this namespace")
        if len(rows) > 1:
            raise LegislationError("ambiguous", "record key is held by several sources; name the source")
        return rows[0]

    def records_for_bill(self, namespace: str, bill_key: str, *, scopes: Iterable[str]) -> dict[str, list]:
        authorize(namespace, scopes, READ_SCOPE)
        return {"linked": self._rows("namespace=? AND bill_key=?", [namespace, bill_key]),
                "candidates": self._rows("namespace=? AND bill_key IS NULL AND candidate_bill_key=?",
                                         [namespace, bill_key])}

    def history(self, namespace: str, record_key: str, source_id: str, *, scopes: Iterable[str]
                ) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT observation_no, change, revision_id, content_hash, native_revision, run_id, observed_at_ms, "
            "record_json FROM legislation_record_revisions WHERE namespace=? AND record_key=? AND source_id=? "
            "ORDER BY observation_no", [namespace, record_key, source_id]).fetchall()
        return [{**dict(zip(("observation_no", "change", "revision_id", "content_hash", "native_revision",
                              "run_id", "observed_at_ms"), row[:7])), "record": json.loads(row[7])} for row in rows]

    def receipts(self, namespace: str, run_id: str | None = None, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT run_id, source_id, unit_index, receipt_json, outcome_json, observed_at_ms FROM "
            "legislation_receipts WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY observed_at_ms, source_id, "
            "unit_index", [namespace, run_id, run_id]).fetchall()
        return [{"run_id": r[0], "source_id": r[1], "unit_index": r[2], "receipt": json.loads(r[3]),
                 "outcome": json.loads(r[4]), "observed_at_ms": r[5]} for r in rows]

    # ------------------------------------------------------------------ candidate link review

    def _review_row(self, namespace: str, review_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT review_id, record_key, source_id, bill_key, basis, state, evidence_json, history_json, created_by, "
            "created_at_ms FROM legislation_link_reviews WHERE namespace=? AND review_id=?",
            [namespace, review_id]).fetchone()
        if row is None:
            raise LegislationError("not_found", "no such record-to-bill link review")
        view = dict(zip(("review_id", "record_key", "source_id", "bill_key", "basis", "state"), row[:6]))
        view.update(evidence=json.loads(row[6]), history=json.loads(row[7]), created_by=row[8], created_at_ms=row[9])
        return view

    def link_candidates(self, namespace: str, bill_key: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Divisions and debate references declared for the bill whose source names no bill, with review state."""
        records = self.records_for_bill(namespace, bill_key, scopes=scopes)["candidates"]
        out = []
        for record in records:
            review_id = "legislation-link:" + digest([namespace, record["record_key"], record["source_id"],
                                                      bill_key])[:24]
            try:
                review = self._review_row(namespace, review_id)
            except LegislationError:
                review = None
            out.append({"record_key": record["record_key"], "source_id": record["source_id"],
                        "record_kind": record["record_kind"], "candidate_bill_key": bill_key,
                        "candidate_basis": (record["record"] or {}).get("bill_link", {}).get("candidate_basis"),
                        "review_id": review_id, "state": review["state"] if review else "candidate",
                        "review": review, "citation": record["citation"],
                        "notice": "the source states no bill; unlinked until a reviewer accepts"})
        return out

    def review_link(self, namespace: str, record_key: str, source_id: str, bill_key: str, decision: str,
                    reason: str, *, principal_id: str, scopes: Iterable[str],
                    evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Accept or reject a candidate record-to-bill link, recording reviewer, reason, evidence and time."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise LegislationError("invalid_decision", "accept or reject with a reason")
        record = self.record(namespace, record_key, source_id=source_id, scopes=scopes | {READ_SCOPE})
        if record["bill_key"] is not None:
            raise LegislationError("invalid_state", "the source itself links this record; nothing to review")
        if record["candidate_bill_key"] != bill_key:
            raise LegislationError("not_found", "the record is not a candidate for that bill")
        review_id = "legislation-link:" + digest([namespace, record_key, source_id, bill_key])[:24]
        state = "accepted" if decision == "accept" else "rejected"
        entry = {"state": state, "by": principal_id, "reason": reason.strip(), "at_ms": self.now(),
                 "evidence": dict(evidence or {}), "record_revision_id": record["revision_id"]}
        try:
            current = self._review_row(namespace, review_id)
        except LegislationError:
            current = None
        if current is None:
            self.conn.execute("INSERT INTO legislation_link_reviews VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              [namespace, review_id, record_key, source_id, bill_key, "operator-selection", state,
                               canonical(dict(evidence or {})), canonical([entry]), principal_id, self.now()])
        else:
            if current["state"] in {"accepted", "rejected"}:
                raise LegislationError("invalid_state", f"link is {current['state']}; revert it before re-review")
            self.conn.execute("UPDATE legislation_link_reviews SET state=?, evidence_json=?, history_json=? "
                              "WHERE namespace=? AND review_id=?",
                              [state, canonical(dict(evidence or {})), canonical(current["history"] + [entry]),
                               namespace, review_id])
        return self._review_row(namespace, review_id)

    def revert_link(self, namespace: str, review_id: str, reason: str, *, principal_id: str,
                    scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise LegislationError("invalid_decision", "a revert needs a reason")
        current = self._review_row(namespace, review_id)
        if current["state"] not in {"accepted", "rejected"}:
            raise LegislationError("invalid_state", "only an accepted or rejected link can be reverted")
        history = current["history"] + [{"state": "reverted", "by": principal_id, "reason": reason.strip(),
                                         "at_ms": self.now()}]
        self.conn.execute("UPDATE legislation_link_reviews SET state='reverted', history_json=? WHERE namespace=? "
                          "AND review_id=?", [canonical(history), namespace, review_id])
        return self._review_row(namespace, review_id)

    def accepted_links(self, namespace: str, bill_key: str) -> dict[str, dict[str, Any]]:
        """document id -> accepted review, for the dossier builder."""
        if not table_exists(self.conn, "legislation_link_reviews"):
            return {}
        rows = self.conn.execute(
            "SELECT r.review_id, x.document_id FROM legislation_link_reviews r JOIN legislation_records x ON "
            "x.namespace=r.namespace AND x.record_key=r.record_key AND x.source_id=r.source_id "
            "WHERE r.namespace=? AND r.bill_key=? AND r.state='accepted' ORDER BY r.review_id",
            [namespace, bill_key]).fetchall()
        out = {}
        for review_id, document_id in rows:
            review = self._review_row(namespace, review_id)
            last = review["history"][-1]
            out[document_id] = {"review_id": review_id, "reviewer": last["by"], "reason": last["reason"],
                                "reviewed_at_ms": last["at_ms"], "basis": "reviewed-assertion",
                                "candidate_basis": review["basis"]}
        return out


class LegislationDossiers:
    """Builds and finds bill dossiers in the existing :class:`LegislativeDossierStore`."""

    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None, initialize: bool = True) -> None:
        from src.domains.political.legislative_dossiers import LegislativeDossierStore

        self.conn = conn
        self.store = LegislationStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.dossiers = LegislativeDossierStore(conn, now=self.now, initialize=initialize)

    @staticmethod
    def document_scopes(rows: Iterable[Mapping[str, Any]]) -> set[str]:
        """Legislation documents are public official records: reading them follows the legislation read scope."""
        return {f"document:{row['document_id']}:read" for row in rows}

    def build(self, namespace: str, bill_key: str, dossier_namespace: str, *, principal_id: str,
              scopes: Iterable[str], request_key: str | None = None) -> dict[str, Any]:
        """Save (or idempotently re-save) the bill's dossier revision from the current record revisions."""
        from src.domains.political.legislative_dossiers import DossierError

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        try:
            jurisdiction = bill_jurisdiction(bill_key)
        except LegislationMappingError as exc:
            raise LegislationError(exc.code, str(exc)) from exc
        found = self.store.records_for_bill(namespace, bill_key, scopes=scopes)
        rows = found["linked"] + found["candidates"]
        if not found["linked"]:
            raise LegislationError("none_on_record", f"no acquired record identifies {bill_key}")
        refs = [{"document_id": r["document_id"], "revision_id": r["revision_id"]} for r in rows]
        try:
            saved = self.dossiers.save(
                dossier_namespace, request_key or bill_key, jurisdiction, bill_key, refs,
                principal_id=principal_id, scopes=scopes | self.document_scopes(rows),
                reviewed_links=self.store.accepted_links(namespace, bill_key))
        except DossierError as exc:
            raise LegislationError(exc.code, str(exc)) from exc
        self.conn.execute("INSERT OR REPLACE INTO legislation_dossier_index VALUES (?,?,?,?,?,?,?,?)",
                          [namespace, dossier_namespace, bill_key, principal_id, saved["dossier_id"], jurisdiction,
                           saved["revision"], self.now()])
        return saved

    def find(self, namespace: str, dossier_namespace: str, bill_key: str, owner: str) -> dict[str, Any] | None:
        if not table_exists(self.conn, "legislation_dossier_index"):
            return None
        row = self.conn.execute(
            "SELECT dossier_id, jurisdiction FROM legislation_dossier_index WHERE namespace=? AND "
            "dossier_namespace=? AND bill_key=? AND owner=?", [namespace, dossier_namespace, bill_key, owner]
        ).fetchone()
        return {"dossier_id": row[0], "jurisdiction": row[1]} if row else None

    def dossier(self, namespace: str, dossier_namespace: str, dossier_id: str, *, principal_id: str,
                scopes: Iterable[str], revision: int | None = None) -> dict[str, Any]:
        """A dossier revision in full, read with the document scopes of its legislation sources."""
        from src.domains.political.legislative_dossiers import DossierError

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        try:
            current = self.dossiers._state(dossier_namespace, dossier_id)
            state = self.dossiers._state(dossier_namespace, dossier_id, revision or current["revision"])
            docs = {s["citation"]["document_id"] for s in state["stages"] + state["review_candidates"]}
            return self.dossiers._full(dossier_namespace, dossier_id, state["revision"], principal_id,
                                       scopes | {f"document:{d}:read" for d in docs})
        except DossierError as exc:
            raise LegislationError(exc.code, str(exc)) from exc


class LegislationProjector:
    """Source-pack runtime projector for ``noesis-legislation-record-v1`` pages (one selection unit per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = LegislationStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("legislation") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, principal_id
        items = []
        for item in records:
            record = item.get("legislation_record")
            if not isinstance(record, Mapping):
                raise LegislationError("invalid_record", "page record is not a legislation record")
            items.append(dict(record))
        return [self.store.project(self._namespace(source), items, run_id=run_id, source_id=source["source_id"],
                                   receipt=dict(page_receipt or {}))]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        rows = self.store.conn.execute(
            "SELECT count(*) FROM legislation_receipts WHERE namespace=? AND run_id=? AND source_id=?",
            [self._namespace(source), run_id, source["source_id"]]).fetchone()
        return {"status": status, "units": int(rows[0])}


def readiness(conn: Any) -> dict[str, Any]:
    from src.ingestion.legislation_sources import LIVE_VERIFICATION, PROVIDER_CONTRACTS

    store = LegislationStore(conn, initialize=False)
    counts = {}
    if store.ready():
        counts = dict(conn.execute("SELECT provider, count(*) FROM legislation_records GROUP BY provider").fetchall())
    return {
        "feature": "political.legislation",
        "enabled": {"US": feature_enabled(conn, FEATURES["US"]), "GB": feature_enabled(conn, FEATURES["GB"])},
        "store_ready": store.ready(),
        "providers": {p: {"access_decision": c["access_decision"], "live": LIVE_VERIFICATION[p]["status"],
                          "records": int(counts.get(p, 0))} for p, c in PROVIDER_CONTRACTS.items()},
        "review_boundary": REVIEW_BOUNDARY,
    }


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in an answer that would carry a prediction, member score or legal-effect reading."""
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
