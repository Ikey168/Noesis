"""Task plans must satisfy the shared provider contract before execution."""

import pytest

from src.integrations.decisions import DecisionRequest
from src.kb.jev_tasks import JevTaskError, TASKS, prepare_task


CASES = {
    "stance": ({"topic": "water policy", "sentence_index": 0}, 1),
    "frames": ({"frames": {"economic": "Economic costs", "legal": "Law and rights"}, "document_kind": "news"}, 1),
    "screen_abstract": ({"criteria": ["Adult participants", "Randomized design"], "protocol_id": "p", "protocol_revision": 1}, 1),
    "screen_fulltext": ({"criteria": ["Adult participants"], "protocol_id": "p", "protocol_revision": 1}, 1),
    "reranking": ({"query": "what changed"}, 2),
    "answer_support": ({"statement": "A new rule was adopted", "locator": {"page": 2}}, 1),
    "claim_links": ({"claim_a": "The law passed", "claim_b": "The law was adopted"}, 2),
    "intake_routing": ({"intent": "I need to decide", "modes": {"Decision Support": "Compare choices", "Awareness": "Monitor stream"}}, 1),
    "review_priority": ({"review_goal": "Verify a legal claim", "current_uncertainty": 0.5}, 1),
    "methodology": ({"study_designs": {"trial": "Randomized trial", "cohort": "Observational cohort"}, "study_id": "s"}, 1),
    "entity_matching": ({"reference_identity": "Alice", "candidates": {"a": "Alice in Berlin", "b": "Alice in Bonn"}}, 1),
    "source_matching": ({"reference_identity": "Agency", "candidates": {"a": "Agency A", "b": "Agency B"}}, 1),
    "revision_significance": ({}, 2),
    "source_selection": ({"objective": "Find legal text", "eligible_sources": {"a": "Official law archive", "b": "Parliament records"}}, 1),
    "claim_detection": ({"sentence": "The law entered force."}, 1),
    "checkworthiness": ({"claim": "The law doubled costs", "topic": "households"}, 1),
    "sentiment": ({"target": "the proposal"}, 1),
    "attribution": ({"statement": "The law passed", "candidates": {"a": "Speaker A", "b": "Speaker B"}}, 1),
}


@pytest.mark.parametrize("task", sorted(TASKS))
def test_every_task_plan_is_accepted_by_the_provider_neutral_contract(task):
    parameters, source_count = CASES[task]
    state, questions = prepare_task(task, parameters, source_count=source_count)
    request = DecisionRequest(
        task_id=task, rubric_id=task + "-v1", state={**state, "sources": ["captured"]},
        questions=questions, source_binding=[{"kind": "document_revision", "document_id": "d", "revision_id": "r"}],
        model="jev-1.13.0",
    )
    assert set(request.as_wire()["questions"]) == set(questions)


def test_multilabel_frames_and_bidirectional_links_remain_independent_questions():
    _, frame_questions = prepare_task("frames", CASES["frames"][0], source_count=1)
    assert len(frame_questions) == 2
    assert {q["type"] for q in frame_questions.values()} == {"noul"}
    _, relation_questions = prepare_task("claim_links", CASES["claim_links"][0], source_count=2)
    assert set(relation_questions) == {"a_to_b", "b_to_a"}


def test_exact_source_count_and_caller_source_spoofing_are_rejected():
    with pytest.raises(JevTaskError, match="two exact"):
        prepare_task("claim_links", CASES["claim_links"][0], source_count=1)
    with pytest.raises(JevTaskError, match="supported task"):
        prepare_task("sentiment", {"target": "policy", "sources": ["caller text"]}, source_count=1)
