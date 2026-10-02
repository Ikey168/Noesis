"""Immutable, release-addressable life-science reference records (#2652, LS02 #2661).

One record per (source, record type, native accession); every statement the adapters produce
(:mod:`src.ingestion.lifesci_sources`) is applied as a **revision**:

* a revision is identified by the source's version marker (UniProt entry version, PDB major.minor revision) or, where
  the source publishes none, by the digest of its content. Replaying a marker with the same content adds nothing; the
  same marker with different content is recorded as a **conflict** and never overwrites the stored revision;
* each application also records **release membership** - the source release (UniProt ``2099_01``, ChEMBL
  ``CHEMBL_99``, a declared NCBI or PDB release) in which the revision was observed - so an entry that did not change
  between releases keeps one revision and still answers "as of" every release it was seen in;
* obsoletion, merges, replacements and deletions are revisions with the published status and successors; merges and
  replacements are also ``redirect`` decisions in :class:`src.kb.entity_history.EntityHistoryStore`. Nothing is
  deleted;
* cross-references are stored per revision exactly as published.

As-of lookup (:meth:`LifeSciStore.in_force`) selects the revision in force at a release label or a date: the revision
of the latest release membership not after the requested release (and published by the requested date). A revision
that arrives late for an older release is history and never displaces the one in force for a newer release.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from typing import Any

from src.kb.lifesci_records import (
    _ENTITY_HISTORY_SCOPES,
    CONTRACT,
    INACTIVE,
    SOURCES,
    LifeSciError,
    authorize,
    canonical,
    detect_reference,
    digest,
    native_key,
    record_id_for,
    release_order,
    table_exists,
    validate_statement,
)

_DDL = """
CREATE SEQUENCE IF NOT EXISTS lifesci_seq START 1;
CREATE TABLE IF NOT EXISTS lifesci_records (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, source TEXT NOT NULL, record_type TEXT NOT NULL,
  native_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS lifesci_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL, seq BIGINT NOT NULL,
  marker TEXT NOT NULL, basis TEXT NOT NULL, order_json TEXT, version_date TEXT, content_sha TEXT NOT NULL,
  status TEXT NOT NULL, successors_json TEXT NOT NULL, release_label TEXT NOT NULL, released_on TEXT,
  release_basis TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, run_id TEXT, source_id TEXT, evidence_origin TEXT,
  statement_json TEXT NOT NULL, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS lifesci_release_members (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, release_label TEXT NOT NULL, revision_id TEXT NOT NULL,
  released_on TEXT, release_basis TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, run_id TEXT,
  PRIMARY KEY(namespace, record_id, release_label, revision_id)
);
CREATE TABLE IF NOT EXISTS lifesci_xrefs (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL, position INTEGER NOT NULL,
  database TEXT NOT NULL, xref_id TEXT NOT NULL, relation TEXT, properties_json TEXT NOT NULL,
  PRIMARY KEY(namespace, revision_id, position)
);
CREATE TABLE IF NOT EXISTS lifesci_conflicts (
  namespace TEXT NOT NULL, conflict_id TEXT NOT NULL, record_id TEXT NOT NULL, marker TEXT NOT NULL,
  stored_revision_id TEXT NOT NULL, offered_sha TEXT NOT NULL, offered_json TEXT NOT NULL,
  observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, conflict_id)
);
CREATE TABLE IF NOT EXISTS lifesci_successions (
  namespace TEXT NOT NULL, source TEXT NOT NULL, record_type TEXT NOT NULL, from_id TEXT NOT NULL, to_id TEXT NOT NULL,
  kind TEXT NOT NULL, revision_id TEXT NOT NULL, decision_id TEXT, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, source, record_type, from_id, to_id, revision_id)
);
CREATE TABLE IF NOT EXISTS lifesci_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT, provider TEXT,
  receipt_json TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, receipt_id)
);
"""
TABLES = ("lifesci_records", "lifesci_revisions", "lifesci_release_members", "lifesci_xrefs", "lifesci_conflicts",
          "lifesci_successions", "lifesci_receipts")
_REVISION_COLUMNS = ("revision_id", "record_id", "seq", "marker", "basis", "order_json", "version_date",
                     "content_sha", "status", "successors_json", "release_label", "released_on", "release_basis",
                     "observed_at_ms", "run_id", "source_id", "evidence_origin")


def iso_from_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def as_of_day(value: Any) -> str | None:
    """An as-of date (ISO date or instant; a date means the whole day)."""
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value.isoformat()
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise LifeSciError("invalid_as_of", "as_of is an ISO date (YYYY-MM-DD) or instant") from exc


def _content(statement: Mapping[str, Any]) -> dict[str, Any]:
    """What makes a revision distinct: everything but the release it was observed in."""
    return {k: v for k, v in statement.items() if k != "release"}


class LifeSciStore:
    """Namespace-scoped revisions of gene, protein, structure, taxon and ChEMBL records."""

    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            for sql in _DDL.split(";"):
                if sql.strip():
                    conn.execute(sql)

    def ready(self) -> bool:
        return table_exists(self.conn, "lifesci_revisions")

    def require_ready(self) -> None:
        if not self.ready():
            raise LifeSciError("not_ready", "no life-science record is stored yet; run the life-sciences sources of "
                                            "primary-scientific-evidence")

    # ------------------------------------------------------------------ writes

    def observe_page(self, run_id: str, source: Mapping[str, Any], namespace: str,
                     records: Sequence[Mapping[str, Any]], *, page_receipt: Mapping[str, Any] | None = None
                     ) -> dict[str, Any]:
        """Apply one runtime page in one transaction; a replay adds nothing."""
        statements = []
        for item in records:
            statement = dict(item.get("lifesci_statement") or {})
            if statement.get("contract") != CONTRACT:
                raise LifeSciError("invalid_record", "page record lacks a life-science statement")
            origin = str(dict(item.get("lifesci_page") or {}).get("evidence_origin") or "fixture")
            statements.append((statement, origin))
        counts = {"created": 0, "revised": 0, "history": 0, "unchanged": 0, "conflict": 0}
        results = []
        now = self.now()
        self.conn.execute("BEGIN")
        try:
            for statement, origin in statements:
                result = self._apply(namespace, statement, run_id=run_id, source_id=source.get("source_id"),
                                     evidence_origin=origin, observed_at_ms=now)
                counts[result["status"]] += 1
                results.append(result)
            receipt = dict(page_receipt or {})
            if receipt:
                receipt_id = "lifesci-receipt:" + digest([namespace, run_id, source.get("source_id"), receipt])[:24]
                self.conn.execute("INSERT OR IGNORE INTO lifesci_receipts VALUES (?,?,?,?,?,?,?)",
                                  [namespace, receipt_id, run_id, source.get("source_id"), receipt.get("provider"),
                                   canonical({**receipt, "applied": counts}), now])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"counts": counts, "results": results}

    def apply(self, namespace: str, statement: Mapping[str, Any], *, run_id: str | None = None,
              source_id: str | None = None, evidence_origin: str = "operator") -> dict[str, Any]:
        """Apply one statement outside a runtime page (tests, operator imports); same rules as a page."""
        self.conn.execute("BEGIN")
        try:
            result = self._apply(namespace, dict(statement), run_id=run_id, source_id=source_id,
                                 evidence_origin=evidence_origin, observed_at_ms=self.now())
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return result

    def _apply(self, namespace: str, statement: Mapping[str, Any], *, run_id, source_id, evidence_origin,
               observed_at_ms: int) -> dict[str, Any]:
        statement = validate_statement(statement)
        source, record_type = statement["source"], statement["record_type"]
        native = native_key(source, statement["native_id"])
        record_id = record_id_for(namespace, source, record_type, native)
        version, release = statement["version"], statement["release"]
        content_sha = digest(_content(statement))
        head = self.conn.execute("SELECT 1 FROM lifesci_records WHERE namespace=? AND record_id=?",
                                 [namespace, record_id]).fetchone()
        stored = self.conn.execute(
            "SELECT revision_id, content_sha FROM lifesci_revisions WHERE namespace=? AND record_id=? AND marker=? "
            "AND basis=?", [namespace, record_id, version["marker"], version["basis"]]).fetchone()
        if stored and stored[1] != content_sha:
            conflict_id = "lifesci-conflict:" + digest([namespace, record_id, version["marker"], content_sha])[:24]
            self.conn.execute("INSERT OR IGNORE INTO lifesci_conflicts VALUES (?,?,?,?,?,?,?,?)",
                              [namespace, conflict_id, record_id, version["marker"], stored[0], content_sha,
                               canonical(statement), observed_at_ms])
            return {"status": "conflict", "record_id": record_id, "revision_id": stored[0],
                    "conflict_id": conflict_id}
        if stored:
            revision_id, status = stored[0], "unchanged"
        else:
            if head is None:
                self.conn.execute("INSERT INTO lifesci_records VALUES (?,?,?,?,?,?)",
                                  [namespace, record_id, source, record_type, native, observed_at_ms])
            seq = int(self.conn.execute("SELECT nextval('lifesci_seq')").fetchone()[0])
            revision_id = "lifesci-revision:" + digest([namespace, record_id, version["basis"], version["marker"]])[:24]
            self.conn.execute(
                "INSERT INTO lifesci_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, revision_id, record_id, seq, version["marker"], version["basis"],
                 canonical(version.get("order")) if version.get("order") is not None else None,
                 version.get("date"), content_sha, statement["status"], canonical(statement["successors"]),
                 release["label"], release["published_on"], release["basis"], observed_at_ms, run_id, source_id,
                 evidence_origin, canonical(statement)])
            for position, xref in enumerate(statement["xrefs"]):
                self.conn.execute("INSERT INTO lifesci_xrefs VALUES (?,?,?,?,?,?,?,?)",
                                  [namespace, revision_id, record_id, position, xref["database"], xref["id"],
                                   xref.get("relation"), canonical(xref.get("properties") or {})])
            if statement["status"] in INACTIVE:
                self._succession(namespace, statement, revision_id, observed_at_ms)
            status = "created" if head is None else "pending"
        self.conn.execute("INSERT OR IGNORE INTO lifesci_release_members VALUES (?,?,?,?,?,?,?,?)",
                          [namespace, record_id, release["label"], revision_id, release["published_on"],
                           release["basis"], observed_at_ms, run_id])
        if status == "pending":
            after = self.in_force(namespace, record_id)
            status = "revised" if after and after["revision_id"] == revision_id else "history"
        return {"status": status, "record_id": record_id, "revision_id": revision_id}

    def _succession(self, namespace: str, statement: Mapping[str, Any], revision_id: str, observed: int) -> None:
        from src.kb.entity_history import EntityHistoryStore

        source, record_type = statement["source"], statement["record_type"]
        old = native_key(source, statement["native_id"])
        for target in statement["successors"]:
            new = native_key(source, target)
            decision_id = None
            if statement["status"] in {"merged", "replaced", "demerged", "obsolete"}:
                history = EntityHistoryStore(self.conn, now=self.now)
                left, right = f"lifesci:{source}:{old}", f"lifesci:{source}:{new}"
                for entity in (left, right):
                    history.register_entity(namespace, entity, [entity.split(":", 1)[1]],
                                            principal_id=f"provider:{source}", scopes=_ENTITY_HISTORY_SCOPES)
                decided = history.decide(
                    namespace, "redirect", [left, right],
                    {"source": source, "from": old, "to": new, "revision_id": revision_id,
                     "status": statement["status"], "release": statement["release"]["label"],
                     "provenance": {"producer": "science.life-sciences", "asserted_by": source},
                     "policy": {"merge": False, "note": "the source's own succession, recorded as identity history"}},
                    reviewer_id=f"provider:{source}", principal_id=f"provider:{source}",
                    scopes=_ENTITY_HISTORY_SCOPES, event_key=f"lifesci-succession:{namespace}:{source}:{old}:{new}")
                decision_id = decided["decision_id"]
            self.conn.execute("INSERT OR IGNORE INTO lifesci_successions VALUES (?,?,?,?,?,?,?,?,?)",
                              [namespace, source, record_type, old, new, statement["status"], revision_id,
                               decision_id, observed])

    # ------------------------------------------------------------------ reads

    def record(self, namespace: str, record_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT record_id, source, record_type, native_id FROM lifesci_records WHERE namespace=? AND record_id=?",
            [namespace, record_id]).fetchone() if self.ready() else None
        if row is None:
            raise LifeSciError("not_found", "life-science record is not visible in this namespace")
        return dict(zip(("record_id", "source", "record_type", "native_id"), row))

    def records(self, namespace: str, *, source: str | None = None,
                record_type: str | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT record_id, source, record_type, native_id FROM lifesci_records WHERE namespace=? "
            "AND (? IS NULL OR source=?) AND (? IS NULL OR record_type=?) ORDER BY source, record_type, native_id",
            [namespace, source, source, record_type, record_type]).fetchall()
        return [dict(zip(("record_id", "source", "record_type", "native_id"), r)) for r in rows]

    def find(self, namespace: str, source: str, native_id: str, record_type: str | None = None) -> list[str]:
        """Record ids of a source's native accession (one per record type that uses it)."""
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT record_id FROM lifesci_records WHERE namespace=? AND source=? AND native_id=? "
            "AND (? IS NULL OR record_type=?) ORDER BY record_type",
            [namespace, source, native_key(source, native_id), record_type, record_type]).fetchall()
        return [r[0] for r in rows]

    def resolve(self, namespace: str, reference: str) -> list[str]:
        """Record ids a reference names: a record id, ``source:native`` or a bare accession."""
        text = str(reference or "").strip()
        if text.startswith("lifesci-record:"):
            self.record(namespace, text)
            return [text]
        found: list[str] = []
        for source, native in detect_reference(text):
            found += self.find(namespace, source, native)
        return found

    def _revision_row(self, row: Sequence[Any]) -> dict[str, Any]:
        value = dict(zip(_REVISION_COLUMNS, row))
        value["order"] = json.loads(value.pop("order_json")) if value.get("order_json") else None
        value["successors"] = json.loads(value.pop("successors_json"))
        value["retrieved_at"] = iso_from_ms(value["observed_at_ms"])
        return value

    def revisions(self, namespace: str, record_id: str) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            f"SELECT {', '.join(_REVISION_COLUMNS)} FROM lifesci_revisions WHERE namespace=? AND record_id=? "
            "ORDER BY seq", [namespace, record_id]).fetchall()
        return [self._revision_row(r) for r in rows]

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            f"SELECT {', '.join(_REVISION_COLUMNS)} FROM lifesci_revisions WHERE namespace=? AND revision_id=?",
            [namespace, revision_id]).fetchone() if self.ready() else None
        if row is None:
            raise LifeSciError("not_found", "revision is not visible in this namespace")
        return self._revision_row(row)

    def statement(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT statement_json FROM lifesci_revisions WHERE namespace=? AND revision_id=?",
                                [namespace, revision_id]).fetchone()
        if row is None:
            raise LifeSciError("not_found", "revision is not visible in this namespace")
        return json.loads(row[0])

    def memberships(self, namespace: str, record_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT m.release_label, m.revision_id, m.released_on, m.release_basis, m.observed_at_ms, r.seq "
            "FROM lifesci_release_members m JOIN lifesci_revisions r ON r.namespace=m.namespace AND "
            "r.revision_id=m.revision_id WHERE m.namespace=? AND m.record_id=?", [namespace, record_id]).fetchall()
        items = [dict(zip(("release_label", "revision_id", "released_on", "release_basis", "observed_at_ms", "seq"),
                          r)) for r in rows]
        return sorted(items, key=lambda m: (release_order(m["release_label"]), m["seq"]))

    def releases(self, namespace: str, record_id: str) -> list[dict[str, Any]]:
        """Every release the record was observed in, oldest first, with the revision it carried."""
        return [{k: m[k] for k in ("release_label", "released_on", "release_basis", "revision_id")}
                for m in self.memberships(namespace, record_id)]

    def in_force(self, namespace: str, record_id: str, *, release: str | None = None,
                 as_of: Any = None) -> dict[str, Any] | None:
        """The revision in force at a release label and/or date; the latest when neither is given."""
        day = as_of_day(as_of)
        cutoff = release_order(release) if release else None
        chosen = None
        for member in self.memberships(namespace, record_id):
            if cutoff is not None and release_order(member["release_label"]) > cutoff:
                continue
            published = member["released_on"] or (iso_from_ms(member["observed_at_ms"]) or "")[:10]
            if day is not None and published > day:
                continue
            chosen = member
        return None if chosen is None else self.revision(namespace, chosen["revision_id"])

    def xrefs(self, namespace: str, revision_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT database, xref_id, relation, properties_json FROM lifesci_xrefs WHERE namespace=? AND "
            "revision_id=? ORDER BY position", [namespace, revision_id]).fetchall()
        return [{"database": r[0], "id": r[1], "relation": r[2], "properties": json.loads(r[3])} for r in rows]

    def xrefs_naming(self, namespace: str, databases: Iterable[str], xref_id: str) -> list[dict[str, Any]]:
        """Revisions (any record) whose published cross-references name this id in one of the databases."""
        wanted = sorted(set(databases))
        if not wanted or not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT x.record_id, x.revision_id, x.database, x.xref_id, x.relation FROM lifesci_xrefs x WHERE "
            "x.namespace=? AND upper(x.xref_id)=upper(?) AND x.database IN (" + ",".join("?" * len(wanted)) + ") "
            "ORDER BY x.record_id, x.revision_id", [namespace, xref_id, *wanted]).fetchall()
        return [dict(zip(("record_id", "revision_id", "database", "id", "relation"), r)) for r in rows]

    def conflicts(self, namespace: str, record_id: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "lifesci_conflicts"):
            return []
        rows = self.conn.execute(
            "SELECT conflict_id, record_id, marker, stored_revision_id, offered_sha, observed_at_ms FROM "
            "lifesci_conflicts WHERE namespace=? AND (? IS NULL OR record_id=?) ORDER BY conflict_id",
            [namespace, record_id, record_id]).fetchall()
        return [dict(zip(("conflict_id", "record_id", "marker", "stored_revision_id", "offered_sha",
                          "observed_at_ms"), r)) for r in rows]

    def successions(self, namespace: str, source: str, native_id: str) -> list[dict[str, Any]]:
        key = native_key(source, native_id)
        rows = self.conn.execute(
            "SELECT s.source, s.record_type, s.from_id, s.to_id, s.kind, s.revision_id, s.decision_id, "
            "r.release_label, r.released_on FROM lifesci_successions s JOIN lifesci_revisions r ON "
            "r.namespace=s.namespace AND r.revision_id=s.revision_id WHERE s.namespace=? AND s.source=? AND "
            "(s.from_id=? OR s.to_id=?) ORDER BY r.seq", [namespace, source, key, key]).fetchall()
        return [dict(zip(("source", "record_type", "from_id", "to_id", "kind", "revision_id", "decision_id",
                          "release", "released_on"), r)) for r in rows]

    def receipts(self, namespace: str, source_id: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "lifesci_receipts"):
            return []
        rows = self.conn.execute(
            "SELECT receipt_id, run_id, source_id, provider, receipt_json, observed_at_ms FROM lifesci_receipts "
            "WHERE namespace=? AND (? IS NULL OR source_id=?) ORDER BY observed_at_ms, receipt_id",
            [namespace, source_id, source_id]).fetchall()
        return [{"receipt_id": r[0], "run_id": r[1], "source_id": r[2], "provider": r[3], **json.loads(r[4]),
                 "observed_at": iso_from_ms(r[5])} for r in rows]

    def citation(self, namespace: str, revision: Mapping[str, Any], *, as_of: Any = None,
                 release: str | None = None, answered: bool = True) -> dict[str, Any]:
        """Source, record revision and as-of time for one answer item."""
        head = self.record(namespace, revision["record_id"])
        statement = self.statement(namespace, revision["revision_id"])
        return {"record_id": head["record_id"], "source": head["source"], "record_type": head["record_type"],
                "native_id": head["native_id"], "revision_id": revision["revision_id"],
                "version_marker": revision["marker"], "version_basis": revision["basis"],
                "version_date": revision["version_date"], "status": revision["status"],
                "release": revision["release_label"], "released_on": revision["released_on"],
                "release_basis": revision["release_basis"], "retrieved_at": revision["retrieved_at"],
                "evidence_origin": revision["evidence_origin"], "url": statement.get("url"),
                "licence": statement.get("licence"),
                "as_of": {"release": release, "date": as_of_day(as_of),
                          "answered_at": iso_from_ms(self.now()) if answered else None}}

    def generation(self, namespace: str) -> str:
        """Digest of the namespace's revisions and memberships (monitors use it as their state)."""
        if not self.ready():
            return digest([])
        rows = self.conn.execute(
            "SELECT record_id, release_label, revision_id FROM lifesci_release_members WHERE namespace=? "
            "ORDER BY ALL", [namespace]).fetchall()
        return digest([list(r) for r in rows])[:24]

    def counts(self, namespace: str) -> dict[str, dict[str, int]]:
        if not self.ready():
            return {}
        out: dict[str, dict[str, int]] = {}
        for source, record_type, count in self.conn.execute(
                "SELECT source, record_type, count(*) FROM lifesci_records WHERE namespace=? GROUP BY ALL "
                "ORDER BY ALL", [namespace]).fetchall():
            out.setdefault(source, {})[record_type] = int(count)
        return out


def read_store(conn: Any, namespace: str, scopes: Iterable[str]) -> LifeSciStore:
    from src.kb.lifesci_records import READ_SCOPE

    authorize(namespace, scopes, READ_SCOPE)
    return LifeSciStore(conn, initialize=False)


class LifeSciProjector:
    """Source-pack runtime projector for ``noesis-lifesci-record-v2``."""

    def __init__(self, conn: Any) -> None:
        self.store = LifeSciStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("life_sciences") or {}).get("namespace") or "global")

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, principal_id
        return self.store.observe_page(run_id, source, self._namespace(source), records, page_receipt=page_receipt)

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, principal_id
        return {"source_id": source["source_id"], "status": status}


def selected_features(conn: Any) -> list[str]:
    from src.kb.education_statistics import selected_features as science_features

    return science_features(conn)


def readiness(conn: Any, namespace: str = "global") -> dict[str, Any]:
    from src.ingestion.lifesci_sources import LIVE_VERIFICATION
    from src.kb.lifesci_records import FEATURES

    selected = selected_features(conn)
    store = LifeSciStore(conn, initialize=False)
    counts = store.counts(namespace)
    return {
        "provider": "science.life-sciences",
        "features": {feature: feature in selected for feature in sorted(set(FEATURES.values()))},
        "stores_ready": store.ready(),
        "providers": {source: {"feature": FEATURES[source], "records": counts.get(source, {}),
                               "live_verification": LIVE_VERIFICATION[source]["status"]} for source in SOURCES},
        "note": "offline fixture coverage is never reported as live coverage",
    }


__all__ = ["TABLES", "LifeSciProjector", "LifeSciStore", "as_of_day", "iso_from_ms", "read_store", "readiness"]
