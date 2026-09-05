from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft202012Validator

from src.kb.source_planner import (
    EXECUTE_SCOPE,
    READ_SCOPE,
    WRITE_SCOPE,
    SourcePlannerError,
    SourcePlannerStore,
)

READ = {READ_SCOPE}
WRITE = {WRITE_SCOPE}
EXECUTE = {EXECUTE_SCOPE}
SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def _validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def _capability(
    store,
    source_id,
    *,
    namespace="research",
    version="1",
    supersedes=None,
    group=None,
    authority=0.8,
    cost=1,
    access=None,
    observed_at_ms=10,
):
    return store.register_capability(
        namespace,
        source_id,
        version,
        coverage={"domains": [namespace], "evidence_classes": ["primary", "dataset"]},
        authority={"score": authority, "basis": "editorial-policy"},
        access=access
        or {"license_id": "open", "terms_accepted": True, "redistribution": True},
        latency={"p95_ms": 100},
        cost={"per_query": cost},
        rate_limits={"requests_per_minute": 10},
        query_forms=["search", "series"],
        connector={
            "kind": "source-pack",
            "pack_id": "pack:fixtures",
            "source_id": source_id,
        },
        dependency_group=group or source_id,
        supersedes_capability_id=supersedes,
        principal_id="operator",
        scopes=WRITE,
        observed_at_ms=observed_at_ms,
        provenance={"citation": f"registry:{source_id}:{version}"},
    )


def _objective(store, *, namespace="research", constraints=None, parts=None):
    return store.create_objective(
        namespace,
        "What changed and what is the primary evidence?",
        parts or [{"question": "What changed?", "query_form": "search"}],
        ["primary"],
        {"domain": namespace, **(constraints or {})},
        principal_id="analyst",
        scopes=WRITE,
        observed_at_ms=20,
        provenance={"request": "fixture"},
    )


def test_capability_credentials_licenses_outages_secrets_and_version_changes():
    conn = duckdb.connect(":memory:")
    store = SourcePlannerStore(conn, now=lambda: 100)
    missing = _capability(
        store,
        "credentialed",
        access={
            "credential_required": True,
            "credential_ref": "NOESIS_SOURCE_KEY",
            "license_id": "restricted",
            "terms_accepted": False,
            "redistribution": False,
        },
    )
    _capability(
        store,
        "outage",
        access={
            "outage": True,
            "license_id": "open",
            "terms_accepted": True,
            "redistribution": True,
        },
    )
    objective = _objective(store, constraints={"redistribute": True})
    preview = store.preview(
        "research",
        objective["objective_id"],
        at_ms=30,
        scopes=READ,
        credential_available=lambda _: False,
    )
    exclusions = {
        item["source_id"]: set(item["reasons"]) for item in preview["exclusions"]
    }
    assert {
        "credential-missing",
        "license-not-accepted",
        "redistribution-forbidden",
    } <= exclusions["credentialed"]
    assert "source-unavailable" in exclusions["outage"]
    with pytest.raises(SourcePlannerError, match="credential material"):
        store.register_capability(
            "research",
            "unsafe",
            "1",
            coverage={},
            authority={"score": 1},
            access={"api_key": "plaintext"},
            latency={},
            cost={},
            rate_limits={},
            query_forms=["search"],
            connector={},
            dependency_group="unsafe",
            principal_id="operator",
            scopes=WRITE,
        )
    upgraded = _capability(
        store,
        "credentialed",
        version="2",
        supersedes=missing["capability_id"],
        access={"license_id": "open", "terms_accepted": True, "redistribution": True},
        observed_at_ms=40,
    )
    assert (
        store.capability("research", missing["capability_id"], scopes=READ)["status"]
        == "superseded"
    )
    assert upgraded["status"] == "active"
    _validate("noesis-source-capability-v1.json", upgraded)
    conn.close()


def test_objective_defaults_conflicts_infeasibility_and_canonical_hashing():
    conn = duckdb.connect(":memory:")
    store = SourcePlannerStore(conn, now=lambda: 100)
    objective = _objective(store, parts=[])
    repeated = _objective(store, parts=[])
    assert (
        repeated["objective_id"] == objective["objective_id"] and repeated["idempotent"]
    )
    assert objective["constraints"]["budget"] == 10
    assert objective["decomposition"][0]["query_form"] == "search"
    preview = store.preview(
        "research", objective["objective_id"], at_ms=30, scopes=READ
    )
    assert not preview["feasible"]
    assert {"question-parts-uncovered", "independence-unmet"} <= set(
        preview["infeasibility"]
    )
    with pytest.raises(SourcePlannerError, match="both required and forbidden"):
        _objective(
            store,
            constraints={"required_sources": ["a"], "forbidden_sources": ["a"]},
        )
    _validate("noesis-source-research-objective-v1.json", objective)
    _validate("noesis-source-acquisition-plan-v1.json", preview)
    conn.close()


def test_explainable_selection_redundancy_independence_low_budget_and_ties():
    conn = duckdb.connect(":memory:")
    store = SourcePlannerStore(conn, now=lambda: 100)
    for source_id, group in (
        ("alpha", "owner:a"),
        ("beta", "owner:a"),
        ("gamma", "owner:g"),
    ):
        _capability(store, source_id, group=group, authority=0.8, cost=1)
    objective = _objective(store, constraints={"min_independence": 2, "budget": 2})
    first = store.preview("research", objective["objective_id"], at_ms=30, scopes=READ)
    second = store.preview("research", objective["objective_id"], at_ms=30, scopes=READ)
    assert first == second
    assert [item["source_id"] for item in first["steps"]] == ["alpha", "gamma"]
    assert any(
        item["source_id"] == "beta" and item["reasons"] == ["redundant-source"]
        for item in first["exclusions"]
    )
    assert first["feasible"] and first["coverage"]["independent_groups"] == 2
    poor = _objective(store, constraints={"budget": 0.5, "min_independence": 2})
    low = store.preview("research", poor["objective_id"], at_ms=30, scopes=READ)
    assert not low["feasible"] and low["steps"] == []
    conn.close()


def test_execution_rate_limit_partial_failure_fallback_budget_and_replay():
    conn = duckdb.connect(":memory:")
    store = SourcePlannerStore(conn, now=lambda: 100)
    _capability(store, "alpha", group="same", authority=1, cost=1)
    _capability(store, "beta", group="same", authority=0.5, cost=1)
    objective = _objective(store, constraints={"budget": 2, "retries": 1})
    plan = store.preview(
        "research",
        objective["objective_id"],
        at_ms=30,
        scopes=WRITE,
        persist=True,
        principal_id="analyst",
    )
    assert plan["fallback_steps"][0]["source_id"] == "beta"
    attempts = {"alpha": 0, "beta": 0}

    def runner(capability, step, checkpoint):
        source_id = capability["source_id"]
        attempts[source_id] += 1
        if source_id == "alpha":
            return {"status": "failed", "error": {"code": "rate_limited"}}
        return {
            "status": "completed",
            "cost": 1,
            "counts": {"items": 2},
            "cursor": {"page": 1},
        }

    receipt = store.execute(
        "research",
        plan["plan_id"],
        "run-1",
        runner=runner,
        principal_id="operator",
        scopes=EXECUTE,
    )
    assert attempts == {"alpha": 2, "beta": 1}
    assert receipt["status"] == "partial" and receipt["adaptive_replanned"]
    assert receipt["budget"]["spent"] <= receipt["budget"]["limit"]
    assert store.execute(
        "research",
        plan["plan_id"],
        "run-1",
        runner=runner,
        principal_id="operator",
        scopes=EXECUTE,
    )["idempotent"]
    assert store.replay("research", receipt["run_id"], scopes=READ)["deterministic"]
    _validate("noesis-source-plan-receipt-v1.json", receipt)
    conn.close()


def test_stale_capability_crash_recovery_cancellation_and_scope():
    conn = duckdb.connect(":memory:")
    clock = iter(range(100, 200))
    store = SourcePlannerStore(conn, now=lambda: next(clock))
    old = _capability(store, "alpha")
    objective = _objective(store)
    plan = store.preview(
        "research",
        objective["objective_id"],
        at_ms=30,
        scopes=WRITE,
        persist=True,
        principal_id="analyst",
    )
    _capability(
        store, "alpha", version="2", supersedes=old["capability_id"], observed_at_ms=40
    )
    stale = store.execute(
        "research",
        plan["plan_id"],
        "stale",
        runner=lambda *_: {"status": "completed"},
        principal_id="operator",
        scopes=EXECUTE,
    )
    assert stale["failures"][0]["code"] == "stale-capability"

    fresh_plan = store.preview(
        "research",
        objective["objective_id"],
        at_ms=50,
        scopes=WRITE,
        persist=True,
        principal_id="analyst",
    )
    with pytest.raises(KeyboardInterrupt):
        store.execute(
            "research",
            fresh_plan["plan_id"],
            "crash",
            runner=lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()),
            principal_id="operator",
            scopes=EXECUTE,
        )
    recovered = store.execute(
        "research",
        fresh_plan["plan_id"],
        "crash",
        runner=lambda *_: {"status": "completed", "cost": 1},
        principal_id="operator",
        scopes=EXECUTE,
    )
    assert recovered["status"] == "completed"
    cancelled = store.execute(
        "research",
        fresh_plan["plan_id"],
        "cancel",
        runner=lambda *_: {"status": "completed"},
        cancelled=lambda: True,
        principal_id="operator",
        scopes=EXECUTE,
    )
    assert cancelled["status"] == "cancelled"
    with pytest.raises(SourcePlannerError, match="missing required scope"):
        store.preview(
            "research", objective["objective_id"], at_ms=50, scopes={"knowledge:read"}
        )
    conn.close()


def test_scholarly_objective_discovers_native_ids_and_replays():
    from src.ingestion.source_pack_runtime import HTTPSPageAdapter
    from src.ingestion.source_packs import validate_source_pack
    from src.kb.source_planner import scholarly_decomposition

    conn = duckdb.connect()
    store = SourcePlannerStore(conn, now=lambda: 100)
    try:
        _capability(store, "crossref-works")
        parts = scholarly_decomposition(
            "climate evidence", author="Jane Doe", from_date="2024-01-01"
        )
        objective = _objective(store, parts=parts, constraints={"budget": 2})
        plan = store.preview(
            "research",
            objective["objective_id"],
            at_ms=30,
            scopes=WRITE,
            persist=True,
            principal_id="analyst",
        )
        source = validate_source_pack(
            json.loads(Path("config/source_packs/research.json").read_text())
        )["sources"][0]
        calls = []

        def transport(**kw):
            calls.append(kw["params"])
            return {
                "content": json.dumps(
                    {
                        "message": {
                            "items": [
                                {"DOI": "10.1234/discovered", "title": ["Evidence"]}
                            ]
                        }
                    }
                )
            }

        def runner(capability, step, checkpoint):
            page = HTTPSPageAdapter(source, transport=transport).fetch_page(
                {"operation": "search", "parameters": step["queries"][0]["parameters"], "limit": 10},
                cursor=None,
            )
            return {
                "status": "completed",
                "counts": {"items": len(page.records)},
                "cost": 1,
                "cursor": {"work_ids": [record["id"] for record in page.records]},
            }

        result = store.execute(
            "research",
            plan["plan_id"],
            "native",
            runner=runner,
            principal_id="operator",
            scopes=EXECUTE,
        )
        assert calls[0]["query.author"] == "Jane Doe"
        assert "from-pub-date:2024-01-01" in calls[0]["filter"]
        assert store.execute(
            "research",
            plan["plan_id"],
            "native",
            runner=runner,
            principal_id="operator",
            scopes=EXECUTE,
        )["idempotent"]
        assert len(calls) == 1
        assert store.replay("research", result["run_id"], scopes=READ)["deterministic"]
        assert "10.1234/discovered" in json.dumps(result)
    finally:
        conn.close()


def test_cp_sat_preserves_execution_fallbacks_and_plan_replay():
    pytest.importorskip("ortools")
    conn = duckdb.connect()
    store = SourcePlannerStore(conn, now=lambda: 100)
    _capability(store, "berlin-primary", cost=1)
    _capability(store, "berlin-alternative", cost=1)
    objective = _objective(store, constraints={"budget": 2, "retries": 0})
    plan = store.preview("research", objective["objective_id"], at_ms=30,
                         scopes=WRITE, persist=True, principal_id="analyst", optimizer="cp-sat")
    assert plan["constraints"]["optimization"]["status"] == "OPTIMAL"
    assert len(plan["steps"]) == 1 and len(plan["fallback_steps"]) == 1
    assert plan["budget"]["projected"] == 1
    repeated = store.preview("research", objective["objective_id"], at_ms=30,
                             scopes=READ, optimizer="cp-sat")
    assert repeated["plan_hash"] == plan["plan_hash"]
    primary = plan["steps"][0]["source_id"]
    calls = []
    def runner(capability, step, checkpoint):
        calls.append(capability["source_id"])
        if capability["source_id"] == primary:
            return {"status": "failed", "error": {"code": "unavailable"}}
        return {"status": "completed", "cost": 1, "counts": {"items": 1}}
    receipt = store.execute("research", plan["plan_id"], "optimized-run", runner=runner,
                            principal_id="operator", scopes=EXECUTE)
    assert calls == [primary, plan["fallback_steps"][0]["source_id"]]
    assert receipt["adaptive_replanned"]
    assert receipt["budget"]["spent"] <= 2
    _validate("noesis-source-acquisition-plan-v1.json", plan)
    conn.close()
