from __future__ import annotations

import asyncio
import inspect

import duckdb

from tools.knowledge_engine_mcp import server


def _call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def test_freshness_mcp_policy_assessment_simulation_propagation_and_auth(
    tmp_path, monkeypatch
):
    database = tmp_path / "freshness.duckdb"
    scopes = {"knowledge:freshness:read"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(database))
    )
    tools = asyncio.run(server.mcp.get_tools())
    expected = {
        "register_evidence_freshness_policy",
        "get_evidence_freshness_policy",
        "annotate_evidence_freshness",
        "relate_evidence_applicability",
        "review_evidence_freshness_override",
        "assess_evidence_freshness",
        "get_evidence_freshness_assessment",
        "list_expiring_evidence",
        "simulate_evidence_freshness_policy",
        "compare_evidence_freshness_policies",
        "register_evidence_freshness_dependency",
        "propagate_evidence_freshness",
        "replay_evidence_freshness_assessment",
    }
    assert expected <= tools.keys()
    denied = _call(
        tools["register_evidence_freshness_policy"],
        namespace="economic",
        domain="economic",
        source_type="report",
        object_type="claim",
        semantic_version="1",
        rules={"max_age_ms": 100},
    )
    assert denied["error"]["code"] == "unauthorized"

    scopes.add("knowledge:freshness:write")
    old = _call(
        tools["register_evidence_freshness_policy"],
        namespace="economic",
        domain="economic",
        source_type="report",
        object_type="claim",
        semantic_version="1",
        rules={"max_age_ms": 100, "warning_before_ms": 10},
        observed_at_ms=10,
    )
    annotated = _call(
        tools["annotate_evidence_freshness"],
        namespace="economic",
        evidence_id="claim-evidence:1",
        domain="economic",
        source_type="report",
        object_type="claim",
        retrieved_at_ms=100,
        published_at_ms=100,
        provenance={"citation": "report:1"},
    )
    assert annotated["evidence_id"] == "claim-evidence:1"
    assessment = _call(
        tools["assess_evidence_freshness"],
        namespace="economic",
        evidence_id="claim-evidence:1",
        at_ms=200,
    )
    assert assessment["state"] == "expiring-soon"
    explained = _call(
        tools["get_evidence_freshness_assessment"],
        namespace="economic",
        assessment_id=assessment["assessment_id"],
    )
    assert explained["calculation_hash"] == assessment["calculation_hash"]
    visible_policy = _call(
        tools["get_evidence_freshness_policy"],
        namespace="economic",
        policy_id=old["policy_id"],
    )
    assert visible_policy["policy_id"] == old["policy_id"]
    hidden_policy = _call(
        tools["get_evidence_freshness_policy"],
        namespace="political",
        policy_id=old["policy_id"],
    )
    assert hidden_policy is None
    replay = _call(
        tools["replay_evidence_freshness_assessment"],
        namespace="economic",
        assessment_id=assessment["assessment_id"],
    )
    assert replay["deterministic"]

    new = _call(
        tools["register_evidence_freshness_policy"],
        namespace="economic",
        domain="economic",
        source_type="report",
        object_type="claim",
        semantic_version="2",
        rules={"max_age_ms": 1_000},
        supersedes_policy_id=old["policy_id"],
        observed_at_ms=20,
    )
    assert new["status"] == "active"
    comparison = _call(
        tools["compare_evidence_freshness_policies"],
        namespace="economic",
        evidence_ids=["claim-evidence:1"],
        at_ms=250,
        old_version="1",
        new_version="2",
    )
    assert comparison["changed"] == 1
    simulation = _call(
        tools["simulate_evidence_freshness_policy"],
        namespace="economic",
        evidence_ids=["claim-evidence:1"],
        at_ms=250,
        policy_override={"max_age_ms": 10},
        limit=1,
    )
    assert simulation["items"][0]["state"] == "stale"
    dependency = _call(
        tools["register_evidence_freshness_dependency"],
        namespace="economic",
        evidence_id="claim-evidence:1",
        consumer_kind="answer",
        consumer_id="answer:1",
    )
    assert dependency["consumer_id"] == "answer:1"
    propagated = _call(
        tools["propagate_evidence_freshness"], namespace="economic", at_ms=250
    )
    assert propagated["items"][0]["consumer_kind"] == "answer"

    denied_review = _call(
        tools["review_evidence_freshness_override"],
        namespace="economic",
        evidence_id="claim-evidence:1",
        state="fresh",
        reason="reviewed",
        evidence=[{"citation": "review:1"}],
    )
    assert denied_review["error"]["code"] == "unauthorized"
    scopes.add("knowledge:freshness:review")
    reviewed = _call(
        tools["review_evidence_freshness_override"],
        namespace="economic",
        evidence_id="claim-evidence:1",
        state="fresh",
        reason="reviewed",
        evidence=[{"citation": "review:1"}],
    )
    assert reviewed["state"] == "fresh"


def test_freshness_capabilities_advertise_contracts_and_features():
    capabilities = server.knowledge_engine_capabilities.fn()
    assert {
        "noesis-evidence-freshness-policy-v1",
        "noesis-evidence-freshness-assessment-v1",
        "noesis-evidence-applicability-relation-v1",
        "noesis-evidence-freshness-impact-v1",
    } <= set(capabilities["contracts"])
    assert {
        "versioned-evidence-freshness-policies",
        "provenance-preserving-evidence-supersession",
        "side-effect-free-freshness-simulation",
        "freshness-impact-propagation",
    } <= set(capabilities["features"])
