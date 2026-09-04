"""Durable generation vectors for repeatable multi-call research sessions."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Mapping
from typing import Any

CONTRACT = "noesis-research-snapshot-v1"
TOKEN_CONTRACT = "noesis-research-snapshot-token-v1"
READ_SCOPE = "knowledge:snapshot:read"
WRITE_SCOPE = "knowledge:snapshot:write"
MAX_LIFETIME_MS = 86_400_000

_DDL = """
CREATE TABLE IF NOT EXISTS research_snapshot_sessions (
  session_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
  principal_id TEXT NOT NULL, scopes_json TEXT NOT NULL, selection_json TEXT NOT NULL,
  vector_json TEXT NOT NULL, vector_hash TEXT NOT NULL, status TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, expires_at_ms BIGINT NOT NULL,
  maximum_expires_at_ms BIGINT NOT NULL, closed_at_ms BIGINT
);
CREATE TABLE IF NOT EXISTS research_snapshot_pins (
  session_id TEXT NOT NULL, component_kind TEXT NOT NULL,
  component_id TEXT NOT NULL, generation TEXT NOT NULL,
  PRIMARY KEY(session_id,component_kind,component_id)
);
CREATE TABLE IF NOT EXISTS research_snapshot_audit (
  event_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, principal_id TEXT NOT NULL,
  action TEXT NOT NULL, detail_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshot_expiry
  ON research_snapshot_sessions(status,expires_at_ms);
"""


class ResearchSnapshotError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    raw = value if isinstance(value, str) else _canonical(value)
    return hashlib.sha256(raw.encode()).hexdigest()


def _load(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _table(conn: Any, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
            [name],
        ).fetchone()
    )


def _require(scopes: set[str], required: str) -> None:
    if required not in scopes and "operator" not in scopes:
        raise ResearchSnapshotError(
            "unauthorized", f"missing required scope {required}"
        )


class ResearchSnapshotStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _audit(
        self, session_id: str, principal_id: str, action: str, detail: Any, now: int
    ) -> None:
        event_id = "snapshot-event:" + _digest([session_id, action, detail, now])[:24]
        self.conn.execute(
            "INSERT OR IGNORE INTO research_snapshot_audit VALUES (?,?,?,?,?,?)",
            [event_id, session_id, principal_id, action, _canonical(detail), now],
        )

    def _resolve(
        self, selection: Mapping[str, Any]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        packs = sorted({str(value) for value in selection.get("packs") or []})
        namespaces = sorted({str(value) for value in selection.get("namespaces") or []})
        domains = sorted({str(value) for value in selection.get("domains") or []})
        allow_degraded = bool(selection.get("allow_degraded", False))
        omissions: list[dict[str, Any]] = []
        vector: dict[str, Any] = {
            "packs": {},
            "namespaces": {},
            "schemas": {},
            "federated": {},
        }
        if _table(self.conn, "knowledge_maintenance_generations"):
            clauses, params = ["status IN ('complete','partial')"], []
            if packs:
                marks = ",".join("?" for _ in packs)
                clauses.append(f"pack_id IN ({marks})")
                params.extend(packs)
            rows = self.conn.execute(
                "SELECT pack_id,generation,source_watermark,workflow_watermark,artifact_watermark,"
                "generation_id,receipt_hash,status FROM knowledge_maintenance_generations "
                f"WHERE {' AND '.join(clauses)} QUALIFY ROW_NUMBER() OVER "
                "(PARTITION BY pack_id ORDER BY generation DESC)=1 ORDER BY pack_id",
                params,
            ).fetchall()
            for row in rows:
                vector["packs"][row[0]] = {
                    "generation": int(row[1]),
                    "source_watermark": int(row[2]),
                    "workflow_watermark": int(row[3]),
                    "artifact_watermark": int(row[4]),
                    "generation_id": row[5],
                    "receipt_hash": row[6],
                    "status": row[7],
                }
        for pack in packs:
            if pack not in vector["packs"]:
                omissions.append(
                    {"kind": "pack", "id": pack, "reason": "generation-unavailable"}
                )
        if _table(self.conn, "derived_object_generations"):
            clauses, params = ["status='committed'"], []
            if namespaces:
                marks = ",".join("?" for _ in namespaces)
                clauses.append(f"namespace IN ({marks})")
                params.extend(namespaces)
            rows = self.conn.execute(
                "SELECT namespace,generation,change_hash FROM derived_object_generations "
                f"WHERE {' AND '.join(clauses)} QUALIFY ROW_NUMBER() OVER "
                "(PARTITION BY namespace ORDER BY generation DESC)=1 ORDER BY namespace",
                params,
            ).fetchall()
            for namespace, generation, change_hash in rows:
                artifact = None
                if _table(self.conn, "knowledge_artifact_watermarks"):
                    artifact = self.conn.execute(
                        "SELECT watermark FROM knowledge_artifact_watermarks WHERE namespace=?",
                        [namespace],
                    ).fetchone()
                vector["namespaces"][namespace] = {
                    "derived_generation": int(generation),
                    "change_hash": change_hash,
                    "artifact_watermark": None if not artifact else int(artifact[0]),
                }
        for namespace in namespaces:
            if namespace not in vector["namespaces"]:
                omissions.append(
                    {
                        "kind": "namespace",
                        "id": namespace,
                        "reason": "generation-unavailable",
                    }
                )
        if _table(self.conn, "knowledge_schema_modules"):
            for name, version, content_hash in self.conn.execute(
                "SELECT name,semantic_version,content_hash FROM knowledge_schema_modules "
                "WHERE status='active' ORDER BY name,semantic_version"
            ).fetchall():
                vector["schemas"][str(name)] = {
                    "version": str(version),
                    "content_hash": str(content_hash),
                }
        for item in sorted(
            selection.get("federated") or [],
            key=lambda value: str(value.get("source_id")),
        ):
            source_id = str(item.get("source_id") or "")
            consistency = str(item.get("consistency") or "")
            generation = item.get("generation") or item.get("revision")
            if (
                not source_id
                or consistency not in {"snapshot", "backend-snapshot"}
                or generation is None
            ):
                omissions.append(
                    {
                        "kind": "federated",
                        "id": source_id,
                        "reason": "snapshot-unsupported",
                    }
                )
                continue
            vector["federated"][source_id] = {
                "generation": str(generation),
                "capability_hash": item.get("capability_hash"),
                "consistency": consistency,
            }
        if omissions and not allow_degraded:
            raise ResearchSnapshotError(
                "generation_unavailable",
                "one or more requested generations cannot be pinned",
                omissions=omissions,
            )
        vector["domains"] = domains
        vector["omissions"] = omissions
        return vector, omissions

    def begin(
        self,
        selection: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
        ttl_ms: int = 3_600_000,
        maximum_lifetime_ms: int = MAX_LIFETIME_MS,
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        if not principal_id or not 1_000 <= int(ttl_ms) <= MAX_LIFETIME_MS:
            raise ResearchSnapshotError(
                "invalid_session", "principal and bounded ttl are required"
            )
        maximum = min(max(int(maximum_lifetime_ms), int(ttl_ms)), MAX_LIFETIME_MS)
        vector, omissions = self._resolve(selection)
        vector_hash = _digest(vector)
        now = self.now()
        token = secrets.token_urlsafe(32)
        token_hash = _digest(token)
        session_id = (
            "research-snapshot:" + _digest([principal_id, vector_hash, token_hash])[:24]
        )
        expires = now + int(ttl_ms)
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO research_snapshot_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
                [
                    session_id,
                    token_hash,
                    principal_id,
                    _canonical(sorted(scopes)),
                    _canonical(dict(selection)),
                    _canonical(vector),
                    vector_hash,
                    "active",
                    now,
                    expires,
                    now + maximum,
                ],
            )
            for kind in ("packs", "namespaces", "schemas", "federated"):
                for component_id, detail in sorted(vector[kind].items()):
                    generation = (
                        detail.get("generation")
                        or detail.get("derived_generation")
                        or detail.get("version")
                    )
                    self.conn.execute(
                        "INSERT INTO research_snapshot_pins VALUES (?,?,?,?)",
                        [
                            session_id,
                            kind.removesuffix("s"),
                            component_id,
                            str(generation),
                        ],
                    )
            self._audit(
                session_id, principal_id, "begin", {"vector_hash": vector_hash}, now
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "contract": TOKEN_CONTRACT,
            "session_id": session_id,
            "token": token,
            "vector": vector,
            "vector_hash": vector_hash,
            "omissions": omissions,
            "created_at_ms": now,
            "expires_at_ms": expires,
        }

    def _session(
        self, token: str, principal_id: str, *, allow_expired: bool = False
    ) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT session_id,principal_id,scopes_json,selection_json,vector_json,vector_hash,status,"
            "created_at_ms,expires_at_ms,maximum_expires_at_ms,closed_at_ms "
            "FROM research_snapshot_sessions WHERE token_hash=?",
            [_digest(token)],
        ).fetchone()
        if not row or row[1] != principal_id:
            raise ResearchSnapshotError("not_found", "research snapshot does not exist")
        status = row[6]
        now = self.now()
        if status == "active" and int(row[8]) <= now:
            status = "expired"
        if status != "active" and not allow_expired:
            raise ResearchSnapshotError(
                "snapshot_expired", f"research snapshot is {status}"
            )
        return {
            "contract": CONTRACT,
            "session_id": row[0],
            "principal_id": row[1],
            "scopes": _load(row[2]),
            "selection": _load(row[3]),
            "vector": _load(row[4]),
            "vector_hash": row[5],
            "status": status,
            "created_at_ms": int(row[7]),
            "expires_at_ms": int(row[8]),
            "maximum_expires_at_ms": int(row[9]),
            "closed_at_ms": None if row[10] is None else int(row[10]),
        }

    def inspect(
        self, token: str, *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        return self._session(token, principal_id, allow_expired=True)

    def renew(
        self, token: str, *, principal_id: str, scopes: set[str], ttl_ms: int
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        session = self._session(token, principal_id)
        now = self.now()
        expires = min(now + max(1_000, int(ttl_ms)), session["maximum_expires_at_ms"])
        if expires <= now:
            raise ResearchSnapshotError(
                "snapshot_expired", "maximum snapshot lifetime reached"
            )
        self.conn.execute(
            "UPDATE research_snapshot_sessions SET expires_at_ms=? WHERE session_id=?",
            [expires, session["session_id"]],
        )
        self._audit(
            session["session_id"],
            principal_id,
            "renew",
            {"expires_at_ms": expires},
            now,
        )
        return {**session, "expires_at_ms": expires}

    def close(
        self, token: str, *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        _require(scopes, WRITE_SCOPE)
        session = self._session(token, principal_id, allow_expired=True)
        now = self.now()
        self.conn.execute(
            "UPDATE research_snapshot_sessions SET status='closed',closed_at_ms=? WHERE session_id=?",
            [now, session["session_id"]],
        )
        self._audit(session["session_id"], principal_id, "close", {}, now)
        return {
            "session_id": session["session_id"],
            "status": "closed",
            "closed_at_ms": now,
        }

    def bind_query(
        self,
        token: str,
        request: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        _require(scopes, READ_SCOPE)
        session = self._session(token, principal_id)
        original_scopes = set(session["scopes"])
        if "operator" not in original_scopes and not scopes <= original_scopes:
            raise ResearchSnapshotError(
                "scope_escalation", "session cannot gain authorization scopes"
            )
        selection = session["selection"]
        requested = dict(request.get("scope") or {})
        for key in ("domains", "namespaces"):
            allowed = set(selection.get(key) or [])
            values = set(requested.get(key) or [])
            if allowed and not values <= allowed:
                raise ResearchSnapshotError(
                    "scope_mismatch", f"query {key} exceed the pinned selection"
                )
        bound = json.loads(_canonical(dict(request)))
        bound["snapshot"] = {
            "contract": CONTRACT,
            "session_id": session["session_id"],
            "vector_hash": session["vector_hash"],
            "vector": session["vector"],
            "expires_at_ms": session["expires_at_ms"],
        }
        return bound

    def pins(
        self, token: str, *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        session = self.inspect(token, principal_id=principal_id, scopes=scopes)
        rows = self.conn.execute(
            "SELECT component_kind,component_id,generation FROM research_snapshot_pins "
            "WHERE session_id=? ORDER BY component_kind,component_id",
            [session["session_id"]],
        ).fetchall()
        return {
            "session_id": session["session_id"],
            "status": session["status"],
            "pins": [
                {"component_kind": row[0], "component_id": row[1], "generation": row[2]}
                for row in rows
            ],
        }

    def health(self) -> dict[str, Any]:
        now = self.now()
        counts: dict[str, int] = {}
        for status, expires_at_ms in self.conn.execute(
            "SELECT status,expires_at_ms FROM research_snapshot_sessions"
        ).fetchall():
            effective = (
                "expired"
                if status == "active" and int(expires_at_ms) <= now
                else status
            )
            counts[effective] = counts.get(effective, 0) + 1
        return {
            "contract": "noesis-research-snapshot-health-v1",
            "status": "healthy",
            "sessions": counts,
            "active_pins": int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM research_snapshot_pins p JOIN research_snapshot_sessions s "
                    "ON s.session_id=p.session_id WHERE s.status='active' AND s.expires_at_ms>?",
                    [now],
                ).fetchone()[0]
            ),
            "at_ms": now,
        }
