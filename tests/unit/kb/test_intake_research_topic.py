"""A research handoff cannot leave half a topic after a rejected start."""

import json
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.kb.intake_exploration import IntakeExplorationStore
from src.kb.intake_inbox import IntakeInboxStore
from src.kb.intake_modes import IntakeError, IntakeStore
from src.kb.intake_research_topic import start_research_topic
from src.kb.research_projects import ResearchProjectStore

SCOPES = {
    "knowledge:intake:read", "knowledge:intake:write",
    "knowledge:projects:read", "knowledge:projects:write",
    "namespace:research:read", "namespace:research:write",
}


def _start(conn, key, owner="alice", scopes=SCOPES, references=None):
    return start_research_topic(
        conn, "research", key,
        questions=["What explains the indexing delay?"],
        success_criteria=["Identify supported and unresolved causes"],
        scope={"domains": [], "namespaces": ["research"]},
        budget={"requests": 5, "tokens": 10000, "usd_micros": 0},
        origin=None, references=references or [], workspace_links=None,
        principal_id=owner, scopes=scopes,
    )


def test_research_topic_links_project_replays_and_rolls_back_at_capacity(tmp_path):
    path = str(tmp_path / "research.duckdb")
    conn = duckdb.connect(path)
    first = _start(conn, "topic-one")
    assert first["contract"] == "noesis-intake-research-topic-v1"
    assert not first["idempotent"]
    project_id = first["project"]["project_id"]
    session_id = first["session"]["session_id"]
    assert first["session"]["inputs"]["research_project_id"] == project_id
    assert {"kind": "research_project", "id": project_id,
            "namespace": "research", "version": 1} in first["session"]["references"]
    assert _start(conn, "topic-one")["idempotent"]
    ResearchProjectStore(conn, initialize=False).revise(
        "research", project_id, 1,
        success_criteria=["Revised review criteria"],
        principal_id="alice", scopes=SCOPES,
    )
    assert _start(conn, "topic-one")["idempotent"]
    _start(conn, "topic-two")
    _start(conn, "topic-three")
    with pytest.raises(IntakeError) as limit:
        _start(conn, "topic-four")
    assert limit.value.code == "active_topic_limit"
    assert conn.execute("SELECT count(*) FROM research_projects").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM intake_sessions").fetchone()[0] == 3
    assert [item["project_id"] for item in ResearchProjectStore(
        conn, initialize=False,
    ).list("research", principal_id="alice", scopes=SCOPES)["projects"]]
    conn.close()

    conn = duckdb.connect(path)
    assert IntakeStore(conn, initialize=False).inspect(
        "research", session_id, principal_id="alice", scopes=SCOPES,
    )["inputs"]["research_project_id"] == project_id
    assert _start(conn, "topic-one")["idempotent"]


def test_research_topic_requires_both_project_and_intake_scopes():
    conn = duckdb.connect(":memory:")
    with pytest.raises(Exception) as denied:
        _start(conn, "denied", scopes=SCOPES - {"knowledge:projects:write"})
    assert getattr(denied.value, "code", None) == "unauthorized"
    assert conn.execute("SELECT count(*) FROM research_projects").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM intake_sessions").fetchone()[0] == 0


def test_research_topic_pins_owned_source_and_reports_correction(tmp_path):
    conn = duckdb.connect(str(tmp_path / "source-handoff.duckdb"))
    exploration = IntakeExplorationStore(conn)
    session = IntakeStore(conn).create(
        "research", "Exploration", "saved-source", intent="Find sources",
        principal_id="alice", scopes=SCOPES,
    )
    first = exploration.capture(
        "research", session["session_id"], "capture-one", expected_revision=1,
        url="https://example.org/research", title="Original", content="Original text",
        saved=True, principal_id="alice", scopes=SCOPES,
    )
    ref = first["references"][0]
    started = _start(conn, "source-topic", references=[ref])
    project = started["project"]
    schema = json.loads(Path(
        "contracts/schemas/jsonschema/noesis-research-project-v1.json"
    ).read_text())
    jsonschema.validate(project, schema)
    assert project["links"] == [{
        "kind": "intake_source", "id": ref["id"], "namespace": "research",
        "revision": 1, "question_revision": 1,
    }]
    store = ResearchProjectStore(conn, initialize=False)
    availability = store.inspect("research", project["project_id"],
                                 principal_id="alice", scopes=SCOPES)["reference_availability"]
    assert availability[0]["status"] == "current"
    assert availability[0]["revision_verified"]

    exploration.capture(
        "research", session["session_id"], "capture-two", expected_revision=2,
        url="https://example.org/research", title="Corrected", content="Corrected text",
        saved=True, principal_id="alice", scopes=SCOPES,
    )
    assert store.inspect("research", project["project_id"],
                         principal_id="alice", scopes=SCOPES)["reference_availability"][0]["status"] == "superseded"
    assert _start(conn, "source-topic", references=[ref])["idempotent"]
    assert conn.execute("SELECT count(*) FROM research_projects").fetchone()[0] == 1


def test_research_topic_rejects_nonexistent_source_without_half_handoff():
    conn = duckdb.connect(":memory:")
    with pytest.raises(IntakeError) as missing:
        _start(conn, "missing-source", references=[{
            "kind": "exploration_source", "id": "explore:missing",
            "namespace": "research", "version": 1,
        }])
    assert missing.value.code == "source_not_found"
    assert conn.execute("SELECT count(*) FROM research_projects").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM intake_sessions").fetchone()[0] == 0


def test_research_topic_rejects_another_owners_saved_source():
    conn = duckdb.connect(":memory:")
    session = IntakeStore(conn).create(
        "research", "Exploration", "bob-source", intent="Save",
        principal_id="bob", scopes=SCOPES,
    )
    captured = IntakeExplorationStore(conn).capture(
        "research", session["session_id"], "save", expected_revision=1,
        url="https://example.org/bob", title="Bob's source", saved=True,
        principal_id="bob", scopes=SCOPES,
    )
    with pytest.raises(IntakeError) as denied:
        _start(conn, "alice-topic", references=captured["references"])
    assert denied.value.code == "source_not_found"
    assert conn.execute("SELECT count(*) FROM research_projects").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM intake_sessions").fetchone()[0] == 1


def test_research_topic_pins_feed_revision():
    conn = duckdb.connect(":memory:")
    inbox = IntakeInboxStore(conn)
    other_subscription = inbox.subscribe(
        "research", "https://example.org/feed", "Example", "rss_atom",
        principal_id="bob", scopes=SCOPES,
    )
    inbox.ingest(
        "research", other_subscription["subscription_id"],
        [{"url": "https://example.org/story", "title": "Story", "content": "Bob's copy"}],
        principal_id="bob", scopes=SCOPES,
    )
    inbox.ingest(
        "research", other_subscription["subscription_id"],
        [{"url": "https://example.org/story", "title": "Story", "content": "Bob's revision"}],
        principal_id="bob", scopes=SCOPES,
    )
    subscription = inbox.subscribe(
        "research", "https://example.org/feed", "Example", "rss_atom",
        principal_id="alice", scopes=SCOPES,
    )
    inbox.ingest(
        "research", subscription["subscription_id"],
        [{"url": "https://example.org/story", "title": "Story", "content": "First"}],
        principal_id="alice", scopes=SCOPES,
    )
    ref = inbox.list("research", principal_id="alice", scopes=SCOPES)["items"][0]["reference"]
    assert inbox.list("research", principal_id="bob", scopes=SCOPES)["items"][0]["item_id"] == ref["id"]
    started = _start(conn, "feed-topic", references=[ref])
    assert started["project"]["links"][0]["id"] == ref["id"]
    store = ResearchProjectStore(conn, initialize=False)
    assert store.inspect("research", started["project"]["project_id"],
                         principal_id="alice", scopes=SCOPES)["reference_availability"][0]["status"] == "current"
    inbox.ingest(
        "research", subscription["subscription_id"],
        [{"url": "https://example.org/story", "title": "Story", "content": "Corrected"}],
        principal_id="alice", scopes=SCOPES,
    )
    availability = store.inspect(
        "research", started["project"]["project_id"],
        principal_id="alice", scopes=SCOPES,
    )["reference_availability"][0]
    assert availability["status"] == "superseded"
    assert availability["revision_verified"]
