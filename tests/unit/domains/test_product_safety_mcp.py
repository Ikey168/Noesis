"""Products safety MCP entry points: registration, declared scopes, read-only answers (R08/R10, #2011 #2026)."""

from __future__ import annotations

import asyncio

import duckdb
import pytest

from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import product_safety_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.products import (
    PRODUCT_SCOPES,
    SAFETY_READS,
    SAFETY_TOOLS,
    SAFETY_WRITES,
)

NAMESPACE = {"namespace:global:read", "namespace:global:write"}


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "product-safety-mcp.duckdb")
    env = h.Env(duckdb.connect(path)).loaded()
    ids = {
        "model": env.model("icecat", "EX-32U8"),
        "notice": env.notice("safety-gate", "SR/00417/26"),
    }
    ids["match"] = env.candidate(ids["notice"], ids["model"])["match_id"]
    env.conn.execute(
        "CREATE TABLE canonical_entities (canonical_id TEXT PRIMARY KEY, preferred_name TEXT, "
        "entity_type TEXT)"
    )
    env.conn.execute(
        "INSERT INTO canonical_entities VALUES ('ent:brightway', 'Brightway Home Ltd', 'ORG')"
    )
    env.conn.close()
    state = {"principal": "analyst", "scopes": set(h.ALL)}
    monkeypatch.setattr(
        server, "_context", lambda: (state["principal"], state["scopes"])
    )
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    return asyncio.run(server.mcp.get_tools()), state, ids


def test_tools_are_registered_with_mutability_scopes_and_boundaries(mcp_env):
    tools, _, _ = mcp_env
    assert SAFETY_TOOLS <= set(tools)
    for name in SAFETY_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in SAFETY_WRITES else "read"), name
        assert (
            _required_scopes("knowledge_engine_mcp", mutability, name)
            == PRODUCT_SCOPES[name]
        ), name
    text = tools["lookup_product_notices"].description.lower()
    assert "no notice on record" in text and "never a safety verdict" in text


def test_every_tool_works_holding_exactly_its_declared_scopes(mcp_env):
    tools, state, ids = mcp_env
    calls = {
        "product_safety_source_contracts": {},
        "lookup_product_notices": {"namespace": "global", "gtin": "012345678905"},
        "inspect_product_notice": {
            "namespace": "global",
            "notice": "safety-gate:SR/00417/26",
        },
        "propose_product_notice_matches": {"namespace": "global"},
        "review_product_notice_match": {
            "namespace": "global",
            "match_id": ids["match"],
            "decision": "accepted",
            "reason": "brand and model agree",
        },
        "link_product_notice_citations": {"namespace": "global"},
        "propose_product_notice_party_links": {"namespace": "global"},
        "create_product_notice_monitor": {
            "namespace": "global",
            "request_key": "exact",
            "models": [ids["model"]],
        },
    }
    results = {}
    for name, arguments in calls.items():
        state["scopes"] = set(PRODUCT_SCOPES[name]) | NAMESPACE
        results[name] = tools[name].fn(**arguments)
        assert results[name].get("ok") is not False, (name, results[name])
    subscription = results["create_product_notice_monitor"]["subscription_id"]
    for name, arguments in (
        ("run_product_notice_monitor", {"subscription_id": subscription}),
        ("poll_product_notice_monitor", {"subscription_id": subscription}),
    ):
        state["scopes"] = set(PRODUCT_SCOPES[name]) | NAMESPACE
        result = tools[name].fn(**arguments)
        assert result.get("ok") is not False, (name, result)
    assert results["lookup_product_notices"]["status"] == "notices on record"
    # An optional argument's extra scope is checked at call time.
    state["scopes"] = set(PRODUCT_SCOPES["lookup_product_notices"]) | NAMESPACE
    refused = tools["lookup_product_notices"].fn(
        namespace="global", gtin="012345678905", include_news=True
    )
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    for name in ("link_product_notice_citations", "run_product_notice_monitor"):
        for missing in PRODUCT_SCOPES[name]:
            state["scopes"] = (set(PRODUCT_SCOPES[name]) - {missing}) | NAMESPACE
            arguments = (
                {"namespace": "global"}
                if name.startswith("link")
                else {"subscription_id": subscription}
            )
            result = tools[name].fn(**arguments)
            assert result.get("ok") is False, (name, missing, result)


def test_party_link_tools_need_their_entity_history_scopes(mcp_env):
    tools, state, _ = mcp_env
    state["scopes"] = (
        set(PRODUCT_SCOPES["review_product_notice_party_link"]) | NAMESPACE
    )
    decided = tools["review_product_notice_party_link"].fn(
        namespace="global",
        party_name="Brightway Home Ltd.",
        entity_id="ent:brightway",
        decision="match",
        reason="same company",
    )
    assert decided.get("ok") is not False and decided["merged"] is False, decided
    unknown = tools["review_product_notice_party_link"].fn(
        namespace="global",
        party_name="Brightway Home Ltd.",
        entity_id="lei:5493000EXAMPLE000001",
        decision="match",
        reason="not acquired",
    )
    assert unknown["ok"] is False and unknown["error"]["code"] == "not_found"
    state["scopes"] = (
        set(PRODUCT_SCOPES["review_product_notice_party_link"]) | NAMESPACE
    )
    refused = tools["revert_product_notice_party_link"].fn(
        namespace="global", link_id=decided["link_id"]
    )
    assert refused["ok"] is False
    state["scopes"] = (
        set(PRODUCT_SCOPES["revert_product_notice_party_link"]) | NAMESPACE
    )
    reverted = tools["revert_product_notice_party_link"].fn(
        namespace="global", link_id=decided["link_id"]
    )
    assert reverted.get("status") == "reverted", reverted


def test_reads_run_on_read_only_connections_and_revoked_scope_is_refused(mcp_env):
    tools, state, ids = mcp_env
    state["scopes"] = {"knowledge:products:read", "namespace:global:read"}
    for name in SAFETY_READS - {"poll_product_notice_monitor"}:
        assert name in tools
    answer = tools["lookup_product_notices"].fn(
        namespace="global", model_id=ids["model"], as_of="2026-09-01"
    )
    assert answer["status"] == "no notice on record" and not h.forbidden_keys(answer)
    notice = tools["inspect_product_notice"].fn(
        namespace="global", notice=ids["notice"]
    )
    assert notice["notice_number"] == "SR/00417/26" and not h.forbidden_keys(notice)
    readiness = tools["products_readiness"].fn()
    assert set(readiness["notice_providers"]) == {
        "safety-gate",
        "cpsc",
        "nhtsa",
        "rasff",
        "baua",
        "gpsr",
    }
    assert (
        readiness["notice_providers"]["cpsc"]["live_verification"] == "unverified-live"
    )
    assert readiness["notice_providers"]["baua"]["live"] == "not-implemented"
    assert readiness["safety_feature_enabled"] is False
    state["scopes"] = {"namespace:global:read"}
    refused = tools["lookup_product_notices"].fn(
        namespace="global", gtin="012345678905"
    )
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    state["scopes"] = {"knowledge:products:read", "namespace:global:read"}
    write = tools["propose_product_notice_matches"].fn(namespace="global")
    assert write["ok"] is False and write["error"]["code"] == "unauthorized"
