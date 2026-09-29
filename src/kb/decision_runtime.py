"""Authorized, source-bound execution of optional hosted decisions.

The reservation is durable before network I/O. An interrupted reservation is
indeterminate and is never sent again under the same run ID. Request text and
provider credentials are not stored by this module.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from typing import Any

from src.kb.artifacts import ArtifactGraph
from src.kb.intake_inbox import _authorize as authorize_inbox
from src.kb.review_inbox import ReviewInboxStore


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


class DecisionRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_DDL = """
CREATE TABLE IF NOT EXISTS hosted_decision_runs(
 namespace TEXT NOT NULL, owner TEXT NOT NULL, run_id TEXT NOT NULL,
 request_hash TEXT NOT NULL, sources_json TEXT NOT NULL, status TEXT NOT NULL,
 result_json TEXT, reserved_cost_micros BIGINT NOT NULL, created_at_ms BIGINT NOT NULL,
 updated_at_ms BIGINT NOT NULL, deadline_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace,owner,run_id));
CREATE TABLE IF NOT EXISTS hosted_decision_budgets(
 namespace TEXT NOT NULL, owner TEXT NOT NULL, budget_id TEXT NOT NULL,
 policy_hash TEXT NOT NULL, reserved_cost_micros BIGINT NOT NULL,
 PRIMARY KEY(namespace,owner,budget_id));
CREATE TABLE IF NOT EXISTS hosted_decision_attempts(
 namespace TEXT NOT NULL, owner TEXT NOT NULL, run_id TEXT NOT NULL,
 ordinal INTEGER NOT NULL, reserved_cost_micros BIGINT NOT NULL, reserved_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace,owner,run_id,ordinal));
CREATE TABLE IF NOT EXISTS hosted_decision_task_rollouts(
 namespace TEXT NOT NULL, task TEXT NOT NULL, mode TEXT NOT NULL,
 model TEXT, rubric_id TEXT, evaluation_ref TEXT,
 configured_by TEXT NOT NULL, updated_at_ms BIGINT NOT NULL,
 PRIMARY KEY(namespace,task));
"""


class DecisionRuntime:
    """Execute one optional TypeSafe decision against authorized exact sources.

    A client exposes ``decide(DecisionRequest, api_key=..., timeout_s=...,
    request_id=...)``. The credential resolver receives a non-secret reference
    and must return the current secret only at execution time.
    """

    def __init__(
        self,
        conn: Any,
        *,
        client: Any,
        credential_resolver: Callable[[str], str] | None = None,
        input_resolver: Callable[..., Mapping[str, Any]] | None = None,
        now: Callable[[], int] | None = None,
        initialize: bool = True,
    ) -> None:
        self.conn = conn
        self.graph = ArtifactGraph(conn, initialize=initialize)
        self.client = client
        self.credential_resolver = credential_resolver
        self.input_resolver = input_resolver
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _authorize(namespace: str, principal_id: str, scopes: set[str], *, execute: bool) -> None:
        needed = "knowledge:decision:execute" if execute else "knowledge:decision:read"
        ns_scope = f"namespace:{namespace}:{'write' if execute else 'read'}"
        if not namespace or not principal_id or ("operator" not in scopes and
            (needed not in scopes or ns_scope not in scopes)):
            raise DecisionRuntimeError("unauthorized", "current decision and namespace access required")

    def configure_task_rollout(self, namespace: str, task: str, mode: str, *,
                               model: str | None, rubric_id: str | None,
                               evaluation_ref: str | None, principal_id: str,
                               scopes: set[str]) -> dict[str, Any]:
        """Pin an operator-reviewed task mode without changing local defaults."""
        if not namespace or not principal_id or (
            "operator" not in scopes and not {
                "knowledge:decision:configure", f"namespace:{namespace}:write"
            } <= scopes
        ):
            raise DecisionRuntimeError("unauthorized", "task rollout configuration access required")
        if (
            not isinstance(task, str) or not 1 <= len(task) <= 200
            or not isinstance(namespace, str) or len(namespace) > 200
            or mode not in {"off", "shadow", "suggestion"}
            or any(value is not None and (not isinstance(value, str) or len(value) > 200)
                   for value in (model, rubric_id))
        ):
            raise DecisionRuntimeError("invalid_rollout", "bounded task and off, shadow or suggestion mode required")
        if mode != "off" and (
            not isinstance(model, str) or not model or
            not isinstance(rubric_id, str) or not rubric_id
        ):
            raise DecisionRuntimeError("invalid_rollout", "enabled task needs pinned model and rubric")
        if mode == "suggestion" and (
            not isinstance(evaluation_ref, str) or not evaluation_ref.strip()
            or len(evaluation_ref) > 500
        ):
            raise DecisionRuntimeError("invalid_rollout", "suggestion mode needs evaluation evidence reference")
        value = {
            "contract": "noesis-decision-task-rollout-v1", "namespace": namespace,
            "task": task, "mode": mode, "model": model, "rubric_id": rubric_id,
            "evaluation_ref": evaluation_ref, "configured_by": principal_id,
            "updated_at_ms": self.now(),
        }
        self.conn.execute(
            "INSERT OR REPLACE INTO hosted_decision_task_rollouts VALUES (?,?,?,?,?,?,?,?)",
            [namespace, task, mode, model, rubric_id, evaluation_ref,
             principal_id, value["updated_at_ms"]],
        )
        return value

    def inspect_task_rollout(self, namespace: str, task: str, *, principal_id: str,
                             scopes: set[str]) -> dict[str, Any]:
        self._authorize(namespace, principal_id, scopes, execute=False)
        exists = self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='hosted_decision_task_rollouts'"
        ).fetchone()
        row = (
            self.conn.execute(
                "SELECT mode,model,rubric_id,evaluation_ref,configured_by,updated_at_ms "
                "FROM hosted_decision_task_rollouts WHERE namespace=? AND task=?",
                [namespace, task],
            ).fetchone()
            if exists else None
        )
        return {
            "contract": "noesis-decision-task-rollout-v1", "namespace": namespace,
            "task": task, "mode": row[0] if row else "off",
            "model": row[1] if row else None, "rubric_id": row[2] if row else None,
            "evaluation_ref": row[3] if row else None,
            "configured_by": row[4] if row else None,
            "updated_at_ms": row[5] if row else None,
        }

    def _sources(self, namespace: str, owner: str, refs: list[dict[str, Any]], scopes: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not isinstance(refs, list) or not 1 <= len(refs) <= 20:
            raise DecisionRuntimeError("invalid_sources", "one to 20 exact source versions required")
        bindings: list[dict[str, Any]] = []
        captures: list[dict[str, Any]] = []
        documents = ReviewInboxStore(self.conn, initialize=False)
        for ref in refs:
            if not isinstance(ref, dict):
                raise DecisionRuntimeError("invalid_sources", "source reference must be an object")
            slice_range = None
            if "slice_start" in ref or "slice_end" in ref:
                start, end = ref.get("slice_start"), ref.get("slice_end")
                if type(start) is not int or type(end) is not int or not 0 <= start < end or end - start > 65_536:
                    raise DecisionRuntimeError("invalid_sources", "source slice must be a bounded exact character range")
                slice_range = (start, end)
                ref = {key: value for key, value in ref.items() if key not in {"slice_start", "slice_end"}}
            if set(ref) == {"document_id", "revision_id"}:
                try:
                    fingerprint = documents._sources([ref], scopes)[0]
                except Exception as exc:
                    code = getattr(exc, "code", "source_unavailable")
                    raise DecisionRuntimeError(code, "document revision is unavailable or unreadable") from exc
                current = self.conn.execute(
                    "SELECT revision_id FROM document_current_revisions WHERE document_id=?",
                    [ref["document_id"]],
                ).fetchone()
                if not current or current[0] != ref["revision_id"]:
                    raise DecisionRuntimeError("source_changed", "document revision is no longer current")
                row = self.conn.execute(
                    "SELECT payload_json FROM document_revision_records WHERE document_id=? AND revision_id=? AND committed_watermark IS NOT NULL",
                    [ref["document_id"], ref["revision_id"]],
                ).fetchone()
                payload = json.loads(row[0]) if row else {}
                content = payload.get("content")
                if payload.get("_payload_reclaimed") or not isinstance(content, str) or not content:
                    raise DecisionRuntimeError("source_unavailable", "captured document text is unavailable")
                binding = {"kind": "document_revision", **ref,
                           "content_hash": fingerprint["content_hash"], "payload_hash": fingerprint["payload_hash"]}
                capture = {"binding": binding, "content": content}
            elif set(ref) == {"item_id", "source_version"}:
                item_id, version = ref["item_id"], ref["source_version"]
                if not isinstance(item_id, str) or not item_id or type(version) is not int or version < 1:
                    raise DecisionRuntimeError("invalid_sources", "valid inbox identity and version required")
                try:
                    authorize_inbox(namespace, owner, owner, scopes)
                except Exception as exc:
                    raise DecisionRuntimeError("unauthorized", "inbox item is unavailable or unreadable") from exc
                current = self.conn.execute(
                    "SELECT source_version FROM intake_inbox_items WHERE namespace=? AND owner=? AND item_id=?",
                    [namespace, owner, item_id],
                ).fetchone()
                if not current:
                    raise DecisionRuntimeError("source_unavailable", "inbox item is unavailable")
                if int(current[0]) != version:
                    raise DecisionRuntimeError("source_changed", "inbox item version is no longer current")
                row = self.conn.execute(
                    "SELECT title,content,original_url FROM intake_inbox_item_revisions WHERE namespace=? AND owner=? AND item_id=? AND source_version=?",
                    [namespace, owner, item_id, version],
                ).fetchone()
                if not row or not isinstance(row[1], str) or not row[1]:
                    raise DecisionRuntimeError("source_unavailable", "inbox item text is unavailable")
                binding = {"kind": "inbox_item_version", "namespace": namespace, "owner": owner,
                           **ref, "content_hash": _hash([row[0], row[1], row[2]])}
                capture = {"binding": binding, "title": row[0], "content": row[1], "original_url": row[2]}
            elif set(ref) == {"input_id", "input_version", "content_hash"} and self.input_resolver:
                if any(not isinstance(value, str) or not value for value in ref.values()):
                    raise DecisionRuntimeError("invalid_sources", "versioned input identity required")
                try:
                    resolved = self.input_resolver(namespace=namespace, principal_id=owner, scopes=scopes, reference=ref)
                except Exception as exc:
                    raise DecisionRuntimeError("source_unavailable", "versioned input is unavailable") from exc
                if resolved.get("version") != ref["input_version"] or resolved.get("content_hash") != ref["content_hash"]:
                    raise DecisionRuntimeError("source_changed", "versioned input changed")
                content = resolved.get("content")
                if not isinstance(content, str) or not content:
                    raise DecisionRuntimeError("source_unavailable", "versioned input text is unavailable")
                binding = {"kind": "user_input_version", "namespace": namespace, "owner": owner,
                           "input_id": ref["input_id"], "version": ref["input_version"],
                           "content_hash": ref["content_hash"]}
                capture = {"binding": binding, "content": content}
            else:
                raise DecisionRuntimeError("invalid_sources", "unsupported exact source reference")
            if slice_range is not None:
                content = capture["content"]
                start, end = slice_range
                if end > len(content):
                    raise DecisionRuntimeError("source_changed", "source slice exceeds the current text")
                fragment = content[start:end]
                slice_binding = {"start": start, "end": end, "content_hash": _hash(fragment)}
                binding["slice"] = slice_binding
                capture["slice"] = slice_binding
                capture["content"] = fragment
            bindings.append(binding)
            captures.append(capture)
        if len(_json(captures).encode()) > 2_000_000:
            raise DecisionRuntimeError("input_limit", "captured source text exceeds hosted input limit")
        return bindings, captures

    def capture_sources(self, namespace: str, principal_id: str,
                        source_refs: list[dict[str, Any]], scopes: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Read authorized exact source versions for task-specific span construction.

        The decision run independently rechecks these bindings before a remote
        request, so a caller cannot use this capture to bypass currency checks.
        """
        return self._sources(namespace, principal_id, source_refs, scopes)

    @staticmethod
    def _policy(policy: Mapping[str, Any] | None, allow_remote: bool, max_attempts: int,
                max_cost_usd_micros: int, deadline_s: float) -> dict[str, Any]:
        if not allow_remote or not isinstance(policy, Mapping) or policy.get("hosted_allowed") is not True:
            raise DecisionRuntimeError("remote_disabled", "hosted decision processing is not enabled")
        required = ("model", "rubric_id", "policy_id", "credential_ref", "budget_id", "max_total_cost_usd_micros")
        allowed = set(required) | {"hosted_allowed", "response_retention", "max_concurrent", "calibration_id", "discard_max_relevance_p"}
        if set(policy) - allowed:
            raise DecisionRuntimeError("invalid_policy", "hosted policy contains unsupported fields")
        if any(not isinstance(policy.get(key), str) or not policy[key] for key in required[:-1]):
            raise DecisionRuntimeError("invalid_policy", "pinned model, rubric, policy, credential and budget references required")
        if policy.get("response_retention", "decision") not in {"decision", "full", "none"}:
            raise DecisionRuntimeError("invalid_policy", "response retention must be decision, full or none")
        total = policy["max_total_cost_usd_micros"]
        if (type(total) is not int or total < 1 or type(max_cost_usd_micros) is not int or
            not 1 <= max_cost_usd_micros <= total or type(max_attempts) is not int or
            not 1 <= max_attempts <= 3 or not isinstance(deadline_s, (int, float)) or
            not math.isfinite(deadline_s) or not 0.1 <= deadline_s <= 120 or
            max_cost_usd_micros < max_attempts):
            raise DecisionRuntimeError("invalid_budget", "bounded cost, attempts and deadline required")
        if policy.get("max_concurrent", 1) not in {1, 2, 3, 4}:
            raise DecisionRuntimeError("invalid_budget", "max_concurrent must be one to four")
        if "discard_max_relevance_p" in policy:
            threshold = policy["discard_max_relevance_p"]
            if (
                type(threshold) not in {int, float} or not math.isfinite(threshold)
                or not 0 <= threshold <= 1
                or not isinstance(policy.get("calibration_id"), str)
                or not policy["calibration_id"]
            ):
                raise DecisionRuntimeError("invalid_policy", "discard threshold requires a pinned calibration")
        return dict(policy)

    def inspect(self, namespace: str, run_id: str, *, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        self._authorize(namespace, principal_id, scopes, execute=False)
        row = self.conn.execute(
            "SELECT sources_json,status,result_json FROM hosted_decision_runs WHERE namespace=? AND owner=? AND run_id=?",
            [namespace, principal_id, run_id],
        ).fetchone()
        if not row:
            raise DecisionRuntimeError("run_unavailable", "hosted decision run is unavailable")
        bindings, _ = self._sources(namespace, principal_id, json.loads(row[0]), scopes)
        attempts = self.conn.execute(
            "SELECT count(*) FROM hosted_decision_attempts WHERE namespace=? AND owner=? AND run_id=?",
            [namespace, principal_id, run_id],
        ).fetchone()[0]
        result = json.loads(row[2]) if row[2] else {"status": "indeterminate", "receipt": None,
                                                    "remote_processing_used": attempts > 0,
                                                    "hosted_inference_used": attempts > 0,
                                                    "attempts_reserved": attempts}
        return {**result, "source_binding": bindings, "replayed": True}

    def run(self, namespace: str, run_id: str, task: str, *, state: Mapping[str, Any],
            questions: Mapping[str, Any], source_refs: list[dict[str, Any]], principal_id: str,
            scopes: set[str], allow_remote: bool = False, policy: Mapping[str, Any] | None = None,
            max_attempts: int = 1, max_cost_usd_micros: int = 0, deadline_s: float = 30,
            cancelled: Callable[[], bool] | None = None,
            source_slices: list[dict[str, int]] | None = None) -> dict[str, Any]:
        self._authorize(namespace, principal_id, scopes, execute=True)
        settings = self._policy(policy, allow_remote, max_attempts, max_cost_usd_micros, deadline_s)
        if (not isinstance(run_id, str) or not 1 <= len(run_id) <= 256 or
            not isinstance(task, str) or not task or not isinstance(state, Mapping) or
            not isinstance(questions, Mapping) or not questions or "sources" in state):
            raise DecisionRuntimeError("invalid_request", "bounded run, task, structured state and questions required")
        if source_slices is not None:
            if (not isinstance(source_slices, list) or not isinstance(source_refs, list)
                or len(source_slices) != len(source_refs) or
                any(not isinstance(ref, dict) for ref in source_refs) or
                any(not isinstance(item, dict) or set(item) != {"start", "end"} for item in source_slices)):
                raise DecisionRuntimeError("invalid_sources", "one exact slice per source reference required")
            source_refs = [{**ref, "slice_start": item["start"], "slice_end": item["end"]}
                           for ref, item in zip(source_refs, source_slices)]
        rollout = self.conn.execute(
            "SELECT mode,model,rubric_id,evaluation_ref FROM hosted_decision_task_rollouts "
            "WHERE namespace=? AND task=?", [namespace, task],
        ).fetchone()
        rollout_mode = rollout[0] if rollout else "manual"
        evaluation_ref = rollout[3] if rollout else None
        if rollout and rollout_mode == "off":
            raise DecisionRuntimeError("task_disabled", "hosted processing is off for this task")
        if rollout and (settings["model"] != rollout[1] or settings["rubric_id"] != rollout[2]):
            raise DecisionRuntimeError("rollout_mismatch", "task rollout pins a different model or rubric")
        bindings, captures = self._sources(namespace, principal_id, source_refs, scopes)
        if cancelled and cancelled():
            raise DecisionRuntimeError("cancelled", "hosted decision cancelled before reservation")
        from src.integrations.decisions import DecisionRequest

        request = DecisionRequest(task_id=task, rubric_id=settings["rubric_id"],
                                  state={**state, "sources": captures}, questions=questions,
                                  source_binding=bindings, model=settings["model"],
                                  policy_id=settings["policy_id"], calibration_id=settings.get("calibration_id"))
        request_hash = _hash({"namespace": namespace, "owner": principal_id,
                              "request": request.as_wire(), "task": task,
                              "rubric_id": settings["rubric_id"], "source_binding": bindings,
                              "policy": settings,
                              "max_attempts": max_attempts, "max_cost_usd_micros": max_cost_usd_micros,
                              "deadline_s": deadline_s, "retention": settings.get("response_retention", "decision"),
                              "rollout_mode": rollout_mode, "evaluation_ref": evaluation_ref})
        existing = self.conn.execute(
            "SELECT request_hash,status,result_json FROM hosted_decision_runs WHERE namespace=? AND owner=? AND run_id=?",
            [namespace, principal_id, run_id],
        ).fetchone()
        if existing:
            if existing[0] != request_hash:
                raise DecisionRuntimeError("run_conflict", "run ID is bound to a different request")
            if existing[1] == "reserved":
                raise DecisionRuntimeError("indeterminate", "reserved hosted request cannot be retried automatically")
            return {**json.loads(existing[2]), "replayed": True}
        if self.credential_resolver is None:
            raise DecisionRuntimeError("credential_unavailable", "hosted credential resolver is unavailable")
        try:
            credential = self.credential_resolver(settings["credential_ref"])
        except Exception as exc:
            raise DecisionRuntimeError("credential_unavailable", "hosted credential is unavailable") from exc
        if not isinstance(credential, str) or not credential:
            raise DecisionRuntimeError("credential_unavailable", "hosted credential is unavailable")
        # Never store the credential or the input text. Reserve budget and run
        # atomically before calling the remote provider.
        budget_hash = _hash({key: settings[key] for key in ("model", "policy_id", "budget_id", "max_total_cost_usd_micros", "max_concurrent") if key in settings})
        now = self.now()
        self.conn.execute("BEGIN")
        try:
            budget = self.conn.execute(
                "SELECT policy_hash,reserved_cost_micros FROM hosted_decision_budgets WHERE namespace=? AND owner=? AND budget_id=?",
                [namespace, principal_id, settings["budget_id"]],
            ).fetchone()
            if budget and budget[0] != budget_hash:
                raise DecisionRuntimeError("budget_conflict", "hosted budget policy changed")
            used = int(budget[1]) if budget else 0
            if used + max_cost_usd_micros > settings["max_total_cost_usd_micros"]:
                raise DecisionRuntimeError("budget_exhausted", "hosted decision cost budget exhausted")
            active = self.conn.execute(
                "SELECT count(*) FROM hosted_decision_runs WHERE namespace=? AND owner=? AND status='reserved' AND deadline_at_ms>?",
                [namespace, principal_id, now],
            ).fetchone()[0]
            if active >= settings.get("max_concurrent", 1):
                raise DecisionRuntimeError("concurrency_limit", "hosted decision concurrency limit reached")
            if budget:
                self.conn.execute(
                    "UPDATE hosted_decision_budgets SET reserved_cost_micros=? WHERE namespace=? AND owner=? AND budget_id=?",
                    [used + max_cost_usd_micros, namespace, principal_id, settings["budget_id"]],
                )
            else:
                self.conn.execute("INSERT INTO hosted_decision_budgets VALUES (?,?,?,?,?)",
                                  [namespace, principal_id, settings["budget_id"], budget_hash, max_cost_usd_micros])
            self.conn.execute("INSERT INTO hosted_decision_runs VALUES (?,?,?,?,?,'reserved',NULL,?,?,?,?)",
                              [namespace, principal_id, run_id, request_hash, _json(source_refs),
                               max_cost_usd_micros, now, now, now + int(deadline_s * 1000)])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        start = time.monotonic()
        def reserve_attempt(ordinal: int, amount: int) -> None:
            if cancelled and cancelled():
                raise DecisionRuntimeError("cancelled", "hosted decision cancelled before attempt")
            if type(ordinal) is not int or not 1 <= ordinal <= max_attempts or amount != max_cost_usd_micros // max_attempts:
                raise DecisionRuntimeError("invalid_attempt", "hosted attempt exceeds its reservation")
            self._sources(namespace, principal_id, source_refs, scopes)
            self.conn.execute(
                "INSERT INTO hosted_decision_attempts VALUES (?,?,?,?,?,?)",
                [namespace, principal_id, run_id, ordinal, amount, self.now()],
            )

        try:
            receipt = self.client.decide(
                request, api_key=credential, timeout_s=deadline_s,
                request_id=_hash([namespace, principal_id, run_id]),
                max_attempts=max_attempts,
                max_cost_micros_per_attempt=max_cost_usd_micros // max_attempts,
                reserve_attempt=reserve_attempt,
            )
            receipt = receipt.as_dict() if hasattr(receipt, "as_dict") else receipt
            if not isinstance(receipt, dict) or receipt.get("status") not in {"answered", "abstained", "unavailable"}:
                raise DecisionRuntimeError("invalid_receipt", "hosted provider returned an invalid receipt")
            attempt_count = self.conn.execute(
                "SELECT count(*) FROM hosted_decision_attempts WHERE namespace=? AND owner=? AND run_id=?",
                [namespace, principal_id, run_id],
            ).fetchone()[0]
            if attempt_count < 1:
                raise DecisionRuntimeError("unreserved_receipt", "hosted receipt has no reserved attempt")
            if time.monotonic() - start > deadline_s:
                raise DecisionRuntimeError("deadline_exceeded", "hosted decision exceeded its deadline")
            if cancelled and cancelled():
                raise DecisionRuntimeError("cancelled", "hosted decision cancelled before publication")
            fresh_bindings, _ = self._sources(namespace, principal_id, source_refs, scopes)
            if fresh_bindings != bindings:
                raise DecisionRuntimeError("source_changed", "source changed before decision publication")
            retention = settings.get("response_retention", "decision")
            if retention == "none":
                retained = {"contract": receipt.get("contract"), "status": receipt["status"],
                            "receipt_hash": _hash(receipt), "answers": {}}
            elif retention == "decision":
                retained = {key: value for key, value in receipt.items() if key not in {"raw_response", "raw_request", "text", "rationale", "explanation"}}
            else:
                retained = receipt
            if receipt.get("model_requested") != settings["model"] or (
                receipt["status"] == "answered" and receipt.get("model_returned") != settings["model"]
            ):
                raise DecisionRuntimeError("model_mismatch", "provider returned a different model version")
            result = {"status": "completed" if receipt["status"] == "answered" else receipt["status"],
                      "rollout_mode": rollout_mode,
                      "evaluation_ref": evaluation_ref,
                      "decision_policy": {
                          "calibration_id": settings.get("calibration_id"),
                          "discard_max_relevance_p": settings.get("discard_max_relevance_p"),
                      },
                      "receipt": retained, "source_binding": bindings,
                      "artifact": None,
                      "remote_processing_used": True, "hosted_inference_used": True, "replayed": False,
                      "reserved_cost_usd_micros": max_cost_usd_micros}
        except Exception as exc:
            code = getattr(exc, "code", "hosted_failure")
            result = {"status": "failed", "failure_code": code, "receipt": None,
                      "rollout_mode": rollout_mode,
                      "evaluation_ref": evaluation_ref,
                      "decision_policy": {
                          "calibration_id": settings.get("calibration_id"),
                          "discard_max_relevance_p": settings.get("discard_max_relevance_p"),
                      },
                      "source_binding": bindings, "artifact": None,
                      "remote_processing_used": False,
                      "hosted_inference_used": False, "replayed": False,
                      "reserved_cost_usd_micros": max_cost_usd_micros}
        attempts_made = self.conn.execute(
            "SELECT count(*) FROM hosted_decision_attempts WHERE namespace=? AND owner=? AND run_id=?",
            [namespace, principal_id, run_id],
        ).fetchone()[0]
        result["attempts_reserved"] = attempts_made
        result["remote_processing_used"] = attempts_made > 0
        result["hosted_inference_used"] = attempts_made > 0
        if result["status"] == "completed":
            self.conn.execute("BEGIN")
            try:
                final_bindings, _ = self._sources(namespace, principal_id, source_refs, scopes)
                if final_bindings != bindings:
                    raise DecisionRuntimeError("source_changed", "source changed before decision publication")
                if settings.get("response_retention", "decision") != "none":
                    result["artifact"] = self.graph.register(
                        namespace, "enrichment", "decision:" + _hash([principal_id, run_id])[:32],
                        {"task": task, "answers": result["receipt"].get("answers", {}),
                         "receipt_hash": _hash(receipt), "machine_suggestion": True},
                        configuration={"request_hash": request_hash, "rubric_id": settings["rubric_id"],
                                       "policy_id": settings["policy_id"], "retention": settings.get("response_retention", "decision")},
                        producer={"name": "typesafe", "version": settings["model"]},
                        dependencies=[
                            {"dependency_id": binding.get("document_id") or binding.get("item_id") or binding["input_id"],
                             "kind": "source", "content_hash": binding.get("content_hash"), "detail": binding}
                            for binding in bindings
                        ],
                    )
                self.conn.execute(
                    "UPDATE hosted_decision_runs SET status=?,result_json=?,updated_at_ms=? WHERE namespace=? AND owner=? AND run_id=?",
                    [result["status"], _json(result), self.now(), namespace, principal_id, run_id],
                )
                self.conn.execute("COMMIT")
                return result
            except Exception as exc:
                self.conn.execute("ROLLBACK")
                result = {**result, "status": "failed", "failure_code": getattr(exc, "code", "publication_failed"),
                          "receipt": None, "artifact": None}
        self.conn.execute(
            "UPDATE hosted_decision_runs SET status=?,result_json=?,updated_at_ms=? WHERE namespace=? AND owner=? AND run_id=?",
            [result["status"], _json(result), self.now(), namespace, principal_id, run_id],
        )
        return result
