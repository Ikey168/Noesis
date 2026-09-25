"""Read-only upgrade impact and guarded application of a reviewed source-pack preview."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from src.ingestion.source_packs import (
    SourcePackConformance, SourcePackError, SourcePackStore, validate_source_pack,
)

IMPACT_CONTRACT = "noesis-source-pack-upgrade-impact-v1"
RECEIPT_CONTRACT = "noesis-source-pack-upgrade-receipt-v1"

_DDL = """
CREATE TABLE IF NOT EXISTS source_pack_upgrade_receipts(
 apply_id TEXT PRIMARY KEY,pack_id TEXT NOT NULL,principal_id TEXT NOT NULL,
 apply_key TEXT NOT NULL,request_hash TEXT NOT NULL,receipt_json TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL,UNIQUE(pack_id,principal_id,apply_key));
"""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _tables(conn):
    return {row[0] for row in conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}


class SourcePackUpgradeStore:
    def __init__(self, conn: Any, *, initialize=False, now=None, root=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _read_access(principal_id, scopes):
        if not principal_id or "operator" not in scopes and "knowledge:read" not in scopes:
            raise SourcePackError("unauthorized", "current source-pack read access required")

    @staticmethod
    def _bounded_candidate(candidate):
        try:
            size = len(_json(candidate).encode())
        except (TypeError, ValueError) as exc:
            raise SourcePackError("invalid_manifest", "candidate must be JSON") from exc
        if size > 2_000_000:
            raise SourcePackError("invalid_manifest", "candidate exceeds 2 MiB")

    @staticmethod
    def _write_access(principal_id, scopes):
        if not principal_id or "operator" not in scopes:
            raise SourcePackError("unauthorized", "operator scope is required for source-pack upgrade")

    def _retained(self, pack_id, version, expected_hash=None):
        row = self.conn.execute(
            "SELECT manifest_hash,manifest_json FROM source_pack_versions WHERE pack_id=? AND version=?",
            [pack_id, version],
        ).fetchone()
        if not row:
            return "missing_retained_version"
        if expected_hash and expected_hash not in {row[0], _hash(json.loads(row[1]))}:
            return "pinned_hash_mismatch"
        return "retained"

    @staticmethod
    def _visible(owner, namespace, principal_id, scopes, *, kind, state=None):
        if "operator" in scopes:
            return True
        required = "knowledge:reports:read" if kind == "report" else "knowledge:projects:read"
        if owner != principal_id or required not in scopes:
            return False
        state = state or {}
        if kind == "report":
            namespaces = {namespace, *(state.get("content", {}).get("snapshot", {}).get("generations", {}))}
            namespaces.update(dep.get("namespace") for section in state.get("content", {}).get("sections", [])
                              for assertion in section.get("assertions", []) for dep in assertion.get("dependencies", []))
            domains = set()
        else:
            scope = state.get("definition", {}).get("scope", {}) if kind == "template" else state.get("scope", {})
            namespaces = {namespace, *(scope.get("namespaces") or [])}
            domains = set(scope.get("domains") or [])
        return (all(f"namespace:{ns}:read" in scopes or f"namespace:{ns}:write" in scopes for ns in namespaces)
                and all(f"domain:{domain}:read" in scopes for domain in domains))

    def _impacts(self, pack_id, principal_id, scopes):
        tables = _tables(self.conn)
        effects = []
        inaccessible = {"templates": 0, "projects": 0, "reports": 0, "schedules": 0}
        if {"investigation_templates", "investigation_template_revisions"} <= tables:
            rows = self.conn.execute(
                "SELECT t.template_id,t.namespace,t.owner,r.revision,r.state_json FROM investigation_templates t "
                "JOIN investigation_template_revisions r ON r.template_id=t.template_id "
                "ORDER BY t.namespace,t.template_id,r.revision LIMIT 10001"
            ).fetchall()
            if len(rows) > 10000:
                raise SourcePackError("impact_limit", "too many saved templates to assess")
            for template_id, namespace, owner, revision, encoded in rows:
                state = json.loads(encoded)
                pins = [v for v in state["definition"].get("source_packs", []) if v.get("pack_id") == pack_id]
                if not pins:
                    continue
                if not self._visible(owner, namespace, principal_id, scopes, kind="template", state=state):
                    inaccessible["templates"] += 1
                    continue
                for pin in pins:
                    retained = self._retained(pack_id, pin["version"])
                    effects.append({"kind": "template", "id": template_id, "revision": int(revision), "namespace": namespace,
                                    "pinned_version": pin["version"], "retained_status": retained,
                                    "reproducibility": "pinned_manifest_available" if retained == "retained" else retained,
                                    "future_execution": "requires_source_selection_and_mapping_review"})
        if {"research_projects", "research_project_revisions"} <= tables:
            rows = self.conn.execute(
                "SELECT p.project_id,p.namespace,p.owner,r.revision,r.content_json FROM research_projects p "
                "JOIN research_project_revisions r ON r.project_id=p.project_id "
                "ORDER BY p.namespace,p.project_id,r.revision LIMIT 10001"
            ).fetchall()
            if len(rows) > 10000:
                raise SourcePackError("impact_limit", "too many saved projects to assess")
            for project_id, namespace, owner, revision, encoded in rows:
                state = json.loads(encoded)
                pins = [v for v in (state.get("template_origin") or {}).get("source_packs", [])
                        if v.get("pack_id") == pack_id]
                if not pins:
                    continue
                if not self._visible(owner, namespace, principal_id, scopes, kind="project", state=state):
                    inaccessible["projects"] += 1
                    continue
                for pin in pins:
                    retained = self._retained(pack_id, pin["version"], pin.get("manifest_sha256"))
                    effects.append({"kind": "project", "id": project_id, "revision": int(revision), "namespace": namespace,
                                    "pinned_version": pin["version"], "retained_status": retained,
                                    "reproducibility": "pinned_manifest_available" if retained == "retained" else retained,
                                    "future_execution": "requires_current_pack_review"})
        if "source_pack_schedules" in tables:
            row = self.conn.execute(
                "SELECT schedule_json,enabled,next_run_at_ms FROM source_pack_schedules WHERE pack_id=?",
                [pack_id],
            ).fetchone()
            if row:
                if "operator" in scopes:
                    effects.append({"kind": "schedule", "id": pack_id, "namespace": None,
                                    "schedule_hash": _hash(json.loads(row[0])), "enabled": bool(row[1]),
                                    "next_run_at_ms": int(row[2]),
                                    "future_execution": "requires_source_credential_license_and_mapping_review",
                                    "migration": "unchanged_unless_explicitly_selected"})
                else:
                    inaccessible["schedules"] += 1
        if {"authored_reports", "authored_report_revisions", "document_revision_records"} <= tables:
            rows = self.conn.execute(
                "SELECT p.report_id,p.namespace,p.owner,r.revision,r.content_json FROM authored_reports p "
                "JOIN authored_report_revisions r ON r.report_id=p.report_id "
                "ORDER BY p.namespace,p.report_id,r.revision LIMIT 10001"
            ).fetchall()
            if len(rows) > 10000:
                raise SourcePackError("impact_limit", "too many authored reports to assess")
            examined_dependencies = 0
            for report_id, namespace, owner, revision, encoded in rows:
                state = json.loads(encoded)
                content = state.get("content") or {}
                dependencies = [d for section in content.get("sections", [])
                                for assertion in section.get("assertions", [])
                                for d in assertion.get("dependencies", [])]
                examined_dependencies += len(dependencies)
                if examined_dependencies > 50_000:
                    raise SourcePackError("impact_limit", "too many report dependencies to assess")
                matching = []
                unsupported = set()
                unresolved = []
                for dep in dependencies:
                    if dep.get("kind") != "source":
                        unsupported.add(str(dep.get("kind")))
                        continue
                    source_id = dep.get("locator", {}).get("document_id") or dep.get("id")
                    revision_id = dep.get("locator", {}).get("revision_id") or dep.get("revision")
                    row = self.conn.execute(
                        "SELECT pack_id,payload_json FROM document_revision_records "
                        "WHERE document_id=? AND revision_id=? AND committed_watermark IS NOT NULL",
                        [source_id, revision_id],
                    ).fetchone()
                    if not row:
                        unresolved.append({"document_id": source_id, "revision_id": revision_id})
                    if row and row[0] == pack_id:
                        payload = json.loads(row[1])
                        metadata = payload.get("metadata") or {}
                        version = metadata.get("source_pack_version")
                        matching.append({"document_id": source_id, "revision_id": revision_id,
                                         "pack_version": version,
                                         "retained_status": self._retained(pack_id, version) if version else "version_unrecorded"})
                if not matching:
                    continue
                if not self._visible(owner, namespace, principal_id, scopes, kind="report", state=state) or (
                    "operator" not in scopes and any(f"document:{v['document_id']}:read" not in scopes for v in matching)
                ):
                    inaccessible["reports"] += 1
                    continue
                effects.append({"kind": "report", "id": report_id, "revision": int(revision), "namespace": namespace,
                                "pinned_sources": matching,
                                "reproducibility": "pinned_sources_available" if all(v["retained_status"] == "retained" for v in matching)
                                else "missing_retained_dependency",
                                "unsupported_dependency_types": sorted(unsupported),
                                "unresolved_source_dependencies": unresolved,
                                "citation_action": "none; existing citations remain pinned"})
        return sorted(effects, key=lambda item: (item["kind"], item["namespace"] or "", item["id"], item.get("revision", 0))), inaccessible

    def preview_impact(self, candidate: Mapping[str, Any], *, principal_id, scopes,
                       limit=100, offset=0):
        self._read_access(principal_id, scopes)
        self._bounded_candidate(candidate)
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise SourcePackError("invalid_page", "impact page must be bounded to 100 items")
        preview = SourcePackStore(self.conn, initialize=False).preview_upgrade(candidate)
        effects, inaccessible = self._impacts(preview["pack_id"], principal_id, scopes)
        disclosed = inaccessible if "operator" in scopes else {"withheld": True}
        impact_hash = _hash([preview["preview_hash"], effects, disclosed])
        return {"contract": IMPACT_CONTRACT, "preview": preview,
                "impact_hash": impact_hash, "total": len(effects),
                "effects": effects[offset:offset + limit], "offset": offset,
                "next_offset": offset + limit if offset + limit < len(effects) else None,
                "inaccessible": disclosed,
                "retained_version_count": self.conn.execute(
                    "SELECT count(*) FROM source_pack_versions WHERE pack_id=?", [preview["pack_id"]]
                ).fetchone()[0],
                "applied": False}

    def apply(self, candidate: Mapping[str, Any], *, preview_hash, impact_hash, apply_key,
              principal_id, scopes, accepted_license_sources: Sequence[str] = (),
              migrate_schedule=False, secret_available: Callable[[str], bool] | None = None,
              dns_resolver: Callable[[str], Sequence[str]] | None = None):
        self._write_access(principal_id, scopes)
        self._bounded_candidate(candidate)
        if not isinstance(apply_key, str) or not 1 <= len(apply_key) <= 200:
            raise SourcePackError("invalid_request", "bounded upgrade apply key required")
        if not isinstance(accepted_license_sources, (list, tuple)) or len(accepted_license_sources) > 100 or (
            len(set(accepted_license_sources)) != len(accepted_license_sources) or
            any(not isinstance(v, str) or not v for v in accepted_license_sources)
        ):
            raise SourcePackError("invalid_request", "bounded explicit license acceptance list required")
        value = validate_source_pack(candidate)
        if set(accepted_license_sources) - {v["source_id"] for v in value["sources"]}:
            raise SourcePackError("invalid_request", "license acceptance names an unknown candidate source")
        schedule = (candidate.get("defaults") or {}).get("schedule") if migrate_schedule else None
        if migrate_schedule and not isinstance(schedule, Mapping):
            raise SourcePackError("invalid_schedule", "candidate default schedule required for explicit migration")
        apply_id = "source-pack-upgrade:" + _hash([value["pack_id"], principal_id, apply_key])[:24]
        request_hash = _hash([value["manifest_hash"], preview_hash, impact_hash,
                              sorted(accepted_license_sources), bool(migrate_schedule)])
        self.conn.execute(_DDL)
        prior = self.conn.execute(
            "SELECT request_hash,receipt_json FROM source_pack_upgrade_receipts WHERE apply_id=?",
            [apply_id],
        ).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise SourcePackError("apply_conflict", "apply key is bound to a different upgrade")
            return {**json.loads(prior[1]), "idempotent": True}
        # Existing offline conformance checks validate fixture provenance and
        # mapping output without any provider request.
        conformance = SourcePackConformance(self.root).offline(value)
        if not conformance["valid"]:
            raise SourcePackError("fixture_drift", "candidate offline conformance failed")
        store = SourcePackStore(self.conn, initialize=False)
        from src.ingestion.source_pack_runtime import SourcePackRuntime

        try:
            self.conn.execute("BEGIN")
            impact = self.preview_impact(candidate, principal_id=principal_id, scopes=scopes)
            preview = impact["preview"]
            if preview["preview_hash"] != preview_hash or impact["impact_hash"] != impact_hash:
                raise SourcePackError("stale_preview", "installed pack or dependent impact changed since preview")
            if preview["idempotent"] or preview["candidate_version"] == preview["installed_version"]:
                raise SourcePackError("no_upgrade", "candidate does not create a new immutable version")
            current = self.conn.execute(
                "SELECT version FROM source_pack_current WHERE pack_id=?", [value["pack_id"]]
            ).fetchone()
            if not current or current[0] != preview["installed_version"]:
                raise SourcePackError("stale_preview", "installed pack changed since preview")
            existing = self.conn.execute(
                "SELECT manifest_hash FROM source_pack_versions WHERE pack_id=? AND version=?",
                [value["pack_id"], value["version"]],
            ).fetchone()
            if existing and existing[0] != value["manifest_hash"]:
                raise SourcePackError("immutable_version", "candidate version has different retained content")
            if not existing:
                self.conn.execute("INSERT INTO source_pack_versions VALUES (?,?,?,?,?,?)",
                                  [value["pack_id"], value["version"], value["manifest_hash"],
                                   _json(value), principal_id, self.now()])
            changed = self.conn.execute(
                "UPDATE source_pack_current SET version=?,updated_at_ms=? WHERE pack_id=? AND version=? RETURNING version",
                [value["version"], self.now(), value["pack_id"], preview["installed_version"]],
            ).fetchone()
            if not changed:
                raise SourcePackError("stale_preview", "installed pack changed during apply")
            runtime = SourcePackRuntime(self.conn, initialize=False, now=self.now)
            for source_id in accepted_license_sources:
                runtime.accept_license(value["pack_id"], source_id, principal_id=principal_id)
            preflight = runtime.preflight(
                {"pack_id": value["pack_id"], "run_key": "upgrade-preflight:" + apply_id,
                 "operation": "search", "source_ids": [], "network": "disabled"},
                secret_available=secret_available, dns_resolver=dns_resolver,
            )
            if not preflight["sources"] or any(not source["ready"] for source in preflight["sources"]):
                raise SourcePackError("preflight_failed", "candidate credential, license or network preflight failed",
                                      failures={source["source_id"]: source["failures"] for source in preflight["sources"] if source["failures"]})
            old_schedule = self.conn.execute(
                "SELECT schedule_json,enabled FROM source_pack_schedules WHERE pack_id=?", [value["pack_id"]]
            ).fetchone()
            old_schedule_hash = _hash([old_schedule[0], old_schedule[1]]) if old_schedule else None
            if migrate_schedule:
                runtime.set_schedule(value["pack_id"], schedule, principal_id=principal_id,
                                     enabled=bool(old_schedule[1]) if old_schedule else True)
            new_schedule = self.conn.execute(
                "SELECT schedule_json,enabled FROM source_pack_schedules WHERE pack_id=?", [value["pack_id"]]
            ).fetchone()
            new_schedule_hash = _hash([new_schedule[0], new_schedule[1]]) if new_schedule else None
            now = self.now()
            receipt = {"contract": RECEIPT_CONTRACT, "apply_id": apply_id,
                       "pack_id": value["pack_id"], "applied_by": principal_id,
                       "installed_version": preview["installed_version"],
                       "installed_hash": preview["installed_hash"],
                       "candidate_version": value["version"], "candidate_hash": value["manifest_hash"],
                       "preview_hash": preview_hash, "impact_hash": impact_hash,
                       "preflight": preflight, "conformance_hash": _hash(conformance),
                       "retained_old_version": True, "schedule_migrated": bool(migrate_schedule),
                       "old_schedule_hash": old_schedule_hash, "new_schedule_hash": new_schedule_hash,
                       "applied_at_ms": now}
            receipt["receipt_hash"] = _hash(receipt)
            self.conn.execute("INSERT INTO source_pack_upgrade_receipts VALUES (?,?,?,?,?,?,?)",
                              [apply_id, value["pack_id"], principal_id, apply_key, request_hash,
                               _json(receipt), now])
            store._audit(value["pack_id"], principal_id, "apply-upgrade",
                         {"apply_id": apply_id, "receipt_hash": receipt["receipt_hash"]}, now)
            self.conn.execute("COMMIT")
            return {**receipt, "idempotent": False}
        except Exception as exc:
            self.conn.execute("ROLLBACK")
            if isinstance(exc, SourcePackError):
                raise
            import duckdb
            if isinstance(exc, (duckdb.TransactionException, duckdb.ConstraintException)):
                raise SourcePackError("stale_preview", "concurrent source-pack upgrade changed the reviewed state") from exc
            raise

    def inspect_receipt(self, pack_id, apply_key, *, principal_id, scopes):
        self._write_access(principal_id, scopes)
        apply_id = "source-pack-upgrade:" + _hash([pack_id, principal_id, apply_key])[:24]
        row = self.conn.execute(
            "SELECT receipt_json FROM source_pack_upgrade_receipts WHERE apply_id=? AND pack_id=?",
            [apply_id, pack_id],
        ).fetchone()
        if not row:
            raise SourcePackError("receipt_unavailable", "source-pack upgrade receipt is unavailable")
        receipt = json.loads(row[0])
        current = SourcePackStore(self.conn, initialize=False).status(pack_id)
        return {**receipt, "current_matches_candidate": current["manifest_hash"] == receipt["candidate_hash"]}
