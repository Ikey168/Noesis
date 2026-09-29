"""Append-only, revision-addressable store for humanitarian records (HR02, #2235).

One DuckDB owner for every ``noesis-humanitarian-record-v1`` record:

* ``humanitarian_records`` - one row per record key (source family, record
  type, source id);
* ``humanitarian_revisions`` - every distinct revision, never overwritten:
  provider revision/release label, ``as_of`` (when the provider published
  it), ``retrieved_at``, content hash, the fields that changed against the
  predecessor and the run receipt that brought it;
* ``humanitarian_receipts`` - one receipt per applied page or failure;
* ``humanitarian_provider_state`` - last success/failure per source, so a
  failed refresh reads as stale and never as "no longer published".

As-of reads return the revision with the latest ``as_of`` not after the
requested instant (``basis="published"``) or the latest revision retrieved by
then (``basis="retrieved"``), and always say which revision was used.
Re-acquiring an unchanged revision adds nothing. A complete coder release
(``event_release`` with ``complete: true``) that no longer contains a candidate
event appends a ``dropped-in-release`` revision to that event; nothing is ever
deleted.

The store follows the ``src/kb/entity_history.py`` pattern (immutable rows,
append-only decisions, audit) and adds no scheduler or spatial store.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb import humanitarian_records as hr
from src.kb.humanitarian_records import (
    READ_SCOPE,
    WRITE_SCOPE,
    HumanitarianError,
    canonical,
    digest,
    iso,
    record_key,
    to_ms,
    validate_record,
)

_DDL = """
CREATE TABLE IF NOT EXISTS humanitarian_records(
 namespace TEXT NOT NULL, record_key TEXT NOT NULL, record_type TEXT NOT NULL, source_family TEXT NOT NULL,
 source_id TEXT NOT NULL, first_seen_ms BIGINT NOT NULL, PRIMARY KEY(namespace, record_key));
CREATE TABLE IF NOT EXISTS humanitarian_revisions(
 revision_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, record_key TEXT NOT NULL, seq BIGINT NOT NULL,
 source TEXT NOT NULL, provider_revision TEXT NOT NULL, predecessor_revision_id TEXT, as_of_ms BIGINT NOT NULL,
 retrieved_at_ms BIGINT NOT NULL, content_hash TEXT NOT NULL, content_json TEXT NOT NULL, changed_json TEXT NOT NULL,
 run_id TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL, UNIQUE(namespace, record_key, seq));
CREATE TABLE IF NOT EXISTS humanitarian_receipts(
 receipt_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, run_id TEXT NOT NULL, source TEXT NOT NULL,
 execution TEXT NOT NULL, detail_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS humanitarian_provider_state(
 namespace TEXT NOT NULL, source TEXT NOT NULL, last_success_ms BIGINT, last_failure_ms BIGINT,
 last_failure_code TEXT, last_execution TEXT, last_run_id TEXT, last_outcome TEXT, PRIMARY KEY(namespace, source));
"""
DEFAULT_NAMESPACE = "humanitarian"
REVISION_CONTRACT = "noesis-humanitarian-revision-v1"


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    """Humanitarian scope plus namespace access (operator bypasses)."""

    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    if required not in scopes or not needed & scopes:
        raise HumanitarianError("unauthorized", f"{required} and namespace access are required")


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def citation(revision: Mapping[str, Any]) -> dict[str, Any]:
    """The citation every answer carries for one record revision."""

    content = revision["content"]
    return {"record_key": revision["record_key"], "revision_id": revision["revision_id"],
            "source": content["source"], "source_id": content["source_id"],
            "revision": content["revision"], "as_of": content["as_of"], "retrieved_at": content.get("retrieved_at"),
            "source_url": content.get("source_url"), "attribution": content.get("attribution")}


class HumanitarianStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "humanitarian_revisions")

    def require_ready(self) -> None:
        if not self.ready():
            raise HumanitarianError("not_ready", "no humanitarian records have been acquired in this deployment")

    # ------------------------------------------------------------------ writes

    def _latest(self, namespace: str, key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT revision_id, seq, content_json, content_hash, as_of_ms FROM humanitarian_revisions "
            "WHERE namespace=? AND record_key=? ORDER BY as_of_ms DESC, seq DESC LIMIT 1", [namespace, key]).fetchone()
        if row is None:
            return None
        return {"revision_id": row[0], "seq": int(row[1]), "content": json.loads(row[2]), "content_hash": row[3],
                "as_of_ms": int(row[4])}

    def _append(self, namespace: str, record: dict[str, Any], *, run_id: str, evidence: Mapping[str, Any],
                retrieved_at_ms: int) -> tuple[str, dict[str, Any] | None]:
        key = record_key(record)
        if not record.get("retrieved_at"):
            record["retrieved_at"] = iso(retrieved_at_ms)
        body = {k: v for k, v in record.items() if k != "retrieved_at"}
        content_hash = digest(body)
        latest = self._latest(namespace, key)
        if latest is not None and latest["content_hash"] == content_hash:
            return "unchanged", None
        seq_row = self.conn.execute("SELECT max(seq) FROM humanitarian_revisions WHERE namespace=? AND record_key=?",
                                    [namespace, key]).fetchone()
        seq = int(seq_row[0] or 0) + 1
        if latest is None:
            self.conn.execute("INSERT INTO humanitarian_records VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                              [namespace, key, record["record_type"], key.split(":", 1)[0], record["source_id"],
                               self.now()])
        changed = hr.changed_fields(latest["content"] if latest else None, record)
        revision_id = "hum-rev:" + digest([namespace, key, seq, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO humanitarian_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [revision_id, namespace, key, seq, record["source"], record["revision"],
             latest["revision_id"] if latest else None, to_ms(record["as_of"]), retrieved_at_ms, content_hash,
             canonical(record), canonical(changed), run_id, canonical(dict(evidence)), self.now()])
        return ("created" if latest is None else "revised"), {"record_key": key, "revision_id": revision_id,
                                                              "changed_fields": [c["field"] for c in changed]}

    def apply(self, namespace: str, records: Iterable[Mapping[str, Any]], *, run_id: str, principal_id: str,
              scopes: Iterable[str], evidence: Mapping[str, Any] | None = None, retrieved_at_ms: int | None = None,
              execution: str = "fixture", source: str | None = None) -> dict[str, Any]:
        """Validate and append records; invalid ones are rejected with their reason, never dropped silently."""

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        evidence = dict(evidence or {})
        outcome: dict[str, list] = {"created": [], "revised": [], "unchanged": [], "rejected": [], "dropped": []}
        valid, releases = [], []
        for index, raw in enumerate(records):
            try:
                record = validate_record(raw)
            except HumanitarianError as exc:
                outcome["rejected"].append({"index": index, "source_id": dict(raw).get("source_id"), **exc.as_dict()})
                continue
            (releases if record["record_type"] == "event_release" else valid).append(record)
        sources = {r["source"] for r in valid + releases} | ({source} if source else set())
        self.conn.execute("BEGIN")
        try:
            for record in valid + releases:
                status, detail = self._append(namespace, record, run_id=run_id, evidence=evidence,
                                              retrieved_at_ms=retrieved)
                outcome[status].append(detail or {"record_key": record_key(record)})
            for release in releases:
                outcome["dropped"] += self._drop_absent(namespace, release, run_id=run_id, evidence=evidence,
                                                        retrieved_at_ms=retrieved)
            for name in sorted(sources):
                self._state(namespace, name, success=retrieved, run_id=run_id, execution=execution)
            receipt = {"run_id": run_id, "principal_id": principal_id, "evidence": evidence,
                       **{k: len(v) for k, v in outcome.items()},
                       "rejected_detail": outcome["rejected"]}
            receipt_id = "hum-receipt:" + digest([namespace, run_id, sorted(sources), receipt])[:24]
            self.conn.execute("INSERT INTO humanitarian_receipts VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                              [receipt_id, namespace, run_id, ",".join(sorted(sources)) or "none", execution,
                               canonical(receipt), self.now()])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"namespace": namespace, "run_id": run_id, "receipt_id": receipt_id,
                **{k: v for k, v in outcome.items()}}

    def _drop_absent(self, namespace, release, *, run_id, evidence, retrieved_at_ms):
        """A complete final release that omits a candidate event of its scope records a dropped revision."""

        if not release.get("complete") or release["coding_source"] != "ucdp-ged":
            return []
        window = dict(release.get("window") or {})
        start, end = to_ms(window.get("start")), to_ms(window.get("end"))
        country = str(release.get("country") or "")
        included = set(release["event_ids"])
        dropped = []
        for (key,) in self.conn.execute(
                "SELECT record_key FROM humanitarian_records WHERE namespace=? AND record_type='conflict_event' "
                "AND source_family='ucdp' ORDER BY record_key", [namespace]).fetchall():
            latest = self._latest(namespace, key)
            content = latest["content"]
            if content["coding_status"] != "candidate" or content["source_id"] in included:
                continue
            when = to_ms(content.get("date_start"))
            gw = str(dict(content.get("location") or {}).get("country_code") or "")
            if when is None or (start is not None and when < start) or (end is not None and when > end):
                continue
            if country and gw != country:
                continue
            revised = {**content, "coding_status": "dropped-in-release", "dataset_version": release["dataset_version"],
                       "revision": release["dataset_version"], "as_of": release["as_of"],
                       "retrieved_at": iso(retrieved_at_ms),
                       "dropped_by_release": {"source_id": release["source_id"], "dataset_version": release["dataset_version"]}}
            _, detail = self._append(namespace, revised, run_id=run_id, evidence=evidence,
                                          retrieved_at_ms=retrieved_at_ms)
            if detail:
                dropped.append(detail)
        return dropped

    def _state(self, namespace, source, *, success=None, failure=None, code=None, run_id=None, execution=None):
        """Latest outcome wins: a failure after a success reads as stale until the next success."""
        row = self.conn.execute("SELECT last_success_ms, last_failure_ms, last_failure_code, last_execution FROM "
                                "humanitarian_provider_state WHERE namespace=? AND source=?", [namespace, source]).fetchone()
        current = list(row) if row else [None, None, None, None]
        if success is not None:
            current[0], current[3] = success, execution
        if failure is not None:
            current[1], current[2] = failure, code
        outcome = "success" if success is not None else "failure"
        self.conn.execute(
            "INSERT INTO humanitarian_provider_state VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (namespace, source) DO UPDATE SET "
            "last_success_ms=excluded.last_success_ms, last_failure_ms=excluded.last_failure_ms, "
            "last_failure_code=excluded.last_failure_code, last_execution=excluded.last_execution, "
            "last_run_id=excluded.last_run_id, last_outcome=excluded.last_outcome",
            [namespace, source, *current, run_id, outcome])

    def record_failure(self, namespace: str, source: str, *, code: str, run_id: str, scopes: Iterable[str]) -> dict:
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        now = self.now()
        self._state(namespace, source, failure=now, code=code, run_id=run_id)
        self.conn.execute("INSERT INTO humanitarian_receipts VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          ["hum-receipt:" + digest([namespace, run_id, source, code, now])[:24], namespace, run_id,
                           source, "failed", canonical({"failure_code": code}), now])
        return {"source": source, "failure_code": code, "recorded_at_ms": now,
                "effect": "stored revisions unchanged; the source reads as stale, nothing is marked withdrawn"}

    # ------------------------------------------------------------------ reads

    def provider_state(self, namespace: str, source: str) -> dict[str, Any]:
        row = None
        if table_exists(self.conn, "humanitarian_provider_state"):
            row = self.conn.execute("SELECT last_success_ms, last_failure_ms, last_failure_code, last_execution, last_run_id, "
                                    "last_outcome FROM humanitarian_provider_state WHERE namespace=? AND source=?",
                                    [namespace, source]).fetchone()
        if row is None:
            return {"source": source, "last_success_ms": None, "stale": True, "reason": "never acquired"}
        stale = row[0] is None or row[5] == "failure"
        return {"source": source, "last_success_ms": row[0], "last_failure_ms": row[1], "last_failure_code": row[2],
                "last_execution": row[3], "last_run_id": row[4], "stale": stale}

    def _render(self, row) -> dict[str, Any]:
        return {"contract": REVISION_CONTRACT, "revision_id": row[0], "record_key": row[1], "seq": int(row[2]),
                "predecessor_revision_id": row[3], "as_of_ms": int(row[4]), "retrieved_at_ms": int(row[5]),
                "content": json.loads(row[6]), "changed_fields": json.loads(row[7]), "run_id": row[8],
                "evidence": json.loads(row[9])}

    _COLUMNS = ("revision_id, record_key, seq, predecessor_revision_id, as_of_ms, retrieved_at_ms, content_json, "
                "changed_json, run_id, evidence_json")

    def revision(self, namespace: str, key: str, *, scopes: Iterable[str], as_of_ms: int | None = None,
                 basis: str = "published", revision_id: str | None = None) -> dict[str, Any] | None:
        """The revision in force at ``as_of_ms`` (or a pinned revision id); ``None`` when none was available."""

        authorize(namespace, scopes, READ_SCOPE)
        if not self.ready():
            return None
        if basis not in {"published", "retrieved"}:
            raise HumanitarianError("invalid_request", "basis is 'published' or 'retrieved'")
        column = "as_of_ms" if basis == "published" else "retrieved_at_ms"
        row = self.conn.execute(
            f"SELECT {self._COLUMNS} FROM humanitarian_revisions WHERE namespace=? AND record_key=? "
            f"AND (? IS NULL OR revision_id=?) AND (? IS NULL OR {column}<=?) "
            f"ORDER BY {column} DESC, seq DESC LIMIT 1",
            [namespace, key, revision_id, revision_id, as_of_ms, as_of_ms]).fetchone()
        return self._render(row) if row else None

    def history(self, namespace: str, key: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Every revision of a record, oldest first, with the fields each one changed."""

        authorize(namespace, scopes, READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(f"SELECT {self._COLUMNS} FROM humanitarian_revisions WHERE namespace=? AND record_key=? "
                                 "ORDER BY seq", [namespace, key]).fetchall()
        return [self._render(r) for r in rows]

    def keys(self, namespace: str, *, record_type: str | None = None, source_family: str | None = None) -> list[str]:
        if not self.ready():
            return []
        return [r[0] for r in self.conn.execute(
            "SELECT record_key FROM humanitarian_records WHERE namespace=? AND (? IS NULL OR record_type=?) "
            "AND (? IS NULL OR source_family=?) ORDER BY record_key",
            [namespace, record_type, record_type, source_family, source_family]).fetchall()]

    def current(self, namespace: str, *, scopes: Iterable[str], record_type: str | None = None,
                as_of_ms: int | None = None, basis: str = "published") -> list[dict[str, Any]]:
        """The revision of every record in force at ``as_of_ms`` (records with none available are left out)."""

        authorize(namespace, scopes, READ_SCOPE)
        result = []
        for key in self.keys(namespace, record_type=record_type):
            revision = self.revision(namespace, key, scopes=scopes, as_of_ms=as_of_ms, basis=basis)
            if revision is not None:
                result.append(revision)
        return result

    def receipts(self, namespace: str, *, scopes: Iterable[str], run_id: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "humanitarian_receipts"):
            return []
        rows = self.conn.execute("SELECT receipt_id, run_id, source, execution, detail_json, created_at_ms FROM "
                                 "humanitarian_receipts WHERE namespace=? AND (? IS NULL OR run_id=?) "
                                 "ORDER BY created_at_ms, receipt_id", [namespace, run_id, run_id]).fetchall()
        return [{"receipt_id": r[0], "run_id": r[1], "source": r[2], "execution": r[3], "detail": json.loads(r[4]),
                 "created_at_ms": int(r[5])} for r in rows]


class HumanitarianProjector:
    """Source-pack runtime projector for ``noesis-humanitarian-record-v1`` pages."""

    @staticmethod
    def scopes_for(namespace: str) -> set[str]:
        return {WRITE_SCOPE, READ_SCOPE, f"namespace:{namespace}:write", f"namespace:{namespace}:read"}

    def __init__(self, conn: Any) -> None:
        self.store = HumanitarianStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("humanitarian") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        namespace = self._namespace(source)
        payload = [dict(item["humanitarian_record"]) for item in records if item.get("humanitarian_record")]
        evidence = {"run_id": run_id, "source_id": source["source_id"], "pack_id": manifest["pack_id"],
                    "pack_version": manifest["version"], "page_sha256": dict(page_receipt or {}).get("page_sha256"),
                    "documents": sorted(str(d["document_id"]) for d in documents)}
        retrieved = max((int(d["ingested_at"]) for d in documents if d.get("ingested_at") is not None), default=None)
        provider = str(dict(source.get("humanitarian") or {}).get("provider") or "")
        return self.store.apply(namespace, payload, run_id=run_id, principal_id=principal_id,
                                scopes=self.scopes_for(namespace), evidence=evidence, retrieved_at_ms=retrieved,
                                execution=str(dict(page_receipt or {}).get("execution") or "source-pack"),
                                source=provider or None)

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        namespace = self._namespace(source)
        provider = str(dict(source.get("humanitarian") or {}).get("provider"))
        if status != "complete":
            self.store.record_failure(namespace, provider, code="source_run_" + status, run_id=run_id,
                                      scopes=self.scopes_for(namespace))
        return {"status": status, "provider": provider, "namespace": namespace,
                "provider_state": self.store.provider_state(namespace, provider)}
