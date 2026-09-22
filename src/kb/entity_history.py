"""Immutable entity identity decisions, reversible merges/splits, and impact publication."""

from __future__ import annotations

import hashlib
import json
import time

DECISION_CONTRACT = "noesis-entity-identity-decision-v1"
MERGE_CONTRACT = "noesis-entity-merge-v1"
SPLIT_CONTRACT = "noesis-entity-split-v1"
IMPACT_CONTRACT = "noesis-entity-impact-v1"
EXPORT_CONTRACT = "noesis-entity-history-export-v1"
READ_SCOPE = "knowledge:entity-history:read"
WRITE_SCOPE = "knowledge:entity-history:write"
REVIEW_SCOPE = "knowledge:entity-history:review"
EXECUTE_SCOPE = "knowledge:entity-history:execute"
_DDL = """
CREATE TABLE IF NOT EXISTS entity_history_identities(entity_id TEXT NOT NULL,namespace TEXT NOT NULL,aliases_json TEXT NOT NULL,status TEXT NOT NULL,created_at_ms BIGINT NOT NULL,PRIMARY KEY(namespace,entity_id));
CREATE TABLE IF NOT EXISTS entity_identity_decisions(decision_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,event_key TEXT NOT NULL,revision BIGINT NOT NULL,decision_type TEXT NOT NULL,subject_ids_json TEXT NOT NULL,payload_json TEXT NOT NULL,reviewer_id TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,event_key,revision));
CREATE TABLE IF NOT EXISTS entity_history_redirects(namespace TEXT NOT NULL,source_id TEXT NOT NULL,target_id TEXT NOT NULL,decision_id TEXT NOT NULL,active BOOLEAN NOT NULL,PRIMARY KEY(namespace,source_id));
CREATE TABLE IF NOT EXISTS entity_history_assignments(namespace TEXT NOT NULL,object_type TEXT NOT NULL,object_id TEXT NOT NULL,entity_id TEXT NOT NULL,decision_id TEXT NOT NULL,active BOOLEAN NOT NULL,PRIMARY KEY(namespace,object_type,object_id));
CREATE TABLE IF NOT EXISTS entity_history_dependencies(namespace TEXT NOT NULL,entity_id TEXT NOT NULL,dependent_type TEXT NOT NULL,dependent_id TEXT NOT NULL,independent BOOLEAN NOT NULL,payload_json TEXT NOT NULL,PRIMARY KEY(namespace,entity_id,dependent_type,dependent_id));
CREATE TABLE IF NOT EXISTS entity_history_publications(publication_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,decision_id TEXT NOT NULL,generation BIGINT NOT NULL,status TEXT NOT NULL,payload_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL,UNIQUE(namespace,decision_id,generation));
CREATE TABLE IF NOT EXISTS entity_history_audit(audit_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,operation TEXT NOT NULL,object_id TEXT NOT NULL,principal_id TEXT NOT NULL,detail_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL);
"""


class EntityHistoryError(ValueError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _c(v):
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _h(v):
    return hashlib.sha256(_c(v).encode()).hexdigest()


def _l(v, d):
    return d if v is None else json.loads(v) if isinstance(v, str) else v


def _r(s, x):
    if x not in s and "operator" not in s:
        raise EntityHistoryError("unauthorized", f"missing required scope {x}")


def _b(v, m=500):
    return min(max(int(v), 1), m)


class EntityHistoryStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        conn.execute(_DDL) if initialize else None

    def _audit(self, n, o, i, p, d, t):
        self.conn.execute(
            "INSERT OR IGNORE INTO entity_history_audit VALUES (?,?,?,?,?,?,?)",
            ["entity-audit:" + _h([n, o, i, p, d, t])[:24], n, o, i, p, _c(d), t],
        )

    def register_entity(
        self, namespace, entity_id, aliases=(), *, principal_id, scopes
    ):
        _r(scopes, WRITE_SCOPE)
        now = self.now()
        existing = self.conn.execute(
            "SELECT aliases_json FROM entity_history_identities WHERE entity_id=? AND namespace=?",
            [entity_id, namespace],
        ).fetchone()
        if existing:
            return {
                "namespace": namespace,
                "entity_id": entity_id,
                "aliases": _l(existing[0], []),
                "idempotent": True,
            }
        self.conn.execute(
            "INSERT INTO entity_history_identities VALUES (?,?,?,?,?)",
            [entity_id, namespace, _c(sorted(set(aliases))), "active", now],
        )
        return {
            "namespace": namespace,
            "entity_id": entity_id,
            "aliases": sorted(set(aliases)),
            "idempotent": False,
        }

    def _entity(self, n, e):
        r = self.conn.execute(
            "SELECT aliases_json,status FROM entity_history_identities WHERE namespace=? AND entity_id=?",
            [n, e],
        ).fetchone()
        if not r:
            raise EntityHistoryError("entity_not_found", f"entity {e} not found")
        return {"entity_id": e, "aliases": _l(r[0], []), "status": r[1]}

    def decide(
        self,
        namespace,
        decision_type,
        subject_ids,
        payload,
        *,
        reviewer_id,
        principal_id,
        scopes,
        event_key=None,
    ):
        _r(scopes, REVIEW_SCOPE)
        if decision_type not in {
            "alias",
            "match",
            "non-match",
            "merge",
            "split",
            "redirect",
            "review",
            "undo",
        }:
            raise EntityHistoryError(
                "invalid_decision", "unsupported identity decision"
            )
        for e in subject_ids:
            self._entity(namespace, e)
        key = (
            event_key
            or "entity-event:"
            + _h([namespace, decision_type, sorted(subject_ids)])[:24]
        )
        current = self.conn.execute(
            "SELECT decision_id,revision,payload_json,reviewer_id FROM entity_identity_decisions WHERE namespace=? AND event_key=? ORDER BY revision DESC LIMIT 1",
            [namespace, key],
        ).fetchone()
        content = {
            "contract": DECISION_CONTRACT,
            "namespace": namespace,
            "event_key": key,
            "revision": 1 if not current else int(current[1]) + 1,
            "decision_type": decision_type,
            "subject_ids": list(subject_ids),
            "payload": dict(payload),
            "reviewer_id": reviewer_id,
            "generation": int(payload.get("generation", 0)),
            "valid_time": dict(payload.get("valid_time") or {}),
            "observed_at_ms": payload.get("observed_at_ms", self.now()),
            "producer": dict(payload.get("producer") or {}),
            "policy": dict(payload.get("policy") or {}),
            "provenance": dict(payload.get("provenance") or {}),
        }
        comparable = {k: v for k, v in content.items() if k != "revision"}
        if (
            current
            and {
                k: v
                for k, v in _l(current[2], {}).items()
                if k not in {"revision", "decision_id"}
            }
            == comparable
        ):
            return {**_l(current[2], {}), "idempotent": True}
        did = "entity-decision:" + _h([key, content["revision"], comparable])[:24]
        content["decision_id"] = did
        now = self.now()
        self.conn.execute(
            "INSERT INTO entity_identity_decisions VALUES (?,?,?,?,?,?,?,?,?)",
            [
                did,
                namespace,
                key,
                content["revision"],
                decision_type,
                _c(subject_ids),
                _c(content),
                reviewer_id,
                now,
            ],
        )
        self._audit(
            namespace, "decide", did, principal_id, {"type": decision_type}, now
        )
        return {
            **content,
            "idempotent": False,
            "reviewer_conflict": bool(
                current
                and current[3] != reviewer_id
                and _l(current[2], {}).get("payload") != dict(payload)
            ),
        }

    def resolve(self, namespace, entity_id, *, scopes, at_revision=None):
        _r(scopes, READ_SCOPE)
        self._entity(namespace, entity_id)
        path = []
        current = entity_id
        while True:
            if current in path:
                raise EntityHistoryError(
                    "redirect_cycle", "entity redirects contain a cycle"
                )
            path.append(current)
            row = self.conn.execute(
                "SELECT target_id,decision_id FROM entity_history_redirects WHERE namespace=? AND source_id=? AND active=true",
                [namespace, current],
            ).fetchone()
            if not row:
                break
            if at_revision is not None:
                rev = self.conn.execute(
                    "SELECT revision FROM entity_identity_decisions WHERE decision_id=?",
                    [row[1]],
                ).fetchone()
                if rev and int(rev[0]) > int(at_revision):
                    break
            current = row[0]
        return {
            "namespace": namespace,
            "requested_id": entity_id,
            "canonical_id": current,
            "redirect_path": path,
            "snapshot_revision": at_revision,
        }

    def merge_preview(
        self,
        namespace,
        source_ids,
        target_id,
        *,
        scopes,
        dual_control=False,
        approvals=(),
    ):
        _r(scopes, READ_SCOPE)
        self._entity(namespace, target_id)
        sources = sorted(set(source_ids))
        for e in sources:
            self._entity(namespace, e)
        if target_id in sources:
            raise EntityHistoryError(
                "merge_cycle", "target cannot be merged into itself"
            )
        for source in sources:
            if (
                source
                in self.resolve(namespace, target_id, scopes=scopes)["redirect_path"]
            ):
                raise EntityHistoryError(
                    "merge_cycle", "merge would create a redirect cycle"
                )
        impact = self.impact(namespace, sources + [target_id], scopes=scopes)
        p = {
            "contract": MERGE_CONTRACT,
            "namespace": namespace,
            "source_ids": sources,
            "target_id": target_id,
            "redirects": [{"source_id": v, "target_id": target_id} for v in sources],
            "impact": impact,
            "dual_control": bool(dual_control),
            "approvals": sorted(set(approvals)),
            "eligible": not dual_control or len(set(approvals)) >= 2,
        }
        p["preview_hash"] = _h(p)
        return p

    def execute_merge(self, namespace, preview, *, reviewer_id, principal_id, scopes):
        _r(scopes, EXECUTE_SCOPE)
        if preview.get("namespace") != namespace or not preview.get("eligible"):
            raise EntityHistoryError("review_required", "merge preview is not eligible")
        now = self.now()
        self.conn.execute("BEGIN TRANSACTION")
        try:
            decision = self.decide(
                namespace,
                "merge",
                preview["source_ids"] + [preview["target_id"]],
                {
                    "preview_hash": preview["preview_hash"],
                    "generation": preview.get("generation", 0),
                },
                reviewer_id=reviewer_id,
                principal_id=principal_id,
                scopes={REVIEW_SCOPE},
                event_key="merge:" + preview["preview_hash"],
            )
            for source in preview["source_ids"]:
                self.conn.execute(
                    "INSERT INTO entity_history_redirects VALUES (?,?,?,?,true) ON CONFLICT(namespace,source_id) DO UPDATE SET target_id=excluded.target_id,decision_id=excluded.decision_id,active=true",
                    [namespace, source, preview["target_id"], decision["decision_id"]],
                )
                self.conn.execute(
                    "UPDATE entity_history_identities SET status='redirected' WHERE namespace=? AND entity_id=?",
                    [namespace, source],
                )
            self._audit(
                namespace,
                "merge",
                decision["decision_id"],
                principal_id,
                {"target": preview["target_id"]},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            **preview,
            "decision_id": decision["decision_id"],
            "executed": True,
            "originals_preserved": True,
        }

    def split_preview(
        self,
        namespace,
        source_id,
        new_entities,
        reassignments,
        *,
        scopes,
        ambiguous_object_ids=(),
    ):
        _r(scopes, READ_SCOPE)
        self._entity(namespace, source_id)
        ids = [v["entity_id"] for v in new_entities]
        if len(ids) < 2 or len(ids) != len(set(ids)):
            raise EntityHistoryError(
                "invalid_split", "split requires at least two unique identities"
            )
        assigned = {v["object_id"] for v in reassignments}
        ambiguous = sorted(set(ambiguous_object_ids) - assigned)
        p = {
            "contract": SPLIT_CONTRACT,
            "namespace": namespace,
            "source_id": source_id,
            "new_entities": [dict(v) for v in new_entities],
            "reassignments": [dict(v) for v in reassignments],
            "ambiguous_object_ids": ambiguous,
            "partial": bool(ambiguous),
            "impact": self.impact(namespace, [source_id], scopes=scopes),
        }
        p["preview_hash"] = _h(p)
        return p

    def execute_split(self, namespace, preview, *, reviewer_id, principal_id, scopes):
        _r(scopes, EXECUTE_SCOPE)
        if preview.get("namespace") != namespace:
            raise EntityHistoryError(
                "namespace_conflict", "split preview belongs elsewhere"
            )
        subject = [preview["source_id"]]
        now = self.now()
        self.conn.execute("BEGIN TRANSACTION")
        try:
            for v in preview["new_entities"]:
                self.conn.execute(
                    "INSERT INTO entity_history_identities VALUES (?,?,?,?,?)",
                    [
                        v["entity_id"],
                        namespace,
                        _c(sorted(set(v.get("aliases", [])))),
                        "active",
                        now,
                    ],
                )
                subject.append(v["entity_id"])
            decision = self.decide(
                namespace,
                "split",
                subject,
                {"preview_hash": preview["preview_hash"]},
                reviewer_id=reviewer_id,
                principal_id=principal_id,
                scopes={REVIEW_SCOPE},
                event_key="split:" + preview["preview_hash"],
            )
            for v in preview["reassignments"]:
                self.conn.execute(
                    "INSERT INTO entity_history_assignments VALUES (?,?,?,?,?,true) ON CONFLICT(namespace,object_type,object_id) DO UPDATE SET entity_id=excluded.entity_id,decision_id=excluded.decision_id,active=true",
                    [
                        namespace,
                        v["object_type"],
                        v["object_id"],
                        v["entity_id"],
                        decision["decision_id"],
                    ],
                )
            self._audit(
                namespace,
                "split",
                decision["decision_id"],
                principal_id,
                {"partial": preview["partial"]},
                now,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**preview, "decision_id": decision["decision_id"], "executed": True}

    def undo(self, namespace, decision_id, *, reviewer_id, principal_id, scopes):
        _r(scopes, EXECUTE_SCOPE)
        row = self.conn.execute(
            "SELECT decision_type,subject_ids_json FROM entity_identity_decisions WHERE namespace=? AND decision_id=?",
            [namespace, decision_id],
        ).fetchone()
        if not row:
            raise EntityHistoryError("decision_not_found", "decision not found")
        undo = self.decide(
            namespace,
            "undo",
            _l(row[1], []),
            {"undoes": decision_id},
            reviewer_id=reviewer_id,
            principal_id=principal_id,
            scopes={REVIEW_SCOPE},
            event_key="undo:" + decision_id,
        )
        self.conn.execute("BEGIN TRANSACTION")
        try:
            if row[0] == "merge":
                self.conn.execute(
                    "UPDATE entity_history_redirects SET active=false WHERE namespace=? AND decision_id=?",
                    [namespace, decision_id],
                )
                self.conn.execute(
                    "UPDATE entity_history_identities SET status='active' WHERE namespace=? AND entity_id IN (SELECT source_id FROM entity_history_redirects WHERE decision_id=?)",
                    [namespace, decision_id],
                )
            if row[0] == "split":
                self.conn.execute(
                    "UPDATE entity_history_assignments SET active=false WHERE namespace=? AND decision_id=?",
                    [namespace, decision_id],
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "contract": DECISION_CONTRACT,
            "namespace": namespace,
            "decision_id": undo["decision_id"],
            "undoes": decision_id,
            "reversible": True,
        }

    def add_dependency(
        self,
        namespace,
        entity_id,
        dependent_type,
        dependent_id,
        *,
        independent=False,
        payload=None,
        principal_id,
        scopes,
    ):
        _r(scopes, WRITE_SCOPE)
        self._entity(namespace, entity_id)
        self.conn.execute(
            "INSERT OR REPLACE INTO entity_history_dependencies VALUES (?,?,?,?,?,?)",
            [
                namespace,
                entity_id,
                dependent_type,
                dependent_id,
                bool(independent),
                _c(payload or {}),
            ],
        )
        return {
            "entity_id": entity_id,
            "dependent_type": dependent_type,
            "dependent_id": dependent_id,
            "independent": bool(independent),
        }

    def impact(self, namespace, entity_ids, *, scopes, limit=500):
        _r(scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT entity_id,dependent_type,dependent_id,independent,payload_json FROM entity_history_dependencies WHERE namespace=? ORDER BY dependent_type,dependent_id",
            [namespace],
        ).fetchall()
        wanted = set(entity_ids)
        affected = [
            {
                "entity_id": r[0],
                "dependent_type": r[1],
                "dependent_id": r[2],
                "payload": _l(r[4], {}),
            }
            for r in rows
            if r[0] in wanted and not r[3]
        ][: _b(limit)]
        independent = [
            {"entity_id": r[0], "dependent_type": r[1], "dependent_id": r[2]}
            for r in rows
            if r[0] in wanted and r[3]
        ][: _b(limit)]
        return {
            "contract": IMPACT_CONTRACT,
            "namespace": namespace,
            "entity_ids": sorted(wanted),
            "affected": affected,
            "independent": independent,
            "rebuild_types": sorted({v["dependent_type"] for v in affected}),
            "bounded": len(affected) >= _b(limit),
        }

    def publish_rebuild(
        self,
        namespace,
        decision_id,
        generation,
        results,
        *,
        principal_id,
        scopes,
        cancel_requested=False,
    ):
        _r(scopes, EXECUTE_SCOPE)
        if cancel_requested:
            return {
                "contract": IMPACT_CONTRACT,
                "namespace": namespace,
                "decision_id": decision_id,
                "status": "cancelled",
                "published": False,
            }
        failures = [dict(v) for v in results if v.get("status") != "completed"]
        if failures:
            return {
                "contract": IMPACT_CONTRACT,
                "namespace": namespace,
                "decision_id": decision_id,
                "status": "failed",
                "published": False,
                "failures": failures,
            }
        p = {
            "contract": IMPACT_CONTRACT,
            "namespace": namespace,
            "decision_id": decision_id,
            "generation": int(generation),
            "status": "published",
            "published": True,
            "results": [dict(v) for v in results],
        }
        p["publication_hash"] = _h(p)
        pid = "entity-publication:" + p["publication_hash"][:24]
        now = self.now()
        self.conn.execute(
            "INSERT OR IGNORE INTO entity_history_publications VALUES (?,?,?,?,?,?,?)",
            [pid, namespace, decision_id, generation, "published", _c(p), now],
        )
        return p

    def history(self, namespace, entity_id, *, scopes, limit=100, offset=0):
        _r(scopes, READ_SCOPE)
        rows = [
            _l(r[0], {})
            for r in self.conn.execute(
                "SELECT payload_json FROM entity_identity_decisions WHERE namespace=? ORDER BY created_at_ms,decision_id",
                [namespace],
            ).fetchall()
        ]
        items = [v for v in rows if entity_id in v["subject_ids"]]
        start = max(int(offset), 0)
        page = items[start : start + _b(limit)]
        return {
            "items": page,
            "total": len(items),
            "next_offset": start + len(page)
            if start + len(page) < len(items)
            else None,
        }

    def export(self, namespace, entity_ids, *, scopes):
        _r(scopes, READ_SCOPE)
        histories = {
            e: self.history(namespace, e, scopes=scopes)["items"]
            for e in list(entity_ids)[:100]
        }
        p = {
            "contract": EXPORT_CONTRACT,
            "namespace": namespace,
            "entities": [self._entity(namespace, e) for e in histories],
            "histories": histories,
            "audit_complete": True,
        }
        p["export_hash"] = _h(p)
        return p
