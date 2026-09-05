import duckdb
import pytest

from src.kb.project_branches import ProjectBranchStore
from src.kb.research_projects import ResearchProjectError
from src.kb.derived_revisions import DerivedRevisionStore

AUTH = {"principal_id": "alice", "scopes": {"knowledge:projects:read", "knowledge:projects:write", "namespace:r:write"}}


def setup():
    conn = duckdb.connect()
    derived = DerivedRevisionStore(conn, fixture_mode=True)
    derived.apply_generation("r", 1, [], [])
    derived.publish_generation("r", 1)
    store = ProjectBranchStore(conn)
    parent = store.create("r", "parent", questions=["Why?"], success_criteria=["Independent evidence"],
                          scope={"namespaces": ["r"], "domains": []}, budget={"tokens": 100}, **AUTH)
    reference = {"kind": "evidence", "id": "claim", "revision": 1,
                 "locator": {"document_id": "source", "revision_id": "source-v1", "start": 0, "end": 10}}
    parent = store.revise("r", parent["project_id"], 1, add_links=[reference], **AUTH)
    return conn, store, parent


def branch(store, parent, key="alternative", **overrides):
    return store.branch("r", parent["project_id"], parent["revision"], key,
        **{"baseline": {"r": 1}, "changes": {"methods": ["Alternate extraction configuration"]},
           "budget": {"tokens": 20}, **overrides}, **AUTH)


def test_branch_replay_independent_changes_and_costs():
    conn, store, parent = setup()
    result = branch(store, parent)
    child = result["project"]
    assert branch(store, parent)["idempotent"]
    assert child["links"] == parent["links"]
    store.record_expenditure("r", child["project_id"], "execution", {"tokens": 10}, 1, **AUTH)
    comparison = store.compare("r", parent["project_id"], child["project_id"], **AUTH)
    assert comparison["evidence_references_equal"]
    assert comparison["right"]["incremental_costs"]["tokens"] == 10
    assert comparison["right"]["declared_changes"]["methods"]
    assert not comparison["coverage_comparable"] and comparison["winner"] is None
    assert store.inspect("r", parent["project_id"], **AUTH)["spent"]["tokens"] == 0
    store.revise("r", child["project_id"], 2, replace_links=[], status="archived", **AUTH)
    difference = store.compare("r", parent["project_id"], child["project_id"], **AUTH)
    assert difference["right"]["removed"] == parent["links"]
    assert store.inspect("r", parent["project_id"], **AUTH)["status"] == "active"


def test_source_correction_between_siblings_and_missing_baseline():
    conn, store, parent = setup()
    first = branch(store, parent, "first")["project"]
    second = branch(store, parent, "second", changes={"assumptions": ["Alternative explanation"]})["project"]
    corrected = {**second["links"][0], "revision": 2, "locator": {"document_id": "source", "revision_id": "source-v2", "start": 5, "end": 15}}
    store.revise("r", second["project_id"], 1, replace_links=[corrected], **AUTH)
    comparison = store.compare("r", first["project_id"], second["project_id"], **AUTH)
    assert comparison["right"]["revised"][0]["after"]["locator"]["revision_id"] == "source-v2"
    assert not comparison["evidence_references_equal"]
    conn.execute("DELETE FROM derived_object_generations")
    assert store.compare("r", first["project_id"], second["project_id"], **AUTH)["baseline_availability"][0]["status"] == "unavailable"
    assert branch(store, parent, "first")["idempotent"]
    with pytest.raises(ResearchProjectError, match="retained"):
        branch(store, parent, "new")


def test_access_conflicts_and_incompatible_baselines():
    conn, store, parent = setup()
    child = branch(store, parent)["project"]
    with pytest.raises(ResearchProjectError) as error:
        branch(store, parent, changes={"questions": ["Different request"]})
    assert error.value.code == "idempotency_conflict"
    for scopes in [set(), {"knowledge:projects:read", "knowledge:projects:write"}]:
        with pytest.raises(ResearchProjectError) as error:
            store.compare("r", parent["project_id"], child["project_id"], principal_id="alice", scopes=scopes)
        assert error.value.code == "unauthorized"
    revised = store.revise("r", parent["project_id"], 2, questions=["A different question"], **AUTH)
    other = branch(store, revised, "later")["project"]
    with pytest.raises(ResearchProjectError) as error:
        store.compare("r", child["project_id"], other["project_id"], **AUTH)
    assert error.value.code == "incompatible_baseline"


def test_branch_atomic_failure_does_not_create_orphan(monkeypatch):
    conn, store, parent = setup()
    conn.execute("CREATE UNIQUE INDEX one_branch_hash ON research_project_branches(request_hash)")
    branch(store, parent, "one")
    # Different key, same payload hits the deliberately injected constraint.
    before = conn.execute("SELECT count(*) FROM research_projects").fetchone()[0]
    with pytest.raises(ResearchProjectError):
        branch(store, parent, "two")
    assert conn.execute("SELECT count(*) FROM research_projects").fetchone()[0] == before
