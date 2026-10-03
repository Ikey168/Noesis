"""Waste MCP entry points: catalog registration, declared scopes, exclusions, minimisation and answers (#2740, WC11)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.kb.waste_records import forbidden_paths, personal_data_paths
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import waste_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.waste import WASTE_SCOPES, WASTE_TOOLS, WASTE_WRITES


@pytest.fixture()
def mcp_env(tmp_path, monkeypatch):
    path = str(tmp_path / "waste-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn, revisions=True)
    h.register_places(conn)
    conn.close()
    state = {"principal": "alice", "scopes": set(h.SCOPES)}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_tools_are_registered_with_every_scope_they_always_read_and_write(mcp_env):
    tools, _ = mcp_env
    assert WASTE_TOOLS <= set(tools) and set(WASTE_SCOPES) == WASTE_TOOLS
    for name in WASTE_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in WASTE_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == WASTE_SCOPES[name]
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in WASTE_TOOLS:
        assert by_name[name]["required_scopes"] == WASTE_SCOPES[name]
    for name in ("waste_indicator_for_place", "waste_facility_transfers", "waste_series_history",
                 "waste_source_contracts", "link_waste_records", "export_waste_bundle"):
        description = " ".join(tools[name].description.lower().split())
        assert "no nowcasting" in description and "no blending of eurostat, oecd and eea figures" in description, name
        assert "no summing of facility transfers into national totals" in description, name


def test_reads_work_with_the_declared_scopes_answers_are_minimised_and_writes_are_scoped(mcp_env):
    tools, state = mcp_env
    state["scopes"] = set(h.SCOPES)
    places = tools["propose_waste_place_matches"].fn()
    assert places["assertions"] and {a["state"] for a in places["assertions"]} == {"proposed"}
    facilities = tools["propose_waste_facility_matches"].fn()
    assert facilities["unmatched_inspire_ids"] == [h.UNKNOWN_FACILITY]
    state["principal"] = h.REVIEWER
    for a in places["assertions"] + [a for a in facilities["assertions"] if a["state"] == "proposed"]:
        assert tools["review_waste_identity_match"].fn(assertion_id=a["assertion_id"], decision="accept",
                                                       reason="published code")["state"] == "accepted"
    state["scopes"] = set(h.READ_ONLY)
    answer = tools["waste_indicator_for_place"].fn(place={"scheme": "eurostat-geo", "code": "DE"},
                                                   as_of="2099-12-31")
    assert answer["status"] == "reported" and answer["side_by_side"] is True
    assert "blending Eurostat, OECD and EEA figures" in answer["exclusions"]
    assert forbidden_paths(answer) == [] and personal_data_paths(answer) == []
    assert {r["provider"] for r in answer["results"]} == {"eurostat-waste", "eurostat-circular-economy",
                                                          "oecd-municipal-waste"}
    denied = tools["waste_facility_transfers"].fn(facility=h.FACILITY_1)
    assert denied["ok"] is False and denied["error"]["code"] == "unauthorized"  # environment read is declared
    state["scopes"] = set(h.READ_ONLY) | {"knowledge:environment:read"}
    transfers = tools["waste_facility_transfers"].fn(facility=h.FACILITY_1)
    assert transfers["status"] == "reported" and transfers["facility_record"]["native_id"] == h.FACILITY_1
    assert personal_data_paths(transfers) == [] and transfers["totals"] is None
    bundle = tools["export_waste_bundle"].fn(place={"scheme": "eurostat-geo", "code": "FR"}, facility=h.FACILITY_2)
    assert bundle["items"] and all(i["record_revision"] and i["as_of"] for i in bundle["items"])
    readiness = tools["waste_readiness"].fn()
    assert readiness["links"]["chemicals.substances"]["status"] == "provider_absent"
    refused = tools["link_waste_records"].fn()
    assert refused["ok"] is False and refused["error"]["code"] == "unauthorized"
    state["scopes"] = set(h.SCOPES)
    linked = tools["link_waste_records"].fn()
    assert linked["chemicals"]["missing"] and linked["facilities"]["linked"]
    links = tools["list_waste_links"].fn(kind="chemicals")["links"]
    assert {link["state"] for link in links} == {"provider_absent"}
    state["scopes"] = set()
    contracts = tools["waste_source_contracts"].fn()
    assert contracts["live_verification"]["oecd-municipal-waste"]["status"] == "unverified-live"
    assert contracts["minimisation"]["decision"].startswith("published aggregates")


def test_an_answer_carrying_an_operator_field_is_refused():
    from src.kb.waste_records import WasteError
    from tools.knowledge_engine_mcp import waste

    with pytest.raises(WasteError) as caught:
        waste._declared({"facility": {"operator": "x"}})
    assert caught.value.code == "minimisation"
    with pytest.raises(WasteError):
        waste._declared({"rows": [{"national_total": "1"}]})
    assert waste._declared({"ok": True})["exclusions"]
