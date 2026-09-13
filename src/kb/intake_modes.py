"""Durable, owner-scoped sessions for the ten information-workflow modes.

This is the workflow ledger, not a second source/evidence store. References point
to the existing authoritative objects and must carry a version. Completion is a
recorded, mode-specific claim; live-source and human outcome validation remain
separate acceptance work.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

CONTRACT = "noesis-intake-session-v1"
READ_SCOPE = "knowledge:intake:read"
WRITE_SCOPE = "knowledge:intake:write"
MODES = (
    "Awareness",
    "Exploration",
    "Deep Research",
    "Decision Support",
    "Problem-Solving",
    "Creation",
    "Externalization",
    "Internalization",
    "Iteration",
    "Maintenance",
)
DEFAULT_MINUTES = {
    "Awareness": 15,
    "Exploration": 90,
    "Deep Research": 120,
    "Decision Support": 60,
    "Problem-Solving": 45,
    "Creation": 120,
    "Externalization": 60,
    "Internalization": 30,
    "Iteration": 45,
    "Maintenance": 45,
}
MAX_MINUTES = {"Awareness": 15, "Exploration": 120, "Maintenance": 60}
MIN_MINUTES = {"Exploration": 60, "Maintenance": 30}
ROUTING_QUESTIONS = (
    ("urgent_or_broken", "Problem-Solving"),
    ("curiosity_only", "Exploration"),
    ("decision_needed", "Decision Support"),
    ("systematic_understanding", "Deep Research"),
    ("creating", "Creation"),
    ("staying_current", "Awareness"),
    ("externalize", "Externalization"),
    ("internalize", "Internalization"),
    ("iterating", "Iteration"),
    ("maintenance", "Maintenance"),
)
DECISIONS = {"watch", "escalate", "schedule", "discard", "archive", "flag"}
STATUSES = {"active", "paused", "completed", "cancelled"}
_REF_FIELDS = {"kind", "id", "namespace", "version", "locator"}
_WORKSPACE_KINDS = {"intake_item", "session", "artifact", "project", "note", "task"}
_DDL = """
CREATE TABLE IF NOT EXISTS intake_sessions (
  session_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
  request_hash TEXT NOT NULL, revision BIGINT NOT NULL, status TEXT NOT NULL,
  mode TEXT NOT NULL, content_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intake_session_revisions (
  session_id TEXT NOT NULL, revision BIGINT NOT NULL, content_json TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(session_id,revision)
);
CREATE TABLE IF NOT EXISTS intake_session_commands (
  session_id TEXT NOT NULL, command_key TEXT NOT NULL, request_hash TEXT NOT NULL,
  revision BIGINT NOT NULL, PRIMARY KEY(session_id,command_key)
);
CREATE INDEX IF NOT EXISTS idx_intake_sessions_owner
  ON intake_sessions(namespace,owner,mode,status);
"""


class IntakeError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value: Any, field: str, *, limit: int = 10_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise IntakeError(
            "invalid_input", f"{field} must be nonempty text within {limit} characters"
        )
    return value.strip()


def _bounded(value: Any, *, limit: int = 256_000) -> Any:
    try:
        encoded = _json(value)
    except (TypeError, ValueError) as exc:
        raise IntakeError("invalid_input", "input must be finite JSON data") from exc
    if len(encoded.encode()) > limit:
        raise IntakeError("input_too_large", "session input exceeds its byte budget")
    return json.loads(encoded)


def _reference(value: Any, namespace: str, scopes: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - _REF_FIELDS:
        raise IntakeError("invalid_reference", "reference has unsupported fields")
    kind = _text(value.get("kind"), "reference kind", limit=80)
    identity = _text(value.get("id"), "reference id", limit=512)
    ns = _text(value.get("namespace", namespace), "reference namespace", limit=128)
    version = value.get("version")
    if type(version) is not int or version < 1:
        raise IntakeError(
            "invalid_reference", "reference needs a positive authoritative version"
        )
    if (
        ns != namespace
        and "operator" not in scopes
        and f"namespace:{ns}:read" not in scopes
    ):
        raise IntakeError(
            "unauthorized", "current access to referenced namespace is required"
        )
    result = {"kind": kind, "id": identity, "namespace": ns, "version": version}
    if "locator" in value:
        locator = value["locator"]
        if not isinstance(locator, dict) or set(locator) - {
            "url",
            "page",
            "start",
            "end",
            "section",
        }:
            raise IntakeError("invalid_reference", "unsupported source locator")
        result["locator"] = _bounded(locator, limit=4096)
    return result


def _workspace_links(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list) or len(values) > 20:
        raise IntakeError(
            "invalid_workspace_link",
            "at most twenty Modulo workspace links are allowed",
        )
    result = []
    for value in values:
        if (
            not isinstance(value, dict)
            or set(value) != {"system", "workspace_id", "kind", "id", "version"}
            or value["system"] != "modulo"
            or value["kind"] not in _WORKSPACE_KINDS
        ):
            raise IntakeError(
                "invalid_workspace_link",
                "Modulo link requires system, workspace, kind, id and version",
            )
        if type(value["version"]) is not int or value["version"] < 1:
            raise IntakeError(
                "invalid_workspace_link",
                "Modulo link requires a positive object version",
            )
        link = {
            "system": "modulo",
            "workspace_id": _text(value["workspace_id"], "workspace_id", limit=128),
            "kind": value["kind"],
            "id": _text(value["id"], "workspace object id", limit=512),
            "version": value["version"],
        }
        if link not in result:
            result.append(link)
    return result


def _completion(state: dict[str, Any]) -> list[str]:
    """Return unmet recorded checks; never infer real-world truth from counts."""
    mode, data, refs = state["mode"], state["data"], state["references"]
    missing: list[str] = []

    def require(condition: bool, label: str) -> None:
        if not condition:
            missing.append(label)

    def filled(key: str) -> bool:
        return isinstance(data.get(key), str) and bool(data[key].strip())

    def has_ref(kind: str) -> bool:
        return any(ref.get("kind") == kind for ref in refs)

    if mode == "Awareness":
        queue = state["inputs"].get("feed_item_ids", [])
        require(isinstance(queue, list) and len(queue) > 0, "feed_item_ids")
        decisions = data.get("decisions")
        require(
            isinstance(decisions, dict)
            and all(decisions.get(item) in DECISIONS for item in queue),
            "all_feed_items_decided",
        )
    elif mode == "Exploration":
        require(
            state.get("elapsed_ms", 0) >= state["duration_minutes"] * 60_000
            or filled("escalation_reason"),
            "timebox_or_escalation",
        )
    elif mode == "Deep Research":
        for kind in (
            "evidence_card",
            "concept",
            "claim_ledger",
            "brief",
            "mental_model",
            "map",
        ):
            require(has_ref(kind), f"reference:{kind}")
        for key in ("known", "uncertain", "unresolved"):
            require(filled(key), key)
        require(data.get("definition_of_done_met") is True, "definition_of_done_review")
    elif mode == "Decision Support":
        require(filled("selected_option"), "selected_option")
        require(filled("rationale"), "rationale")
        require(has_ref("decision"), "reference:decision")
    elif mode == "Problem-Solving":
        require(data.get("verified") is True, "verified_fix")
        require(filled("verification"), "verification")
        if state["inputs"].get("problem_contract") == "noesis-problem-trail-v1":
            trail = data.get("problem_trail", [])
            require(
                isinstance(trail, list)
                and bool(trail)
                and isinstance(trail[-1], dict)
                and trail[-1].get("kind") == "verification"
                and trail[-1].get("passed") is True
                and bool(trail[-1].get("observation")),
                "latest_observed_success_check",
            )
    elif mode == "Creation":
        require(has_ref("created_artifact"), "reference:created_artifact")
        checks = data.get("acceptance_checks")
        require(
            isinstance(checks, dict)
            and bool(checks)
            and all(value is True for value in checks.values()),
            "acceptance_checks",
        )
    elif mode == "Externalization":
        require(has_ref("procedure"), "reference:procedure")
        require(filled("rehearsal_or_execution"), "rehearsal_or_execution")
    elif mode == "Internalization":
        attempts = data.get("attempts")
        require(
            isinstance(attempts, list)
            and any(
                isinstance(a, dict)
                and a.get("assisted") is False
                and a.get("demonstrated") is True
                and a.get("answer")
                for a in attempts
            ),
            "unaided_demonstration",
        )
    elif mode == "Iteration":
        for key in ("expected", "observed", "learning"):
            require(filled(key), key)
        require(has_ref("revised_artifact"), "reference:revised_artifact")
    elif mode == "Maintenance":
        if state["inputs"].get("maintenance_contract") == "noesis-intake-maintenance-review-v1":
            findings = state["inputs"].get("findings", [])
            reviews = data.get("maintenance_reviews", {})
            require(
                isinstance(reviews, dict)
                and all(item["id"] in reviews for item in findings),
                "all_findings_reviewed_or_deferred",
            )
            assessment = data.get("health_assessment")
            require(
                isinstance(assessment, dict)
                and assessment.get("acceptable") is True
                and bool(assessment.get("criteria"))
                and bool(assessment.get("observation")),
                "reviewed_health_criteria",
            )
        checks = data.get("checklist")
        require(
            isinstance(checks, dict)
            and bool(checks)
            and all(
                isinstance(value, str) and value in {"done", "deferred"}
                for value in checks.values()
            ),
            "checklist_reviewed",
        )
        require(data.get("health_acceptable") is True, "health_acceptable")
    return missing


class IntakeStore:
    """Transactional session ledger with optimistic revisions and command replay."""

    def __init__(
        self,
        conn: Any,
        *,
        initialize: bool = True,
        now=None,
        active_research_limit: int | None = None,
    ):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if active_research_limit is None:
            from src.config.env import resolve_env

            try:
                active_research_limit = int(
                    resolve_env("INTAKE_ACTIVE_RESEARCH_LIMIT", "3") or "3"
                )
            except ValueError as exc:
                raise IntakeError(
                    "invalid_config", "active research limit must be an integer"
                ) from exc
        if (
            type(active_research_limit) is not int
            or not 1 <= active_research_limit <= 100
        ):
            raise IntakeError(
                "invalid_config", "active research limit must be between one and 100"
            )
        self.active_research_limit = active_research_limit
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _authorize(
        state: dict[str, Any], principal_id: str, scopes: set[str], *, write=False
    ) -> None:
        required = WRITE_SCOPE if write else READ_SCOPE
        namespace = state["namespace"]
        allowed_ns = f"namespace:{namespace}:{'write' if write else 'read'}" in scopes
        if (
            not principal_id
            or "operator" not in scopes
            and (
                required not in scopes
                or state["owner"] != principal_id
                or not allowed_ns
            )
        ):
            raise IntakeError(
                "unauthorized",
                "current owner, intake, and namespace access are required",
            )

    def _state(
        self, namespace: str, session_id: str, revision: int | None = None
    ) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT r.content_json FROM intake_sessions s JOIN intake_session_revisions r "
            "ON s.session_id=r.session_id WHERE s.namespace=? AND s.session_id=? "
            "AND r.revision=coalesce(?,s.revision)",
            [namespace, session_id, revision],
        ).fetchone()
        if row is None:
            raise IntakeError("session_not_found", "session revision is unavailable")
        return json.loads(row[0])

    def _visible(
        self, state: dict[str, Any], scopes: set[str], *, live: bool = True
    ) -> dict[str, Any]:
        value = json.loads(_json(state))
        value["validation_state"] = "recorded_only"
        value["access_degraded"] = False
        if (
            live
            and value["status"] == "active"
            and value.get("active_since_ms") is not None
        ):
            value["elapsed_ms"] += max(0, self.now() - value["active_since_ms"])
        value["remaining_minutes"] = max(
            0, (value["duration_minutes"] * 60_000 - value["elapsed_ms"]) / 60_000
        )
        if "operator" not in scopes:
            inaccessible = any(
                ref["namespace"] != state["namespace"]
                and f"namespace:{ref['namespace']}:read" not in scopes
                for ref in value["references"]
            )
            value["references"] = [
                ref
                if ref["namespace"] == state["namespace"]
                or f"namespace:{ref['namespace']}:read" in scopes
                else {"redacted": True}
                for ref in value["references"]
            ]
            if inaccessible:
                value["data"] = {"redacted": True}
                value["access_degraded"] = True
        value["unmet_completion_checks"] = (
            _completion(value) if value["status"] != "completed" else []
        )
        return value

    def create(
        self,
        namespace: str,
        mode: str,
        request_key: str,
        *,
        intent: str,
        inputs: dict[str, Any] | None = None,
        duration_minutes: int | None = None,
        origin: dict[str, Any] | None = None,
        workspace_links: list[dict[str, Any]] | None = None,
        references: list[dict[str, Any]] | None = None,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        namespace = _text(namespace, "namespace", limit=128)
        request_key = _text(request_key, "request_key", limit=256)
        if mode not in MODES:
            raise IntakeError("invalid_mode", "select one of the ten supported modes")
        intent = _text(intent, "intent")
        inputs = _bounded(inputs or {})
        supplied_workspace_links = workspace_links is not None
        workspace_links = _workspace_links(workspace_links or [])
        if references is not None and (
            not isinstance(references, list) or len(references) > 1000
        ):
            raise IntakeError(
                "invalid_reference", "at most 1000 initial references are allowed"
            )
        initial_references = []
        for raw in references or []:
            ref = _reference(raw, namespace, scopes)
            if ref not in initial_references:
                initial_references.append(ref)
        if not isinstance(inputs, dict):
            raise IntakeError("invalid_input", "inputs must be an object")
        if mode == "Awareness":
            items = inputs.get("feed_item_ids")
            if (
                not isinstance(items, list)
                or not items
                or len(items) > 1000
                or any(not isinstance(x, str) or not x for x in items)
                or len(set(items)) != len(items)
            ):
                raise IntakeError(
                    "invalid_input", "Awareness needs a bounded, unique feed-item queue"
                )
        minutes = (
            DEFAULT_MINUTES[mode] if duration_minutes is None else duration_minutes
        )
        if type(minutes) is not int or not MIN_MINUTES.get(
            mode, 1
        ) <= minutes <= MAX_MINUTES.get(mode, 1440):
            raise IntakeError("invalid_budget", "mode time budget is invalid")
        state = {
            "contract": CONTRACT,
            "session_id": "intake:"
            + _hash([namespace, principal_id, request_key])[:32],
            "namespace": namespace,
            "owner": principal_id,
            "mode": mode,
            "intent": intent,
            "inputs": inputs,
            "duration_minutes": minutes,
            "origin": None,
            "workspace_links": workspace_links,
            "status": "active",
            "revision": 1,
            "data": {},
            "references": initial_references,
            "history": [],
        }
        self._authorize(state, principal_id, scopes, write=True)
        origin_request = None
        if origin is not None:
            parent_id = _text(
                origin.get("session_id") if isinstance(origin, dict) else None,
                "origin session_id",
            )
            reason = _text(origin.get("reason"), "transition reason")
            origin_request = {"session_id": parent_id, "reason": reason}
            parent = self._state(namespace, parent_id)
            self._authorize(parent, principal_id, scopes)
            state["origin"] = {
                "session_id": parent_id,
                "revision": parent["revision"],
                "mode": parent["mode"],
                "reason": reason,
            }
            if not supplied_workspace_links:
                state["workspace_links"] = list(parent.get("workspace_links", []))
        request = {
            **{
                k: state[k]
                for k in (
                    "namespace",
                    "owner",
                    "mode",
                    "intent",
                    "inputs",
                    "duration_minutes",
                    "workspace_links",
                )
            },
            "origin": origin_request,
        }
        if references is not None:
            request["references"] = initial_references
        digest = _hash(request)
        existing = self.conn.execute(
            "SELECT request_hash FROM intake_sessions WHERE session_id=?",
            [state["session_id"]],
        ).fetchone()
        if existing:
            if existing[0] != digest:
                raise IntakeError(
                    "idempotency_conflict", "request_key identifies a different session"
                )
            current = self._state(namespace, state["session_id"])
            self._authorize(current, principal_id, scopes)
            return {**self._visible(current, scopes), "idempotent": True}
        if mode == "Deep Research" and "operator" not in scopes:
            count = self.conn.execute(
                "SELECT count(*) FROM intake_sessions WHERE owner=? "
                "AND mode='Deep Research' AND status IN ('active','paused')",
                [principal_id],
            ).fetchone()[0]
            if count >= self.active_research_limit:
                raise IntakeError(
                    "active_topic_limit",
                    "pause or complete an active research topic first",
                )
        state["created_at_ms"] = state["updated_at_ms"] = self.now()
        state["active_since_ms"] = state["created_at_ms"]
        state["elapsed_ms"] = 0
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO intake_sessions VALUES (?,?,?,?,?,?,?,?)",
                [
                    state["session_id"],
                    namespace,
                    principal_id,
                    digest,
                    1,
                    "active",
                    mode,
                    _json(state),
                ],
            )
            self.conn.execute(
                "INSERT INTO intake_session_revisions VALUES (?,?,?,?)",
                [state["session_id"], 1, _json(state), state["created_at_ms"]],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**self._visible(state, scopes), "idempotent": False}

    def inspect(
        self,
        namespace: str,
        session_id: str,
        *,
        principal_id: str,
        scopes: set[str],
        revision: int | None = None,
    ) -> dict[str, Any]:
        current = self._state(namespace, session_id)
        self._authorize(current, principal_id, scopes)
        state = (
            self._state(namespace, session_id, revision)
            if revision is not None
            else current
        )
        return self._visible(state, scopes, live=revision is None)

    def list(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: set[str],
        mode: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._authorize(
            {"namespace": namespace, "owner": principal_id}, principal_id, scopes
        )
        if mode is not None and mode not in MODES:
            raise IntakeError("invalid_mode", "unsupported mode filter")
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or offset < 0
        ):
            raise IntakeError(
                "invalid_page", "bounded limit and nonnegative offset are required"
            )
        owner = None if "operator" in scopes else principal_id
        rows = self.conn.execute(
            "SELECT content_json FROM intake_sessions WHERE namespace=? AND (? IS NULL OR owner=?) "
            "AND (? IS NULL OR mode=?) ORDER BY session_id LIMIT ? OFFSET ?",
            [namespace, owner, owner, mode, mode, limit, offset],
        ).fetchall()
        return {
            "contract": "noesis-intake-session-page-v1",
            "sessions": [self._visible(json.loads(row[0]), scopes) for row in rows],
            "limit": limit,
            "offset": offset,
        }

    def export(
        self, namespace: str, session_id: str, *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        """Return a portable revision chain only while all linked namespaces remain readable."""
        state = self._state(namespace, session_id)
        self._authorize_full_read(state, principal_id, scopes)
        rows = self.conn.execute(
            "SELECT content_json FROM intake_session_revisions WHERE session_id=? ORDER BY revision",
            [session_id],
        ).fetchall()
        revisions = [json.loads(row[0]) for row in rows]
        return {
            "contract": "noesis-intake-session-export-v1",
            "session_id": session_id,
            "revisions": revisions,
            "sha256": _hash(revisions),
        }

    @classmethod
    def _authorize_full_read(
        cls, state: dict[str, Any], principal_id: str, scopes: set[str]
    ) -> None:
        cls._authorize(state, principal_id, scopes)
        if "operator" not in scopes and any(
            ref["namespace"] != state["namespace"]
            and f"namespace:{ref['namespace']}:read" not in scopes
            for ref in state["references"]
        ):
            raise IntakeError(
                "unauthorized", "current access to all linked references is required"
            )

    def modulo_handoff(
        self, namespace: str, session_id: str, *, principal_id: str, scopes: set[str]
    ) -> dict[str, Any]:
        """Export a current-access bridge projection without copying source content."""
        state = self._state(namespace, session_id)
        self._authorize_full_read(state, principal_id, scopes)
        return {
            "contract": "noesis-modulo-intake-handoff-v1",
            "scope": {"namespace": state["namespace"], "owner": state["owner"]},
            "correlation_key": state["session_id"],
            "session": {
                "id": state["session_id"],
                "revision": state["revision"],
                "mode": state["mode"],
                "status": state["status"],
                "created_at_ms": state["created_at_ms"],
                "updated_at_ms": state["updated_at_ms"],
            },
            "transition": {
                "origin": state["origin"],
                "at_ms": state["created_at_ms"],
            },
            "modulo_links": state["workspace_links"],
            "noesis_references": state["references"],
            "access_state": "current",
        }

    def command(
        self,
        namespace: str,
        session_id: str,
        command_key: str,
        *,
        expected_revision: int,
        action: str,
        payload: dict[str, Any] | None,
        principal_id: str,
        scopes: set[str],
        record_hook=None,
    ) -> dict[str, Any]:
        command_key = _text(command_key, "command_key", limit=256)
        if type(expected_revision) is not int or expected_revision < 1:
            raise IntakeError("invalid_revision", "expected_revision must be positive")
        if action not in {"record", "pause", "resume", "complete", "cancel"}:
            raise IntakeError("invalid_action", "unsupported session command")
        payload = _bounded(payload or {})
        if not isinstance(payload, dict):
            raise IntakeError("invalid_input", "command payload must be an object")
        digest = _hash([action, payload])
        self.conn.execute("BEGIN")
        try:
            state = self._state(namespace, session_id)
            self._authorize(state, principal_id, scopes, write=True)
            replay = self.conn.execute(
                "SELECT request_hash,revision FROM intake_session_commands WHERE session_id=? AND command_key=?",
                [session_id, command_key],
            ).fetchone()
            if replay:
                if replay[0] != digest:
                    raise IntakeError(
                        "idempotency_conflict",
                        "command_key identifies another operation",
                    )
                recorded = self._state(namespace, session_id, int(replay[1]))
                self.conn.execute("COMMIT")
                return {
                    **self._visible(recorded, scopes, live=False),
                    "idempotent": True,
                }
            if state["revision"] != expected_revision:
                raise IntakeError(
                    "revision_conflict", "session changed; inspect before retry"
                )
            if state["status"] in {"completed", "cancelled"}:
                raise IntakeError(
                    "closed_session",
                    "completed or cancelled session cannot be modified",
                )
            now_ms = self.now()
            if state["status"] == "active" and state.get("active_since_ms") is not None:
                state["elapsed_ms"] += max(0, now_ms - state["active_since_ms"])
                state["active_since_ms"] = now_ms
            if action == "record":
                if state["status"] != "active":
                    raise IntakeError(
                        "paused_session", "resume before recording progress"
                    )
                if (
                    state["mode"] == "Problem-Solving"
                    and state["inputs"].get("problem_contract")
                    == "noesis-problem-trail-v1"
                    and record_hook is None
                ):
                    raise IntakeError(
                        "typed_record_required",
                        "use record_problem_step for a typed troubleshooting trail",
                    )
                if (
                    state["mode"] == "Maintenance"
                    and state["inputs"].get("maintenance_contract")
                    == "noesis-intake-maintenance-review-v1"
                    and record_hook is None
                ):
                    raise IntakeError(
                        "typed_record_required",
                        "use record_maintenance_finding or assess_maintenance_health",
                    )
                if set(payload) - {"data", "references"} or not payload:
                    raise IntakeError(
                        "invalid_input", "record accepts data and references"
                    )
                patch = payload.get("data", {})
                if not isinstance(patch, dict):
                    raise IntakeError("invalid_input", "data must be an object")
                if state["mode"] == "Awareness" and "decisions" in patch:
                    decisions = patch["decisions"]
                    queue = set(state["inputs"]["feed_item_ids"])
                    if (
                        not isinstance(decisions, dict)
                        or set(decisions) - queue
                        or any(d not in DECISIONS for d in decisions.values())
                    ):
                        raise IntakeError(
                            "invalid_decision",
                            "triage decisions must target queued items",
                        )
                    patch = {
                        **patch,
                        "decisions": {
                            **state["data"].get("decisions", {}),
                            **decisions,
                        },
                    }
                if state["mode"] == "Internalization" and "attempts" in patch:
                    if not isinstance(patch["attempts"], list) or not all(
                        isinstance(a, dict)
                        and isinstance(a.get("assisted"), bool)
                        and isinstance(a.get("answer"), str)
                        for a in patch["attempts"]
                    ):
                        raise IntakeError(
                            "invalid_attempt",
                            "practice attempts need answer and assisted state",
                        )
                    patch = {
                        **patch,
                        "attempts": state["data"].get("attempts", [])
                        + [
                            {**attempt, "at_ms": self.now()}
                            for attempt in patch["attempts"]
                        ],
                    }
                state["data"].update(patch)
                refs = payload.get("references", [])
                if not isinstance(refs, list) or len(refs) > 100:
                    raise IntakeError(
                        "invalid_reference", "at most 100 references per command"
                    )
                for raw in refs:
                    ref = _reference(raw, namespace, scopes)
                    if ref not in state["references"]:
                        state["references"].append(ref)
                if len(state["references"]) > 1000:
                    raise IntakeError(
                        "invalid_reference", "session reference limit exceeded"
                    )
                if record_hook is not None:
                    record_hook(state, payload)
            elif action == "pause":
                if state["status"] != "active":
                    raise IntakeError(
                        "invalid_status", "only an active session can pause"
                    )
                state["status"] = "paused"
                state["active_since_ms"] = None
            elif action == "resume":
                if state["status"] != "paused":
                    raise IntakeError(
                        "invalid_status", "only a paused session can resume"
                    )
                state["status"] = "active"
                state["active_since_ms"] = now_ms
            elif action == "complete":
                if "operator" not in scopes and any(
                    ref["namespace"] != namespace
                    and f"namespace:{ref['namespace']}:read" not in scopes
                    for ref in state["references"]
                ):
                    raise IntakeError(
                        "unauthorized",
                        "current access to completion references is required",
                    )
                unmet = _completion(state)
                if unmet:
                    raise IntakeError(
                        "incomplete_mode",
                        "unmet completion checks: " + ", ".join(unmet),
                    )
                state["status"] = "completed"
                state["active_since_ms"] = None
            else:
                state["status"] = "cancelled"
                state["active_since_ms"] = None
            state["revision"] += 1
            state["updated_at_ms"] = now_ms
            state["history"].append(
                {
                    "revision": state["revision"],
                    "action": action,
                    "at_ms": state["updated_at_ms"],
                    "command_key": command_key,
                }
            )
            _bounded(state, limit=4_000_000)
            changed = self.conn.execute(
                "UPDATE intake_sessions SET revision=?,status=?,content_json=? WHERE session_id=? AND revision=? RETURNING revision",
                [
                    state["revision"],
                    state["status"],
                    _json(state),
                    session_id,
                    expected_revision,
                ],
            ).fetchone()
            if not changed:
                raise IntakeError(
                    "revision_conflict", "concurrent session update; inspect and retry"
                )
            self.conn.execute(
                "INSERT INTO intake_session_revisions VALUES (?,?,?,?)",
                [session_id, state["revision"], _json(state), state["updated_at_ms"]],
            )
            self.conn.execute(
                "INSERT INTO intake_session_commands VALUES (?,?,?,?)",
                [session_id, command_key, digest, state["revision"]],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**self._visible(state, scopes), "idempotent": False}


def discover_modes() -> dict[str, Any]:
    return {
        "contract": "noesis-intake-modes-v1",
        "modes": [
            {
                "name": mode,
                "default_minutes": DEFAULT_MINUTES[mode],
                "minimum_minutes": MIN_MINUTES.get(mode, 1),
                "maximum_minutes": MAX_MINUTES.get(mode),
                "session_ledger_ready": True,
                "native_workflow_ready": False,
                "readiness_reason": "mode-specific native workflow and outcome validation are not composed yet",
                "requires": {
                    "Awareness": ["feed_item_ids"],
                    "Deep Research": [
                        "versioned research artifact references",
                        "Definition of Done review",
                    ],
                    "Problem-Solving": ["recorded verification"],
                    "Internalization": ["unaided attempt"],
                }.get(mode, ["mode-specific completion evidence"]),
            }
            for mode in MODES
        ],
    }


def route_mode(
    answers: dict[str, bool], *, override: str | None = None
) -> dict[str, Any]:
    """Suggest a mode from the user's ten questions while preserving override."""
    if not isinstance(answers, dict) or set(answers) - {
        key for key, _ in ROUTING_QUESTIONS
    }:
        raise IntakeError(
            "invalid_route", "answers must use the ten mode-selection questions"
        )
    if any(type(value) is not bool for value in answers.values()):
        raise IntakeError("invalid_route", "mode-selection answers must be boolean")
    if override is not None and override not in MODES:
        raise IntakeError("invalid_mode", "override must name a supported mode")
    matched = [(key, mode) for key, mode in ROUTING_QUESTIONS if answers.get(key)]
    suggested = matched[0][1] if matched else "Awareness"
    return {
        "contract": "noesis-intake-route-v1",
        "mode": override or suggested,
        "suggested_mode": suggested,
        "overridden": override is not None,
        "matched_questions": [key for key, _ in matched],
    }


def verify_export(bundle: dict[str, Any]) -> dict[str, Any]:
    """Check a portable session chain without requiring the source warehouse."""
    reasons: list[str] = []
    if (
        not isinstance(bundle, dict)
        or bundle.get("contract") != "noesis-intake-session-export-v1"
    ):
        reasons.append("invalid_contract")
        bundle = {}
    revisions = bundle.get("revisions")
    session_id = bundle.get("session_id")
    if not isinstance(revisions, list) or not revisions:
        reasons.append("missing_revisions")
        revisions = []
    if not isinstance(session_id, str) or not session_id.startswith("intake:"):
        reasons.append("invalid_session_id")
    first = revisions[0] if revisions and isinstance(revisions[0], dict) else {}
    for ordinal, state in enumerate(revisions, 1):
        history = state.get("history") if isinstance(state, dict) else None
        if (
            not isinstance(state, dict)
            or state.get("contract") != CONTRACT
            or state.get("session_id") != session_id
            or state.get("revision") != ordinal
            or state.get("namespace") != first.get("namespace")
            or state.get("owner") != first.get("owner")
            or state.get("mode") != first.get("mode")
            or not isinstance(history, list)
            or len(history) != ordinal - 1
            or (
                ordinal > 1
                and (
                    not isinstance(history[-1], dict)
                    or history[-1].get("revision") != ordinal
                )
            )
        ):
            reasons.append("broken_revision_chain")
            break
    if bundle.get("sha256") != _hash(revisions):
        reasons.append("digest_mismatch")
    return {
        "contract": "noesis-intake-session-export-verification-v1",
        "valid": not reasons,
        "reasons": reasons,
        "revision_count": len(revisions),
    }
