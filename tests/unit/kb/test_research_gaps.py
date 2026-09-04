from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.research_gaps import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    ResearchGapError,
    ResearchGapStore,
)

READ = {READ_SCOPE}
WRITE = {WRITE_SCOPE}
REVIEW = {REVIEW_SCOPE}
SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def _validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def _policy(store, namespace="research", version="1", supersedes=None, **thresholds):
    return store.register_policy(
        namespace,
        version,
        thresholds,
        {},
        supersedes_policy_id=supersedes,
        principal_id="analyst",
        scopes=WRITE,
        observed_at_ms=int(version) * 10,
        provenance={"citation": f"method:{version}"},
    )


def _observe(store, object_id, supports, *, namespace="research", known=True, **values):
    return store.observe(
        namespace,
        values.pop("object_kind", "claim"),
        object_id,
        values.pop("dimension", {"geography": "DE", "source_class": "official"}),
        coverage_known=known,
        supports=supports,
        signals=values.pop("signals", {}),
        principal_id="analyst",
        scopes=WRITE,
        observed_at_ms=values.pop("observed_at_ms", 20),
        provenance={"query": f"coverage:{object_id}"},
        **values,
    )


def _support(evidence_id, source_id, **values):
    return {
        "evidence_id": evidence_id,
        "source_id": source_id,
        "accessible": True,
        "primary": True,
        "independence_group": source_id,
        "current": True,
        "method_adequate": True,
        "stance": "supports",
        **values,
    }


def test_dimensions_unknown_overlapping_gaps_stable_identity_and_lifecycle():
    clock = iter(range(100, 150))
    conn = duckdb.connect(":memory:")
    store = ResearchGapStore(conn, now=lambda: next(clock))
    policy = _policy(store)
    unknown = _observe(
        store,
        "claim:unknown",
        [],
        known=False,
        dimension={
            "claim": "claim:unknown",
            "entity": "entity:bank",
            "event": "event:decision",
            "time_range": {"start": 1, "end": 2},
            "geography": "DE",
            "source_class": "official",
            "methodology": "survey-v2",
        },
        generation=3,
        valid_from_ms=1,
        valid_to_ms=1000,
        producer={"name": "fixture", "version": "1"},
        policy={"visibility": "research"},
    )
    report = store.discover("research", principal_id="analyst", scopes=WRITE)
    assert [item["gap_type"] for item in report["items"]] == ["unknown-coverage"]
    gap = report["items"][0]
    repeated = store.discover("research", principal_id="analyst", scopes=WRITE)
    assert repeated["items"][0]["gap_id"] == gap["gap_id"]
    assert repeated["items"][0]["idempotent"]

    _observe(store, "claim:overlap", [], observed_at_ms=30)
    overlap = store.discover("research", principal_id="analyst", scopes=WRITE)
    overlap_types = {
        item["gap_type"]
        for item in overlap["items"]
        if item["object_id"] == "claim:overlap"
    }
    assert overlap_types == {
        "missing-primary",
        "insufficient-independent-support",
        "missing-current-support",
        "methodologically-inadequate",
    }
    resolved = store.set_status(
        "research",
        gap["gap_id"],
        "resolved",
        reason="coverage baseline established",
        evidence=[{"observation_id": unknown["observation_id"]}],
        principal_id="reviewer",
        scopes=REVIEW,
    )
    assert resolved["revision"] == 2 and resolved["status"] == "resolved"
    assert store.replay("research", gap["gap_id"], scopes=READ)["deterministic"]
    _validate("noesis-research-gap-policy-v1.json", policy)
    _validate("noesis-research-coverage-v1.json", unknown)
    _validate("noesis-research-gap-v1.json", resolved)
    conn.close()


def test_weak_support_inaccessible_disputed_and_policy_variation():
    conn = duckdb.connect(":memory:")
    store = ResearchGapStore(conn, now=lambda: 100)
    first = _policy(store)
    one_source = _support("e1", "source:a")
    inaccessible = _support("e2", "source:b", accessible=False)
    contradiction = _support(
        "e3", "source:c", stance="contradicts", content_hash="contradiction"
    )
    _observe(store, "claim:disputed", [one_source, inaccessible, contradiction])
    report = store.discover("research", principal_id="analyst", scopes=WRITE)
    types = {item["gap_type"] for item in report["items"]}
    assert "inaccessible-evidence" in types
    assert "unresolved-contradiction" in types
    assert "insufficient-independent-support" not in types

    _observe(store, "claim:single", [one_source], observed_at_ms=21)
    report = store.discover("research", principal_id="analyst", scopes=WRITE)
    single = next(
        item
        for item in report["items"]
        if item["object_id"] == "claim:single"
        and item["gap_type"] == "insufficient-independent-support"
    )
    second = _policy(
        store,
        version="2",
        supersedes=first["policy_id"],
        min_independent=1,
    )
    assert (
        store.discover(
            "research", principal_id="analyst", scopes=WRITE, policy_version="2"
        )["policy_id"]
        == second["policy_id"]
    )
    assert store.get("research", single["gap_id"], scopes=READ)["status"] == "resolved"
    conn.close()


def test_circular_citations_mirrors_retractions_and_false_contradictions():
    conn = duckdb.connect(":memory:")
    store = ResearchGapStore(conn, now=lambda: 100)
    _policy(
        store, min_primary=0, min_independent=0, min_current=0, min_method_adequate=0
    )
    supports = [
        _support(
            "a",
            "source:a",
            primary=False,
            cites_source_ids=["source:b"],
            content_hash="same",
        ),
        _support(
            "b",
            "source:b",
            primary=False,
            cites_source_ids=["source:a"],
            stance="contradicts",
            mirrored_from="source:a",
            content_hash="same",
        ),
        _support("r", "source:r", retracted=True),
    ]
    _observe(store, "claim:chain", supports)
    result = store.discover("research", principal_id="analyst", scopes=WRITE)
    types = {item["gap_type"] for item in result["items"]}
    assert {
        "missing-original-source",
        "circular-citation",
        "retracted-support",
    } <= types
    assert "unresolved-contradiction" not in types
    conn.close()


def test_budget_ties_blocked_sources_and_policy_reprioritization():
    conn = duckdb.connect(":memory:")
    store = ResearchGapStore(conn, now=lambda: 100)
    first = store.register_policy(
        "research",
        "1",
        {},
        {
            "decision_relevance": 1,
            "uncertainty_reduction": 0,
            "feasibility": 0,
            "freshness": 0,
            "policy_priority": 0,
            "cost": 0,
        },
        principal_id="analyst",
        scopes=WRITE,
        observed_at_ms=10,
    )
    for object_id, relevance, feasibility, source_class in (
        ("claim:a", 0.8, 0.1, "official"),
        ("claim:b", 0.8, 0.9, "official"),
        ("claim:c", 1.0, 1.0, "blocked"),
    ):
        _observe(
            store,
            object_id,
            [],
            known=False,
            dimension={"source_class": source_class},
            signals={
                "decision_relevance": relevance,
                "feasibility": feasibility,
                "estimated_cost": 2,
                "recommended_source_class": source_class,
            },
        )
    store.discover("research", principal_id="analyst", scopes=WRITE)
    plan = store.prioritize(
        "research",
        budget=2,
        max_tasks=5,
        blocked_source_classes=["blocked"],
        principal_id="analyst",
        scopes=WRITE,
    )
    assert len(plan["tasks"]) == 1 and plan["spent"] == 2
    repeated = store.prioritize(
        "research",
        budget=2,
        max_tasks=5,
        blocked_source_classes=["blocked"],
        principal_id="analyst",
        scopes=WRITE,
    )
    assert repeated["plan_hash"] == plan["plan_hash"]
    assert repeated["tasks"][0]["task_id"] == plan["tasks"][0]["task_id"]
    assert repeated["tasks"][0]["idempotent"]
    second = store.register_policy(
        "research",
        "2",
        {},
        {
            "decision_relevance": 0,
            "uncertainty_reduction": 0,
            "feasibility": 1,
            "freshness": 0,
            "policy_priority": 0,
            "cost": 0,
        },
        supersedes_policy_id=first["policy_id"],
        principal_id="analyst",
        scopes=WRITE,
        observed_at_ms=20,
    )
    reprioritized = store.prioritize(
        "research",
        budget=2,
        policy_version="2",
        blocked_source_classes=["blocked"],
        principal_id="simulation",
        scopes=READ,
        persist=False,
    )
    selected_gap = store.get(
        "research", reprioritized["tasks"][0]["gap_id"], scopes=READ
    )
    assert selected_gap["object_id"] == "claim:b"
    assert reprioritized["policy_id"] == second["policy_id"]
    assert (
        store.tasks("research", scopes=READ)["items"][0]["task_id"]
        == plan["tasks"][0]["task_id"]
    )
    _validate("noesis-research-gap-task-v1.json", plan["tasks"][0])
    conn.close()


def test_pagination_before_after_auth_and_six_domain_evaluation():
    clock = iter(range(100, 500))
    conn = duckdb.connect(":memory:")
    store = ResearchGapStore(conn, now=lambda: next(clock))
    for domain in (
        "research",
        "political",
        "economic",
        "osint",
        "technical",
        "scientific",
    ):
        _policy(store, namespace=domain)
        _observe(store, f"claim:{domain}", [], namespace=domain, known=False)
        report = store.discover(domain, principal_id="analyst", scopes=WRITE, limit=1)
        assert report["scanned"] == 1 and report["items"][0]["namespace"] == domain
    for index in range(2):
        _observe(store, f"entity:{index}", [], known=False, object_kind="entity")
    store.discover("research", principal_id="analyst", scopes=WRITE)
    first = store.list("research", scopes=READ, limit=1)
    second = store.list("research", scopes=READ, limit=1, cursor=first["next_cursor"])
    assert first["items"][0]["gap_id"] != second["items"][0]["gap_id"]
    gap = first["items"][0]
    before = gap["observed_at_ms"]
    resolved = store.set_status(
        "research",
        gap["gap_id"],
        "resolved",
        reason="completed task",
        evidence=[{"task": "manual"}],
        principal_id="reviewer",
        scopes=REVIEW,
    )
    comparison = store.compare_coverage(
        "research", before, resolved["observed_at_ms"], scopes=READ
    )
    assert comparison["resolved_delta"] == 1
    with pytest.raises(ResearchGapError, match="missing required scope"):
        store.list("research", scopes={"knowledge:read"})
    cancelled = store.discover(
        "research",
        principal_id="analyst",
        scopes=WRITE,
        cancel_requested=True,
    )
    _validate("noesis-research-gap-report-v1.json", cancelled)
    conn.close()
