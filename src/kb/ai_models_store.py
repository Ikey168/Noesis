"""Append-only store for AI model and dataset registry records (#2742, AI02).

Follows :mod:`src.kb.oss_ecosystem_store` and :mod:`src.kb.business_statistics_store`. Tables (all ``ai_*``,
namespace-scoped):

* ``ai_records`` - one row per record: a Hub model or dataset repository, its refs, an OpenML dataset version or task,
  or an Epoch notable-model row (``record_kind`` and the source's own key);
* ``ai_revisions`` - append-only revisions with the source's revision key (the Hub ``sha``; the content digest
  otherwise), a state (``published``, ``withdrawn``, ``removed_by_source``, ``renamed``), the source time it is dated
  by and its basis (``last_modified``, ``upload_date``, ``page_last_updated`` or ``retrieval_time``), the retrieval
  time, the recorded changes against the previous revision and the Epoch vintage it came from. A pinned ``sha`` is
  immutable: different content under a stored ``sha`` is refused, never overwritten;
* ``ai_observations`` and ``ai_observation_states`` - self-reported card results and OpenML run evaluations, each with
  who reported it; an evaluation is immutable per run id and a run a complete later listing no longer names gets an
  appended ``not-returned`` state, never a deletion;
* ``ai_vintages`` - Epoch file vintages by content digest, dated by the page's last-updated stamp or the retrieval
  time (``retrieval_time``);
* ``ai_receipts`` - one receipt per applied unit or failure (provider state is read from them).

As-of reads select the revision current at the requested instant by its source time and say which. Nothing is merged
across sources and nothing is deleted.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.ingestion.ai_models_sources import FORMATS, LIVE_VERIFICATION
from src.kb.ai_models_records import (
    CONTRACT,
    READ_SCOPE,
    WRITE_SCOPE,
    AiModelsError,
    authorize,
    canonical,
    declared_licence_value,
    digest,
    iso,
    licence_of,
    load,
    observation_key,
    record_id,
    record_identity,
    table_exists,
    to_ms,
    validate,
)

_DDL = """
CREATE TABLE IF NOT EXISTS ai_records (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, source TEXT NOT NULL, record_kind TEXT NOT NULL,
  native_key TEXT NOT NULL, label TEXT NOT NULL, first_run_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS ai_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL, seq INTEGER NOT NULL,
  revision_key TEXT NOT NULL, state TEXT NOT NULL, source_time_ms BIGINT NOT NULL, time_basis TEXT NOT NULL,
  retrieved_at_ms BIGINT NOT NULL, content_hash TEXT NOT NULL, statement_json TEXT NOT NULL,
  previous_revision_id TEXT, changes_json TEXT NOT NULL, vintage_id TEXT, unit_key TEXT NOT NULL,
  source_id TEXT, run_id TEXT NOT NULL, evidence_origin TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS ai_observations (
  namespace TEXT NOT NULL, observation_id TEXT NOT NULL, record_id TEXT NOT NULL, revision_id TEXT,
  kind TEXT NOT NULL, observation_key TEXT NOT NULL, reported_by TEXT NOT NULL, value_hash TEXT NOT NULL,
  statement_json TEXT NOT NULL, first_seen_ms BIGINT NOT NULL, run_id TEXT NOT NULL, evidence_origin TEXT NOT NULL,
  PRIMARY KEY(namespace, observation_id)
);
CREATE TABLE IF NOT EXISTS ai_observation_states (
  namespace TEXT NOT NULL, observation_id TEXT NOT NULL, seq INTEGER NOT NULL, state TEXT NOT NULL,
  at_ms BIGINT NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY(namespace, observation_id, seq)
);
CREATE TABLE IF NOT EXISTS ai_vintages (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, source TEXT NOT NULL, unit_key TEXT NOT NULL,
  file_sha256 TEXT NOT NULL, release_at_ms BIGINT NOT NULL, release_basis TEXT NOT NULL,
  retrieved_at_ms BIGINT NOT NULL, last_updated TEXT, run_id TEXT NOT NULL, evidence_origin TEXT NOT NULL,
  PRIMARY KEY(namespace, vintage_id)
);
CREATE TABLE IF NOT EXISTS ai_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT, provider TEXT NOT NULL,
  outcome TEXT NOT NULL, execution TEXT NOT NULL, detail_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, receipt_id)
);
"""
_REVISION_COLUMNS = ("revision_id, record_id, seq, revision_key, state, source_time_ms, time_basis, retrieved_at_ms, "
                     "content_hash, statement_json, previous_revision_id, changes_json, vintage_id, unit_key, "
                     "source_id, run_id, evidence_origin")
_REVISION_KEYS = ("revision_id", "record_id", "seq", "revision_key", "state", "source_time_ms", "time_basis",
                  "retrieved_at_ms", "content_hash", "statement", "previous_revision_id", "changes", "vintage_id",
                  "unit_key", "source_id", "run_id", "evidence_origin")
# Statement fields that are bookkeeping, not content (excluded from what a revision changes).
_BOOKKEEPING = frozenset({"url"})


def _state(statement: Mapping[str, Any]) -> str:
    if statement.get("record_type") == "openml_dataset_revision" and statement.get("status") == "deactivated":
        return "withdrawn"  # the source states the dataset deactivated
    return str(statement.get("state") or "published")


def _label(statement: Mapping[str, Any]) -> str:
    for key in ("repo_id", "name", "model"):
        if statement.get(key):
            return str(statement[key])
    if statement.get("task_id") is not None:
        return f"OpenML task {statement['task_id']}"
    return str(statement.get("record_type"))


def revision_reference(record: Mapping[str, Any], revision: Mapping[str, Any]) -> dict[str, Any]:
    """The source's own revision reference: the Hub sha, the OpenML id and version, or the Epoch vintage."""
    statement = revision["statement"]
    if record["record_kind"] in {"hub-model", "hub-dataset"}:
        return {"repo_id": record["native_key"], "sha": statement.get("sha"), "state": revision["state"]}
    if record["record_kind"] == "openml-dataset":
        return {"openml_id": int(record["native_key"]), "version": statement.get("version"),
                "status": statement.get("status")}
    if record["record_kind"] == "openml-task":
        return {"openml_task_id": int(record["native_key"])}
    if record["record_kind"] == "epoch-model":
        return {"epoch_model": record["native_key"], "vintage_id": revision.get("vintage_id")}
    return {"native_key": record["native_key"]}


def citation(record: Mapping[str, Any], revision: Mapping[str, Any]) -> dict[str, Any]:
    """What every answer cites: source, record revision and as-of time."""
    return {
        "source": record["source"], "record_id": record["record_id"], "record_kind": record["record_kind"],
        "revision_id": revision["revision_id"], "revision": revision_reference(record, revision),
        "as_of": revision["source_time"], "time_basis": revision["time_basis"],
        "retrieved_at": revision["retrieved_at"], "url": revision["statement"].get("url"),
        "source_id": revision.get("source_id"), "evidence_origin": revision["evidence_origin"], "contract": CONTRACT,
        "live_verification": LIVE_VERIFICATION[record["source"]]["status"],
    }


def _changes(before: Mapping[str, Any] | None, after: Mapping[str, Any], state_before: str | None,
             state_after: str) -> dict[str, Any]:
    if before is None:
        return {"first_revision": True}
    keys = sorted((set(before) | set(after)) - _BOOKKEEPING - {"record_type", "source"})
    changed = [k for k in keys if before.get(k) != after.get(k)]
    out: dict[str, Any] = {"changed_fields": changed}
    old, new = declared_licence_value(licence_of(before)), declared_licence_value(licence_of(after))
    if licence_of(before) != licence_of(after) and (licence_of(before) or licence_of(after)):
        out["licence_change"] = {"before": licence_of(before), "after": licence_of(after),
                                 "declared_before": old, "declared_after": new}
    if state_before != state_after:
        out["state_change"] = {"before": state_before, "after": state_after}
    return out


class AiModelsStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "ai_revisions")

    # ------------------------------------------------------------------ writes

    def apply_unit(self, namespace: str, header: Mapping[str, Any], statements: Sequence[Mapping[str, Any]], *,
                   source_id: str | None, run_id: str, principal_id: str, scopes: Iterable[str],
                   retrieved_at_ms: int | None = None) -> dict[str, Any]:
        """Append one acquired unit: revisions, observations, not-returned runs and removed rows; never an overwrite."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        del principal_id
        provider, fmt = str(header.get("provider") or ""), header.get("format")
        if FORMATS.get(str(fmt), {}).get("provider") != provider:
            raise AiModelsError("invalid_unit", "a unit names a known provider and its format")
        if int(header.get("statement_count", -1)) != len(statements):
            raise AiModelsError("incomplete_unit", "a unit carries every statement it states")
        # The whole unit is refused when one statement breaks the record rules or the minimisation decision.
        values = [validate(s) for s in statements]
        if any(v["source"] != provider for v in values):
            raise AiModelsError("invalid_unit", "every statement of a unit belongs to its provider")
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        origin = header.get("evidence_origin") if header.get("evidence_origin") in {"fixture", "operator"} else "live"
        counts = {"revisions": 0, "unchanged": 0, "observations": 0, "not_returned": 0, "removed": 0}
        self.conn.execute("BEGIN")
        try:
            vintage = self._vintage(namespace, header, provider, retrieved, run_id, origin)
            context = {"unit_key": str(header.get("unit_key")), "source_id": source_id, "run_id": run_id,
                       "origin": origin, "retrieved": retrieved, "vintage": vintage}
            revised: dict[str, str] = {}
            for value in values:
                if value["record_type"] in {"evaluation_listing", "epoch_listing", "observation"}:
                    continue
                outcome, revision = self._revise(namespace, value, context)
                counts["revisions" if outcome == "new" else "unchanged"] += 1
                kind, native = record_identity(value)
                revised[record_id(namespace, kind, native)] = revision
            for value in values:
                if value["record_type"] == "observation":
                    counts["observations"] += self._observe(namespace, value, context, revised)
            for value in values:
                if value["record_type"] == "evaluation_listing" and value["complete"]:
                    counts["not_returned"] += self._not_returned(namespace, value, context)
                elif value["record_type"] == "epoch_listing" and value["complete"]:
                    counts["removed"] += self._removed_rows(namespace, value, context)
            status = "applied" if counts["revisions"] or counts["observations"] or counts["not_returned"] or \
                counts["removed"] else "unchanged"
            self._receipt(namespace, run_id, source_id, provider, status, origin,
                          {"unit_key": header.get("unit_key"), "content_sha256": header.get("content_sha256"),
                           "retrieved_at": iso(retrieved), **counts})
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"unit_key": header.get("unit_key"), "status": status, **counts}

    def _vintage(self, namespace, header, provider, retrieved, run_id, origin) -> dict[str, Any] | None:
        declared = dict(header.get("vintage") or {})
        if provider != "epoch-ai" or not declared.get("file_sha256"):
            return None
        vintage_id = "ai-vintage:" + digest([namespace, header.get("unit_key"), declared["file_sha256"]])[:24]
        row = self.conn.execute("SELECT release_at_ms, release_basis FROM ai_vintages WHERE namespace=? AND "
                                "vintage_id=?", [namespace, vintage_id]).fetchone()
        if row:
            return {"vintage_id": vintage_id, "release_at_ms": int(row[0]), "release_basis": row[1]}
        stamp = declared.get("last_updated")
        release_ms, basis = (to_ms(stamp), "page_last_updated") if stamp else (retrieved, "retrieval_time")
        self.conn.execute("INSERT INTO ai_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          [namespace, vintage_id, provider, str(header.get("unit_key")), declared["file_sha256"],
                           release_ms, basis, retrieved, stamp, run_id, origin])
        return {"vintage_id": vintage_id, "release_at_ms": int(release_ms), "release_basis": basis}

    def _record(self, namespace: str, value: Mapping[str, Any], run_id: str) -> str:
        kind, native = record_identity(value)
        rid = record_id(namespace, kind, native)
        if not self.conn.execute("SELECT 1 FROM ai_records WHERE namespace=? AND record_id=?",
                                 [namespace, rid]).fetchone():
            self.conn.execute("INSERT INTO ai_records VALUES (?,?,?,?,?,?,?,?)",
                              [namespace, rid, value["source"], kind, native, _label(value), run_id, self.now()])
        return rid

    def _clock(self, value: Mapping[str, Any], prior: list[dict[str, Any]], context) -> tuple[int, str]:
        record_type, state = value["record_type"], _state(value)
        if record_type == "hub_repository_revision" and state == "published":
            return int(to_ms(value["last_modified"])), "last_modified"
        if record_type == "openml_dataset_revision" and not prior and value.get("upload_date"):
            return int(to_ms(value["upload_date"])), "upload_date"
        if record_type == "epoch_model_revision" and context["vintage"]:
            return int(context["vintage"]["release_at_ms"]), context["vintage"]["release_basis"]
        return int(context["retrieved"]), "retrieval_time"

    def _insert_revision(self, namespace, rid, revision_key, value, state, clock, basis, prior, context) -> str:
        latest = prior[-1] if prior else None
        content_hash = digest({k: v for k, v in value.items() if k not in _BOOKKEEPING})
        revision_id = "ai-revision:" + digest([namespace, rid, revision_key, content_hash, len(prior)])[:24]
        changes = _changes(latest["statement"] if latest else None, value, latest["state"] if latest else None, state)
        self.conn.execute(
            "INSERT INTO ai_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, rid, 1 + len(prior), revision_key, state, clock, basis, context["retrieved"],
             content_hash, canonical(value), latest["revision_id"] if latest else None, canonical(changes),
             (context["vintage"] or {}).get("vintage_id") if value["record_type"] == "epoch_model_revision" else None,
             context["unit_key"], context["source_id"], context["run_id"], context["origin"], self.now()])
        return revision_id

    def _revise(self, namespace: str, value: Mapping[str, Any], context) -> tuple[str, str]:
        rid = self._record(namespace, value, context["run_id"])
        prior = self.revision_rows(namespace, rid, order="seq")
        latest = prior[-1] if prior else None
        state = _state(value)
        content_hash = digest({k: v for k, v in value.items() if k not in _BOOKKEEPING})
        clock, basis = self._clock(value, prior, context)
        if value["record_type"] == "hub_repository_revision" and state == "published":
            pinned = [r for r in prior if r["revision_key"] == value["sha"]]
            if pinned:
                if pinned[0]["content_hash"] != content_hash:
                    raise AiModelsError("immutable_revision", "a pinned sha is immutable; the source stated other "
                                                              "content under a stored sha", sha=value["sha"])
                if latest is not None and latest["state"] != "published":
                    # The repository is public again at a stored sha: a new revision, dated by retrieval.
                    revision_id = self._insert_revision(namespace, rid, f"{value['sha']}@{context['retrieved']}",
                                                        value, state, int(context["retrieved"]), "retrieval_time",
                                                        prior, context)
                    return "new", revision_id
                return "unchanged", pinned[0]["revision_id"]
            revision_key = value["sha"]
        else:
            if latest is not None and latest["content_hash"] == content_hash:
                return "unchanged", latest["revision_id"]
            revision_key = f"{state}:{content_hash[:16]}:{len(prior) + 1}"
        return "new", self._insert_revision(namespace, rid, revision_key, value, state, clock, basis, prior, context)

    def _observe(self, namespace: str, value: Mapping[str, Any], context, revised: Mapping[str, str]) -> int:
        kind, native = record_identity(value)
        rid = record_id(namespace, kind, native)
        if not self.conn.execute("SELECT 1 FROM ai_records WHERE namespace=? AND record_id=?",
                                 [namespace, rid]).fetchone():
            raise AiModelsError("invalid_unit", "an observation belongs to a record of the same unit")
        key = observation_key(value)
        observation_id = "ai-observation:" + digest([namespace, key])[:24]
        value_hash = digest({k: v for k, v in value.items() if k not in _BOOKKEEPING})
        row = self.conn.execute("SELECT value_hash FROM ai_observations WHERE namespace=? AND observation_id=?",
                                [namespace, observation_id]).fetchone()
        if row:
            if row[0] != value_hash:
                raise AiModelsError("immutable_observation", "an evaluation is immutable per run id and a "
                                                             "self-reported result per revision sha; the stored "
                                                             "value is kept", observation_key=key)
            states = self.observation_states(namespace, observation_id)
            if states and states[-1]["state"] == "not-returned":
                self._observation_state(namespace, observation_id, "reported", context)
                return 1
            return 0
        revision_id = None
        if value["observation_kind"] == "self_reported_result":
            pinned = self.conn.execute("SELECT revision_id FROM ai_revisions WHERE namespace=? AND record_id=? AND "
                                       "revision_key=?", [namespace, rid, value["sha"]]).fetchone()
            revision_id = pinned[0] if pinned else None
        else:
            revision_id = revised.get(rid)
        self.conn.execute("INSERT INTO ai_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                          [namespace, observation_id, rid, revision_id, value["observation_kind"], canonical(key),
                           value["reported_by"], value_hash, canonical(value), context["retrieved"],
                           context["run_id"], context["origin"]])
        self._observation_state(namespace, observation_id, "reported", context)
        return 1

    def _observation_state(self, namespace, observation_id, state, context) -> None:
        seq = 1 + int(self.conn.execute("SELECT count(*) FROM ai_observation_states WHERE namespace=? AND "
                                        "observation_id=?", [namespace, observation_id]).fetchone()[0])
        self.conn.execute("INSERT INTO ai_observation_states VALUES (?,?,?,?,?,?)",
                          [namespace, observation_id, seq, state, context["retrieved"], context["run_id"]])

    def _not_returned(self, namespace: str, listing: Mapping[str, Any], context) -> int:
        rid = record_id(namespace, "openml-task", str(listing["task_id"]))
        listed = {int(r) for r in listing["run_ids"]}
        count = 0
        for observation in self.observations(namespace, rid):
            statement = observation["statement"]
            if statement.get("measure") != listing["measure"] or observation["state"] != "reported":
                continue
            if int(statement["run_id"]) not in listed:
                self._observation_state(namespace, observation["observation_id"], "not-returned", context)
                count += 1
        return count

    def _removed_rows(self, namespace: str, listing: Mapping[str, Any], context) -> int:
        count = 0
        for model in sorted(set(listing["declared"]) - set(listing["present"])):
            rid = record_id(namespace, "epoch-model", model)
            prior = self.revision_rows(namespace, rid, order="seq")
            if not prior or prior[-1]["state"] == "removed_by_source":
                continue
            value = {"record_type": "epoch_model_revision", "source": "epoch-ai", "model": model,
                     "state": "removed_by_source", "state_detail": {
                         "basis": "a declared row missing from a complete later file; earlier revisions stay in its "
                                  "history"}, "url": listing.get("url")}
            clock, basis = self._clock(value, prior, context)
            self._insert_revision(namespace, rid, f"removed_by_source:{(context['vintage'] or {}).get('vintage_id')}",
                                  value, "removed_by_source", clock, basis, prior, context)
            count += 1
        return count

    def _receipt(self, namespace, run_id, source_id, provider, outcome, execution, detail) -> str:
        body = {"run_id": run_id, "source_id": source_id, "provider": provider, "outcome": outcome, **detail}
        receipt_id = "ai-receipt:" + digest([namespace, body])[:24]
        self.conn.execute("INSERT INTO ai_receipts VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [namespace, receipt_id, run_id, source_id, provider, outcome, execution or "none",
                           canonical(body), self.now()])
        return receipt_id

    def record_failure(self, namespace: str, provider: str, *, code: str, run_id: str, source_id: str | None,
                       scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self._receipt(namespace, run_id, source_id, provider, "failed", "none",
                      {"failure_code": code, "at": iso(self.now())})
        return {"provider": provider, "failure_code": code,
                "effect": "stored revisions unchanged; the source reads as stale, nothing is marked removed, withdrawn "
                          "or not-returned"}

    # ------------------------------------------------------------------ reads

    def _record_view(self, namespace: str, row: Sequence[Any]) -> dict[str, Any]:
        rid, source, kind, native, label = row
        revisions = self.revision_rows(namespace, rid)
        current = revisions[-1] if revisions else None
        return {"contract": CONTRACT, "namespace": namespace, "record_id": rid, "source": source,
                "record_kind": kind, "native_key": native, "label": label, "revision_count": len(revisions),
                "current_revision_id": current["revision_id"] if current else None,
                "current_state": current["state"] if current else None,
                "live_verification": LIVE_VERIFICATION[source]["status"]}

    def record(self, namespace: str, rid: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT record_id, source, record_kind, native_key, label FROM ai_records WHERE "
                                "namespace=? AND record_id=?", [namespace, rid]).fetchone() if self.ready() else None
        if row is None:
            raise AiModelsError("not_found", "no such AI model or dataset record")
        return self._record_view(namespace, row)

    def records(self, namespace: str, *, source: str | None = None,
                record_kind: str | None = None) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT record_id, source, record_kind, native_key, label FROM ai_records WHERE namespace=? AND "
            "(? IS NULL OR source=?) AND (? IS NULL OR record_kind=?) ORDER BY source, record_kind, native_key",
            [namespace, source, source, record_kind, record_kind]).fetchall()
        return [self._record_view(namespace, r) for r in rows]

    def find(self, namespace: str, record_kind: str, native_key: str) -> dict[str, Any] | None:
        rid = record_id(namespace, record_kind, str(native_key))
        try:
            return self.record(namespace, rid)
        except AiModelsError:
            return None

    def revision_rows(self, namespace: str, rid: str, *, order: str = "time") -> list[dict[str, Any]]:
        """Revisions of a record, by source time (``order='time'``, as-of order) or insertion (``'seq'``)."""
        if not self.ready():
            return []
        sort = "source_time_ms, seq" if order == "time" else "seq"
        rows = self.conn.execute(f"SELECT {_REVISION_COLUMNS} FROM ai_revisions WHERE namespace=? AND record_id=? "
                                 f"ORDER BY {sort}", [namespace, rid]).fetchall()
        out = []
        for row in rows:
            view = dict(zip(_REVISION_KEYS, row))
            view["statement"], view["changes"] = load(view["statement"], {}), load(view["changes"], {})
            view["source_time"], view["retrieved_at"] = iso(view["source_time_ms"]), iso(view["retrieved_at_ms"])
            out.append(view)
        return out

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT record_id FROM ai_revisions WHERE namespace=? AND revision_id=?",
                                [namespace, revision_id]).fetchone()
        if row is None:
            raise AiModelsError("not_found", "no such revision")
        return next(r for r in self.revision_rows(namespace, row[0]) if r["revision_id"] == revision_id)

    def select_revision(self, namespace: str, rid: str, *, as_of_ms: int | None = None
                        ) -> tuple[dict[str, Any] | None, str | None]:
        """The revision current at the cutoff by its source time; the reason when there is none."""
        revisions = self.revision_rows(namespace, rid)
        cutoff = as_of_ms if as_of_ms is not None else 2**62
        eligible = [r for r in revisions if r["source_time_ms"] <= cutoff]
        if eligible:
            return eligible[-1], None
        return None, "no_revision_by_as_of" if revisions else "no_revision"

    def observation_states(self, namespace: str, observation_id: str) -> list[dict[str, Any]]:
        return [{"seq": r[0], "state": r[1], "at": iso(r[2]), "at_ms": r[2], "run_id": r[3]} for r in self.conn.execute(
            "SELECT seq, state, at_ms, run_id FROM ai_observation_states WHERE namespace=? AND observation_id=? "
            "ORDER BY seq", [namespace, observation_id]).fetchall()]

    def observations(self, namespace: str, rid: str, *, sha: str | None = None,
                     as_of_ms: int | None = None) -> list[dict[str, Any]]:
        """Observations of a record with their state (as of a cutoff when given); who reported each is kept."""
        if not table_exists(self.conn, "ai_observations"):
            return []
        rows = self.conn.execute(
            "SELECT observation_id, revision_id, kind, reported_by, statement_json, first_seen_ms, evidence_origin "
            "FROM ai_observations WHERE namespace=? AND record_id=? ORDER BY kind, observation_key",
            [namespace, rid]).fetchall()
        out = []
        for observation_id, revision_id, kind, reported_by, statement, first_seen, origin in rows:
            statement = load(statement, {})
            if sha is not None and statement.get("sha") not in (None, sha):
                continue
            states = self.observation_states(namespace, observation_id)
            if as_of_ms is not None:
                states = [s for s in states if s["at_ms"] <= as_of_ms]
                if kind == "openml_run_evaluation" and statement.get("upload_time") and \
                        to_ms(statement["upload_time"]) > as_of_ms:
                    continue
            state = states[-1]["state"] if states else "reported"
            out.append({"observation_id": observation_id, "kind": kind, "revision_id": revision_id,
                        "reported_by": reported_by, "statement": statement, "state": state,
                        "state_history": [{k: s[k] for k in ("state", "at", "run_id")} for s in states],
                        "first_seen": iso(first_seen), "evidence_origin": origin})
        return out

    def vintage(self, namespace: str, vintage_id: str | None) -> dict[str, Any] | None:
        if not vintage_id or not table_exists(self.conn, "ai_vintages"):
            return None
        row = self.conn.execute("SELECT vintage_id, source, unit_key, file_sha256, release_at_ms, release_basis, "
                                "retrieved_at_ms, last_updated FROM ai_vintages WHERE namespace=? AND vintage_id=?",
                                [namespace, vintage_id]).fetchone()
        if row is None:
            return None
        return {"vintage_id": row[0], "source": row[1], "unit_key": row[2], "file_sha256": row[3],
                "release_at": iso(row[4]), "release_basis": row[5], "retrieved_at": iso(row[6]),
                "last_updated": row[7]}

    def provider_state(self, namespace: str, provider: str) -> dict[str, Any]:
        if not table_exists(self.conn, "ai_receipts"):
            return {"provider": provider, "last_success_ms": None, "stale": True, "reason": "never acquired"}
        rows = self.conn.execute("SELECT outcome, created_at_ms, run_id, detail_json FROM ai_receipts WHERE "
                                 "namespace=? AND provider=? ORDER BY rowid",
                                 [namespace, provider]).fetchall()
        successes = [r for r in rows if r[0] in {"applied", "unchanged"}]
        if not successes:
            return {"provider": provider, "last_success_ms": None, "stale": True,
                    "reason": "never acquired" if not rows else load(rows[-1][3], {}).get("failure_code")}
        failed = rows[-1][0] == "failed"
        return {"provider": provider, "last_success_ms": int(successes[-1][1]), "last_run_id": rows[-1][2],
                "stale": failed, "reason": load(rows[-1][3], {}).get("failure_code") if failed else None}

    def generation(self, namespace: str) -> str:
        if not self.ready():
            return digest([])[:24]
        revisions = [r[0] for r in self.conn.execute("SELECT revision_id FROM ai_revisions WHERE namespace=? "
                                                     "ORDER BY revision_id", [namespace]).fetchall()]
        states = [list(r) for r in self.conn.execute(
            "SELECT observation_id, seq FROM ai_observation_states WHERE namespace=? ORDER BY observation_id, seq",
            [namespace]).fetchall()] if table_exists(self.conn, "ai_observation_states") else []
        return digest([revisions, states])[:24]

    def latest_ms(self, namespace: str) -> int | None:
        if not self.ready():
            return None
        row = self.conn.execute("SELECT max(retrieved_at_ms) FROM ai_revisions WHERE namespace=?",
                                [namespace]).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def receipts(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "ai_receipts"):
            return []
        rows = self.conn.execute("SELECT receipt_id, run_id, source_id, provider, outcome, execution, detail_json FROM "
                                 "ai_receipts WHERE namespace=? ORDER BY rowid",
                                 [namespace]).fetchall()
        return [{"receipt_id": r[0], "run_id": r[1], "source_id": r[2], "provider": r[3], "outcome": r[4],
                 "execution": r[5], "detail": load(r[6], {})} for r in rows]


class AiModelsProjector:
    """Source-pack runtime projector for ``noesis-ai-model-record-v2`` pages (one unit per page)."""

    @staticmethod
    def scopes_for(namespace: str) -> set[str]:
        return {WRITE_SCOPE, READ_SCOPE, f"namespace:{namespace}:write", f"namespace:{namespace}:read"}

    def __init__(self, conn: Any) -> None:
        self.store = AiModelsStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("ai_models") or {}).get("namespace") or "global")

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, page_receipt, documents
        namespace = self._namespace(source)
        units: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for record in records:
            header, statement = record.get("ai_unit"), record.get("ai_statement")
            if not header or not isinstance(statement, Mapping):
                raise AiModelsError("invalid_record", "page record is not an ai-models unit statement")
            units.setdefault(str(header["unit_key"]) + str(header.get("content_sha256")),
                             (dict(header), []))[1].append(dict(statement))
        return [self.store.apply_unit(namespace, header, statements, source_id=source.get("source_id"),
                                      run_id=run_id, principal_id=principal_id or "source-runtime",
                                      scopes=self.scopes_for(namespace))
                for header, statements in units.values()]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        namespace = self._namespace(source)
        provider = str(dict(source.get("ai_models") or {}).get("provider"))
        if status != "complete":
            self.store.record_failure(namespace, provider, code="source_run_" + str(status), run_id=run_id,
                                      source_id=source.get("source_id"), scopes=self.scopes_for(namespace))
        return {"status": status, "provider": provider, "namespace": namespace,
                "provider_state": self.store.provider_state(namespace, provider)}


__all__ = ["AiModelsProjector", "AiModelsStore", "citation", "revision_reference"]
