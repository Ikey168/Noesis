"""A research handoff cannot leave half a topic after a rejected start."""

import duckdb
import pytest

from src.kb.intake_modes import IntakeError, IntakeStore
from src.kb.intake_research_topic import start_research_topic
from src.kb.research_projects import ResearchProjectStore

SCOPES = {
    "knowledge:intake:read", "knowledge:intake:write",
    "knowledge:projects:read", "knowledge:projects:write",
    "namespace:research:read", "namespace:research:write",
}


def _start(conn, key, owner="alice", scopes=SCOPES):
    return start_research_topic(
        conn, "research", key,
        questions=["What explains the indexing delay?"],
        success_criteria=["Identify supported and unresolved causes"],
        scope={"domains": [], "namespaces": ["research"]},
        budget={"requests": 5, "tokens": 10000, "usd_micros": 0},
        origin=None, references=[], workspace_links=None,
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
