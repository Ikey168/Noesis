"""Demographic MCP entry points: catalog registration, declared scopes and read-only answers (#2007)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.kb.demographics import forbidden_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import demographics_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.demographics import (
    DEMOGRAPHIC_SCOPES,
    DEMOGRAPHIC_TOOLS,
    DEMOGRAPHIC_WRITES,
)


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "demographics-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn)
    series_id = h.series_id(conn, series_code="demo_pjan")
    conn.close()
    state = {"principal": "alice", "scopes": set(h.REVIEW_SCOPES)}
    monkeypatch.setattr(
        server, "_context", lambda: (state["principal"], state["scopes"])
    )
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    return asyncio.run(server.mcp.get_tools()), state, series_id


def test_tools_are_registered_with_every_scope_they_always_read_and_write(mcp_env):
    tools, _, _ = mcp_env
    assert (
        DEMOGRAPHIC_TOOLS <= set(tools) and set(DEMOGRAPHIC_SCOPES) == DEMOGRAPHIC_TOOLS
    )
    for name in DEMOGRAPHIC_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in DEMOGRAPHIC_WRITES else "read"), name
        assert (
            _required_scopes("knowledge_engine_mcp", mutability, name)
            == DEMOGRAPHIC_SCOPES[name]
        )
    catalog = json.loads(
        (h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in DEMOGRAPHIC_TOOLS:
        assert by_name[name]["required_scopes"] == DEMOGRAPHIC_SCOPES[name]
    assert "conditional scope" in tools["assert_demographic_link"].description.lower()
    descriptor = json.loads(
        (h.ROOT / "packs/economics/providers/economics.demographics.json").read_text()
    )
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        assert name in DEMOGRAPHIC_TOOLS and name not in DEMOGRAPHIC_WRITES
        assert op["required_scopes"] == DEMOGRAPHIC_SCOPES[name]


def test_reads_work_with_exactly_the_declared_scopes_and_writes_are_scoped(mcp_env):
    tools, state, series_id = mcp_env
    state["scopes"] = {"knowledge:demographics:read", "namespace:global:read"}
    listed = tools["list_demographic_series"].fn(
        namespace="global", provider="eurostat"
    )
    assert len(listed["series"]) == 5
    inspected = tools["inspect_demographic_series"].fn(
        namespace="global", series_id=series_id
    )
    assert inspected["definition_history"] and inspected["links"] == []
    values = tools["demographic_series_values"].fn(
        namespace="global", series_id=series_id, as_of_ms=1
    )
    assert values["reason"] == "historical_vintage_unavailable"
    compared = tools["compare_demographic_publishers"].fn(
        namespace="global", concept="population_stock"
    )
    assert forbidden_keys(compared) == [] and compared["pairs"]
    assert tools["list_demographic_resolutions"].fn(namespace="global") == {
        "resolutions": []
    }
    assert tools["list_demographic_pins"].fn(namespace="global") == {"pins": []}
    denied = tools["import_demographic_figure_sheet"].fn(
        namespace="global", sheet=json.loads(h.body(h.BAMF_SHEET))
    )
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    state["scopes"] = {"knowledge:demographics:write", "namespace:global:write"}
    imported = tools["import_demographic_figure_sheet"].fn(
        namespace="global", sheet=json.loads(h.body(h.BAMF_SHEET))
    )
    assert imported["status"] == "applied"
    refused = tools["assert_demographic_link"].fn(
        namespace="global",
        subject={"series_id": series_id},
        target={"kind": "legal-work", "namespace": "legal", "id": "legal-work:x"},
        relation="referenced_in",
        locator="p. 1",
        statement="x",
    )
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    state["scopes"] = set()
    assert (
        tools["demographic_source_contracts"].fn()["contracts"]["bamf"][
            "access_decision"
        ]
        == "not-implemented"
    )
