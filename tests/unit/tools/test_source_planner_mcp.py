from __future__ import annotations

import asyncio
import inspect
import json

import duckdb

from tools.knowledge_engine_mcp import server


def _call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def _register(tools, namespace, source_id="official"):
    return _call(
        tools["register_source_capability"],
        namespace=namespace,
        source_id=f"{source_id}:{namespace}",
        semantic_version="1",
        coverage={"domains": [namespace], "evidence_classes": ["primary"]},
        authority={"score": 0.9},
        access={
            "credential_required": True,
            "credential_ref": "NOESIS_FIXTURE_KEY",
            "license_id": "open",
            "terms_accepted": True,
            "redistribution": True,
        },
        latency={"p95_ms": 100},
        cost={"per_query": 1},
        rate_limits={"requests_per_minute": 10},
        query_forms=["search"],
        connector={
            "kind": "source-pack",
            "pack_id": "pack:fixture",
            "source_id": f"{source_id}:{namespace}",
        },
        dependency_group=f"owner:{namespace}",
        observed_at_ms=10,
    )


def test_source_planner_mcp_preview_execution_receipts_scopes_and_secret_isolation(
    tmp_path, monkeypatch
):
    database = tmp_path / "source-planner.duckdb"
    scopes = {"knowledge:source-planner:read"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(database))
    )
    monkeypatch.setattr(
        server,
        "_secret_resolver",
        lambda ref: "super-secret-value" if ref == "NOESIS_FIXTURE_KEY" else None,
    )

    class Runtime:
        def fixture_adapters(self, pack_id, root):
            return {"fixture": object()}

        def run(self, request, **kwargs):
            return {
                "status": "complete",
                "sources": [
                    {
                        "counts": {"inserted": 2},
                        "cursor": {"end": "done"},
                    }
                ],
            }

    monkeypatch.setattr(
        server, "_source_pack_runtime", lambda conn, initialize=False: Runtime()
    )
    tools = asyncio.run(server.mcp.get_tools())
    expected = {
        "register_source_capability",
        "get_source_capability",
        "create_source_research_objective",
        "preview_source_acquisition_plan",
        "create_source_acquisition_plan",
        "get_source_acquisition_plan",
        "explain_source_acquisition_plan",
        "execute_source_acquisition_plan",
        "cancel_source_acquisition_plan",
        "inspect_source_acquisition_run",
        "replay_source_acquisition_run",
    }
    assert expected <= tools.keys()
    denied = _register(tools, "research")
    assert denied["error"]["code"] == "unauthorized"
    scopes.add("knowledge:source-planner:write")
    capability = _register(tools, "research")
    assert "super-secret-value" not in json.dumps(capability)
    objective = _call(
        tools["create_source_research_objective"],
        namespace="research",
        question="What changed?",
        decomposition=[{"question": "What changed?", "query_form": "search"}],
        evidence_classes=["primary"],
        constraints={"domain": "research", "budget": 2},
        observed_at_ms=20,
    )
    preview = _call(
        tools["preview_source_acquisition_plan"],
        namespace="research",
        objective_id=objective["objective_id"],
        at_ms=30,
    )
    assert preview["feasible"]
    assert "super-secret-value" not in json.dumps(preview)
    plan = _call(
        tools["create_source_acquisition_plan"],
        namespace="research",
        objective_id=objective["objective_id"],
        at_ms=30,
    )
    explained = _call(
        tools["explain_source_acquisition_plan"],
        namespace="research",
        plan_id=plan["plan_id"],
    )
    assert explained["steps"][0]["score_components"]["authority"] == 0.9
    denied_execute = _call(
        tools["execute_source_acquisition_plan"],
        namespace="research",
        plan_id=plan["plan_id"],
        execution_key="fixture-run",
    )
    assert denied_execute["error"]["code"] == "unauthorized"
    scopes.add("knowledge:source-planner:execute")
    receipt = _call(
        tools["execute_source_acquisition_plan"],
        namespace="research",
        plan_id=plan["plan_id"],
        execution_key="fixture-run",
    )
    assert receipt["status"] == "completed" and receipt["checkpointed"]
    inspected = _call(
        tools["inspect_source_acquisition_run"],
        namespace="research",
        run_id=receipt["run_id"],
    )
    assert inspected["receipt_hash"] == receipt["receipt_hash"]
    assert _call(
        tools["replay_source_acquisition_run"],
        namespace="research",
        run_id=receipt["run_id"],
    )["deterministic"]
    inactive = _call(
        tools["cancel_source_acquisition_plan"],
        namespace="research",
        plan_id=plan["plan_id"],
        execution_key="fixture-run",
    )
    assert inactive["error"]["code"] == "run_not_active"


def test_source_planner_mcp_six_domain_reproducibility(tmp_path, monkeypatch):
    database = tmp_path / "source-planner-six.duckdb"
    scopes = {"knowledge:source-planner:read", "knowledge:source-planner:write"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(database))
    )
    monkeypatch.setattr(server, "_secret_resolver", lambda _: "available")
    tools = asyncio.run(server.mcp.get_tools())
    for domain in (
        "research",
        "political",
        "economic",
        "osint",
        "technical",
        "scientific",
    ):
        _register(tools, domain)
        objective = _call(
            tools["create_source_research_objective"],
            namespace=domain,
            question=f"Investigate {domain}",
            decomposition=[
                {"question": f"Investigate {domain}", "query_form": "search"}
            ],
            evidence_classes=["primary"],
            constraints={"domain": domain},
            observed_at_ms=20,
        )
        one = _call(
            tools["preview_source_acquisition_plan"],
            namespace=domain,
            objective_id=objective["objective_id"],
            at_ms=30,
        )
        two = _call(
            tools["preview_source_acquisition_plan"],
            namespace=domain,
            objective_id=objective["objective_id"],
            at_ms=30,
        )
        assert one["plan_hash"] == two["plan_hash"] and one["feasible"]


def test_source_planner_capabilities_advertise_contracts_and_features():
    capabilities = server.knowledge_engine_capabilities.fn()
    assert {
        "noesis-source-capability-v1",
        "noesis-source-research-objective-v1",
        "noesis-source-acquisition-plan-v1",
        "noesis-source-plan-receipt-v1",
    } <= set(capabilities["contracts"])
    assert {
        "credential-safe-source-capability-registry",
        "explainable-constrained-source-selection",
        "checkpointed-source-plan-execution",
        "adaptive-source-plan-fallbacks",
    } <= set(capabilities["features"])
