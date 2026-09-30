"""Offline place-to-water-records acceptance for the Climate and Environment pack (#2582, WA12 #2642).

The journey replays the pinned PEGELONLINE (two stations, gauge zero, raw
levels with a gap), USGS (provisional and approved daily values) and EEA WISE
(status per reporting cycle, a published geometry) fixtures through the
source-pack runtime with sockets blocked, and drives the MCP tools: reviewable
identity, cross-pack links, place and river answers, a value as of a date, a
place with no records, the export bundle and a monitor hearing a later
acquisition. Stations, codes and values are fictional, not live evidence.
"""

from __future__ import annotations

import asyncio
import json
import socket

import duckdb
import pytest

from src.kb.water_records import personal_keys
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.water import fixture_builder, harness
from tests.unit.water.harness import ALL, NS
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.water import WATER_TOOLS, WATER_WRITES

EXAMPLA = f"pegelonline:{fixture_builder.EXAMPLA}"
NORTHWIND = f"pegelonline:{fixture_builder.NORTHWIND}"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def _tools(monkeypatch, path, principal="alice"):
    state = {"principal": principal, "scopes": set(ALL) | {"knowledge:schema:register"}}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def _matches(proposed, method):
    return {m["subject_key"]: m for m in proposed["matches"] if m["method"] == method}


def test_place_to_gauging_stations_cited_observations_and_water_body_status_history(tmp_path, monkeypatch):
    path = str(tmp_path / "water.duckdb")
    env = harness.Env(duckdb.connect(path)).loaded()
    assert {r["evidence_origin"] for r in env.store.runs(NS)} == {"fixture"}
    places = env.places()
    env.seed_other_packs()
    env.conn.close()
    tools, state = _tools(monkeypatch, path)

    # Reviewable identity: proposals only, identifiers first; then a reviewer accepts.
    proposed = tools["propose_water_identity_matches"].fn(namespace=NS)
    assert proposed["matches"] and {m["state"] for m in proposed["matches"]} == {"proposed"}
    assert "wfd:DEFX_EXAMPLA_02" in proposed["unmatched"]  # no published geometry: stays unmatched
    state["principal"] = "bob"
    within = _matches(proposed, "published-coordinates-within")
    rivers = _matches(proposed, "published-river-identifier")
    geometry = _matches(proposed, "published-geometry-within")
    for match in (within[EXAMPLA], rivers[EXAMPLA], rivers[NORTHWIND], geometry["wfd:DEFX_EXAMPLA_01"]):
        reviewed = tools["review_water_identity_match"].fn(namespace=NS, match_id=match["match_id"],
                                                           decision="accept", reason="published identifier or "
                                                                                     "published location")
        assert reviewed["state"] == "accepted" and reviewed["reviewer"] == "bob"
    rejected = tools["review_water_identity_match"].fn(namespace=NS, match_id=within["usgs:USGS-99990002"]["match_id"],
                                                       decision="reject", reason="outside the journey's scope")
    assert rejected["state"] == "rejected"

    # Cross-pack links by citation, shared identifier and accepted match; missing targets reported.
    linked = tools["link_water_records"].fn(namespace=NS)
    assert linked["created"]["citation"] == 1 and linked["created"]["shared_identifier"] == 2
    assert linked["created"]["accepted_match"] == 4
    assert linked["unavailable"]["dams-and-waterways"]["status"] == "unavailable"
    flood = next(link for link in linked["links"] if link["target_kind"] == "hazard")
    assert flood["subject_key"] == EXAMPLA and flood["target_revision"] and flood["revision_id"]

    # Place to its stations and water bodies, with the geometry version and status per reporting cycle.
    place = tools["water_for_place"].fn(namespace=NS, place_id=places["exampla"])
    (station,) = place["stations"]
    assert station["subject_key"] == EXAMPLA and station["membership"]["basis"] == "accepted identity match"
    assert station["membership"]["geometry_id"] and station["membership"]["reviewer"] == "bob"
    assert station["latest"]["water_level"]["quality"] == "provisional"
    assert station["latest"]["discharge"]["citation"]["revision_id"]
    (body,) = place["water_bodies"]
    assert [(c["cycle_year"], c["ecological"]["label"]) for c in body["status_history"]] == [
        ("2016", "Moderate"), ("2022", "Poor")]
    on_river = tools["water_for_place"].fn(namespace=NS, place_id=places["nordfluss"])
    assert {s["subject_key"] for s in on_river["stations"]} == {EXAMPLA, NORTHWIND}
    within_place = tools["water_for_place"].fn(namespace=NS, place_id=places["exampla"], river=places["nordfluss"])
    assert [s["subject_key"] for s in within_place["stations"]] == [EXAMPLA]

    # Level at a station and time, with gauge zero, quality and the cited revision; a gap stays missing.
    level = tools["water_value_at"].fn(namespace=NS, station="59990001", parameter="water_level",
                                       time="2026-09-20T01:30:00+02:00")
    assert level["value"] == 541.0 and level["quality"]["state"] == "provisional"
    assert level["gauge_zero"]["valid_from"] == "2019-11-01" and level["citation"]["revision_no"] == 1
    gap = tools["water_value_at"].fn(namespace=NS, station="EXAMPLA", parameter="W",
                                     time="2026-09-20T00:45:00+02:00")
    assert gap["status"] == "no value published for this time" and gap["value"] is None

    # A subject with no records.
    empty = tools["water_for_place"].fn(namespace=NS, place_id=places["moor"])
    assert empty["status"] == "no station or water body on record for this place"
    unknown = tools["lookup_water_station"].fn(namespace=NS, station="No such gauge")
    assert unknown["status"] == "no station on record"

    # Export: every item cites source, record revision and retrieval time.
    exported = tools["export_water_bundle"].fn(namespace=NS, place_id=places["exampla"])
    assert exported["citations"] and all(i["source_url"].startswith("https://") and i["revision_id"]
                                         and i["retrieved_at"] for i in exported["citations"])
    assert {i["kind"] for i in exported["citations"]} == {"station", "observation", "water_body", "assessment"}

    # A monitor hears a later acquisition; an as-of answer still returns what was on record before it.
    monitor = tools["create_water_monitor"].fn(namespace=NS, request_key="creek-and-river",
                                               stations=["USGS-99990001"], rivers=[places["nordfluss"]],
                                               water_bodies=["DEFX_NORTHWIND_03"])
    baseline = tools["run_water_monitor"].fn(namespace=NS, subscription_id=monitor["subscription_id"])
    assert baseline["baseline"] and {n["kind"] for n in baseline["notifications"]} == {
        "observation_above_threshold"}
    later = harness.Env(duckdb.connect(path))
    later.advance(8)
    assert later.run("water-later", later=True)["status"] == "complete"
    later.conn.close()
    heard = tools["run_water_monitor"].fn(namespace=NS, subscription_id=monitor["subscription_id"])
    kinds = {n["kind"] for n in heard["notifications"]}
    assert {"observation_revised", "observation_removed", "station_revised", "assessment_new_cycle",
            "observation_above_threshold"} <= kinds
    assert all(n["new"]["revision_id"] and n["what_changed"] for n in heard["notifications"])
    assert tools["run_water_monitor"].fn(namespace=NS, subscription_id=monitor["subscription_id"])[
        "notifications"] == []
    before = tools["water_value_at"].fn(namespace=NS, station="USGS-99990001", parameter="discharge",
                                        time="2026-09-04", as_of="2026-09-25")
    assert before["value"] == 14.1 and before["quality"]["state"] == "provisional"
    assert [r["quality"]["state"] for r in before["later_revisions"]] == ["approved"]
    after = tools["water_value_at"].fn(namespace=NS, station="USGS-99990001", parameter="discharge",
                                       time="2026-09-04")
    assert after["value"] == 14.0 and after["quality"]["state"] == "approved"
    history = tools["water_body_status_history"].fn(namespace=NS, water_body="DEFX_EXAMPLA_01")
    assert [c["cycle_year"] for c in history["cycles"]] == ["2016", "2022"] and "not merged" in history["notice"]

    # Exclusions and minimisation: no forecast, gap-filled, assessed or risk field and no personal data anywhere.
    for answer in (place, on_river, level, gap, exported, heard, before, after, history):
        plain = json.loads(json.dumps(answer))
        assert not harness.forbidden_keys(plain) and not personal_keys(plain)
        assert "error" not in plain


def test_tools_are_declared_with_environment_scopes_and_live_state_is_separate(tmp_path, monkeypatch):
    for name in WATER_TOOLS:
        assert _mutability(name) == ("write" if name in WATER_WRITES else "read"), name
    assert _required_scopes("knowledge_engine_mcp", "read", "water_source_contracts") == []
    assert _required_scopes("knowledge_engine_mcp", "write", "review_water_identity_match")[0] == \
        "knowledge:environment:review"
    catalog = json.loads((harness.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    assert {t["name"] for t in catalog["tools"] if t["name"] in WATER_TOOLS} == WATER_TOOLS
    path = str(tmp_path / "w.duckdb")
    harness.Env(duckdb.connect(path)).loaded().conn.close()
    tools, _ = _tools(monkeypatch, path)
    contracts = tools["water_source_contracts"].fn()
    assert contracts["not_implemented"]["grdc"]["status"] == "not-implemented"
    assert {v["status"] for k, v in contracts["live_verification"].items() if k != "grdc"} == {"unverified-live"}
    assert contracts["minimisation"]["decision"] == "no-personal-data"
    ready = tools["water_readiness"].fn(namespace=NS)
    assert ready["features"] == {"water-pegelonline": False, "water-usgs": False, "water-eea-wise": False}
    assert {p["state"]["last_evidence_origin"] for p in ready["providers"].values()} == {"fixture"}
    description = tools["water_value_at"].description
    assert "interpolated" in description and "provisional" in description
