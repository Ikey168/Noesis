"""Skills keep a stable identity and separate evidence bases (#1577, #1580)."""

import duckdb
import pytest

from src.kb.intake_modes import IntakeError
from src.kb.intake_practice import IntakePracticeStore
from src.kb.intake_skills import IntakeSkillStore
from tests.unit.kb.test_intake_practice import CARDS, SCOPES

KW = {"principal_id": "alice", "scopes": SCOPES}
REVIEWER = {"principal_id": "bob", "scopes": {"knowledge:intake:review", "namespace:research:read"}}


def _practiced(conn, *, assisted=False):
    practice = IntakePracticeStore(conn, now=lambda: 1_000)
    pack = practice.create_pack("research", "pack", "Index recovery", CARDS, **KW)
    review = practice.start_review("research", pack["pack_id"], "card-1", "r1", **KW)
    for key, action, payload in (
        ("a", "attempt", {"answer": "Worker stopped", "assisted": assisted}),
        ("b", "reveal", {}),
        ("c", "assess", {"passed": True, "notes": "Matched"}),
    ):
        review = practice.command_review("research", review["review_id"], key, review["revision"], action, payload, **KW)
    return pack


def test_skill_identity_evidence_bases_and_independent_mastery():
    conn = duckdb.connect(":memory:")
    pack = _practiced(conn)
    skills = IntakeSkillStore(conn, now=lambda: 2_000)
    skill = skills.create("research", "index-recovery", name="Index recovery", description="Explain and fix",
                          mastery_criteria=["Explain the cause unaided"],
                          practice_links=[{"pack_id": pack["pack_id"], "card_id": "card-1"}],
                          procedure_links=[], **KW)
    assert skill["skill_id"].startswith("skill:")
    assert skills.create("research", "index-recovery", name="Index recovery", description="Explain and fix",
                         mastery_criteria=["Explain the cause unaided"],
                         practice_links=[{"pack_id": pack["pack_id"], "card_id": "card-1"}],
                         procedure_links=[], **KW)["idempotent"]

    evidence = skills.evidence("research", skill["skill_id"], **KW)
    assert [item["basis"] for item in evidence["evidence"]] == ["self_reported_unaided_recall"]
    assert evidence["mastery_demonstrated"] is False

    with pytest.raises(IntakeError) as error:
        skills.record_assessment("research", skill["skill_id"], "self", expected_revision=1,
                                 criterion="Explain the cause unaided", passed=True, evidence_note="me", **KW)
    assert error.value.code == "self_assessment"
    assessed = skills.record_assessment("research", skill["skill_id"], "bob-1", expected_revision=1,
                                        criterion="Explain the cause unaided", passed=True,
                                        evidence_note="Explained without notes in session", **REVIEWER)
    assert assessed["revision"] == 2
    assert skills.evidence("research", skill["skill_id"], **KW)["mastery_demonstrated"] is True
    # Changing what the skill means invalidates the earlier judgement.
    skills.revise("research", skill["skill_id"], "tighten", expected_revision=2,
                  mastery_criteria=["Explain the cause unaided", "Fix it in staging"],
                  practice_links=[{"pack_id": pack["pack_id"], "card_id": "card-1"}],
                  procedure_links=[], **KW)
    revised = skills.evidence("research", skill["skill_id"], **KW)
    assert revised["mastery_demonstrated"] is False
    assert revised["criteria"][0]["independent_assessment"] is None
    for key, criterion in (("bob-2", "Explain the cause unaided"), ("bob-3", "Fix it in staging")):
        current = skills.inspect("research", skill["skill_id"], **KW)["revision"]
        skills.record_assessment("research", skill["skill_id"], key, expected_revision=current,
                                 criterion=criterion, passed=True, evidence_note="Observed", **REVIEWER)
    final = skills.evidence("research", skill["skill_id"], **KW)
    assert final["mastery_demonstrated"] is True
    assert [item["independent_assessment"] for item in final["criteria"]] == [True, True]
    assert skills.inspect("research", skill["skill_id"], revision=1, **KW)["assessments"] == []


def test_assisted_attempts_and_unknown_links_are_not_mastery():
    conn = duckdb.connect(":memory:")
    pack = _practiced(conn, assisted=True)
    skills = IntakeSkillStore(conn, now=lambda: 2_000)
    with pytest.raises(IntakeError):
        skills.create("research", "bad", name="X", description="Y", mastery_criteria=["Z"],
                      practice_links=[{"pack_id": pack["pack_id"], "card_id": "card-9"}], procedure_links=[], **KW)
    skill = skills.create("research", "s", name="X", description="Y", mastery_criteria=["Z"],
                          practice_links=[{"pack_id": pack["pack_id"], "card_id": "card-1"}], procedure_links=[], **KW)
    evidence = skills.evidence("research", skill["skill_id"], **KW)
    assert evidence["evidence"][0]["basis"] == "self_reported_assisted_recall"
    with pytest.raises(IntakeError) as error:
        skills.evidence("research", skill["skill_id"], principal_id="mallory", scopes=SCOPES)
    assert error.value.code == "unauthorized"


def test_procedure_rehearsal_is_procedural_evidence_not_mastery():
    from src.kb.intake_playbooks import IntakePlaybookStore
    from tests.unit.kb.test_intake_playbooks import SCOPES as PB_SCOPES, _promote, _verified_problem

    conn = duckdb.connect(":memory:")
    kw = {"principal_id": "alice", "scopes": PB_SCOPES}
    playbooks = IntakePlaybookStore(conn, now=lambda: 2_000)
    playbook = _promote(playbooks, _verified_problem(conn))
    run = playbooks.start_run("research", playbook["playbook_id"], "run", playbook_revision=1, environment="Staging", **kw)
    for key, action, payload in (
        ("s1", "step", {"step_id": "step-1", "passed": True, "observation": "ok"}),
        ("s2", "step", {"step_id": "step-2", "passed": True, "observation": "ok"}),
        ("v", "verify", {"passed": True, "observation": "item appeared"}),
    ):
        run = playbooks.command_run("research", run["run_id"], key, expected_revision=run["revision"],
                                    action=action, payload=payload, **kw)
    skills = IntakeSkillStore(conn, now=lambda: 3_000)
    skill = skills.create("research", "s", name="Index repair", description="Repair the index",
                          mastery_criteria=["Repair without the playbook"], practice_links=[],
                          procedure_links=[{"playbook_id": playbook["playbook_id"]}], **kw)
    evidence = skills.evidence("research", skill["skill_id"], **kw)
    assert [(item["kind"], item["basis"], item["environment"]) for item in evidence["evidence"]] == [
        ("procedure_run", "caller_reported_rehearsal", "Staging")]
    assert evidence["mastery_demonstrated"] is False
