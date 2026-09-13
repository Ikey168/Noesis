"""Read-only composition of a paired topic, existing loop receipts, and synthesis."""

from __future__ import annotations

import json

from src.kb.intake_modes import IntakeError, IntakeStore
from src.kb.intake_research_bundle import IntakeResearchBundleStore
from src.kb.research_loops import ResearchLoopRuntimeError, ResearchLoopStore
from src.kb.research_projects import ResearchProjectStore

CONTRACT = "noesis-intake-research-progress-v1"


def _has_table(conn, table):
    return bool(conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
        [table],
    ).fetchone())


def inspect_research_progress(conn, namespace, session_id, *, principal_id, scopes):
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
                bundle = {"bundle_id": row[0], "revision": None,
                          "checks": {"ready": False, "source_status": {}, "independent_hosts": 0}}
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
                stages = []
                if run_id and ("knowledge:recipes:read" in scopes or "operator" in scopes):
                    if _has_table(conn, "research_recipe_runs") and _has_table(conn, "research_recipe_checkpoints"):
                        authorized = conn.execute(
                            "SELECT 1 FROM research_recipe_runs WHERE run_id=? AND namespace=? "
                            "AND (principal_id=? OR ?)",
                            [run_id, namespace, principal_id, "operator" in scopes],
                        ).fetchone()
                        if authorized:
                            checkpoints = conn.execute(
                                "SELECT step_id,status,attempt,input_hash,output_hash,error_json "
                                "FROM research_recipe_checkpoints WHERE run_id=? ORDER BY ordinal LIMIT 101",
                                [run_id],
                            ).fetchall()
                            if len(checkpoints) > 100:
                                limitations.append("only_first_hundred_stage_receipts_included")
                            for step_id, step_status, attempt, input_hash, output_hash, error_json in checkpoints[:100]:
                                error = json.loads(error_json) if error_json else None
                                stages.append({"step_id": step_id, "status": step_status,
                                               "attempt": int(attempt), "input_hash": input_hash,
                                               "output_hash": output_hash,
                                               "error_code": error.get("code") if isinstance(error, dict) else None})
                elif run_id:
                    limitations.append("stage_receipts_need_knowledge_recipes_read")
                actions.append({"ordinal": int(ordinal), "attempts": int(attempts),
                                "status": status, "recipe_run_id": run_id, "stages": stages})
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

    return {
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
                                                "checks": bundle["checks"]},
        "loops": loops, "blockers": sorted(set(blockers)),
        "limitations": sorted(set(limitations)),
    }
