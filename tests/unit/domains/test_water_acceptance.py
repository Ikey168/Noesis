"""Offline place-to-water-records acceptance for the Climate and Environment pack (#2582, WA12 #2642).

The journey replays the pinned PEGELONLINE (stations with gauge zero and
characteristic values, raw measurement windows with a missing value), USGS
(monitoring location with vertical datum, provisional values later approved)
and EEA WISE (WFD status per reporting cycle and published water-body
geometries) fixtures through the source-pack runtime with sockets blocked, and
drives the MCP tools: reviewable identity, cross-pack links, as-of answers,
place answers with status history, a subscription and the evidence bundle.
Identifiers and values are illustrative, not live evidence.
"""

from __future__ import annotations

import asyncio
import json
import socket

import duckdb
import pytest

from src.kb.water_store import iso
from tests.unit.water import fixture_builder
from tests.unit.water import harness as h
from tools.knowledge_engine_mcp import server

NS = h.NS


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def _tools(monkeypatch, path, principal="alice"):
    state = {"principal": principal, "scopes": set(h.ALL) | {"knowledge:schema:register"}}
    monkeypatch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    monkeypatch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    return asyncio.run(server.mcp.get_tools()), state


def test_place_to_gauging_stations_cited_observations_and_water_body_status_history(tmp_path, monkeypatch):
    path = str(tmp_path / "water.duckdb")
    env = h.Env(duckdb.connect(path)).loaded()
    assert {r["evidence_origin"] for r in env.store.runs(NS)} == {"fixture"}
    first = env.clock
    places = env.places()
    h.seed_hazard(env.conn, cited="990001")
    h.seed_infrastructure_asset(env.conn, scheme="eu-water-body", value=fixture_builder.ELBE_WB)
    env.conn.close()
    tools, state = _tools(monkeypatch, path)

    # Reviewable identity: proposals only, identifiers before names, accepted by a reviewer; unmatched stay visible.
    proposed = tools["propose_water_identity_matches"].fn(namespace=NS)
    assert proposed["proposed"] and {m["state"] for m in proposed["matches"]} == {"proposed"}
    assert [u["subject_key"] for u in proposed["unmatched"]] == ["eu-wb:" + fixture_builder.WEISSERITZ_WB]
    station_key = "pegelonline:" + fixture_builder.DRESDEN
    in_dresden = next(m for m in proposed["matches"] if m["subject_key"] == station_key
                      and m["place_id"] == places["dresden"])
    on_elbe = next(m for m in proposed["matches"] if m["subject_key"] == station_key
                   and m["place_id"] == places["elbe"])
    body = next(m for m in proposed["matches"] if m["subject_key"] == "eu-wb:" + fixture_builder.ELBE_WB)
    assert on_elbe["method"] == "published-identifier" and body["method"] == "published-geometry"
    state["principal"] = "bob"
    for match in (in_dresden, on_elbe, body):
        reviewed = tools["review_water_identity_match"].fn(namespace=NS, match_id=match["match_id"],
                                                           decision="accept", reason="published evidence checked")
        assert reviewed["state"] == "accepted" and reviewed["reviewer"] == "bob"
    state["principal"] = "alice"

    # Cross-pack links by citation, shared identifier and accepted match, each pinned to revisions.
    linked = tools["link_water_records"].fn(namespace=NS)
    bases = {(link["target"]["kind"], link["basis"]) for link in linked["links"]}
    assert {("hazard-record", "citation"), ("infrastructure-asset", "shared_identifier"),
            ("place", "accepted_match")} <= bases
    assert all(link["record_revision_id"] and link["target"]["revision"] for link in linked["links"])
    assert linked["unavailable_providers"]["weather"]["status"] == "unavailable"

    # Place to stations and water bodies with the geometry version and status history per cycle.
    place = tools["water_for_place"].fn(namespace=NS, place_id=places["dresden"])
    assert [s["subject_key"] for s in place["stations"]] == [station_key]
    assert {b["basis"] for b in place["stations"][0]["bases"]} == {"accepted_match", "geospatial-containment"}
    assert place["place"]["geometry_version"]["geometry_id"]
    (elbe,) = place["water_bodies"]
    assert [c["cycle"] for c in elbe["history"]] == ["2016", "2022"] and elbe["latest"]["ecological_status"] == "Poor"
    assert all(c["citation"]["revision_id"] for c in elbe["history"])
    empty = tools["water_for_place"].fn(namespace=NS, place_id=places["nowhere"])
    assert empty["status"] == "no station or water body on record for this place"

    # A later acquisition: approval, a correction, a datum change, a withdrawal and a new cycle.
    monitor = tools["create_water_monitor"].fn(namespace=NS, request_key="elbe-dresden",
                                               rivers=[places["elbe"]], stations=["USGS-01646500"],
                                               water_bodies=[fixture_builder.WEISSERITZ_WB])
    baseline = tools["run_water_monitor"].fn(namespace=NS, subscription_id=monitor["subscription_id"])
    assert baseline["baseline"]
    later = h.Env(duckdb.connect(path))
    later.clock = first
    later.advance(8)
    assert later.run("water-later", later=True)["status"] == "complete"
    later.conn.close()
    heard = tools["run_water_monitor"].fn(namespace=NS, subscription_id=monitor["subscription_id"])
    kinds = {n["kind"] for n in heard["notifications"]}
    assert {"observation_above_threshold", "observation_revised", "station_revised", "station_removed",
            "assessment_new_cycle"} <= kinds
    assert all(n["new"]["revision_id"] for n in heard["notifications"])
    assert tools["run_water_monitor"].fn(namespace=NS, subscription_id=monitor["subscription_id"])[
        "notifications"] == []

    # As-of answers: provisional before the approval was retrieved, approved after, missing stays missing.
    before = tools["water_value_at"].fn(namespace=NS, station="USGS-01646500", parameter="discharge",
                                        time="2026-09-20T12:00:00Z", as_of=iso(first))
    assert before["values"][0]["on_record"]["quality"]["state"] == "provisional"
    assert before["values"][0]["later_revisions"][0]["quality"]["state"] == "approved"
    after = tools["water_value_at"].fn(namespace=NS, station="USGS-01646500", parameter="discharge",
                                       time="2026-09-20T12:00:00Z")
    assert after["values"][0]["on_record"]["quality"]["state"] == "approved"
    gap = tools["water_value_at"].fn(namespace=NS, station="990001", parameter="W",
                                     time="2026-09-20T00:45:00+02:00")
    assert gap["status"] == "no value published for that time" and "no interpolation" in gap["missing"]["policy"]
    history = tools["water_station_history"].fn(namespace=NS, station="990001")
    assert history["datum_changes"] == 1
    status = tools["water_body_status_history"].fn(namespace=NS, water_body=fixture_builder.WEISSERITZ_WB)
    assert [c["cycle"] for c in status["cycles"]] == ["2016", "2022"]

    # The accepted match still stands but says the station was revised since it was reviewed.
    (match,) = tools["list_water_identity_matches"].fn(namespace=NS, subject_key=station_key,
                                                       place_id=places["dresden"])["matches"]
    assert match["state"] == "accepted" and match["subject_revised_since"] is True

    # Evidence bundle: every item cites source, record revision and as-of time.
    exported = tools["export_water_bundle"].fn(namespace=NS, place_id=places["dresden"])
    assert exported["every_item_cited"] and exported["items"]
    for item in exported["items"]:
        citation = item["citation"]
        assert citation["url"].startswith("https://") and citation["revision_id"] and citation["retrieved_at"]

    # Exclusions and minimisation: nothing forecast, filled, scored, assessed or personal anywhere.
    for answer in (place, before, after, gap, history, status, exported, heard, linked):
        assert not h.forbidden_keys(json.loads(json.dumps(answer)))
    assert tools["water_source_contracts"].fn()["minimisation"]["decision"] == "no personal data"
