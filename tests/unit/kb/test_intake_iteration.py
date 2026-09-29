"""Measured playbook iterations retain their accepted revision and replay safety."""

import duckdb
import pytest

from src.kb.intake_iteration import IntakeIterationStore
from src.kb.intake_modes import IntakeError, IntakeStore
from src.kb.intake_exploration import IntakeExplorationStore
from src.kb.intake_playbooks import IntakePlaybookStore
from src.kb.intake_problem import IntakeProblemStore
from src.kb.intake_research_bundle import IntakeResearchBundleStore
from src.kb.intake_research_topic import start_research_topic
from src.kb.decisions import DecisionError, DecisionStore
from src.kb.authored_reports import AuthoredReportStore

SCOPES = {"knowledge:intake:read", "knowledge:intake:write",
          "namespace:research:read", "namespace:research:write"}


def _current_verification_reference(conn, *, suffix="verification"):
    opened = IntakeStore(conn, now=lambda: 1000).create(
        "research", "Exploration", f"{suffix}-source-session",
        intent="Capture verification evidence", principal_id="alice", scopes=SCOPES,
    )
    captured = IntakeExplorationStore(conn, now=lambda: 1000).capture(
        "research", opened["session_id"], f"{suffix}-source",
        expected_revision=1, url=f"https://example.org/{suffix}",
        title="Verification observation", content="Observed the expected result.",
        principal_id="alice", scopes=SCOPES,
    )
    return captured["references"][0]


def _concept_bundle(conn):
    scopes = SCOPES | {
        "knowledge:projects:read", "knowledge:projects:write",
        "knowledge:recipes:read", "knowledge:recipes:write",
    }
    intake = IntakeStore(conn)
    opened = intake.create(
        "research", "Exploration", "iteration-concept-source",
        intent="Capture a source for a concept", principal_id="alice", scopes=scopes,
    )
    captured = IntakeExplorationStore(conn).capture(
        "research", opened["session_id"], "capture-concept-source",
        expected_revision=1, url="https://example.org/concept",
        title="Concept source", content="A concrete source passage supports this concept.",
        principal_id="alice", scopes=scopes,
    )
    reference = captured["references"][0]
    source_text = "A concrete source passage supports this concept."
    started = start_research_topic(
        conn, "research", "iteration-concept-topic", questions=["What does this show?"],
        success_criteria=["Explain the source"],
        scope={"domains": [], "namespaces": ["research"]},
        budget={"requests": 5}, origin=None, references=[reference],
        workspace_links=None, principal_id="alice", scopes=scopes,
    )
    document = {
        "cards": [{
            "id": "card-concept",
            "source": {"namespace": "research", "id": reference["id"],
                       "version": reference["version"], "start": 0,
                       "end": len(source_text)},
            "quote": source_text, "summary": source_text,
        }],
        "claims": [{"id": "claim-concept", "statement": "The passage supports the concept",
                    "supports": ["card-concept"], "contradicts": [], "confidence": "medium"}],
        "concepts": [{"id": "concept-1", "name": "Concept", "explanation": "Initial explanation",
                      "card_ids": ["card-concept"]}],
        "brief": {"text": "A brief", "card_ids": ["card-concept"]},
        "mental_model": {"text": "A model", "card_ids": ["card-concept"]},
        "map": {"text": "Source to concept", "card_ids": ["card-concept"]},
        "known": [{"text": "The passage is explicit", "card_ids": ["card-concept"]}],
        "uncertain": [{"text": "Generalization", "card_ids": []}],
        "unresolved": [{"text": "Find another source", "card_ids": []}],
        "definition_of_done": [{"criterion": "Explain the source", "met": True,
                                "rationale": "The cited passage states the point",
                                "card_ids": ["card-concept"]}],
    }
    bundle = IntakeResearchBundleStore(conn).save(
        "research", started["project"]["project_id"], "save-concept-bundle",
        document, principal_id="alice", scopes=scopes,
    )
    return bundle, scopes


def test_iteration_accepts_measured_playbook_revision_and_reopens_new_cycle(monkeypatch):
    conn = duckdb.connect(":memory:")
    kwargs = {"principal_id": "alice", "scopes": SCOPES}
    problem = IntakeProblemStore(conn, now=lambda: 1000)
    opened = problem.start("research", "broken-search", symptom="Search stale",
                           environment="Desktop", urgency="blocking",
                           success_check="New item visible", **kwargs)
    verification_reference = _current_verification_reference(conn)
    problem.record_step("research", opened["session_id"], "observed", expected_revision=1,
                        kind="verification", summary="Search again",
                        observation="New item visible", passed=True,
                        references=[verification_reference], **kwargs)
    IntakeStore(conn, now=lambda: 1000).command(
        "research", opened["session_id"], "resolve", expected_revision=2,
        action="complete", payload=None, **kwargs)
    playbooks = IntakePlaybookStore(conn, now=lambda: 1000)
    playbook = playbooks.promote_problem(
        "research", opened["session_id"], "guide", title="Repair search",
        prerequisites=[], environment="Desktop",
        steps=[{"action": "Restart worker", "expected_result": "Item visible",
                "recovery": "Inspect logs"}], verification="New item visible",
        source_rationale="Observed repair", **kwargs)
    store = IntakeIterationStore(conn, now=lambda: 2000)
    session = store.start("research", "cycle-one", playbook_id=playbook["playbook_id"],
                          expected_revision=1, expected="Search result in under 2 seconds",
                          stability_criteria="Three runs without a stale result",
                          intent="Check the repair after use", **kwargs)
    with pytest.raises(IntakeError, match="typed Iteration"):
        IntakeStore(conn, now=lambda: 2000).command(
            "research", session["session_id"], "bypass", expected_revision=1,
            action="record", payload={"data": {"learning": "fabricated"}}, **kwargs)
    observed = store.record_outcome(
        "research", session["session_id"], "observation", expected_revision=1,
        observed="Search took 5 seconds", learning="Restart alone is insufficient",
        measurements=[{"metric": "search latency", "expected": "2", "observed": "5",
                       "unit": "seconds"}], uncertainty="One run",
        external_causes="Heavy sync may contribute", evidence=[], **kwargs)
    assert store.record_outcome(
        "research", session["session_id"], "observation", expected_revision=1,
        observed="Search took 5 seconds", learning="Restart alone is insufficient",
        measurements=[{"metric": "search latency", "expected": "2", "observed": "5",
                       "unit": "seconds"}], uncertainty="One run",
        external_causes="Heavy sync may contribute", evidence=[], **kwargs)["idempotent"]
    proposed = store.propose(
        "research", session["session_id"], "proposal", expected_revision=observed["revision"],
        title="Repair search", prerequisites=[], environment="Desktop",
        steps=[{"action": "Restart and wait for sync", "expected_result": "Item visible",
                "recovery": "Inspect logs"}], verification="New item in two seconds",
        source_rationale="Observed slow search", before_after_rationale="Wait for sync after restart",
        **kwargs)
    original_command = IntakeStore.command
    interrupted = False

    def fail_session_write_once(self, namespace, session_id, command_key, **options):
        nonlocal interrupted
        if command_key == "accept" and not interrupted:
            interrupted = True
            raise IntakeError("interrupted", "simulate a failed session write")
        return original_command(self, namespace, session_id, command_key, **options)

    monkeypatch.setattr(IntakeStore, "command", fail_session_write_once)
    with pytest.raises(IntakeError, match="simulate a failed session write"):
        store.accept("research", session["session_id"], "accept",
                     expected_revision=proposed["revision"], **kwargs)
    assert playbooks.inspect("research", playbook["playbook_id"], **kwargs)["revision"] == 1
    assert IntakeStore(conn, now=lambda: 2000).inspect(
        "research", session["session_id"], **kwargs)["revision"] == proposed["revision"]
    accepted = store.accept("research", session["session_id"], "accept",
                            expected_revision=proposed["revision"], **kwargs)
    assert accepted["data"]["accepted_revision"]["revision"] == 2
    assert store.accept("research", session["session_id"], "accept",
                        expected_revision=proposed["revision"], **kwargs)["idempotent"]
    revised = playbooks.inspect("research", playbook["playbook_id"], **kwargs)
    assert revised["revision"] == 2
    assert revised["iteration_history"][0]["outcome"]["uncertainty"] == "One run"
    assert revised["iteration_history"][0]["proposal_sha256"]
    with pytest.raises(IntakeError, match="stability_review"):
        IntakeStore(conn, now=lambda: 2000).command(
            "research", session["session_id"], "too-early", expected_revision=accepted["revision"],
            action="complete", payload=None, **kwargs)
    stable = store.review_stability(
        "research", session["session_id"], "stability", expected_revision=accepted["revision"],
        stable=False, observation="One run is not enough to call stable", **kwargs)
    completed = IntakeStore(conn, now=lambda: 2000).command(
        "research", session["session_id"], "finish", expected_revision=stable["revision"],
        action="complete", payload=None, **kwargs)
    assert completed["status"] == "completed"
    next_cycle = store.start("research", "cycle-two", playbook_id=playbook["playbook_id"],
                             expected_revision=2, expected="Two-second result",
                             stability_criteria="Three clean runs", intent="New feedback",
                             origin_session_id=session["session_id"], **kwargs)
    assert next_cycle["origin"]["session_id"] == session["session_id"]
    assert store.start("research", "cycle-one", playbook_id=playbook["playbook_id"],
                       expected_revision=1, expected="Search result in under 2 seconds",
                       stability_criteria="Three runs without a stale result",
                       intent="Check the repair after use", **kwargs)["idempotent"]
    followup = store.record_outcome(
        "research", next_cycle["session_id"], "followup-observation",
        expected_revision=1, observed="Search still takes 3 seconds",
        learning="The sync wait helped but did not meet the target",
        measurements=[{"metric": "search latency", "expected": "2", "observed": "3",
                       "unit": "seconds"}], uncertainty="One follow-up run",
        external_causes="Index growth", evidence=[], **kwargs,
    )
    followup_proposal = store.propose(
        "research", next_cycle["session_id"], "followup-proposal",
        expected_revision=followup["revision"], title="Repair search",
        prerequisites=[], environment="Desktop",
        steps=[{"action": "Restart, wait for sync, and confirm indexing",
                "expected_result": "Item visible", "recovery": "Inspect worker logs"}],
        verification="New item in two seconds", source_rationale="Second field run",
        before_after_rationale="Add an index-completion check before search verification",
        **kwargs,
    )
    followup_accepted = store.accept(
        "research", next_cycle["session_id"], "followup-accept",
        expected_revision=followup_proposal["revision"], **kwargs,
    )
    followup_stability = store.review_stability(
        "research", next_cycle["session_id"], "followup-stability",
        expected_revision=followup_accepted["revision"], stable=False,
        observation="The latest run still exceeded the target", **kwargs,
    )
    playbooks.revise(
        "research", playbook["playbook_id"], "external-edit",
        expected_revision=3, title="Repair search", prerequisites=[],
        environment="Desktop", steps=[{
            "action": "Restart, wait for sync, confirm indexing, then search",
            "expected_result": "Item visible", "recovery": "Inspect worker logs",
        }], verification="New item in two seconds",
        source_rationale="An external edit after iteration acceptance", **kwargs,
    )
    with pytest.raises(IntakeError, match="accepted playbook revision must still be current"):
        IntakeStore(conn, now=lambda: 2000).command(
            "research", next_cycle["session_id"], "finish-stale-playbook",
            expected_revision=followup_stability["revision"],
            action="complete", payload=None, **kwargs,
        )


def test_iteration_gap_handoff_is_atomic_replayable_and_access_checked(tmp_path):
    path = str(tmp_path / "iteration-gap.duckdb")
    conn = duckdb.connect(path)
    kwargs = {"principal_id": "alice", "scopes": SCOPES}
    ledger = IntakeStore(conn, now=lambda: 1000)
    source = ledger.create(
        "research", "Iteration", "gap-source", intent="Review a changed decision",
        inputs={"iteration_contract": "noesis-intake-iteration-playbook-v1",
                "playbook": {"kind": "playbook", "id": "playbook:one",
                             "namespace": "research", "version": 1},
                "expected": "Expected", "stability_criteria": "Three checks"},
        references=[{"kind": "playbook", "id": "playbook:one",
                     "namespace": "research", "version": 1}], **kwargs,
    )
    store = IntakeIterationStore(conn, now=lambda: 1000)
    observed = store.record_outcome(
        "research", source["session_id"], "observed", expected_revision=1,
        observed="Mismatch", learning="Source gap", uncertainty="One observation",
        external_causes="Unclear", measurements=[{"metric": "count", "expected": "2",
                                                   "observed": "1", "unit": "items"}],
        evidence=[], **kwargs,
    )
    with pytest.raises(IntakeError, match="feed-item queue"):
        store.route_gap(
            "research", source["session_id"], "bad", expected_revision=2,
            target_mode="Awareness", reason="Need current sources", intent="Watch feed",
            inputs={}, references=[], **kwargs,
        )
    assert ledger.inspect("research", source["session_id"], **kwargs)["revision"] == 2
    routed = store.route_gap(
        "research", source["session_id"], "handoff", expected_revision=observed["revision"],
        target_mode="Exploration", reason="Evidence is missing", intent="Find source",
        inputs={}, references=[], **kwargs,
    )
    assert routed["parent"]["data"]["gap_handoffs"][0]["session_id"] == routed["child"]["session_id"]
    assert routed["child"]["origin"]["session_id"] == source["session_id"]
    assert store.route_gap(
        "research", source["session_id"], "handoff", expected_revision=observed["revision"],
        target_mode="Exploration", reason="Evidence is missing", intent="Find source",
        inputs={}, references=[], **kwargs,
    )["idempotent"]
    ledger.command(
        "research", routed["child"]["session_id"], "child-progress",
        expected_revision=1, action="record",
        payload={"data": {"observation": "Found one source"},
                 "references": [{"kind": "evidence_card", "id": "evidence:one",
                                 "namespace": "research", "version": 1}]}, **kwargs,
    )
    assert store.route_gap(
        "research", source["session_id"], "handoff", expected_revision=observed["revision"],
        target_mode="Exploration", reason="Evidence is missing", intent="Find source",
        inputs={}, references=[], **kwargs,
    )["idempotent"]
    with pytest.raises(IntakeError, match="different session|another operation"):
        store.route_gap(
            "research", source["session_id"], "handoff", expected_revision=observed["revision"],
            target_mode="Exploration", reason="Different gap", intent="Find source",
            inputs={}, references=[], **kwargs,
        )
    conn.close()
    conn = duckdb.connect(path)
    child = IntakeStore(conn).inspect("research", routed["child"]["session_id"], **kwargs)
    assert child["origin"]["reason"] == "Evidence is missing"
    with pytest.raises(IntakeError, match="current owner"):
        IntakeIterationStore(conn).route_gap(
            "research", source["session_id"], "revoked", expected_revision=3,
            target_mode="Exploration", reason="New gap", intent="Find source",
            inputs={}, references=[], principal_id="alice",
            scopes={"knowledge:intake:read", "knowledge:intake:write"},
        )


def test_decision_iteration_writes_reviewed_measurement_to_decision_history_and_replays(tmp_path, monkeypatch):
    path = str(tmp_path / "decision-iteration.duckdb")
    conn = duckdb.connect(path)
    scopes = SCOPES | {"knowledge:decisions:read", "knowledge:decisions:write"}
    kwargs = {"principal_id": "alice", "scopes": scopes}
    content = {
        "project": None,
        "decision_context": {
            "question": "Keep the scheduled index refresh?", "stakes": "One hour of delay",
            "required_confidence": "Moderate", "stop_condition": "Latency is measured",
            "uncertainty": "Only one observation", "missing_inputs": [],
            "deadline_at_ms": None,
        },
        "options": [{"id": "keep", "description": "Keep the schedule"},
                    {"id": "change", "description": "Change the schedule"}],
        "constraints": ["Keep data current"], "assumptions": [], "observations": [],
        "preferences": ["Avoid stale results"], "selected_action": "keep",
        "rationale": "No measured reason to change yet",
        "review_conditions": ["Review after latency measurements"],
    }
    decisions = DecisionStore(conn, now=lambda: 1000)
    decision = decisions.create("research", "refresh-choice", content, **kwargs)
    store = IntakeIterationStore(conn, now=lambda: 2000)
    session = store.start_decision(
        "research", "decision-cycle", decision_id=decision["decision_id"],
        expected_revision=1, expected="Refresh latency stays under 5 seconds",
        stability_criteria="Three measurements below 5 seconds",
        intent="Review the refresh decision after use", **kwargs,
    )
    assert session["references"] == [{
        "kind": "decision", "id": decision["decision_id"],
        "namespace": "research", "version": 1,
    }]
    observed = store.record_outcome(
        "research", session["session_id"], "measured", expected_revision=1,
        observed="The last refresh took 8 seconds", learning="The current schedule is too frequent",
        measurements=[{"metric": "refresh latency", "expected": "under 5",
                       "observed": "8", "unit": "seconds"}],
        uncertainty="One refresh is insufficient", external_causes="Index size changed",
        evidence=[], **kwargs,
    )
    revised_content = {**content, "selected_action": "change",
                       "rationale": "Measured refresh latency exceeded the declared limit"}
    proposal = store.propose_decision(
        "research", session["session_id"], "propose-change",
        expected_revision=observed["revision"], content=revised_content,
        before_after_rationale="Change from the daily schedule to a measured threshold schedule",
        **kwargs,
    )

    original_command = IntakeStore.command
    interrupted = False

    def fail_session_write_once(self, namespace, session_id, command_key, **options):
        nonlocal interrupted
        if command_key == "accept-change" and not interrupted:
            interrupted = True
            raise IntakeError("interrupted", "simulate a failed iteration receipt write")
        return original_command(self, namespace, session_id, command_key, **options)

    monkeypatch.setattr(IntakeStore, "command", fail_session_write_once)
    with pytest.raises(IntakeError, match="failed iteration receipt"):
        store.accept_decision(
            "research", session["session_id"], "accept-change",
            expected_revision=proposal["revision"], **kwargs,
        )
    assert decisions.inspect("research", decision["decision_id"], **kwargs)["revision"] == 1
    monkeypatch.setattr(IntakeStore, "command", original_command)
    accepted = store.accept_decision(
        "research", session["session_id"], "accept-change",
        expected_revision=proposal["revision"], **kwargs,
    )
    assert accepted["data"]["accepted_revision"]["revision"] == 2
    assert decisions.inspect("research", decision["decision_id"], revision=1,
                             **kwargs)["content"] == content
    revised = decisions.inspect("research", decision["decision_id"], **kwargs)
    assert revised["revision"] == 2
    receipt = revised["iteration_history"][0]
    assert receipt["contract"] == "noesis-intake-iteration-decision-v1"
    assert receipt["outcome"]["uncertainty"] == "One refresh is insufficient"
    assert receipt["proposal_sha256"]
    assert store.propose_decision(
        "research", session["session_id"], "propose-change",
        expected_revision=observed["revision"], content=revised_content,
        before_after_rationale="Change from the daily schedule to a measured threshold schedule",
        **kwargs,
    )["idempotent"]
    assert store.start_decision(
        "research", "decision-cycle", decision_id=decision["decision_id"],
        expected_revision=1, expected="Refresh latency stays under 5 seconds",
        stability_criteria="Three measurements below 5 seconds",
        intent="Review the refresh decision after use", **kwargs,
    )["idempotent"]
    conn.close()

    conn = duckdb.connect(path)
    store = IntakeIterationStore(conn, now=lambda: 2000)
    decisions = DecisionStore(conn, now=lambda: 2000)
    replay = store.accept_decision(
        "research", session["session_id"], "accept-change",
        expected_revision=proposal["revision"], **kwargs,
    )
    assert replay["idempotent"]
    stability = store.review_stability(
        "research", session["session_id"], "stability", expected_revision=replay["revision"],
        stable=False, observation="More measurements are needed", **kwargs,
    )
    completed = IntakeStore(conn).command(
        "research", session["session_id"], "finish", expected_revision=stability["revision"],
        action="complete", payload=None, **kwargs,
    )
    assert completed["status"] == "completed"

    next_cycle = store.start_decision(
        "research", "decision-cycle-followup", decision_id=decision["decision_id"],
        expected_revision=2, expected="Latency remains under 5 seconds",
        stability_criteria="Three monthly measurements below 5 seconds",
        intent="Recheck the revised schedule after another month", **kwargs,
    )
    followup = store.record_outcome(
        "research", next_cycle["session_id"], "followup-measured", expected_revision=1,
        observed="The last refresh took 6 seconds", learning="The threshold is still too low",
        measurements=[{"metric": "refresh latency", "expected": "under 5",
                       "observed": "6", "unit": "seconds"}],
        uncertainty="One additional refresh", external_causes="Index grew",
        evidence=[], **kwargs,
    )
    followup_proposal = store.propose_decision(
        "research", next_cycle["session_id"], "followup-propose",
        expected_revision=followup["revision"],
        content={**revised_content, "rationale": "Raise the refresh threshold after another slow result"},
        before_after_rationale="Increase the threshold based on the second measured delay",
        **kwargs,
    )
    followup_accepted = store.accept_decision(
        "research", next_cycle["session_id"], "followup-accept",
        expected_revision=followup_proposal["revision"], **kwargs,
    )
    followup_stability = store.review_stability(
        "research", next_cycle["session_id"], "followup-stability",
        expected_revision=followup_accepted["revision"], stable=False,
        observation="One month is not enough to establish stability", **kwargs,
    )
    decisions.revise(
        "research", decision["decision_id"], 3,
        {**revised_content, "rationale": "An independent review changed the rationale"},
        **kwargs,
    )
    with pytest.raises(IntakeError, match="accepted decision revision must still be current"):
        IntakeStore(conn).command(
            "research", next_cycle["session_id"], "finish-stale-decision",
            expected_revision=followup_stability["revision"],
            action="complete", payload=None, **kwargs,
        )
    conn.close()


def test_decision_iteration_rechecks_access_and_pinned_revision():
    conn = duckdb.connect(":memory:")
    scopes = SCOPES | {"knowledge:decisions:read", "knowledge:decisions:write"}
    kwargs = {"principal_id": "alice", "scopes": scopes}
    content = {
        "project": None,
        "decision_context": {
            "question": "Keep service?", "stakes": "One month", "required_confidence": "Moderate",
            "stop_condition": "Usage is known", "uncertainty": "Future usage is unclear",
            "missing_inputs": [], "deadline_at_ms": None,
        },
        "options": [{"id": "keep", "description": "Keep"},
                    {"id": "stop", "description": "Stop"}],
        "constraints": [], "assumptions": [], "observations": [], "preferences": [],
        "selected_action": "keep", "rationale": "Usage may resume", "review_conditions": [],
    }
    decision = DecisionStore(conn).create("research", "service-choice", content, **kwargs)
    store = IntakeIterationStore(conn)
    read_only_decision_scopes = SCOPES | {"knowledge:decisions:read"}
    session = store.start_decision(
        "research", "readable-cycle", decision_id=decision["decision_id"],
        expected_revision=1, expected="Service is used", stability_criteria="Three months",
        intent="Review service decision", principal_id="alice",
        scopes=read_only_decision_scopes,
    )
    outcome = store.record_outcome(
        "research", session["session_id"], "observed", expected_revision=1,
        observed="Usage resumed", learning="The stop condition was met",
        measurements=[{"metric": "monthly usage", "expected": "0", "observed": "3",
                       "unit": "sessions"}], uncertainty="Only one month", external_causes="Seasonal use",
        evidence=[], principal_id="alice", scopes=read_only_decision_scopes,
    )
    with pytest.raises(IntakeError, match="decision scope and ownership"):
        store.propose_decision(
            "research", session["session_id"], "proposal",
            expected_revision=outcome["revision"],
            content={**content, "selected_action": "stop"},
            before_after_rationale="Reconsider after usage resumed",
            principal_id="alice", scopes=read_only_decision_scopes,
        )
    revised_content = {**content, "rationale": "Reconsidered after usage resumed"}
    DecisionStore(conn).revise(
        "research", decision["decision_id"], 1, revised_content, **kwargs,
    )
    with pytest.raises(IntakeError, match="changed"):
        store.start_decision(
            "research", "stale", decision_id=decision["decision_id"],
            expected_revision=1, expected="Service is used", stability_criteria="Three months",
            intent="Review service decision", **kwargs,
        )
    current = DecisionStore(conn).inspect("research", decision["decision_id"], **kwargs)
    cycle = store.start_decision(
        "research", "accept-race", decision_id=decision["decision_id"],
        expected_revision=current["revision"], expected="Usage remains above zero",
        stability_criteria="Three months of activity", intent="Check the revised decision",
        **kwargs,
    )
    outcome = store.record_outcome(
        "research", cycle["session_id"], "observed", expected_revision=1,
        observed="Usage continued", learning="The stop option is no longer suitable",
        measurements=[{"metric": "monthly use", "expected": "0", "observed": "3",
                       "unit": "sessions"}], uncertainty="One month", external_causes="Seasonality",
        evidence=[], **kwargs,
    )
    proposal = store.propose_decision(
        "research", cycle["session_id"], "propose", expected_revision=outcome["revision"],
        content={**revised_content, "selected_action": "stop"},
        before_after_rationale="Keep only if use resumes",
        **kwargs,
    )
    DecisionStore(conn).revise(
        "research", decision["decision_id"], current["revision"], revised_content,
        **kwargs,
    )
    with pytest.raises(DecisionError, match="decision changed"):
        store.accept_decision(
            "research", cycle["session_id"], "accept-stale",
            expected_revision=proposal["revision"], **kwargs,
        )
    unchanged = IntakeStore(conn).inspect("research", cycle["session_id"], **kwargs)
    assert unchanged["revision"] == proposal["revision"]
    assert "accepted_revision" not in unchanged["data"]
    assert "iteration_history" not in DecisionStore(conn).inspect(
        "research", decision["decision_id"], **kwargs,
    )


def test_report_iteration_commits_revision_and_receipt_atomically_and_requires_current_revision():
    conn = duckdb.connect(":memory:")
    scopes = SCOPES | {"knowledge:reports:read", "knowledge:reports:write"}
    kwargs = {"principal_id": "alice", "scopes": scopes}

    def content(title, text):
        return {
            "title": title,
            "snapshot": {"id": "snapshot:report", "generations": {"research": 1}},
            "sections": [{"id": "section:summary", "title": "Summary", "assertions": [{
                "id": "assertion:summary", "text": text, "kind": "commentary",
                "dependencies": [], "citations": [],
            }]}],
            "bibliography": [], "limitations": ["Caller-authored revision"],
        }

    reports = AuthoredReportStore(conn, now=lambda: 1000)
    report = reports.create("research", "report", content("Repair guide", "Restart the worker"), **kwargs)
    store = IntakeIterationStore(conn, now=lambda: 2000)
    session = store.start_report(
        "research", "report-cycle", report_id=report["report_id"], expected_revision=1,
        expected="New records appear within 5 seconds", stability_criteria="Three runs pass",
        intent="Review the repair guide after field use", **kwargs,
    )
    observed = store.record_outcome(
        "research", session["session_id"], "observed", expected_revision=1,
        observed="Records appeared after 9 seconds", learning="The guide needs a sync wait",
        measurements=[{"metric": "search latency", "expected": "5", "observed": "9", "unit": "seconds"}],
        uncertainty="One field run", external_causes="Large index", evidence=[], **kwargs,
    )
    proposed_content = content("Repair guide", "Restart, wait for sync, then search again")
    proposal = store.propose_report(
        "research", session["session_id"], "propose", expected_revision=observed["revision"],
        content=proposed_content, before_after_rationale="Add the observed sync wait before verification",
        **kwargs,
    )
    accepted = store.accept_report(
        "research", session["session_id"], "accept", expected_revision=proposal["revision"], **kwargs,
    )
    assert accepted["data"]["accepted_revision"]["revision"] == 2
    assert store.accept_report(
        "research", session["session_id"], "accept", expected_revision=proposal["revision"], **kwargs,
    )["idempotent"]
    historical = reports.inspect("research", report["report_id"], revision=1, **kwargs)
    current = reports.inspect("research", report["report_id"], **kwargs)
    assert historical["content"] == report["content"]
    assert current["content"] == proposed_content
    receipt = current["iteration_history"][0]
    assert receipt["contract"] == "noesis-intake-iteration-report-v1"
    assert receipt["outcome"]["uncertainty"] == "One field run"
    assert receipt["before_after_rationale"] == "Add the observed sync wait before verification"
    assert receipt["proposal_sha256"] == accepted["data"]["accepted_revision"]["proposal_sha256"]
    assert reports.revise(
        "research", report["report_id"], 2, content("Updated repair guide", "Use the new procedure"),
        **kwargs,
    )["revision"] == 3
    stability = store.review_stability(
        "research", session["session_id"], "stable", expected_revision=accepted["revision"],
        stable=False, observation="Two more field runs are needed", **kwargs,
    )
    with pytest.raises(IntakeError, match="must still be current"):
        IntakeStore(conn).command(
            "research", session["session_id"], "finish", expected_revision=stability["revision"],
            action="complete", payload=None, **kwargs,
        )
    assert IntakeStore(conn).inspect("research", session["session_id"], **kwargs)["status"] == "active"
    conn.close()


def test_concept_iteration_updates_pinned_research_bundle_with_receipt():
    conn = duckdb.connect(":memory:")
    bundle, scopes = _concept_bundle(conn)
    kwargs = {"principal_id": "alice", "scopes": scopes}
    store = IntakeIterationStore(conn, now=lambda: 5000)
    session = store.start_concept(
        "research", "concept-cycle", bundle_id=bundle["bundle_id"],
        concept_id="concept-1", expected_revision=1,
        expected="A reader can explain the finding from the cited source",
        stability_criteria="Two readers explain it without prompting",
        intent="Improve the concept explanation", **kwargs,
    )
    observed = store.record_outcome(
        "research", session["session_id"], "concept-observation", expected_revision=1,
        observed="A reader confused the claim with its implication",
        learning="The explanation needs to separate evidence from inference",
        measurements=[{"metric": "unaided explanation", "expected": "clear distinction",
                       "observed": "confused", "unit": "review outcome"}],
        uncertainty="One reader", external_causes="No prompt was available",
        evidence=[], **kwargs,
    )
    proposed = store.propose_concept(
        "research", session["session_id"], "concept-proposal",
        expected_revision=observed["revision"],
        concept={"id": "concept-1", "name": "Concept",
                 "explanation": "The passage states the finding; the broader implication is an inference.",
                 "card_ids": ["card-concept"]},
        before_after_rationale="Separate the source claim from the inferred implication",
        **kwargs,
    )
    accepted = store.accept_concept(
        "research", session["session_id"], "concept-accept",
        expected_revision=proposed["revision"], **kwargs,
    )
    assert accepted["data"]["accepted_revision"]["revision"] == 2
    assert store.accept_concept(
        "research", session["session_id"], "concept-accept",
        expected_revision=proposed["revision"], **kwargs,
    )["idempotent"]
    current = IntakeResearchBundleStore(conn, initialize=False).inspect(
        "research", bundle["bundle_id"], **kwargs,
    )
    assert current["revision"] == 2
    assert current["document"]["concepts"][0]["explanation"].startswith("The passage states")
    receipt = current["iteration_history"][0]
    assert receipt["contract"] == "noesis-intake-iteration-concept-v1"
    assert receipt["concept_id"] == "concept-1"
    assert receipt["outcome"]["uncertainty"] == "One reader"
    assert receipt["proposal_sha256"] == accepted["data"]["accepted_revision"]["proposal_sha256"]
    conn.close()


def test_modulo_note_iteration_accepts_only_a_local_candidate():
    conn = duckdb.connect(":memory:")
    kwargs = {"principal_id": "alice", "scopes": SCOPES}
    store = IntakeIterationStore(conn, now=lambda: 8000)
    plugin_link = {
        "workspace_id": "workspace-4", "account_id": "account-9",
        "plugin_id": "notes-editor", "collection": "notes", "record_id": "note-12",
        "authoritative_version": 6, "representation": "intentional_snapshot",
        "authority": "modulo", "source_locator": {"url": "https://example.org/note-12"},
    }
    source_snapshot = {"title": "Reading note", "body": "The first draft of the note."}
    session = store.start_modulo_note(
        "research", "modulo-note-cycle", plugin_link=plugin_link,
        source_snapshot=source_snapshot,
        expected="A reader can distinguish observation from interpretation",
        stability_criteria="Two readers identify both parts without help",
        intent="Revise a linked note after review", **kwargs,
    )
    observed = store.record_outcome(
        "research", session["session_id"], "note-observation", expected_revision=1,
        observed="The reader treated the interpretation as a direct quote",
        learning="Label the interpretation explicitly",
        measurements=[{"metric": "quote attribution", "expected": "correct",
                       "observed": "incorrect", "unit": "review outcome"}],
        uncertainty="One review", external_causes="The note used a block quote",
        evidence=[], **kwargs,
    )
    proposed = store.propose_modulo_note(
        "research", session["session_id"], "note-proposal",
        expected_revision=observed["revision"],
        content={"title": "Reading note", "body": "Observation: the source says X.\n\n"
                 "Interpretation: this may imply Y."},
        before_after_rationale="Label the inference separately from the source observation",
        **kwargs,
    )
    accepted = store.accept_modulo_note(
        "research", session["session_id"], "note-accept",
        expected_revision=proposed["revision"], **kwargs,
    )
    assert accepted["data"]["accepted_revision"]["content"]["title"] == "Reading note"
    assert accepted["data"]["accepted_revision"]["local_only"] is True
    assert accepted["data"]["accepted_revision"]["modulo_access_state"] == "not_checked_by_noesis"
    assert accepted["data"]["accepted_revision"]["writeback_state"] == "not_written"
    receipt = accepted["data"]["modulo_note_candidate_history"][0]
    assert receipt["plugin_link"] == plugin_link
    assert receipt["source_snapshot_sha256"]
    assert store.accept_modulo_note(
        "research", session["session_id"], "note-accept",
        expected_revision=proposed["revision"], **kwargs,
    )["idempotent"]
    with pytest.raises(IntakeError, match="notes-editor"):
        store.start_modulo_note(
            "research", "wrong-note-plugin",
            plugin_link={**plugin_link, "plugin_id": "todo-lists"},
            source_snapshot=source_snapshot, expected="Expected", stability_criteria="Stable",
            intent="Revise a note", **kwargs,
        )
    conn.close()
