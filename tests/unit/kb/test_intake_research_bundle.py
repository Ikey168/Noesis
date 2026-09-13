import copy
import json
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.kb.intake_exploration import IntakeExplorationStore
from src.kb.intake_modes import IntakeError, IntakeStore
from src.kb.intake_research_bundle import (
    IntakeResearchBundleStore,
    verify_research_bundle_export,
)
from src.kb.intake_research_topic import start_research_topic

SCOPES = {
    "knowledge:intake:read", "knowledge:intake:write",
    "knowledge:projects:read", "knowledge:projects:write",
    "namespace:research:read", "namespace:research:write",
}


def _fixture(conn):
    intake = IntakeStore(conn)
    exploration = IntakeExplorationStore(conn)
    source_session = intake.create("research", "Exploration", "sources",
                                   intent="Gather", principal_id="alice", scopes=SCOPES)
    refs = []
    for index, (url, content) in enumerate((
        ("https://example.org/one", "Alpha supports finding."),
        ("https://example.net/two", "Beta corroborates finding."),
    ), 1):
        captured = exploration.capture(
            "research", source_session["session_id"], f"capture-{index}",
            expected_revision=index, url=url, title=f"Source {index}", content=content,
            saved=True, principal_id="alice", scopes=SCOPES,
        )
        refs.append(captured["references"][-1])
    started = start_research_topic(
        conn, "research", "topic", questions=["Why?"],
        success_criteria=["Explain with independent evidence"],
        scope={"domains": [], "namespaces": ["research"]},
        budget={"requests": 10}, origin=None, references=refs,
        workspace_links=None, principal_id="alice", scopes=SCOPES,
    )
    cards = []
    for index, (ref, content) in enumerate(zip(refs, (
        "Alpha supports finding.", "Beta corroborates finding.",
    )), 1):
        cards.append({
            "id": f"card-{index}",
            "source": {"namespace": "research", "id": ref["id"], "version": ref["version"],
                       "start": 0, "end": len(content)},
            "quote": content, "summary": content,
        })
    document = {
        "cards": cards,
        "claims": [{"id": "claim-1", "statement": "Both sources support the finding",
                    "supports": ["card-1", "card-2"], "contradicts": [], "confidence": "high"}],
        "concepts": [{"id": "concept-1", "name": "Finding", "explanation": "Supported finding",
                      "card_ids": ["card-1"]}],
        "brief": {"text": "Finding in brief", "card_ids": ["card-1", "card-2"]},
        "mental_model": {"text": "Evidence converges", "card_ids": ["card-1", "card-2"]},
        "map": {"text": "Sources -> claim -> finding", "card_ids": ["card-1", "card-2"]},
        "known": [{"text": "Both texts make the finding", "card_ids": ["card-1", "card-2"]}],
        "uncertain": [{"text": "Generalizability unknown", "card_ids": []}],
        "unresolved": [{"text": "Need a third source", "card_ids": []}],
        "definition_of_done": [{"criterion": "Explain with independent evidence", "met": True,
                                "rationale": "Two separately hosted sources are cited",
                                "card_ids": ["card-1", "card-2"]}],
    }
    return started, document, exploration, source_session


def test_bundle_requires_exact_pinned_spans_and_gates_research_completion(tmp_path):
    conn = duckdb.connect(str(tmp_path / "research.duckdb"))
    started, document, exploration, source_session = _fixture(conn)
    store = IntakeResearchBundleStore(conn)
    project_id = started["project"]["project_id"]
    session_id = started["session"]["session_id"]

    wrong = copy.deepcopy(document)
    wrong["cards"][0]["quote"] = "Fabricated"
    with pytest.raises(IntakeError) as invalid:
        store.save("research", project_id, "bad", wrong, principal_id="alice", scopes=SCOPES)
    assert invalid.value.code == "invalid_citation"

    single_host = copy.deepcopy(document)
    single_host["claims"][0]["supports"] = ["card-1"]
    with pytest.raises(IntakeError) as insufficient:
        store.save("research", project_id, "one-host", single_host,
                   principal_id="alice", scopes=SCOPES)
    assert insufficient.value.code == "insufficient_independence"

    uncited = copy.deepcopy(document)
    uncited["known"][0]["card_ids"] = []
    with pytest.raises(IntakeError) as missing_citation:
        store.save("research", project_id, "uncited-known", uncited,
                   principal_id="alice", scopes=SCOPES)
    assert missing_citation.value.code == "invalid_bundle"

    saved = store.save("research", project_id, "first", document,
                       principal_id="alice", scopes=SCOPES)
    schema = json.loads((Path(__file__).resolve().parents[3] /
                         "contracts/schemas/jsonschema/noesis-intake-research-bundle-v1.json").read_text())
    jsonschema.validate(saved, schema)
    assert saved["checks"]["ready"]
    assert store.save("research", project_id, "first", document,
                      principal_id="alice", scopes=SCOPES)["idempotent"]
    exported = store.export("research", saved["bundle_id"],
                            principal_id="alice", scopes=SCOPES)
    assert verify_research_bundle_export(exported)["valid"]
    exported["revisions"][0]["document"]["brief"]["text"] = "Tampered"
    assert not verify_research_bundle_export(exported)["valid"]

    intake = IntakeStore(conn, initialize=False)
    with pytest.raises(IntakeError, match="research_bundle"):
        intake.command("research", session_id, "early", expected_revision=1,
                       action="complete", payload=None, principal_id="alice", scopes=SCOPES)
    recorded = intake.command(
        "research", session_id, "link", expected_revision=1, action="record",
        payload={"references": [{"kind": "research_bundle", "id": saved["bundle_id"],
                                  "namespace": "research", "version": 1}]},
        principal_id="alice", scopes=SCOPES,
    )
    assert recorded["revision"] == 2
    exploration.capture(
        "research", source_session["session_id"], "correction", expected_revision=3,
        url="https://example.org/one", title="Corrected", content="Corrected source text",
        saved=True, principal_id="alice", scopes=SCOPES,
    )
    assert not store.inspect("research", saved["bundle_id"],
                             principal_id="alice", scopes=SCOPES)["checks"]["ready"]
    with pytest.raises(IntakeError, match="not ready"):
        intake.command("research", session_id, "finish", expected_revision=2,
                       action="complete", payload=None, principal_id="alice", scopes=SCOPES)


def test_bundle_completion_and_owner_access():
    conn = duckdb.connect(":memory:")
    started, document, _, _ = _fixture(conn)
    store = IntakeResearchBundleStore(conn)
    saved = store.save("research", started["project"]["project_id"], "save", document,
                       principal_id="alice", scopes=SCOPES)
    with pytest.raises(Exception) as denied:
        store.inspect("research", saved["bundle_id"], principal_id="bob", scopes=SCOPES)
    assert denied.value.code == "unauthorized"
    intake = IntakeStore(conn, initialize=False)
    session_id = started["session"]["session_id"]
    intake.command("research", session_id, "link", expected_revision=1, action="record",
                   payload={"references": [{"kind": "research_bundle", "id": saved["bundle_id"],
                                             "namespace": "research", "version": 1}]},
                   principal_id="alice", scopes=SCOPES)
    completed = intake.command("research", session_id, "finish", expected_revision=2,
                               action="complete", payload=None, principal_id="alice", scopes=SCOPES)
    assert completed["status"] == "completed"
