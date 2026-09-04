from __future__ import annotations

import asyncio
import inspect

import duckdb

from tools.knowledge_engine_mcp import server


def _call(tool, **kwargs):
    value = tool.fn(**kwargs)
    return asyncio.run(value) if inspect.isawaitable(value) else value


def test_quantitative_mcp_discovery_vintages_comparison_calculation_and_auth(
    tmp_path, monkeypatch
):
    database = tmp_path / "quantitative.duckdb"
    scopes = {"knowledge:quantitative:read"}
    monkeypatch.setattr(server, "_context", lambda: ("analyst", scopes))
    monkeypatch.setattr(
        server, "_connection", lambda *, read_only: duckdb.connect(str(database))
    )
    tools = asyncio.run(server.mcp.get_tools())
    expected = {
        "register_quantitative_unit",
        "register_quantitative_metric",
        "revise_quantitative_metric",
        "record_quantitative_observation",
        "record_quantitative_series_break",
        "discover_quantitative_metrics",
        "get_quantitative_metric",
        "read_quantitative_series",
        "assess_quantitative_comparability",
        "convert_quantitative_value",
        "evaluate_quantitative_formula",
        "transform_quantitative_frequency",
        "adjust_quantitative_inflation",
        "replay_quantitative_calculation",
    }
    assert expected <= tools.keys()
    denied = _call(
        tools["register_quantitative_metric"],
        namespace="economic",
        canonical_name="GDP",
        definition="Gross domestic product.",
        unit="EUR",
    )
    assert denied["error"]["code"] == "unauthorized"
    scopes.update({"knowledge:quantitative:write", "knowledge:quantitative:calculate"})
    metric = _call(
        tools["register_quantitative_metric"],
        namespace="economic",
        canonical_name="GDP",
        definition="Gross domestic product.",
        unit="EUR",
        frequency="quarterly",
        synonyms=["output"],
        mappings={"fred": "GDP"},
    )
    first = _call(
        tools["record_quantitative_observation"],
        namespace="economic",
        metric_id=metric["metric_id"],
        period="2026-Q1",
        value="100",
        provider="fred",
        provider_series_id="GDP",
        vintage_id="v1",
        release_at_ms=100,
        retrieved_at_ms=110,
        valid_from_ms=100,
    )
    second = _call(
        tools["record_quantitative_observation"],
        namespace="economic",
        metric_id=metric["metric_id"],
        period="2026-Q1",
        value="101",
        provider="fred",
        provider_series_id="GDP",
        vintage_id="v2",
        release_at_ms=120,
        retrieved_at_ms=130,
        valid_from_ms=300,
        revision_of=first["observation_id"],
    )
    _call(
        tools["record_quantitative_series_break"],
        namespace="economic",
        metric_id=metric["metric_id"],
        break_type="rebase",
        boundary_ms=200,
        before={"base": 2015},
        after={"base": 2025},
        evidence=[{"citation": "provider:methodology"}],
        confidence=1,
    )
    discovered = _call(
        tools["discover_quantitative_metrics"],
        namespace="economic",
        query="fred",
    )
    assert discovered["items"][0]["metric_id"] == metric["metric_id"]
    pinned = _call(
        tools["read_quantitative_series"],
        namespace="economic",
        metric_id=metric["metric_id"],
        as_of_ms=115,
    )
    assert [item["vintage_id"] for item in pinned["items"]] == ["v1"]
    assessment = _call(
        tools["assess_quantitative_comparability"],
        namespace="economic",
        left_observation_id=first["observation_id"],
        right_observation_id=second["observation_id"],
    )
    assert assessment["reasons"] == ["series-break:rebase"]
    conversion = _call(
        tools["convert_quantitative_value"],
        namespace="scientific",
        value="2",
        from_unit="km",
        to_unit="m",
    )
    assert conversion["result"]["value"] == "2000.000000"
    replay = _call(
        tools["replay_quantitative_calculation"],
        namespace="scientific",
        calculation_id=conversion["calculation_id"],
    )
    assert replay["deterministic"]


def test_quantitative_capabilities_advertise_contracts_and_features():
    capabilities = server.knowledge_engine_capabilities.fn()
    assert {
        "noesis-quantitative-metric-v1",
        "noesis-quantitative-observation-v1",
        "noesis-quantitative-calculation-v1",
        "noesis-quantitative-comparability-v1",
    } <= set(capabilities["contracts"])
    assert {
        "versioned-quantitative-semantics",
        "vintage-aware-observations",
        "reproducible-quantitative-transformations",
        "series-break-comparability",
    } <= set(capabilities["features"])
