"""Compose paired-topic progress and persist replayable readiness assessments."""

from __future__ import annotations

import json
import time

from src.kb.coverage_assessments import CoverageAssessmentStore
from src.kb.intake_modes import IntakeError, IntakeStore, _hash, _json, _text
from src.kb.intake_research_bundle import IntakeResearchBundleStore
from src.kb.research_loops import ResearchLoopRuntimeError, ResearchLoopStore
from src.kb.research_projects import ResearchProjectStore

CONTRACT = "noesis-intake-research-progress-v1"
ASSESSMENT_CONTRACT = "noesis-intake-research-assessment-v1"
_ASSESSMENT_DDL = """
CREATE TABLE IF NOT EXISTS intake_research_progress_assessments(
 assessment_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 session_id TEXT NOT NULL, command_key TEXT NOT NULL, request_hash TEXT NOT NULL,
 input_hash TEXT NOT NULL, payload_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
 UNIQUE(namespace,owner,session_id,command_key));
"""


def _has_table(conn, table):
    return bool(conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
        [table],
    ).fetchone())


def inspect_research_progress(
    conn, namespace, session_id, *, principal_id, scopes,
    coverage_assessment_id=None,
):
    session = IntakeStore(conn, initialize=False).inspect(
        namespace, session_id, principal_id=principal_id, scopes=scopes,
    )
    project_id = session["inputs"].get("research_project_id")
    if session["mode"] != "Deep Research" or not project_id:
        raise IntakeError("invalid_mode", "paired Deep Research session is required")
    project = ResearchProjectStore(conn, initialize=False).inspect(
        namespace, project_id, principal_id=principal_id, scopes=scopes,
    )
    if project["owner"] != session["owner"]:
        raise IntakeError("scope_mismatch", "topic and project owners differ")

    blockers = []
    limitations = []
    bundle = None
    if _has_table(conn, "intake_research_bundles"):
        row = conn.execute(
            "SELECT bundle_id FROM intake_research_bundles "
            "WHERE namespace=? AND owner=? AND project_id=?",
            [namespace, session["owner"], project_id],
        ).fetchone()
        if row:
            try:
                bundle = IntakeResearchBundleStore(conn, initialize=False).inspect(
                    namespace, row[0], principal_id=principal_id, scopes=scopes,
                )
            except IntakeError as exc:
                if exc.code not in {"source_not_found", "revision_not_found", "unpinned_source"}:
                    raise
                bundle = {"bundle_id": row[0], "revision": None, "document": {},
                          "checks": {"ready": False, "source_status": {},
                                     "independent_hosts": 0,
                                     "reasons": ["research_bundle_source_unavailable"]}}
                blockers.append("research_bundle_source_unavailable")
    if bundle is None:
        blockers.append("research_bundle_missing")
    elif not bundle["checks"]["ready"]:
        blockers.append("research_bundle_unready")

    loops = []
    if _has_table(conn, "research_loops"):
        ids = conn.execute(
            "SELECT loop_id FROM research_loops WHERE namespace=? AND project_id=? "
            "ORDER BY loop_id LIMIT 21", [namespace, project_id],
        ).fetchall()
        if len(ids) > 20:
            limitations.append("only_first_twenty_research_loops_included")
        store = ResearchLoopStore(conn, initialize=False)
        for (loop_id,) in ids[:20]:
            try:
                loop = store.inspect_loop(
                    namespace, loop_id, principal_id=principal_id, scopes=scopes,
                )
            except ResearchLoopRuntimeError as exc:
                if exc.code != "unauthorized":
                    raise
                limitations.append("some_research_loops_outside_current_scope")
                continue
            action_rows = conn.execute(
                "SELECT ordinal,attempts,status,result_json FROM research_loop_actions "
                "WHERE loop_id=? ORDER BY ordinal LIMIT 101", [loop_id],
            ).fetchall() if _has_table(conn, "research_loop_actions") else []
            if len(action_rows) > 100:
                limitations.append("only_first_hundred_loop_actions_included")
            actions = []
            for ordinal, attempts, status, result_json in action_rows[:100]:
                result = json.loads(result_json) if result_json else {}
                run_id = result.get("run_id")
                action_definition = loop["actions"][int(ordinal)] if int(ordinal) < len(loop["actions"]) else {}
                if (not run_id and action_definition.get("recipe_revision_id")
                        and _has_table(conn, "research_recipe_runs")):
                    run_row = conn.execute(
                        "SELECT run_id FROM research_recipe_runs WHERE namespace=? "
                        "AND recipe_revision_id=? AND run_key=?",
                        [namespace, action_definition["recipe_revision_id"], f"{loop_id}:{ordinal}"],
                    ).fetchone()
                    if run_row:
                        run_id = run_row[0]
                stages = []
                recipe_run_status = None
                if run_id and ("knowledge:recipes:read" in scopes or "operator" in scopes):
                    if _has_table(conn, "research_recipe_runs") and _has_table(conn, "research_recipe_checkpoints"):
                        authorized = conn.execute(
                            "SELECT status FROM research_recipe_runs WHERE run_id=? AND namespace=? "
                            "AND (principal_id=? OR ?)",
                            [run_id, namespace, principal_id, "operator" in scopes],
                        ).fetchone()
                        if authorized:
                            recipe_run_status = authorized[0]
                            checkpoints = conn.execute(
                                "SELECT step_id,status,attempt,input_hash,output_hash,error_json,tool_version "
                                "FROM research_recipe_checkpoints WHERE run_id=? ORDER BY ordinal LIMIT 101",
                                [run_id],
                            ).fetchall()
                            if len(checkpoints) > 100:
                                limitations.append("only_first_hundred_stage_receipts_included")
                            for step_id, step_status, attempt, input_hash, output_hash, error_json, tool_version in checkpoints[:100]:
                                error = json.loads(error_json) if error_json else None
                                stages.append({"step_id": step_id, "status": step_status,
                                               "attempt": int(attempt), "input_hash": input_hash,
                                               "output_hash": output_hash,
                                               "error_code": error.get("code") if isinstance(error, dict) else None,
                                               "tool_version": tool_version})
                elif run_id:
                    limitations.append("stage_receipts_need_knowledge_recipes_read")
                actions.append({"ordinal": int(ordinal), "attempts": int(attempts),
                                "domain": action_definition.get("domain"),
                                "status": status, "recipe_run_id": run_id,
                                "recipe_run_status": recipe_run_status, "stages": stages})
            state = loop["state"]
            loops.append({
                "loop_id": loop_id, "status": loop["status"],
                "stop_reason": state.get("stop_reason"),
                "coverage": {domain: len(sources) for domain, sources in state.get("coverage", {}).items()},
                "required_independent_sources_per_domain": loop["limits"]["independent_sources_per_domain"],
                "completed_iterations": state.get("completed_iterations", 0),
                "result_count": state.get("results", 0),
                "actions": actions,
            })
            if loop["status"] in {"blocked", "cancelled", "stopped"}:
                blockers.append("research_loop_" + loop["status"])
    if not loops:
        limitations.append("no_accessible_research_loop_receipts")

    coverage = None
    if coverage_assessment_id is not None:
        assessment = CoverageAssessmentStore(conn, initialize=False).inspect(
            namespace, coverage_assessment_id, principal_id=principal_id,
            scopes=scopes, limit=64,
        )
        statuses = {}
        for cell in assessment["cells"]:
            statuses[cell["status"]] = statuses.get(cell["status"], 0) + 1
        coverage = {
            "assessment_id": assessment["assessment_id"],
            "denominator": assessment["denominator"],
            "status_counts": statuses,
            "scope": assessment["scope"],
            "limitations": assessment["limitations"],
        }
        if statuses.get("unavailable") or statuses.get("unattempted"):
            blockers.append("coverage_gaps_need_review")
        limitations.append("coverage_assessment_selected_by_caller_not_project_bound")

    dod_reviews = []
    if bundle is not None:
        for review in bundle.get("document", {}).get("definition_of_done", []):
            dod_reviews.append({"criterion": review["criterion"], "met": review["met"],
                                "cited_card_count": len(review["card_ids"])})
    progress = {
        "contract": CONTRACT,
        "session": {"id": session_id, "revision": session["revision"], "status": session["status"],
                    "unmet_completion_checks": session["unmet_completion_checks"]},
        "project": {"id": project_id, "revision": project["revision"],
                    "question_revision": project["question_revision"],
                    "status": project["status"], "questions": project["questions"],
                    "success_criteria": project["success_criteria"],
                    "budget": project["budget"], "spent": project["spent"],
                    "source_status": [{"id": link["id"], "revision": link["revision"],
                                       "status": availability["status"]}
                                      for link, availability in zip(project["links"],
                                                                    project["reference_availability"], strict=True)
                                      if link["kind"] == "intake_source"]},
        "bundle": None if bundle is None else {"id": bundle["bundle_id"],
                                                "revision": bundle["revision"],
                                                "checks": bundle["checks"],
                                                "definition_of_done": dod_reviews},
        "coverage_assessment": coverage,
        "loops": loops, "blockers": sorted(set(blockers)),
        "limitations": sorted(set(limitations)),
    }
    progress["assessment"] = _evaluate_progress(progress)
    return progress


def _evaluate_progress(progress):
    """Turn inspectable receipts into structural readiness checks and next steps."""
    blockers = []

    def add(code, target, next_action):
        blockers.append({"code": code, "target": target, "next_action": next_action})

    bundle = progress["bundle"]
    if bundle is None:
        add("research_bundle_missing", {"kind": "session", "id": progress["session"]["id"]},
            "Build and save a cited synthesis bundle for the paired project.")
    else:
        checks = bundle["checks"]
        reasons = set(checks.get("reasons", []))
        if not checks.get("ready"):
            if not reasons:
                reasons.add("research_bundle_unready")
            for reason in sorted(reasons):
                actions = {
                    "evidence_cards_missing": "Add Evidence Cards with exact spans from pinned source revisions.",
                    "claims_missing": "Record claims and link their supporting and contradicting cards.",
                    "concepts_missing": "Add concepts with cited Evidence Cards.",
                    "brief_missing": "Write a cited L1 Brief.",
                    "mental_model_missing": "Write a cited Mental Model.",
                    "map_missing": "Add a cited research map.",
                    "known_missing": "State at least one known point and cite its cards.",
                    "uncertain_missing": "State the remaining uncertainty.",
                    "unresolved_missing": "Record unresolved questions explicitly.",
                    "definition_of_done_unmet": "Review each unmet criterion; gather more evidence or leave the topic open.",
                    "source_revision_superseded": "Review the corrected source, capture its current revision, and revise affected cards.",
                    "project_revision_changed": "Inspect the revised project and recheck the bundle against its current revision.",
                    "project_question_revision_changed": "Reconcile changed questions and rerun affected research before revising the bundle.",
                    "research_bundle_source_unavailable": "Restore access to the pinned source or replace it with an accessible source revision.",
                }
                add(reason, {"kind": "research_bundle", "id": bundle["id"]},
                    actions.get(reason, "Inspect the cited bundle revision and address this readiness check."))

    coverage = progress.get("coverage_assessment")
    if coverage:
        statuses = coverage["status_counts"]
        if statuses.get("unavailable") or statuses.get("unattempted"):
            add(
                "coverage_gaps_need_review",
                {"kind": "coverage_assessment", "id": coverage["assessment_id"],
                 "status_counts": statuses},
                "Review unavailable or unattempted coverage cells; gather evidence or record why those cells remain uncovered.",
            )

    for source in progress["project"]["source_status"]:
        if source["status"] != "current":
            add("project_source_" + source["status"],
                {"kind": "intake_source", "id": source["id"], "revision": source["revision"]},
                "Review the pinned source revision and update the project before relying on it.")

    loops = progress["loops"]
    if not loops:
        add("research_loop_missing", {"kind": "project", "id": progress["project"]["id"]},
            "Create and run a bounded research loop for the selected project questions and gap tasks.")

    required_stage_ids = ("acquire", "derive", "query")
    total_stages = 0
    completed_stages = 0
    receipt_access_missing = "stage_receipts_need_knowledge_recipes_read" in progress["limitations"]
    for loop in loops:
        if loop["status"] in {"blocked", "cancelled", "stopped"}:
            reason = loop.get("stop_reason") or loop["status"]
            action = ("Resolve the reported loop blocker, then resume the same loop if its project questions remain current."
                      if loop["status"] == "blocked" else
                      "Review the stop reason and source coverage; resume or create a bounded follow-up loop.")
            add("research_loop_" + loop["status"],
                {"kind": "research_loop", "id": loop["loop_id"], "stop_reason": reason}, action)
        if receipt_access_missing and any(item.get("recipe_run_id") for item in loop["actions"]):
            add("recipe_stage_receipts_unavailable",
                {"kind": "research_loop", "id": loop["loop_id"]},
                "Grant knowledge:recipes:read to inspect acquisition, extraction, and evidence-assessment receipts.")
        for action in loop["actions"]:
            domain = action.get("domain")
            for step_id in required_stage_ids:
                total_stages += 1
                stage = next((item for item in action["stages"] if item["step_id"] == step_id), None)
                if stage is not None and stage["status"] == "completed":
                    completed_stages += 1
                    continue
                if receipt_access_missing:
                    continue
                target = {"kind": "research_stage", "loop_id": loop["loop_id"],
                          "action_ordinal": action["ordinal"], "step_id": step_id}
                if stage is None:
                    add("research_stage_receipt_missing", target,
                        "Run or resume the bounded loop and inspect the resulting stage receipt.")
                else:
                    add("research_stage_incomplete", {**target, "error_code": stage.get("error_code")},
                        "Inspect the stage error, correct its cause, then resume the bounded loop.")
            if not domain:
                add("research_loop_domain_missing",
                    {"kind": "research_loop_action", "loop_id": loop["loop_id"],
                     "action_ordinal": action["ordinal"]},
                    "Recreate the loop with an explicit project domain for each selected action.")

        domains = sorted({item.get("domain") for item in loop["actions"] if item.get("domain")})
        required = loop["required_independent_sources_per_domain"]
        for domain in domains:
            observed = loop["coverage"].get(domain, 0)
            if observed < required:
                add("source_coverage_incomplete",
                    {"kind": "research_coverage", "loop_id": loop["loop_id"],
                     "domain": domain, "observed": observed, "required": required},
                    "Acquire distinct-source evidence for this domain or state explicitly why coverage remains insufficient.")

    if not loops and receipt_access_missing:
        add("recipe_stage_receipts_unavailable",
            {"kind": "session", "id": progress["session"]["id"]},
            "Grant knowledge:recipes:read to inspect stage receipts.")

    reviews = {item["criterion"]: item for item in (bundle or {}).get("definition_of_done", [])}
    stale_bundle = bool(bundle and set(bundle["checks"].get("reasons", [])) & {
        "source_revision_superseded", "project_revision_changed", "project_question_revision_changed",
    })
    dod = []
    for criterion in progress["project"]["success_criteria"]:
        review = reviews.get(criterion)
        reviewed = review is not None
        met = bool(review and review["met"])
        if not reviewed:
            status = "unreviewed"
        elif stale_bundle:
            status = "needs_review"
        elif met:
            status = "met"
        else:
            status = "not_met"
        dod.append({"criterion": criterion, "status": status, "reviewed": reviewed,
                    "recorded_met": met,
                    "cited_card_count": review["cited_card_count"] if review else 0})
        if status in {"unreviewed", "needs_review", "not_met"}:
            add("definition_of_done_" + status,
                {"kind": "definition_of_done_criterion", "criterion": criterion},
                "Review the criterion against current cited evidence; retain an explicit unmet result when evidence is insufficient.")

    for check in progress["session"]["unmet_completion_checks"]:
        add("session_completion_check_unmet",
            {"kind": "session_completion_check", "check": check},
            "Record the current research bundle revision on the paired session, then re-inspect completion checks.")

    unique = {_json(item): item for item in blockers}
    ordered = [unique[key] for key in sorted(unique)]
    return {
        "ready": not ordered,
        "bundle_ready": bool(bundle and bundle["checks"].get("ready")),
        "required_stage_ids": list(required_stage_ids),
        "stage_receipt_count": total_stages,
        "completed_stage_receipt_count": completed_stages,
        "definition_of_done": dod,
        "blockers": ordered,
        "limitations": [
            "A stage receipt proves that its configured step completed, not that its substantive conclusions are correct.",
            "Different source hosts are a limited independence check and do not prove separate authorship or corroboration.",
            "Definition of Done results are author-reviewed against structurally validated citations; substantive support is not independently verified.",
        ],
        "substantive_support_verified": False,
    }


class ResearchProgressAssessmentStore:
    """Store immutable, replayable readiness snapshots for paired topics."""

    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_ASSESSMENT_DDL)

    def assess(self, namespace, session_id, command_key, *, principal_id, scopes):
        command_key = _text(command_key, "command_key", limit=256)
        progress = inspect_research_progress(
            self.conn, namespace, session_id,
            principal_id=principal_id, scopes=scopes,
        )
        session = IntakeStore(self.conn, initialize=False).inspect(
            namespace, session_id, principal_id=principal_id, scopes=scopes,
        )
        project_id = progress["project"]["id"]
        request = {"namespace": namespace, "owner": session["owner"],
                   "session_id": session_id, "command_key": command_key}
        request_hash = _hash(request)
        assessment_id = "research-progress-assessment:" + _hash(request)[:32]
        prior = self.conn.execute(
            "SELECT request_hash,payload_json FROM intake_research_progress_assessments "
            "WHERE namespace=? AND owner=? AND session_id=? AND command_key=?",
            [namespace, session["owner"], session_id, command_key],
        ).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise IntakeError("idempotency_conflict", "assessment key identifies another request")
            return {**json.loads(prior[1]), "idempotent": True}
        project = ResearchProjectStore(self.conn, initialize=False).inspect(
            namespace, project_id, principal_id=principal_id, scopes=scopes,
        )
        if project["owner"] != session["owner"]:
            raise IntakeError("scope_mismatch", "topic and project owners differ")
        state = {
            "contract": ASSESSMENT_CONTRACT, "assessment_id": assessment_id,
            "namespace": namespace, "owner": session["owner"], "session_id": session_id,
            "command_key": command_key, "assessed_at_ms": self.now(),
            "input_hash": _hash(progress), "snapshot": progress,
            "assessment": progress["assessment"], "idempotent": False,
        }
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO intake_research_progress_assessments VALUES (?,?,?,?,?,?,?,?,?)",
                [assessment_id, namespace, session["owner"], session_id, command_key,
                 request_hash, state["input_hash"], _json(state), state["assessed_at_ms"]],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            replay = self.conn.execute(
                "SELECT request_hash,payload_json FROM intake_research_progress_assessments "
                "WHERE namespace=? AND owner=? AND session_id=? AND command_key=?",
                [namespace, session["owner"], session_id, command_key],
            ).fetchone()
            if replay:
                if replay[0] != request_hash:
                    raise IntakeError("idempotency_conflict", "assessment key identifies another request")
                return {**json.loads(replay[1]), "idempotent": True}
            raise
        return state

    def inspect(self, namespace, assessment_id, *, principal_id, scopes):
        if not _has_table(self.conn, "intake_research_progress_assessments"):
            raise IntakeError("assessment_not_found", "research progress assessment is unavailable")
        row = self.conn.execute(
            "SELECT session_id,owner,payload_json FROM intake_research_progress_assessments "
            "WHERE namespace=? AND assessment_id=?", [namespace, assessment_id],
        ).fetchone()
        if not row:
            raise IntakeError("assessment_not_found", "research progress assessment is unavailable")
        inspect_research_progress(
            self.conn, namespace, row[0], principal_id=principal_id, scopes=scopes,
        )
        session = IntakeStore(self.conn, initialize=False).inspect(
            namespace, row[0], principal_id=principal_id, scopes=scopes,
        )
        if session["owner"] != row[1] or (principal_id != row[1] and "operator" not in scopes):
            raise IntakeError("unauthorized", "assessment owner is required")
        stored = json.loads(row[2])
        if ("operator" not in scopes and "knowledge:recipes:read" not in scopes
                and any(action.get("stages") for loop in stored["snapshot"]["loops"]
                        for action in loop["actions"])):
            raise IntakeError("unauthorized", "knowledge:recipes:read is required for saved stage receipts")
        return stored
