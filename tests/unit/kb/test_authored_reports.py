import copy

import duckdb
import pytest

from src.kb.authored_reports import AuthoredReportStore, ReportError

AUTH = {"principal_id": "alice", "scopes": {"knowledge:reports:read", "knowledge:reports:write", "namespace:r:write", "namespace:sources:read"}}
CONTENT = {"title": "Authored investigation", "snapshot": {"id": "snapshot:1", "generations": {"sources": 1}},
    "sections": [{"id": "summary", "title": "Summary", "assertions": [
        {"id": "a1", "text": "The reported value increased.", "kind": "sourced", "citations": ["source-1"],
         "dependencies": [{"kind": "source", "id": "doc", "revision": "revision-1", "namespace": "sources",
                           "locator": {"document_id": "doc", "revision_id": "revision-1", "start": 0, "end": 29}}]},
        {"id": "a2", "text": "I suspect a seasonal effect.", "kind": "commentary", "citations": [], "dependencies": []}]}],
    "bibliography": [{"id": "source-1", "text": "Source One. Observations. 2026."}], "limitations": ["One source; no causal identification."]}


def test_export_restart_and_reopen_preserve_authored_content(tmp_path):
    path = str(tmp_path / "reports.duckdb")
    conn = duckdb.connect(path)
    store = AuthoredReportStore(conn)
    report = store.create("r", "report", CONTENT, **AUTH)
    exported = store.export("r", report["report_id"], **AUTH)
    assert "[Author commentary] I suspect" in exported["markdown"]
    assert "support not independently verified" in exported["markdown"]
    assert exported["bibliography"] == CONTENT["bibliography"]
    conn.close()
    conn = duckdb.connect(path)
    store = AuthoredReportStore(conn)
    assert store.inspect("r", report["report_id"], **AUTH)["content"] == CONTENT
    assert store.reopen("r", "import", exported, **AUTH)["content"] == CONTENT
    assert store.reopen("r", "import", exported, **AUTH)["idempotent"]
    exported["report"]["content"]["title"] = "Tampered"
    with pytest.raises(ReportError, match="integrity"):
        store.reopen("r", "bad-import", exported, **AUTH)


def test_revision_conflicts_preserve_history_and_access_is_current():
    conn = duckdb.connect()
    store = AuthoredReportStore(conn)
    report = store.create("r", "report", CONTENT, **AUTH)
    revised = copy.deepcopy(CONTENT)
    revised["sections"][0]["assertions"][1]["text"] = "Seasonality remains uncertain."
    store.revise("r", report["report_id"], 1, revised, **AUTH)
    with pytest.raises(ReportError) as error:
        store.revise("r", report["report_id"], 1, CONTENT, **AUTH)
    assert error.value.code == "revision_conflict"
    assert store.inspect("r", report["report_id"], revision=1, **AUTH)["content"] == CONTENT
    for auth in [{**AUTH, "principal_id": "bob"}, {**AUTH, "scopes": AUTH["scopes"] - {"namespace:sources:read"}}]:
        with pytest.raises(ReportError) as error:
            store.export("r", report["report_id"], revision=1, **auth)
        assert error.value.code == "unauthorized"
    assert store.create("r", "report", CONTENT, **AUTH)["revision"] == 2


@pytest.mark.parametrize("change", ["missing_reference", "duplicate_id", "missing_citation", "secret", "coordinate"])
def test_invalid_structure_is_rejected_before_writing(change):
    content = copy.deepcopy(CONTENT)
    assertion = content["sections"][0]["assertions"][0]
    if change == "missing_reference":
        assertion["dependencies"] = []
    elif change == "duplicate_id":
        assertion["id"] = "summary"
    elif change == "missing_citation":
        assertion["citations"] = ["unknown"]
    elif change == "secret":
        content["snapshot"]["bearer_token"] = "secret"
    else:
        assertion["dependencies"][0]["locator"]["end"] = -1
    store = AuthoredReportStore(duckdb.connect())
    with pytest.raises(ReportError):
        store.create("r", "bad", content, **AUTH)
    assert store.conn.execute("SELECT count(*) FROM authored_reports").fetchone()[0] == 0
