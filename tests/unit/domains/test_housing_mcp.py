"""Housing MCP entry points: catalog registration, declared scopes and answers with exactly those scopes (#2015)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import housing_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.housing import (
    HOUSING_SCOPES,
    HOUSING_TOOLS,
    HOUSING_WRITES,
)


@pytest.fixture(scope="module")
def mcp_env(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("housing") / "housing-mcp.duckdb")
    conn = duckdb.connect(path)
    env = h.Env(conn=conn).world()
    place = env.address()
    from src.kb.subscriptions import SubscriptionStore

    SubscriptionStore(conn).commit_watermark(h.NS, 1, kind="ingestion", detail={"run": "fixture"})
    conn.close()
    state = {"principal": "alice", "scopes": set(h.REVIEW_SCOPES)}
    patch = pytest.MonkeyPatch()
    patch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    patch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    yield asyncio.run(server.mcp.get_tools()), state, place
    patch.undo()


def test_tools_are_registered_with_every_scope_they_always_read_and_write(mcp_env):
    tools, _, _ = mcp_env
    assert HOUSING_TOOLS <= set(tools) and set(HOUSING_SCOPES) == HOUSING_TOOLS
    for name in HOUSING_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in HOUSING_WRITES else "read"), name
        assert (
            _required_scopes("knowledge_engine_mcp", mutability, name)
            == HOUSING_SCOPES[name]
        )
    catalog = json.loads(
        (h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text()
    )
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in HOUSING_TOOLS:
        assert by_name[name]["required_scopes"] == HOUSING_SCOPES[name]
    assert "conditional scope" in tools["housing_dossier"].description.lower()
    assert "conditional scope" in tools["create_housing_monitor"].description.lower()
    descriptor = json.loads(
        (h.ROOT / "packs/geospatial/providers/geospatial.housing.json").read_text()
    )
    for op in descriptor["operations"]:
        name = op["tool"].split(".", 1)[1]
        assert name in HOUSING_TOOLS and op["required_scopes"] == HOUSING_SCOPES[name]
        assert (op["side_effect"] == "read-only") == (name not in HOUSING_WRITES)


def test_each_tool_works_with_exactly_its_declared_scopes(mcp_env):
    tools, state, place = mcp_env
    namespace = {f"namespace:{h.NS}:read"}
    state["scopes"] = set(HOUSING_SCOPES["list_housing_records"]) | namespace
    records = tools["list_housing_records"].fn(
        namespace=h.NS, record_type="land_value_revision"
    )
    assert len(records["records"]) == 3
    compared = tools["compare_land_value_revisions"].fn(
        namespace=h.NS, zone_id="1099001"
    )
    assert compared["valuation_dates"][0]["readings"][0]["value"] == "5400"
    edition = tools["housing_rent_index_edition"].fn(
        namespace=h.NS, edition_id="berliner-mietspiegel-2099"
    )
    assert len(edition["cells"]) == 5
    statistics = tools["housing_statistics"].fn(
        namespace=h.NS, scheme="berlin-bezirk", code="001"
    )
    assert len(statistics["statistics"]) == 4
    record_id = records["records"][0]["record_id"]
    assert (
        tools["inspect_housing_record"].fn(namespace=h.NS, record_id=record_id)[
            "current"
        ]
        is True
    )
    assert tools["list_housing_projection_outcomes"].fn(namespace=h.NS) == {
        "outcomes": []
    }
    assert tools["list_housing_links"].fn(namespace=h.NS) == {"links": []}
    denied = tools["housing_dossier"].fn(
        namespace=h.NS, as_of="2099-12-31", place_id=place["place_id"]
    )
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"
    state["scopes"] = set(HOUSING_SCOPES["housing_dossier"]) | namespace
    answer = tools["housing_dossier"].fn(
        namespace=h.NS, as_of="2099-12-31", place_id=place["place_id"]
    )
    assert (
        answer["status"] == "answered"
        and answer["land_value"]["zones"][0]["zone_id"] == "1099001"
    )
    replayed = tools["replay_housing_dossier"].fn(receipt=answer["receipt"])
    assert replayed["status"] == "reproduced"
    refused = tools["housing_dossier"].fn(
        namespace=h.NS,
        as_of="2099-12-31",
        place_id=place["place_id"],
        include_transit_feed="vbb",
    )
    assert (
        refused["ok"] is False
        and "knowledge:transit:read" in refused["error"]["message"]
    )
    state["scopes"] = (
        set(HOUSING_SCOPES["create_housing_monitor"])
        | namespace
        | {f"namespace:{h.NS}:read"}
    )
    created = tools["create_housing_monitor"].fn(
        namespace=h.NS, request_key="zone", selector={"zone_id": "1099001"}
    )
    assert created["subscription_id"]
    state["scopes"] = set(HOUSING_SCOPES["run_housing_monitor"]) | namespace
    ran = tools["run_housing_monitor"].fn(subscription_id=created["subscription_id"])
    assert ran["status"] == "evaluated" and [n["kind"] for n in ran["notifications"]] == [
        "new_land_value_publication"
    ]
    state["scopes"] = set(HOUSING_SCOPES["poll_housing_monitor"]) | namespace
    assert len(tools["poll_housing_monitor"].fn(subscription_id=created["subscription_id"])["events"]) == 1
    state["scopes"] = set(HOUSING_SCOPES["create_housing_monitor"]) | namespace
    geo = tools["create_housing_monitor"].fn(
        namespace=h.NS, request_key="place", selector={"place_id": place["place_id"]}
    )
    assert geo["ok"] is False and geo["error"]["code"] == "unauthorized"
    state["scopes"] = set()
    assert tools["housing_source_contracts"].fn()["contracts"]["boris-berlin"][
        "access_decision"
    ] == ("not-implemented")
