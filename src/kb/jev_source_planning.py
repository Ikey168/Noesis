"""Version-bound semantic relevance advice for already eligible source capabilities."""

from __future__ import annotations

import json

from src.kb.source_planner import READ_SCOPE, SourcePlannerError, SourcePlannerStore

CONTRACT = "noesis-jev-source-planning-v1"
TASK = "jev-source-planning-v1"


class PlanningInputResolver:
    def __init__(self, conn):
        self.conn = conn
        self.planner = SourcePlannerStore(conn, initialize=False)

    def __call__(self, *, namespace, principal_id, scopes, reference):
        if READ_SCOPE not in scopes and "operator" not in scopes:
            raise SourcePlannerError("unauthorized", "source planner read scope required")
        input_id = reference.get("input_id", "")
        if input_id.startswith("source-objective:"):
            objective_id = input_id.removeprefix("source-objective:")
            objective = self.planner.objective(namespace, objective_id, scopes=scopes)
            content = {key: objective[key] for key in ("question", "decomposition", "evidence_classes", "constraints")}
            return {"version": objective["input_hash"], "content_hash": objective["input_hash"],
                    "content": json.dumps(content, sort_keys=True)}
        if input_id.startswith("source-capability:"):
            capability_id = input_id.removeprefix("source-capability:")
            capability = self.planner.capability(namespace, capability_id, scopes=scopes)
            if not capability or capability["status"] != "active":
                raise SourcePlannerError("source_changed", "capability version is unavailable")
            current = self.conn.execute(
                "SELECT capability_id FROM source_capability_current WHERE namespace=? AND source_id=?",
                [namespace, capability["source_id"]]).fetchone()
            if not current or current[0] != capability_id:
                raise SourcePlannerError("source_changed", "capability is no longer current")
            content = {key: capability[key] for key in ("source_id", "semantic_version", "coverage", "authority", "query_forms")}
            return {"version": capability_id, "content_hash": capability["content_hash"],
                    "content": json.dumps(content, sort_keys=True)}
        raise SourcePlannerError("invalid_source", "unsupported planning input")


def suggest_source_relevance(runtime, planner: SourcePlannerStore, namespace: str,
                             objective_id: str, run_id: str, *, at_ms: int,
                             principal_id: str, scopes: set[str], credential_available=None,
                             allow_remote=False, policy=None, max_attempts=1,
                             max_cost_usd_micros=0, deadline_s=30):
    baseline = planner.preview(namespace, objective_id, scopes=scopes, at_ms=at_ms,
                               credential_available=credential_available)
    objective = planner.objective(namespace, objective_id, scopes=scopes)
    eligible = {step["capability_id"]: step["source_id"]
                for step in baseline["steps"] + baseline["fallback_steps"]}
    if not eligible or len(eligible) > 32:
        return {"contract": CONTRACT, "status": "pending", "reason": "no_bounded_eligible_capabilities",
                "objective_id": objective_id, "eligible_capability_ids": sorted(eligible),
                "baseline_plan": baseline, "advised_plan": None, "scores": {},
                "accepted": False, "decision_run": None}
    capabilities = [planner.capability(namespace, capability_id, scopes=scopes)
                    for capability_id in sorted(eligible)]
    refs = [{"input_id": "source-objective:" + objective_id,
             "input_version": objective["input_hash"], "content_hash": objective["input_hash"]}]
    refs.extend({"input_id": "source-capability:" + cap["capability_id"],
                 "input_version": cap["capability_id"], "content_hash": cap["content_hash"]}
                for cap in capabilities)
    questions = {
        f"relevance_{index}": {"kind": "score",
                               "instructions": f"How relevant is source {cap['source_id']} to the objective and required evidence? Judge likely evidence yield, not license, cost, or access.",
                               "criteria": ["None", "Low", "Moderate", "High"]}
        for index, cap in enumerate(capabilities)
    }
    run = runtime.run(
        namespace, run_id, TASK,
        state={"objective_id": objective_id, "objective_input_hash": objective["input_hash"],
               "eligible_capability_ids": [cap["capability_id"] for cap in capabilities],
               "baseline_plan_hash": baseline["plan_hash"], "at_ms": at_ms},
        questions=questions, source_refs=refs,
        principal_id=principal_id, scopes=scopes, allow_remote=allow_remote,
        policy=policy, max_attempts=max_attempts,
        max_cost_usd_micros=max_cost_usd_micros, deadline_s=deadline_s)
    answers = (run.get("receipt") or {}).get("answers") or {}
    scores = {}
    if run.get("status") == "completed" and run.get("rollout_mode") != "shadow":
        for index, cap in enumerate(capabilities):
            answer = answers.get(f"relevance_{index}") or {}
            value = answer.get("value") if answer.get("status") == "answered" else None
            if type(value) in {int, float} and 0 <= value <= 3:
                scores[cap["capability_id"]] = {
                    "score": round(value / 3, 8),
                    "objective_input_hash": objective["input_hash"],
                    "capability_content_hash": cap["content_hash"],
                    "evaluation_ref": run.get("evaluation_ref") or "unaccepted-machine-advice",
                    "decision_run_id": run_id,
                }
    # Re-run all hard filters and arithmetic from the planner. This is a dry-run
    # comparison: no source plan, acquisition, or approval is persisted.
    advised = planner.preview(namespace, objective_id, scopes=scopes, at_ms=at_ms,
                              credential_available=credential_available,
                              semantic_scores=scores) if scores else None
    return {"contract": CONTRACT, "status": "suggested" if scores else "pending",
            "objective_id": objective_id, "objective_input_hash": objective["input_hash"],
            "eligible_capability_ids": sorted(eligible), "source_binding": run.get("source_binding", []),
            "baseline_plan": baseline, "advised_plan": advised, "scores": scores,
            "accepted": False, "plan_persisted": False, "execution_authorized": False,
            "decision_run": run}
