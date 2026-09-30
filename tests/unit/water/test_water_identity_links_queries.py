"""WA06-WA09 (#2612, #2617, #2622, #2627): reviewable identity, cross-pack links and as-of answers."""

import pytest

from src.kb.water_identity import WaterIdentity
from src.kb.water_links import WaterLinks
from src.kb.water_queries import place_water, series, status_history, value_at
from src.kb.water_records import WaterError
from tests.unit.water import fixture_builder, harness
from tests.unit.water.harness import ALL, NS, WRITE

EXAMPLA = f"pegelonline:{fixture_builder.EXAMPLA}"
NORTHWIND = f"pegelonline:{fixture_builder.NORTHWIND}"


@pytest.fixture()
def env():
    item = harness.Env().loaded()
    item.ids = item.places()
    item.identity = WaterIdentity(item.conn, now=item.tick)
    return item


def _match(proposed, subject, method):
    return next(m for m in proposed["matches"] if m["subject_key"] == subject and m["method"] == method)


def test_identity_is_proposed_with_evidence_never_accepted_and_unmatched_stay_visible(env):
    proposed = env.identity.propose(NS, principal_id="alice", scopes=ALL)
    assert {m["state"] for m in proposed["matches"]} == {"proposed"}
    river = _match(proposed, EXAMPLA, "published-river-identifier")
    assert river["place_id"] == env.ids["nordfluss"] and river["evidence"]["identifier"] == "NORDFLUSS"
    within = _match(proposed, EXAMPLA, "published-coordinates-within")
    assert within["place_id"] == env.ids["exampla"] and within["evidence"]["receipt_id"]
    assert within["evidence"]["geometry_id"] and within["evidence_class"] == "deterministic"
    body = _match(proposed, "wfd:DEFX_EXAMPLA_01", "published-geometry-within")
    assert body["evidence"]["published_geometry"]["cycle"] == "2022" and body["evidence"]["vertices_inside"] == 3
    # A water body without a published geometry is never placed by name or guesswork.
    assert "wfd:DEFX_EXAMPLA_02" in proposed["unmatched"]
    assert not [m for m in proposed["matches"] if m["method"] == "river-name"]  # identifiers come before names
    again = env.identity.propose(NS, principal_id="alice", scopes=ALL)
    assert again["proposed"] == []  # idempotent


def test_review_accept_reject_and_revert_are_entity_identity_decisions(env):
    proposed = env.identity.propose(NS, principal_id="alice", scopes=ALL)
    within = _match(proposed, EXAMPLA, "published-coordinates-within")
    with pytest.raises(WaterError):
        env.identity.review(NS, within["match_id"], "accept", "ok", principal_id="bob", scopes=WRITE)
    accepted = env.identity.review(NS, within["match_id"], "accept", "published point in the boundary",
                                   principal_id="bob", scopes=ALL)
    assert accepted["state"] == "accepted" and accepted["reviewer"] == "bob" and accepted["decision_id"]
    reverted = env.identity.revert(NS, within["match_id"], "boundary vintage changed", principal_id="bob",
                                   scopes=ALL)
    assert reverted["state"] == "reverted" and [h["state"] for h in reverted["history"]] == [
        "proposed", "accepted", "reverted"]
    usgs = _match(proposed, "usgs:USGS-99990001", "published-coordinates-within")
    rejected = env.identity.review(NS, usgs["match_id"], "reject", "not in scope", principal_id="bob", scopes=ALL)
    assert rejected["state"] == "rejected" and env.identity.accepted(NS) == []


def test_links_record_basis_point_at_revisions_and_report_missing_providers(env):
    missing = WaterLinks(env.conn, now=env.tick).link(NS, principal_id="alice", scopes=ALL)
    assert {"hazards", "weather", "infrastructure"} <= set(missing["unavailable"])
    env.seed_other_packs()
    proposed = env.identity.propose(NS, principal_id="alice", scopes=ALL)
    env.identity.review(NS, _match(proposed, EXAMPLA, "published-coordinates-within")["match_id"], "accept", "ok",
                        principal_id="bob", scopes=ALL)
    result = WaterLinks(env.conn, now=env.tick).link(NS, principal_id="alice", scopes=ALL)
    assert result["created"] == {"citation": 1, "shared_identifier": 2, "accepted_match": 1}
    by_kind = {(link["subject_key"], link["target_kind"]): link for link in result["links"]}
    flood = by_kind[(EXAMPLA, "hazard")]
    assert flood["basis"] == "citation" and flood["evidence"]["token"] == "59990001"
    assert flood["target_revision"].startswith("hazard-rev") and flood["revision_id"].startswith("water-revision")
    assert by_kind[(NORTHWIND, "weather-station")]["basis"] == "shared_identifier"
    assert by_kind[("wfd:DEFX_EXAMPLA_01", "infrastructure")]["evidence"]["stated_as"] == ["eu-water-body-code"]
    assert by_kind[(EXAMPLA, "place")]["basis"] == "accepted_match"
    assert result["unavailable"]["dams-and-waterways"]["status"] == "unavailable"  # reported, not dropped
    assert "no causal" in flood["evidence"]["notice"]
    unrelated = [link for link in result["links"] if link["target_kind"] == "hazard"]
    assert len(unrelated) == 1  # the event that cites nothing gets no link
    narrow = WaterLinks(env.conn, now=env.tick).link(NS, principal_id="alice", scopes=WRITE)
    assert "knowledge:hazards:read" in narrow["unavailable"]["hazards"]["reason"]


def test_value_at_states_quality_cites_the_revision_and_keeps_later_revisions(env):
    before = value_at(env.conn, NS, "USGS-99990001", "discharge", "2026-09-04", scopes=ALL)
    assert before["status"] == "value on record" and before["quality"]["state"] == "provisional"
    assert before["qualifiers"] == ["e"] and before["citation"]["revision_no"] == 1
    as_of = env.clock
    env.advance(7)
    assert env.run("water-later", later=True)["status"] == "complete"
    then = value_at(env.conn, NS, "USGS-99990001", "00060", "2026-09-04", scopes=ALL, as_of=as_of)
    assert then["value"] == 14.1 and then["quality"]["state"] == "provisional"
    assert [(r["value"], r["quality"]["state"]) for r in then["later_revisions"]] == [(14.0, "approved")]
    now = value_at(env.conn, NS, "USGS-99990001", "discharge", "2026-09-04", scopes=ALL)
    assert now["value"] == 14.0 and now["quality"]["state"] == "approved" and now["earlier_revisions"]
    withdrawn = value_at(env.conn, NS, "USGS-99990001", "discharge", "2026-09-06", scopes=ALL)
    assert withdrawn["status"] == "withdrawn by the source" and withdrawn["value"] is None
    gap = value_at(env.conn, NS, "USGS-99990001", "discharge", "2026-09-05", scopes=ALL)
    assert gap["status"] == "no value published for this time" and gap["value"] is None
    assert gap["missing"]["previous_published"] == "2026-09-04"
    early = value_at(env.conn, NS, "USGS-99990001", "discharge", "2026-09-04", scopes=ALL, as_of="2020-01-01")
    assert early["status"] == "not yet on record at as_of"


def test_levels_keep_gauge_zero_and_missing_steps_stay_missing(env):
    level = value_at(env.conn, NS, "59990001", "water_level", "2026-09-19T22:15:00Z", scopes=ALL)
    assert level["value"] == 515.0 and level["unit"] == "cm" and level["gauge_zero"]["value"] == 30.12
    assert level["quality"]["state"] == "provisional"
    window = series(env.conn, NS, "EXAMPLA", "W", start=fixture_builder.START, end=fixture_builder.END, scopes=ALL)
    assert window["missing"] == ["2026-09-20T00:45:00+02:00"] and len(window["values"]) == 6
    assert all(v["citation"]["revision_id"] for v in window["values"])
    with pytest.raises(WaterError):
        value_at(env.conn, NS, "no such gauge", "water_level", "2026-09-20T00:00:00+02:00", scopes=ALL)


def test_place_answers_use_stated_geometry_versions_and_list_status_per_cycle(env):
    answer = place_water(env.conn, NS, env.ids["exampla"], scopes=ALL)
    (station,) = answer["stations"]
    assert station["subject_key"] == EXAMPLA and station["membership"]["geometry_id"]
    assert "not reviewed" in station["membership"]["basis"]
    assert station["latest"]["water_level"]["time"] == "2026-09-20T01:30:00+02:00"
    (body,) = answer["water_bodies"]
    assert [c["cycle_year"] for c in body["status_history"]] == ["2016", "2022"]
    assert [c["ecological"]["label"] for c in body["status_history"]] == ["Moderate", "Poor"]
    assert all(c["citation"]["revision_id"] for c in body["status_history"])
    proposed = env.identity.propose(NS, principal_id="alice", scopes=ALL)
    env.identity.review(NS, _match(proposed, NORTHWIND, "published-river-identifier")["match_id"], "accept", "ok",
                        principal_id="bob", scopes=ALL)
    env.identity.review(NS, _match(proposed, EXAMPLA, "published-river-identifier")["match_id"], "accept", "ok",
                        principal_id="bob", scopes=ALL)
    on_river = place_water(env.conn, NS, env.ids["exampla"], river=env.ids["nordfluss"], scopes=ALL)
    assert [s["subject_key"] for s in on_river["stations"]] == [EXAMPLA]  # the river within the place only
    river = place_water(env.conn, NS, env.ids["nordfluss"], scopes=ALL)
    assert {s["subject_key"] for s in river["stations"]} == {EXAMPLA, NORTHWIND}
    assert {s["membership"]["method"] for s in river["stations"]} == {"published-river-identifier"}
    empty = place_water(env.conn, NS, env.ids["moor"], scopes=ALL)
    assert empty["status"] == "no station or water body on record for this place"
    history = status_history(env.conn, NS, "DEFX_EXAMPLA_02", scopes=ALL)
    assert [c["ecological"]["value"] for c in history["cycles"]] == ["2", "Unknown"]
    assert "not merged" in history["notice"]
