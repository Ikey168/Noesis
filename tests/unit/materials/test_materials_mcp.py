"""Materials MCP tools: catalog registration, exact declared scopes and not-ready reads (MT12, #2090)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.kb.materials_store import record_key
from src.kb.subscriptions import SubscriptionStore
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.materials import harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.materials import (
    MATERIALS_SCOPES,
    MATERIALS_TOOLS,
    MATERIALS_WRITES,
)

NS_READ, NS_WRITE = f"namespace:{h.NS}:read", f"namespace:{h.NS}:write"
MP1 = record_key("materials-project", "mp-990001")


def _server(monkeypatch, path):
    state = {"principal": "alice", "scopes": set()}
    monkeypatch.setattr(
        server, "_context", lambda: (state["principal"], state["scopes"])
    )
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    return asyncio.run(server.mcp.get_tools()), state


@pytest.fixture()
def loaded(tmp_path, monkeypatch):
    path = str(tmp_path / "materials.duckdb")
    env = h.Env(duckdb.connect(path)).load()
    SubscriptionStore(env.conn).commit_watermark(h.NS, 1, kind="ingestion")
    env.conn.close()
    return _server(monkeypatch, path)


def call(tools, state, name, scopes, **kwargs):
    state["scopes"] = set(scopes)
    return tools[name].fn(**kwargs)


def test_tools_are_registered_in_the_catalog_with_every_scope_they_always_use(loaded):
    tools, _ = loaded
    assert MATERIALS_TOOLS <= set(tools) and set(MATERIALS_SCOPES) == MATERIALS_TOOLS
    catalog = json.loads(
        (h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in MATERIALS_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in MATERIALS_WRITES else "read"), name
        assert (
            _required_scopes("knowledge_engine_mcp", mutability, name)
            == MATERIALS_SCOPES[name]
        )
        assert by_name[name]["required_scopes"] == MATERIALS_SCOPES[name]
    assert "conditional scope" in tools["material_properties"].description.lower()


READS = {
    "materials_source_contracts": {},
    "materials_bundle_status": {"namespace": h.NS},
    "lookup_material": {"namespace": h.NS, "query": "TiO2"},
    "material_properties": {"namespace": h.NS, "material": MP1},
    "compare_material_property": {
        "namespace": h.NS,
        "material": MP1,
        "property": "band_gap",
    },
    "search_materials_by_property": {
        "namespace": h.NS,
        "ranges": [{"property": "band_gap", "min": "1.7"}],
    },
    "material_release_changes": {"namespace": h.NS, "provider": "materials-project"},
}


def test_reads_work_with_exactly_their_declared_scopes_and_fail_without_them(loaded):
    tools, state = loaded
    for name, kwargs in READS.items():
        result = call(tools, state, name, {*MATERIALS_SCOPES[name], NS_READ}, **kwargs)
        assert result.get("ok") is not False, (name, result)
        for missing in MATERIALS_SCOPES[name]:
            denied = call(
                tools,
                state,
                name,
                {*MATERIALS_SCOPES[name], NS_READ} - {missing},
                **kwargs,
            )
            assert (
                denied.get("ok") is False and denied["error"]["code"] == "unauthorized"
            ), (name, missing)
    optional = call(
        tools,
        state,
        "material_properties",
        {*MATERIALS_SCOPES["material_properties"], NS_READ},
        namespace=h.NS,
        material=MP1,
        standards_namespace="global",
    )
    assert (
        optional["ok"] is False and optional["error"]["code"] == "unauthorized"
    )  # checked at call time
    granted = call(
        tools,
        state,
        "material_properties",
        {
            *MATERIALS_SCOPES["material_properties"],
            NS_READ,
            "knowledge:standards:read",
            "namespace:global:read",
        },
        namespace=h.NS,
        material=MP1,
        standards_namespace="global",
    )
    assert granted.get("ok") is not False, granted


def test_writes_work_with_exactly_their_declared_scopes(loaded):
    tools, state = loaded

    def write(name, **kwargs):
        full = {*MATERIALS_SCOPES[name], NS_READ, NS_WRITE}
        for missing in MATERIALS_SCOPES[name]:
            denied = call(tools, state, name, full - {missing}, **kwargs)
            assert (
                denied.get("ok") is False and denied["error"]["code"] == "unauthorized"
            ), (name, missing)
        result = call(tools, state, name, full, **kwargs)
        assert result.get("ok") is not False, (name, result)
        return result

    proposed = write("propose_material_matches", namespace=h.NS)
    candidate = next(
        c for c in proposed["candidates"] if MP1 in (c["left_key"], c["right_key"])
    )
    state["principal"] = "bob"
    reviewed = write(
        "review_material_match",
        namespace=h.NS,
        candidate_id=candidate["candidate_id"],
        decision="accept",
        reason="stated cross-reference",
    )
    assert reviewed["state"] == "accepted"
    reverted = write(
        "revert_material_match",
        namespace=h.NS,
        candidate_id=candidate["candidate_id"],
        reason="changed my mind",
    )
    assert reverted["state"] == "reverted"
    state["principal"] = "alice"
    created = write(
        "create_material_watch", namespace=h.NS, request_key="w1", property="band_gap"
    )
    run = write(
        "run_material_watch", namespace=h.NS, subscription_id=created["subscription_id"]
    )
    assert run["notifications"]
    polled = call(
        tools,
        state,
        "poll_material_watch",
        {*MATERIALS_SCOPES["poll_material_watch"], NS_READ},
        namespace=h.NS,
        subscription_id=created["subscription_id"],
    )
    assert polled.get("ok") is not False and polled["events"], polled
    schemas = call(
        tools,
        state,
        "register_material_schemas",
        {*MATERIALS_SCOPES["register_material_schemas"]},
        namespace=h.NS,
    )
    assert len(schemas["modules"]) == 2


def test_every_entry_point_before_any_source_ran_is_not_ready(tmp_path, monkeypatch):
    path = str(tmp_path / "empty.duckdb")
    duckdb.connect(path).close()
    tools, state = _server(monkeypatch, path)
    requests = {
        **{
            k: v
            for k, v in READS.items()
            if k not in {"materials_source_contracts", "materials_bundle_status"}
        },
        "poll_material_watch": {"namespace": h.NS, "subscription_id": "subscription:x"},
        "propose_material_matches": {"namespace": h.NS},
        "create_material_watch": {
            "namespace": h.NS,
            "request_key": "w",
            "property": "band_gap",
        },
    }
    for name, kwargs in requests.items():
        result = call(
            tools, state, name, {*MATERIALS_SCOPES[name], NS_READ, NS_WRITE}, **kwargs
        )
        assert result.get("ok") is False and result["error"]["code"] == "not_ready", (
            name,
            result,
        )
    status = call(
        tools,
        state,
        "materials_bundle_status",
        {*MATERIALS_SCOPES["materials_bundle_status"], NS_READ},
        namespace=h.NS,
    )
    assert status["stores_ready"] is False
    assert {p["status"] for p in status["providers"].values()} == {"unavailable"}
