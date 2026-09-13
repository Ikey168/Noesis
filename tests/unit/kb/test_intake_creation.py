"""Creation-project history remains separate from authored-report revisions."""

import duckdb
import pytest

from src.kb.authored_reports import AuthoredReportStore
from src.kb.intake_creation import IntakeCreationStore
from src.kb.intake_modes import IntakeError

SCOPES = {
    "knowledge:intake:read", "knowledge:intake:write",
    "knowledge:reports:read", "knowledge:reports:write",
    "namespace:research:read", "namespace:research:write",
}


def _report(title="Index repair"):
    return {
        "title": title,
        "snapshot": {"id": "snapshot:one", "generations": {"research": 1}},
        "sections": [{"id": "section:one", "title": "Summary", "assertions": [{
            "id": "assertion:one", "text": "The worker stopped", "kind": "commentary",
            "dependencies": [], "citations": [],
        }]}],
        "bibliography": [], "limitations": ["Author-reviewed; no independent source check"],
    }


def test_creation_requires_current_report_and_all_author_review_checks():
    conn = duckdb.connect(":memory:")
    now = iter(range(1000, 1100)).__next__
    creations = IntakeCreationStore(conn, now=now)
    reports = AuthoredReportStore(conn, now=now)
    kwargs = {"principal_id": "alice", "scopes": SCOPES}
    created = creations.create(
        "research", "make-doc", title="Repair guide", audience="Operators",
        artifact_type="documentation", purpose="Explain recovery",
        criteria=["Steps are usable", "Citations reviewed"],
        inputs=[{"kind": "decision", "id": "decision:one", "namespace": "research", "version": 1}],
        workspace_links=[{"system": "modulo", "workspace_id": "personal",
                          "kind": "project", "id": "project:one", "version": 1}],
        **kwargs,
    )
    project_id = created["project_id"]
    assert creations.create(
        "research", "make-doc", title="Repair guide", audience="Operators",
        artifact_type="documentation", purpose="Explain recovery",
        criteria=["Steps are usable", "Citations reviewed"],
        inputs=[{"kind": "decision", "id": "decision:one", "namespace": "research", "version": 1}],
        workspace_links=[{"system": "modulo", "workspace_id": "personal",
                          "kind": "project", "id": "project:one", "version": 1}],
        **kwargs,
    )["idempotent"]
    with pytest.raises(IntakeError, match="only post"):
        creations.create("research", "software", title="App", audience="Team",
                         artifact_type="software", purpose="Ship code", criteria=["Works"],
                         inputs=[], workspace_links=[], **kwargs)
    with pytest.raises(IntakeError, match="attach a versioned"):
        creations.command("research", project_id, "early-review", expected_revision=1,
                          action="review", payload={"checks": {}, "notes": "No report"}, **kwargs)
    report = reports.create("research", "report-key", _report(), **kwargs)
    attached = creations.command("research", project_id, "attach-one", expected_revision=1,
                                 action="attach_report", payload={"report_id": report["report_id"],
                                                                   "revision": 1}, **kwargs)
    assert attached["status"] == "draft" and attached["revision"] == 2
    assert creations.command("research", project_id, "attach-one", expected_revision=1,
                             action="attach_report", payload={"report_id": report["report_id"],
                                                               "revision": 1}, **kwargs)["idempotent"]
    failed = creations.command("research", project_id, "review-fail", expected_revision=2,
                               action="review", payload={"checks": {
                                   "Steps are usable": True, "Citations reviewed": False,
                               }, "notes": "Citation needs review"}, **kwargs)
    assert failed["status"] == "review"
    with pytest.raises(IntakeError, match="failed acceptance"):
        creations.command("research", project_id, "finish-fail", expected_revision=3,
                          action="finish", payload={}, **kwargs)
    assert creations.inspect("research", project_id, **kwargs)["revision"] == 3
    reviewed = creations.command("research", project_id, "review-pass", expected_revision=3,
                                 action="review", payload={"checks": {
                                     "Steps are usable": True, "Citations reviewed": True,
                                 }, "notes": "Reviewed by author"}, **kwargs)
    assert reviewed["review"]["basis"] == "author_reported"
    finished = creations.command("research", project_id, "finish", expected_revision=4,
                                 action="finish", payload={}, **kwargs)
    assert finished["status"] == "finished"
    exported = creations.export("research", project_id, **kwargs)
    assert exported["publication_authorized"] is False
    assert exported["authored_report_export"]["report"]["report_id"] == report["report_id"]
    assert [state["revision"] for state in exported["revisions"]] == [1, 2, 3, 4, 5]
    assert creations.inspect("research", project_id, revision=2, **kwargs)["status"] == "draft"
    with pytest.raises(IntakeError, match="current owner"):
        creations.inspect("research", project_id, principal_id="bob", scopes=SCOPES)
    reports.revise("research", report["report_id"], 1, _report("Updated guide"), **kwargs)
    with pytest.raises(IntakeError, match="report changed"):
        creations.export("research", project_id, **kwargs)
    reopened = creations.command("research", project_id, "reopen", expected_revision=5,
                                 action="reopen", payload={}, **kwargs)
    assert reopened["status"] == "draft"
    updated = creations.command("research", project_id, "attach-two", expected_revision=6,
                                action="attach_report", payload={"report_id": report["report_id"],
                                                                  "revision": 2}, **kwargs)
    assert updated["report"]["revision"] == 2 and updated["review"] is None
