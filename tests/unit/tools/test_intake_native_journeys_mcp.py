"""Cross-mode journeys over native artifacts through a real MCP client (#1583).

Unlike the ledger-only journey, every step here calls the public
knowledge-engine MCP server through ``fastmcp.Client`` and creates the native
objects each mode owns: feed items, captured sources, Research Bundles,
Decision Records, authored reports, procedures, practice reviews, skills and
Maintenance receipts. Each tool call opens a fresh DuckDB connection, so every
step also survives a process-level restart of the store. These are
deterministic transport and state checks; they are not live-source,
signed-in Modulo, or user-outcome evidence.
"""

from __future__ import annotations

import asyncio
from typing import Any

import duckdb
from fastmcp import Client

from src.kb import intake_inbox
from tools.knowledge_engine_mcp import server

NS = "research"
BASE_SCOPES = {
    "knowledge:intake:read", "knowledge:intake:write", "knowledge:intake:fetch",
    "knowledge:intake:review",
    "knowledge:projects:read", "knowledge:projects:write",
    "knowledge:recipes:read",
    "knowledge:decisions:read", "knowledge:decisions:write",
    "knowledge:reports:read", "knowledge:reports:write",
    f"namespace:{NS}:read", f"namespace:{NS}:write",
}
FEED = (
    b'<rss version="2.0"><channel>'
    b"<item><title>Index lag grows</title><link>https://example.org/lag</link><guid>lag</guid></item>"
    b"<item><title>Unrelated promo</title><link>https://example.org/promo</link><guid>promo</guid></item>"
    b"<item><title>Index lag grows</title><link>https://example.org/lag</link><guid>lag</guid></item>"
    b"</channel></rss>"
)


class Journey:
    """One MCP client session against an isolated warehouse file."""

    def __init__(self, tmp_path, monkeypatch, principal="alice"):
        self.path = str(tmp_path / "journey.duckdb")
        duckdb.connect(self.path).close()
        self.principal = principal
        self.scopes = set(BASE_SCOPES)
        monkeypatch.setattr(server, "_context", lambda: (self.principal, self.scopes))
        monkeypatch.setattr(server, "_connection",
                            lambda *, read_only: duckdb.connect(self.path, read_only=read_only))
        monkeypatch.setattr(intake_inbox, "_fetch_public_feed", lambda _url: FEED)

    async def call(self, client: Client, tool: str, /, **arguments: Any) -> dict:
        result = await client.call_tool(tool, arguments, raise_on_error=False)
        assert not result.is_error, (tool, result)
        data = result.structured_content
        if isinstance(data, dict) and set(data) == {"result"}:
            data = data["result"]
        return data

    async def ok(self, client: Client, tool: str, /, **arguments: Any) -> dict:
        data = await self.call(client, tool, **arguments)
        assert "error" not in data, (tool, data)
        return data

    async def error(self, client: Client, tool: str, /, **arguments: Any) -> str:
        data = await self.call(client, tool, **arguments)
        assert "error" in data, (tool, data)
        return data["error"]["code"]

    def run(self, scenario) -> None:
        async def main() -> None:
            async with Client(server.mcp) as client:
                await scenario(client)

        asyncio.run(main())


async def _capture_sources(j: Journey, client: Client, key: str) -> tuple[dict, list[dict]]:
    session = await j.ok(client, "start_intake_mode", namespace=NS, mode="Exploration",
                         request_key=f"{key}-sources", intent="Collect sources", inputs={})
    refs = []
    for index, (url, text) in enumerate((
        ("https://example.org/one", "Index lag follows worker restarts."),
        ("https://example.net/two", "Worker restarts precede index lag."),
    ), 1):
        captured = await j.ok(client, "capture_exploration_page", namespace=NS,
                              session_id=session["session_id"], command_key=f"{key}-capture-{index}",
                              expected_revision=index, url=url, title=f"Source {index}",
                              content=text, saved=True)
        refs.append(captured["references"][-1])
    return session, refs


def _bundle_document(refs: list[dict]) -> dict:
    texts = ("Index lag follows worker restarts.", "Worker restarts precede index lag.")
    cards = [{"id": f"card-{i}", "quote": text, "summary": text,
              "source": {"namespace": NS, "id": ref["id"], "version": ref["version"],
                         "start": 0, "end": len(text)}}
             for i, (ref, text) in enumerate(zip(refs, texts), 1)]
    return {
        "cards": cards,
        "claims": [{"id": "claim-1", "statement": "Worker restarts cause index lag",
                    "supports": ["card-1", "card-2"], "contradicts": [], "confidence": "high",
                    "independence_review": {"status": "independent",
                                            "basis": "Separate reporting origins.",
                                            "groups": [{"group_id": "a", "card_ids": ["card-1"]},
                                                       {"group_id": "b", "card_ids": ["card-2"]}]}}],
        "concepts": [{"id": "concept-1", "name": "Restart lag",
                      "explanation": "A worker restart delays indexing", "card_ids": ["card-1"]}],
        "brief": {"text": "Restarts cause lag", "card_ids": ["card-1", "card-2"]},
        "mental_model": {"text": "Restart -> backlog -> lag", "card_ids": ["card-1"]},
        "map": {"text": "Worker -> index", "card_ids": ["card-2"]},
        "known": [{"text": "Lag follows restarts", "card_ids": ["card-1", "card-2"]}],
        "uncertain": [{"text": "Lag duration varies", "card_ids": []}],
        "unresolved": [{"text": "Whether batching helps", "card_ids": []}],
        "definition_of_done": [{"criterion": "Explain the lag", "met": True,
                                "rationale": "Two independent sources", "card_ids": ["card-1", "card-2"]}],
    }


async def _ready_research(j: Journey, client: Client, key: str, refs: list[dict], origin=None) -> dict:
    started = await j.ok(client, "start_intake_research_topic", namespace=NS, request_key=f"{key}-topic",
                         questions=["Why does the index lag?"], success_criteria=["Explain the lag"],
                         scope={"domains": [], "namespaces": [NS]}, budget={"requests": 5},
                         references=refs, origin=origin)
    document = _bundle_document(refs)
    saved = await j.ok(client, "save_intake_research_bundle", namespace=NS,
                       project_id=started["project"]["project_id"], command_key=f"{key}-bundle",
                       document=document)
    assert not saved["checks"]["ready"]  # independence not yet reviewed
    reviewed = await j.ok(client, "review_research_claim_independence", namespace=NS,
                          bundle_id=saved["bundle_id"], expected_revision=1, command_key=f"{key}-review",
                          claim_id="claim-1", status="independent", basis="Checked bylines.",
                          groups=document["claims"][0]["independence_review"]["groups"])
    assert reviewed["bundle"]["checks"]["ready"]
    session = started["session"]
    bundle_ref = {"kind": "research_bundle", "id": saved["bundle_id"], "namespace": NS, "version": 1}
    linked = await j.ok(client, "command_intake_mode", namespace=NS, session_id=session["session_id"],
                        command_key=f"{key}-link", expected_revision=session["revision"],
                        action="record", payload={"references": [bundle_ref]})
    done = await j.ok(client, "command_intake_mode", namespace=NS, session_id=session["session_id"],
                      command_key=f"{key}-done", expected_revision=linked["revision"], action="complete")
    assert done["status"] == "completed"
    return {"started": started, "bundle": saved, "bundle_ref": bundle_ref, "session": done}


def test_awareness_discard_and_awareness_exploration_research(tmp_path, monkeypatch):
    j = Journey(tmp_path, monkeypatch)

    async def scenario(client: Client) -> None:
        await j.ok(client, "subscribe_intake_feed", namespace=NS, url="https://example.org/rss", name="Ops")
        refreshed = await j.ok(client, "refresh_intake_feed_inbox", namespace=NS)
        assert refreshed["results"][0]["created"] == 2  # duplicate GUID collapsed
        inbox = await j.ok(client, "list_intake_feed_inbox", namespace=NS)
        assert inbox["remaining_unprocessed"] == 2
        items = {item["title"]: item["item_id"] for item in inbox["items"]}
        awareness = await j.ok(client, "start_awareness_from_inbox", namespace=NS, request_key="today")

        # Awareness -> Discard, retried with the same command key.
        triage = dict(namespace=NS, session_id=awareness["session_id"], item_id=items["Unrelated promo"],
                      command_key="discard-promo", expected_revision=1, decision="discard")
        discarded = await j.ok(client, "triage_awareness_item", **triage)
        assert (await j.ok(client, "triage_awareness_item", **triage))["idempotent"]

        # Awareness -> Exploration keeps the feed item's identity.
        escalated = await j.ok(client, "triage_awareness_item", namespace=NS,
                               session_id=awareness["session_id"], item_id=items["Index lag grows"],
                               command_key="escalate-lag", expected_revision=discarded["revision"],
                               decision="escalate")
        promoted = await j.ok(client, "promote_awareness_item", namespace=NS,
                              awareness_session_id=awareness["session_id"], item_id=items["Index lag grows"],
                              request_key="explore-lag", target_mode="Exploration",
                              reason="Worth a look", intent="Follow the lag")
        assert any(ref["id"] == items["Index lag grows"] for ref in promoted["references"])
        assert (await j.ok(client, "list_intake_feed_inbox", namespace=NS))["remaining_unprocessed"] == 0
        closed = await j.ok(client, "command_intake_mode", namespace=NS, session_id=awareness["session_id"],
                            command_key="close-awareness", expected_revision=escalated["revision"],
                            action="complete")
        assert closed["status"] == "completed"

        # Lightweight exploration: no output, ends by a recorded choice to stop.
        idle = await j.ok(client, "start_intake_mode", namespace=NS, mode="Exploration",
                          request_key="idle", intent="Browse", inputs={})
        stopped = await j.ok(client, "command_intake_mode", namespace=NS, session_id=idle["session_id"],
                             command_key="nothing", expected_revision=1, action="record",
                             payload={"data": {"escalation_reason": "Nothing worth keeping"}})
        assert (await j.ok(client, "command_intake_mode", namespace=NS, session_id=idle["session_id"],
                           command_key="idle-done", expected_revision=stopped["revision"],
                           action="complete"))["status"] == "completed"

        # Exploration -> Research with the captured sources pinned and cited.
        _, refs = await _capture_sources(j, client, "lag")
        research = await _ready_research(j, client, "lag", refs,
                                         origin={"session_id": promoted["session_id"],
                                                 "reason": "Escalated from Exploration"})
        project = research["started"]["project"]
        # The topic pins the captured sources and inherits the escalated feed item.
        linked = [link["id"] for link in project["links"]]
        assert linked == [items["Index lag grows"], *(ref["id"] for ref in refs)]
        exported = await j.ok(client, "export_intake_research_bundle", namespace=NS,
                              bundle_id=research["bundle"]["bundle_id"])
        assert (await j.ok(client, "verify_intake_research_bundle_export", bundle=exported))["valid"]

    j.run(scenario)


DECISION = {
    "project": None,
    "decision_context": {"question": "Batch index writes?", "stakes": "Search freshness",
                         "required_confidence": "Moderate", "stop_condition": "Lag cause explained",
                         "uncertainty": "Lag duration varies", "missing_inputs": ["Batch size cost"],
                         "deadline_at_ms": None},
    "options": [{"id": "batch", "description": "Batch writes after restarts"},
                {"id": "keep", "description": "Keep immediate writes"}],
    "constraints": ["Keep results within five minutes"], "assumptions": ["Restarts stay rare"],
    "observations": [], "preferences": ["Fresh results"], "selected_action": "batch",
    "rationale": "Research shows restarts cause the lag", "review_conditions": ["Lag above five minutes"],
}
REPORT = {
    "title": "Restart recovery note",
    "snapshot": {"id": "snapshot:journey", "generations": {NS: 1}},
    "sections": [{"id": "summary", "title": "Summary", "assertions": [{
        "id": "a1", "text": "Batch writes after a worker restart.", "kind": "commentary",
        "dependencies": [], "citations": []}]}],
    "bibliography": [], "limitations": ["Author-reviewed"],
}
STEPS = [
    {"action": "Check worker uptime", "expected_result": "Restart time known", "recovery": "Read logs"},
    {"action": "Enable batched writes", "expected_result": "Lag falls", "recovery": "Disable batching"},
]


async def _guided_rehearsal(j, client, playbook, key, environment="Staging"):
    run = await j.ok(client, "start_guided_playbook_run", namespace=NS, playbook_id=playbook["playbook_id"],
                     request_key=key, playbook_revision=playbook["revision"], environment=environment)
    for command_key, action, payload in (
        (f"{key}-1", "step", {"step_id": "step-1", "passed": True, "observation": "Uptime read"}),
        (f"{key}-2", "step", {"step_id": "step-2", "passed": True, "observation": "Lag fell"}),
        (f"{key}-v", "verify", {"passed": True, "observation": "Search fresh within a minute"}),
    ):
        run = await j.ok(client, "command_guided_playbook_run", namespace=NS, run_id=run["run_id"],
                         command_key=command_key, expected_revision=run["revision"], action=action, payload=payload)
    return run


def test_research_decision_creation_externalization_iteration(tmp_path, monkeypatch):
    j = Journey(tmp_path, monkeypatch)

    async def scenario(client: Client) -> None:
        _, refs = await _capture_sources(j, client, "chain")
        research = await _ready_research(j, client, "chain", refs)
        research_session = research["session"]

        # Decision bounded by the research evidence; closes while research is done.
        decision = await j.ok(client, "create_research_decision", namespace=NS, request_key="batch", content=DECISION)
        decision_ref = {"kind": "decision", "id": decision["decision_id"], "namespace": NS,
                        "version": decision["revision"]}
        choosing = await j.ok(client, "start_intake_mode", namespace=NS, mode="Decision Support",
                              request_key="batch-choice", intent="Batch writes?", inputs={},
                              references=[decision_ref, research["bundle_ref"]],
                              origin={"session_id": research_session["session_id"], "reason": "Research informs choice"})
        chosen = await j.ok(client, "command_intake_mode", namespace=NS, session_id=choosing["session_id"],
                            command_key="choose", expected_revision=1, action="record",
                            payload={"data": {"selected_option": "batch", "rationale": DECISION["rationale"]}})
        assert (await j.ok(client, "command_intake_mode", namespace=NS, session_id=choosing["session_id"],
                           command_key="decided", expected_revision=chosen["revision"],
                           action="complete"))["status"] == "completed"

        # Creation cites the bundle and the decision; export is not publication.
        report = await j.ok(client, "create_authored_report", namespace=NS, request_key="note", content=REPORT)
        project = await j.ok(client, "start_intake_creation", namespace=NS, request_key="note-project",
                             title="Restart recovery note", audience="Operators", artifact_type="documentation",
                             purpose="Explain the batching choice", criteria=["Cites the decision"],
                             inputs=[research["bundle_ref"], decision_ref])
        step = project
        for key, action, payload in (
            ("attach", "attach_report", {"report_id": report["report_id"], "revision": report["revision"]}),
            ("review", "review", {"checks": {"Cites the decision": True}, "notes": "Checked"}),
            ("finish", "finish", None),
        ):
            step = await j.ok(client, "command_intake_creation", namespace=NS, project_id=project["project_id"],
                              command_key=key, expected_revision=step["revision"], action=action, payload=payload)
        exported = await j.ok(client, "export_intake_creation", namespace=NS, project_id=project["project_id"])
        assert exported["publication_authorized"] is False
        assert {ref["kind"] for ref in exported["project"]["inputs"]} == {"research_bundle", "decision"}

        # Creation -> Externalization: the finished artifact becomes a checklist.
        handoff = await j.ok(client, "handoff_intake_creation", namespace=NS, project_id=project["project_id"],
                             request_key="externalize-note", destination_mode="Externalization",
                             reason="Offload the recovery steps")
        checklist = await j.ok(client, "promote_intake_work_procedure", namespace=NS,
                               session_id=handoff["session_id"], request_key="recovery-checklist",
                               artifact_kind="checklist", title="Restart recovery", prerequisites=["Admin"],
                               environment="Staging", steps=STEPS, verification="Search fresh within a minute",
                               source_rationale="From the finished recovery note")
        await _guided_rehearsal(j, client, checklist, "rehearse-1")
        inspected = await j.ok(client, "inspect_intake_playbook", namespace=NS, playbook_id=checklist["playbook_id"])
        assert inspected["trust_state"] == "rehearsed_reported"
        procedure_ref = {"kind": "procedure", "id": checklist["playbook_id"], "namespace": NS, "version": 1}
        recorded = await j.ok(client, "command_intake_mode", namespace=NS, session_id=handoff["session_id"],
                              command_key="externalized", expected_revision=handoff["revision"], action="record",
                              payload={"references": [procedure_ref],
                                       "data": {"rehearsal_or_execution": "Guided rehearsal in Staging"}})
        externalized = await j.ok(client, "command_intake_mode", namespace=NS, session_id=handoff["session_id"],
                                  command_key="externalized-done", expected_revision=recorded["revision"],
                                  action="complete")
        assert externalized["status"] == "completed"

        # Iteration: a measured outcome revises the original checklist.
        cycle = await j.ok(client, "start_intake_iteration", namespace=NS, request_key="checklist-cycle",
                           playbook_id=checklist["playbook_id"], expected_revision=1,
                           expected="Lag under one minute after restarts", stability_criteria="Three restarts under a minute",
                           intent="Did batching work?", origin_session_id=handoff["session_id"])
        observed = await j.ok(client, "record_intake_iteration_outcome", namespace=NS, session_id=cycle["session_id"],
                              command_key="measured", expected_revision=cycle["revision"],
                              observed="Lag was 3 minutes after one restart", learning="Batch size too large",
                              measurements=[{"metric": "lag", "expected": "under 1", "observed": "3", "unit": "minutes"}],
                              uncertainty="Single observation", external_causes="Traffic spike", evidence=[])
        proposal = await j.ok(client, "propose_intake_playbook_revision", namespace=NS, session_id=cycle["session_id"],
                              command_key="smaller-batch", expected_revision=observed["revision"],
                              title="Restart recovery", prerequisites=["Admin"], environment="Staging",
                              steps=[STEPS[0], {**STEPS[1], "action": "Enable batched writes of 100"}],
                              verification="Search fresh within a minute", source_rationale="Measured lag",
                              before_after_rationale="Smaller batches after measured 3-minute lag")
        accepted = await j.ok(client, "accept_intake_playbook_revision", namespace=NS, session_id=cycle["session_id"],
                              command_key="accept", expected_revision=proposal["revision"])
        assert accepted["status"] in {"active", "completed"}
        revised = await j.ok(client, "inspect_intake_playbook", namespace=NS, playbook_id=checklist["playbook_id"])
        assert revised["revision"] == 2 and revised["artifact_kind"] == "checklist"
        assert revised["trust_state"] == "draft"  # a revision needs a new rehearsal
        assert revised["iteration_history"][-1]["outcome"]["measurements"][0]["observed"] == "3"

    j.run(scenario)


def test_problem_to_playbook_and_research_to_internalization(tmp_path, monkeypatch):
    j = Journey(tmp_path, monkeypatch)

    async def scenario(client: Client) -> None:
        _, refs = await _capture_sources(j, client, "fix")
        research = await _ready_research(j, client, "fix", refs)

        # Problem-Solving resolves only with an observed verification.
        problem = await j.ok(client, "start_problem_session", namespace=NS, request_key="lagging",
                             symptom="Search results are ten minutes stale", environment="Desktop 2.7",
                             urgency="blocking", success_check="New item searchable within a minute")
        hypothesis = await j.ok(client, "record_problem_step", namespace=NS, session_id=problem["session_id"],
                                command_key="h1", expected_revision=1, kind="hypothesis",
                                summary="Worker restart left a backlog", next_action="Batch writes")
        unverified = await j.error(client, "command_intake_mode", namespace=NS, session_id=problem["session_id"],
                                   command_key="too-early", expected_revision=hypothesis["revision"],
                                   action="complete")
        assert unverified == "incomplete_mode"
        verified = await j.ok(client, "record_problem_step", namespace=NS, session_id=problem["session_id"],
                              command_key="v1", expected_revision=hypothesis["revision"], kind="verification",
                              summary="Searched for a new item", observation="Found after 20 seconds", passed=True,
                              references=[refs[0]])
        await j.ok(client, "command_intake_mode", namespace=NS, session_id=problem["session_id"],
                   command_key="fixed", expected_revision=verified["revision"], action="complete")
        concept_ref = {"kind": "concept", "id": "concept-1", "namespace": NS, "version": 1}
        playbook = await j.ok(client, "promote_problem_playbook", namespace=NS, problem_session_id=problem["session_id"],
                              request_key="lag-playbook", title="Clear restart lag", prerequisites=["Admin"],
                              environment="Desktop 2.7", steps=STEPS, verification="New item within a minute",
                              source_rationale="Verified troubleshooting session", concept_references=[concept_ref])
        assert playbook["trust_state"] == "draft" and concept_ref in playbook["references"]
        await _guided_rehearsal(j, client, playbook, "lag-run")

        # Research -> Internalization: a reviewed pack from the bundle concept.
        selections = [{"reference": research["bundle_ref"], "practice_kind": "explanation",
                       "item_kind": "concept", "item_id": "concept-1"}]
        draft = await j.ok(client, "draft_intake_practice_pack", namespace=NS, selections=selections)
        pack = await j.ok(client, "create_reviewed_intake_practice_pack", namespace=NS, request_key="lag-pack",
                          title="Restart lag", selections=selections, draft_sha256=draft["sha256"],
                          reviewed_cards=[{"prompt": "Why does the index lag after a restart?",
                                           "answer": "The restart leaves a write backlog.",
                                           "mastery_criterion": "Explain unaided", "approved": True}])
        review = await j.ok(client, "start_practice_review", namespace=NS, pack_id=pack["pack_id"],
                            card_id="card-1", request_key="first-review")
        assert "answer" not in review  # hidden until an attempt is recorded
        for key, action, payload in (
            ("attempt", "attempt", {"answer": "Backlog from the restart", "assisted": False}),
            ("reveal", "reveal", {}),
            ("assess", "assess", {"passed": True, "notes": "Matched"}),
        ):
            review = await j.ok(client, "command_practice_review", namespace=NS, review_id=review["review_id"],
                                command_key=key, expected_revision=review["revision"], action=action, payload=payload)
        assert review["assistance"] == "reported_unaided"

        # A skill connects the complementary procedure and concept practice.
        skill = await j.ok(client, "create_intake_skill", namespace=NS, request_key="restart-lag",
                           name="Restart lag", description="Explain and clear restart lag",
                           mastery_criteria=["Explain unaided", "Clear lag in Staging"],
                           practice_links=[{"pack_id": pack["pack_id"], "card_id": "card-1"}],
                           procedure_links=[{"playbook_id": playbook["playbook_id"]}])
        evidence = await j.ok(client, "inspect_intake_skill_evidence", namespace=NS, skill_id=skill["skill_id"])
        assert {item["basis"] for item in evidence["evidence"]} == {
            "self_reported_unaided_recall", "caller_reported_rehearsal"}
        assert evidence["mastery_demonstrated"] is False
        # The owner cannot certify their own mastery.
        assert await j.error(client, "assess_intake_skill", namespace=NS, skill_id=skill["skill_id"],
                             command_key="self", expected_revision=1, criterion="Explain unaided",
                             passed=True, evidence_note="me") == "self_assessment"

    j.run(scenario)


def test_maintenance_over_stale_sources_procedures_learning_and_automation(tmp_path, monkeypatch):
    from src.kb.maintenance import MaintenanceOrchestrator

    j = Journey(tmp_path, monkeypatch)
    j.scopes |= {"knowledge:maintenance:admin"}

    async def scenario(client: Client) -> None:
        sources, refs = await _capture_sources(j, client, "maint")
        research = await _ready_research(j, client, "maint", refs)
        project_id = research["started"]["project"]["project_id"]
        pack = await j.ok(client, "create_practice_pack", namespace=NS, request_key="maint-pack", title="Lag",
                          cards=[{"kind": "recall", "prompt": "What follows a restart?", "answer": "Index lag",
                                  "mastery_criterion": "Recall unaided", "references": [refs[0]]}])
        # Outdated procedure: its latest guided rehearsal failed.
        problem = await j.ok(client, "start_problem_session", namespace=NS, request_key="maint-problem",
                             symptom="Stale search", environment="Desktop", urgency="high",
                             success_check="Fresh search")
        verified = await j.ok(client, "record_problem_step", namespace=NS, session_id=problem["session_id"],
                              command_key="v", expected_revision=1, kind="verification", summary="Checked",
                              observation="Fresh", passed=True, references=[refs[1]])
        await j.ok(client, "command_intake_mode", namespace=NS, session_id=problem["session_id"],
                   command_key="c", expected_revision=verified["revision"], action="complete")
        playbook = await j.ok(client, "promote_problem_playbook", namespace=NS, problem_session_id=problem["session_id"],
                              request_key="maint-playbook", title="Fix stale search", prerequisites=[],
                              environment="Desktop", steps=STEPS, verification="Fresh search",
                              source_rationale="Verified problem")
        run = await j.ok(client, "start_guided_playbook_run", namespace=NS, playbook_id=playbook["playbook_id"],
                         request_key="maint-run", playbook_revision=1, environment="Desktop")
        await j.ok(client, "command_guided_playbook_run", namespace=NS, run_id=run["run_id"], command_key="fail",
                   expected_revision=1, action="step",
                   payload={"step_id": "step-1", "passed": False, "observation": "Uptime API removed"})
        # Broken automation: a dead-lettered worker job.
        with duckdb.connect(j.path) as conn:
            MaintenanceOrchestrator(conn, execution_mode="fixture", now=lambda: 1000)
            conn.execute("INSERT INTO knowledge_maintenance_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         ["job:feeds", "key:feeds", "pack:feeds", 1000, "{}", "d", "dead-letter",
                          1000, None, None, None, 2, 2, False, "{}", None, 1000, 2000])
        # Stale source and learning material: the first source is corrected.
        await j.ok(client, "capture_exploration_page", namespace=NS, session_id=sources["session_id"],
                   command_key="maint-correct", expected_revision=3, url="https://example.org/one",
                   title="Source 1 (corrected)", content="Correction: lag follows deploys.", saved=True)

        review = await j.ok(client, "start_intake_maintenance", namespace=NS, request_key="monthly",
                            intent="Monthly review", duration_minutes=45)
        reasons = {item["reason"]: item for item in review["inputs"]["findings"]}
        assert {"superseded_research_source", "superseded_practice_source", "failed_guided_rehearsal",
                "failed_automation"} <= set(reasons)

        async def act(finding_reason, action, preview_action, key, revision):
            finding = reasons[finding_reason]
            preview = await j.ok(client, "preview_intake_maintenance_impact", namespace=NS,
                                 finding_id=finding["id"], action=preview_action)
            done = await j.ok(client, "execute_intake_maintenance_action", namespace=NS,
                              session_id=review["session_id"], command_key=key, expected_revision=revision,
                              finding_id=finding["id"], action=action, preview_hash=preview["preview_hash"])
            return preview, done

        preview, repaired = await act("superseded_research_source", "repair", "refresh", "repin", review["revision"])
        assert any(item["kind"] == "intake_session" for item in preview["affected"])
        assert repaired["maintenance_action"]["outcome"]["repinned_revision"] == 2
        before = await j.ok(client, "inspect_research_project", namespace=NS, project_id=project_id, revision=1)
        assert before["links"][0]["revision"] == 1  # the original pin stays recoverable
        _, refreshed = await act("failed_automation", "refresh", "refresh", "requeue", repaired["revision"])
        assert refreshed["maintenance_action"]["outcome"]["status"] == "retry"
        assert (await j.ok(client, "inspect_maintenance_job", job_id="job:feeds"))["status"] == "retry"
        deferred = await j.ok(client, "record_maintenance_finding", namespace=NS, session_id=review["session_id"],
                              command_key="defer-card", expected_revision=refreshed["revision"],
                              finding_id=reasons["superseded_practice_source"]["id"], action="defer",
                              observation="Rewrite the card after the deploy study")
        reviewed = await j.ok(client, "record_maintenance_finding", namespace=NS, session_id=review["session_id"],
                              command_key="rehearse-again", expected_revision=deferred["revision"],
                              finding_id=reasons["failed_guided_rehearsal"]["id"], action="repair_reported",
                              observation="Step 1 needs the new uptime command")
        for index, finding in enumerate(review["inputs"]["findings"]):
            if finding["id"] in reviewed["data"]["checklist"]:
                continue
            reviewed = await j.ok(client, "record_maintenance_finding", namespace=NS,
                                  session_id=review["session_id"], command_key=f"review-{index}",
                                  expected_revision=reviewed["revision"], finding_id=finding["id"],
                                  action="reviewed", observation="Checked")
        health = await j.ok(client, "assess_maintenance_health", namespace=NS, session_id=review["session_id"],
                            command_key="health", expected_revision=reviewed["revision"], acceptable=True,
                            criteria="No unrecoverable failures", observation="One card deferred")
        closed = await j.ok(client, "command_intake_mode", namespace=NS, session_id=review["session_id"],
                            command_key="close", expected_revision=health["revision"], action="complete")
        assert closed["status"] == "completed"
        assert closed["data"]["checklist"][reasons["superseded_practice_source"]["id"]] == "deferred"
        due = await j.ok(client, "list_due_practice", namespace=NS)
        assert due["cards"][0]["source_status"] == "superseded"  # deferred work stays visible
        assert pack["pack_id"] == due["cards"][0]["pack_id"]

    j.run(scenario)


def test_fresh_deployment_import_interruption_duplicates_revocation_correction_export(tmp_path, monkeypatch):
    from src.kb.intake_modes import _hash

    j = Journey(tmp_path, monkeypatch)

    async def scenario(client: Client) -> None:
        # Fresh deployment: discovery and preflight work on an empty warehouse.
        discovered = await j.ok(client, "discover_intake_workflows", namespace=NS)
        assert len(discovered["modes"]) == 10 and discovered["sessions"] == []
        assert discovered["live_source_verified"] is False
        preflight = await j.ok(client, "preflight_intake_mode", namespace=NS, mode="Awareness")
        assert preflight["enabled_feed_subscription_count"] == 0

        # Imported data: exact Modulo flashcard identity, logs and schedule.
        value = {"card_id": "modulo-card-1", "question": "What causes restart lag?", "answer": "A write backlog",
                 "review_logs": [{"rating": 3, "reviewed_at_ms": 100}],
                 "schedule_params": {"algorithm": "FSRS-6", "weights": [0.1]}}
        record = {"record_id": "rec-1", "authoritative_version": 2, "fields": list(value), "relation_ids": [],
                  "attachment_ids": [], "content_sha256": _hash(value), "source_locator": {}}
        inventory = {"workspace_id": "personal", "account_id": "alice", "observed_at_ms": 50,
                     "source": "caller_supplied_plugin_state",
                     "plugins": [{"plugin_id": "flashcards-spaced-repetition", "installed_version": "5.0",
                                  "collections": [{"collection": "cards", "schema_id": "modulo.flashcard",
                                                   "schema_version": 5, "persistence": "authenticated_plugin_state",
                                                   "records": [record]}]}]}
        preview = await j.ok(client, "preview_modulo_intake_migration", namespace=NS, request_key="import",
                             inventory=inventory, mappings=[])
        source = [{"plugin_id": "flashcards-spaced-repetition", "collection": "cards", "record_id": "rec-1",
                   "authoritative_version": 2, "content_sha256": _hash(value), "value": value,
                   "mastery_criterion": "Recall unaided"}]
        imported = await j.ok(client, "import_modulo_flashcards", namespace=NS, preview_id=preview["preview_id"],
                              request_key="import-cards", title="Imported", source_values=source)
        replayed = await j.ok(client, "import_modulo_flashcards", namespace=NS, preview_id=preview["preview_id"],
                              request_key="import-cards", title="Imported", source_values=source)
        assert replayed["pack_id"] == imported["pack_id"]
        assert imported["modulo_import"]["records"][0]["source_review_history"] == value["review_logs"]

        # Interruption: pause, resume and duplicate commands on one session.
        start = dict(namespace=NS, mode="Exploration", request_key="browse", intent="Browse", inputs={})
        session = await j.ok(client, "start_intake_mode", **start)
        assert (await j.ok(client, "start_intake_mode", **start))["idempotent"]
        pause = dict(namespace=NS, session_id=session["session_id"], command_key="pause",
                     expected_revision=1, action="pause")
        paused = await j.ok(client, "command_intake_mode", **pause)
        assert paused["status"] == "paused"
        assert (await j.ok(client, "command_intake_mode", **pause))["idempotent"]
        resumed = await j.ok(client, "command_intake_mode", namespace=NS, session_id=session["session_id"],
                             command_key="resume", expected_revision=paused["revision"], action="resume")
        assert resumed["status"] == "active"
        assert await j.error(client, "command_intake_mode", namespace=NS, session_id=session["session_id"],
                             command_key="stale", expected_revision=1, action="pause") == "revision_conflict"

        # Insufficient evidence and correction: the topic cannot complete.
        sources, refs = await _capture_sources(j, client, "thin")
        started = await j.ok(client, "start_intake_research_topic", namespace=NS, request_key="thin-topic",
                             questions=["Why?"], success_criteria=["Explain the lag"],
                             scope={"domains": [], "namespaces": [NS]}, budget={"requests": 2}, references=refs)
        document = _bundle_document(refs)
        document["claims"][0]["confidence"] = "low"
        document["definition_of_done"][0].update(met=False, rationale="Only correlation observed")
        thin = await j.ok(client, "save_intake_research_bundle", namespace=NS,
                          project_id=started["project"]["project_id"], command_key="thin", document=document)
        assert not thin["checks"]["ready"]
        research = started["session"]
        linked = await j.ok(client, "command_intake_mode", namespace=NS, session_id=research["session_id"],
                            command_key="link", expected_revision=research["revision"], action="record",
                            payload={"references": [{"kind": "research_bundle", "id": thin["bundle_id"],
                                                     "namespace": NS, "version": 1}]})
        assert await j.error(client, "command_intake_mode", namespace=NS, session_id=research["session_id"],
                             command_key="done", expected_revision=linked["revision"],
                             action="complete") in {"incomplete_mode", "research_bundle_unready"}
        await j.ok(client, "capture_exploration_page", namespace=NS, session_id=sources["session_id"],
                   command_key="thin-correct", expected_revision=3, url="https://example.org/one",
                   title="Retracted", content="This report was retracted.", saved=True)
        project = await j.ok(client, "inspect_research_project", namespace=NS,
                             project_id=started["project"]["project_id"])
        assert project["reference_availability"][0]["status"] == "superseded"

        # Export verification and tamper detection.
        exported = await j.ok(client, "export_intake_mode", namespace=NS, session_id=session["session_id"])
        assert (await j.ok(client, "verify_intake_mode_export", bundle=exported))["valid"]
        exported["revisions"][0]["intent"] = "Tampered"
        assert not (await j.ok(client, "verify_intake_mode_export", bundle=exported))["valid"]

        # Access revocation hides the session and its artifacts.
        j.scopes.discard(f"namespace:{NS}:read")
        j.scopes.discard(f"namespace:{NS}:write")
        assert await j.error(client, "inspect_intake_mode", namespace=NS,
                             session_id=session["session_id"]) == "unauthorized"
        assert await j.error(client, "inspect_intake_research_bundle", namespace=NS,
                             bundle_id=thin["bundle_id"]) == "unauthorized"
        j.principal = "mallory"
        j.scopes = set(BASE_SCOPES)
        assert await j.error(client, "inspect_intake_mode", namespace=NS,
                             session_id=session["session_id"]) in {"unauthorized", "session_not_found"}

    j.run(scenario)
