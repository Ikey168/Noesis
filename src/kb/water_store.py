"""Revisioned water statements with station vintages, quality-state revisions, withdrawals and run receipts (WA02-WA05).

Owns ``noesis-water-record-v1`` for the Climate and Environment pack's
optional water features. Like the environment and biodiversity record stores
it is namespace-scoped and revision-addressable, and nothing is overwritten or
deleted:

* **Records** are keyed by provider, record type and native key (station UUID
  or monitoring-location id; station, parameter and timestamp; EU water-body
  code; EU code and reporting cycle). A statement whose published content
  differs from the current revision is a new immutable revision with the next
  number, its retrieval time and the aspects that changed (``location``,
  ``datum``, ``quality``, ``value``, ``status`` ...); a replay adds nothing.
* **Provisional to approved.** An observation first published as provisional
  and later as approved is two revisions of one record; the provisional value
  stays readable for any as-of time before the approved one was retrieved.
* **Station vintages.** Every station revision is a vintage; location and
  gauge-zero/datum history are read from the revision chain.
* **Withdrawals.** A declared station, water body or assessment the source no
  longer publishes gets a dated ``removed`` revision; it is never deleted.

Runtime pages arrive through :class:`WaterProjector` (registered for
``noesis-water-record-v1`` in ``src/ingestion/source_pack_runtime.py``); each
source's run outcome, with its bounded windows, is a receipt row.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from src.kb.water_records import (
    CONTRACT,
    READ_SCOPE,
    WaterError,
    authorize,
    canonical,
    changes,
    digest,
    validate_statement,
)

_DDL = """
CREATE SEQUENCE IF NOT EXISTS water_seq;
CREATE TABLE IF NOT EXISTS water_records (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, record_type TEXT NOT NULL, provider TEXT NOT NULL,
  record_key TEXT NOT NULL, subject_key TEXT NOT NULL, subject_name TEXT, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS water_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL, seq BIGINT NOT NULL,
  revision_no INTEGER NOT NULL, content_sha TEXT NOT NULL, statement_json TEXT NOT NULL, event TEXT NOT NULL,
  changes_json TEXT NOT NULL, supersedes TEXT, observed_at_ms BIGINT NOT NULL, run_id TEXT, source_id TEXT,
  evidence_origin TEXT, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS water_source_runs (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, provider TEXT, status TEXT NOT NULL,
  outcomes_json TEXT NOT NULL, cutoff_seq BIGINT NOT NULL, evidence_origin TEXT, finished_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, run_id, source_id)
);
"""
TABLES = ("water_records", "water_revisions", "water_source_runs")


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def iso(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def day(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC).date().isoformat()


def record_id(namespace: str, provider: str, record_type: str, record_key: str) -> str:
    return "water-record:" + digest([namespace, provider, record_type, record_key])[:24]


def _content(value: Mapping[str, Any]) -> dict[str, Any]:
    """What makes a revision distinct: the published content and event, not when or how it was fetched."""
    return {"subject": value["subject"], "as_published": value["as_published"], "event": value["effective"]["event"]}


class WaterStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            for sql in _DDL.split(";"):
                if sql.strip():
                    conn.execute(sql)

    def ready(self) -> bool:
        return table_exists(self.conn, "water_revisions")

    def require_ready(self) -> None:
        if not self.ready():
            raise WaterError("not_ready", "no water record has been acquired yet")

    # ------------------------------------------------------------------ writes

    def _apply(self, namespace: str, statement: Mapping[str, Any], *, run_id: str | None, source_id: str | None,
               observed_at_ms: int) -> dict[str, Any]:
        value = validate_statement(statement)
        rid = record_id(namespace, value["provider"], value["record_type"], value["record_key"])
        self.conn.execute(
            "INSERT OR IGNORE INTO water_records VALUES (?,?,?,?,?,?,?,?)",
            [namespace, rid, value["record_type"], value["provider"], value["record_key"], value["subject"]["key"],
             value["subject"].get("name"), observed_at_ms])
        content_sha = digest(_content(value))
        latest = self.conn.execute(
            "SELECT content_sha, revision_no, revision_id, statement_json FROM water_revisions WHERE namespace=? AND "
            "record_id=? ORDER BY revision_no DESC LIMIT 1", [namespace, rid]).fetchone()
        if latest and latest[0] == content_sha:
            return {"record_id": rid, "revision_id": latest[2], "status": "unchanged"}
        number = (int(latest[1]) if latest else 0) + 1
        revision_id = "water-revision:" + digest([rid, content_sha, number])[:24]
        seq = self.conn.execute("SELECT nextval('water_seq')").fetchone()[0]
        changed = changes(json.loads(latest[3]) if latest else None, value)
        self.conn.execute(
            "INSERT INTO water_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, rid, seq, number, content_sha, canonical(value), value["effective"]["event"],
             canonical(changed), latest[2] if latest else None, observed_at_ms, run_id, source_id,
             value["source"].get("evidence_origin")])
        return {"record_id": rid, "revision_id": revision_id, "seq": seq, "revision_no": number,
                "changes": changed, "status": "created" if number == 1 else "revised"}

    def observe(self, namespace: str, statements: Sequence[Mapping[str, Any]], *, run_id: str | None = None,
                source_id: str | None = None, observed_at_ms: int | None = None) -> dict[str, Any]:
        """Keep a batch of statements in one transaction; replays add nothing."""
        observed = observed_at_ms if observed_at_ms is not None else self.now()
        counts = {"created": 0, "revised": 0, "unchanged": 0}
        results = []
        self.conn.execute("BEGIN")
        try:
            for item in statements:
                result = self._apply(namespace, item, run_id=run_id, source_id=source_id, observed_at_ms=observed)
                counts[result["status"]] += 1
                results.append(result)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"counts": counts, "results": results}

    def withdraw(self, namespace: str, provider: str, record_type: str, record_key: str, *, basis: str,
                 run_id: str | None, source_id: str | None, observed_at_ms: int | None = None) -> dict[str, Any] | None:
        """A declared record the source no longer publishes: a dated ``removed`` revision, never a deletion."""
        observed = observed_at_ms if observed_at_ms is not None else self.now()
        rid = record_id(namespace, provider, record_type, record_key)
        revisions = self.revisions(namespace, rid)
        if not revisions or revisions[-1]["event"] == "removed":
            return None
        prior = revisions[-1]["statement"]
        tombstone = {**prior, "effective": {"event": "removed", "date": day(observed),
                                            "date_basis": f"{basis}; retrieved on {day(observed)}; the record and its "
                                                          "earlier revisions are kept, never deleted"}}
        return self.observe(namespace, [tombstone], run_id=run_id, source_id=source_id,
                            observed_at_ms=observed)["results"][0]

    def record_run(self, namespace: str, run_id: str, source_id: str, *, provider: str | None, status: str,
                   outcomes: Sequence[Mapping[str, Any]], evidence_origin: str | None) -> dict[str, Any]:
        cutoff = self.generation(namespace)
        self.conn.execute(
            "INSERT OR REPLACE INTO water_source_runs VALUES (?,?,?,?,?,?,?,?,?)",
            [namespace, run_id, source_id, provider, status, canonical(list(outcomes)), int(cutoff), evidence_origin,
             self.now()])
        return {"namespace": namespace, "run_id": run_id, "source_id": source_id, "status": status,
                "cutoff_seq": int(cutoff), "outcomes": list(outcomes)}

    # ------------------------------------------------------------------ reads

    _COLUMNS = ("revision_id, record_id, seq, revision_no, content_sha, observed_at_ms, statement_json, event, "
                "changes_json, supersedes, run_id, source_id, evidence_origin")

    @staticmethod
    def _revision(row: Sequence[Any]) -> dict[str, Any]:
        return {"revision_id": row[0], "record_id": row[1], "seq": int(row[2]), "revision_no": int(row[3]),
                "content_sha": row[4], "observed_at_ms": int(row[5]), "retrieved_at": iso(int(row[5])),
                "statement": json.loads(row[6]), "event": row[7], "changes": json.loads(row[8]),
                "supersedes": row[9], "run_id": row[10], "source_id": row[11], "evidence_origin": row[12]}

    def revisions(self, namespace: str, rid: str, *, cutoff_seq: int | None = None,
                  as_of_ms: int | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            f"SELECT {self._COLUMNS} FROM water_revisions WHERE namespace=? AND record_id=? "
            "AND (? IS NULL OR seq<=?) AND (? IS NULL OR observed_at_ms<=?) ORDER BY seq",
            [namespace, rid, cutoff_seq, cutoff_seq, as_of_ms, as_of_ms]).fetchall()
        return [self._revision(r) for r in rows]

    def current(self, namespace: str, rid: str, *, as_of_ms: int | None = None,
                cutoff_seq: int | None = None) -> dict[str, Any] | None:
        """The revision on record at ``as_of_ms`` (retrieval time): the latest one retrieved by then."""
        revisions = self.revisions(namespace, rid, as_of_ms=as_of_ms, cutoff_seq=cutoff_seq)
        return revisions[-1] if revisions else None

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {self._COLUMNS} FROM water_revisions WHERE namespace=? AND revision_id=?",
                                [namespace, revision_id]).fetchone() if self.ready() else None
        if row is None:
            raise WaterError("not_found", "water revision is not visible in this namespace")
        return self._revision(row)

    def records(self, namespace: str, *, record_type: str | None = None, provider: str | None = None,
                subject_keys: Iterable[str] | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT record_id, record_type, provider, record_key, subject_key, subject_name FROM water_records "
            "WHERE namespace=? AND (? IS NULL OR record_type=?) AND (? IS NULL OR provider=?) "
            "ORDER BY record_type, provider, record_key",
            [namespace, record_type, record_type, provider, provider]).fetchall()
        wanted = None if subject_keys is None else set(subject_keys)
        return [dict(zip(("record_id", "record_type", "provider", "record_key", "subject_key", "subject_name"), r))
                for r in rows if wanted is None or r[4] in wanted]

    def find(self, namespace: str, record_type: str, provider: str, record_key: str) -> dict[str, Any] | None:
        rid = record_id(namespace, provider, record_type, record_key)
        rows = self.conn.execute(
            "SELECT record_id, record_type, provider, record_key, subject_key, subject_name FROM water_records "
            "WHERE namespace=? AND record_id=?", [namespace, rid]).fetchall() if self.ready() else []
        return dict(zip(("record_id", "record_type", "provider", "record_key", "subject_key", "subject_name"),
                        rows[0])) if rows else None

    def station(self, namespace: str, subject: str) -> dict[str, Any] | None:
        """A station record by subject key (``provider:native_id``)."""
        provider, _, native = subject.partition(":")
        return self.find(namespace, "station", provider, native)

    def generation(self, namespace: str) -> int:
        if not self.ready():
            return 0
        return int(self.conn.execute("SELECT coalesce(max(seq), 0) FROM water_revisions WHERE namespace=?",
                                     [namespace]).fetchone()[0])

    def runs(self, namespace: str, run_id: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "water_source_runs"):
            return []
        rows = self.conn.execute(
            "SELECT run_id, source_id, provider, status, outcomes_json, cutoff_seq, evidence_origin, finished_at_ms "
            "FROM water_source_runs WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY finished_at_ms, "
            "source_id", [namespace, run_id, run_id]).fetchall()
        return [{"run_id": r[0], "source_id": r[1], "provider": r[2], "status": r[3], "outcomes": json.loads(r[4]),
                 "cutoff_seq": int(r[5]), "evidence_origin": r[6], "finished_at_ms": int(r[7])} for r in rows]

    def provider_state(self, namespace: str, provider: str) -> dict[str, Any]:
        runs = [r for r in self.runs(namespace) if r["provider"] == provider]
        done = [r for r in runs if r["status"] == "complete"]
        return {"runs": len(runs), "last_success_ms": done[-1]["finished_at_ms"] if done else None,
                "last_evidence_origin": done[-1]["evidence_origin"] if done else None,
                "last_status": runs[-1]["status"] if runs else None}


def read_store(conn: Any, namespace: str, scopes: Iterable[str]) -> WaterStore:
    authorize(namespace, scopes, READ_SCOPE)
    store = WaterStore(conn, initialize=False)
    store.require_ready()
    return store


def cite(record: Mapping[str, Any], revision: Mapping[str, Any]) -> dict[str, Any]:
    """The citation every answer carries: source, record, revision and its as-of (retrieval) time."""
    source = revision["statement"]["source"]
    return {"record_id": record["record_id"], "revision_id": revision["revision_id"],
            "revision_no": revision["revision_no"], "provider": revision["statement"]["provider"],
            "url": source["url"], "api_url": source.get("api_url"), "attribution": source.get("attribution"),
            "licence": source.get("licence"), "retrieved_at": revision["retrieved_at"],
            "as_of_basis": "retrieval time of this revision", "evidence_origin": revision["evidence_origin"]}


class WaterProjector:
    """Runtime projector for ``noesis-water-record-v1`` pages; declared items not found are withdrawn."""

    def __init__(self, conn: Any) -> None:
        self.store = WaterStore(conn)
        self._outcomes: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._origin: dict[tuple[str, str], str] = {}

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("water") or {}).get("namespace") or "global")

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, principal_id
        namespace = self._namespace(source)
        statements = [dict(r["water_record"]) for r in records if r.get("water_record")]
        if any(s.get("contract") != CONTRACT for s in statements):
            raise WaterError("invalid_record", "page record lacks a water statement")
        result = self.store.observe(namespace, statements, run_id=run_id, source_id=source["source_id"])
        receipt = dict(page_receipt or {})
        withdrawn = []
        for item in receipt.get("withdrawn") or []:
            done = self.store.withdraw(namespace, item["provider"], item["record_type"], item["record_key"],
                                       basis=item["basis"], run_id=run_id, source_id=source["source_id"])
            if done:
                withdrawn.append(done["record_id"])
        key = (run_id, source["source_id"])
        if receipt.get("selection"):
            self._outcomes.setdefault(key, []).append(
                {"selection": receipt["selection"], "outcome": receipt.get("outcome"), "window": receipt.get("window"),
                 "statements": len(statements), "counts": result["counts"], "withdrawn": len(withdrawn),
                 "url": receipt.get("url"), "response_sha256": receipt.get("response_sha256"),
                 "personal_fields_dropped": receipt.get("personal_fields_dropped", [])})
        if receipt.get("evidence_origin"):
            self._origin[key] = receipt["evidence_origin"]
        return result["counts"]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        key = (run_id, source["source_id"])
        return self.store.record_run(self._namespace(source), run_id, source["source_id"],
                                     provider=dict(source.get("water") or {}).get("provider"), status=status,
                                     outcomes=self._outcomes.pop(key, []), evidence_origin=self._origin.pop(key, None))


__all__ = ["TABLES", "WaterProjector", "WaterStore", "cite", "day", "iso", "read_store", "record_id", "table_exists"]
