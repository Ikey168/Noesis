"""Retrieval drafts require current source pins and explicit author answers."""

import duckdb
import pytest

from src.kb.intake_exploration import IntakeExplorationStore
from src.kb.intake_modes import IntakeError, IntakeStore
from src.kb.intake_practice_drafts import IntakePracticeDrafts
from src.kb.intake_practice import IntakePracticeStore
from src.kb.intake_research_bundle import IntakeResearchBundleStore
from src.kb.intake_research_topic import start_research_topic

SCOPES = {"knowledge:intake:read", "knowledge:intake:write",
          "namespace:research:read", "namespace:research:write"}


def test_practice_draft_review_replay_and_source_correction(tmp_path):
    path = str(tmp_path / "practice-draft.duckdb")
    conn = duckdb.connect(path)
    kwargs = {"principal_id": "alice", "scopes": SCOPES}
    session = IntakeStore(conn).create(
        "research", "Exploration", "source-session", intent="Capture a source", **kwargs,
    )
    exploration = IntakeExplorationStore(conn)
    source = exploration.capture(
        "research", session["session_id"], "source", expected_revision=1,
        url="https://example.org/worker", title="Index worker repair",
        content="Restart the index worker and verify a result", saved=True, **kwargs,
    )
    selection = [{"reference": source["references"][0], "practice_kind": "procedure"}]
    drafts = IntakePracticeDrafts(conn)
    draft = drafts.build("research", selection, **kwargs)
    assert draft["cards"][0]["answer_status"] == "pending_author_review"
    assert "answer" not in draft["cards"][0]
    reviewed = [{"prompt": "How do you repair the index?", "answer": "Restart the worker and verify a result.",
                 "mastery_criterion": "Execute without notes", "approved": True}]
    with pytest.raises(IntakeError, match="approve"):
        drafts.create_pack("research", "pack", "Repair", selection, draft["sha256"],
                           [{**reviewed[0], "approved": False}], **kwargs)
    pack = drafts.create_pack("research", "pack", "Repair", selection,
                              draft["sha256"], reviewed, **kwargs)
    assert pack["cards"][0]["answer_status"] == "author_supplied_unverified"
    assert drafts.create_pack("research", "pack", "Repair", selection,
                              draft["sha256"], reviewed, **kwargs)["idempotent"]
    conn.close()
    conn = duckdb.connect(path)
    with pytest.raises(IntakeError, match="current owner"):
        IntakePracticeDrafts(conn).build(
            "research", selection, principal_id="alice",
            scopes={"knowledge:intake:read"},
        )
    exploration = IntakeExplorationStore(conn)
    exploration.capture(
        "research", session["session_id"], "corrected", expected_revision=2,
        url="https://example.org/worker", title="Corrected index worker repair",
        content="Use a different recovery path", saved=True, **kwargs,
    )
    with pytest.raises(IntakeError, match="current source revision"):
        IntakePracticeDrafts(conn).create_pack(
            "research", "other-pack", "Repair", selection, draft["sha256"], reviewed, **kwargs,
        )


def test_research_bundle_concepts_and_cards_can_be_drafted_and_pause_after_correction():
    conn = duckdb.connect(":memory:")
    scopes = SCOPES | {"knowledge:projects:read", "knowledge:projects:write"}
    kwargs = {"principal_id": "alice", "scopes": scopes}
    intake = IntakeStore(conn, now=lambda: 1000)
    source_session = intake.create(
        "research", "Exploration", "bundle-source", intent="Capture evidence", **kwargs,
    )
    exploration = IntakeExplorationStore(conn, now=lambda: 1000)
    source = exploration.capture(
        "research", source_session["session_id"], "capture", expected_revision=1,
        url="https://example.org/index", title="Index worker finding",
        content="The worker restarts before new records become searchable.",
        saved=True, **kwargs,
    )
    source_ref = source["references"][0]
    topic = start_research_topic(
        conn, "research", "topic", questions=["How does recovery work?"],
        success_criteria=["Explain the observed recovery"],
        scope={"domains": [], "namespaces": ["research"]},
        budget={"requests": 5}, origin=None, references=[source_ref],
        workspace_links=None, **kwargs,
    )
    content = "The worker restarts before new records become searchable."
    document = {
        "cards": [{
            "id": "card-1",
            "source": {"namespace": "research", "id": source_ref["id"],
                       "version": source_ref["version"], "start": 0, "end": len(content)},
            "quote": content, "summary": "Restarting the worker restores indexing.",
        }],
        "claims": [{"id": "claim-1", "statement": "Restart restores indexing",
                    "supports": ["card-1"], "contradicts": [], "confidence": "medium"}],
        "concepts": [{"id": "concept-1", "name": "Index recovery",
                      "explanation": "Restart the worker before searching new records",
                      "card_ids": ["card-1"]}],
        "brief": {"text": "Restarting restores indexing", "card_ids": ["card-1"]},
        "mental_model": {"text": "A restart refreshes the search projection", "card_ids": ["card-1"]},
        "map": {"text": "Worker restart -> searchable records", "card_ids": ["card-1"]},
        "known": [{"text": "Restart restored indexing in this observation", "card_ids": ["card-1"]}],
        "uncertain": [{"text": "The cause of the interruption is unknown", "card_ids": []}],
        "unresolved": [{"text": "Long-term reliability is untested", "card_ids": []}],
        "definition_of_done": [{"criterion": "Explain the observed recovery", "met": True,
                                "rationale": "The source supports the reported sequence",
                                "card_ids": ["card-1"]}],
    }
    bundle = IntakeResearchBundleStore(conn, now=lambda: 1000).save(
        "research", topic["project"]["project_id"], "save-bundle", document, **kwargs,
    )
    bundle_ref = {"kind": "research_bundle", "id": bundle["bundle_id"],
                  "namespace": "research", "version": bundle["revision"]}
    selections = [
        {"reference": bundle_ref, "practice_kind": "recall",
         "item_kind": "concept", "item_id": "concept-1"},
        {"reference": bundle_ref, "practice_kind": "explanation",
         "item_kind": "evidence_card", "item_id": "card-1"},
    ]
    drafts = IntakePracticeDrafts(conn)
    draft = drafts.build("research", selections, **kwargs)
    assert "Index recovery" in draft["cards"][0]["proposed_prompt"]
    assert draft["cards"][0]["answer_reference"]["locator"]["section"] == "concepts/concept-1"
    assert draft["cards"][1]["answer_reference"]["locator"]["section"] == "evidence_cards/card-1"
    assert all(card["answer_status"] == "pending_author_review"
               and card["requires_author_answer"] for card in draft["cards"])
    with pytest.raises(IntakeError, match="current Research Bundle access"):
        drafts.build("research", selections, principal_id="alice", scopes=SCOPES)

    reviewed = [
        {"prompt": "Explain index recovery without notes",
         "answer": "The worker restarts before new records become searchable.",
         "mastery_criterion": "Explain the sequence unaided", "approved": True},
        {"prompt": "What does the card show about indexing?",
         "answer": "Restarting restores indexing.",
         "mastery_criterion": "Link the restart to searchable records", "approved": True},
    ]
    pack = drafts.create_pack("research", "bundle-practice", "Index recovery",
                              selections, draft["sha256"], reviewed, **kwargs)
    due = IntakePracticeStore(conn).due("research", **kwargs)
    assert all(card["source_status"] == "current" and card["reviewable"] for card in due["cards"])
    assert "answer" not in due["cards"][0]

    exploration.capture(
        "research", source_session["session_id"], "correction", expected_revision=2,
        url="https://example.org/index", title="Corrected recovery finding",
        content="A different worker recovery sequence is required.", saved=True, **kwargs,
    )
    corrected_due = IntakePracticeStore(conn).due("research", **kwargs)
    assert all(card["source_status"] == "superseded" and not card["reviewable"]
               for card in corrected_due["cards"])
    with pytest.raises(IntakeError, match="linked source changes"):
        IntakePracticeStore(conn).start_review(
            "research", pack["pack_id"], "card-1", "stale-review", **kwargs,
        )
