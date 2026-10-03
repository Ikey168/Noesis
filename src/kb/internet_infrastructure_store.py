"""Append-only store for network registry records and routing observations (#2743, II02).

Follows :mod:`src.kb.entity_history` (immutable, revision-addressable rows with an audit trail) and the wave's series
stores. Tables (all ``ii_*``, namespace-scoped):

* ``ii_units`` - one row per acquired unit (every request one declared resource needed): provider, resource, file
  digests, retrieval clock, evidence origin, run id;
* ``ii_declared`` - the resources the source declarations name; queries answer declared resources only;
* ``ii_objects`` - one row per (source, object kind, native id). The same ASN stated by RIPEstat, PeeringDB and RDAP is
  three objects; statements of different sources are never merged into one record;
* ``ii_revisions`` - registry-record revisions, never overwritten: state (``published`` or ``removed_by_source``),
  revision basis and the source's own revision (``updated``, ``last changed``, CT log list version, crt.sh entry time
  or the content digest), the as-of time the revision is valid from, retrieval clock, content and recorded changes;
* ``ii_observations`` - RIPEstat answers dated by RIPEstat's stated time and data-call version, never rewritten;
* ``ii_listings`` - complete listings (a network's IX presence, the CT log list), so a member a complete later
  listing no longer states gets a ``removed_by_source`` revision instead of a deletion;
* ``ii_receipts`` - one receipt per applied unit or failure. A failure records a receipt only: it never writes a
  revision, so a failed run never produces a removal.

As-of reads select the revision whose as-of time is the latest on or before the requested instant (a removal is
dated by the source when it states a time, else by the retrieval that saw it) and the observation whose stated time
is the latest on or before it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.ingestion.internet_infrastructure_sources import (
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
)
from src.kb.internet_infrastructure_records import (
    CONTRACT,
    READ_SCOPE,
    SHAPES,
    WRITE_SCOPE,
    InfrastructureRecordError,
    authorize,
    canonical,
    check_item,
    digest,
    iso,
    load,
    table_exists,
    to_ms,
)

_DDL = """
CREATE TABLE IF NOT EXISTS ii_units (
  namespace TEXT NOT NULL, unit_id TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT, unit_key TEXT NOT NULL,
  resource_kind TEXT NOT NULL, resource TEXT NOT NULL, header_json TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL,
  evidence_origin TEXT NOT NULL, run_id TEXT NOT NULL, recorded_by TEXT, PRIMARY KEY(namespace, unit_id)
);
CREATE TABLE IF NOT EXISTS ii_declared (
  namespace TEXT NOT NULL, resource_kind TEXT NOT NULL, resource TEXT NOT NULL, provider TEXT NOT NULL,
  source_id TEXT, first_unit_id TEXT NOT NULL, declared_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, resource_kind, resource, provider)
);
CREATE TABLE IF NOT EXISTS ii_objects (
  namespace TEXT NOT NULL, object_id TEXT NOT NULL, provider TEXT NOT NULL, object_kind TEXT NOT NULL,
  native_id TEXT NOT NULL, shape TEXT NOT NULL, resource_kind TEXT NOT NULL, resource TEXT NOT NULL,
  label TEXT, first_unit_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, object_id)
);
CREATE TABLE IF NOT EXISTS ii_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, object_id TEXT NOT NULL, revision_no INTEGER NOT NULL,
  previous_revision_id TEXT, state TEXT NOT NULL, basis TEXT NOT NULL, source_revision TEXT,
  valid_from_ms BIGINT NOT NULL, valid_from_basis TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL,
  content_digest TEXT NOT NULL, content_json TEXT NOT NULL, identifiers_json TEXT NOT NULL, url TEXT,
  unit_id TEXT NOT NULL, changes_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS ii_observations (
  namespace TEXT NOT NULL, observation_id TEXT NOT NULL, object_id TEXT NOT NULL, data_call TEXT NOT NULL,
  data_call_version TEXT NOT NULL, data_call_status TEXT NOT NULL, stated_time_ms BIGINT NOT NULL,
  time_basis TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL, content_digest TEXT NOT NULL,
  content_json TEXT NOT NULL, query_json TEXT NOT NULL, identifiers_json TEXT NOT NULL,
  previous_observation_id TEXT, changed BOOLEAN NOT NULL, url TEXT, unit_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, observation_id)
);
CREATE TABLE IF NOT EXISTS ii_listings (
  namespace TEXT NOT NULL, listing_id TEXT NOT NULL, listing_key TEXT NOT NULL, provider TEXT NOT NULL,
  object_kind TEXT NOT NULL, members_json TEXT NOT NULL, unit_id TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, listing_id)
);
CREATE TABLE IF NOT EXISTS ii_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT, provider TEXT NOT NULL,
  outcome TEXT NOT NULL, detail_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, receipt_id)
);
"""
_REVISION_COLUMNS = ("revision_id, object_id, revision_no, previous_revision_id, state, basis, source_revision, "
                     "valid_from_ms, valid_from_basis, retrieved_at_ms, content_digest, content_json, "
                     "identifiers_json, url, unit_id, changes_json")
_OBSERVATION_COLUMNS = ("observation_id, object_id, data_call, data_call_version, data_call_status, stated_time_ms, "
                        "time_basis, retrieved_at_ms, content_digest, content_json, query_json, identifiers_json, "
                        "previous_observation_id, changed, url, unit_id")
_OBJECT_COLUMNS = "object_id, provider, object_kind, native_id, shape, resource_kind, resource, label, first_unit_id"


def object_id_for(namespace: str, provider: str, object_kind: str, native_id: str) -> str:
    return "ii-object:" + digest([namespace, provider, object_kind, str(native_id)])[:24]


def _changes(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> dict[str, Any]:
    if before is None:
        return {"new_object": True, "fields": sorted(after)}
    changed = {k: {"before": before.get(k), "after": after.get(k)} for k in sorted(set(before) | set(after))
               if before.get(k) != after.get(k)}
    return {"new_object": False, "fields": changed}


def citation(obj: Mapping[str, Any], record: Mapping[str, Any], unit: Mapping[str, Any]) -> dict[str, Any]:
    """What every answer cites for one statement: source, record revision (or observation) and as-of time."""
    observation = "observation_id" in record
    return {
        "provider": obj["provider"], "source_id": unit.get("source_id"), "object_id": obj["object_id"],
        "object_kind": obj["object_kind"], "native_id": obj["native_id"], "shape": obj["shape"],
        "record_id": record["observation_id"] if observation else record["revision_id"],
        "revision_no": None if observation else record["revision_no"],
        "source_revision": f"{record['data_call']} v{record['data_call_version']}" if observation
        else record["source_revision"],
        "revision_basis": record["time_basis"] if observation else record["basis"],
        "as_of": record["stated_time"] if observation else record["valid_from"],
        "retrieved_at": record["retrieved_at"], "url": record.get("url"), "unit_id": unit["unit_id"],
        "evidence_origin": unit["evidence_origin"], "contract": CONTRACT,
        "redistribution": PROVIDER_CONTRACTS[obj["provider"]].get("redistribution"),
        "live_verification": LIVE_VERIFICATION[obj["provider"]]["status"],
    }


class InternetInfrastructureStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "ii_revisions")

    # ------------------------------------------------------------------ writes

    def apply_unit(self, namespace: str, header: Mapping[str, Any], items: Sequence[Mapping[str, Any]], *,
                   source_id: str | None, run_id: str, principal_id: str, scopes: Iterable[str],
                   retrieved_at_ms: int | None = None) -> dict[str, Any]:
        """Append one unit: new revisions, observations and listing removals; never an overwrite."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        provider = str(header.get("provider") or "")
        if provider not in LIVE_VERIFICATION or provider in ("rfc6962-logs", "caida"):
            raise InfrastructureRecordError("invalid_unit", "a unit names an implemented provider")
        if int(header.get("item_count", -1)) != len(items) or not header.get("complete", False):
            raise InfrastructureRecordError("incomplete_unit", "a unit carries every item it states")
        for item in items:
            # The whole unit is refused when one item breaks the record rules or the minimisation decision.
            check_item(item)
            if item["provider"] != provider:
                raise InfrastructureRecordError("invalid_unit", "every item of a unit belongs to its provider")
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        resource = dict(header["resource"])
        unit_id = "ii-unit:" + digest([namespace, header["unit_key"], header["unit_sha256"]])[:24]
        if self.conn.execute("SELECT 1 FROM ii_units WHERE namespace=? AND unit_id=?",
                             [namespace, unit_id]).fetchone():
            self._receipt(namespace, run_id, source_id, provider, "unchanged", retrieved,
                          {"unit_id": unit_id, "unit_key": header["unit_key"]})
            return {"unit_id": unit_id, "status": "unchanged", "revisions": 0, "observations": 0, "removed": 0,
                    "unchanged": len(items)}
        counts = {"revisions": 0, "observations": 0, "removed": 0, "unchanged": 0}
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO ii_units VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, unit_id, provider, source_id, header["unit_key"], resource["kind"], resource["value"],
                 canonical(dict(header)), retrieved, self._origin(header), run_id, principal_id])
            self.conn.execute("INSERT OR IGNORE INTO ii_declared VALUES (?,?,?,?,?,?,?)",
                              [namespace, resource["kind"], resource["value"], provider, source_id, unit_id,
                               retrieved])
            seen: set[str] = set()
            for item in items:
                if item["record_type"] == "listing":
                    continue
                url = self._url(header)
                if item["record_type"] == "observation":
                    added = self._observe(namespace, item, unit_id, retrieved, url)
                    counts["observations" if added else "unchanged"] += 1
                else:
                    object_id, added = self._revise(namespace, item, unit_id, retrieved, url)
                    seen.add(object_id)
                    if added == "removed_by_source":
                        counts["removed"] += 1
                    counts["revisions" if added else "unchanged"] += 1
            for item in items:
                if item["record_type"] == "listing":
                    counts["removed"] += self._listing(namespace, item, unit_id, retrieved, seen)
            self._receipt(namespace, run_id, source_id, provider, "applied", retrieved,
                          {"unit_id": unit_id, "unit_key": header["unit_key"], **counts})
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"unit_id": unit_id, "status": "applied", **counts}

    @staticmethod
    def _origin(header: Mapping[str, Any]) -> str:
        origin = str(header.get("evidence_origin") or "live")
        return origin if origin in {"fixture", "live", "operator"} else "live"

    @staticmethod
    def _url(header: Mapping[str, Any]) -> str | None:
        files = [f for f in header.get("files") or [] if not f.get("bootstrap")]
        return files[0]["url"] if files else None

    def _object(self, namespace: str, item: Mapping[str, Any], unit_id: str) -> str:
        object_id = object_id_for(namespace, item["provider"], item["object_kind"], item["native_id"])
        self.conn.execute(
            "INSERT OR IGNORE INTO ii_objects VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, object_id, item["provider"], item["object_kind"], str(item["native_id"]),
             SHAPES[item["record_type"]], item["resource"]["kind"], item["resource"]["value"], item.get("label"),
             unit_id, self.now()])
        return object_id

    def _latest(self, namespace: str, object_id: str) -> dict[str, Any] | None:
        rows = self.revisions(namespace, object_id)
        return rows[-1] if rows else None

    def _revise(self, namespace: str, item: Mapping[str, Any], unit_id: str, retrieved: int,
                url: str | None) -> tuple[str, str | None]:
        object_id = self._object(namespace, item, unit_id)
        latest = self._latest(namespace, object_id)
        content = dict(item.get("content") or {})
        content_digest = digest([item["state"], content])
        if latest and latest["content_digest"] == content_digest:
            return object_id, None
        # A 404 for a declared object never seen before is recorded too, so the absence is cited, not silent.
        revision = dict(item["revision"])
        stated = to_ms(revision.get("valid_from"))
        valid_from, valid_basis = (stated, "source_stated") if stated is not None else (retrieved, "retrieval_time")
        basis = revision["basis"] if stated is not None or revision["basis"] in {"not_found", "content_digest"} \
            else "content_digest"
        source_revision = revision.get("source_revision") or ("sha256:" + content_digest[:16])
        self._insert_revision(namespace, object_id, latest, item["state"], basis, source_revision, valid_from,
                              valid_basis, retrieved, content_digest, content, item.get("stated_identifiers") or {},
                              url, unit_id, _changes(None if latest is None else latest["content"], content))
        return object_id, item["state"]

    def _insert_revision(self, namespace, object_id, latest, state, basis, source_revision, valid_from, valid_basis,
                         retrieved, content_digest, content, identifiers, url, unit_id, changes):
        number = 1 + (latest["revision_no"] if latest else 0)
        revision_id = "ii-rev:" + digest([namespace, object_id, number, content_digest])[:24]
        self.conn.execute(
            "INSERT INTO ii_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, object_id, number, latest["revision_id"] if latest else None, state, basis,
             source_revision, valid_from, valid_basis, retrieved, content_digest, canonical(content),
             canonical(identifiers), url, unit_id, canonical(changes), self.now()])
        return revision_id

    def _observe(self, namespace: str, item: Mapping[str, Any], unit_id: str, retrieved: int,
                 url: str | None) -> bool:
        object_id = self._object(namespace, item, unit_id)
        stated = to_ms(item["stated_time"])
        content = dict(item.get("content") or {})
        content_digest = digest(content)
        observation_id = "ii-obs:" + digest([namespace, object_id, stated, item["data_call_version"],
                                             content_digest])[:24]
        if self.conn.execute("SELECT 1 FROM ii_observations WHERE namespace=? AND observation_id=?",
                             [namespace, observation_id]).fetchone():
            return False
        previous = self.conn.execute(
            "SELECT observation_id, content_digest FROM ii_observations WHERE namespace=? AND object_id=? AND "
            "stated_time_ms<? ORDER BY stated_time_ms DESC, created_at_ms DESC LIMIT 1",
            [namespace, object_id, stated]).fetchone()
        self.conn.execute(
            "INSERT INTO ii_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, observation_id, object_id, item["data_call"], item["data_call_version"],
             item["data_call_status"], stated, item["time_basis"], retrieved, content_digest, canonical(content),
             canonical(item.get("query") or {}), canonical(item.get("stated_identifiers") or {}),
             previous[0] if previous else None, bool(previous is None or previous[1] != content_digest), url,
             unit_id, self.now()])
        return True

    def _listing(self, namespace: str, item: Mapping[str, Any], unit_id: str, retrieved: int,
                 seen: set[str]) -> int:
        """Members a complete earlier listing stated and this complete listing does not are removed_by_source."""
        previous = self.conn.execute(
            "SELECT members_json FROM ii_listings WHERE namespace=? AND listing_key=? ORDER BY retrieved_at_ms DESC, "
            "listing_id DESC LIMIT 1", [namespace, item["listing_key"]]).fetchone()
        members = [str(m) for m in item["members"]]
        listing_id = "ii-listing:" + digest([namespace, item["listing_key"], unit_id])[:24]
        self.conn.execute("INSERT OR IGNORE INTO ii_listings VALUES (?,?,?,?,?,?,?,?)",
                          [namespace, listing_id, item["listing_key"], item["provider"], item["object_kind"],
                           canonical(members), unit_id, retrieved])
        removed = 0
        for member in sorted(set(load(previous[0], []) if previous else []) - set(members)):
            object_id = object_id_for(namespace, item["provider"], item["object_kind"], member)
            latest = self._latest(namespace, object_id)
            if latest is None or latest["state"] != "published" or object_id in seen:
                continue
            content = {**latest["content"], "listing_as_answered": f"absent from the complete listing "
                                                                   f"{item['listing_key']}"}
            content_digest = digest(["removed_by_source", content])
            self._insert_revision(namespace, object_id, latest, "removed_by_source", "absent_from_complete_listing",
                                  item["listing_key"], retrieved, "retrieval_time", retrieved, content_digest,
                                  content, latest["identifiers"], None, unit_id,
                                  {"new_object": False, "fields": {"state": {"before": "published",
                                                                             "after": "removed_by_source"}}})
            removed += 1
        return removed

    def record_failure(self, namespace: str, provider: str, *, code: str, run_id: str, source_id: str | None,
                       scopes: Iterable[str], detail: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """A failed unit or run: a receipt only. Earlier revisions stay current and nothing is marked removed."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        now = self.now()
        receipt_id = self._receipt(namespace, run_id, source_id, provider, "failed", now,
                                   {"code": code, **dict(detail or {})})
        return {"receipt_id": receipt_id, "outcome": "failed", "code": code,
                "note": "earlier revisions stay current; a failure never produces a removal"}

    def _receipt(self, namespace, run_id, source_id, provider, outcome, at_ms, detail) -> str:
        receipt_id = "ii-receipt:" + digest([namespace, run_id, source_id, outcome, detail, at_ms])[:24]
        self.conn.execute("INSERT OR IGNORE INTO ii_receipts VALUES (?,?,?,?,?,?,?,?)",
                          [namespace, receipt_id, run_id, source_id, provider, outcome, canonical(dict(detail)),
                           at_ms])
        return receipt_id

    # ------------------------------------------------------------------ reads

    def receipts(self, namespace: str, provider: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT receipt_id, run_id, source_id, provider, outcome, detail_json, created_at_ms FROM ii_receipts "
            "WHERE namespace=? AND (? IS NULL OR provider=?) ORDER BY created_at_ms, receipt_id",
            [namespace, provider, provider]).fetchall()
        return [{"receipt_id": r[0], "run_id": r[1], "source_id": r[2], "provider": r[3], "outcome": r[4],
                 "detail": load(r[5], {}), "at": iso(r[6])} for r in rows]

    def provider_state(self, namespace: str, provider: str) -> dict[str, Any]:
        rows = self.receipts(namespace, provider)
        if not [r for r in rows if r["outcome"] in {"applied", "unchanged"}]:
            return {"stale": True, "reason": "never acquired"}
        return {"stale": rows[-1]["outcome"] == "failed",
                "reason": "last run failed" if rows[-1]["outcome"] == "failed" else None}

    def unit(self, namespace: str, unit_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT unit_id, provider, source_id, unit_key, resource_kind, resource, header_json, retrieved_at_ms, "
            "evidence_origin, run_id FROM ii_units WHERE namespace=? AND unit_id=?", [namespace, unit_id]).fetchone()
        if not row:
            raise InfrastructureRecordError("unit_not_found", "no such unit")
        return {"unit_id": row[0], "provider": row[1], "source_id": row[2], "unit_key": row[3],
                "resource": {"kind": row[4], "value": row[5]}, "header": load(row[6], {}),
                "retrieved_at": iso(row[7]), "evidence_origin": row[8], "run_id": row[9]}

    def declared(self, namespace: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT resource_kind, resource, list(DISTINCT provider ORDER BY provider) FROM ii_declared WHERE "
            "namespace=? GROUP BY resource_kind, resource ORDER BY resource_kind, resource", [namespace]).fetchall()
        return [{"kind": r[0], "value": r[1], "providers": list(r[2])} for r in rows]

    def is_declared(self, namespace: str, resource: Mapping[str, str]) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM ii_declared WHERE namespace=? AND resource_kind=? AND resource=?",
            [namespace, resource["kind"], resource["value"]]).fetchone())

    @staticmethod
    def _object_row(row: Sequence[Any]) -> dict[str, Any]:
        return {"object_id": row[0], "provider": row[1], "object_kind": row[2], "native_id": row[3], "shape": row[4],
                "resource": {"kind": row[5], "value": row[6]}, "label": row[7], "first_unit_id": row[8]}

    def object(self, namespace: str, object_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {_OBJECT_COLUMNS} FROM ii_objects WHERE namespace=? AND object_id=?",
                                [namespace, object_id]).fetchone()
        if not row:
            raise InfrastructureRecordError("object_not_found", "no such registry object or observation series")
        return self._object_row(row)

    def objects(self, namespace: str, *, provider: str | None = None, resource: Mapping[str, str] | None = None,
                object_kind: str | None = None) -> list[dict[str, Any]]:
        sql = f"SELECT {_OBJECT_COLUMNS} FROM ii_objects WHERE namespace=?"
        params: list[Any] = [namespace]
        if provider:
            sql += " AND provider=?"
            params.append(provider)
        if object_kind:
            sql += " AND object_kind=?"
            params.append(object_kind)
        if resource:
            sql += " AND resource_kind=? AND resource=?"
            params += [resource["kind"], resource["value"]]
        return [self._object_row(r) for r in self.conn.execute(sql + " ORDER BY provider, object_kind, native_id",
                                                               params).fetchall()]

    @staticmethod
    def _revision_row(row: Sequence[Any]) -> dict[str, Any]:
        return {"revision_id": row[0], "object_id": row[1], "revision_no": row[2], "previous_revision_id": row[3],
                "state": row[4], "basis": row[5], "source_revision": row[6], "valid_from_ms": row[7],
                "valid_from": iso(row[7]), "valid_from_basis": row[8], "retrieved_at": iso(row[9]),
                "content_digest": row[10], "content": load(row[11], {}), "identifiers": load(row[12], {}),
                "url": row[13], "unit_id": row[14], "changes": load(row[15], {})}

    def revisions(self, namespace: str, object_id: str) -> list[dict[str, Any]]:
        """The revision chain of one object, oldest first (``previous_revision_id`` links each to the one before)."""
        return [self._revision_row(r) for r in self.conn.execute(
            f"SELECT {_REVISION_COLUMNS} FROM ii_revisions WHERE namespace=? AND object_id=? ORDER BY revision_no",
            [namespace, object_id]).fetchall()]

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute(f"SELECT {_REVISION_COLUMNS} FROM ii_revisions WHERE namespace=? AND revision_id=?",
                                [namespace, revision_id]).fetchone()
        if not row:
            raise InfrastructureRecordError("revision_not_found", "no such revision")
        return self._revision_row(row)

    def revision_as_of(self, namespace: str, object_id: str, as_of_ms: int) -> dict[str, Any] | None:
        """The revision in force at an instant: the latest as-of time on or before it (ties: the later revision)."""
        row = self.conn.execute(
            f"SELECT {_REVISION_COLUMNS} FROM ii_revisions WHERE namespace=? AND object_id=? AND valid_from_ms<=? "
            "ORDER BY valid_from_ms DESC, revision_no DESC LIMIT 1", [namespace, object_id, as_of_ms]).fetchone()
        return self._revision_row(row) if row else None

    @staticmethod
    def _observation_row(row: Sequence[Any]) -> dict[str, Any]:
        return {"observation_id": row[0], "object_id": row[1], "data_call": row[2], "data_call_version": row[3],
                "data_call_status": row[4], "stated_time_ms": row[5], "stated_time": iso(row[5]),
                "time_basis": row[6], "retrieved_at": iso(row[7]), "content_digest": row[8],
                "content": load(row[9], {}), "query": load(row[10], {}), "identifiers": load(row[11], {}),
                "previous_observation_id": row[12], "changed": bool(row[13]), "url": row[14], "unit_id": row[15]}

    def observations(self, namespace: str, object_id: str) -> list[dict[str, Any]]:
        return [self._observation_row(r) for r in self.conn.execute(
            f"SELECT {_OBSERVATION_COLUMNS} FROM ii_observations WHERE namespace=? AND object_id=? "
            "ORDER BY stated_time_ms, created_at_ms", [namespace, object_id]).fetchall()]

    def observation_as_of(self, namespace: str, object_id: str, as_of_ms: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            f"SELECT {_OBSERVATION_COLUMNS} FROM ii_observations WHERE namespace=? AND object_id=? AND "
            "stated_time_ms<=? ORDER BY stated_time_ms DESC, created_at_ms DESC LIMIT 1",
            [namespace, object_id, as_of_ms]).fetchone()
        return self._observation_row(row) if row else None

    def cite(self, namespace: str, obj: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
        return citation(obj, record, self.unit(namespace, record["unit_id"]))

    def history(self, namespace: str, object_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Every revision (or observation) of one object with its citation; nothing is rewritten."""
        authorize(namespace, scopes, READ_SCOPE)
        obj = self.object(namespace, object_id)
        if obj["shape"] == "observations":
            entries = [{**o, "citation": self.cite(namespace, obj, o)} for o in self.observations(namespace,
                                                                                                    object_id)]
            return {"object": obj, "observations": entries}
        entries = [{**r, "citation": self.cite(namespace, obj, r)} for r in self.revisions(namespace, object_id)]
        return {"object": obj, "revisions": entries}

    def latest_ms(self, namespace: str) -> int | None:
        row = self.conn.execute("SELECT max(retrieved_at_ms) FROM ii_units WHERE namespace=?",
                                [namespace]).fetchone()
        return int(row[0]) if row and row[0] is not None else None


class InternetInfrastructureProjector:
    """Source-pack runtime projector for ``noesis-internet-infrastructure-record-v2`` pages (one unit per page)."""

    @staticmethod
    def scopes_for(namespace: str) -> set[str]:
        return {WRITE_SCOPE, READ_SCOPE, f"namespace:{namespace}:write", f"namespace:{namespace}:read"}

    def __init__(self, conn: Any) -> None:
        self.store = InternetInfrastructureStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("internet_infrastructure") or {}).get("namespace") or "global")

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, page_receipt, documents
        namespace = self._namespace(source)
        units: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for record in records:
            header, body = record.get("ii_unit"), record.get("ii_item")
            if not header or not isinstance(body, Mapping):
                raise InfrastructureRecordError("invalid_record", "page record is not an internet-infrastructure item")
            units.setdefault(header["unit_sha256"], (dict(header), []))[1].append(dict(body))
        return [self.store.apply_unit(namespace, header, items, source_id=source.get("source_id"), run_id=run_id,
                                      principal_id=principal_id or "source-runtime",
                                      scopes=self.scopes_for(namespace))
                for header, items in units.values()]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        namespace = self._namespace(source)
        provider = str(dict(source.get("internet_infrastructure") or {}).get("provider"))
        if status != "complete":
            self.store.record_failure(namespace, provider, code="source_run_" + str(status), run_id=run_id,
                                      source_id=source.get("source_id"), scopes=self.scopes_for(namespace))
        return {"status": status, "provider": provider, "namespace": namespace,
                "provider_state": self.store.provider_state(namespace, provider)}


__all__ = ["InternetInfrastructureProjector", "InternetInfrastructureStore", "citation", "object_id_for"]
