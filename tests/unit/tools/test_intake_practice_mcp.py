"""Supported MCP discovery and calls expose the same practice state machine."""

import asyncio

import duckdb

from src.mcp_host.catalog import _required_scopes
from src.kb.intake_exploration import IntakeExplorationStore
from src.kb.intake_modes import IntakeStore
from src.kb.intake_research_bundle import IntakeResearchBundleStore
from src.kb.intake_research_topic import start_research_topic
from tools.knowledge_engine_mcp import server


def test_practice_public_mcp_round_trip_and_revocation(tmp_path, monkeypatch):
    path = str(tmp_path / "practice-mcp.duckdb")
    scopes = {
        "knowledge:intake:read", "knowledge:intake:write",
        "namespace:research:read", "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server, "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    tools = asyncio.run(server.mcp.get_tools())
    cards = [{
        "kind": "recall", "prompt": "What stopped?", "answer": "The worker",
        "mastery_criterion": "Recall without notes on three reviews",
        "references": [{"kind": "concept", "id": "concept:worker",
                        "namespace": "research", "version": 1}],
    }]
    pack = tools["create_practice_pack"].fn(
        namespace="research", request_key="pack", title="Worker", cards=cards,
        plugin_links=[{
            "workspace_id": "personal", "account_id": "alice",
            "plugin_id": "flashcards-spaced-repetition", "collection": "decks",
            "record_id": "deck-mcp", "authoritative_version": 1,
            "representation": "linked_projection", "authority": "modulo",
        }],
    )
    assert pack["pack_id"].startswith("practice-pack:")
    assert pack["plugin_links"][0]["plugin_id"] == "flashcards-spaced-repetition"
    assert tools["list_due_practice"].fn(namespace="research")["cards"][0][
        "prompt"] == "What stopped?"
    review = tools["start_practice_review"].fn(
        namespace="research", pack_id=pack["pack_id"],
        card_id="card-1", request_key="review",
    )
    assert "answer" not in review
    attempt = tools["command_practice_review"].fn(
        namespace="research", review_id=review["review_id"],
        command_key="attempt", expected_revision=1, action="attempt",
        payload={"answer": "The worker", "assisted": False},
    )
    assert "answer" not in attempt
    revealed = tools["command_practice_review"].fn(
        namespace="research", review_id=review["review_id"],
        command_key="reveal", expected_revision=2, action="reveal",
    )
    assert revealed["answer"] == "The worker"
    exported = tools["export_practice_pack"].fn(
        namespace="research", pack_id=pack["pack_id"],
    )
    assert tools["verify_practice_export"].fn(bundle=exported)["valid"]
    assert _required_scopes("knowledge_engine_mcp", "write",
                            "command_practice_review") == ["knowledge:intake:write"]
    scopes.remove("namespace:research:read")
    denied = tools["inspect_practice_review"].fn(
        namespace="research", review_id=review["review_id"],
    )
    assert denied["error"]["code"] == "unauthorized"


def test_practice_draft_mcp_accepts_selected_bundle_concepts_and_evidence(tmp_path, monkeypatch):
    path = str(tmp_path / "bundle-practice-mcp.duckdb")
    scopes = {
        "knowledge:intake:read", "knowledge:intake:write",
        "knowledge:projects:read", "knowledge:projects:write",
        "namespace:research:read", "namespace:research:write",
    }
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server, "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    conn = duckdb.connect(path)
    source_session = IntakeStore(conn, now=lambda: 1000).create(
        "research", "Exploration", "source", intent="Capture evidence",
        principal_id="alice", scopes=scopes,
    )
    source = IntakeExplorationStore(conn, now=lambda: 1000).capture(
        "research", source_session["session_id"], "capture", expected_revision=1,
        url="https://example.org/recovery", title="Worker recovery",
        content="Restart the worker to restore search.", saved=True,
        principal_id="alice", scopes=scopes,
    )
    source_ref = source["references"][0]
    topic = start_research_topic(
        conn, "research", "topic", questions=["How is search restored?"],
        success_criteria=["Explain the recovery"],
        scope={"domains": [], "namespaces": ["research"]},
        budget={"requests": 3}, origin=None, references=[source_ref],
        workspace_links=None, principal_id="alice", scopes=scopes,
    )
    content = "Restart the worker to restore search."
    document = {
        "cards": [{
            "id": "card-1",
            "source": {"namespace": "research", "id": source_ref["id"],
                       "version": source_ref["version"], "start": 0, "end": len(content)},
            "quote": content, "summary": "A worker restart restores search.",
        }],
        "claims": [{"id": "claim-1", "statement": "Restart restores search",
                    "supports": ["card-1"], "contradicts": [], "confidence": "medium"}],
        "concepts": [{"id": "concept-1", "name": "Search recovery",
                      "explanation": "Restart the worker to restore search",
                      "card_ids": ["card-1"]}],
        "brief": {"text": "Restart restores search", "card_ids": ["card-1"]},
        "mental_model": {"text": "Restart -> fresh search", "card_ids": ["card-1"]},
        "map": {"text": "Worker restart -> search", "card_ids": ["card-1"]},
        "known": [{"text": "Restart restored search", "card_ids": ["card-1"]}],
        "uncertain": [{"text": "Other failure causes are unknown", "card_ids": []}],
        "unresolved": [{"text": "Recovery time needs more observations", "card_ids": []}],
        "definition_of_done": [{"criterion": "Explain the recovery", "met": True,
                                "rationale": "The cited observation shows the sequence",
                                "card_ids": ["card-1"]}],
    }
    bundle = IntakeResearchBundleStore(conn, now=lambda: 1000).save(
        "research", topic["project"]["project_id"], "save", document,
        principal_id="alice", scopes=scopes,
    )
    conn.close()

    tools = asyncio.run(server.mcp.get_tools())
    reference = {"kind": "research_bundle", "id": bundle["bundle_id"],
                 "namespace": "research", "version": bundle["revision"]}
    selections = [{"reference": reference, "practice_kind": "recall",
                   "item_kind": "concept", "item_id": "concept-1"}]
    draft = tools["draft_intake_practice_pack"].fn(
        namespace="research", selections=selections,
    )
    assert draft["cards"][0]["answer_reference"]["locator"]["section"] == "concepts/concept-1"
    pack = tools["create_reviewed_intake_practice_pack"].fn(
        namespace="research", request_key="bundle-pack", title="Search recovery",
        selections=selections, draft_sha256=draft["sha256"],
        reviewed_cards=[{"prompt": "Explain search recovery without notes",
                         "answer": "Restart the worker to restore search.",
                         "mastery_criterion": "Explain the sequence unaided",
                         "approved": True}],
    )
    assert pack["cards"][0]["references"][0]["kind"] == "research_bundle"
    assert _required_scopes("knowledge_engine_mcp", "read",
                            "draft_intake_practice_pack") == ["knowledge:intake:read"]
    assert _required_scopes("knowledge_engine_mcp", "write",
                            "create_reviewed_intake_practice_pack") == ["knowledge:intake:write"]
