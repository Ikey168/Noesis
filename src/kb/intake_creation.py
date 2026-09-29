"""Creation projects that point to authoritative, versioned authored reports.

The project stores purpose, audience, criteria and review state. Report content
and its evidence dependencies remain in AuthoredReportStore; a finished project
is an author-accepted result, never a publishing authorization.
"""

from __future__ import annotations

import json
import time
from typing import Any

from src.kb.authored_reports import AuthoredReportStore
from src.kb.intake_modes import (
    IntakeError,
    IntakeStore,
    _bounded,
    _hash,
    _json,
    _plugin_links,
    _plugin_ref_accessible,
    _reference,
    _text,
    _workspace_links,
)

CONTRACT = "noesis-intake-creation-v1"
BUILD_CONTRACT = "noesis-intake-creation-build-v1"
SUPPORTED_TYPES = {"post", "documentation", "teaching_material"}
# Artifact types produced from the finished authored report by an explicit,
# deployment-configured build adapter. The public server configures none.
BUILD_TYPES = {"slide_deck", "static_site", "code_package", "dataset_release"}
_DDL = """
CREATE TABLE IF NOT EXISTS intake_creation_projects (
  project_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
  request_hash TEXT NOT NULL, revision BIGINT NOT NULL, content_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intake_creation_revisions (
  project_id TEXT NOT NULL, revision BIGINT NOT NULL, content_json TEXT NOT NULL,
  PRIMARY KEY(project_id,revision)
);
CREATE TABLE IF NOT EXISTS intake_creation_commands (
  project_id TEXT NOT NULL, command_key TEXT NOT NULL, request_hash TEXT NOT NULL,
  revision BIGINT NOT NULL, PRIMARY KEY(project_id,command_key)
);
CREATE TABLE IF NOT EXISTS intake_creation_builds (
  namespace TEXT NOT NULL, owner TEXT NOT NULL, idempotency_key TEXT NOT NULL,
  project_id TEXT NOT NULL, project_revision BIGINT NOT NULL, request_hash TEXT NOT NULL,
  status TEXT NOT NULL, receipt_json TEXT, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, owner, idempotency_key)
);
"""


class IntakeCreationStore:
    def __init__(self, conn: Any, *, initialize=True, now=None, build_adapters=None):
        self.build_adapters = dict(build_adapters or {})
        if set(self.build_adapters) - BUILD_TYPES or any(
            not callable(adapter) for adapter in self.build_adapters.values()
        ):
            raise IntakeError("invalid_adapter", "only callable build adapters for supported types can be configured")
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _state(self, namespace: str, project_id: str, revision: int | None = None) -> dict:
        row = self.conn.execute(
            "SELECT r.content_json FROM intake_creation_projects p "
            "JOIN intake_creation_revisions r ON p.project_id=r.project_id "
            "WHERE p.namespace=? AND p.project_id=? AND r.revision=coalesce(?,p.revision)",
            [namespace, project_id, revision],
        ).fetchone()
        if row is None:
            raise IntakeError("creation_not_found", "creation project revision is unavailable")
        return json.loads(row[0])

    @staticmethod
    def _access(state: dict, principal_id: str, scopes: set[str], *, write=False) -> None:
        IntakeStore._authorize(state, principal_id, scopes, write=write)
        for ref in state["inputs"]:
            _reference(ref, state["namespace"], scopes)
        if any(
            not _plugin_ref_accessible(link, state["namespace"], scopes)
            for link in state.get("plugin_links", [])
        ):
            raise IntakeError("unauthorized", "current access to plugin-linked creation sources is required")

    def _report(self, state: dict, principal_id: str, scopes: set[str], *, current=False) -> dict:
        link = state.get("report")
        if link is None:
            raise IntakeError("report_missing", "attach a versioned authored report first")
        report = AuthoredReportStore(self.conn, initialize=False).inspect(
            state["namespace"], link["id"], revision=None if current else link["revision"],
            principal_id=principal_id, scopes=scopes,
        )
        if current and report["revision"] != link["revision"]:
            raise IntakeError("stale_report", "report changed; review and attach its new revision")
        return report

    def create(
        self, namespace: str, request_key: str, *, title: str, audience: str,
        artifact_type: str, purpose: str, criteria: list[str],
        inputs: list[dict] | None, workspace_links: list[dict] | None,
        plugin_links: list[dict] | None = None,
        principal_id: str, scopes: set[str],
    ) -> dict:
        namespace = _text(namespace, "namespace", limit=128)
        request_key = _text(request_key, "request_key", limit=256)
        if artifact_type not in SUPPORTED_TYPES | BUILD_TYPES:
            raise IntakeError(
                "unsupported_artifact",
                "only post, documentation and teaching material use the authored-report "
                "adapter; build adapter types are " + ", ".join(sorted(BUILD_TYPES)),
            )
        if not isinstance(criteria, list) or not 1 <= len(criteria) <= 30:
            raise IntakeError("invalid_creation", "declare one to 30 acceptance criteria")
        reviewed_criteria = [_text(value, "criterion", limit=1000) for value in criteria]
        if len(set(reviewed_criteria)) != len(reviewed_criteria):
            raise IntakeError("invalid_creation", "acceptance criteria must be unique")
        if not isinstance(inputs, (list, type(None))) or len(inputs or []) > 100:
            raise IntakeError("invalid_creation", "at most 100 versioned inputs are supported")
        content = {
            "contract": CONTRACT,
            "project_id": "creation:" + _hash([namespace, principal_id, request_key])[:32],
            "namespace": namespace, "owner": principal_id, "revision": 1,
            "status": "draft", "title": _text(title, "title", limit=1000),
            "audience": _text(audience, "audience", limit=1000),
            "artifact_type": artifact_type, "purpose": _text(purpose, "purpose", limit=5000),
            "criteria": reviewed_criteria,
            "inputs": [_reference(ref, namespace, scopes) for ref in inputs or []],
            "workspace_links": _workspace_links(workspace_links or []),
            "report": None, "review": None, "history": [],
        }
        normalized_plugin_links = _plugin_links(plugin_links or [], namespace, scopes)
        if normalized_plugin_links:
            content["plugin_links"] = normalized_plugin_links
        self._access(content, principal_id, scopes, write=True)
        _bounded(content)
        digest = _hash(content)
        prior = self.conn.execute(
            "SELECT request_hash FROM intake_creation_projects WHERE project_id=?",
            [content["project_id"]],
        ).fetchone()
        if prior:
            if prior[0] != digest:
                raise IntakeError("idempotency_conflict", "request_key identifies another creation project")
            current = self.inspect(namespace, content["project_id"],
                                   principal_id=principal_id, scopes=scopes)
            return {**current, "idempotent": True}
        content["created_at_ms"] = content["updated_at_ms"] = self.now()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("INSERT INTO intake_creation_projects VALUES (?,?,?,?,?,?)", [
                content["project_id"], namespace, principal_id, digest, 1, _json(content),
            ])
            self.conn.execute("INSERT INTO intake_creation_revisions VALUES (?,?,?)", [
                content["project_id"], 1, _json(content),
            ])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**content, "idempotent": False}

    def inspect(
        self, namespace: str, project_id: str, *, principal_id: str,
        scopes: set[str], revision: int | None = None,
    ) -> dict:
        current = self._state(namespace, project_id)
        self._access(current, principal_id, scopes)
        value = current if revision is None else self._state(namespace, project_id, revision)
        self._access(value, principal_id, scopes)
        if value["report"] is not None:
            self._report(value, principal_id, scopes)
        return value

    def command(
        self, namespace: str, project_id: str, command_key: str, *,
        expected_revision: int, action: str, payload: dict | None,
        principal_id: str, scopes: set[str],
    ) -> dict:
        command_key = _text(command_key, "command_key", limit=256)
        if type(expected_revision) is not int or expected_revision < 1:
            raise IntakeError("invalid_revision", "expected_revision must be positive")
        if action not in {"attach_report", "review", "finish", "reopen"}:
            raise IntakeError("invalid_action", "unsupported creation command")
        payload = _bounded(payload or {}, limit=40_000)
        if not isinstance(payload, dict):
            raise IntakeError("invalid_input", "payload must be an object")
        digest = _hash([action, payload])
        self.conn.execute("BEGIN")
        try:
            value = self._state(namespace, project_id)
            self._access(value, principal_id, scopes, write=True)
            prior = self.conn.execute(
                "SELECT request_hash,revision FROM intake_creation_commands "
                "WHERE project_id=? AND command_key=?", [project_id, command_key],
            ).fetchone()
            if prior:
                if prior[0] != digest:
                    raise IntakeError("idempotency_conflict", "command_key identifies another creation action")
                replay = self._state(namespace, project_id, int(prior[1]))
                self._access(replay, principal_id, scopes)
                if replay["report"] is not None:
                    self._report(replay, principal_id, scopes)
                self.conn.execute("COMMIT")
                return {**replay, "idempotent": True}
            if value["revision"] != expected_revision:
                raise IntakeError("revision_conflict", "creation changed; inspect before retry")
            if action == "attach_report":
                if value["status"] == "finished" or set(payload) != {"report_id", "revision"}:
                    raise IntakeError("invalid_status", "attach a report while the project is open")
                report_id = _text(payload["report_id"], "report_id", limit=256)
                revision = payload["revision"]
                if type(revision) is not int or revision < 1:
                    raise IntakeError("invalid_revision", "report revision must be positive")
                report = AuthoredReportStore(self.conn, initialize=False).inspect(
                    namespace, report_id, principal_id=principal_id, scopes=scopes,
                )
                if report["revision"] != revision:
                    raise IntakeError("stale_report", "attach the current report revision")
                value["report"] = {"id": report_id, "revision": revision}
                value["review"] = None
                value["status"] = "draft"
            elif action == "review":
                if value["status"] == "finished" or set(payload) != {"checks", "notes"}:
                    raise IntakeError("invalid_status", "review an open project with all criteria")
                self._report(value, principal_id, scopes, current=True)
                checks = payload["checks"]
                if not isinstance(checks, dict) or set(checks) != set(value["criteria"]) or any(
                    type(result) is not bool for result in checks.values()
                ):
                    raise IntakeError("invalid_review", "review every declared criterion as pass or fail")
                value["review"] = {"checks": checks, "notes": _text(payload["notes"], "notes", limit=5000),
                                   "report_revision": value["report"]["revision"], "at_ms": self.now(),
                                   "basis": "author_reported"}
                value["status"] = "review"
            elif action == "finish":
                if payload or value["status"] != "review" or value["review"] is None:
                    raise IntakeError("invalid_status", "review the report before finishing")
                self._report(value, principal_id, scopes, current=True)
                if not all(value["review"]["checks"].values()):
                    raise IntakeError("criteria_unmet", "resolve failed acceptance checks before finishing")
                value["status"] = "finished"
                value["finished_at_ms"] = self.now()
            else:
                if payload or value["status"] != "finished":
                    raise IntakeError("invalid_status", "only a finished project can reopen")
                value["status"] = "draft"
                value["review"] = None
            value["revision"] += 1
            value["updated_at_ms"] = self.now()
            value["history"] = [*value["history"], {
                "action": action, "revision": value["revision"], "at_ms": value["updated_at_ms"],
            }]
            _bounded(value)
            changed = self.conn.execute(
                "UPDATE intake_creation_projects SET revision=?,content_json=? "
                "WHERE project_id=? AND revision=? RETURNING revision",
                [value["revision"], _json(value), project_id, expected_revision],
            ).fetchone()
            if not changed:
                raise IntakeError("revision_conflict", "concurrent creation update")
            self.conn.execute("INSERT INTO intake_creation_revisions VALUES (?,?,?)", [
                project_id, value["revision"], _json(value),
            ])
            self.conn.execute("INSERT INTO intake_creation_commands VALUES (?,?,?,?)", [
                project_id, command_key, digest, value["revision"],
            ])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**value, "idempotent": False}

    def export(self, namespace: str, project_id: str, *, principal_id: str, scopes: set[str]) -> dict:
        value = self.inspect(namespace, project_id, principal_id=principal_id, scopes=scopes)
        if value["status"] != "finished":
            raise IntakeError("not_finished", "finish the reviewed project before exporting")
        self._report(value, principal_id, scopes, current=True)
        report = AuthoredReportStore(self.conn, initialize=False).export(
            namespace, value["report"]["id"], revision=value["report"]["revision"],
            principal_id=principal_id, scopes=scopes,
        )
        revisions = [json.loads(row[0]) for row in self.conn.execute(
            "SELECT content_json FROM intake_creation_revisions WHERE project_id=? ORDER BY revision",
            [project_id],
        ).fetchall()]
        for revision in revisions:
            self._access(revision, principal_id, scopes)
        builds = [json.loads(row[0]) for row in self.conn.execute(
            "SELECT receipt_json FROM intake_creation_builds WHERE namespace=? AND project_id=? "
            "AND project_revision=? AND status='completed' ORDER BY created_at_ms",
            [namespace, project_id, value["revision"]],
        ).fetchall()]
        payload = {"contract": "noesis-intake-creation-export-v1", "project": value,
                   "revisions": revisions, "authored_report_export": report,
                   **({"builds": builds} if builds else {}),
                   "publication_authorized": False}
        return {**payload, "sha256": _hash(payload)}

    def build(
        self, namespace: str, project_id: str, *, expected_revision: int,
        idempotency_key: str, principal_id: str, scopes: set[str],
    ) -> dict:
        """Run the configured build adapter once for a finished project revision.

        The adapter receives the verified authored-report export and must return
        ``artifact_locator``, ``artifact_sha256`` and ``adapter_version``. A
        build is not publication: the receipt keeps ``publication_authorized``
        false. Without a configured adapter the result is ``unavailable``.
        """

        idempotency_key = _text(idempotency_key, "idempotency_key", limit=256)
        request_hash = _hash([namespace, principal_id, project_id, expected_revision])
        prior = self.conn.execute(
            "SELECT request_hash,status,receipt_json FROM intake_creation_builds "
            "WHERE namespace=? AND owner=? AND idempotency_key=?",
            [namespace, principal_id, idempotency_key],
        ).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise IntakeError("idempotency_conflict", "idempotency key identifies another build")
            return {"contract": BUILD_CONTRACT, "status": prior[1],
                    "receipt": json.loads(prior[2]) if prior[2] else None, "idempotent": True}
        project = self.inspect(namespace, project_id, principal_id=principal_id, scopes=scopes)
        self._access(project, principal_id, scopes, write=True)
        if project["revision"] != expected_revision:
            raise IntakeError("revision_conflict", "creation changed; inspect before building")
        if project["artifact_type"] not in BUILD_TYPES:
            raise IntakeError("not_build_type", "this artifact type is exported directly from its authored report")
        if project["status"] != "finished":
            raise IntakeError("not_finished", "finish the reviewed project before building")
        adapter = self.build_adapters.get(project["artifact_type"])
        if adapter is None:
            return {"contract": BUILD_CONTRACT, "status": "unavailable",
                    "reason": "build_adapter_unavailable", "artifact_type": project["artifact_type"],
                    "built": False}
        source = self.export(namespace, project_id, principal_id=principal_id, scopes=scopes)
        self.conn.execute(
            "INSERT INTO intake_creation_builds VALUES (?,?,?,?,?,?,?,?,?)",
            [namespace, principal_id, idempotency_key, project_id, expected_revision,
             request_hash, "reserved", None, self.now()],
        )
        try:
            result = _bounded(adapter(source, idempotency_key=idempotency_key), limit=64_000)
            if (
                not isinstance(result, dict)
                or not {"artifact_locator", "artifact_sha256", "adapter_version"} <= set(result)
                or not isinstance(result["artifact_sha256"], str)
                or len(result["artifact_sha256"]) != 64
            ):
                raise IntakeError("invalid_adapter_result", "build adapter must return locator, sha256 and version")
            status, error_code = "completed", None
        except Exception as exc:  # the build outcome may be uncertain
            result, status = None, "indeterminate"
            error_code = getattr(exc, "code", "build_call_uncertain")
        receipt = {
            "contract": BUILD_CONTRACT, "status": status, "project_id": project_id,
            "project_revision": expected_revision, "artifact_type": project["artifact_type"],
            "report": project["report"], "source_export_sha256": source["sha256"],
            "idempotency_key": idempotency_key, "result": result, "error_code": error_code,
            "publication_authorized": False, "recorded_at_ms": self.now(),
        }
        self.conn.execute(
            "UPDATE intake_creation_builds SET status=?,receipt_json=? "
            "WHERE namespace=? AND owner=? AND idempotency_key=?",
            [status, _json(receipt), namespace, principal_id, idempotency_key],
        )
        return {"contract": BUILD_CONTRACT, "status": status, "receipt": receipt, "idempotent": False}

    def handoff(
        self, namespace: str, project_id: str, request_key: str, *,
        destination_mode: str, reason: str, principal_id: str, scopes: set[str],
    ) -> dict:
        """Open a downstream session from a current finished artifact revision."""
        if destination_mode not in {"Externalization", "Internalization", "Maintenance"}:
            raise IntakeError("invalid_mode", "creation handoff supports procedure, practice, or maintenance")
        project = self.inspect(namespace, project_id, principal_id=principal_id, scopes=scopes)
        if project["status"] != "finished":
            raise IntakeError("not_finished", "finish the reviewed project before handoff")
        self._report(project, principal_id, scopes, current=True)
        report = project["report"]
        reason = _text(reason, "handoff reason", limit=2000)
        references = [
            {"kind": "creation_project", "id": project_id, "namespace": namespace,
             "version": project["revision"]},
            {"kind": "authored_report", "id": report["id"], "namespace": namespace,
             "version": report["revision"]},
        ]
        return IntakeStore(self.conn, now=self.now).create(
            namespace, destination_mode, request_key, intent=reason,
            inputs={"creation_project_id": project_id,
                    "creation_project_revision": project["revision"],
                    "authored_report_id": report["id"],
                    "authored_report_revision": report["revision"],
                    "handoff_reason": reason},
            workspace_links=project["workspace_links"],
            plugin_links=project.get("plugin_links"), references=references,
            principal_id=principal_id, scopes=scopes,
        )
