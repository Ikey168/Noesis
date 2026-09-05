import json

import duckdb
import pytest

from src.kb.research_projects import ResearchProjectError, ResearchProjectStore, READ_SCOPE, WRITE_SCOPE

SCOPES = {READ_SCOPE, WRITE_SCOPE, "namespace:research:write", "namespace:papers:write", "domain:economics:read", "domain:policy:read"}
AUTH = {"principal_id": "alice", "scopes": SCOPES}
REQUEST = {"questions": ["What changed?"], "success_criteria": ["Cite both domains"],
           "scope": {"domains": ["economics", "policy"], "namespaces": ["papers"]},
           "budget": {"tokens": 100, "requests": 4, "usd_micros": 1000}}


def create(store, **overrides):
    return store.create("research", "investigation-1", **{**REQUEST, **overrides}, **AUTH)


def test_restart_preserves_questions_evidence_history_and_costs(tmp_path):
    path = str(tmp_path / "projects.duckdb")
    conn = duckdb.connect(path)
    store = ResearchProjectStore(conn)
    project = create(store)
    project_id = project["project_id"]
    assert create(store)["idempotent"]
    link = {"kind": "evidence", "id": "claim-1", "namespace": "papers", "revision": 2,
            "locator": {"document_id": "doc-1", "revision_id": "revision-2", "start": 5, "end": 25}}
    store.revise("research", project_id, 1, add_links=[link], **AUTH)
    revised = store.revise("research", project_id, 2, questions=["What caused the change?"], **AUTH)
    assert revised["links"][0]["question_revision"] == 1
    costs = {"tokens": 40, "requests": 1}
    store.record_expenditure("research", project_id, "run-1", costs, 3, **AUTH)
    assert store.record_expenditure("research", project_id, "run-1", costs, 3, **AUTH)["idempotent"]
    conn.close()
    conn = duckdb.connect(path, read_only=True)
    store = ResearchProjectStore(conn, initialize=False)
    reopened = store.inspect("research", project_id, **AUTH)
    assert reopened["revision"] == 4 and reopened["spent"]["tokens"] == 40
    assert reopened["questions"] == ["What caused the change?"]
    assert reopened["links"][0]["locator"] == link["locator"]
    assert store.inspect("research", project_id, revision=2, **AUTH)["questions"] == REQUEST["questions"]
    assert store.list("research", **AUTH)["projects"][0]["project_id"] == project_id
    conn.close()


def test_stale_revision_and_budget_failure_are_atomic():
    conn = duckdb.connect()
    store = ResearchProjectStore(conn)
    pid = create(store)["project_id"]
    store.revise("research", pid, 1, questions=["A new question"], **AUTH)
    with pytest.raises(ResearchProjectError, match="project changed"):
        store.revise("research", pid, 1, questions=["Lost update"], **AUTH)
    for expected, costs in [(1, {"tokens": 5}), (2, {"tokens": 101})]:
        with pytest.raises(ResearchProjectError):
            store.record_expenditure("research", pid, "failed", costs, expected, **AUTH)
    assert conn.execute("SELECT count(*) FROM research_project_expenditures").fetchone()[0] == 0
    assert store.inspect("research", pid, **AUTH)["spent"]["tokens"] == 0
    assert store.inspect("research", pid, **AUTH)["questions"] == ["A new question"]


def test_owner_namespace_and_domain_access_are_rechecked():
    conn = duckdb.connect()
    store = ResearchProjectStore(conn)
    pid = create(store)["project_id"]
    for auth in [{**AUTH, "principal_id": "bob"},
                 {**AUTH, "scopes": SCOPES - {"namespace:papers:write"}},
                 {**AUTH, "scopes": SCOPES - {"domain:policy:read"}}]:
        with pytest.raises(ResearchProjectError) as exc:
            store.inspect("research", pid, **auth)
        assert exc.value.code == "unauthorized"
        assert store.list("research", **auth)["projects"] == []
        with pytest.raises(ResearchProjectError):
            store.revise("research", pid, 1, status="archived", **auth)
    with pytest.raises(ResearchProjectError):
        store.inspect("other", pid, **AUTH)


def test_archived_history_and_idempotency_conflicts():
    store = ResearchProjectStore(duckdb.connect())
    pid = create(store)["project_id"]
    with pytest.raises(ResearchProjectError) as exc:
        create(store, questions=["Different request"])
    assert exc.value.code == "idempotency_conflict"
    store.revise("research", pid, 1, status="archived", **AUTH)
    assert create(store)["status"] == "archived"
    with pytest.raises(ResearchProjectError, match="immutable"):
        store.revise("research", pid, 2, questions=["Cannot change"], **AUTH)
    assert store.inspect("research", pid, revision=1, **AUTH)["status"] == "active"


def test_snapshot_expiration_and_secret_rejection():
    from src.kb.research_snapshots import ResearchSnapshotStore
    conn = duckdb.connect()
    ResearchSnapshotStore(conn)
    store = ResearchProjectStore(conn, now=lambda: 100)
    pid = create(store)["project_id"]
    with pytest.raises(ResearchProjectError, match="bearer"):
        store.revise("research", pid, 1, add_links=[{"kind": "snapshot", "id": "s1", "generation": 1, "token": "secret"}], **AUTH)
    store.revise("research", pid, 1, add_links=[{"kind": "snapshot", "id": "s1", "generation": 1}], **AUTH)
    assert store.inspect("research", pid, **AUTH)["reference_availability"][0]["status"] == "unavailable"
    conn.execute("INSERT INTO research_snapshot_sessions VALUES ('s1','hash','alice','[]','{}','{}','v','active',0,50,50,NULL)")
    assert store.inspect("research", pid, **AUTH)["reference_availability"][0]["status"] == "expired"
    assert "secret" not in conn.execute("SELECT string_agg(content_json,'') FROM research_project_revisions").fetchone()[0]


def test_public_contract():
    from pathlib import Path
    import jsonschema
    project = create(ResearchProjectStore(duckdb.connect()))
    schema = json.loads(Path("contracts/schemas/jsonschema/noesis-research-project-v1.json").read_text())
    jsonschema.validate(project, schema)


def test_concurrent_writers_cannot_overwrite_each_other(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    path = str(tmp_path / "concurrent.duckdb")
    conn = duckdb.connect(path)
    pid = create(ResearchProjectStore(conn))["project_id"]
    barrier = Barrier(2)

    def write(question):
        connection = duckdb.connect(path)
        store = ResearchProjectStore(connection, initialize=False)
        barrier.wait(timeout=5)
        try:
            return store.revise("research", pid, 1, questions=[question], **AUTH)
        except ResearchProjectError as exc:
            return {"error": exc.code}
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ["First question", "Second question"]))
    assert sum(r.get("revision") == 2 for r in results) == 1
    assert sum(r.get("error") == "revision_conflict" for r in results) == 1
    assert conn.execute("SELECT count(*) FROM research_project_revisions").fetchone()[0] == 2
    conn.close()
