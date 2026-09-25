"""Separate semantic priority advice for authorized operational review tasks."""

from __future__ import annotations

import json
import time
from typing import Any

from src.kb.research_projects import _hash, _json
from src.kb.review_inbox import ReviewInboxStore
from src.kb.review_targets import ReviewTargetError

CONTRACT = "noesis-jev-review-priority-suggestion-v1"
TASK = "jev-review-priority-v1"
_DDL = """
CREATE TABLE IF NOT EXISTS review_inbox_machine_priorities(
 namespace TEXT NOT NULL,task_id TEXT NOT NULL,principal_id TEXT NOT NULL,
 run_id TEXT NOT NULL,request_hash TEXT NOT NULL,result_json TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL,PRIMARY KEY(namespace,task_id,principal_id,run_id));
"""


class ReviewPriorityAdvisor:
    def __init__(self, conn: Any, runtime: Any, *, initialize=True, now=None):
        self.conn, self.runtime = conn, runtime
        self.inbox = ReviewInboxStore(conn, initialize=False)
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _task(self, namespace, task_id, principal_id, scopes):
        task = self.inbox.inspect(namespace, task_id, principal_id=principal_id, scopes=scopes)
        if "operator" not in scopes and task["owner"] != principal_id:
            raise ReviewTargetError("unauthorized", "review priority requires the task coordinator")
        if task["stale"]:
            raise ReviewTargetError("target_stale", "current target revision is required")
        return task

    def _baseline(self, task_id, task):
        row = self.conn.execute("SELECT priority FROM review_inbox_tasks WHERE task_id=?", [task_id]).fetchone()
        return float(row[0]) if row else 2 * float(task["impact"]) + float(task["uncertainty"])

    def suggest(self, namespace, task_id, run_id, *, principal_id, scopes,
                allow_remote=False, policy=None, max_attempts=1,
                max_cost_usd_micros=0, deadline_s=30):
        task = self._task(namespace, task_id, principal_id, scopes)
        baseline = self._baseline(task_id, task)
        refs = task["sources"]
        bindings, captures = self.runtime.capture_sources(namespace, principal_id, refs, scopes)
        coverage = {"source_count": len(captures),
                    "characters": sum(len(source["content"]) for source in captures),
                    "complete": all(len(source["content"]) <= 8_000 for source in captures)}
        if not coverage["complete"]:
            return {"contract": CONTRACT, "status": "pending", "task_id": task_id,
                    "target_revision_hash": task["target_revision_hash"],
                    "vote_hash": _hash(task["votes"]),
                    "source_binding": bindings, "coverage": coverage,
                    "baseline_priority": baseline, "suggested_priority": None,
                    "machine_impact": None, "uncertainty_component": task["uncertainty"],
                    "observed_disagreement": None, "vendor_confidence": None,
                    "accepted": False, "votes_created": False, "resolution_created": False,
                    "decision_run": None}
        questions = {"impact": {"kind": "score",
                                "instructions": "How consequential would an error in this exact review target be for the declared review goal? Do not judge whether the target is true.",
                                "criteria": ["Negligible", "Limited", "Material", "Critical"]}}
        run = self.runtime.run(
            namespace, run_id, TASK,
            state={"task_id": task_id, "target": task["target"],
                   "target_revision_hash": task["target_revision_hash"],
                   "declared_impact": task["impact"], "declared_uncertainty": task["uncertainty"],
                   "review_goal": task["priority_rationale"], "coverage": coverage},
            questions=questions, source_refs=refs,
            source_slices=[{"start": 0, "end": len(source["content"])} for source in captures],
            principal_id=principal_id, scopes=scopes, allow_remote=allow_remote,
            policy=policy, max_attempts=max_attempts,
            max_cost_usd_micros=max_cost_usd_micros, deadline_s=deadline_s)
        current = self._task(namespace, task_id, principal_id, scopes)
        if (current["target_revision_hash"] != task["target_revision_hash"] or
            current["source_fingerprints"] != task["source_fingerprints"] or
            self._baseline(task_id, current) != baseline):
            raise ReviewTargetError("target_stale", "review task changed during machine priority assessment")
        labels = [vote["label"] for vote in current["votes"]]
        observed = len(labels) >= 2 and len({_json(label) for label in labels}) > 1
        uncertainty = max(float(task["uncertainty"]), 1.0 if observed else 0.0)
        answer = ((run.get("receipt") or {}).get("answers") or {}).get("impact") or {}
        level = answer.get("value") if answer.get("status") == "answered" else None
        active = run.get("status") == "completed" and run.get("rollout_mode") != "shadow"
        if type(level) is not int or level not in range(4) or not active:
            level = None
        machine_impact = level / 3 if level is not None else None
        suggestion = 2 * machine_impact + uncertainty if machine_impact is not None else None
        result = {"contract": CONTRACT, "status": "suggested" if suggestion is not None else "pending",
                  "task_id": task_id, "target_revision_hash": task["target_revision_hash"],
                  "vote_hash": _hash(current["votes"]),
                  "source_binding": run.get("source_binding", bindings), "coverage": coverage,
                  "baseline_priority": baseline, "suggested_priority": suggestion,
                  "machine_impact": machine_impact, "uncertainty_component": uncertainty,
                  "observed_disagreement": observed,
                  "vendor_confidence": answer.get("vendor_confidence"),
                  "vendor_confidence_used_as_error_probability": False,
                  "contributions": {"impact_weight": 2, "uncertainty_weight": 1},
                  "accepted": False, "votes_created": False, "resolution_created": False,
                  "decision_run": run}
        request_hash = _hash([task_id, run_id, task["target_revision_hash"], _hash(current["votes"]),
                              task["source_fingerprints"], policy, max_cost_usd_micros,
                              max_attempts, deadline_s])
        prior = self.conn.execute(
            "SELECT request_hash,result_json FROM review_inbox_machine_priorities "
            "WHERE namespace=? AND task_id=? AND principal_id=? AND run_id=?",
            [namespace, task_id, principal_id, run_id]).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise ReviewTargetError("run_conflict", "priority run ID identifies a different request")
            return {**json.loads(prior[1]), "replayed": True}
        self.conn.execute("INSERT INTO review_inbox_machine_priorities VALUES (?,?,?,?,?,?,?)",
                          [namespace, task_id, principal_id, run_id, request_hash,
                           _json(result), self.now()])
        return {**result, "replayed": False}

    def inspect(self, namespace, task_id, run_id, *, principal_id, scopes):
        task = self._task(namespace, task_id, principal_id, scopes)
        row = self.conn.execute(
            "SELECT result_json FROM review_inbox_machine_priorities "
            "WHERE namespace=? AND task_id=? AND principal_id=? AND run_id=?",
            [namespace, task_id, principal_id, run_id]).fetchone()
        if not row:
            raise ReviewTargetError("suggestion_unavailable", "priority suggestion is unavailable")
        result = json.loads(row[0])
        run = self.runtime.inspect(namespace, run_id, principal_id=principal_id, scopes=scopes)
        stale = (task["target_revision_hash"] != result["target_revision_hash"] or
                 _hash(task["votes"]) != result["vote_hash"] or
                 run.get("source_binding") != result["source_binding"])
        return {**result, "status": "stale" if stale else result["status"],
                "suggested_priority": None if stale else result["suggested_priority"],
                "valid": not stale}
