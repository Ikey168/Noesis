import duckdb
import pytest

from src.ingestion.revisions import DocumentRevisionStore
from src.kb.derived_revisions import DerivedRevisionStore
from src.kb.investigation_comparisons import InvestigationComparisonStore
from src.kb.research_projects import ResearchProjectError
from src.kb.research_recipes import ResearchRecipeStore

AUTH = {"principal_id": "alice", "scopes": {"operator"}}


def completed(conn, key, version="1"):
    recipes = ResearchRecipeStore(conn)
    definition = {
        "recipe_id": "review",
        "version": version,
        "namespace": "r",
        "inputs": {},
        "steps": [
            {
                "id": "check",
                "tool": "check",
                "depends_on": [],
                "input_schema": "v1",
                "output_schema": "v1",
                "network": False,
            }
        ],
        "outputs": ["check"],
        "compatibility": {},
    }
    recipe = recipes.register(definition, known_tools={"check"}, **AUTH)
    return recipes.run(
        "r",
        recipe["recipe_revision_id"],
        {},
        run_key=key,
        adapters={"check": lambda *_: {"checked": True}},
        **AUTH,
    )["run_id"]


def setup(conn=None):
    conn = conn or duckdb.connect()
    store = InvestigationComparisonStore(conn)
    source = DocumentRevisionStore(conn).observe(
        {
            "document_id": "paper",
            "source_id": "publisher",
            "content": "The benefits remain uncertain in this study.",
        }
    )
    derived = DerivedRevisionStore(conn, fixture_mode=True)

    def finding(generation, statement, configuration):
        derived.apply_generation(
            "r",
            generation,
            [
                {
                    "object_type": "claim",
                    "logical_id": "finding",
                    "content": {"statement": statement},
                    "document_id": "paper",
                    "source_revision_id": source["revision_id"],
                    "producer": {"name": "authored-test", "version": "1"},
                    "configuration": configuration,
                }
            ],
            [
                {
                    "document_id": "paper",
                    "revision_id": source["revision_id"],
                    "change_kind": "updated",
                }
            ],
        )
        derived.publish_generation("r", generation)
        identity, revision = conn.execute(
            "SELECT logical_id,revision FROM derived_object_revisions WHERE object_type='claim' ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        return {
            "kind": "finding",
            "id": identity,
            "revision": revision,
            "locator": {
                "document_id": "paper",
                "revision_id": source["revision_id"],
                "start": 0,
                "end": 12,
            },
        }

    first = finding(1, "Benefits uncertain", {"threshold": 0.5})
    run = completed(conn, "first")
    p = store.projects.create(
        "r",
        "project",
        questions=["What changed?"],
        success_criteria=["Evidence"],
        scope={"namespaces": ["r"], "domains": []},
        budget={},
        **AUTH,
    )
    p = store.projects.revise(
        "r", p["project_id"], 1, add_links=[first, {"kind": "run", "id": run}], **AUTH
    )
    left = {
        "project_id": p["project_id"],
        "project_revision": p["revision"],
        "run_id": run,
        "generations": {"r": 1},
    }
    second = finding(2, "Benefits uncertain", {"threshold": 0.8})
    run = completed(conn, "second", "2")
    p = store.projects.revise(
        "r",
        p["project_id"],
        p["revision"],
        replace_links=[second, {"kind": "run", "id": run}],
        **AUTH,
    )
    right = {
        "project_id": p["project_id"],
        "project_revision": p["revision"],
        "run_id": run,
        "generations": {"r": 2},
    }
    return conn, store, left, right


def test_completed_run_method_change_export_and_restart(tmp_path):
    path = str(tmp_path / "comparison.duckdb")
    conn, store, left, right = setup(duckdb.connect(path))
    result = store.create("r", "comparison", left, right, **AUTH)
    assert result["finding_changes"][0]["kind"] == "changed_method"
    assert result["method_changed"] and result["coverage_comparable"]
    assert result["source_changes"] == [] and result["winner"] is None
    ident = result["comparison_id"]
    assert (
        store.create("r", "comparison", left, right, **AUTH)["comparison_id"] == ident
    )
    conn.close()
    conn = duckdb.connect(path)
    store = InvestigationComparisonStore(conn, initialize=False)
    reopened = store.inspect("r", ident, **AUTH)
    assert reopened["finding_changes"] == result["finding_changes"]
    exported = store.export("r", ident, **AUTH)
    assert exported["contract"] == "noesis-investigation-comparison-export-v1"
    assert "Finding Changes" in exported["markdown"]
    assert exported["comparison"]["left"]["selector"] == left
    DocumentRevisionStore(conn).observe(
        {
            "document_id": "paper",
            "source_id": "publisher",
            "content": "A later corrected document with additional findings.",
        }
    )
    assert (
        store.inspect("r", ident, **AUTH)["source_changes"] == result["source_changes"]
    )
    with pytest.raises(ResearchProjectError, match="reused"):
        store.create("r", "comparison", right, left, **AUTH)
    conn.close()


def test_same_run_missing_generation_incompatible_scope_and_unfinished_run():
    conn, store, left, right = setup()
    result = store.create("r", "same", left, left, **AUTH)
    assert not result["finding_changes"] and not result["method_changed"]
    conn.execute(
        "UPDATE derived_object_generations SET status='pending' WHERE namespace='r' AND generation=1"
    )
    result = store.create("r", "missing", left, right, **AUTH)
    assert not result["coverage_comparable"]
    assert all(
        x["kind"] == "unavailable_or_outside_coverage"
        for x in result["finding_changes"]
    )
    p = store.projects.inspect("r", right["project_id"], **AUTH)
    changed = store.projects.revise(
        "r", p["project_id"], p["revision"], questions=["Another scope"], **AUTH
    )
    result = store.create(
        "r", "scope", right, {**right, "project_revision": changed["revision"]}, **AUTH
    )
    assert not result["scope_compatible"] and not result["coverage_comparable"]
    conn.execute(
        "UPDATE research_recipe_runs SET status='running' WHERE run_id=?",
        [right["run_id"]],
    )
    with pytest.raises(ResearchProjectError, match="completed"):
        store.create("r", "unfinished", left, right, **AUTH)


def test_revocation_unlinked_run_and_generation_mismatch():
    _conn, store, left, right = setup()
    result = store.create("r", "comparison", left, right, **AUTH)
    allowed = {
        "knowledge:projects:read",
        "knowledge:projects:write",
        "knowledge:recipes:read",
        "namespace:r:write",
        "document:paper:read",
    }
    assert store.inspect(
        "r", result["comparison_id"], principal_id="alice", scopes=allowed
    )
    with pytest.raises(ResearchProjectError) as exc:
        store.inspect(
            "r",
            result["comparison_id"],
            principal_id="alice",
            scopes=allowed - {"document:paper:read"},
        )
    assert exc.value.code == "unauthorized"
    with pytest.raises(ResearchProjectError, match="linked"):
        store.create(
            "r", "unlinked", {**left, "run_id": right["run_id"]}, right, **AUTH
        )
    with pytest.raises(ResearchProjectError, match="outside"):
        store.create("r", "future", left, {**right, "generations": {"r": 1}}, **AUTH)


def test_gap_review_history_uses_run_completion_not_latest_status():
    from src.kb.research_gaps import ResearchGapStore
    from tests.unit.kb.test_research_gaps import _observe, _policy

    conn, store, left, right = setup()
    finding = store.projects.inspect(
        "r", left["project_id"], revision=left["project_revision"], **AUTH
    )["links"][0]["id"]
    # Explicit test clock: first review before run 1, resolution before run 2.
    gaps = ResearchGapStore(conn, now=lambda: 100)
    _policy(gaps, namespace="r")
    _observe(gaps, finding, [], namespace="r", known=False, generation=1)
    discovered = gaps.discover("r", **AUTH)["items"][0]
    conn.execute(
        "UPDATE research_recipe_runs SET updated_at_ms=150 WHERE run_id=?",
        [left["run_id"]],
    )
    gaps.now = lambda: 200
    gaps.set_status(
        "r",
        discovered["gap_id"],
        "resolved",
        reason="Reviewed",
        evidence=[{"review_id": "human-fixture"}],
        **AUTH,
    )
    conn.execute(
        "UPDATE research_recipe_runs SET updated_at_ms=250 WHERE run_id=?",
        [right["run_id"]],
    )
    result = store.create("r", "gaps", left, right, **AUTH)
    assert len(result["question_changes"]) == 1
    assert result["question_changes"][0]["kind"] == "resolved"
    assert result["question_changes"][0]["before"]["status"] == "open"
    assert result["question_changes"][0]["after"]["status"] == "resolved"
    gaps.now = lambda: 300
    gaps.set_status(
        "r",
        discovered["gap_id"],
        "open",
        reason="New concern",
        evidence=[{"review_id": "second-human-fixture"}],
        **AUTH,
    )
    # A later review retains the old generation number; the completion cutoff
    # prevents it from rewriting either historical run's comparison.
    replay = store.inspect("r", result["comparison_id"], **AUTH)
    assert replay["question_changes"] == result["question_changes"]
    fresh = store.create("r", "same-historical-inputs", left, right, **AUTH)
    assert fresh["question_changes"] == result["question_changes"]

    third_run = completed(conn, "reopened-review")
    conn.execute(
        "UPDATE research_recipe_runs SET updated_at_ms=350 WHERE run_id=?", [third_run]
    )
    project = store.projects.inspect("r", right["project_id"], **AUTH)
    project = store.projects.revise(
        "r",
        project["project_id"],
        project["revision"],
        add_links=[{"kind": "run", "id": third_run}],
        **AUTH,
    )
    third = {**right, "run_id": third_run, "project_revision": project["revision"]}
    reopened = store.create("r", "reopened-gap", right, third, **AUTH)
    assert reopened["question_changes"][0]["kind"] == "reopened"


def test_relation_findings_preserve_recorded_contradiction_transitions():
    conn, store, _left, right = setup()
    source = DocumentRevisionStore(conn).revision("paper")
    derived = DerivedRevisionStore(conn, fixture_mode=True)
    p = store.projects.inspect("r", right["project_id"], **AUTH)
    selectors = []
    for generation, status in [(3, "open"), (4, "resolved")]:
        derived.apply_generation(
            "r",
            generation,
            [
                {
                    "object_type": "relation",
                    "logical_id": "dispute",
                    "content": {
                        "subject_id": "claim:a",
                        "predicate": "contradicts",
                        "object_id": "claim:b",
                        "status": status,
                    },
                    "document_id": "paper",
                    "source_revision_id": source["revision_id"],
                    "producer": {"name": "authored-test", "version": "1"},
                    "configuration": {},
                }
            ],
            [
                {
                    "document_id": "paper",
                    "revision_id": source["revision_id"],
                    "change_kind": "updated",
                }
            ],
        )
        derived.publish_generation("r", generation)
        row = conn.execute(
            "SELECT logical_id,revision FROM derived_object_revisions WHERE object_type='relation' ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        run = completed(conn, "relation-" + str(generation))
        p = store.projects.revise(
            "r",
            p["project_id"],
            p["revision"],
            replace_links=[
                {"kind": "run", "id": run},
                {
                    "kind": "finding",
                    "id": row[0],
                    "revision": row[1],
                    "locator": {
                        "document_id": "paper",
                        "revision_id": source["revision_id"],
                    },
                },
            ],
            **AUTH,
        )
        selectors.append(
            {
                "project_id": p["project_id"],
                "project_revision": p["revision"],
                "run_id": run,
                "generations": {"r": generation},
            }
        )
    result = store.create("r", "relations", *selectors, **AUTH)
    assert len(result["contradiction_changes"]) == 1
    change = result["contradiction_changes"][0]
    assert (
        change["before"]["status"] == "open" and change["after"]["status"] == "resolved"
    )
    assert change["after"]["supports"][0]["revision_id"] == source["revision_id"]
