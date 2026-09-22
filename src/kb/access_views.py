"""Default-deny, policy-bound knowledge views and safe derived projections."""

from __future__ import annotations

import hashlib
import json
import time

POLICY_CONTRACT = "noesis-access-view-policy-v1"
DECISION_CONTRACT = "noesis-access-decision-v1"
PROJECTION_CONTRACT = "noesis-redacted-projection-v1"
GRANT_CONTRACT = "noesis-share-grant-v1"
HEALTH_CONTRACT = "noesis-access-view-health-v1"
READ_SCOPE = "knowledge:views:read"
WRITE_SCOPE = "knowledge:views:write"
ADMIN_SCOPE = "knowledge:views:admin"
EXPORT_SCOPE = "knowledge:views:export"

_DDL = """
CREATE TABLE IF NOT EXISTS access_view_policies(policy_id TEXT NOT NULL,namespace TEXT NOT NULL,version BIGINT NOT NULL,status TEXT NOT NULL,rules_json TEXT NOT NULL,created_by TEXT NOT NULL,created_at_ms BIGINT NOT NULL,PRIMARY KEY(namespace,policy_id,version));
CREATE TABLE IF NOT EXISTS access_view_objects(namespace TEXT NOT NULL,object_type TEXT NOT NULL,object_id TEXT NOT NULL,classification TEXT NOT NULL,source_license TEXT,jurisdiction TEXT,policy_id TEXT NOT NULL,policy_version BIGINT NOT NULL,payload_json TEXT NOT NULL,lineage_json TEXT NOT NULL,generation BIGINT NOT NULL,valid_time_json TEXT NOT NULL,observed_at_ms BIGINT NOT NULL,PRIMARY KEY(namespace,object_type,object_id));
CREATE TABLE IF NOT EXISTS access_redacted_projections(projection_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,source_type TEXT NOT NULL,source_id TEXT NOT NULL,policy_id TEXT NOT NULL,policy_version BIGINT NOT NULL,transformation TEXT NOT NULL,payload_json TEXT NOT NULL,safe_lineage_json TEXT NOT NULL,status TEXT NOT NULL,generation BIGINT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,source_type,source_id,policy_id,policy_version,transformation));
CREATE TABLE IF NOT EXISTS access_share_grants(grant_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,recipient_id TEXT NOT NULL,purpose TEXT NOT NULL,expires_at_ms BIGINT NOT NULL,redistribution BOOLEAN NOT NULL,watermark_required BOOLEAN NOT NULL,policy_id TEXT NOT NULL,policy_version BIGINT NOT NULL,status TEXT NOT NULL,object_ids_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS access_view_audit(audit_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,operation TEXT NOT NULL,outcome TEXT NOT NULL,principal_id TEXT NOT NULL,purpose TEXT,object_ref TEXT,detail_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL);
"""


class AccessViewError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.details = code, details


def _canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value):
    return hashlib.sha256(_canon(value).encode()).hexdigest()


def _load(value, default):
    return (
        default
        if value is None
        else json.loads(value)
        if isinstance(value, str)
        else value
    )


def _require(scopes, required):
    if required not in scopes and "operator" not in scopes:
        raise AccessViewError("unauthorized", f"missing required scope {required}")


class AccessViewStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _audit(
        self,
        namespace,
        operation,
        outcome,
        principal,
        purpose=None,
        object_ref=None,
        detail=None,
    ):
        now = self.now()
        detail = dict(detail or {})
        self.conn.execute(
            "INSERT OR IGNORE INTO access_view_audit VALUES (?,?,?,?,?,?,?,?,?)",
            [
                "access-audit:"
                + _hash(
                    [
                        namespace,
                        operation,
                        outcome,
                        principal,
                        purpose,
                        object_ref,
                        detail,
                        now,
                    ]
                )[:24],
                namespace,
                operation,
                outcome,
                principal,
                purpose,
                object_ref,
                _canon(detail),
                now,
            ],
        )

    def register_policy(
        self,
        namespace,
        policy_id,
        version,
        rules,
        *,
        principal_id,
        scopes,
        status="active",
    ):
        _require(scopes, ADMIN_SCOPE)
        required = {
            "allowed_principals",
            "allowed_purposes",
            "allowed_classifications",
            "allowed_transformations",
        }
        if not required <= rules.keys():
            raise AccessViewError(
                "invalid_policy", "policy omits mandatory default-deny rule sets"
            )
        content = dict(rules)
        existing = self.conn.execute(
            "SELECT rules_json,status FROM access_view_policies WHERE namespace=? AND policy_id=? AND version=?",
            [namespace, policy_id, version],
        ).fetchone()
        if existing:
            if _load(existing[0], {}) != content:
                raise AccessViewError(
                    "policy_version_conflict", "policy version is immutable"
                )
            return self.policy(
                namespace, policy_id, version, scopes={ADMIN_SCOPE}, idempotent=True
            )
        now = self.now()
        self.conn.execute(
            "INSERT INTO access_view_policies VALUES (?,?,?,?,?,?,?)",
            [policy_id, namespace, version, status, _canon(content), principal_id, now],
        )
        self._audit(
            namespace,
            "register_policy",
            "allowed",
            principal_id,
            object_ref=f"{policy_id}:{version}",
        )
        self._invalidate_old(namespace, policy_id, version)
        return self.policy(namespace, policy_id, version, scopes={ADMIN_SCOPE})

    def _invalidate_old(self, namespace, policy_id, version):
        self.conn.execute(
            "UPDATE access_redacted_projections SET status='invalidated' WHERE namespace=? AND policy_id=? AND policy_version<>?",
            [namespace, policy_id, version],
        )

    def policy(self, namespace, policy_id, version, *, scopes, idempotent=False):
        _require(scopes, ADMIN_SCOPE)
        row = self.conn.execute(
            "SELECT status,rules_json,created_by,created_at_ms FROM access_view_policies WHERE namespace=? AND policy_id=? AND version=?",
            [namespace, policy_id, version],
        ).fetchone()
        if not row:
            raise AccessViewError("policy_not_found", "access policy not found")
        return {
            "contract": POLICY_CONTRACT,
            "namespace": namespace,
            "policy_id": policy_id,
            "version": int(version),
            "status": row[0],
            "rules": _load(row[1], {}),
            "created_by": row[2],
            "created_at_ms": int(row[3]),
            "default": "deny",
            "idempotent": idempotent,
        }

    def register_object(
        self,
        namespace,
        object_type,
        object_id,
        classification,
        policy_id,
        policy_version,
        payload,
        *,
        source_license=None,
        jurisdiction=None,
        lineage=(),
        generation=0,
        valid_time=None,
        observed_at_ms=None,
        principal_id,
        scopes,
    ):
        _require(scopes, WRITE_SCOPE)
        self.policy(namespace, policy_id, policy_version, scopes={ADMIN_SCOPE})
        existing = self.conn.execute(
            "SELECT payload_json,classification FROM access_view_objects WHERE namespace=? AND object_type=? AND object_id=?",
            [namespace, object_type, object_id],
        ).fetchone()
        if existing:
            if _load(existing[0], {}) != dict(payload) or existing[1] != classification:
                raise AccessViewError(
                    "object_conflict", "access-bound object identity is immutable"
                )
            return {
                "namespace": namespace,
                "object_type": object_type,
                "object_id": object_id,
                "idempotent": True,
            }
        now = self.now()
        self.conn.execute(
            "INSERT INTO access_view_objects VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                object_type,
                object_id,
                classification,
                source_license,
                jurisdiction,
                policy_id,
                policy_version,
                _canon(dict(payload)),
                _canon(list(lineage)),
                generation,
                _canon(dict(valid_time or {})),
                observed_at_ms or now,
            ],
        )
        self._audit(
            namespace,
            "register_object",
            "allowed",
            principal_id,
            object_ref=f"{object_type}:{object_id}",
        )
        return {
            "namespace": namespace,
            "object_type": object_type,
            "object_id": object_id,
            "policy_id": policy_id,
            "policy_version": policy_version,
            "idempotent": False,
        }

    def decide(
        self,
        namespace,
        object_type,
        object_id,
        *,
        principal_id,
        purpose,
        scopes,
        transformation="read",
        recipient_id=None,
        disclose=False,
        audit_decision=True,
    ):
        _require(scopes, ADMIN_SCOPE if disclose else READ_SCOPE)
        row = self.conn.execute(
            "SELECT classification,source_license,jurisdiction,policy_id,policy_version FROM access_view_objects WHERE namespace=? AND object_type=? AND object_id=?",
            [namespace, object_type, object_id],
        ).fetchone()
        reasons = []
        policy_id, version = "", 0
        if not row:
            reasons.append("object_not_visible")
        else:
            policy_id, version = row[3], int(row[4])
            policy = self.policy(namespace, policy_id, version, scopes={ADMIN_SCOPE})
            rules = policy["rules"]
            tests = (
                (
                    principal_id in rules["allowed_principals"]
                    or "*" in rules["allowed_principals"],
                    "principal_not_allowed",
                ),
                (purpose in rules["allowed_purposes"], "purpose_not_allowed"),
                (
                    row[0] in rules["allowed_classifications"],
                    "classification_not_allowed",
                ),
                (
                    transformation in rules["allowed_transformations"],
                    "transformation_not_allowed",
                ),
                (
                    not rules.get("allowed_licenses")
                    or row[1] in rules["allowed_licenses"],
                    "license_not_allowed",
                ),
                (
                    not rules.get("allowed_jurisdictions")
                    or row[2] in rules["allowed_jurisdictions"],
                    "jurisdiction_not_allowed",
                ),
                (policy["status"] == "active", "policy_inactive"),
            )
            reasons.extend(reason for passed, reason in tests if not passed)
        allowed = not reasons
        if audit_decision:
            self._audit(
                namespace,
                "decide",
                "allowed" if allowed else "denied",
                principal_id,
                purpose,
                f"{object_type}:{object_id}",
                {"policy_id": policy_id, "version": version, "reason_codes": reasons},
            )
        result = {
            "contract": DECISION_CONTRACT,
            "namespace": namespace,
            "allowed": allowed,
            "policy_id": policy_id,
            "policy_version": version,
            "purpose": purpose,
            "transformation": transformation,
        }
        if disclose:
            result.update(
                {
                    "object_type": object_type,
                    "object_id": object_id,
                    "principal_id": principal_id,
                    "reason_codes": reasons or ["allowed"],
                }
            )
        elif not allowed:
            result["error"] = {
                "code": "not_available",
                "message": "requested knowledge is not available",
            }
        return result

    def simulate(
        self,
        namespace,
        object_type,
        object_id,
        *,
        principal_id,
        purpose,
        transformation="read",
        scopes,
    ):
        _require(scopes, ADMIN_SCOPE)
        return self.decide(
            namespace,
            object_type,
            object_id,
            principal_id=principal_id,
            purpose=purpose,
            transformation=transformation,
            scopes={ADMIN_SCOPE},
            disclose=True,
            audit_decision=False,
        )

    def filter_query(
        self,
        namespace,
        candidates,
        *,
        principal_id,
        purpose,
        scopes,
        limit=100,
        offset=0,
    ):
        _require(scopes, READ_SCOPE)
        limit = min(max(int(limit), 1), 500)
        visible = []
        for candidate in candidates[:1000]:
            decision = self.decide(
                namespace,
                candidate["object_type"],
                candidate["object_id"],
                principal_id=principal_id,
                purpose=purpose,
                scopes={READ_SCOPE},
            )
            if decision["allowed"]:
                row = self.conn.execute(
                    "SELECT payload_json FROM access_view_objects WHERE namespace=? AND object_type=? AND object_id=?",
                    [namespace, candidate["object_type"], candidate["object_id"]],
                ).fetchone()
                visible.append({**candidate, "payload": _load(row[0], {})})
        page = visible[max(int(offset), 0) : max(int(offset), 0) + limit]
        return {
            "contract": DECISION_CONTRACT,
            "namespace": namespace,
            "allowed": True,
            "purpose": purpose,
            "transformation": "query",
            "results": page,
            "visible_count": len(visible),
            "next_offset": offset + len(page)
            if offset + len(page) < len(visible)
            else None,
        }

    def derive_redacted(
        self,
        namespace,
        object_type,
        object_id,
        transformation,
        redacted_payload,
        *,
        principal_id,
        purpose,
        scopes,
        generation=0,
    ):
        _require(scopes, WRITE_SCOPE)
        decision = self.decide(
            namespace,
            object_type,
            object_id,
            principal_id=principal_id,
            purpose=purpose,
            transformation=transformation,
            scopes={READ_SCOPE},
        )
        if not decision["allowed"]:
            raise AccessViewError(
                "not_available", "requested knowledge is not available"
            )
        row = self.conn.execute(
            "SELECT lineage_json,policy_id,policy_version,payload_json FROM access_view_objects WHERE namespace=? AND object_type=? AND object_id=?",
            [namespace, object_type, object_id],
        ).fetchone()
        source_payload = _load(row[3], {})
        leaked = set(redacted_payload) & set(source_payload) - set(
            redacted_payload.get("allowed_fields", [])
        )
        if any(redacted_payload.get(key) == source_payload.get(key) for key in leaked):
            raise AccessViewError(
                "unsafe_projection", "projection contains an unapproved source field"
            )
        projection_id = (
            "redacted:"
            + _hash(
                [namespace, object_type, object_id, row[1], row[2], transformation]
            )[:24]
        )
        safe_lineage = [
            {"opaque_ref": "lineage:" + _hash(item)[:20]} for item in _load(row[0], [])
        ]
        now = self.now()
        self.conn.execute(
            "INSERT INTO access_redacted_projections VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(projection_id) DO UPDATE SET payload_json=excluded.payload_json,safe_lineage_json=excluded.safe_lineage_json,status=excluded.status,generation=excluded.generation,created_at_ms=excluded.created_at_ms",
            [
                projection_id,
                namespace,
                object_type,
                object_id,
                row[1],
                row[2],
                transformation,
                _canon(dict(redacted_payload)),
                _canon(safe_lineage),
                "active",
                generation,
                now,
            ],
        )
        self._audit(
            namespace,
            "derive_redacted",
            "allowed",
            principal_id,
            purpose,
            projection_id,
        )
        return {
            "contract": PROJECTION_CONTRACT,
            "projection_id": projection_id,
            "namespace": namespace,
            "source_ref": {
                "type": object_type,
                "opaque_id": "object:" + _hash([namespace, object_id])[:20],
            },
            "policy_id": row[1],
            "policy_version": int(row[2]),
            "transformation": transformation,
            "payload": dict(redacted_payload),
            "safe_lineage": safe_lineage,
            "status": "active",
            "generation": generation,
        }

    def create_grant(
        self,
        namespace,
        recipient_id,
        purpose,
        expires_at_ms,
        policy_id,
        policy_version,
        object_ids,
        *,
        redistribution=False,
        watermark_required=True,
        principal_id,
        scopes,
    ):
        _require(scopes, EXPORT_SCOPE)
        self.policy(namespace, policy_id, policy_version, scopes={ADMIN_SCOPE})
        grant_id = (
            "share-grant:"
            + _hash(
                [
                    namespace,
                    recipient_id,
                    purpose,
                    expires_at_ms,
                    policy_id,
                    policy_version,
                    sorted(object_ids),
                ]
            )[:24]
        )
        now = self.now()
        self.conn.execute(
            "INSERT OR IGNORE INTO access_share_grants VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                grant_id,
                namespace,
                recipient_id,
                purpose,
                expires_at_ms,
                redistribution,
                watermark_required,
                policy_id,
                policy_version,
                "active",
                _canon(sorted(set(object_ids))),
                now,
            ],
        )
        self._audit(
            namespace, "create_grant", "allowed", principal_id, purpose, grant_id
        )
        return self._grant(namespace, grant_id)

    def _grant(self, namespace, grant_id):
        row = self.conn.execute(
            "SELECT recipient_id,purpose,expires_at_ms,redistribution,watermark_required,policy_id,policy_version,status,object_ids_json FROM access_share_grants WHERE namespace=? AND grant_id=?",
            [namespace, grant_id],
        ).fetchone()
        if not row:
            raise AccessViewError("grant_not_found", "share grant not found")
        return {
            "contract": GRANT_CONTRACT,
            "grant_id": grant_id,
            "namespace": namespace,
            "recipient_id": row[0],
            "purpose": row[1],
            "expires_at_ms": int(row[2]),
            "redistribution": bool(row[3]),
            "watermark_required": bool(row[4]),
            "policy_id": row[5],
            "policy_version": int(row[6]),
            "status": row[7],
            "object_ids": _load(row[8], []),
        }

    def authorize_export(
        self,
        namespace,
        grant_id,
        recipient_id,
        purpose,
        object_ids,
        *,
        watermark=None,
        redistribution=False,
        principal_id,
        scopes,
    ):
        _require(scopes, EXPORT_SCOPE)
        grant = self._grant(namespace, grant_id)
        reasons = []
        if grant["status"] != "active":
            reasons.append("grant_revoked")
        if grant["expires_at_ms"] <= self.now():
            reasons.append("grant_expired")
        if grant["recipient_id"] != recipient_id:
            reasons.append("recipient_mismatch")
        if grant["purpose"] != purpose:
            reasons.append("purpose_mismatch")
        if not set(object_ids) <= set(grant["object_ids"]):
            reasons.append("object_not_granted")
        if redistribution and not grant["redistribution"]:
            reasons.append("redistribution_denied")
        if grant["watermark_required"] and not watermark:
            reasons.append("watermark_required")
        allowed = not reasons
        self._audit(
            namespace,
            "authorize_export",
            "allowed" if allowed else "denied",
            principal_id,
            purpose,
            grant_id,
            {"reason_codes": reasons},
        )
        return {
            **grant,
            "authorized": allowed,
            "watermark": watermark if allowed else None,
            "reason_codes": reasons,
        }

    def revoke_grant(self, namespace, grant_id, *, principal_id, scopes):
        _require(scopes, EXPORT_SCOPE)
        self._grant(namespace, grant_id)
        self.conn.execute(
            "UPDATE access_share_grants SET status='revoked' WHERE namespace=? AND grant_id=?",
            [namespace, grant_id],
        )
        self._audit(
            namespace, "revoke_grant", "allowed", principal_id, object_ref=grant_id
        )
        return self._grant(namespace, grant_id)

    def audit(self, namespace, *, scopes, limit=100):
        _require(scopes, ADMIN_SCOPE)
        rows = self.conn.execute(
            "SELECT operation,outcome,principal_id,purpose,object_ref,detail_json,created_at_ms FROM access_view_audit WHERE namespace=? ORDER BY created_at_ms DESC LIMIT ?",
            [namespace, min(max(int(limit), 1), 500)],
        ).fetchall()
        return {
            "namespace": namespace,
            "events": [
                {
                    "operation": r[0],
                    "outcome": r[1],
                    "principal_id": r[2],
                    "purpose": r[3],
                    "object_ref": r[4],
                    "detail": _load(r[5], {}),
                    "created_at_ms": int(r[6]),
                }
                for r in rows
            ],
        }

    def health(self, namespace, *, scopes):
        _require(scopes, ADMIN_SCOPE)
        policies = self.conn.execute(
            "SELECT count(*) FROM access_view_policies WHERE namespace=? AND status='active'",
            [namespace],
        ).fetchone()[0]
        invalid = self.conn.execute(
            "SELECT count(*) FROM access_redacted_projections WHERE namespace=? AND status='invalidated'",
            [namespace],
        ).fetchone()[0]
        expired = self.conn.execute(
            "SELECT count(*) FROM access_share_grants WHERE namespace=? AND status='active' AND expires_at_ms<=?",
            [namespace, self.now()],
        ).fetchone()[0]
        return {
            "contract": HEALTH_CONTRACT,
            "namespace": namespace,
            "status": "degraded" if invalid or expired else "healthy",
            "active_policies": int(policies),
            "invalidated_projections": int(invalid),
            "expired_active_grants": int(expired),
        }
