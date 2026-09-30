"""As-of value answers, series with gaps, place answers and WFD status history (#2582, WA08, WA09)."""

from __future__ import annotations

import pytest

from src.kb import water_queries as q
from src.kb.water_identity import WaterIdentity
from src.kb.water_records import WaterError
from src.kb.water_store import iso
from tests.unit.water import fixture_builder
from tests.unit.water import harness as h

NS = h.NS


@pytest.fixture(scope="module")
def world():
    env = h.Env().loaded()
    first = env.clock
    env.advance(8)
    assert env.run("water-later", later=True)["status"] == "complete"
    return env, first


def test_value_at_states_quality_cites_the_revision_and_lists_later_revisions(world):
    env, first = world
    earlier = q.value_at(env.conn, NS, "USGS-01646500", "discharge", "2026-09-20T12:30:00Z", scopes=h.READ,
                         as_of=iso(first))
    (value,) = earlier["values"]
    assert value["on_record"]["quality"]["state"] == "provisional" and value["on_record"]["value"] == "4180"
    assert value["on_record"]["qualifiers"] == ["Estimated"]
    assert value["on_record"]["citation"]["revision_no"] == 1 and value["on_record"]["citation"]["url"].startswith(
        "https://api.waterdata.usgs.gov/")
    (later,) = value["later_revisions"]
    assert later["quality"]["state"] == "approved" and later["value"] == "4170" and later["changes"] == [
        "quality", "value"]
    now = q.value_at(env.conn, NS, "usgs:USGS-01646500", "00060", "2026-09-20T12:30:00+00:00", scopes=h.READ)
    assert now["values"][0]["on_record"]["quality"]["state"] == "approved" and not now["values"][0]["later_revisions"]
    assert q.value_at(env.conn, NS, "USGS-01646500", "discharge", "2026-09-20T12:30:00Z", scopes=h.READ,
                      as_of="2020-01-01")["status"] == "not yet on record at the as-of time"


def test_value_at_keeps_missing_times_missing_and_states_the_datum(world):
    env, first = world
    missing = q.value_at(env.conn, NS, "990001", "W", "2026-09-20T00:45:00+02:00", scopes=h.READ)
    assert missing["status"] == "no value published for that time" and missing["values"] == []
    assert missing["missing"]["previous_published_time"] == "2026-09-20T00:30:00+02:00"
    assert missing["missing"]["next_published_time"] == "2026-09-20T01:00:00+02:00"
    assert "no interpolation" in missing["missing"]["policy"]
    level = q.value_at(env.conn, NS, "990001", "water_level", "2026-09-19T22:30:00Z", scopes=h.READ, as_of=iso(first))
    datum = level["values"][0]["reference_datum"]
    assert datum["kind"] == "gauge-zero" and datum["value"] == "102.73" and datum["valid_from"] == "2019-11-01"
    assert level["values"][0]["on_record"]["value"] == "215.0"
    assert level["values"][0]["later_revisions"][0]["value"] == "214.0"
    with pytest.raises(WaterError):
        q.value_at(env.conn, NS, "no-such-station", "W", "2026-09-20T00:00:00Z", scopes=h.READ)


def test_series_reports_gaps_from_the_published_interval_and_never_fills(world):
    env, first = world
    window = q.series(env.conn, NS, "990001", "W", "2026-09-19T22:00:00Z", "2026-09-20T00:00:00Z", scopes=h.READ,
                      as_of=iso(first))
    assert [v["time"] for v in window["values"]] == [t for t, _ in fixture_builder.DRESDEN_W]
    assert window["gaps"] == [{"parameter": "W", "after": "2026-09-20T00:30:00+02:00",
                               "before": "2026-09-20T01:00:00+02:00", "published_interval_min": 15,
                               "status": "no value published", "policy": q.NOT_FILLED}]
    assert window["quality_states"] == ["provisional"]
    empty = q.series(env.conn, NS, "990001", "Q", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", scopes=h.READ)
    assert empty["status"] == "no value on record in the window"


def test_station_history_shows_datum_vintages_and_the_withdrawal(world):
    env, _ = world
    history = q.station_history(env.conn, NS, "990001", scopes=h.READ)
    assert [v["changes"] for v in history["vintages"]] == [["created"], ["datum"]] and history["datum_changes"] == 1
    assert [d["valid_from"] for v in history["vintages"] for d in v["datums"]] == ["2019-11-01", "2026-09-25"]
    meissen = q.station_history(env.conn, NS, "990002", scopes=h.READ)
    assert [v["event"] for v in meissen["vintages"]] == ["published", "removed"]


def test_status_history_lists_each_cycle_as_published_and_never_merges(world):
    env, first = world
    history = q.status_history(env.conn, NS, "DERW_DESN_FIX-0001", scopes=h.READ)
    assert [c["cycle"] for c in history["cycles"]] == ["2016", "2022"] and history["latest_cycle"] == "2022"
    assert [c["ecological_status"] for c in history["cycles"]] == ["Moderate", "Poor"]
    assert all(c["citation"]["revision_id"] for c in history["cycles"])
    weisseritz = q.status_history(env.conn, NS, "eu-wb:DERW_DESN_FIX-0002", scopes=h.READ, as_of=iso(first))
    assert [c["cycle"] for c in weisseritz["cycles"]] == ["2016"]
    assert "never merged" in history["notice"]


def test_place_answers_state_geometry_versions_bases_status_and_unmatched(world):
    env, _ = world
    places = env.places()
    identity = WaterIdentity(env.conn, now=env.tick)
    proposed = identity.propose(NS, principal_id="alice", scopes=h.ALL)
    body = next(m for m in proposed["matches"] if m["subject_key"] == "eu-wb:DERW_DESN_FIX-0001")
    identity.review(NS, body["match_id"], "accept", "published geometry crosses Dresden", principal_id="bob",
                    scopes=h.ALL)
    answer = q.for_place(env.conn, NS, places["dresden"], scopes=h.READ)
    assert answer["place"]["geometry_version"]["geometry_id"]
    (station,) = answer["stations"]
    assert station["subject_key"] == "pegelonline:" + fixture_builder.DRESDEN
    assert station["bases"][0]["basis"] == "geospatial-containment" and station["bases"][0]["geometry_version"]
    (water_body,) = answer["water_bodies"]
    assert {b["basis"] for b in water_body["bases"]} == {"accepted_match", "geospatial-intersection"}
    assert water_body["latest"]["cycle"] == "2022" and [c["cycle"] for c in water_body["history"]] == [
        "2016", "2022"]
    assert "eu-wb:DERW_DESN_FIX-0002" in {u["subject_key"] for u in answer["unmatched"]}
    river = q.for_place(env.conn, NS, places["elbe"], scopes=h.READ)
    assert {s["name"] for s in river["stations"]} == {"DRESDEN", "MEISSEN"}
    assert q.for_place(env.conn, NS, places["nowhere"], scopes=h.READ)["status"] == \
        "no station or water body on record for this place"


def test_bundle_cites_every_item_with_revision_and_as_of(world):
    env, first = world
    exported = q.bundle(env.conn, NS, scopes=h.READ, station="USGS-01646500", water_body="DERW_DESN_FIX-0001",
                        as_of=iso(first))
    assert exported["every_item_cited"] and exported["item_count"] == 1 + 6 + 2
    for item in exported["items"]:
        assert item["citation"]["revision_id"] and item["citation"]["url"] and item["citation"]["retrieved_at"]
    assert not h.forbidden_keys(exported)
    with pytest.raises(WaterError):
        q.bundle(env.conn, NS, scopes=h.READ)
    with pytest.raises(WaterError):
        q.value_at(env.conn, NS, "990001", "W", "2026-09-20T00:00:00Z", scopes={"knowledge:environment:read"})
