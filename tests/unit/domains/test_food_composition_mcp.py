"""Food composition MCP entry points: registration, declared scopes, exclusions and answers (FC10, #2292)."""

from __future__ import annotations

import asyncio

import duckdb
import pytest

from src.kb.food_composition import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import food_composition_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.products import FOOD_TOOLS, FOOD_WRITES, PRODUCT_SCOPES

NAMESPACE = {"namespace:global:read", "namespace:global:write"}


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "food-mcp.duckdb")
    env = h.Env(duckdb.connect(path))
    assert env.run_notices()["status"] == "complete"
    assert env.run_food()["status"] == "complete"
    env.conn.close()
    state = {"principal": "analyst", "scopes": set(h.ALL)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_registered_with_mutability_scopes_and_exclusions(mcp_env):
    tools, _ = mcp_env
    assert FOOD_TOOLS <= set(tools)
    for name in FOOD_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in FOOD_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == PRODUCT_SCOPES[name], name
    for name in ("lookup_food_products", "food_composition_as_of", "food_label_history",
                 "food_composition_source_contracts"):
        text = " ".join(tools[name].description.lower().split())
        assert "no nutrition score" in text and ("diet advice" in text or "ranking" in text), name
    assert "no notice on record" in tools["food_composition_as_of"].description.lower()


def test_every_tool_works_holding_exactly_its_declared_scopes(mcp_env):
    tools, state = mcp_env

    def call(name, **kwargs):
        state["scopes"] = set(PRODUCT_SCOPES[name]) | NAMESPACE
        result = tools[name].fn(**kwargs)
        assert result.get("ok") is not False, (name, result)
        return result

    assert call("food_composition_source_contracts")["tables"]["ciqual"]["decision"] == "acquire"
    lookup = call("lookup_food_products", namespace="global", gtin="071000000208")
    assert {f["provider"] for f in lookup["foods"]} == {"open-food-facts", "fooddata-central"}
    proposed = call("propose_food_matches", namespace="global")
    assert proposed["conflicts"]
    assert call("list_food_identity_conflicts", namespace="global")["conflicts"]
    match = next(c for c in proposed["candidates"] if c["candidate_state"] == "proposed")
    reviewed = call("review_food_match", namespace="global", match_id=match["match_id"], decision="accepted",
                    reason="GTIN and brand agree")
    assert reviewed["accepted"]
    linked = call("link_food_notices", namespace="global")
    assert [(link["basis"], link["state"]) for link in linked["linked"]] == [("brand+designation", "cited")]
    answer = call("food_composition_as_of", namespace="global", gtin=h.KEBAB, as_of="2026-03-15")
    assert answer["notices"]["status"] == "notices on record" and not forbidden_keys(answer)
    assert call("food_label_history", namespace="global", gtin=h.KEBAB)["histories"][0]["revisions"]
    created = call("create_food_composition_monitor", namespace="global", request_key="k", gtins=[h.KEBAB])
    ran = call("run_food_composition_monitor", subscription_id=created["subscription_id"])
    assert {n["kind"] for n in ran["notifications"]} >= {"label_revision", "new_linked_notice"}
    assert call("poll_food_composition_monitor", subscription_id=created["subscription_id"])["events"]


def test_reads_without_the_products_scope_are_refused(mcp_env):
    tools, state = mcp_env
    state["scopes"] = NAMESPACE
    refused = tools["food_composition_as_of"].fn(namespace="global", gtin=h.KEBAB)
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
