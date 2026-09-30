"""Revisioned record store for the Fisheries and Maritime Activity pack (#2222, FI02 #2307).

Owns ``noesis-fisheries-record-v1``. Every statement an adapter emits is kept
as an immutable revision of its record (provider, record type, subject and
record key): a payload that differs from the record's latest revision is a new
revision with the next number, a replay adds nothing, and nothing is updated
in place or deleted. A newer FishStat release or GFW dataset version is a new
revision that supersedes the prior one, which stays readable.

Register and list pages are **snapshots**: each is recorded with its snapshot
date (as published) and its retrieval time. A record last seen in an earlier
snapshot of the same list and selection and absent from a later one receives
a dated ``removed`` revision at that snapshot - it is never deleted, and
absence is only ever compared within one list and one declared selection.

Runtime pages arrive through :class:`FisheriesProjector` (registered for
``noesis-fisheries-record-v1`` in ``src/ingestion/source_pack_runtime.py``);
each source's run outcome is a receipt row.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from src.kb.fisheries_records import (
    CONTRACT,
    READ_SCOPE,
    FisheriesError,
    authorize,
    call_sign_key,
    canonical,
    digest,
    flag_code,
    imo_key,
    name_key,
    validate_statement,
)

_DDL = """
CREATE SEQUENCE IF NOT EXISTS fisheries_seq;
CREATE TABLE IF NOT EXISTS fisheries_records (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, record_type TEXT NOT NULL, provider TEXT NOT NULL,
  record_key TEXT NOT NULL, subject_key TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_name TEXT,
  list_key TEXT, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS fisheries_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL, seq BIGINT NOT NULL,
  revision_no INTEGER NOT NULL, content_sha TEXT NOT NULL, statement_json TEXT NOT NULL, effective_from DATE,
  effective_to DATE, event TEXT NOT NULL, snapshot_date DATE, release TEXT, supersedes TEXT,
  observed_at_ms BIGINT NOT NULL, run_id TEXT, source_id TEXT, document_id TEXT, evidence_origin TEXT,
  PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS fisheries_identifiers (
  namespace TEXT NOT NULL, identifier_id TEXT NOT NULL, subject_key TEXT NOT NULL, provider TEXT NOT NULL,
  scheme TEXT NOT NULL, value TEXT NOT NULL, value_key TEXT, revision_id TEXT NOT NULL,
  PRIMARY KEY(namespace, identifier_id)
);
CREATE TABLE IF NOT EXISTS fisheries_snapshots (
  namespace TEXT NOT NULL, snapshot_id TEXT NOT NULL, list_key TEXT NOT NULL, provider TEXT NOT NULL,
  list_kind TEXT NOT NULL, snapshot_date DATE, release TEXT, selection_key TEXT NOT NULL, entry_count INTEGER NOT NULL,
  url TEXT, run_id TEXT, source_id TEXT, removed INTEGER NOT NULL, observed_at_ms BIGINT NOT NULL,
  evidence_origin TEXT, members_json TEXT NOT NULL, PRIMARY KEY(namespace, snapshot_id)
);
CREATE TABLE IF NOT EXISTS fisheries_source_runs (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, provider TEXT, status TEXT NOT NULL,
  outcomes_json TEXT NOT NULL, cutoff_seq BIGINT NOT NULL, evidence_origin TEXT, finished_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, run_id, source_id)
);
"""
TABLES = ("fisheries_records", "fisheries_revisions", "fisheries_identifiers", "fisheries_snapshots",
          "fisheries_source_runs")
# Identifier schemes indexed per revision for exact lookup and identity matching.
IDENTIFIER_FIELDS = {
    "imo": ("imo",), "name": ("vessel_name", "shipname"), "flag": ("flag",), "call_sign": ("call_sign", "callsign"),
    "register_number": ("register_number",), "gfw_vessel_id": ("gfw_vessel_id",), "mmsi": ("mmsi",),
    "list_entry": ("list_entry",),
}


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def _day(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if isinstance(value, date) else str(value)


def _content(statement: Mapping[str, Any]) -> dict[str, Any]:
    """What makes a revision distinct: the published content and its release, not when it was fetched."""
    source = statement.get("source") or {}
    return {"subject": statement["subject"], "as_published": statement["as_published"],
            "effective": statement["effective"], "release": source.get("release"),
            "dataset_version": source.get("dataset_version")}


def identifier_key(scheme: str, value: Any) -> str | None:
    if scheme == "imo":
        return imo_key(value)
    if scheme == "name":
        return name_key(value) or None
    if scheme == "flag":
        return flag_code(value)
    if scheme == "call_sign":
        return call_sign_key(value)
    text = str(value or "").strip()
    return text or None


def list_key_of(value: Mapping[str, Any]) -> str | None:
    kind = (value.get("source") or {}).get("list")
    return f"{value['provider']}:{kind}" if kind else None


class FisheriesStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            for sql in _DDL.split(";"):
                if sql.strip():
                    conn.execute(sql)

    def ready(self) -> bool:
        return table_exists(self.conn, "fisheries_revisions")

    def require_ready(self) -> None:
        if not self.ready():
            raise FisheriesError("not_ready", "no fisheries record has been acquired yet")

    # ------------------------------------------------------------------ writes

    def _latest(self, namespace: str, record_id: str) -> tuple[str, int, str] | None:
        row = self.conn.execute(
            "SELECT content_sha, revision_no, revision_id FROM fisheries_revisions WHERE namespace=? AND record_id=? "
            "ORDER BY revision_no DESC LIMIT 1", [namespace, record_id]).fetchone()
        return (row[0], int(row[1]), row[2]) if row else None

    def _apply(self, namespace: str, statement: Mapping[str, Any], *, run_id: str | None, source_id: str | None,
               document_id: str | None, observed_at_ms: int) -> dict[str, Any]:
        value = validate_statement(statement)
        subject = value["subject"]
        record_id = "fisheries-record:" + digest([namespace, value["provider"], value["record_type"], subject["key"],
                                                  value["record_key"]])[:24]
        self.conn.execute(
            "INSERT OR IGNORE INTO fisheries_records VALUES (?,?,?,?,?,?,?,?,?,?)",
            [namespace, record_id, value["record_type"], value["provider"], value["record_key"], subject["key"],
             subject["kind"], subject.get("name"), list_key_of(value), observed_at_ms])
        content_sha = digest(_content(value))
        latest = self._latest(namespace, record_id)
        if latest and latest[0] == content_sha:
            return {"record_id": record_id, "revision_id": latest[2], "status": "unchanged"}
        number = (latest[1] if latest else 0) + 1
        revision_id = "fisheries-revision:" + digest([record_id, content_sha, number])[:24]
        seq = self.conn.execute("SELECT nextval('fisheries_seq')").fetchone()[0]
        effective, source = value["effective"], value["source"]
        self.conn.execute(
            "INSERT INTO fisheries_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, record_id, seq, number, content_sha, canonical(value), effective.get("from"),
             effective.get("to"), effective["event"], source.get("snapshot_date"), source.get("release"),
             latest[2] if latest else None, observed_at_ms, run_id, source_id, document_id,
             source.get("evidence_origin")])
        published = value["as_published"]
        for scheme, fields in IDENTIFIER_FIELDS.items():
            for field in fields:
                raw = published.get(field)
                if raw in (None, ""):
                    continue
                self.conn.execute(
                    "INSERT OR IGNORE INTO fisheries_identifiers VALUES (?,?,?,?,?,?,?,?)",
                    [namespace, "fisheries-identifier:" + digest([revision_id, scheme, field])[:24], subject["key"],
                     value["provider"], scheme, str(raw), identifier_key(scheme, raw), revision_id])
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

    def close_snapshot(self, namespace: str, *, list_key: str, provider: str, list_kind: str,
                       snapshot_date: str | None, release: str | None, selection_key: str,
                       present_record_ids: Iterable[str], url: str | None, run_id: str | None,
                       source_id: str | None, evidence_origin: str | None,
                       observed_at_ms: int | None = None) -> dict[str, Any]:
        """Record one list snapshot; entries last seen in the previous snapshot and absent now get a removal."""
        observed = observed_at_ms if observed_at_ms is not None else self.now()
        present = set(present_record_ids)
        snapshot_id = "fisheries-snapshot:" + digest([namespace, list_key, snapshot_date, release, selection_key,
                                                      sorted(present)])[:24]
        if self.conn.execute("SELECT 1 FROM fisheries_snapshots WHERE namespace=? AND snapshot_id=?",
                             [namespace, snapshot_id]).fetchone():
            return {"snapshot_id": snapshot_id, "status": "unchanged", "removed": []}
        previous = self.conn.execute(
            "SELECT snapshot_date, selection_key, members_json FROM fisheries_snapshots WHERE namespace=? AND list_key=? "
            "ORDER BY snapshot_date DESC NULLS LAST, observed_at_ms DESC LIMIT 1", [namespace, list_key]).fetchone()
        removed: list[str] = []
        note = None
        if list_kind in {"authorised-vessels", "iuu-vessels"} and previous is not None:
            if previous[1] != selection_key:
                note = "selection changed since the previous snapshot; absence is not compared"
            elif snapshot_date and previous[0] and snapshot_date < _day(previous[0]):
                note = "snapshot is older than the latest recorded one; absence is not compared"
            else:
                removed = self._remove_absent(namespace, set(json.loads(previous[2])) - present, snapshot_date,
                                              run_id, source_id, observed)
        self.conn.execute(
            "INSERT INTO fisheries_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, snapshot_id, list_key, provider, list_kind, snapshot_date, release, selection_key,
             len(present), url, run_id, source_id, len(removed), observed, evidence_origin,
             canonical(sorted(present))])
        return {"snapshot_id": snapshot_id, "status": "recorded", "removed": removed, "note": note}

    def _remove_absent(self, namespace, absent, snapshot_date, run_id, source_id, observed) -> list[str]:
        """Entries of the previous snapshot missing from this one get a dated removal revision."""
        removed = []
        for record_id in sorted(absent):
            latest = self.revisions(namespace, record_id)[-1]
            if latest["event"] == "removed":
                continue
            prior = latest["statement"]
            removal = {**prior, "effective": {"from": snapshot_date, "to": None, "event": "removed",
                                              "date_basis": f"absent from the list snapshot of {snapshot_date}; "
                                                            "the entry is kept, not deleted"},
                       "source": {**prior["source"], "snapshot_date": snapshot_date}}
            self._apply(namespace, removal, run_id=run_id, source_id=source_id, document_id=None,
                        observed_at_ms=observed)
            removed.append(record_id)
        return removed

    def record_run(self, namespace: str, run_id: str, source_id: str, *, provider: str | None, status: str,
                   outcomes: Sequence[Mapping[str, Any]], evidence_origin: str | None) -> dict[str, Any]:
        cutoff = self.conn.execute("SELECT coalesce(max(seq), 0) FROM fisheries_revisions WHERE namespace=?",
                                   [namespace]).fetchone()[0]
        self.conn.execute(
            "INSERT OR REPLACE INTO fisheries_source_runs VALUES (?,?,?,?,?,?,?,?,?)",
            [namespace, run_id, source_id, provider, status, canonical(list(outcomes)), int(cutoff), evidence_origin,
             self.now()])
        return {"namespace": namespace, "run_id": run_id, "source_id": source_id, "status": status,
                "cutoff_seq": int(cutoff), "outcomes": list(outcomes)}

    # ------------------------------------------------------------------ reads

    _REVISION_COLUMNS = ("revision_id, record_id, seq, revision_no, content_sha, observed_at_ms, statement_json, "
                         "effective_from, effective_to, event, snapshot_date, release, supersedes, run_id, source_id, "
                         "document_id, evidence_origin")

    @staticmethod
    def _revision(row: Sequence[Any]) -> dict[str, Any]:
        return {"revision_id": row[0], "record_id": row[1], "seq": int(row[2]), "revision_no": int(row[3]),
                "content_sha": row[4], "observed_at_ms": int(row[5]), "statement": json.loads(row[6]),
                "effective_from": _day(row[7]), "effective_to": _day(row[8]), "event": row[9],
                "snapshot_date": _day(row[10]), "release": row[11], "supersedes": row[12], "run_id": row[13],
                "source_id": row[14], "document_id": row[15], "evidence_origin": row[16]}

    def revisions(self, namespace: str, record_id: str, *, cutoff_seq: int | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT {self._REVISION_COLUMNS} FROM fisheries_revisions WHERE namespace=? AND record_id=? "
            "AND (? IS NULL OR seq<=?) ORDER BY seq", [namespace, record_id, cutoff_seq, cutoff_seq]).fetchall()
        return [self._revision(r) for r in rows]

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {self._REVISION_COLUMNS} FROM fisheries_revisions WHERE namespace=? AND "
                                "revision_id=?", [namespace, revision_id]).fetchone()
        if row is None:
            raise FisheriesError("not_found", "fisheries revision is not visible in this namespace")
        return self._revision(row)

    def records(self, namespace: str, *, subject_keys: Iterable[str] | None = None, record_type: str | None = None,
                provider: str | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT record_id, record_type, provider, record_key, subject_key, subject_kind, subject_name, list_key "
            "FROM fisheries_records WHERE namespace=? AND (? IS NULL OR record_type=?) AND (? IS NULL OR provider=?) "
            "ORDER BY subject_key, record_type, record_key, provider",
            [namespace, record_type, record_type, provider, provider]).fetchall()
        wanted = None if subject_keys is None else set(subject_keys)
        return [dict(zip(("record_id", "record_type", "provider", "record_key", "subject_key", "subject_kind",
                          "subject_name", "list_key"), r)) for r in rows if wanted is None or r[4] in wanted]

    def subjects(self, namespace: str, *, kind: str | None = None) -> list[dict[str, Any]]:
        subjects: dict[str, dict[str, Any]] = {}
        for record in self.records(namespace):
            if kind and record["subject_kind"] != kind:
                continue
            item = subjects.setdefault(record["subject_key"], {
                "subject_key": record["subject_key"], "kind": record["subject_kind"], "name": record["subject_name"],
                "providers": set(), "record_types": set()})
            item["providers"].add(record["provider"])
            item["record_types"].add(record["record_type"])
        return [{**s, "providers": sorted(s["providers"]), "record_types": sorted(s["record_types"])}
                for _, s in sorted(subjects.items())]

    def identifiers(self, namespace: str, subject_key: str | None = None, *,
                    cutoff_seq: int | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT i.subject_key, i.provider, i.scheme, i.value, i.value_key, i.revision_id, r.snapshot_date, "
            "r.effective_from, r.seq FROM fisheries_identifiers i JOIN fisheries_revisions r ON "
            "r.namespace=i.namespace AND r.revision_id=i.revision_id WHERE i.namespace=? AND "
            "(? IS NULL OR i.subject_key=?) AND (? IS NULL OR r.seq<=?) ORDER BY i.subject_key, r.seq, i.scheme",
            [namespace, subject_key, subject_key, cutoff_seq, cutoff_seq]).fetchall()
        return [dict(zip(("subject_key", "provider", "scheme", "value", "value_key", "revision_id", "snapshot_date",
                          "effective_from", "seq"), (*r[:6], _day(r[6]), _day(r[7]), int(r[8])))) for r in rows]

    def find_subjects(self, namespace: str, scheme: str, key: str) -> list[str]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT DISTINCT subject_key FROM fisheries_identifiers WHERE namespace=? AND scheme=? AND value_key=? "
            "ORDER BY subject_key", [namespace, scheme, key]).fetchall()
        return [r[0] for r in rows]

    def snapshots(self, namespace: str, *, list_key: str | None = None, as_of: str | None = None,
                  provider: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "fisheries_snapshots"):
            return []
        rows = self.conn.execute(
            "SELECT snapshot_id, list_key, provider, list_kind, snapshot_date, release, selection_key, entry_count, url, "
            "run_id, source_id, removed, observed_at_ms, evidence_origin FROM fisheries_snapshots WHERE namespace=? "
            "AND (? IS NULL OR list_key=?) AND (? IS NULL OR provider=?) ORDER BY list_key, snapshot_date, "
            "observed_at_ms", [namespace, list_key, list_key, provider, provider]).fetchall()
        items = [dict(zip(("snapshot_id", "list_key", "provider", "list_kind", "snapshot_date", "release",
                           "selection_key", "entry_count", "url", "run_id", "source_id", "removed",
                           "retrieved_at_ms", "evidence_origin"), (*r[:4], _day(r[4]), *r[5:]))) for r in rows]
        return [s for s in items if as_of is None or s["snapshot_date"] is None or s["snapshot_date"] <= as_of]

    def generation(self, namespace: str) -> int:
        if not self.ready():
            return 0
        return int(self.conn.execute("SELECT coalesce(max(seq), 0) FROM fisheries_revisions WHERE namespace=?",
                                     [namespace]).fetchone()[0])

    def runs(self, namespace: str, run_id: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "fisheries_source_runs"):
            return []
        rows = self.conn.execute(
            "SELECT run_id, source_id, provider, status, outcomes_json, cutoff_seq, evidence_origin, finished_at_ms "
            "FROM fisheries_source_runs WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY finished_at_ms, "
            "source_id", [namespace, run_id, run_id]).fetchall()
        return [{"run_id": r[0], "source_id": r[1], "provider": r[2], "status": r[3], "outcomes": json.loads(r[4]),
                 "cutoff_seq": int(r[5]), "evidence_origin": r[6], "finished_at_ms": int(r[7])} for r in rows]

    def provider_state(self, namespace: str, provider: str) -> dict[str, Any]:
        runs = [r for r in self.runs(namespace) if r["provider"] == provider]
        done = [r for r in runs if r["status"] == "complete"]
        return {"runs": len(runs), "last_success_ms": done[-1]["finished_at_ms"] if done else None,
                "last_evidence_origin": done[-1]["evidence_origin"] if done else None,
                "last_status": runs[-1]["status"] if runs else None}


def read_store(conn: Any, namespace: str, scopes: Iterable[str]) -> FisheriesStore:
    authorize(namespace, scopes, READ_SCOPE)
    store = FisheriesStore(conn, initialize=False)
    store.require_ready()
    return store


class FisheriesProjector:
    """Runtime projector for ``noesis-fisheries-record-v1`` pages; a list page closes its snapshot."""

    def __init__(self, conn: Any) -> None:
        self.store = FisheriesStore(conn)
        self._outcomes: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._origin: dict[tuple[str, str], str] = {}

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("fisheries") or {}).get("namespace") or "global")

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, principal_id
        namespace = self._namespace(source)
        statements = [dict(r["fisheries_record"]) for r in records if r.get("fisheries_record")]
        if any(s.get("contract") != CONTRACT for s in statements):
            raise FisheriesError("invalid_record", "page record lacks a fisheries statement")
        by_id = {str(dict(d.get("metadata") or {}).get("source_pack_record_id")): d["document_id"] for d in documents}
        result = self.store.observe(namespace, statements, run_id=run_id, source_id=source["source_id"],
                                    document_ids=[by_id.get(str(r.get("id"))) for r in records
                                                  if r.get("fisheries_record")])
        receipt = dict(page_receipt or {})
        snapshot = receipt.get("snapshot")
        closed = None
        if snapshot and receipt.get("outcome") == "found":
            closed = self.store.close_snapshot(
                namespace, list_key=snapshot["list_key"], provider=snapshot["provider"],
                list_kind=snapshot["list_kind"], snapshot_date=snapshot.get("snapshot_date"),
                release=snapshot.get("release"), selection_key=snapshot["selection_key"],
                present_record_ids=[r["record_id"] for r in result["results"]], url=snapshot.get("url"),
                run_id=run_id, source_id=source["source_id"], evidence_origin=receipt.get("evidence_origin"))
        key = (run_id, source["source_id"])
        if receipt.get("selection"):
            self._outcomes.setdefault(key, []).append(
                {"selection": receipt["selection"], "outcome": receipt.get("outcome"), "statements": len(statements),
                 "snapshot_date": (snapshot or {}).get("snapshot_date"),
                 "removed": len((closed or {}).get("removed") or []),
                 "excluded_fields_dropped": receipt.get("excluded_fields_dropped", [])})
        if receipt.get("evidence_origin"):
            self._origin[key] = receipt["evidence_origin"]
        return result["counts"]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        key = (run_id, source["source_id"])
        return self.store.record_run(self._namespace(source), run_id, source["source_id"],
                                     provider=dict(source.get("fisheries") or {}).get("provider"), status=status,
                                     outcomes=self._outcomes.pop(key, []), evidence_origin=self._origin.pop(key, None))


__all__ = ["FisheriesProjector", "FisheriesStore", "IDENTIFIER_FIELDS", "TABLES", "identifier_key", "list_key_of",
           "read_store", "table_exists"]
