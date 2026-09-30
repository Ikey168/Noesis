"""Revisioned life-science statements with release lookup, removal tombstones and run receipts (LS02-LS06).

Owns ``noesis-lifesci-record-v1`` for the Science pack's ``science.life-sciences``
provider. Like the other domain record stores (:mod:`src.kb.biodiversity_store`,
following :mod:`src.kb.entity_history` patterns) it is namespace-scoped and
revision-addressable, and nothing is overwritten or deleted:

* **Records** are keyed by provider, record type and native key (UniProt
  accession, Gene ID, Tax ID, PDB ID, ChEMBL ID). A statement whose published
  content differs is a new immutable revision with the next number and its
  retrieval time; a replay adds nothing.
* **Releases.** UniProt and ChEMBL statements carry their release, PDB
  statements their major.minor revision; each release is a new revision of the
  same record and release-scoped content is de-duplicated against every earlier
  revision, so re-reading an older release never re-adds it.
  :meth:`LifeSciStore.at_release` answers the revision in force at a release.
* **Obsolescence is a revision.** A UniProt entry that became inactive (merged,
  demerged, deleted), a discontinued or replaced gene, a merged taxon and an
  obsolete PDB entry are ``obsoleted`` revisions keeping the successors the
  source names; earlier revisions stay readable.
* **Removal tombstones.** A ChEMBL activity page is a snapshot of one declared
  target selection in one release. An activity present in the previous
  *complete* snapshot and absent from the next complete one gets a dated
  ``removed`` revision; it is never deleted.

Runtime pages arrive through :class:`LifeSciProjector` (registered for
``noesis-lifesci-record-v1`` in ``src/ingestion/source_pack_runtime.py``); each
source's run outcome is a receipt row.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from src.kb.lifesci_records import (
    CONTRACT,
    READ_SCOPE,
    LifeSciError,
    authorize,
    canonical,
    digest,
    release_of,
    release_order,
    validate_statement,
)

_DDL = """
CREATE SEQUENCE IF NOT EXISTS lifesci_seq;
CREATE TABLE IF NOT EXISTS lifesci_records (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, record_type TEXT NOT NULL, provider TEXT NOT NULL,
  record_key TEXT NOT NULL, subject_key TEXT NOT NULL, subject_name TEXT, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS lifesci_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL, seq BIGINT NOT NULL,
  revision_no INTEGER NOT NULL, content_sha TEXT NOT NULL, statement_json TEXT NOT NULL, event TEXT NOT NULL,
  release TEXT, supersedes TEXT, observed_at_ms BIGINT NOT NULL, run_id TEXT, source_id TEXT,
  evidence_origin TEXT, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS lifesci_snapshots (
  namespace TEXT NOT NULL, snapshot_id TEXT NOT NULL, selection_key TEXT NOT NULL, provider TEXT NOT NULL,
  release TEXT, complete BOOLEAN NOT NULL, entry_count INTEGER NOT NULL, removed INTEGER NOT NULL, url TEXT,
  run_id TEXT, source_id TEXT, observed_at_ms BIGINT NOT NULL, members_json TEXT NOT NULL,
  PRIMARY KEY(namespace, snapshot_id)
);
CREATE TABLE IF NOT EXISTS lifesci_source_runs (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, provider TEXT, status TEXT NOT NULL,
  outcomes_json TEXT NOT NULL, cutoff_seq BIGINT NOT NULL, evidence_origin TEXT, finished_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, run_id, source_id)
);
"""
TABLES = ("lifesci_records", "lifesci_revisions", "lifesci_snapshots", "lifesci_source_runs")


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def day(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, tz=UTC).date().isoformat()


def record_id_for(namespace: str, provider: str, record_type: str, record_key: str) -> str:
    return "lifesci-record:" + digest([namespace, provider, record_type, record_key])[:24]


def _content(value: Mapping[str, Any]) -> dict[str, Any]:
    """What makes a revision distinct: published content, event and release - not when it was fetched."""
    return {"subject": value["subject"], "as_published": value["as_published"],
            "event": value["effective"]["event"], "release": release_of(value)}


class LifeSciStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
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
            raise LifeSciError("not_ready", "no life-sciences record has been acquired yet")

    # ------------------------------------------------------------------ writes

    def _apply(self, namespace: str, statement: Mapping[str, Any], *, run_id: str | None, source_id: str | None,
               observed_at_ms: int) -> dict[str, Any]:
        value = validate_statement(statement)
        record_id = record_id_for(namespace, value["provider"], value["record_type"], value["record_key"])
        self.conn.execute(
            "INSERT OR IGNORE INTO lifesci_records VALUES (?,?,?,?,?,?,?,?)",
            [namespace, record_id, value["record_type"], value["provider"], value["record_key"],
             value["subject"]["key"], value["subject"].get("name"), observed_at_ms])
        content_sha = digest(_content(value))
        release = release_of(value)
        rows = self.conn.execute(
            "SELECT content_sha, revision_no, revision_id FROM lifesci_revisions WHERE namespace=? AND record_id=? "
            "ORDER BY revision_no DESC", [namespace, record_id]).fetchall()
        latest = rows[0] if rows else None
        # Release-scoped content is never re-added when an older release is re-read.
        known = {r[0]: r[2] for r in rows} if release else ({latest[0]: latest[2]} if latest else {})
        if content_sha in known:
            return {"record_id": record_id, "revision_id": known[content_sha], "status": "unchanged"}
        number = (int(latest[1]) if latest else 0) + 1
        revision_id = "lifesci-revision:" + digest([record_id, content_sha, number])[:24]
        seq = self.conn.execute("SELECT nextval('lifesci_seq')").fetchone()[0]
        self.conn.execute(
            "INSERT INTO lifesci_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, record_id, seq, number, content_sha, canonical(value),
             value["effective"]["event"], release, latest[2] if latest else None, observed_at_ms, run_id,
             source_id, value["source"].get("evidence_origin")])
        return {"record_id": record_id, "revision_id": revision_id, "seq": seq, "revision_no": number,
                "status": "created" if number == 1 else "revised"}

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

    def close_snapshot(self, namespace: str, *, selection_key: str, provider: str, release: str | None,
                       complete: bool, present_record_ids: Iterable[str], url: str | None, run_id: str | None,
                       source_id: str | None, observed_at_ms: int | None = None) -> dict[str, Any]:
        """Record one activity page; activities of the previous complete page now absent get a tombstone."""
        observed = observed_at_ms if observed_at_ms is not None else self.now()
        present = sorted(set(present_record_ids))
        previous = self.conn.execute(
            "SELECT snapshot_id, complete, members_json, release FROM lifesci_snapshots WHERE namespace=? AND "
            "selection_key=? ORDER BY observed_at_ms DESC LIMIT 1", [namespace, selection_key]).fetchone()
        if previous is not None and bool(previous[1]) == bool(complete) and json.loads(previous[2]) == present \
                and previous[3] == release:
            return {"snapshot_id": previous[0], "status": "unchanged", "removed": []}
        removed: list[str] = []
        note = None
        if previous is not None and not (previous[1] and complete):
            note = "a page of this selection was truncated; absence is not compared"
        elif previous is not None:
            removed = self._tombstone(namespace, set(json.loads(previous[2])) - set(present), release, observed,
                                      run_id, source_id)
        snapshot_id = "lifesci-snapshot:" + digest([namespace, selection_key, present, complete, release,
                                                    observed])[:24]
        self.conn.execute(
            "INSERT OR IGNORE INTO lifesci_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, snapshot_id, selection_key, provider, release, bool(complete), len(present), len(removed),
             url, run_id, source_id, observed, canonical(present)])
        return {"snapshot_id": snapshot_id, "status": "recorded", "removed": removed, "note": note}

    def _tombstone(self, namespace, absent, release, observed, run_id, source_id) -> list[str]:
        removed = []
        for record_id in sorted(absent):
            latest = self.revisions(namespace, record_id)[-1]
            if latest["event"] == "removed":
                continue
            prior = latest["statement"]
            published = dict(prior["as_published"])
            if release and "release" in published:
                published["release"] = release  # the state in the new release is "removed"
            tombstone = {**prior, "as_published": published,
                         "source": {**prior["source"], "absent_from_release": release},
                         "effective": {"event": "removed", "date": day(observed),
                                       "date_basis": f"absent from the complete page of the same declared selection "
                                                     f"in release {release}, retrieved on {day(observed)}; the record "
                                                     "and its earlier revisions are kept, never deleted"}}
            self._apply(namespace, tombstone, run_id=run_id, source_id=source_id, observed_at_ms=observed)
            removed.append(record_id)
        return removed

    def record_run(self, namespace: str, run_id: str, source_id: str, *, provider: str | None, status: str,
                   outcomes: Sequence[Mapping[str, Any]], evidence_origin: str | None) -> dict[str, Any]:
        cutoff = self.generation(namespace)
        self.conn.execute(
            "INSERT OR REPLACE INTO lifesci_source_runs VALUES (?,?,?,?,?,?,?,?,?)",
            [namespace, run_id, source_id, provider, status, canonical(list(outcomes)), int(cutoff), evidence_origin,
             self.now()])
        return {"namespace": namespace, "run_id": run_id, "source_id": source_id, "status": status,
                "cutoff_seq": int(cutoff), "outcomes": list(outcomes)}

    # ------------------------------------------------------------------ reads

    _COLUMNS = ("revision_id, record_id, seq, revision_no, content_sha, observed_at_ms, statement_json, event, "
                "release, supersedes, run_id, source_id, evidence_origin")

    @staticmethod
    def _revision(row: Sequence[Any]) -> dict[str, Any]:
        return {"revision_id": row[0], "record_id": row[1], "seq": int(row[2]), "revision_no": int(row[3]),
                "content_sha": row[4], "observed_at_ms": int(row[5]), "retrieved_on": day(int(row[5])),
                "statement": json.loads(row[6]), "event": row[7], "release": row[8], "supersedes": row[9],
                "run_id": row[10], "source_id": row[11], "evidence_origin": row[12]}

    def revisions(self, namespace: str, record_id: str, *, cutoff_seq: int | None = None,
                  as_of_ms: int | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            f"SELECT {self._COLUMNS} FROM lifesci_revisions WHERE namespace=? AND record_id=? "
            "AND (? IS NULL OR seq<=?) AND (? IS NULL OR observed_at_ms<=?) ORDER BY seq",
            [namespace, record_id, cutoff_seq, cutoff_seq, as_of_ms, as_of_ms]).fetchall()
        return [self._revision(r) for r in rows]

    def current(self, namespace: str, record_id: str, *, as_of_ms: int | None = None,
                cutoff_seq: int | None = None) -> dict[str, Any] | None:
        """The revision on record at ``as_of_ms`` (retrieval time): the latest one retrieved by then."""
        revisions = self.revisions(namespace, record_id, as_of_ms=as_of_ms, cutoff_seq=cutoff_seq)
        return revisions[-1] if revisions else None

    def at_release(self, namespace: str, record_id: str, release: str, *,
                   as_of_ms: int | None = None) -> dict[str, Any] | None:
        """The revision in force at a published release: the latest revision whose release is not later."""
        wanted = release_order(release)
        candidates = [r for r in self.revisions(namespace, record_id, as_of_ms=as_of_ms)
                      if r["release"] is not None and release_order(r["release"]) <= wanted]
        if not candidates:
            return None
        return max(candidates, key=lambda r: (release_order(r["release"]), r["seq"]))

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {self._COLUMNS} FROM lifesci_revisions WHERE namespace=? AND revision_id=?",
                                [namespace, revision_id]).fetchone() if self.ready() else None
        if row is None:
            raise LifeSciError("not_found", "life-sciences revision is not visible in this namespace")
        return self._revision(row)

    def records(self, namespace: str, *, record_type: str | None = None, provider: str | None = None,
                subject_keys: Iterable[str] | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT record_id, record_type, provider, record_key, subject_key, subject_name FROM lifesci_records "
            "WHERE namespace=? AND (? IS NULL OR record_type=?) AND (? IS NULL OR provider=?) "
            "ORDER BY record_type, provider, record_key",
            [namespace, record_type, record_type, provider, provider]).fetchall()
        wanted = None if subject_keys is None else set(subject_keys)
        return [dict(zip(("record_id", "record_type", "provider", "record_key", "subject_key", "subject_name"), r))
                for r in rows if wanted is None or r[4] in wanted]

    def find(self, namespace: str, record_type: str, record_key: str) -> dict[str, Any] | None:
        rows = [r for r in self.records(namespace, record_type=record_type) if r["record_key"] == record_key]
        return rows[0] if rows else None

    def snapshots(self, namespace: str, *, selection_key: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "lifesci_snapshots"):
            return []
        rows = self.conn.execute(
            "SELECT snapshot_id, selection_key, provider, release, complete, entry_count, removed, url, run_id, "
            "source_id, observed_at_ms FROM lifesci_snapshots WHERE namespace=? AND (? IS NULL OR selection_key=?) "
            "ORDER BY observed_at_ms", [namespace, selection_key, selection_key]).fetchall()
        return [dict(zip(("snapshot_id", "selection_key", "provider", "release", "complete", "entry_count",
                          "removed", "url", "run_id", "source_id", "retrieved_at_ms"), r)) for r in rows]

    def generation(self, namespace: str) -> int:
        if not self.ready():
            return 0
        return int(self.conn.execute("SELECT coalesce(max(seq), 0) FROM lifesci_revisions WHERE namespace=?",
                                     [namespace]).fetchone()[0])

    def runs(self, namespace: str, run_id: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "lifesci_source_runs"):
            return []
        rows = self.conn.execute(
            "SELECT run_id, source_id, provider, status, outcomes_json, cutoff_seq, evidence_origin, finished_at_ms "
            "FROM lifesci_source_runs WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY finished_at_ms, "
            "source_id", [namespace, run_id, run_id]).fetchall()
        return [{"run_id": r[0], "source_id": r[1], "provider": r[2], "status": r[3], "outcomes": json.loads(r[4]),
                 "cutoff_seq": int(r[5]), "evidence_origin": r[6], "finished_at_ms": int(r[7])} for r in rows]

    def provider_state(self, namespace: str, provider: str) -> dict[str, Any]:
        runs = [r for r in self.runs(namespace) if r["provider"] == provider]
        done = [r for r in runs if r["status"] == "complete"]
        held = len(self.records(namespace, provider=provider))
        return {"runs": len(runs), "records": held, "last_success_ms": done[-1]["finished_at_ms"] if done else None,
                "last_evidence_origin": done[-1]["evidence_origin"] if done else None,
                "last_status": runs[-1]["status"] if runs else None}


def read_store(conn: Any, namespace: str, scopes: Iterable[str]) -> LifeSciStore:
    authorize(namespace, scopes, READ_SCOPE)
    store = LifeSciStore(conn, initialize=False)
    store.require_ready()
    return store


class LifeSciProjector:
    """Runtime projector for ``noesis-lifesci-record-v1`` pages; a ChEMBL activity page closes its snapshot."""

    def __init__(self, conn: Any) -> None:
        self.store = LifeSciStore(conn)
        self._outcomes: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self._origin: dict[tuple[str, str], str] = {}

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("life_sciences") or {}).get("namespace") or "global")

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, principal_id
        namespace = self._namespace(source)
        statements = [dict(r["lifesci_record"]) for r in records if r.get("lifesci_record")]
        if any(s.get("contract") != CONTRACT for s in statements):
            raise LifeSciError("invalid_record", "page record lacks a life-sciences statement")
        result = self.store.observe(namespace, statements, run_id=run_id, source_id=source["source_id"])
        receipt = dict(page_receipt or {})
        snapshot = receipt.get("snapshot")
        closed = None
        if snapshot and receipt.get("outcome") == "found":
            activity_ids = [r["record_id"] for r, s in zip(result["results"], statements, strict=True)
                            if s["record_type"] == "activity"]
            closed = self.store.close_snapshot(
                namespace, selection_key=snapshot["selection_key"], provider=snapshot["provider"],
                release=snapshot.get("release"), complete=bool(snapshot.get("complete")),
                present_record_ids=activity_ids, url=snapshot.get("url"), run_id=run_id,
                source_id=source["source_id"])
        key = (run_id, source["source_id"])
        if receipt.get("selection"):
            self._outcomes.setdefault(key, []).append(
                {"selection": receipt["selection"], "outcome": receipt.get("outcome"), "statements": len(statements),
                 "release": receipt.get("release"), "removed": len((closed or {}).get("removed") or []),
                 "personal_fields_dropped": receipt.get("personal_fields_dropped", []),
                 "excluded_fields_dropped": receipt.get("excluded_fields_dropped", [])})
        if receipt.get("evidence_origin"):
            self._origin[key] = receipt["evidence_origin"]
        return result["counts"]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        key = (run_id, source["source_id"])
        return self.store.record_run(self._namespace(source), run_id, source["source_id"],
                                     provider=dict(source.get("life_sciences") or {}).get("provider"), status=status,
                                     outcomes=self._outcomes.pop(key, []), evidence_origin=self._origin.pop(key, None))


__all__ = ["TABLES", "LifeSciProjector", "LifeSciStore", "day", "read_store", "record_id_for", "table_exists"]
