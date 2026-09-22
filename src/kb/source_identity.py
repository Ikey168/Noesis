"""Canonical source identity, ownership history, and independence explanations."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

IDENTITY_CONTRACT = "noesis-source-identity-v1"
ALIAS_CONTRACT = "noesis-source-alias-decision-v1"
RELATIONSHIP_CONTRACT = "noesis-source-relationship-v1"
DOSSIER_CONTRACT = "noesis-source-dossier-v1"
INDEPENDENCE_CONTRACT = "noesis-source-independence-v1"
READ_SCOPE = "knowledge:source-identity:read"
WRITE_SCOPE = "knowledge:source-identity:write"
REVIEW_SCOPE = "knowledge:source-identity:review"
KINDS = (
    "publication",
    "organization",
    "agency",
    "author",
    "channel",
    "account",
    "unknown",
)
RELATIONSHIP_TYPES = (
    "ownership",
    "funding",
    "editorial-control",
    "state-affiliation",
    "syndication",
    "authorship",
    "reporting-origin",
)
INDEPENDENCE_EDGES = frozenset(
    {"ownership", "editorial-control", "syndication", "authorship", "reporting-origin"}
)

_DDL = """
CREATE TABLE IF NOT EXISTS source_identities (
  source_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, kind TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, idempotency_key TEXT NOT NULL,
  UNIQUE(namespace,idempotency_key)
);
CREATE TABLE IF NOT EXISTS source_identity_revisions (
  revision_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, namespace TEXT NOT NULL,
  revision BIGINT NOT NULL, predecessor_revision_id TEXT, display_name TEXT NOT NULL,
  lifecycle TEXT NOT NULL, native_ids_json TEXT NOT NULL, names_json TEXT NOT NULL,
  generation BIGINT NOT NULL, valid_from_ms BIGINT, valid_to_ms BIGINT,
  observed_at_ms BIGINT NOT NULL, producer_json TEXT NOT NULL, policy_json TEXT NOT NULL,
  principal_id TEXT NOT NULL, input_hash TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(source_id,revision)
);
CREATE TABLE IF NOT EXISTS source_identity_current (
  source_id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, revision BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_alias_decisions (
  decision_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, alias_type TEXT NOT NULL,
  normalized_alias TEXT NOT NULL, language TEXT NOT NULL, source_id TEXT NOT NULL,
  action TEXT NOT NULL, confidence DOUBLE NOT NULL, reason TEXT NOT NULL,
  provenance_json TEXT NOT NULL, reviewer_id TEXT NOT NULL, predecessor_decision_id TEXT,
  created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_relationship_revisions (
  relationship_revision_id TEXT PRIMARY KEY, relationship_id TEXT NOT NULL,
  namespace TEXT NOT NULL, revision BIGINT NOT NULL, from_source_id TEXT NOT NULL,
  to_source_id TEXT NOT NULL, relationship_type TEXT NOT NULL, lifecycle TEXT NOT NULL,
  valid_from_ms BIGINT, valid_to_ms BIGINT, observed_at_ms BIGINT NOT NULL,
  confidence DOUBLE NOT NULL, uncertainty DOUBLE NOT NULL, evidence_json TEXT NOT NULL,
  producer_json TEXT NOT NULL, policy_json TEXT NOT NULL, principal_id TEXT NOT NULL,
  input_hash TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(relationship_id,revision)
);
CREATE TABLE IF NOT EXISTS source_relationship_current (
  relationship_id TEXT PRIMARY KEY, relationship_revision_id TEXT NOT NULL,
  revision BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_identity_audit (
  audit_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, operation TEXT NOT NULL,
  object_id TEXT NOT NULL, principal_id TEXT NOT NULL, detail_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_source_alias
  ON source_alias_decisions(namespace,alias_type,normalized_alias,language,created_at_ms);
CREATE INDEX IF NOT EXISTS idx_source_relationship
  ON source_relationship_revisions(namespace,from_source_id,to_source_id,relationship_type);
"""


class SourceIdentityError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return (
        default
        if value is None
        else json.loads(value)
        if isinstance(value, str)
        else value
    )


def _require(scopes: set[str], required: str) -> None:
    if required not in scopes and "operator" not in scopes:
        raise SourceIdentityError("unauthorized", f"missing required scope {required}")


def _bounded(value: Any, name: str) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise SourceIdentityError(
            "invalid_confidence", f"{name} must be between 0 and 1"
        )
    return number


def normalize_alias(alias_type: str, value: str) -> str:
    raw = " ".join(str(value).strip().split())
    if not raw:
        raise SourceIdentityError("invalid_alias", "alias value is required")
    if alias_type == "url":
        parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
        host = (parsed.hostname or "").lower().removeprefix("www.")
        path = parsed.path.rstrip("/") or "/"
        return urlunsplit(("https", host, path, "", ""))
    if alias_type == "domain":
        return (
            (urlsplit(raw if "://" in raw else f"https://{raw}").hostname or "")
            .lower()
            .removeprefix("www.")
        )
    if alias_type == "handle":
        return "@" + raw.lower().lstrip("@")
    if alias_type in {"identifier", "name"}:
        return raw.casefold()
    raise SourceIdentityError("invalid_alias", "alias type is unsupported")


def _encode_cursor(source_id: str, offset: int) -> str:
    payload = f"{source_id}:{offset}"
    return (
        base64.urlsafe_b64encode(f"{payload}:{_digest(payload)[:12]}".encode())
        .decode()
        .rstrip("=")
    )


def _decode_cursor(source_id: str, cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded).decode()
        owner, offset, signature = raw.rsplit(":", 2)
        payload = f"{owner}:{offset}"
        if owner != source_id or signature != _digest(payload)[:12]:
            raise ValueError
        return max(0, int(offset))
    except (ValueError, UnicodeError) as exc:
        raise SourceIdentityError(
            "invalid_cursor", "dossier cursor is invalid"
        ) from exc


class SourceIdentityStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

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
            "source-audit:"
            + _digest([namespace, operation, object_id, principal_id, detail, now])[:24]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO source_identity_audit VALUES (?,?,?,?,?,?,?)",
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

    def register(
        self,
        namespace: str,
        kind: str,
        display_name: str,
        *,
        principal_id: str,
        scopes: set[str],
        native_ids: Mapping[str, str] | None = None,
        names: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        generation: int = 0,
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        observed_at_ms: int | None = None,
        producer: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        if (
            kind not in KINDS
            or not namespace
            or not display_name.strip()
            or generation < 0
        ):
            raise SourceIdentityError(
                "invalid_identity",
                "valid namespace, kind, name, and generation are required",
            )
        if (
            valid_from_ms is not None
            and valid_to_ms is not None
            and valid_to_ms < valid_from_ms
        ):
            raise SourceIdentityError(
                "invalid_temporality", "valid-time interval is reversed"
            )
        native = {
            str(key): str(value) for key, value in sorted((native_ids or {}).items())
        }
        key = idempotency_key or _digest([kind, native or display_name.casefold()])
        existing = self.conn.execute(
            "SELECT source_id FROM source_identities WHERE namespace=? AND idempotency_key=?",
            [namespace, key],
        ).fetchone()
        if existing:
            return {
                **self.get(namespace, existing[0], scopes={READ_SCOPE}),
                "idempotent": True,
            }
        source_id = "source-identity:" + _digest([namespace, kind, key])[:24]
        now = self.now()
        context = {
            "generation": int(generation),
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "observed_at_ms": int(
                observed_at_ms if observed_at_ms is not None else now
            ),
            "producer": dict(
                producer or {"name": "noesis-source-identity", "version": "1.0.0"}
            ),
            "policy": dict(policy or {"resolution": "reviewable-v1"}),
        }
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO source_identities VALUES (?,?,?,?,?,?)",
                [source_id, namespace, kind, principal_id, now, key],
            )
            revision_id = self._write_identity(
                source_id,
                namespace,
                1,
                None,
                display_name.strip(),
                "active",
                native,
                dict(names or {}),
                context,
                principal_id,
                now,
            )
            self._audit(
                namespace, "register", revision_id, principal_id, {"kind": kind}, now
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get(namespace, source_id, scopes={READ_SCOPE})

    def _write_identity(
        self,
        source_id: str,
        namespace: str,
        revision: int,
        predecessor: str | None,
        display_name: str,
        lifecycle: str,
        native_ids: Mapping[str, str],
        names: Mapping[str, str],
        context: Mapping[str, Any],
        principal_id: str,
        now: int,
    ) -> str:
        stable = {
            "source_id": source_id,
            "revision": revision,
            "display_name": display_name,
            "lifecycle": lifecycle,
            "native_ids": native_ids,
            "names": names,
            **context,
        }
        input_hash = _digest(stable)
        revision_id = "source-identity-revision:" + input_hash[:24]
        self.conn.execute(
            "INSERT INTO source_identity_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                revision_id,
                source_id,
                namespace,
                revision,
                predecessor,
                display_name,
                lifecycle,
                _canonical(native_ids),
                _canonical(names),
                context["generation"],
                context["valid_from_ms"],
                context["valid_to_ms"],
                context["observed_at_ms"],
                _canonical(context["producer"]),
                _canonical(context["policy"]),
                principal_id,
                input_hash,
                now,
            ],
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO source_identity_current VALUES (?,?,?)",
            [source_id, revision_id, revision],
        )
        return revision_id

    def get(
        self,
        namespace: str,
        source_id: str,
        *,
        scopes: set[str],
        include_history: bool = False,
    ) -> dict[str, Any] | None:
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT r.revision_id,r.revision,r.predecessor_revision_id,r.display_name,r.lifecycle,"
            "r.native_ids_json,r.names_json,r.generation,r.valid_from_ms,r.valid_to_ms,r.observed_at_ms,"
            "r.producer_json,r.policy_json,r.principal_id,r.input_hash,r.created_at_ms,i.kind "
            "FROM source_identity_revisions r JOIN source_identities i USING(source_id) "
            "WHERE r.namespace=? AND r.source_id=? "
            + (
                "ORDER BY r.revision"
                if include_history
                else "ORDER BY r.revision DESC LIMIT 1"
            ),
            [namespace, source_id],
        ).fetchall()
        values = [self._identity_row(namespace, source_id, row) for row in rows]
        if include_history:
            return {"namespace": namespace, "source_id": source_id, "revisions": values}
        return values[0] if values else None

    @staticmethod
    def _identity_row(
        namespace: str, source_id: str, row: Sequence[Any]
    ) -> dict[str, Any]:
        return {
            "contract": IDENTITY_CONTRACT,
            "source_id": source_id,
            "namespace": namespace,
            "revision_id": row[0],
            "revision": int(row[1]),
            "predecessor_revision_id": row[2],
            "display_name": row[3],
            "lifecycle": row[4],
            "native_ids": _load(row[5], {}),
            "names": _load(row[6], {}),
            "generation": int(row[7]),
            "valid_from_ms": row[8],
            "valid_to_ms": row[9],
            "observed_at_ms": int(row[10]),
            "producer": _load(row[11], {}),
            "policy": _load(row[12], {}),
            "principal_id": row[13],
            "input_hash": row[14],
            "created_at_ms": int(row[15]),
            "kind": row[16],
        }

    def revise(
        self,
        namespace: str,
        source_id: str,
        expected_revision: int,
        *,
        principal_id: str,
        scopes: set[str],
        display_name: str | None = None,
        native_ids: Mapping[str, str] | None = None,
        names: Mapping[str, str] | None = None,
        lifecycle: str | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        prior = self.get(namespace, source_id, scopes={READ_SCOPE})
        if not prior:
            raise SourceIdentityError(
                "not_found", "source identity does not exist in namespace"
            )
        if prior["revision"] != expected_revision:
            raise SourceIdentityError(
                "revision_conflict", "source identity revision changed"
            )
        next_lifecycle = lifecycle or prior["lifecycle"]
        if next_lifecycle not in {"active", "deleted"}:
            raise SourceIdentityError(
                "invalid_lifecycle", "source lifecycle is unsupported"
            )
        context = {
            key: prior[key]
            for key in (
                "generation",
                "valid_from_ms",
                "valid_to_ms",
                "observed_at_ms",
                "producer",
                "policy",
            )
        }
        candidate = {
            "display_name": display_name or prior["display_name"],
            "lifecycle": next_lifecycle,
            "native_ids": dict(native_ids)
            if native_ids is not None
            else prior["native_ids"],
            "names": dict(names) if names is not None else prior["names"],
        }
        if _digest(candidate) == _digest({key: prior[key] for key in candidate}):
            return {**prior, "idempotent": True}
        now = self.now()
        self.conn.execute("BEGIN")
        try:
            revision_id = self._write_identity(
                source_id,
                namespace,
                prior["revision"] + 1,
                prior["revision_id"],
                candidate["display_name"],
                next_lifecycle,
                candidate["native_ids"],
                candidate["names"],
                context,
                principal_id,
                now,
            )
            self._audit(
                namespace,
                "revise",
                revision_id,
                principal_id,
                {"from_revision": prior["revision"]},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self.get(namespace, source_id, scopes={READ_SCOPE})

    def decide_alias(
        self,
        namespace: str,
        source_id: str,
        alias_type: str,
        value: str,
        *,
        language: str = "und",
        confidence: float = 1.0,
        reason: str,
        provenance: Mapping[str, Any] | None = None,
        reviewer_id: str,
        scopes: set[str],
        action: str = "link",
    ) -> dict[str, Any]:
        _require(scopes, REVIEW_SCOPE)
        if action not in {"link", "split"} or len(reason.strip()) < 10:
            raise SourceIdentityError(
                "invalid_alias_decision", "action and substantive reason are required"
            )
        if not self.get(namespace, source_id, scopes={READ_SCOPE}):
            raise SourceIdentityError(
                "not_found", "source identity does not exist in namespace"
            )
        normalized = normalize_alias(alias_type, value)
        confidence_value = _bounded(confidence, "confidence")
        prior = self.conn.execute(
            "SELECT d.decision_id,d.action FROM source_alias_decisions d WHERE d.namespace=? "
            "AND d.alias_type=? AND d.normalized_alias=? AND d.language=? AND d.source_id=? "
            "AND NOT EXISTS (SELECT 1 FROM source_alias_decisions child "
            "WHERE child.predecessor_decision_id=d.decision_id) "
            "ORDER BY d.created_at_ms DESC,d.decision_id DESC LIMIT 1",
            [namespace, alias_type, normalized, language, source_id],
        ).fetchone()
        stable = [
            namespace,
            alias_type,
            normalized,
            language,
            source_id,
            action,
            confidence_value,
            reason.strip(),
            dict(provenance or {}),
            reviewer_id,
        ]
        decision_id = "source-alias-decision:" + _digest(stable)[:24]
        existing = self.conn.execute(
            "SELECT decision_id FROM source_alias_decisions WHERE decision_id=?",
            [decision_id],
        ).fetchone()
        if existing:
            return {**self._alias_decision(decision_id), "idempotent": True}
        now = self.now()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO source_alias_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    decision_id,
                    namespace,
                    alias_type,
                    normalized,
                    language,
                    source_id,
                    action,
                    confidence_value,
                    reason.strip(),
                    _canonical(dict(provenance or {})),
                    reviewer_id,
                    prior[0] if prior else None,
                    now,
                ],
            )
            self._audit(
                namespace,
                f"alias-{action}",
                decision_id,
                reviewer_id,
                {"alias_type": alias_type},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self._alias_decision(decision_id)

    def _alias_decision(self, decision_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT namespace,alias_type,normalized_alias,language,source_id,action,confidence,reason,"
            "provenance_json,reviewer_id,predecessor_decision_id,created_at_ms "
            "FROM source_alias_decisions WHERE decision_id=?",
            [decision_id],
        ).fetchone()
        return {
            "contract": ALIAS_CONTRACT,
            "decision_id": decision_id,
            "namespace": row[0],
            "alias_type": row[1],
            "normalized_alias": row[2],
            "language": row[3],
            "source_id": row[4],
            "action": row[5],
            "confidence": float(row[6]),
            "reason": row[7],
            "provenance": _load(row[8], {}),
            "reviewer_id": row[9],
            "predecessor_decision_id": row[10],
            "created_at_ms": int(row[11]),
        }

    def resolve_alias(
        self,
        namespace: str,
        alias_type: str,
        value: str,
        *,
        scopes: set[str],
        language: str = "und",
        limit: int = 25,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        normalized = normalize_alias(alias_type, value)
        rows = self.conn.execute(
            "SELECT d.source_id,d.decision_id,d.action,d.confidence,d.reason,d.created_at_ms "
            "FROM source_alias_decisions d WHERE d.namespace=? AND d.alias_type=? "
            "AND d.normalized_alias=? AND d.language IN (?,'und') "
            "AND NOT EXISTS (SELECT 1 FROM source_alias_decisions child "
            "WHERE child.predecessor_decision_id=d.decision_id) "
            "ORDER BY d.source_id,d.created_at_ms DESC,d.decision_id DESC",
            [namespace, alias_type, normalized, language],
        ).fetchall()
        latest: dict[str, Sequence[Any]] = {}
        for row in rows:
            latest.setdefault(str(row[0]), row)
        matches = [
            {
                "source_id": source_id,
                "decision_id": row[1],
                "confidence": float(row[3]),
                "reason": row[4],
            }
            for source_id, row in latest.items()
            if row[2] == "link"
        ][: min(max(int(limit), 1), 100)]
        return {
            "alias_type": alias_type,
            "normalized_alias": normalized,
            "language": language,
            "matches": matches,
            "ambiguous": len(matches) > 1,
            "resolved": len(matches) == 1,
        }

    def relate(
        self,
        namespace: str,
        from_source_id: str,
        to_source_id: str,
        relationship_type: str,
        *,
        principal_id: str,
        scopes: set[str],
        valid_from_ms: int | None = None,
        valid_to_ms: int | None = None,
        observed_at_ms: int | None = None,
        confidence: float = 1.0,
        uncertainty: float = 0.0,
        evidence: Sequence[Mapping[str, Any]] = (),
        producer: Mapping[str, Any] | None = None,
        policy: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        if (
            relationship_type not in RELATIONSHIP_TYPES
            or from_source_id == to_source_id
        ):
            raise SourceIdentityError(
                "invalid_relationship",
                "relationship type and distinct endpoints are required",
            )
        for source_id in (from_source_id, to_source_id):
            if not self.get(namespace, source_id, scopes={READ_SCOPE}):
                raise SourceIdentityError(
                    "not_found", "relationship endpoint does not exist in namespace"
                )
        if (
            valid_from_ms is not None
            and valid_to_ms is not None
            and valid_to_ms < valid_from_ms
        ):
            raise SourceIdentityError(
                "invalid_temporality", "valid-time interval is reversed"
            )
        confidence_value, uncertainty_value = (
            _bounded(confidence, "confidence"),
            _bounded(uncertainty, "uncertainty"),
        )
        relationship_id = (
            "source-relationship:"
            + _digest(
                [
                    namespace,
                    from_source_id,
                    to_source_id,
                    relationship_type,
                    valid_from_ms,
                ]
            )[:24]
        )
        current = self.conn.execute(
            "SELECT c.revision,r.input_hash FROM source_relationship_current c "
            "JOIN source_relationship_revisions r USING(relationship_revision_id) WHERE c.relationship_id=?",
            [relationship_id],
        ).fetchone()
        now = self.now()
        current_value = self._relationship(relationship_id) if current else None
        payload = {
            "from_source_id": from_source_id,
            "to_source_id": to_source_id,
            "relationship_type": relationship_type,
            "lifecycle": "active",
            "valid_from_ms": valid_from_ms,
            "valid_to_ms": valid_to_ms,
            "observed_at_ms": int(
                observed_at_ms
                if observed_at_ms is not None
                else current_value["observed_at_ms"]
                if current_value
                else now
            ),
            "confidence": confidence_value,
            "uncertainty": uncertainty_value,
            "evidence": [dict(item) for item in evidence],
            "producer": dict(
                producer or {"name": "noesis-source-identity", "version": "1.0.0"}
            ),
            "policy": dict(policy or {"relationship": "sourced-v1"}),
        }
        input_hash = _digest(payload)
        if current and current[1] == input_hash:
            return {**self._relationship(relationship_id), "idempotent": True}
        revision = int(current[0]) + 1 if current else 1
        revision_id = (
            "source-relationship-revision:"
            + _digest([relationship_id, revision, input_hash])[:24]
        )
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO source_relationship_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    revision_id,
                    relationship_id,
                    namespace,
                    revision,
                    from_source_id,
                    to_source_id,
                    relationship_type,
                    "active",
                    valid_from_ms,
                    valid_to_ms,
                    payload["observed_at_ms"],
                    confidence_value,
                    uncertainty_value,
                    _canonical(payload["evidence"]),
                    _canonical(payload["producer"]),
                    _canonical(payload["policy"]),
                    principal_id,
                    input_hash,
                    now,
                ],
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO source_relationship_current VALUES (?,?,?)",
                [relationship_id, revision_id, revision],
            )
            self._audit(
                namespace,
                "relate",
                revision_id,
                principal_id,
                {"relationship_type": relationship_type},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self._relationship(relationship_id)

    def _relationship(self, relationship_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT relationship_revision_id,namespace,revision,from_source_id,to_source_id,"
            "relationship_type,lifecycle,valid_from_ms,valid_to_ms,observed_at_ms,confidence,uncertainty,"
            "evidence_json,producer_json,policy_json,principal_id,input_hash,created_at_ms "
            "FROM source_relationship_revisions WHERE relationship_id=? ORDER BY revision DESC LIMIT 1",
            [relationship_id],
        ).fetchone()
        if not row:
            raise SourceIdentityError("not_found", "source relationship does not exist")
        return {
            "contract": RELATIONSHIP_CONTRACT,
            "relationship_id": relationship_id,
            "relationship_revision_id": row[0],
            "namespace": row[1],
            "revision": int(row[2]),
            "from_source_id": row[3],
            "to_source_id": row[4],
            "relationship_type": row[5],
            "lifecycle": row[6],
            "valid_from_ms": row[7],
            "valid_to_ms": row[8],
            "observed_at_ms": int(row[9]),
            "confidence": float(row[10]),
            "uncertainty": float(row[11]),
            "evidence": _load(row[12], []),
            "producer": _load(row[13], {}),
            "policy": _load(row[14], {}),
            "principal_id": row[15],
            "input_hash": row[16],
            "created_at_ms": int(row[17]),
        }

    def retract_relationship(
        self,
        namespace: str,
        relationship_id: str,
        reason: str,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        if len(reason.strip()) < 10:
            raise SourceIdentityError(
                "invalid_retraction", "a substantive reason is required"
            )
        prior = self._relationship(relationship_id)
        if prior["namespace"] != namespace:
            raise SourceIdentityError(
                "not_found", "relationship does not exist in namespace"
            )
        if prior["lifecycle"] == "retracted":
            return {**prior, "idempotent": True}
        revision, now = prior["revision"] + 1, self.now()
        evidence = [*prior["evidence"], {"kind": "retraction", "reason": reason}]
        payload = {
            **prior,
            "revision": revision,
            "lifecycle": "retracted",
            "evidence": evidence,
        }
        input_hash = _digest(payload)
        revision_id = (
            "source-relationship-revision:"
            + _digest([relationship_id, revision, input_hash])[:24]
        )
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO source_relationship_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    revision_id,
                    relationship_id,
                    namespace,
                    revision,
                    prior["from_source_id"],
                    prior["to_source_id"],
                    prior["relationship_type"],
                    "retracted",
                    prior["valid_from_ms"],
                    prior["valid_to_ms"],
                    prior["observed_at_ms"],
                    prior["confidence"],
                    prior["uncertainty"],
                    _canonical(evidence),
                    _canonical(prior["producer"]),
                    _canonical(prior["policy"]),
                    principal_id,
                    input_hash,
                    now,
                ],
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO source_relationship_current VALUES (?,?,?)",
                [relationship_id, revision_id, revision],
            )
            self._audit(
                namespace,
                "retract-relationship",
                revision_id,
                principal_id,
                {"reason": reason},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return self._relationship(relationship_id)

    def _relationships_as_of(
        self, namespace: str, as_of_ms: int | None
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT c.relationship_id FROM source_relationship_current c "
            "JOIN source_relationship_revisions r USING(relationship_revision_id) "
            "WHERE r.namespace=? AND r.lifecycle='active' "
            "AND (? IS NULL OR r.valid_from_ms IS NULL OR r.valid_from_ms<=?) "
            "AND (? IS NULL OR r.valid_to_ms IS NULL OR r.valid_to_ms>?) ORDER BY c.relationship_id",
            [namespace, as_of_ms, as_of_ms, as_of_ms, as_of_ms],
        ).fetchall()
        return [self._relationship(row[0]) for row in rows]

    def dossier(
        self,
        namespace: str,
        source_id: str,
        *,
        scopes: set[str],
        as_of_ms: int | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        identity = self.get(namespace, source_id, scopes=scopes)
        if not identity:
            raise SourceIdentityError(
                "not_found", "source identity does not exist in namespace"
            )
        offset, bounded = (
            _decode_cursor(source_id, cursor),
            min(max(int(limit), 1), 100),
        )
        relationships = [
            item
            for item in self._relationships_as_of(namespace, as_of_ms)
            if source_id in {item["from_source_id"], item["to_source_id"]}
        ]
        page = relationships[offset : offset + bounded]
        aliases = self.conn.execute(
            "SELECT decision_id FROM source_alias_decisions WHERE namespace=? AND source_id=? "
            "ORDER BY created_at_ms,decision_id",
            [namespace, source_id],
        ).fetchall()
        citations = [entry for item in page for entry in item["evidence"]]
        return {
            "contract": DOSSIER_CONTRACT,
            "identity": identity,
            "aliases": [self._alias_decision(row[0]) for row in aliases],
            "relationships": page,
            "next_cursor": _encode_cursor(source_id, offset + bounded)
            if offset + bounded < len(relationships)
            else None,
            "as_of_ms": as_of_ms,
            "citation_count": len(citations),
            "citation_complete": all(bool(item["evidence"]) for item in page),
        }

    def path(
        self,
        namespace: str,
        from_source_id: str,
        to_source_id: str,
        *,
        scopes: set[str],
        as_of_ms: int | None = None,
        max_depth: int = 6,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        bounded_depth = min(max(int(max_depth), 1), 12)
        relationships = self._relationships_as_of(namespace, as_of_ms)
        graph: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        for edge in relationships:
            graph[edge["from_source_id"]].append((edge["to_source_id"], edge))
            graph[edge["to_source_id"]].append((edge["from_source_id"], edge))
        queue = deque([(from_source_id, [])])
        seen = {from_source_id}
        while queue:
            node, traversed = queue.popleft()
            if node == to_source_id:
                return {"found": True, "path": traversed, "as_of_ms": as_of_ms}
            if len(traversed) >= bounded_depth:
                continue
            for neighbor, edge in sorted(graph[node], key=lambda item: item[0]):
                if neighbor not in seen:
                    seen.add(neighbor)
                    queue.append((neighbor, [*traversed, edge]))
        return {
            "found": False,
            "path": [],
            "as_of_ms": as_of_ms,
            "bounded_depth": bounded_depth,
        }

    def explain_independence(
        self,
        namespace: str,
        source_ids: Sequence[str],
        *,
        scopes: set[str],
        as_of_ms: int | None = None,
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        requested = list(dict.fromkeys(str(item) for item in source_ids))[:100]
        relationships = self._relationships_as_of(namespace, as_of_ms)
        parent = {item: item for item in requested}

        def find(item):
            parent.setdefault(item, item)
            while parent[item] != item:
                parent[item] = parent[parent[item]]
                item = parent[item]
            return item

        def union(left, right):
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[max(left_root, right_root)] = min(left_root, right_root)

        used = []
        for edge in relationships:
            if edge["relationship_type"] in INDEPENDENCE_EDGES:
                union(edge["from_source_id"], edge["to_source_id"])
                used.append(edge["relationship_revision_id"])
        groups: dict[str, list[str]] = defaultdict(list)
        anonymous, missing = [], []
        for source_id in requested:
            identity = self.get(namespace, source_id, scopes=scopes)
            if not identity:
                missing.append(source_id)
                continue
            if identity["kind"] == "unknown":
                anonymous.append(source_id)
            groups[find(source_id)].append(source_id)
        rendered = [
            {
                "group_id": "source-independence:" + _digest(members)[:16],
                "source_ids": sorted(members),
            }
            for members in sorted(groups.values(), key=lambda values: values[0])
        ]
        return {
            "contract": INDEPENDENCE_CONTRACT,
            "namespace": namespace,
            "source_ids": requested,
            "groups": rendered,
            "as_of_ms": as_of_ms,
            "relationship_revision_ids": sorted(used),
            "anonymous_source_ids": anonymous,
            "missing_source_ids": missing,
            "complete": not anonymous and not missing,
            "limitations": [
                "missing relationships preserve separate groups but do not prove independence"
            ],
            "explanation_hash": _digest(
                [namespace, requested, rendered, used, as_of_ms]
            ),
        }
