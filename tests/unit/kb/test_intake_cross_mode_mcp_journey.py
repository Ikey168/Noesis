"""Deterministic MCP ledger journeys; live Modulo and user outcomes remain separate."""

import asyncio

import duckdb
from fastmcp import Client, FastMCP

from src.kb.intake_modes import IntakeError
from tools.knowledge_engine_mcp.intake import register


def test_ten_mode_handoffs_discard_pause_replay_export_and_revocation(tmp_path):
    path = str(tmp_path / "journey.duckdb")
    caller = {
        "id": "alice",
        "scopes": {"knowledge:intake:read", "knowledge:intake:write",
                   "namespace:research:read", "namespace:research:write"},
    }
    mcp = FastMCP("ten-mode-ledger-fixture")

    def safe(operation, *, write=False, required_scope=None):
        if required_scope not in caller["scopes"]:
            return {"ok": False, "error": {"code": "unauthorized", "message": "missing scope"}}
        conn = duckdb.connect(path, read_only=not write)
        try:
            return operation(conn)
        except IntakeError as exc:
            return {"ok": False, "error": {"code": exc.code, "message": str(exc)}}
        finally:
            conn.close()

    register(mcp, safe, lambda: (caller["id"], caller["scopes"]))
    plugin_link = {
        "workspace_id": "personal", "account_id": "alice-account",
        "plugin_id": "feeds-reading-inbox", "collection": "items",
        "record_id": "feed-1", "authoritative_version": 2,
        "representation": "linked_projection", "authority": "modulo",
    }

    async def exercise():
        async with Client(mcp) as client:
            tools = {tool.name for tool in await client.list_tools()}
            assert {"discover_intake_workflows", "start_intake_mode",
                    "command_intake_mode", "export_modulo_intake_handoff"} <= tools

            async def call(name, **arguments):
                result = await client.call_tool(name, arguments)
                assert not result.is_error, result
                return result.data

            routed = await call("route_intake_mode", answers={"urgent_or_broken": True})
            assert routed["mode"] == "Problem-Solving"
            discard = await call(
                "start_intake_mode", namespace="research", mode="Awareness",
                request_key="discard", intent="Scan and discard",
                inputs={"feed_item_ids": ["feed-discard"]},
                plugin_links=[plugin_link],
            )
            recorded = await call(
                "command_intake_mode", namespace="research",
                session_id=discard["session_id"], command_key="decide-discard",
                expected_revision=1, action="record",
                payload={"data": {"decisions": {"feed-discard": "discard"}}},
            )
            done = await call(
                "command_intake_mode", namespace="research",
                session_id=discard["session_id"], command_key="complete-discard",
                expected_revision=recorded["revision"], action="complete",
            )
            assert done["status"] == "completed"
            replayed = await call(
                "command_intake_mode", namespace="research",
                session_id=discard["session_id"], command_key="complete-discard",
                expected_revision=recorded["revision"], action="complete",
            )
            assert replayed["idempotent"] and replayed["revision"] == done["revision"]

            awareness = await call(
                "start_intake_mode", namespace="research", mode="Awareness",
                request_key="escalate", intent="Scan and escalate",
                inputs={"feed_item_ids": ["feed-1"]},
                plugin_links=[plugin_link],
                references=[{"kind": "source", "id": "feed-source",
                             "namespace": "research", "version": 1}],
            )
            exploration = await call(
                "start_intake_mode", namespace="research", mode="Exploration",
                request_key="explore", intent="Follow one question",
                origin={"session_id": awareness["session_id"], "reason": "Worth exploring"},
            )
            assert exploration["references"] == awareness["references"]
            assert exploration["plugin_links"] == awareness["plugin_links"]
            no_output = await call(
                "command_intake_mode", namespace="research",
                session_id=exploration["session_id"], command_key="no-output",
                expected_revision=1, action="record",
                payload={"data": {"escalation_reason": "Timebox ended without a useful lead"}},
            )
            assert no_output["references"] == awareness["references"]
            finished_exploration = await call(
                "command_intake_mode", namespace="research",
                session_id=exploration["session_id"], command_key="finish-no-output",
                expected_revision=no_output["revision"], action="complete",
            )
            assert finished_exploration["status"] == "completed"
            assert finished_exploration["unmet_completion_checks"] == []
            research = await call(
                "start_intake_mode", namespace="research", mode="Deep Research",
                request_key="research", intent="Investigate the remaining question",
                origin={"session_id": finished_exploration["session_id"], "reason": "Question needs evidence"},
            )
            paused = await call(
                "command_intake_mode", namespace="research",
                session_id=research["session_id"], command_key="pause",
                expected_revision=1, action="pause",
            )
            discovered = await call("discover_intake_workflows", namespace="research", mode="Deep Research")
            current = next(s for s in discovered["sessions"] if s["session_id"] == research["session_id"])
            assert current["status"] == "paused" and "resume" in current["allowed_next_actions"]
            resumed = await call(
                "command_intake_mode", namespace="research",
                session_id=research["session_id"], command_key="resume",
                expected_revision=paused["revision"], action="resume",
            )
            assert resumed["status"] == "active"

            chain = [("Decision Support", "decision"), ("Creation", "creation"),
                     ("Externalization", "procedure"), ("Iteration", "iteration")]
            origin = research
            produced = {}
            for mode, key in chain:
                child = await call(
                    "start_intake_mode", namespace="research", mode=mode,
                    request_key=key, intent=f"Fixture {mode}",
                    origin={"session_id": origin["session_id"], "reason": f"Continue to {mode}"},
                )
                assert child["references"] == awareness["references"]
                produced[key] = child
                origin = child
            problem = await call(
                "start_intake_mode", namespace="research", mode="Problem-Solving",
                request_key="problem", intent="Diagnose a separate problem",
            )
            playbook = await call(
                "start_intake_mode", namespace="research", mode="Externalization",
                request_key="problem-playbook", intent="Record a draft procedure",
                origin={"session_id": problem["session_id"], "reason": "Reported fix"},
            )
            assert playbook["origin"]["mode"] == "Problem-Solving"
            practice = await call(
                "start_intake_mode", namespace="research", mode="Internalization",
                request_key="practice", intent="Practice without notes",
                origin={"session_id": produced["creation"]["session_id"],
                        "reason": "Practice the created concept"},
            )
            maintenance = await call(
                "start_intake_mode", namespace="research", mode="Maintenance",
                request_key="maintenance", intent="Review current health",
                origin={"session_id": produced["iteration"]["session_id"],
                        "reason": "Check later stability"},
            )
            assert practice["mode"] == "Internalization"
            assert maintenance["duration_minutes"] == 45
            handoff = await call(
                "export_modulo_intake_handoff", namespace="research",
                session_id=exploration["session_id"], contract_version="v3",
            )
            assert handoff["plugin_links"][0]["record_id"] == "feed-1"
            assert handoff["plugin_access_state"] == "not_checked_by_noesis"
            exported = await call("export_intake_mode", namespace="research",
                                  session_id=research["session_id"])
            assert (await call("verify_intake_mode_export", bundle=exported))["valid"]
            assert (await call(
                "start_intake_mode", namespace="research", mode="Deep Research",
                request_key="research", intent="Investigate the remaining question",
                origin={"session_id": exploration["session_id"], "reason": "Question needs evidence"},
            ))["idempotent"]
            caller["scopes"] = caller["scopes"] - {"namespace:research:read"}
            denied = await call("export_modulo_intake_handoff", namespace="research",
                                session_id=exploration["session_id"], contract_version="v3")
            assert denied["error"]["code"] == "unauthorized"

    asyncio.run(exercise())
