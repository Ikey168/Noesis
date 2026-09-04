"""Semantic change events, evidence-linked briefs, ranking, and delivery."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence

EVENT_CONTRACT = "noesis-semantic-change-event-v1"
POLICY_CONTRACT = "noesis-change-brief-policy-v1"
BRIEF_CONTRACT = "noesis-change-brief-v1"
DELIVERY_CONTRACT = "noesis-change-brief-delivery-v1"
EXPORT_CONTRACT = "noesis-change-brief-export-v1"
READ_SCOPE = "knowledge:briefs:read"
WRITE_SCOPE = "knowledge:briefs:write"
DELIVER_SCOPE = "knowledge:briefs:deliver"
REVIEW_SCOPE = "knowledge:briefs:review"
_DDL = """
CREATE TABLE IF NOT EXISTS change_brief_policies(policy_revision_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,policy_id TEXT NOT NULL,version TEXT NOT NULL,payload_json TEXT NOT NULL,content_hash TEXT NOT NULL,principal_id TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,policy_id,version));
CREATE TABLE IF NOT EXISTS semantic_change_events(change_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,object_type TEXT NOT NULL,object_id TEXT NOT NULL,from_generation BIGINT,to_generation BIGINT,classification TEXT NOT NULL,score DOUBLE NOT NULL,payload_json TEXT NOT NULL,content_hash TEXT NOT NULL,principal_id TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,object_type,object_id,from_generation,to_generation,content_hash));
CREATE TABLE IF NOT EXISTS change_briefs(brief_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,change_id TEXT NOT NULL,policy_revision_id TEXT NOT NULL,payload_json TEXT NOT NULL,brief_hash TEXT NOT NULL,principal_id TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,change_id,policy_revision_id));
CREATE TABLE IF NOT EXISTS change_brief_subscriptions(subscription_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,subscriber_id TEXT NOT NULL,window_ms BIGINT NOT NULL,filters_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,subscriber_id,filters_json));
CREATE TABLE IF NOT EXISTS change_brief_deliveries(delivery_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,subscription_id TEXT NOT NULL,subscriber_id TEXT NOT NULL,window_start_ms BIGINT NOT NULL,window_end_ms BIGINT NOT NULL,payload_json TEXT NOT NULL,delivery_hash TEXT NOT NULL,status TEXT NOT NULL,attempts BIGINT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(subscription_id,window_start_ms,delivery_hash));
CREATE TABLE IF NOT EXISTS change_brief_feedback(feedback_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,brief_id TEXT NOT NULL,principal_id TEXT NOT NULL,rating TEXT NOT NULL,reason TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,brief_id,principal_id));
CREATE TABLE IF NOT EXISTS change_brief_audit(audit_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,operation TEXT NOT NULL,object_id TEXT NOT NULL,principal_id TEXT NOT NULL,detail_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL);
"""


class ChangeBriefError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _canon(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(v):
    return hashlib.sha256((v if isinstance(v, str) else _canon(v)).encode()).hexdigest()


def _load(v, d):
    return d if v is None else json.loads(v) if isinstance(v, str) else v


def _require(s, r):
    if r not in s and "operator" not in s:
        raise ChangeBriefError("unauthorized", f"missing required scope {r}")


def _limit(v, m=500):
    return min(max(int(v), 1), m)


def _normalized(v):
    return re.sub(r"\W+", " ", str(v).casefold()).strip()


class ChangeBriefStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        conn.execute(_DDL) if initialize else None

    def _audit(self, n, o, i, p, d, t):
        self.conn.execute(
            "INSERT OR IGNORE INTO change_brief_audit VALUES (?,?,?,?,?,?,?)",
            ["brief-audit:" + _hash([n, o, i, p, d, t])[:24], n, o, i, p, _canon(d), t],
        )

    def register_policy(
        self,
        namespace,
        policy_id,
        version,
        *,
        principal_id,
        scopes,
        weights=None,
        minimum_score=0.25,
        user_priorities=None,
        generation=0,
        valid_from_ms=None,
        valid_to_ms=None,
        observed_at_ms=None,
        producer=None,
        policy_context=None,
        provenance=None,
    ):
        _require(scopes, WRITE_SCOPE)
        w = {
            "magnitude": 0.3,
            "novelty": 0.2,
            "decision_relevance": 0.2,
            "source_authority": 0.2,
            "user_policy": 0.1,
        }
        w.update(weights or {})
        if abs(sum(float(v) for v in w.values()) - 1) > 1e-6:
            raise ChangeBriefError("invalid_policy", "ranking weights must sum to one")
        now = self.now()
        payload = {
            "contract": POLICY_CONTRACT,
            "namespace": namespace,
            "policy_id": policy_id,
            "version": version,
            "weights": w,
            "minimum_score": float(minimum_score),
            "user_priorities": dict(user_priorities or {}),
            "generation": int(generation),
            "valid_time": {"from_ms": valid_from_ms, "to_ms": valid_to_ms},
            "observed_at_ms": observed_at_ms if observed_at_ms is not None else now,
            "producer": dict(producer or {}),
            "policy_context": dict(policy_context or {}),
            "provenance": dict(provenance or {}),
        }
        h = _hash(payload)
        rid = "brief-policy:" + h[:24]
        payload["policy_revision_id"] = rid
        row = self.conn.execute(
            "SELECT content_hash,payload_json FROM change_brief_policies WHERE namespace=? AND policy_id=? AND version=?",
            [namespace, policy_id, version],
        ).fetchone()
        if row:
            if row[0] != h:
                raise ChangeBriefError(
                    "version_conflict", "policy version has different content"
                )
            return {**_load(row[1], {}), "idempotent": True}
        self.conn.execute(
            "INSERT INTO change_brief_policies VALUES (?,?,?,?,?,?,?,?)",
            [rid, namespace, policy_id, version, _canon(payload), h, principal_id, now],
        )
        self._audit(namespace, "register-policy", rid, principal_id, {}, now)
        return {**payload, "idempotent": False}

    def policy(self, namespace, policy_revision_id, *, scopes):
        _require(scopes, READ_SCOPE)
        r = self.conn.execute(
            "SELECT payload_json FROM change_brief_policies WHERE namespace=? AND policy_revision_id=?",
            [namespace, policy_revision_id],
        ).fetchone()
        if not r:
            raise ChangeBriefError("policy_not_found", "brief policy not found")
        return _load(r[0], {})

    def preview(
        self,
        namespace,
        object_type,
        object_id,
        before,
        after,
        from_generation,
        to_generation,
        *,
        scopes,
        evidence_before=(),
        evidence_after=(),
        factors=None,
        coverage_before=True,
        coverage_after=True,
    ):
        _require(scopes, READ_SCOPE)
        if object_type not in {
            "claim",
            "entity",
            "event",
            "metric",
            "policy",
            "method",
            "evidence-coverage",
        }:
            raise ChangeBriefError("invalid_object_type", "unsupported change object")
        if before is None:
            kind = "addition"
        elif after is None:
            kind = "removal"
        elif (
            isinstance(before, (int, float))
            and isinstance(after, (int, float))
            and before != after
        ):
            kind = "numeric-change"
        elif isinstance(after, Mapping) and after.get("status") in {
            "retracted",
            "withdrawn",
        }:
            kind = "retraction"
        elif (
            isinstance(before, Mapping)
            and isinstance(after, Mapping)
            and before.get("classification") != after.get("classification")
        ):
            kind = "reclassification"
        elif _normalized(before) == _normalized(after):
            kind = "cosmetic"
        else:
            kind = "correction"
        defaults = {
            "magnitude": 0 if kind == "cosmetic" else 1,
            "novelty": 1,
            "decision_relevance": 0.5,
            "source_authority": 0.5,
            "user_policy": 0.5,
        }
        defaults.update(factors or {})
        if not coverage_before or not coverage_after:
            defaults["novelty"] = 0
        return {
            "contract": EVENT_CONTRACT,
            "namespace": namespace,
            "object_type": object_type,
            "object_id": object_id,
            "from_generation": from_generation,
            "to_generation": to_generation,
            "classification": kind,
            "before": before,
            "after": after,
            "evidence_before": [dict(v) for v in evidence_before],
            "evidence_after": [dict(v) for v in evidence_after],
            "coverage": {
                "before": bool(coverage_before),
                "after": bool(coverage_after),
            },
            "factors": defaults,
            "coverage_warning": None
            if coverage_before and coverage_after
            else "coverage-changed-or-incomplete",
            "preview_hash": _hash(
                [
                    namespace,
                    object_type,
                    object_id,
                    from_generation,
                    to_generation,
                    before,
                    after,
                    evidence_before,
                    evidence_after,
                    defaults,
                ]
            ),
        }

    def generate(
        self,
        namespace,
        policy_revision_id,
        preview,
        *,
        principal_id,
        scopes,
        cancel_requested=False,
    ):
        _require(scopes, WRITE_SCOPE)
        policy = self.policy(namespace, policy_revision_id, scopes={READ_SCOPE})
        if preview.get("namespace") != namespace:
            raise ChangeBriefError(
                "namespace_mismatch", "preview belongs to another namespace"
            )
        if cancel_requested:
            return {
                "contract": BRIEF_CONTRACT,
                "namespace": namespace,
                "status": "cancelled",
                "items": [],
                "replay_hash": _hash([preview, "cancelled"]),
            }
        score = sum(
            float(preview["factors"][k]) * float(v)
            for k, v in policy["weights"].items()
        )
        material = (
            score >= policy["minimum_score"] and preview["classification"] != "cosmetic"
        )
        event = {
            **preview,
            "score": round(score, 6),
            "material": material,
            "policy_revision_id": policy_revision_id,
        }
        content_hash = _hash(event)
        change_id = (
            "semantic-change:"
            + _hash(
                [
                    namespace,
                    preview["object_type"],
                    preview["object_id"],
                    preview["from_generation"],
                    preview["to_generation"],
                    preview["preview_hash"],
                ]
            )[:24]
        )
        event["change_id"] = change_id
        uncertainty = []
        if preview["coverage_warning"]:
            uncertainty.append(preview["coverage_warning"])
        if not preview["evidence_before"]:
            uncertainty.append("missing-prior-evidence")
        if not preview["evidence_after"]:
            uncertainty.append("missing-current-evidence")
        explanation = (
            f"{preview['object_type']} {preview['object_id']} was classified as {preview['classification']}."
            if material
            else f"No material change was established for {preview['object_id']}."
        )
        brief = {
            "contract": BRIEF_CONTRACT,
            "namespace": namespace,
            "status": "generated",
            "change_id": change_id,
            "brief_id": "change-brief:" + _hash([change_id, policy_revision_id])[:24],
            "classification": preview["classification"],
            "material": material,
            "score": round(score, 6),
            "what_changed": explanation,
            "why_it_matters": None
            if not material
            else "The configured materiality threshold was met.",
            "uncertainty": uncertainty,
            "before": {
                "value": preview["before"],
                "evidence": preview["evidence_before"],
            },
            "after": {"value": preview["after"], "evidence": preview["evidence_after"]},
            "policy_revision_id": policy_revision_id,
            "generation": {
                "from": preview["from_generation"],
                "to": preview["to_generation"],
            },
            "created_at_ms": self.now(),
        }
        brief_hash = _hash(brief)
        brief["brief_hash"] = brief_hash
        row = self.conn.execute(
            "SELECT payload_json FROM change_briefs WHERE brief_id=?",
            [brief["brief_id"]],
        ).fetchone()
        if row:
            return {**_load(row[0], {}), "idempotent": True}
        now = self.now()
        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.execute(
                "INSERT INTO semantic_change_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    change_id,
                    namespace,
                    preview["object_type"],
                    preview["object_id"],
                    preview["from_generation"],
                    preview["to_generation"],
                    preview["classification"],
                    score,
                    _canon(event),
                    content_hash,
                    principal_id,
                    now,
                ],
            )
            self.conn.execute(
                "INSERT INTO change_briefs VALUES (?,?,?,?,?,?,?,?)",
                [
                    brief["brief_id"],
                    namespace,
                    change_id,
                    policy_revision_id,
                    _canon(brief),
                    brief_hash,
                    principal_id,
                    now,
                ],
            )
            self._audit(
                namespace,
                "generate",
                brief["brief_id"],
                principal_id,
                {"material": material},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**brief, "idempotent": False}

    def get(self, namespace, brief_id, *, scopes):
        _require(scopes, READ_SCOPE)
        r = self.conn.execute(
            "SELECT payload_json FROM change_briefs WHERE namespace=? AND brief_id=?",
            [namespace, brief_id],
        ).fetchone()
        if not r:
            raise ChangeBriefError("brief_not_found", "change brief not found")
        return _load(r[0], {})

    def history(
        self, namespace, object_type=None, object_id=None, *, scopes, limit=50, offset=0
    ):
        _require(scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT b.payload_json,e.object_type,e.object_id FROM change_briefs b JOIN semantic_change_events e ON e.change_id=b.change_id WHERE b.namespace=? ORDER BY b.created_at_ms,b.brief_id",
            [namespace],
        ).fetchall()
        items = [
            _load(r[0], {})
            for r in rows
            if (not object_type or r[1] == object_type)
            and (not object_id or r[2] == object_id)
        ]
        start = max(int(offset), 0)
        page = items[start : start + _limit(limit)]
        return {
            "items": page,
            "total": len(items),
            "next_offset": start + len(page)
            if start + len(page) < len(items)
            else None,
        }

    def compare(self, namespace, left_brief_id, right_brief_id, *, scopes):
        left = self.get(namespace, left_brief_id, scopes=scopes)
        right = self.get(namespace, right_brief_id, scopes=scopes)
        diff = {
            k: {"left": left.get(k), "right": right.get(k)}
            for k in ("classification", "material", "score", "uncertainty")
            if left.get(k) != right.get(k)
        }
        return {
            "left_brief_id": left_brief_id,
            "right_brief_id": right_brief_id,
            "differences": diff,
            "comparison_hash": _hash(diff),
        }

    def replay(self, namespace, brief_id, *, scopes):
        b = self.get(namespace, brief_id, scopes=scopes)
        expected = b["brief_hash"]
        actual = _hash({k: v for k, v in b.items() if k != "brief_hash"})
        return {
            "brief_id": brief_id,
            "expected_hash": expected,
            "actual_hash": actual,
            "deterministic": expected == actual,
        }

    def subscribe(
        self, namespace, subscriber_id, window_ms, filters, *, principal_id, scopes
    ):
        _require(scopes, WRITE_SCOPE)
        f = _canon(filters)
        sid = "brief-subscription:" + _hash([namespace, subscriber_id, filters])[:24]
        now = self.now()
        self.conn.execute(
            "INSERT OR IGNORE INTO change_brief_subscriptions VALUES (?,?,?,?,?,?)",
            [
                sid,
                namespace,
                subscriber_id,
                min(max(int(window_ms), 1000), 86_400_000),
                f,
                now,
            ],
        )
        return {
            "subscription_id": sid,
            "namespace": namespace,
            "subscriber_id": subscriber_id,
            "window_ms": min(max(int(window_ms), 1000), 86_400_000),
            "filters": dict(filters),
        }

    def deliver(
        self,
        namespace,
        subscription_id,
        window_start_ms,
        window_end_ms,
        *,
        principal_id,
        scopes,
        cancel_requested=False,
    ):
        _require(scopes, DELIVER_SCOPE)
        r = self.conn.execute(
            "SELECT subscriber_id,filters_json FROM change_brief_subscriptions WHERE namespace=? AND subscription_id=?",
            [namespace, subscription_id],
        ).fetchone()
        if not r:
            raise ChangeBriefError(
                "subscription_not_found", "brief subscription not found"
            )
        if cancel_requested:
            return {
                "contract": DELIVERY_CONTRACT,
                "namespace": namespace,
                "subscription_id": subscription_id,
                "status": "cancelled",
                "items": [],
                "delivery_hash": _hash(
                    [subscription_id, window_start_ms, window_end_ms, "cancelled"]
                ),
            }
        filters = _load(r[1], {})
        rows = self.conn.execute(
            "SELECT payload_json FROM change_briefs WHERE namespace=? AND created_at_ms>=? AND created_at_ms<? ORDER BY brief_id",
            [namespace, window_start_ms, window_end_ms],
        ).fetchall()
        items = [_load(v[0], {}) for v in rows]
        items = [
            v for v in items if (not filters.get("material_only") or v["material"])
        ]
        items = list({v["brief_id"]: v for v in items}.values())[:500]
        h = _hash(items)
        did = "brief-delivery:" + _hash([subscription_id, window_start_ms, h])[:24]
        payload = {
            "contract": DELIVERY_CONTRACT,
            "namespace": namespace,
            "delivery_id": did,
            "subscription_id": subscription_id,
            "subscriber_id": r[0],
            "window": {"start_ms": window_start_ms, "end_ms": window_end_ms},
            "status": "pending",
            "items": items,
            "quiet": not items,
            "delivery_hash": h,
            "attempts": 1,
        }
        existing = self.conn.execute(
            "SELECT payload_json,attempts FROM change_brief_deliveries WHERE delivery_id=?",
            [did],
        ).fetchone()
        if existing:
            payload = _load(existing[0], {})
            payload["attempts"] = existing[1] + 1
            self.conn.execute(
                "UPDATE change_brief_deliveries SET attempts=?,payload_json=? WHERE delivery_id=?",
                [payload["attempts"], _canon(payload), did],
            )
            return {**payload, "retry": True}
        now = self.now()
        self.conn.execute(
            "INSERT INTO change_brief_deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                did,
                namespace,
                subscription_id,
                r[0],
                window_start_ms,
                window_end_ms,
                _canon(payload),
                h,
                "pending",
                1,
                now,
            ],
        )
        return {**payload, "retry": False}

    def acknowledge(self, namespace, delivery_id, *, principal_id, scopes):
        _require(scopes, DELIVER_SCOPE)
        r = self.conn.execute(
            "SELECT payload_json FROM change_brief_deliveries WHERE namespace=? AND delivery_id=?",
            [namespace, delivery_id],
        ).fetchone()
        if not r:
            raise ChangeBriefError("delivery_not_found", "delivery not found")
        p = _load(r[0], {})
        p["status"] = "acknowledged"
        self.conn.execute(
            "UPDATE change_brief_deliveries SET status='acknowledged',payload_json=? WHERE delivery_id=?",
            [_canon(p), delivery_id],
        )
        return p

    def feedback(self, namespace, brief_id, rating, reason, *, principal_id, scopes):
        _require(scopes, REVIEW_SCOPE)
        self.get(namespace, brief_id, scopes={READ_SCOPE})
        fid = "brief-feedback:" + _hash([namespace, brief_id, principal_id])[:24]
        now = self.now()
        self.conn.execute(
            "INSERT INTO change_brief_feedback VALUES (?,?,?,?,?,?,?) ON CONFLICT(namespace,brief_id,principal_id) DO UPDATE SET rating=excluded.rating,reason=excluded.reason,created_at_ms=excluded.created_at_ms",
            [fid, namespace, brief_id, principal_id, rating, reason, now],
        )
        return {
            "feedback_id": fid,
            "brief_id": brief_id,
            "rating": rating,
            "reason": reason,
        }

    def export(self, namespace, brief_ids: Sequence[str], *, scopes, limit=100):
        _require(scopes, READ_SCOPE)
        items = [
            self.get(namespace, v, scopes=scopes)
            for v in list(brief_ids)[: _limit(limit, 100)]
        ]
        policy_ids = sorted({v["policy_revision_id"] for v in items})
        policies = [self.policy(namespace, v, scopes=scopes) for v in policy_ids]
        p = {
            "contract": EXPORT_CONTRACT,
            "namespace": namespace,
            "items": items,
            "policies": policies,
            "dependency_complete": True,
        }
        p["export_hash"] = _hash(p)
        return p
