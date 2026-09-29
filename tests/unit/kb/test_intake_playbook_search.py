"""Task search returns only current, accessible playbook revisions."""

import duckdb

from src.kb.intake_modes import IntakeStore
from src.kb.intake_exploration import IntakeExplorationStore
from src.kb.intake_playbook_search import IntakePlaybookSearch
from src.kb.intake_playbooks import IntakePlaybookStore
from src.kb.intake_problem import IntakeProblemStore

SCOPES = {"knowledge:intake:read", "knowledge:intake:write",
          "namespace:research:read", "namespace:research:write",
          "namespace:archive:read"}


def test_task_environment_search_filters_by_owner_and_source_access(tmp_path):
    path = str(tmp_path / "playbook-search.duckdb")
    conn = duckdb.connect(path)
    kwargs = {"principal_id": "alice", "scopes": SCOPES}
    problem = IntakeProblemStore(conn, now=lambda: 1000)
    opened = problem.start(
        "research", "broken-index", symptom="Index stale", environment="Desktop",
        urgency="blocking", success_check="Fresh result visible", **kwargs,
    )
    verification_session = IntakeStore(conn).create(
        "research", "Exploration", "playbook-search-verification-source",
        intent="Capture verification evidence", principal_id="alice", scopes=SCOPES,
    )
    verification_source = IntakeExplorationStore(conn).capture(
        "research", verification_session["session_id"], "playbook-search-verification",
        expected_revision=1, url="https://example.org/playbook-search-verification",
        title="Verification observation", content="Observed the expected result.",
        principal_id="alice", scopes=SCOPES,
    )
    problem.record_step(
        "research", opened["session_id"], "verify", expected_revision=1,
        kind="verification", summary="Check index", observation="Fresh result visible",
        passed=True, references=verification_source["references"], **kwargs,
    )
    IntakeStore(conn, now=lambda: 1000).command(
        "research", opened["session_id"], "complete", expected_revision=2,
        action="complete", payload=None, **kwargs,
    )
    playbook = IntakePlaybookStore(conn, now=lambda: 1000).promote_problem(
        "research", opened["session_id"], "playbook", title="Repair desktop index",
        prerequisites=[], environment="Desktop", steps=[{
            "action": "Restart index worker", "expected_result": "Fresh result visible",
            "recovery": "Inspect worker logs",
        }], verification="Fresh result visible", source_rationale="Observed repair",
        concept_references=[{"kind": "concept", "id": "concept:worker",
                             "namespace": "archive", "version": 1}], **kwargs,
    )
    conn.close()
    conn = duckdb.connect(path)
    store = IntakePlaybookSearch(conn)
    result = store.search("research", "repair index", environment="Desktop", **kwargs)
    assert result["matches"][0]["playbook_id"] == playbook["playbook_id"]
    assert result["matches"][0]["revision"] == 1
    assert result["matches"][0]["trust_state"] == "draft"
    assert result["matches"][0]["concept_references"][0]["id"] == "concept:worker"
    assert store.search("research", "repair index", environment="Mobile", **kwargs)["matches"] == []
    assert store.search("research", "repair index", principal_id="bob", scopes=SCOPES)["matches"] == []
    assert store.search("research", "repair index", principal_id="alice",
                        scopes=SCOPES - {"namespace:archive:read"})["matches"] == []
