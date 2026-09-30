"""Revisioned record store for the Chemicals and Substances pack (#2212, CH02 #2284).

Owns ``noesis-substance-record-v1``. Every statement an adapter emits is kept
as an immutable revision of its record (provider, record type, subject and
record key): a distinct payload is a new revision with the next sequence
number, a replay adds nothing, and nothing is ever updated in place or
deleted. A removal or amendment is a dated revision, so the prior state
always stays visible. Identifier statements are indexed per subject for
exact-identifier lookup; conflicting identifiers are all kept.

Runtime pages arrive through :class:`SubstanceProjector` (registered for
``noesis-substance-record-v1`` in ``src/ingestion/source_pack_runtime.py``);
each source's run outcome (selection outcomes, cut-off sequence, fixture or
live evidence) is a receipt row.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from src.kb.substances_records import (
    CONTRACT,
    READ_SCOPE,
    SubstanceError,
    authorize,
    canonical,
    digest,
    identifier_key,
    validate_statement,
)

_DDL = """
CREATE SEQUENCE IF NOT EXISTS substance_seq;
CREATE TABLE IF NOT EXISTS substance_records (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, record_type TEXT NOT NULL, provider TEXT NOT NULL,
  record_key TEXT NOT NULL, subject_key TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_name TEXT,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS substance_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL, seq BIGINT NOT NULL,
  revision_no INTEGER NOT NULL, content_sha TEXT NOT NULL, statement_json TEXT NOT NULL, effective_from DATE,
  effective_to DATE, event TEXT NOT NULL, legal_act_json TEXT, observed_at_ms BIGINT NOT NULL, run_id TEXT,
  source_id TEXT, document_id TEXT, evidence_origin TEXT, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS substance_identifiers (
  namespace TEXT NOT NULL, identifier_id TEXT NOT NULL, subject_key TEXT NOT NULL, provider TEXT NOT NULL,
  scheme TEXT NOT NULL, value TEXT NOT NULL, value_key TEXT, revision_id TEXT NOT NULL, conflict BOOLEAN NOT NULL,
  malformed BOOLEAN NOT NULL, PRIMARY KEY(namespace, identifier_id)
);
CREATE TABLE IF NOT EXISTS substance_source_runs (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, provider TEXT, status TEXT NOT NULL,
  outcomes_json TEXT NOT NULL, cutoff_seq BIGINT NOT NULL, evidence_origin TEXT, finished_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, run_id, source_id)
);
"""
TABLES = ("substance_records", "substance_revisions", "substance_identifiers", "substance_source_runs")


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def _day(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, date) else str(value)


def _content(statement: Mapping[str, Any]) -> dict[str, Any]:
    """What makes a revision distinct: the published content, not where or when it was fetched."""
    source = statement.get("source") or {}
    return {
        "subject": statement["subject"], "as_published": statement["as_published"],
        "effective": statement["effective"], "legal_act": statement.get("legal_act"),
        "label": statement.get("label"), "record_version": source.get("record_version"),
        "data_version": source.get("data_version"),
    }


class SubstanceStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            for sql in _DDL.split(";"):
                if sql.strip():
                    conn.execute(sql)

    def ready(self) -> bool:
        return table_exists(self.conn, "substance_revisions")

    def require_ready(self) -> None:
        if not self.ready():
            raise SubstanceError("not_ready", "no substance record has been acquired yet")

    # ------------------------------------------------------------------ writes

    def _apply(self, namespace: str, statement: Mapping[str, Any], *, run_id: str | None, source_id: str | None,
               document_id: str | None, observed_at_ms: int) -> dict[str, Any]:
        value = validate_statement(statement)
        subject = value["subject"]
        record_id = "substance-record:" + digest([namespace, value["provider"], value["record_type"], subject["key"],
                                                  value["record_key"]])[:24]
        self.conn.execute(
            "INSERT OR IGNORE INTO substance_records VALUES (?,?,?,?,?,?,?,?,?)",
            [namespace, record_id, value["record_type"], value["provider"], value["record_key"], subject["key"],
             subject["kind"], subject.get("name"), observed_at_ms])
        content_sha = digest(_content(value))
        revision_id = "substance-revision:" + digest([record_id, content_sha])[:24]
        if self.conn.execute("SELECT 1 FROM substance_revisions WHERE namespace=? AND revision_id=?",
                             [namespace, revision_id]).fetchone():
            return {"record_id": record_id, "revision_id": revision_id, "status": "unchanged"}
        number = self.conn.execute("SELECT count(*) FROM substance_revisions WHERE namespace=? AND record_id=?",
                                   [namespace, record_id]).fetchone()[0] + 1
        seq = self.conn.execute("SELECT nextval('substance_seq')").fetchone()[0]
        effective = value["effective"]
        self.conn.execute(
            "INSERT INTO substance_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, record_id, seq, number, content_sha, canonical(value), effective.get("from"),
             effective.get("to"), effective["event"],
             None if value.get("legal_act") is None else canonical(value["legal_act"]), observed_at_ms, run_id,
             source_id, document_id, value["source"].get("evidence_origin")])
        if value["record_type"] == "identifier":
            published = value["as_published"]
            key = identifier_key(published["scheme"], published["value"])
            self.conn.execute(
                "INSERT OR IGNORE INTO substance_identifiers VALUES (?,?,?,?,?,?,?,?,?,?)",
                [namespace, "substance-identifier:" + digest([namespace, revision_id])[:24], subject["key"],
                 value["provider"], published["scheme"], str(published["value"]), key, revision_id,
                 bool(published.get("conflict")), key is None])
        return {"record_id": record_id, "revision_id": revision_id, "seq": seq, "revision_no": number,
                "status": "created" if number == 1 else "revised"}

    def observe(self, namespace: str, statements: Sequence[Mapping[str, Any]], *, run_id: str | None = None,
                source_id: str | None = None, document_ids: Sequence[str | None] | None = None,
                observed_at_ms: int | None = None) -> dict[str, Any]:
        """Keep a batch of statements in one transaction; replays add nothing."""
        observed = observed_at_ms if observed_at_ms is not None else self.now()
        counts = {"created": 0, "revised": 0, "unchanged": 0}
        results = []
        self.conn.execute("BEGIN")
        try:
            ids = list(document_ids or [])
            for index, item in enumerate(statements):
                result = self._apply(namespace, item, run_id=run_id, source_id=source_id,
                                     document_id=ids[index] if index < len(ids) else None, observed_at_ms=observed)
                counts[result["status"]] += 1
                results.append(result)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"counts": counts, "results": results}

    def apply(self, namespace: str, statement: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
        return self.observe(namespace, [statement], **kwargs)["results"][0]

    def record_run(self, namespace: str, run_id: str, source_id: str, *, provider: str | None, status: str,
                   outcomes: Sequence[Mapping[str, Any]], evidence_origin: str | None) -> dict[str, Any]:
        cutoff = self.conn.execute("SELECT coalesce(max(seq), 0) FROM substance_revisions WHERE namespace=?",
                                   [namespace]).fetchone()[0]
        self.conn.execute(
            "INSERT OR REPLACE INTO substance_source_runs VALUES (?,?,?,?,?,?,?,?,?)",
            [namespace, run_id, source_id, provider, status, canonical(list(outcomes)), int(cutoff), evidence_origin,
             self.now()])
        return {"namespace": namespace, "run_id": run_id, "source_id": source_id, "status": status,
                "cutoff_seq": int(cutoff), "outcomes": list(outcomes)}

    # ------------------------------------------------------------------ reads

    @staticmethod
    def _revision(row: Sequence[Any]) -> dict[str, Any]:
        statement = json.loads(row[6])
        return {"revision_id": row[0], "record_id": row[1], "seq": int(row[2]), "revision_no": int(row[3]),
                "content_sha": row[4], "observed_at_ms": int(row[5]), "statement": statement,
                "effective_from": _day(row[7]), "effective_to": _day(row[8]), "event": row[9],
                "legal_act": json.loads(row[10]) if row[10] else None, "run_id": row[11], "source_id": row[12],
                "document_id": row[13], "evidence_origin": row[14]}

    _REVISION_COLUMNS = ("revision_id, record_id, seq, revision_no, content_sha, observed_at_ms, statement_json, "
                         "effective_from, effective_to, event, legal_act_json, run_id, source_id, document_id, "
                         "evidence_origin")

    def revisions(self, namespace: str, record_id: str, *, cutoff_seq: int | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT {self._REVISION_COLUMNS} FROM substance_revisions WHERE namespace=? AND record_id=? "
            "AND (? IS NULL OR seq<=?) ORDER BY seq", [namespace, record_id, cutoff_seq, cutoff_seq]).fetchall()
        return [self._revision(r) for r in rows]

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {self._REVISION_COLUMNS} FROM substance_revisions WHERE namespace=? AND "
                                "revision_id=?", [namespace, revision_id]).fetchone()
        if row is None:
            raise SubstanceError("not_found", "substance revision is not visible in this namespace")
        return self._revision(row)

    def records(self, namespace: str, *, subject_keys: Iterable[str] | None = None, record_type: str | None = None,
                provider: str | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT record_id, record_type, provider, record_key, subject_key, subject_kind, subject_name "
            "FROM substance_records WHERE namespace=? AND (? IS NULL OR record_type=?) AND (? IS NULL OR provider=?) "
            "ORDER BY subject_key, record_type, record_key, provider",
            [namespace, record_type, record_type, provider, provider]).fetchall()
        wanted = None if subject_keys is None else set(subject_keys)
        return [dict(zip(("record_id", "record_type", "provider", "record_key", "subject_key", "subject_kind",
                          "subject_name"), r)) for r in rows if wanted is None or r[4] in wanted]

    def record(self, namespace: str, record_id: str) -> dict[str, Any]:
        found = [r for r in self.records(namespace) if r["record_id"] == record_id]
        if not found:
            raise SubstanceError("not_found", "substance record is not visible in this namespace")
        return {**found[0], "revisions": self.revisions(namespace, record_id)}

    def subjects(self, namespace: str) -> list[dict[str, Any]]:
        """Every provider subject (a substance record) with its names as published."""
        subjects: dict[str, dict[str, Any]] = {}
        for record in self.records(namespace):
            item = subjects.setdefault(record["subject_key"], {
                "subject_key": record["subject_key"], "kind": record["subject_kind"], "name": record["subject_name"],
                "providers": set()})
            item["providers"].add(record["provider"])
        return [{**s, "providers": sorted(s["providers"])} for _, s in sorted(subjects.items())]

    def identifiers(self, namespace: str, subject_key: str | None = None, *,
                    cutoff_seq: int | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT i.subject_key, i.provider, i.scheme, i.value, i.value_key, i.revision_id, i.conflict, i.malformed "
            "FROM substance_identifiers i JOIN substance_revisions r ON r.namespace=i.namespace AND "
            "r.revision_id=i.revision_id WHERE i.namespace=? AND (? IS NULL OR i.subject_key=?) AND "
            "(? IS NULL OR r.seq<=?) ORDER BY i.subject_key, i.scheme, i.value, i.provider",
            [namespace, subject_key, subject_key, cutoff_seq, cutoff_seq]).fetchall()
        return [dict(zip(("subject_key", "provider", "scheme", "value", "value_key", "revision_id", "conflict",
                          "malformed"), r)) for r in rows]

    def find_subjects(self, namespace: str, scheme: str, key: str) -> list[str]:
        """Subjects that publish exactly this identifier (names compare as exact, case-folded strings)."""
        from src.kb.substances_records import NAME_SCHEMES

        schemes = sorted(NAME_SCHEMES) if scheme == "name" else [scheme]
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT DISTINCT subject_key FROM substance_identifiers WHERE namespace=? AND value_key=? AND scheme IN ("
            + ",".join("?" * len(schemes)) + ") ORDER BY subject_key", [namespace, key, *schemes]).fetchall()
        return [r[0] for r in rows]

    def generation(self, namespace: str) -> int:
        if not self.ready():
            return 0
        return int(self.conn.execute("SELECT coalesce(max(seq), 0) FROM substance_revisions WHERE namespace=?",
                                     [namespace]).fetchone()[0])

    def runs(self, namespace: str, run_id: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "substance_source_runs"):
            return []
        rows = self.conn.execute(
            "SELECT run_id, source_id, provider, status, outcomes_json, cutoff_seq, evidence_origin, finished_at_ms "
            "FROM substance_source_runs WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY finished_at_ms, "
            "source_id", [namespace, run_id, run_id]).fetchall()
        return [{"run_id": r[0], "source_id": r[1], "provider": r[2], "status": r[3], "outcomes": json.loads(r[4]),
                 "cutoff_seq": int(r[5]), "evidence_origin": r[6], "finished_at_ms": int(r[7])} for r in rows]

    def provider_state(self, namespace: str, provider: str) -> dict[str, Any]:
        runs = [r for r in self.runs(namespace) if r["provider"] == provider]
        done = [r for r in runs if r["status"] == "complete"]
        return {"runs": len(runs), "last_success_ms": done[-1]["finished_at_ms"] if done else None,
                "last_evidence_origin": done[-1]["evidence_origin"] if done else None,
                "last_status": runs[-1]["status"] if runs else None}


def read_store(conn: Any, namespace: str, scopes: Iterable[str]) -> SubstanceStore:
    authorize(namespace, scopes, READ_SCOPE)
    store = SubstanceStore(conn, initialize=False)
    store.require_ready()
    return store


class SubstanceProjector:
    """Runtime projector for ``noesis-substance-record-v1`` pages."""

    def __init__(self, conn: Any) -> None:
        self.store = SubstanceStore(conn)
        self._outcomes: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._origin: dict[tuple[str, str], str] = {}

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("substances") or {}).get("namespace") or "global")

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, principal_id
        namespace = self._namespace(source)
        statements = [dict(r["substance_record"]) for r in records if r.get("substance_record")]
        if any(s.get("contract") != CONTRACT for s in statements):
            raise SubstanceError("invalid_record", "page record lacks a substance statement")
        # The runtime's document for each record, by the record id it was normalised from.
        by_id = {str(dict(d.get("metadata") or {}).get("source_pack_record_id")): d["document_id"] for d in documents}
        result = self.store.observe(namespace, statements, run_id=run_id, source_id=source["source_id"],
                                    document_ids=[by_id.get(str(r.get("id"))) for r in records
                                                  if r.get("substance_record")])
        key = (run_id, source["source_id"])
        receipt = dict(page_receipt or {})
        if receipt.get("selection"):
            self._outcomes.setdefault(key, []).append(
                {"selection": receipt["selection"], "outcome": receipt.get("outcome"),
                 "statements": len(statements), "excluded_fields_dropped": receipt.get("excluded_fields_dropped", [])})
        if receipt.get("evidence_origin"):
            self._origin[key] = receipt["evidence_origin"]
        return result["counts"]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        key = (run_id, source["source_id"])
        return self.store.record_run(self._namespace(source), run_id, source["source_id"],
                                     provider=dict(source.get("substances") or {}).get("provider"), status=status,
                                     outcomes=self._outcomes.pop(key, []), evidence_origin=self._origin.pop(key, None))


__all__ = ["SubstanceProjector", "SubstanceStore", "TABLES", "read_store", "table_exists"]
