"""Evidence-bearing canonical event resolution with reversible corrections."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from typing import Any

CONTRACT = "noesis-canonical-event-v1"
EVENT_RECORD_CONTRACT = "noesis-event-record-v2"
EVENT_MENTION_CONTRACT = "noesis-event-mention-v1"
EVENT_ACCOUNT_CONTRACT = "noesis-event-account-v1"
EVENT_RELATION_CONTRACT = "noesis-event-relation-v1"
EVENT_SEARCH_CONTRACT = "noesis-event-search-v1"
READ_SCOPE = "knowledge:event:read"
WRITE_SCOPE = "knowledge:event:write"
REVIEW_SCOPE = "knowledge:event:review"

_DDL = """
CREATE TABLE IF NOT EXISTS canonical_events (
  event_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, event_type TEXT NOT NULL,
  participants_json TEXT NOT NULL, location_json TEXT, start_ms BIGINT, end_ms BIGINT,
  recurrence_key TEXT, evidence_json TEXT NOT NULL, revision BIGINT NOT NULL,
  status TEXT NOT NULL, canonical_id TEXT, created_at_ms BIGINT NOT NULL,
  updated_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_event_reports (
  report_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, event_id TEXT,
  report_json TEXT NOT NULL, confidence DOUBLE NOT NULL, alternatives_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL, linked_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS canonical_event_operations (
  operation_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, action TEXT NOT NULL,
  before_json TEXT NOT NULL, after_json TEXT NOT NULL, status TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, reversed_at_ms BIGINT
);
"""


class EventResolutionError(RuntimeError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return (
        default
        if value is None
        else json.loads(value)
        if isinstance(value, str)
        else value
    )


def _jaccard(left: Sequence[str], right: Sequence[str]) -> float:
    a, b = set(left), set(right)
    return len(a & b) / len(a | b) if a | b else 1.0


class EventResolver:
    def __init__(
        self, conn: Any, *, initialize: bool = True, auto_link_threshold: float = 0.82
    ) -> None:
        self.conn, self.threshold = conn, auto_link_threshold
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def normalize(value: Mapping[str, Any]) -> dict[str, Any]:
        event_type = str(value.get("event_type", ""))
        participants = sorted({str(v) for v in value.get("participants", []) if str(v)})
        interval = dict(value.get("time") or {})
        evidence = list(value.get("evidence") or [])
        if not event_type or not participants or not evidence:
            raise EventResolutionError(
                "invalid_event",
                "event type, participants, and source evidence are required",
            )
        start = interval.get("start_ms")
        end = interval.get("end_ms", start)
        if start is not None and end is not None and int(end) < int(start):
            raise EventResolutionError(
                "invalid_interval", "event interval ends before it starts"
            )
        return {
            "event_type": event_type,
            "participants": participants,
            "location": value.get("location"),
            "time": {"start_ms": start, "end_ms": end},
            "recurrence_key": value.get("recurrence_key"),
            "evidence": evidence,
        }

    @staticmethod
    def score(
        left: Mapping[str, Any], right: Mapping[str, Any]
    ) -> tuple[float, dict[str, float]]:
        type_score = float(left["event_type"] == right["event_type"])
        participant = _jaccard(left["participants"], right["participants"])
        location = (
            float(_canonical(left.get("location")) == _canonical(right.get("location")))
            if left.get("location") or right.get("location")
            else 0.5
        )
        ls, le = left["time"].get("start_ms"), left["time"].get("end_ms")
        rs, re = right["time"].get("start_ms"), right["time"].get("end_ms")
        if None in {ls, le, rs, re}:
            temporal = 0.5
        else:
            overlap = max(0, min(int(le), int(re)) - max(int(ls), int(rs)) + 1)
            span = max(int(le), int(re)) - min(int(ls), int(rs)) + 1
            temporal = overlap / span
        recurrence = (
            float(left.get("recurrence_key") == right.get("recurrence_key"))
            if left.get("recurrence_key") or right.get("recurrence_key")
            else 1.0
        )
        parts = {
            "type": type_score,
            "participants": participant,
            "location": location,
            "temporal": temporal,
            "recurrence": recurrence,
        }
        score = (
            0.3 * type_score
            + 0.25 * participant
            + 0.15 * location
            + 0.2 * temporal
            + 0.1 * recurrence
        )
        if (
            left.get("recurrence_key")
            and right.get("recurrence_key")
            and not recurrence
        ):
            score = min(score, 0.79)
        return round(score, 6), parts

    def _event(self, row: Sequence[Any]) -> dict[str, Any]:
        return {
            "contract": CONTRACT,
            "event_id": row[0],
            "namespace": row[1],
            "event_type": row[2],
            "participants": _load(row[3], []),
            "location": _load(row[4], None),
            "time": {"start_ms": row[5], "end_ms": row[6]},
            "recurrence_key": row[7],
            "evidence": _load(row[8], []),
            "revision": int(row[9]),
            "status": row[10],
            "canonical_id": row[11],
            "created_at_ms": int(row[12]),
            "updated_at_ms": int(row[13]),
        }

    def list(self, namespace: str) -> list[dict[str, Any]]:
        return [
            self._event(row)
            for row in self.conn.execute(
                "SELECT * FROM canonical_events WHERE namespace=? ORDER BY event_id",
                [namespace],
            ).fetchall()
        ]

    def resolve_report(
        self,
        namespace: str,
        report: Mapping[str, Any],
        *,
        report_id: str,
        auto_link: bool = True,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        normalized = self.normalize(report)
        candidates = []
        prior = self.conn.execute(
            "SELECT report_json,event_id,confidence,alternatives_json FROM canonical_event_reports WHERE report_id=?",
            [report_id],
        ).fetchone()
        if prior:
            if prior[0] != _canonical(normalized):
                raise EventResolutionError(
                    "report_conflict",
                    "report id was reused with different event content",
                )
            return {
                "report_id": report_id,
                "event_id": prior[1],
                "confidence": float(prior[2]),
                "linked": True,
                "alternatives": _load(prior[3], []),
                "forced_merge": False,
                "idempotent": True,
            }
        for event in self.list(namespace):
            if event["status"] != "active":
                continue
            candidate = {
                key: event[key]
                for key in (
                    "event_type",
                    "participants",
                    "location",
                    "time",
                    "recurrence_key",
                    "evidence",
                )
            }
            score, parts = self.score(normalized, candidate)
            candidates.append(
                {"event_id": event["event_id"], "confidence": score, "factors": parts}
            )
        candidates.sort(key=lambda item: (-item["confidence"], item["event_id"]))
        chosen = (
            candidates[0]
            if candidates
            and candidates[0]["confidence"] >= self.threshold
            and auto_link
            else None
        )
        now = now_ms or int(time.time() * 1000)
        if chosen:
            event_id = chosen["event_id"]
        else:
            event_id = "event:" + _digest({"namespace": namespace, **normalized})[:24]
            self.conn.execute(
                "INSERT OR IGNORE INTO canonical_events VALUES (?,?,?,?,?,?,?,?,?,1,'active',NULL,?,?)",
                [
                    event_id,
                    namespace,
                    normalized["event_type"],
                    _canonical(normalized["participants"]),
                    _canonical(normalized["location"]),
                    normalized["time"]["start_ms"],
                    normalized["time"]["end_ms"],
                    normalized["recurrence_key"],
                    _canonical(normalized["evidence"]),
                    now,
                    now,
                ],
            )
        confidence = chosen["confidence"] if chosen else 1.0
        alternatives = [item for item in candidates if item["event_id"] != event_id][:5]
        self.conn.execute(
            "INSERT OR REPLACE INTO canonical_event_reports VALUES (?,?,?,?,?,?,?,?)",
            [
                report_id,
                namespace,
                event_id,
                _canonical(normalized),
                confidence,
                _canonical(alternatives),
                _canonical(normalized["evidence"]),
                now,
            ],
        )
        return {
            "report_id": report_id,
            "event_id": event_id,
            "confidence": confidence,
            "linked": bool(chosen),
            "alternatives": alternatives,
            "forced_merge": False,
        }

    def revise(
        self,
        event_id: str,
        patch: Mapping[str, Any],
        *,
        reason: str,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM canonical_events WHERE event_id=?", [event_id]
        ).fetchone()
        if not row:
            raise EventResolutionError("not_found", "event does not exist")
        before = self._event(row)
        candidate = {
            key: before[key]
            for key in (
                "event_type",
                "participants",
                "location",
                "time",
                "recurrence_key",
                "evidence",
            )
        }
        candidate.update(patch)
        normalized = self.normalize(candidate)
        now = now_ms or int(time.time() * 1000)
        self.conn.execute(
            "UPDATE canonical_events SET event_type=?,participants_json=?,location_json=?,start_ms=?,end_ms=?,recurrence_key=?,evidence_json=?,revision=revision+1,updated_at_ms=? WHERE event_id=?",
            [
                normalized["event_type"],
                _canonical(normalized["participants"]),
                _canonical(normalized["location"]),
                normalized["time"]["start_ms"],
                normalized["time"]["end_ms"],
                normalized["recurrence_key"],
                _canonical(normalized["evidence"]),
                now,
                event_id,
            ],
        )
        after = self._event(
            self.conn.execute(
                "SELECT * FROM canonical_events WHERE event_id=?", [event_id]
            ).fetchone()
        )
        return {"before": before, "after": after, "reason": reason}

    def merge(
        self, event_ids: Sequence[str], *, reason: str, now_ms: int | None = None
    ) -> dict[str, Any]:
        ids = sorted(set(event_ids))
        if len(ids) < 2:
            raise EventResolutionError(
                "insufficient_events", "merge requires at least two events"
            )
        rows = [
            self.conn.execute(
                "SELECT * FROM canonical_events WHERE event_id=?", [event_id]
            ).fetchone()
            for event_id in ids
        ]
        if any(row is None for row in rows):
            raise EventResolutionError("not_found", "a merge event does not exist")
        before = [self._event(row) for row in rows]
        namespace = before[0]["namespace"]
        if any(event["namespace"] != namespace for event in before):
            raise EventResolutionError(
                "namespace_mismatch", "events must share a namespace"
            )
        recurrence = {
            event["recurrence_key"] for event in before if event["recurrence_key"]
        }
        if len(recurrence) > 1:
            raise EventResolutionError(
                "recurrence_conflict",
                "different recurring occurrences cannot be merged",
            )
        combined = {
            "event_type": before[0]["event_type"],
            "participants": sorted(
                {v for event in before for v in event["participants"]}
            ),
            "location": before[0]["location"],
            "time": {
                "start_ms": min(
                    v for v in [e["time"]["start_ms"] for e in before] if v is not None
                )
                if any(e["time"]["start_ms"] is not None for e in before)
                else None,
                "end_ms": max(
                    v for v in [e["time"]["end_ms"] for e in before] if v is not None
                )
                if any(e["time"]["end_ms"] is not None for e in before)
                else None,
            },
            "recurrence_key": next(iter(recurrence), None),
            "evidence": [v for event in before for v in event["evidence"]],
        }
        now = now_ms or int(time.time() * 1000)
        canonical_id = "event:" + _digest({"namespace": namespace, **combined})[:24]
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO canonical_events VALUES (?,?,?,?,?,?,?,?,?,1,'active',NULL,?,?)",
                [
                    canonical_id,
                    namespace,
                    combined["event_type"],
                    _canonical(combined["participants"]),
                    _canonical(combined["location"]),
                    combined["time"]["start_ms"],
                    combined["time"]["end_ms"],
                    combined["recurrence_key"],
                    _canonical(combined["evidence"]),
                    now,
                    now,
                ],
            )
            for event_id in ids:
                self.conn.execute(
                    "UPDATE canonical_events SET status='merged',canonical_id=?,revision=revision+1,updated_at_ms=? WHERE event_id=?",
                    [canonical_id, now, event_id],
                )
            reports = {
                event_id: [
                    row[0]
                    for row in self.conn.execute(
                        "SELECT report_id FROM canonical_event_reports WHERE event_id=? ORDER BY report_id",
                        [event_id],
                    ).fetchall()
                ]
                for event_id in ids
            }
            self.conn.execute(
                "UPDATE canonical_event_reports SET event_id=? WHERE event_id IN (SELECT unnest(?))",
                [canonical_id, ids],
            )
            after = self._event(
                self.conn.execute(
                    "SELECT * FROM canonical_events WHERE event_id=?", [canonical_id]
                ).fetchone()
            )
            operation_id = (
                "event-operation:" + _digest(["merge", ids, canonical_id, now])[:24]
            )
            self.conn.execute(
                "INSERT INTO canonical_event_operations VALUES (?,?,'merge',?,?,'committed',?,NULL)",
                [
                    operation_id,
                    namespace,
                    _canonical({"events": before, "reports": reports}),
                    _canonical(after),
                    now,
                ],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "operation_id": operation_id,
            "canonical_event": after,
            "merged_event_ids": ids,
            "reversible": True,
            "reason": reason,
        }

    def reverse(
        self, operation_id: str, *, reason: str, now_ms: int | None = None
    ) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT namespace,action,before_json,after_json,status FROM canonical_event_operations WHERE operation_id=?",
            [operation_id],
        ).fetchone()
        if not row:
            raise EventResolutionError("not_found", "event operation does not exist")
        if row[4] != "committed":
            raise EventResolutionError(
                "already_reversed", "event operation was already reversed"
            )
        before_payload = _load(row[2], {})
        before = before_payload.get(
            "events", before_payload if isinstance(before_payload, list) else []
        )
        reports = (
            before_payload.get("reports", {})
            if isinstance(before_payload, dict)
            else {}
        )
        after = _load(row[3], {})
        now = now_ms or int(time.time() * 1000)
        self.conn.execute("BEGIN")
        try:
            for event in before:
                self.conn.execute(
                    "UPDATE canonical_events SET status=?,canonical_id=?,revision=revision+1,updated_at_ms=? WHERE event_id=?",
                    [event["status"], event["canonical_id"], now, event["event_id"]],
                )
            for event_id, report_ids in reports.items():
                if report_ids:
                    self.conn.execute(
                        "UPDATE canonical_event_reports SET event_id=? WHERE report_id IN (SELECT unnest(?))",
                        [event_id, report_ids],
                    )
            self.conn.execute(
                "UPDATE canonical_events SET status='split',revision=revision+1,updated_at_ms=? WHERE event_id=?",
                [now, after["event_id"]],
            )
            self.conn.execute(
                "UPDATE canonical_event_operations SET status='reversed',reversed_at_ms=? WHERE operation_id=?",
                [now, operation_id],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "operation_id": operation_id,
            "status": "reversed",
            "restored_event_ids": [event["event_id"] for event in before],
            "reason": reason,
        }


_MODEL_DDL = """
CREATE TABLE IF NOT EXISTS event_model_revisions (
  revision_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, namespace TEXT NOT NULL,
  revision BIGINT NOT NULL, predecessor_revision_id TEXT, event_type TEXT NOT NULL,
  lifecycle TEXT NOT NULL, granularity TEXT NOT NULL, attributes_json TEXT NOT NULL,
  generation BIGINT NOT NULL, valid_from_ms BIGINT, valid_to_ms BIGINT,
  observed_at_ms BIGINT NOT NULL, producer_json TEXT NOT NULL, policy_json TEXT NOT NULL,
  provenance_json TEXT NOT NULL, principal_id TEXT NOT NULL, input_hash TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, UNIQUE(event_id,revision)
);
CREATE TABLE IF NOT EXISTS event_model_current (
  event_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, revision BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_mentions (
  mention_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, event_id TEXT NOT NULL,
  document_revision_id TEXT NOT NULL, language TEXT NOT NULL, original_text TEXT NOT NULL,
  normalized_json TEXT NOT NULL, features_json TEXT NOT NULL, classifier_json TEXT NOT NULL,
  confidence DOUBLE NOT NULL, alternatives_json TEXT NOT NULL, lifecycle TEXT NOT NULL,
  principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_account_revisions (
  account_revision_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, event_id TEXT NOT NULL,
  namespace TEXT NOT NULL, revision BIGINT NOT NULL, attribute_type TEXT NOT NULL,
  value_json TEXT NOT NULL, role TEXT, confidence DOUBLE NOT NULL, uncertainty DOUBLE NOT NULL,
  evidence_json TEXT NOT NULL, valid_from_ms BIGINT, valid_to_ms BIGINT,
  observed_at_ms BIGINT NOT NULL, lifecycle TEXT NOT NULL, principal_id TEXT NOT NULL,
  input_hash TEXT NOT NULL, created_at_ms BIGINT NOT NULL, UNIQUE(account_id,revision)
);
CREATE TABLE IF NOT EXISTS event_account_current (
  account_id TEXT PRIMARY KEY, account_revision_id TEXT NOT NULL, revision BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_model_relations (
  relation_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, from_event_id TEXT NOT NULL,
  to_event_id TEXT NOT NULL, relation_type TEXT NOT NULL, evidence_json TEXT NOT NULL,
  confidence DOUBLE NOT NULL, valid_from_ms BIGINT, valid_to_ms BIGINT,
  principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_model_audit (
  audit_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, operation TEXT NOT NULL,
  object_id TEXT NOT NULL, principal_id TEXT NOT NULL, detail_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_model_search
  ON event_model_revisions(namespace,event_type,lifecycle,generation);
CREATE INDEX IF NOT EXISTS idx_event_mentions_event ON event_mentions(event_id,mention_id);
CREATE INDEX IF NOT EXISTS idx_event_accounts_event
  ON event_account_revisions(event_id,attribute_type,lifecycle);
"""

_LIFECYCLES = {"planned", "ongoing", "completed", "cancelled", "corrected"}
_GRANULARITIES = {"instant", "interval", "series", "process", "unknown"}
_ACCOUNT_TYPES = {
    "participant",
    "location",
    "time",
    "quantity",
    "cause",
    "consequence",
}
_RELATION_TYPES = {"predecessor", "successor", "recurrence", "cause", "consequence"}


def _require_event_scope(scopes: set[str], required: str) -> None:
    if required not in scopes and "operator" not in scopes:
        raise EventResolutionError("unauthorized", f"missing required scope {required}")


def _bounded_probability(value: Any, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise EventResolutionError(
            "invalid_confidence", f"{name} must be between 0 and 1"
        )
    return number


def _normalize_account_value(attribute_type: str, value: Any) -> Any:
    if attribute_type != "quantity" or not isinstance(value, Mapping):
        return value
    rendered = dict(value)
    amount = rendered.get("value")
    unit = str(rendered.get("unit") or "count").lower()
    conversions = {
        "km": (1000.0, "m"),
        "kilometer": (1000.0, "m"),
        "g": (0.001, "kg"),
        "gram": (0.001, "kg"),
        "percent": (0.01, "ratio"),
        "%": (0.01, "ratio"),
        "thousand": (1000.0, "count"),
        "million": (1000000.0, "count"),
    }
    try:
        number = float(amount)
    except (TypeError, ValueError) as exc:
        raise EventResolutionError(
            "invalid_quantity", "quantity value must be numeric"
        ) from exc
    multiplier, canonical_unit = conversions.get(unit, (1.0, unit))
    if not math.isfinite(number):
        raise EventResolutionError("invalid_quantity", "quantity value must be finite")
    return {
        **rendered,
        "value": number,
        "unit": unit,
        "normalized_value": number * multiplier,
        "normalized_unit": canonical_unit,
    }


def _event_cursor(binding: Mapping[str, Any], offset: int) -> str:
    raw = _canonical({"binding": binding, "offset": offset})
    return (
        base64.urlsafe_b64encode(f"{raw}.{_digest(raw)[:12]}".encode())
        .decode()
        .rstrip("=")
    )


def _read_event_cursor(binding: Mapping[str, Any], cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw, signature = base64.urlsafe_b64decode(padded).decode().rsplit(".", 1)
        payload = _load(raw, {})
        if signature != _digest(raw)[:12] or payload.get("binding") != dict(binding):
            raise ValueError
        return max(0, int(payload["offset"]))
    except (ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise EventResolutionError(
            "invalid_cursor", "event cursor does not match this search"
        ) from exc


class EventKnowledgeStore:
    """Immutable event history and accounts over the canonical event resolver."""

    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            self.resolver = EventResolver(conn)
            conn.execute(_MODEL_DDL)
        else:
            self.resolver = EventResolver(conn, initialize=False)

    def _audit(
        self,
        namespace: str,
        operation: str,
        object_id: str,
        principal_id: str,
        detail: Mapping[str, Any],
        now: int,
    ) -> None:
        audit_id = (
            "event-audit:"
            + _digest([namespace, operation, object_id, principal_id, detail, now])[:24]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO event_model_audit VALUES (?,?,?,?,?,?,?)",
            [
                audit_id,
                namespace,
                operation,
                object_id,
                principal_id,
                _canonical(detail),
                now,
            ],
        )

    def create(
        self,
        namespace: str,
        event: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
        event_key: str | None = None,
        lifecycle: str = "ongoing",
        granularity: str = "interval",
        generation: int = 0,
        observed_at_ms: int | None = None,
        producer: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require_event_scope(scopes, WRITE_SCOPE)
        normalized = self.resolver.normalize(event)
        if (
            lifecycle not in _LIFECYCLES
            or granularity not in _GRANULARITIES
            or generation < 0
        ):
            raise EventResolutionError(
                "invalid_event_context",
                "lifecycle, granularity, or generation is invalid",
            )
        key = event_key or _digest([namespace, normalized])
        event_id = "event:" + _digest([namespace, key])[:24]
        current = self.conn.execute(
            "SELECT revision_id FROM event_model_current WHERE event_id=?", [event_id]
        ).fetchone()
        if current:
            existing = self.get(namespace, event_id, scopes={READ_SCOPE})
            comparable = {
                key: existing[key]
                for key in (
                    "event_type",
                    "participants",
                    "location",
                    "time",
                    "recurrence_key",
                    "evidence",
                )
            }
            if comparable != normalized:
                raise EventResolutionError(
                    "event_conflict", "event key was reused with different content"
                )
            return {
                **existing,
                "idempotent": True,
            }
        now = self.now()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO canonical_events VALUES (?,?,?,?,?,?,?,?,?,1,'active',NULL,?,?)",
                [
                    event_id,
                    namespace,
                    normalized["event_type"],
                    _canonical(normalized["participants"]),
                    _canonical(normalized["location"]),
                    normalized["time"]["start_ms"],
                    normalized["time"]["end_ms"],
                    normalized["recurrence_key"],
                    _canonical(normalized["evidence"]),
                    now,
                    now,
                ],
            )
            revision_id = self._write_revision(
                event_id,
                namespace,
                1,
                None,
                normalized,
                lifecycle,
                granularity,
                generation=generation,
                observed_at_ms=int(
                    observed_at_ms if observed_at_ms is not None else now
                ),
                producer=dict(
                    producer or {"name": "noesis-event-model", "version": "2.0.0"}
                ),
                policy=dict(policy or {"clustering": "deterministic-v1"}),
                provenance=dict(provenance or {}),
                principal_id=principal_id,
                now=now,
            )
            self._audit(namespace, "create", revision_id, principal_id, {}, now)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get(namespace, event_id, scopes={READ_SCOPE})

    def _write_revision(
        self,
        event_id: str,
        namespace: str,
        revision: int,
        predecessor: str | None,
        normalized: Mapping[str, Any],
        lifecycle: str,
        granularity: str,
        *,
        generation: int,
        observed_at_ms: int,
        producer: Mapping[str, Any],
        policy: Mapping[str, Any],
        provenance: Mapping[str, Any],
        principal_id: str,
        now: int,
    ) -> str:
        attributes = {
            "participants": normalized["participants"],
            "location": normalized["location"],
            "time": normalized["time"],
            "recurrence_key": normalized["recurrence_key"],
            "evidence": normalized["evidence"],
        }
        stable = {
            "event_id": event_id,
            "revision": revision,
            "event_type": normalized["event_type"],
            "lifecycle": lifecycle,
            "granularity": granularity,
            "attributes": attributes,
            "generation": generation,
            "observed_at_ms": observed_at_ms,
            "producer": producer,
            "policy": policy,
            "provenance": provenance,
        }
        input_hash = _digest(stable)
        revision_id = "event-revision:" + input_hash[:24]
        self.conn.execute(
            "INSERT INTO event_model_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                revision_id,
                event_id,
                namespace,
                revision,
                predecessor,
                normalized["event_type"],
                lifecycle,
                granularity,
                _canonical(attributes),
                generation,
                normalized["time"]["start_ms"],
                normalized["time"]["end_ms"],
                observed_at_ms,
                _canonical(producer),
                _canonical(policy),
                _canonical(provenance),
                principal_id,
                input_hash,
                now,
            ],
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO event_model_current VALUES (?,?,?)",
            [event_id, revision_id, revision],
        )
        return revision_id

    def get(
        self,
        namespace: str,
        event_id: str,
        *,
        scopes: set[str],
        revision: int | None = None,
        include_history: bool = False,
    ) -> dict[str, Any] | None:
        _require_event_scope(scopes, READ_SCOPE)
        where = "r.namespace=? AND r.event_id=?"
        params: list[Any] = [namespace, event_id]
        if revision is not None:
            where += " AND r.revision=?"
            params.append(int(revision))
        rows = self.conn.execute(
            "SELECT revision_id,revision,predecessor_revision_id,event_type,lifecycle,granularity,"
            "attributes_json,generation,valid_from_ms,valid_to_ms,observed_at_ms,producer_json,"
            "policy_json,provenance_json,principal_id,input_hash,created_at_ms "
            f"FROM event_model_revisions r WHERE {where} "
            + (
                "ORDER BY revision"
                if include_history
                else "ORDER BY revision DESC LIMIT 1"
            ),
            params,
        ).fetchall()
        values = [self._event_row(namespace, event_id, row) for row in rows]
        if include_history:
            return {"namespace": namespace, "event_id": event_id, "revisions": values}
        return values[0] if values else None

    @staticmethod
    def _event_row(namespace: str, event_id: str, row: Sequence[Any]) -> dict[str, Any]:
        attributes = _load(row[6], {})
        return {
            "contract": EVENT_RECORD_CONTRACT,
            "event_id": event_id,
            "namespace": namespace,
            "revision_id": row[0],
            "revision": int(row[1]),
            "predecessor_revision_id": row[2],
            "event_type": row[3],
            "lifecycle": row[4],
            "granularity": row[5],
            "participants": attributes.get("participants", []),
            "location": attributes.get("location"),
            "time": attributes.get("time", {"start_ms": row[8], "end_ms": row[9]}),
            "recurrence_key": attributes.get("recurrence_key"),
            "evidence": attributes.get("evidence", []),
            "generation": int(row[7]),
            "valid_from_ms": row[8],
            "valid_to_ms": row[9],
            "observed_at_ms": int(row[10]),
            "producer": _load(row[11], {}),
            "policy": _load(row[12], {}),
            "provenance": _load(row[13], {}),
            "principal_id": row[14],
            "input_hash": row[15],
            "created_at_ms": int(row[16]),
        }

    def revise(
        self,
        namespace: str,
        event_id: str,
        expected_revision: int,
        patch: Mapping[str, Any],
        *,
        reason: str,
        principal_id: str,
        scopes: set[str],
        lifecycle: str | None = None,
    ) -> dict[str, Any]:
        _require_event_scope(scopes, WRITE_SCOPE)
        prior = self.get(namespace, event_id, scopes={READ_SCOPE})
        if not prior:
            raise EventResolutionError("not_found", "event does not exist in namespace")
        if prior["revision"] != int(expected_revision):
            raise EventResolutionError("revision_conflict", "event revision changed")
        if len(reason.strip()) < 10:
            raise EventResolutionError(
                "invalid_revision", "a substantive correction reason is required"
            )
        candidate = {
            key: prior[key]
            for key in (
                "event_type",
                "participants",
                "location",
                "time",
                "recurrence_key",
                "evidence",
            )
        }
        candidate.update(dict(patch))
        normalized = self.resolver.normalize(candidate)
        next_lifecycle = lifecycle or prior["lifecycle"]
        if next_lifecycle not in _LIFECYCLES:
            raise EventResolutionError(
                "invalid_lifecycle", "event lifecycle is unsupported"
            )
        if _digest([normalized, next_lifecycle]) == _digest(
            [{key: prior[key] for key in candidate}, prior["lifecycle"]]
        ):
            return {**prior, "idempotent": True}
        now = self.now()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "UPDATE canonical_events SET event_type=?,participants_json=?,location_json=?,start_ms=?,"
                "end_ms=?,recurrence_key=?,evidence_json=?,revision=revision+1,updated_at_ms=? WHERE event_id=?",
                [
                    normalized["event_type"],
                    _canonical(normalized["participants"]),
                    _canonical(normalized["location"]),
                    normalized["time"]["start_ms"],
                    normalized["time"]["end_ms"],
                    normalized["recurrence_key"],
                    _canonical(normalized["evidence"]),
                    now,
                    event_id,
                ],
            )
            revision_id = self._write_revision(
                event_id,
                namespace,
                prior["revision"] + 1,
                prior["revision_id"],
                normalized,
                next_lifecycle,
                prior["granularity"],
                generation=prior["generation"],
                observed_at_ms=now,
                producer=prior["producer"],
                policy=prior["policy"],
                provenance={**prior["provenance"], "correction_reason": reason},
                principal_id=principal_id,
                now=now,
            )
            self._audit(
                namespace, "revise", revision_id, principal_id, {"reason": reason}, now
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get(namespace, event_id, scopes={READ_SCOPE})

    def as_of(
        self, namespace: str, event_id: str, as_of_ms: int, *, scopes: set[str]
    ) -> dict[str, Any] | None:
        _require_event_scope(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT revision FROM event_model_revisions WHERE namespace=? AND event_id=? "
            "AND observed_at_ms<=? ORDER BY observed_at_ms DESC,revision DESC LIMIT 1",
            [namespace, event_id, int(as_of_ms)],
        ).fetchone()
        return (
            self.get(namespace, event_id, scopes=scopes, revision=int(row[0]))
            if row
            else None
        )

    def ingest_mentions(
        self,
        namespace: str,
        document_revision_id: str,
        mentions: Sequence[Mapping[str, Any]],
        *,
        language: str,
        principal_id: str,
        scopes: set[str],
        classifier=None,
        classifier_pin: Mapping[str, Any] | None = None,
        max_mentions: int = 100,
        cancel_requested: bool = False,
    ) -> dict[str, Any]:
        _require_event_scope(scopes, WRITE_SCOPE)
        if classifier is not None and not all(
            (classifier_pin or {}).get(key) for key in ("name", "version", "revision")
        ):
            raise EventResolutionError(
                "unpinned_classifier",
                "classifier name, version, and revision are required",
            )
        if cancel_requested:
            return {"status": "cancelled", "processed": 0, "items": []}
        bounded = min(max(int(max_mentions), 1), 100)
        items = []
        for index, raw in enumerate(mentions[:bounded]):
            mention = dict(raw)
            original_text = str(mention.pop("text", ""))
            normalized = self.resolver.normalize(mention)
            model = dict(classifier(normalized) if classifier else {})
            model_event_id = model.get("event_id")
            candidates = []
            for event in self.search(namespace, scopes={READ_SCOPE}, limit=100)[
                "items"
            ]:
                score, factors = self.resolver.score(normalized, event)
                candidates.append(
                    {
                        "event_id": event["event_id"],
                        "confidence": score,
                        "factors": factors,
                    }
                )
            candidates.sort(key=lambda item: (-item["confidence"], item["event_id"]))
            chosen = model_event_id or (
                candidates[0]["event_id"]
                if candidates and candidates[0]["confidence"] >= self.resolver.threshold
                else None
            )
            if chosen and not self.get(namespace, chosen, scopes={READ_SCOPE}):
                raise EventResolutionError(
                    "invalid_classification", "classifier selected an unknown event"
                )
            event = (
                self.get(namespace, chosen, scopes={READ_SCOPE})
                if chosen
                else self.create(
                    namespace,
                    normalized,
                    principal_id=principal_id,
                    scopes=scopes,
                    event_key=_digest([document_revision_id, index, normalized]),
                    producer={"name": "event-mention-extractor", "version": "1.0.0"},
                    provenance={"document_revision_id": document_revision_id},
                )
            )
            confidence = float(
                model.get(
                    "confidence",
                    candidates[0]["confidence"] if chosen and candidates else 1.0,
                )
            )
            confidence = _bounded_probability(confidence, "confidence")
            mention_id = (
                "event-mention:"
                + _digest([document_revision_id, index, normalized])[:24]
            )
            classifier_value = (
                {"kind": "model", **dict(classifier_pin or {})}
                if classifier
                else {"kind": "rules", "name": "event-similarity", "version": "1.0.0"}
            )
            now = self.now()
            exists = self.conn.execute(
                "SELECT mention_id FROM event_mentions WHERE mention_id=?", [mention_id]
            ).fetchone()
            if not exists:
                self.conn.execute("BEGIN")
                try:
                    self.conn.execute(
                        "INSERT INTO event_mentions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [
                            mention_id,
                            namespace,
                            event["event_id"],
                            document_revision_id,
                            language,
                            original_text,
                            _canonical(normalized),
                            _canonical(candidates[0]["factors"] if candidates else {}),
                            _canonical(classifier_value),
                            confidence,
                            _canonical(candidates[:5]),
                            "active",
                            principal_id,
                            now,
                        ],
                    )
                    self._audit(
                        namespace,
                        "ingest-mention",
                        mention_id,
                        principal_id,
                        {
                            "event_id": event["event_id"],
                            "document_revision_id": document_revision_id,
                        },
                        now,
                    )
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise
            items.append(
                {
                    "contract": EVENT_MENTION_CONTRACT,
                    "mention_id": mention_id,
                    "event_id": event["event_id"],
                    "document_revision_id": document_revision_id,
                    "language": language,
                    "original_text": original_text,
                    "normalized": normalized,
                    "features": candidates[0]["factors"] if candidates else {},
                    "classifier": classifier_value,
                    "confidence": confidence,
                    "alternatives": candidates[:5],
                    "lifecycle": "active",
                }
            )
        return {
            "status": "complete",
            "processed": len(items),
            "items": items,
            "truncated": len(mentions) > bounded,
        }

    def attach_account(
        self,
        namespace: str,
        event_id: str,
        attribute_type: str,
        value: Any,
        *,
        principal_id: str,
        scopes: set[str],
        role: str | None = None,
        confidence: float = 1.0,
        uncertainty: float = 0.0,
        evidence: Sequence[Mapping[str, Any]] = (),
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        observed_at_ms: int | None = None,
    ) -> dict[str, Any]:
        _require_event_scope(scopes, WRITE_SCOPE)
        if attribute_type not in _ACCOUNT_TYPES or not self.get(
            namespace, event_id, scopes={READ_SCOPE}
        ):
            raise EventResolutionError(
                "invalid_account", "event and supported attribute type are required"
            )
        if (
            valid_from_ms is not None
            and valid_to_ms is not None
            and valid_to_ms < valid_from_ms
        ):
            raise EventResolutionError(
                "invalid_interval", "account interval ends before it starts"
            )
        confidence_value = _bounded_probability(confidence, "confidence")
        uncertainty_value = _bounded_probability(uncertainty, "uncertainty")
        value = _normalize_account_value(attribute_type, value)
        account_id = (
            "event-account:" + _digest([event_id, attribute_type, role, value])[:24]
        )
        current = self.conn.execute(
            "SELECT c.revision,r.input_hash FROM event_account_current c "
            "JOIN event_account_revisions r USING(account_revision_id) WHERE c.account_id=?",
            [account_id],
        ).fetchone()
        now = self.now()
        current_value = self._account(account_id) if current else None
        payload = {
            "attribute_type": attribute_type,
            "value": value,
            "role": role,
            "confidence": confidence_value,
            "uncertainty": uncertainty_value,
            "evidence": [dict(item) for item in evidence],
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "observed_at_ms": int(
                observed_at_ms
                if observed_at_ms is not None
                else current_value["observed_at_ms"]
                if current_value
                else now
            ),
            "lifecycle": "active",
        }
        input_hash = _digest(payload)
        if current and current[1] == input_hash:
            return {**self._account(account_id), "idempotent": True}
        revision = int(current[0]) + 1 if current else 1
        revision_id = (
            "event-account-revision:" + _digest([account_id, revision, input_hash])[:24]
        )
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO event_account_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    revision_id,
                    account_id,
                    event_id,
                    namespace,
                    revision,
                    attribute_type,
                    _canonical(value),
                    role,
                    confidence_value,
                    uncertainty_value,
                    _canonical(payload["evidence"]),
                    valid_from_ms,
                    valid_to_ms,
                    payload["observed_at_ms"],
                    "active",
                    principal_id,
                    input_hash,
                    now,
                ],
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO event_account_current VALUES (?,?,?)",
                [account_id, revision_id, revision],
            )
            self._audit(
                namespace,
                "attach-account",
                revision_id,
                principal_id,
                {"attribute_type": attribute_type},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self._account(account_id)

    def _account(self, account_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT account_revision_id,event_id,namespace,revision,attribute_type,value_json,role,"
            "confidence,uncertainty,evidence_json,valid_from_ms,valid_to_ms,observed_at_ms,lifecycle,"
            "principal_id,input_hash,created_at_ms FROM event_account_revisions "
            "WHERE account_id=? ORDER BY revision DESC LIMIT 1",
            [account_id],
        ).fetchone()
        if not row:
            raise EventResolutionError("not_found", "event account does not exist")
        return self._account_row(account_id, row)

    @staticmethod
    def _account_row(account_id: str, row: Sequence[Any]) -> dict[str, Any]:
        return {
            "contract": EVENT_ACCOUNT_CONTRACT,
            "account_id": account_id,
            "account_revision_id": row[0],
            "event_id": row[1],
            "namespace": row[2],
            "revision": int(row[3]),
            "attribute_type": row[4],
            "value": _load(row[5], None),
            "role": row[6],
            "confidence": float(row[7]),
            "uncertainty": float(row[8]),
            "evidence": _load(row[9], []),
            "valid_from_ms": row[10],
            "valid_to_ms": row[11],
            "observed_at_ms": int(row[12]),
            "lifecycle": row[13],
            "principal_id": row[14],
            "input_hash": row[15],
            "created_at_ms": int(row[16]),
        }

    def retract_account(
        self,
        namespace: str,
        account_id: str,
        reason: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _require_event_scope(scopes, REVIEW_SCOPE)
        prior = self._account(account_id)
        if prior["namespace"] != namespace or len(reason.strip()) < 10:
            raise EventResolutionError(
                "invalid_retraction", "matching namespace and reason are required"
            )
        if prior["lifecycle"] == "retracted":
            return {**prior, "idempotent": True}
        now, revision = self.now(), prior["revision"] + 1
        evidence = [*prior["evidence"], {"kind": "retraction", "reason": reason}]
        input_hash = _digest([prior["input_hash"], "retracted", reason])
        revision_id = (
            "event-account-revision:" + _digest([account_id, revision, input_hash])[:24]
        )
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO event_account_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    revision_id,
                    account_id,
                    prior["event_id"],
                    namespace,
                    revision,
                    prior["attribute_type"],
                    _canonical(prior["value"]),
                    prior["role"],
                    prior["confidence"],
                    prior["uncertainty"],
                    _canonical(evidence),
                    prior["valid_from_ms"],
                    prior["valid_to_ms"],
                    prior["observed_at_ms"],
                    "retracted",
                    principal_id,
                    input_hash,
                    now,
                ],
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO event_account_current VALUES (?,?,?)",
                [account_id, revision_id, revision],
            )
            self._audit(
                namespace,
                "retract-account",
                revision_id,
                principal_id,
                {"reason": reason},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self._account(account_id)

    def accounts(
        self,
        namespace: str,
        event_id: str,
        *,
        scopes: set[str],
        include_retracted: bool = False,
        include_history: bool = False,
    ) -> list[dict[str, Any]]:
        _require_event_scope(scopes, READ_SCOPE)
        rows = self.conn.execute(
            (
                "SELECT r.account_id FROM event_account_revisions r "
                if include_history
                else "SELECT c.account_id FROM event_account_current c JOIN event_account_revisions r USING(account_revision_id) "
            )
            + "WHERE r.namespace=? AND r.event_id=? ORDER BY r.account_id,r.revision",
            [namespace, event_id],
        ).fetchall()
        if include_history:
            values = []
            for account_id, revision in self.conn.execute(
                "SELECT account_id,revision FROM event_account_revisions WHERE namespace=? "
                "AND event_id=? ORDER BY account_id,revision",
                [namespace, event_id],
            ).fetchall():
                row = self.conn.execute(
                    "SELECT account_revision_id,event_id,namespace,revision,attribute_type,value_json,role,"
                    "confidence,uncertainty,evidence_json,valid_from_ms,valid_to_ms,observed_at_ms,lifecycle,"
                    "principal_id,input_hash,created_at_ms FROM event_account_revisions "
                    "WHERE account_id=? AND revision=?",
                    [account_id, revision],
                ).fetchone()
                values.append(self._account_row(account_id, row))
        else:
            values = [self._account(row[0]) for row in rows]
        return [
            item
            for item in values
            if include_retracted or item["lifecycle"] == "active"
        ]

    def relate(
        self,
        namespace: str,
        from_event_id: str,
        to_event_id: str,
        relation_type: str,
        *,
        principal_id: str,
        scopes: set[str],
        evidence: Sequence[Mapping[str, Any]] = (),
        confidence: float = 1.0,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
    ) -> dict[str, Any]:
        _require_event_scope(scopes, WRITE_SCOPE)
        if relation_type not in _RELATION_TYPES or from_event_id == to_event_id:
            raise EventResolutionError(
                "invalid_relation",
                "supported relation and distinct events are required",
            )
        for event_id in (from_event_id, to_event_id):
            row = self.conn.execute(
                "SELECT namespace FROM event_model_revisions WHERE event_id=? ORDER BY revision DESC LIMIT 1",
                [event_id],
            ).fetchone()
            if not row:
                raise EventResolutionError("not_found", "related event does not exist")
        confidence_value = _bounded_probability(confidence, "confidence")
        stable = [
            namespace,
            from_event_id,
            to_event_id,
            relation_type,
            evidence,
            confidence_value,
            valid_from_ms,
            valid_to_ms,
        ]
        relation_id = "event-relation:" + _digest(stable)[:24]
        existing = self.conn.execute(
            "SELECT relation_id FROM event_model_relations WHERE relation_id=?",
            [relation_id],
        ).fetchone()
        if existing:
            return {**self._relation(relation_id), "idempotent": True}
        now = self.now()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT OR IGNORE INTO event_model_relations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    relation_id,
                    namespace,
                    from_event_id,
                    to_event_id,
                    relation_type,
                    _canonical([dict(item) for item in evidence]),
                    confidence_value,
                    valid_from_ms,
                    valid_to_ms,
                    principal_id,
                    now,
                ],
            )
            self._audit(
                namespace,
                "relate",
                relation_id,
                principal_id,
                {"relation_type": relation_type},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self._relation(relation_id)

    def _relation(self, relation_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT namespace,from_event_id,to_event_id,relation_type,evidence_json,confidence,"
            "valid_from_ms,valid_to_ms,principal_id,created_at_ms FROM event_model_relations "
            "WHERE relation_id=?",
            [relation_id],
        ).fetchone()
        if not row:
            raise EventResolutionError("not_found", "event relation does not exist")
        return {
            "contract": EVENT_RELATION_CONTRACT,
            "relation_id": relation_id,
            "namespace": row[0],
            "from_event_id": row[1],
            "to_event_id": row[2],
            "relation_type": row[3],
            "evidence": _load(row[4], []),
            "confidence": float(row[5]),
            "valid_from_ms": row[6],
            "valid_to_ms": row[7],
            "principal_id": row[8],
            "created_at_ms": int(row[9]),
        }

    def search(
        self,
        namespace: str,
        *,
        scopes: set[str],
        event_types: Sequence[str] = (),
        lifecycles: Sequence[str] = (),
        query: str | None = None,
        snapshot_generation: int | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        _require_event_scope(scopes, READ_SCOPE)
        binding = {
            "namespace": namespace,
            "event_types": sorted(event_types),
            "lifecycles": sorted(lifecycles),
            "query": query or "",
            "snapshot_generation": snapshot_generation,
        }
        offset, bounded = (
            _read_event_cursor(binding, cursor),
            min(max(int(limit), 1), 100),
        )
        rows = self.conn.execute(
            "SELECT c.event_id FROM event_model_current c JOIN event_model_revisions r USING(revision_id) "
            "WHERE r.namespace=? AND (? IS NULL OR r.generation<=?) ORDER BY c.event_id",
            [namespace, snapshot_generation, snapshot_generation],
        ).fetchall()
        values = [self.get(namespace, row[0], scopes=scopes) for row in rows]
        needle = (query or "").casefold()
        values = [
            item
            for item in values
            if item
            and (not event_types or item["event_type"] in event_types)
            and (not lifecycles or item["lifecycle"] in lifecycles)
            and (not needle or needle in _canonical(item).casefold())
        ]
        page = values[offset : offset + bounded]
        return {
            "contract": EVENT_SEARCH_CONTRACT,
            "items": page,
            "next_cursor": _event_cursor(binding, offset + bounded)
            if offset + bounded < len(values)
            else None,
            "snapshot_generation": snapshot_generation,
            "total": len(values),
        }

    def timeline(
        self,
        namespace: str,
        *,
        scopes: set[str],
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        values = self.search(namespace, scopes=scopes, limit=min(max(limit, 1), 100))[
            "items"
        ]
        return sorted(
            [
                item
                for item in values
                if (
                    start_ms is None
                    or item["valid_to_ms"] is None
                    or item["valid_to_ms"] >= start_ms
                )
                and (
                    end_ms is None
                    or item["valid_from_ms"] is None
                    or item["valid_from_ms"] <= end_ms
                )
            ],
            key=lambda item: (
                item["valid_from_ms"] is None,
                item["valid_from_ms"] or 0,
                item["event_id"],
            ),
        )

    def neighborhood(
        self,
        event_id: str,
        *,
        scopes: set[str],
        max_depth: int = 2,
    ) -> dict[str, Any]:
        _require_event_scope(scopes, READ_SCOPE)
        depth = min(max(int(max_depth), 1), 6)
        rows = self.conn.execute(
            "SELECT relation_id,namespace,from_event_id,to_event_id,relation_type,evidence_json,confidence "
            "FROM event_model_relations ORDER BY relation_id"
        ).fetchall()
        graph: dict[str, list[tuple[str, Sequence[Any]]]] = defaultdict(list)
        for row in rows:
            graph[row[2]].append((row[3], row))
            graph[row[3]].append((row[2], row))
        queue, seen, relations = deque([(event_id, 0)]), {event_id}, {}
        while queue:
            node, level = queue.popleft()
            if level >= depth:
                continue
            for neighbor, row in graph[node]:
                relations[row[0]] = {
                    "relation_id": row[0],
                    "namespace": row[1],
                    "from_event_id": row[2],
                    "to_event_id": row[3],
                    "relation_type": row[4],
                    "evidence": _load(row[5], []),
                    "confidence": float(row[6]),
                }
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append((neighbor, level + 1))
        return {
            "event_id": event_id,
            "event_ids": sorted(seen),
            "relations": [relations[key] for key in sorted(relations)],
            "max_depth": depth,
        }

    def diff(
        self,
        namespace: str,
        event_id: str,
        from_revision: int,
        to_revision: int,
        *,
        scopes: set[str],
    ) -> dict[str, Any]:
        left = self.get(namespace, event_id, scopes=scopes, revision=from_revision)
        right = self.get(namespace, event_id, scopes=scopes, revision=to_revision)
        if not left or not right:
            raise EventResolutionError("not_found", "event revision does not exist")
        ignored = {
            "revision",
            "revision_id",
            "predecessor_revision_id",
            "created_at_ms",
            "principal_id",
            "input_hash",
        }
        changes = {
            key: {"before": left.get(key), "after": right.get(key)}
            for key in sorted(set(left) | set(right))
            if key not in ignored and left.get(key) != right.get(key)
        }
        return {
            "event_id": event_id,
            "from_revision": from_revision,
            "to_revision": to_revision,
            "changes": changes,
            "diff_hash": _digest([event_id, from_revision, to_revision, changes]),
        }

    def replay(
        self, namespace: str, event_id: str, *, scopes: set[str]
    ) -> dict[str, Any]:
        history = self.get(namespace, event_id, scopes=scopes, include_history=True)
        if not history or not history["revisions"]:
            raise EventResolutionError("not_found", "event does not exist in namespace")
        revisions = history["revisions"]
        chain_valid = all(
            revision["predecessor_revision_id"]
            == (revisions[index - 1]["revision_id"] if index else None)
            for index, revision in enumerate(revisions)
        )
        replay_hash = _digest(
            [
                {
                    key: value
                    for key, value in revision.items()
                    if key not in {"created_at_ms", "principal_id"}
                }
                for revision in revisions
            ]
        )
        return {
            "event_id": event_id,
            "revision_count": len(revisions),
            "current_revision_id": revisions[-1]["revision_id"],
            "chain_valid": chain_valid,
            "replay_hash": replay_hash,
            "deterministic": chain_valid,
        }
