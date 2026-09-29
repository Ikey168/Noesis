"""Products expansion MCP entry points: registration, declared scopes, category arguments and not_ready (PX10, #2102)."""

from __future__ import annotations

import asyncio

import duckdb
import pytest

from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.domains import test_products_expansion_sources as expansion
from tests.unit.domains import test_products_pack as displays
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.products import (
    EXPANSION_READS,
    EXPANSION_TOOLS,
    EXPANSION_WRITES,
    PRODUCT_SCOPES,
    PRODUCT_TOOLS,
    PRODUCT_WRITES,
    QUERY_EXAMPLES,
)

NAMESPACE = {"namespace:global:read", "namespace:global:write"}
CATEGORY_TOOLS = {
    "lookup_product_models",
    "propose_product_matches",
    "compare_product_models",
    "lookup_component",
}


def _connect(monkeypatch, path, state):
    monkeypatch.setattr(
        server, "_context", lambda: (state["principal"], state["scopes"])
    )
    monkeypatch.setattr(
        server, "_intake_context", lambda: (state["principal"], state["scopes"])
    )
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    return asyncio.run(server.mcp.get_tools())


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "products-expansion-mcp.duckdb")
    conn = duckdb.connect(path)
    value, runtime = displays.install(conn)
    expansion.run(runtime, value, "appliances", expansion.APPLIANCES, "models")
    expansion.run(runtime, value, "components", expansion.COMPONENTS, "components")
    conn.execute(
        "CREATE TABLE canonical_entities (canonical_id TEXT PRIMARY KEY, preferred_name TEXT, "
        "entity_type TEXT)"
    )
    conn.execute(
        "INSERT INTO canonical_entities VALUES ('ent:capatronic', 'Capatronic GmbH', 'ORG')"
    )
    from src.kb.products import ProductStore

    store = ProductStore(conn)
    ids = {
        "wm8": expansion.variant(
            store, "icecat", "WM-8E14", "household washing machines"
        )["model_id"],
        "wm9": expansion.variant(
            store, "icecat", "WM-9E16", "household washing machines"
        )["model_id"],
        "link": store.documents(
            "global",
            expansion.variant(store, "bmecat:capatronic", "CX0603X7R104K500")[
                "variant_id"
            ],
        )[0]["link_id"],
    }
    conn.close()
    state = {"principal": "analyst", "scopes": set()}
    return _connect(monkeypatch, path, state), state, ids


def test_tools_are_registered_with_mutability_scopes_and_query_examples(mcp_env):
    tools, _, _ = mcp_env
    assert EXPANSION_TOOLS <= PRODUCT_TOOLS <= set(tools)
    for name in PRODUCT_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in PRODUCT_WRITES else "read"), name
        assert (
            _required_scopes("knowledge_engine_mcp", mutability, name)
            == PRODUCT_SCOPES[name]
        ), name
    for name in CATEGORY_TOOLS:
        assert "category" in tools[name].parameters["properties"], name
    assert set(QUERY_EXAMPLES) == CATEGORY_TOOLS
    assert all(
        example["semantics"] and example["arguments"]["namespace"]
        for example in QUERY_EXAMPLES.values()
    )
    assert "no cross-reference" in tools["lookup_component"].description.lower()


def test_every_tool_works_holding_exactly_its_declared_scopes(mcp_env):
    tools, state, ids = mcp_env
    calls = {
        "product_category_registry": {"category": "refrigerating appliances"},
        "product_provider_contracts": {},
        "lookup_product_models": {
            "namespace": "global",
            "designation": "WM-8E14",
            "category": "household washing machines",
        },
        "propose_product_matches": {
            "namespace": "global",
            "category": "household washing machines",
        },
        "compare_product_models": {
            "namespace": "global",
            "model_ids": [ids["wm8"], ids["wm9"]],
            "category": "household-washing-machines",
        },
        "lookup_component": {
            "namespace": "global",
            "mpn": "CX0603X7R104K500",
            "manufacturer": "Capatronic",
        },
        "cite_product_document": {
            "namespace": "global",
            "link_id": ids["link"],
            "section": "Dimensions",
        },
        "review_component_manufacturer_link": {
            "namespace": "global",
            "manufacturer": "Capatronic",
            "entity_id": "ent:capatronic",
            "decision": "match",
            "reason": "catalogue header names the company",
        },
        "products_readiness": {},
    }
    results = {}
    for name, arguments in calls.items():
        state["scopes"] = set(PRODUCT_SCOPES[name]) | NAMESPACE
        results[name] = tools[name].fn(**arguments)
        assert results[name].get("ok") is not False, (name, results[name])
    assert list(results["product_category_registry"]["categories"]) == [
        "refrigerating-appliances"
    ]
    assert (
        results["product_provider_contracts"]["component_sources"]["digikey"][
            "decision"
        ]
        == "link-only"
    )
    assert [v["provider"] for v in results["lookup_product_models"]["variants"]] == [
        "eprel",
        "icecat",
    ]
    assert results["compare_product_models"]["category"] == "household-washing-machines"
    assert results["lookup_component"]["count"] == 1
    assert results["products_readiness"]["features"] == {
        "safety": False,
        "appliances": False,
        "components": False,
    }
    name = "revert_component_manufacturer_link"
    state["scopes"] = set(PRODUCT_SCOPES[name]) | NAMESPACE
    reverted = tools[name].fn(
        namespace="global",
        link_id=results["review_component_manufacturer_link"]["link_id"],
    )
    assert reverted.get("status") == "reverted", reverted
    for name in ("review_component_manufacturer_link", "lookup_component"):
        for missing in PRODUCT_SCOPES[name]:
            state["scopes"] = (set(PRODUCT_SCOPES[name]) - {missing}) | NAMESPACE
            result = tools[name].fn(**calls[name])
            assert (
                result.get("ok") is False and result["error"]["code"] == "unauthorized"
            ), (name, missing)


def test_category_arguments_refuse_unknown_and_mixed_categories(mcp_env):
    tools, state, ids = mcp_env
    state["scopes"] = {"knowledge:products:read"} | NAMESPACE
    unknown = tools["lookup_product_models"].fn(namespace="global", category="toasters")
    assert unknown["ok"] is False and unknown["error"]["code"] == "unknown_category"
    mismatch = tools["compare_product_models"].fn(
        namespace="global",
        model_ids=[ids["wm8"], ids["wm9"]],
        category="refrigerating appliances",
    )
    assert mismatch["ok"] is False and mismatch["error"]["code"] == "category_mismatch"
    not_component = tools["lookup_component"].fn(
        namespace="global", mpn="WM-8E14", category="household washing machines"
    )
    assert (
        not_component["ok"] is False
        and not_component["error"]["code"] == "unknown_category"
    )
    registry = tools["product_category_registry"].fn(category="toasters")
    assert registry["ok"] is False and registry["error"]["code"] == "unknown_category"


def test_reads_before_any_source_ran_are_not_ready(tmp_path, monkeypatch):
    path = str(tmp_path / "empty.duckdb")
    duckdb.connect(path).close()
    state = {"principal": "analyst", "scopes": {"knowledge:products:read"} | NAMESPACE}
    tools = _connect(monkeypatch, path, state)
    for name, arguments in (
        ("lookup_product_models", {"namespace": "global"}),
        (
            "inspect_product_identity",
            {"namespace": "global", "identity_id": "product-model:x"},
        ),
        ("compare_product_models", {"namespace": "global", "model_ids": ["a", "b"]}),
        ("lookup_component", {"namespace": "global", "mpn": "CX0603"}),
        (
            "cite_product_document",
            {"namespace": "global", "link_id": "product-document:x"},
        ),
        ("product_selection_outcomes", {"namespace": "global", "run_id": "r"}),
    ):
        result = tools[name].fn(**arguments)
        assert result["ok"] is False and result["error"]["code"] == "not_ready", (
            name,
            result,
        )
    assert EXPANSION_READS.isdisjoint(EXPANSION_WRITES)
