"""Policy-gated, content-addressed citation snapshots, verification, and repair."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from difflib import SequenceMatcher
from typing import Any

POLICY_CONTRACT = "noesis-citation-archive-policy-v1"
SNAPSHOT_CONTRACT = "noesis-citation-snapshot-v1"
VERIFICATION_CONTRACT = "noesis-citation-verification-v1"
HEALTH_CONTRACT = "noesis-citation-health-v1"
EXPORT_CONTRACT = "noesis-citation-export-v1"
READ_SCOPE = "knowledge:citation:read"
WRITE_SCOPE = "knowledge:citation:write"
CAPTURE_SCOPE = "knowledge:citation:capture"
REPAIR_SCOPE = "knowledge:citation:repair"

_DDL = """
CREATE TABLE IF NOT EXISTS citation_policy_revisions (
  policy_revision_id TEXT PRIMARY KEY, policy_id TEXT NOT NULL, namespace TEXT NOT NULL,
  version TEXT NOT NULL, content_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
  predecessor_revision_id TEXT, principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,policy_id,version)
);
CREATE TABLE IF NOT EXISTS citation_policy_current (
  namespace TEXT NOT NULL, policy_id TEXT NOT NULL, policy_revision_id TEXT NOT NULL,
  PRIMARY KEY(namespace,policy_id)
);
CREATE TABLE IF NOT EXISTS citation_blobs (
  blob_hash TEXT PRIMARY KEY, media_type TEXT NOT NULL, byte_size BIGINT NOT NULL,
  content TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS citation_snapshots (
  snapshot_id TEXT PRIMARY KEY, citation_id TEXT NOT NULL, namespace TEXT NOT NULL,
  policy_revision_id TEXT NOT NULL, blob_hash TEXT, manifest_hash TEXT NOT NULL,
  manifest_json TEXT NOT NULL, principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,citation_id,manifest_hash)
);
CREATE TABLE IF NOT EXISTS citation_verifications (
  verification_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, citation_id TEXT NOT NULL,
  snapshot_id TEXT NOT NULL, assertion_hash TEXT NOT NULL, result_hash TEXT NOT NULL,
  payload_json TEXT NOT NULL, principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS citation_health_checks (
  health_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, citation_id TEXT NOT NULL,
  checked_at_ms BIGINT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL,
  principal_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS citation_repairs (
  repair_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, citation_id TEXT NOT NULL,
  archive_url TEXT NOT NULL, preview_hash TEXT NOT NULL, status TEXT NOT NULL,
  payload_json TEXT NOT NULL, principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  UNIQUE(namespace,citation_id,archive_url)
);
CREATE TABLE IF NOT EXISTS citation_preservation_audit (
  audit_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, operation TEXT NOT NULL,
  object_id TEXT NOT NULL, principal_id TEXT NOT NULL, detail_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL
);
"""


class CitationPreservationError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    raw = value.encode() if isinstance(value, str) else _canonical(value).encode()
    return hashlib.sha256(raw).hexdigest()


def _load(value: Any, default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value) if isinstance(value, str) else value


def _require(scopes: set[str], required: str) -> None:
    if required not in scopes and "operator" not in scopes:
        raise CitationPreservationError(
            "unauthorized", f"missing required scope {required}"
        )


def _bounded(value: int, maximum=500) -> int:
    return min(max(int(value), 1), maximum)


def _words(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


class CitationPreservationStore:
    def __init__(self, conn: Any, *, initialize=True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _audit(self, namespace, operation, object_id, principal_id, detail, now):
        aid = (
            "citation-audit:"
            + _digest([namespace, operation, object_id, principal_id, detail, now])[:24]
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO citation_preservation_audit VALUES (?,?,?,?,?,?,?)",
            [
                aid,
                namespace,
                operation,
                object_id,
                principal_id,
                _canonical(detail),
                now,
            ],
        )

    def register_policy(
        self,
        namespace: str,
        policy_id: str,
        version: str,
        *,
        principal_id: str,
        scopes: set[str],
        allow_robots_denied=False,
        allowed_licenses: Sequence[str] = (),
        allow_private=False,
        preserve_excerpts=True,
        preserve_assets=False,
        approved_archives: Sequence[str] = (),
        max_bytes=1_000_000,
        generation=0,
        valid_from_ms=None,
        valid_to_ms=None,
        observed_at_ms=None,
        producer=None,
        policy_context=None,
        provenance=None,
        predecessor_revision_id=None,
    ):
        _require(scopes, WRITE_SCOPE)
        if not all(str(v).strip() for v in (namespace, policy_id, version)):
            raise CitationPreservationError(
                "invalid_policy", "policy identity and version are required"
            )
        current = self.conn.execute(
            "SELECT r.policy_revision_id FROM citation_policy_current c JOIN citation_policy_revisions r ON r.policy_revision_id=c.policy_revision_id WHERE c.namespace=? AND c.policy_id=?",
            [namespace, policy_id],
        ).fetchone()
        if predecessor_revision_id and (
            not current or current[0] != predecessor_revision_id
        ):
            raise CitationPreservationError(
                "revision_conflict", "predecessor is not current"
            )
        now = self.now()
        payload = {
            "contract": POLICY_CONTRACT,
            "namespace": namespace,
            "policy_id": policy_id,
            "version": version,
            "allow_robots_denied": bool(allow_robots_denied),
            "allowed_licenses": list(allowed_licenses),
            "allow_private": bool(allow_private),
            "preserve_excerpts": bool(preserve_excerpts),
            "preserve_assets": bool(preserve_assets),
            "approved_archives": list(approved_archives),
            "max_bytes": min(max(int(max_bytes), 0), 5_000_000),
            "generation": int(generation),
            "valid_time": {"from_ms": valid_from_ms, "to_ms": valid_to_ms},
            "observed_at_ms": observed_at_ms if observed_at_ms is not None else now,
            "producer": dict(producer or {}),
            "policy_context": dict(policy_context or {}),
            "provenance": dict(provenance or {}),
            "predecessor_revision_id": predecessor_revision_id,
        }
        content_hash = _digest(payload)
        revision_id = "citation-policy-revision:" + content_hash[:24]
        payload["policy_revision_id"] = revision_id
        existing = self.conn.execute(
            "SELECT content_hash,payload_json FROM citation_policy_revisions WHERE namespace=? AND policy_id=? AND version=?",
            [namespace, policy_id, version],
        ).fetchone()
        if existing:
            if existing[0] != content_hash:
                raise CitationPreservationError(
                    "version_conflict", "policy version has different content"
                )
            return {**_load(existing[1], {}), "idempotent": True}
        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.execute(
                "INSERT INTO citation_policy_revisions VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    revision_id,
                    policy_id,
                    namespace,
                    version,
                    content_hash,
                    _canonical(payload),
                    predecessor_revision_id,
                    principal_id,
                    now,
                ],
            )
            self.conn.execute(
                "INSERT INTO citation_policy_current VALUES (?,?,?) ON CONFLICT(namespace,policy_id) DO UPDATE SET policy_revision_id=excluded.policy_revision_id",
                [namespace, policy_id, revision_id],
            )
            self._audit(
                namespace, "register-policy", revision_id, principal_id, {}, now
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**payload, "idempotent": False}

    def policy(self, namespace, policy_id, *, scopes, revision_id=None):
        _require(scopes, READ_SCOPE)
        if revision_id:
            row = self.conn.execute(
                "SELECT payload_json FROM citation_policy_revisions WHERE namespace=? AND policy_id=? AND policy_revision_id=?",
                [namespace, policy_id, revision_id],
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT r.payload_json FROM citation_policy_current c JOIN citation_policy_revisions r ON r.policy_revision_id=c.policy_revision_id WHERE c.namespace=? AND c.policy_id=?",
                [namespace, policy_id],
            ).fetchone()
        if not row:
            raise CitationPreservationError(
                "policy_not_found", "citation policy was not found"
            )
        return _load(row[0], {})

    def capture(
        self,
        namespace: str,
        policy_id: str,
        citation_id: str,
        source_url: str,
        *,
        principal_id: str,
        scopes: set[str],
        content: str | None,
        media_type="text/html",
        retrieved_at_ms=None,
        redirects: Sequence[str] = (),
        response_metadata: Mapping[str, Any] | None = None,
        locator: Mapping[str, Any] | None = None,
        excerpts: Sequence[Mapping[str, Any]] = (),
        assets: Sequence[Mapping[str, Any]] = (),
        robots_allowed=True,
        license_id=None,
        private_source=False,
        partial=False,
        cancel_requested=False,
        generation=0,
        valid_from_ms=None,
        valid_to_ms=None,
        producer=None,
        provenance=None,
    ):
        _require(scopes, CAPTURE_SCOPE)
        policy = self.policy(namespace, policy_id, scopes={READ_SCOPE})
        now = self.now()
        omissions = []
        if cancel_requested:
            return {
                "contract": SNAPSHOT_CONTRACT,
                "namespace": namespace,
                "citation_id": citation_id,
                "status": "cancelled",
                "snapshot_id": None,
                "blob_hash": None,
                "manifest_hash": _digest([citation_id, "cancelled"]),
                "omissions": [],
                "truncated": False,
            }
        if not robots_allowed and not policy["allow_robots_denied"]:
            omissions.append({"kind": "content", "reason": "robots-restricted"})
        if policy["allowed_licenses"] and license_id not in policy["allowed_licenses"]:
            omissions.append({"kind": "content", "reason": "license-restricted"})
        if private_source and not policy["allow_private"]:
            omissions.append({"kind": "content", "reason": "private-source"})
        raw = (content or "").encode()
        maximum = policy["max_bytes"]
        truncated = len(raw) > maximum
        preserved = (
            None
            if omissions or content is None
            else raw[:maximum].decode(errors="replace")
        )
        if not policy["preserve_excerpts"] and excerpts:
            omissions.append({"kind": "excerpts", "reason": "policy"})
        if not policy["preserve_assets"] and assets:
            omissions.append({"kind": "assets", "reason": "policy"})
        blob_hash = _digest(preserved) if preserved is not None else None
        status = (
            "omitted"
            if omissions and preserved is None
            else (
                "partial"
                if partial or truncated
                else ("missing" if content is None else "captured")
            )
        )
        manifest = {
            "contract": SNAPSHOT_CONTRACT,
            "namespace": namespace,
            "citation_id": citation_id,
            "source_url": source_url,
            "redirects": list(redirects),
            "response_metadata": dict(response_metadata or {}),
            "media_type": media_type,
            "retrieved_at_ms": retrieved_at_ms if retrieved_at_ms is not None else now,
            "locator": dict(locator or {}),
            "excerpts": [dict(v) for v in excerpts]
            if policy["preserve_excerpts"]
            else [],
            "assets": [dict(v) for v in assets] if policy["preserve_assets"] else [],
            "license_id": license_id,
            "private_source": bool(private_source),
            "policy_id": policy_id,
            "policy_revision_id": policy["policy_revision_id"],
            "status": status,
            "blob_hash": blob_hash,
            "byte_size": len(preserved.encode()) if preserved is not None else None,
            "omissions": omissions,
            "truncated": truncated,
            "generation": int(generation),
            "valid_time": {"from_ms": valid_from_ms, "to_ms": valid_to_ms},
            "observed_at_ms": now,
            "producer": dict(producer or {}),
            "provenance": dict(provenance or {}),
        }
        manifest_hash = _digest(manifest)
        snapshot_id = "citation-snapshot:" + manifest_hash[:24]
        manifest.update({"snapshot_id": snapshot_id, "manifest_hash": manifest_hash})
        existing = self.conn.execute(
            "SELECT manifest_json FROM citation_snapshots WHERE snapshot_id=?",
            [snapshot_id],
        ).fetchone()
        if existing:
            return {**_load(existing[0], {}), "idempotent": True}
        self.conn.execute("BEGIN TRANSACTION")
        try:
            if preserved is not None:
                self.conn.execute(
                    "INSERT OR IGNORE INTO citation_blobs VALUES (?,?,?,?,?)",
                    [blob_hash, media_type, len(preserved.encode()), preserved, now],
                )
            self.conn.execute(
                "INSERT INTO citation_snapshots VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    snapshot_id,
                    citation_id,
                    namespace,
                    policy["policy_revision_id"],
                    blob_hash,
                    manifest_hash,
                    _canonical(manifest),
                    principal_id,
                    now,
                ],
            )
            self._audit(
                namespace, "capture", snapshot_id, principal_id, {"status": status}, now
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        duplicates = (
            [
                row[0]
                for row in self.conn.execute(
                    "SELECT snapshot_id FROM citation_snapshots WHERE namespace=? AND blob_hash=? AND snapshot_id<>? ORDER BY snapshot_id LIMIT 50",
                    [namespace, blob_hash, snapshot_id],
                ).fetchall()
            ]
            if blob_hash
            else []
        )
        return {**manifest, "duplicate_snapshot_ids": duplicates, "idempotent": False}

    def snapshot(self, namespace, snapshot_id, *, scopes, include_content=False):
        _require(scopes, READ_SCOPE)
        row = self.conn.execute(
            "SELECT manifest_json,blob_hash FROM citation_snapshots WHERE namespace=? AND snapshot_id=?",
            [namespace, snapshot_id],
        ).fetchone()
        if not row:
            raise CitationPreservationError(
                "snapshot_not_found", "citation snapshot was not found"
            )
        value = _load(row[0], {})
        if include_content and row[1]:
            value["content"] = self.conn.execute(
                "SELECT content FROM citation_blobs WHERE blob_hash=?", [row[1]]
            ).fetchone()[0]
        return value

    def replay_capture(self, namespace, snapshot_id, *, scopes):
        value = self.snapshot(namespace, snapshot_id, scopes=scopes)
        expected = value["manifest_hash"]
        comparable = {
            k: v
            for k, v in value.items()
            if k
            not in {
                "snapshot_id",
                "manifest_hash",
                "duplicate_snapshot_ids",
                "idempotent",
            }
        }
        actual = _digest(comparable)
        return {
            "snapshot_id": snapshot_id,
            "expected_hash": expected,
            "actual_hash": actual,
            "deterministic": expected == actual,
        }

    def verify(
        self,
        namespace,
        citation_id,
        snapshot_id,
        assertion,
        *,
        principal_id,
        scopes,
        expected_excerpt=None,
        contradiction=None,
        locator=None,
        ocr_tolerance=0.85,
    ):
        _require(scopes, WRITE_SCOPE)
        snap = self.snapshot(
            namespace, snapshot_id, scopes={READ_SCOPE}, include_content=True
        )
        if snap["citation_id"] != citation_id:
            raise CitationPreservationError(
                "citation_mismatch", "snapshot belongs to another citation"
            )
        content = snap.get("content")
        located = expected_excerpt or ""
        normalized_content = _words(content or "")
        normalized_expected = _words(located)
        moved = False
        if content is None:
            status = "unverifiable"
        elif contradiction and _words(contradiction) in normalized_content:
            status = "contradicts"
        elif normalized_expected and normalized_expected in normalized_content:
            status = "supports"
            moved = bool(locator and locator != snap.get("locator"))
        elif normalized_expected and SequenceMatcher(
            None, normalized_expected, normalized_content
        ).ratio() >= float(ocr_tolerance):
            status = "ambiguous"
        elif _words(assertion) in normalized_content:
            status = "supports"
            moved = True
        else:
            status = "no-longer-present"
        details = {
            "contract": VERIFICATION_CONTRACT,
            "namespace": namespace,
            "citation_id": citation_id,
            "snapshot_id": snapshot_id,
            "assertion": assertion,
            "status": status,
            "moved_passage": moved,
            "locator": dict(locator or snap.get("locator") or {}),
            "expected_excerpt": expected_excerpt,
            "confidence": 1.0
            if status in {"supports", "contradicts", "no-longer-present"}
            else 0.0
            if status == "unverifiable"
            else float(ocr_tolerance),
            "preserved_blob_hash": snap.get("blob_hash"),
        }
        result_hash = _digest(details)
        verification_id = (
            "citation-verification:"
            + _digest([citation_id, snapshot_id, _digest(assertion), result_hash])[:24]
        )
        details.update({"verification_id": verification_id, "result_hash": result_hash})
        now = self.now()
        self.conn.execute(
            "INSERT OR IGNORE INTO citation_verifications VALUES (?,?,?,?,?,?,?,?,?)",
            [
                verification_id,
                namespace,
                citation_id,
                snapshot_id,
                _digest(assertion),
                result_hash,
                _canonical(details),
                principal_id,
                now,
            ],
        )
        self._audit(
            namespace, "verify", verification_id, principal_id, {"status": status}, now
        )
        return details

    def record_health(
        self,
        namespace,
        citation_id,
        url,
        http_status,
        *,
        principal_id,
        scopes,
        response_title="",
        paywall=False,
        takedown=False,
        checked_at_ms=None,
    ):
        _require(scopes, WRITE_SCOPE)
        indicators = ("not found", "page missing", "404")
        soft404 = int(http_status) == 200 and any(
            v in response_title.casefold() for v in indicators
        )
        status = (
            "takedown"
            if takedown
            else "paywall"
            if paywall
            else "soft-404"
            if soft404
            else "available"
            if 200 <= int(http_status) < 400
            else "unavailable"
        )
        checked = checked_at_ms if checked_at_ms is not None else self.now()
        payload = {
            "contract": HEALTH_CONTRACT,
            "namespace": namespace,
            "citation_id": citation_id,
            "url": url,
            "http_status": int(http_status),
            "status": status,
            "soft_404": soft404,
            "paywall": bool(paywall),
            "takedown": bool(takedown),
            "checked_at_ms": checked,
        }
        health_id = "citation-health:" + _digest(payload)[:24]
        payload["health_id"] = health_id
        self.conn.execute(
            "INSERT OR IGNORE INTO citation_health_checks VALUES (?,?,?,?,?,?,?)",
            [
                health_id,
                namespace,
                citation_id,
                checked,
                status,
                _canonical(payload),
                principal_id,
            ],
        )
        return payload

    def preview_repair(
        self,
        namespace,
        policy_id,
        citation_id,
        snapshot_id,
        candidates: Sequence[Mapping[str, Any]],
        *,
        scopes,
        limit=20,
    ):
        _require(scopes, READ_SCOPE)
        policy = self.policy(namespace, policy_id, scopes=scopes)
        snap = self.snapshot(namespace, snapshot_id, scopes=scopes)
        allowed = []
        for raw in list(candidates)[: _bounded(limit, 20)]:
            item = dict(raw)
            archive = str(item.get("archive") or "")
            approved = any(
                archive == v or archive.startswith(v)
                for v in policy["approved_archives"]
            )
            content_hash = item.get("content_hash") or (
                _digest(item["content"]) if item.get("content") is not None else None
            )
            exact = bool(snap.get("blob_hash") and content_hash == snap["blob_hash"])
            allowed.append(
                {
                    "archive": archive,
                    "url": item.get("url"),
                    "approved": approved,
                    "content_hash": content_hash,
                    "exact_content_match": exact,
                    "eligible": approved and exact and snap["status"] != "omitted",
                    "warning": None
                    if approved and exact
                    else "archive-mismatch"
                    if approved
                    else "archive-not-approved",
                }
            )
        return {
            "contract": HEALTH_CONTRACT,
            "namespace": namespace,
            "citation_id": citation_id,
            "snapshot_id": snapshot_id,
            "policy_revision_id": policy["policy_revision_id"],
            "candidates": allowed,
            "preview_hash": _digest(
                [citation_id, snapshot_id, policy["policy_revision_id"], allowed]
            ),
            "original_unchanged": True,
        }

    def accept_repair(
        self, namespace, preview, candidate_index, *, principal_id, scopes
    ):
        _require(scopes, REPAIR_SCOPE)
        candidates = preview.get("candidates", [])
        index = int(candidate_index)
        if (
            index < 0
            or index >= len(candidates)
            or not candidates[index].get("eligible")
        ):
            raise CitationPreservationError(
                "repair_not_eligible",
                "repair requires an approved exact-content archive",
            )
        candidate = candidates[index]
        repair_id = (
            "citation-repair:"
            + _digest([namespace, preview["citation_id"], candidate["url"]])[:24]
        )
        payload = {
            "contract": HEALTH_CONTRACT,
            "namespace": namespace,
            "repair_id": repair_id,
            "citation_id": preview["citation_id"],
            "snapshot_id": preview["snapshot_id"],
            "archive_url": candidate["url"],
            "content_hash": candidate["content_hash"],
            "status": "equivalent-copy-recorded",
            "original_unchanged": True,
            "preview_hash": preview["preview_hash"],
        }
        now = self.now()
        self.conn.execute(
            "INSERT OR IGNORE INTO citation_repairs VALUES (?,?,?,?,?,?,?,?,?)",
            [
                repair_id,
                namespace,
                preview["citation_id"],
                candidate["url"],
                preview["preview_hash"],
                payload["status"],
                _canonical(payload),
                principal_id,
                now,
            ],
        )
        self._audit(
            namespace,
            "repair",
            repair_id,
            principal_id,
            {"original_unchanged": True},
            now,
        )
        return payload

    def status(self, namespace, citation_id, *, scopes, limit=100):
        _require(scopes, READ_SCOPE)
        snapshots = [
            _load(r[0], {})
            for r in self.conn.execute(
                "SELECT manifest_json FROM citation_snapshots WHERE namespace=? AND citation_id=? ORDER BY created_at_ms DESC LIMIT ?",
                [namespace, citation_id, _bounded(limit)],
            ).fetchall()
        ]
        health = [
            _load(r[0], {})
            for r in self.conn.execute(
                "SELECT payload_json FROM citation_health_checks WHERE namespace=? AND citation_id=? ORDER BY checked_at_ms DESC LIMIT ?",
                [namespace, citation_id, _bounded(limit)],
            ).fetchall()
        ]
        repairs = [
            _load(r[0], {})
            for r in self.conn.execute(
                "SELECT payload_json FROM citation_repairs WHERE namespace=? AND citation_id=? ORDER BY created_at_ms DESC LIMIT ?",
                [namespace, citation_id, _bounded(limit)],
            ).fetchall()
        ]
        return {
            "citation_id": citation_id,
            "snapshots": snapshots,
            "health": health,
            "repairs": repairs,
        }

    def export(self, namespace, citation_ids: Sequence[str], *, scopes, limit=100):
        _require(scopes, READ_SCOPE)
        ids = list(citation_ids)[: _bounded(limit, 100)]
        items = []
        policies = {}
        for cid in ids:
            state = self.status(namespace, cid, scopes=scopes)
            for snap in state["snapshots"]:
                policy = self.policy(
                    namespace,
                    snap["policy_id"],
                    revision_id=snap["policy_revision_id"],
                    scopes=scopes,
                )
                policies[policy["policy_revision_id"]] = policy
            verifications = [
                _load(r[0], {})
                for r in self.conn.execute(
                    "SELECT payload_json FROM citation_verifications WHERE namespace=? AND citation_id=? ORDER BY created_at_ms",
                    [namespace, cid],
                ).fetchall()
            ]
            items.append({**state, "verifications": verifications})
        payload = {
            "contract": EXPORT_CONTRACT,
            "namespace": namespace,
            "citation_ids": ids,
            "items": items,
            "policies": [policies[k] for k in sorted(policies)],
            "dependency_complete": True,
        }
        payload["export_hash"] = _digest(payload)
        return payload
