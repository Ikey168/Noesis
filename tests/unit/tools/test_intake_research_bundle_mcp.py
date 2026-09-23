import asyncio
import json
from pathlib import Path

import duckdb
import jsonschema

from src.kb.research_loops import ResearchLoopStore
from src.mcp_host.catalog import _mutability, _required_scopes
from tools.knowledge_engine_mcp import server


def test_research_bundle_mcp_discovery_and_scopes(tmp_path, monkeypatch):
    path = str(tmp_path / "bundle-mcp.duckdb")
    duckdb.connect(path).close()
    scopes = {
        "knowledge:intake:read", "knowledge:intake:write",
        "knowledge:projects:read", "knowledge:projects:write",
        "namespace:research:read", "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(server, "_connection",
                        lambda *, read_only: duckdb.connect(path, read_only=read_only))
    tools = asyncio.run(server.mcp.get_tools())
    started = tools["start_intake_research_topic"].fn(
        namespace="research", request_key="topic", questions=["Why?"],
        success_criteria=["Explain why"],
        scope={"domains": [], "namespaces": ["research"]}, budget={"requests": 1},
    )
    document = {
        "cards": [], "claims": [], "concepts": [],
        "brief": {"text": "", "card_ids": []},
        "mental_model": {"text": "", "card_ids": []},
        "map": {"text": "", "card_ids": []},
        "known": [], "uncertain": [], "unresolved": [],
        "definition_of_done": [{"criterion": "Explain why", "met": False,
                                "rationale": "Evidence has not been gathered", "card_ids": []}],
    }
    saved = tools["save_intake_research_bundle"].fn(
        namespace="research", project_id=started["project"]["project_id"],
        command_key="draft", document=document,
    )
    assert saved["contract"] == "noesis-intake-research-bundle-v1"
    assert not saved["checks"]["ready"]
    identity = {"namespace": "research", "bundle_id": saved["bundle_id"]}
    assert tools["inspect_intake_research_bundle"].fn(**identity)["revision"] == 1
    progress = tools["inspect_intake_research_progress"].fn(
        namespace="research", session_id=started["session"]["session_id"],
    )
    assert progress["bundle"]["id"] == saved["bundle_id"]
    assert progress["blockers"] == ["research_bundle_unready"]
    assert progress["loops"] == []
    assert "no_accessible_research_loop_receipts" in progress["limitations"]
    assessment = tools["assess_intake_research_progress"].fn(
        namespace="research", session_id=started["session"]["session_id"],
        command_key="review-1",
    )
    assert assessment["contract"] == "noesis-intake-research-assessment-v1"
    assert not assessment["assessment"]["ready"]
    assert "research_loop_missing" in {
        blocker["code"] for blocker in assessment["assessment"]["blockers"]
    }
    assert tools["assess_intake_research_progress"].fn(
        namespace="research", session_id=started["session"]["session_id"],
        command_key="review-1",
    )["idempotent"]
    assert tools["inspect_intake_research_assessment"].fn(
        namespace="research", assessment_id=assessment["assessment_id"],
    )["assessment_id"] == assessment["assessment_id"]
    with duckdb.connect(path) as conn:
        ResearchLoopStore(conn)
        loop_id = "research-loop:fixture"
        definition = {"project_id": started["project"]["project_id"],
                      "namespace": "research", "owner": "alice",
                      "actions": [{"gap_namespace": "research", "plan_namespace": "research"}],
                      "limits": {"independent_sources_per_domain": 2}}
        state = {"coverage": {"study": ["source-a"]}, "stop_reason": "configured_coverage_met",
                 "completed_iterations": 1, "results": 2}
        conn.execute("INSERT INTO research_loops VALUES (?,?,?,?,'completed',false,?,NULL)",
                     [loop_id, "research", started["project"]["project_id"],
                      json.dumps(definition), json.dumps(state)])
        conn.execute("INSERT INTO research_loop_actions VALUES (?,0,1,'completed',?)",
                     [loop_id, json.dumps({"run_id": "recipe-run:fixture"})])
        conn.execute("INSERT INTO research_recipe_runs "
                     "(run_id,namespace,recipe_revision_id,run_key,input_hash,status,cancel_requested,"
                     "state_json,error_json,receipt_json,principal_id,started_at_ms,updated_at_ms) "
                     "VALUES (?,?,'recipe:fixture','run','input','completed',false,'{}',NULL,NULL,?,1,2)",
                     ["recipe-run:fixture", "research", "alice"])
        conn.execute("INSERT INTO research_recipe_checkpoints "
                     "(checkpoint_id,run_id,step_id,ordinal,status,attempt,input_hash,output_hash,"
                     "output_json,error_json,tool_version,started_at_ms,completed_at_ms) "
                     "VALUES ('checkpoint:fixture',?,'acquire',0,'completed',1,'input','output',NULL,NULL,'v1',1,2)",
                     ["recipe-run:fixture"])
    limited = tools["inspect_intake_research_progress"].fn(
        namespace="research", session_id=started["session"]["session_id"],
    )
    assert limited["loops"][0]["actions"][0]["stages"] == []
    assert "stage_receipts_need_knowledge_recipes_read" in limited["limitations"]
    scopes.add("knowledge:recipes:read")
    observed = tools["inspect_intake_research_progress"].fn(
        namespace="research", session_id=started["session"]["session_id"],
    )
    assert observed["loops"][0]["coverage"] == {"study": 1}
    assert observed["loops"][0]["actions"][0]["stages"][0]["output_hash"] == "output"
    jsonschema.validate(
        observed,
        json.loads((Path(__file__).resolve().parents[3] /
                    "contracts/schemas/jsonschema/noesis-intake-research-progress-v1.json").read_text()),
    )
    exported = tools["export_intake_research_bundle"].fn(**identity)
    assert tools["verify_intake_research_bundle_export"].fn(exported)["valid"]
    assert _mutability("save_intake_research_bundle") == "write"
    assert _mutability("assess_intake_research_progress") == "write"
    assert _required_scopes("knowledge_engine_mcp", "write", "save_intake_research_bundle") == [
        "knowledge:intake:write", "knowledge:projects:write",
    ]
    assert _required_scopes("knowledge_engine_mcp", "read", "inspect_intake_research_progress") == [
        "knowledge:intake:read", "knowledge:projects:read",
    ]
    assert _required_scopes("knowledge_engine_mcp", "write", "assess_intake_research_progress") == [
        "knowledge:intake:write", "knowledge:projects:read",
    ]
    assert _required_scopes("knowledge_engine_mcp", "read", "inspect_intake_research_assessment") == [
        "knowledge:intake:read", "knowledge:projects:read",
    ]
    scopes.remove("knowledge:projects:read")
    denied = tools["inspect_intake_research_bundle"].fn(**identity)
    assert denied["error"]["code"] == "unauthorized"
