"""Tables as of a date with corrections, forfeits, deductions and reschedules (#2142, SP07)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.sports_sources import ACQUISITION_CONTRACT, to_ms
from src.kb.sports_identity import SportsIdentity
from src.kb.sports_queries import SportsQueries
from src.kb.sports_records import SportsError, forbidden_keys
from src.kb.sports_store import SportsStore
from tests.unit.sports import harness as h


@pytest.fixture()
def conn():
    connection = h.connection()
    h.load_league(connection)
    h.record_decisions(connection)
    yield connection
    connection.close()


def points(answer):
    return {row["team_name"]: row["points"] for row in answer["table"]}


def order(answer):
    return [row["team_name"] for row in answer["table"]]


def table(conn, on, **kwargs):
    return SportsQueries(conn).standings_as_of(
        "global", h.SEASON, on, scopes=h.SCOPES, **kwargs
    )


def test_a_result_corrected_after_a_week_changes_the_table_only_from_its_publication(
    conn,
):
    before = table(conn, "2099-08-20")
    assert points(before) == {
        "Northbridge Rovers": 4,
        "Southport United": 2,
        "Westvale Town": 1,
        "Eastfield Athletic": 0,
    }
    after = table(conn, "2099-08-25")
    assert (
        points(after)["Northbridge Rovers"] == 6
        and points(after)["Southport United"] == 1
    )
    (correction,) = [
        c for c in after["corrections"] if c["fixture_key"] == h.fixture_key(103)
    ]
    assert correction["status"] == "corrected" and correction["score"] == {
        "home": 2,
        "away": 1,
    }
    # The match date and the knowledge cutoff are separate axes: 20 August as known on 25 August.
    late_knowledge = table(conn, "2099-08-20", knowledge_cutoff="2099-08-25")
    assert points(late_knowledge)["Northbridge Rovers"] == 6
    assert before["time_axes"]["knowledge_cutoff"] == "2099-08-20T23:59:59Z"
    assert "defaults to the end of the match date" in before["time_axes"]["default"]


def test_a_postponed_fixture_is_unresolved_until_its_rescheduled_result(conn):
    # On 19 August the postponement is published and the new date is not yet.
    on_19 = table(conn, "2099-08-19")
    (item,) = [u for u in on_19["unresolved"] if u["fixture_key"] == h.fixture_key(104)]
    assert item["reason"] == "postponed"
    history = SportsQueries(conn).fixture_history(
        "global", h.fixture_key(104), scopes=h.SCOPES
    )
    assert [m["to"] for m in history["moves"]] == ["2099-09-03T18:30:00Z"]
    assert (
        history["moves"][0]["old"]["status"] == "postponed"
        and history["moves"][0]["new"]["status"] == "rescheduled"
    )
    assert h.fixture_key(104) in {
        c["fixture_key"] for c in table(conn, "2099-09-04")["counted_results"]
    }


def test_a_forfeit_and_a_deduction_change_the_table_from_the_revisions_that_published_them(
    conn,
):
    on_4 = table(conn, "2099-09-04")
    assert points(on_4) == {
        "Northbridge Rovers": 6,
        "Southport United": 4,
        "Westvale Town": 4,
        "Eastfield Athletic": 3,
    }
    assert on_4["comparison"]["status"] == "agrees"
    on_6 = table(conn, "2099-09-06")
    assert points(on_6)["Southport United"] == 1
    (deduction,) = on_6["deductions"]
    assert (
        deduction["deciding_body"] == h.FA
        and deduction["decision"]["url"] == h.DEDUCTION_URL
    )
    on_12 = table(conn, "2099-09-12")
    assert points(on_12) == {
        "Northbridge Rovers": 9,
        "Eastfield Athletic": 3,
        "Southport United": 1,
        "Westvale Town": 1,
    }
    # Tied on one point: goal difference (0 against -6) orders them under the stated rule.
    assert order(on_12) == [
        "Northbridge Rovers",
        "Eastfield Athletic",
        "Southport United",
        "Westvale Town",
    ]
    forfeit = [c for c in on_12["corrections"] if c["status"] == "forfeit_awarded"]
    assert (
        forfeit[0]["deciding_body"] == h.FA
        and forfeit[0]["decision"]["url"] == h.FORFEIT_URL
    )
    # The provider's own table (4 September) is compared, never overwritten: the disagreement is listed.
    comparison = on_12["comparison"]
    assert (
        comparison["status"] == "disagrees"
        and comparison["compared_with"]["as_of"] == "2099-09-04T21:00:00Z"
    )
    changed = {d["team_name"]: d["fields"] for d in comparison["differences"]}
    assert changed["Northbridge Rovers"]["points"] == {"replayed": 9, "published": 6}
    assert "None" not in json.dumps(on_12) and forbidden_keys(on_12) == []
    assert on_12["coverage"]["n"] == 6


def test_a_tie_is_broken_by_head_to_head_under_the_stated_rule():
    conn = h.connection()
    store = SportsStore(conn)
    season = "sports:openfootball:season:h2h:2099"
    records = [
        {
            "record_type": "competition",
            "record_key": "sports:openfootball:competition:h2h",
            "source_record_id": "c",
            "body": {
                "provider": "openfootball",
                "name": "H2H League",
                "sport": "football",
            },
        },
        {
            "record_type": "season",
            "record_key": season,
            "source_record_id": "s",
            "body": {
                "competition_key": "sports:openfootball:competition:h2h",
                "label": "2099",
            },
        },
        {
            "record_type": "table_rule",
            "record_key": f"{season}|rule|scoring",
            "source_record_id": "r",
            "body": {
                "season_key": season,
                "kind": "scoring",
                "points": {"win": 3, "draw": 1, "loss": 0},
                "tiebreakers": ["points", "head_to_head_points", "goal_difference"],
                "source_url": "https://rules.example.org/h2h",
            },
        },
    ]
    # P beats Q 1-0, Q thrashes R 5-0, S beats P 3-0: P, Q and S all on 3 points. Q has the best goal difference
    # (+4) but lost the head-to-head against P, so head-to-head points put S and P above Q; S and P are then
    # separated by goal difference. E and F only draw each other: an unresolved tie stays shared.
    games = [("p", "q", 1, 0), ("q", "r", 5, 0), ("s", "p", 3, 0), ("e", "f", 0, 0)]
    for index, (home, away, hs, as_) in enumerate(games):
        key = f"sports:openfootball:fixture:h2h:{index}"
        records += [
            {
                "record_type": "fixture",
                "record_key": key,
                "source_record_id": key,
                "body": {
                    "sport": "football",
                    "season_key": season,
                    "sides": {
                        "home": f"sports:openfootball:team:h2h:{home}",
                        "away": f"sports:openfootball:team:h2h:{away}",
                    },
                },
            },
            {
                "record_type": "fixture_schedule_revision",
                "record_key": key,
                "source_record_id": key,
                "body": {"status": "scheduled", "kickoff": "2099-08-10"},
            },
            {
                "record_type": "match_result_revision",
                "record_key": key,
                "source_record_id": key,
                "body": {"status": "official", "score": {"home": hs, "away": as_}},
            },
        ]
    for team in "pqrsef":
        records.append(
            {
                "record_type": "team",
                "record_key": f"sports:openfootball:team:h2h:{team}",
                "source_record_id": team,
                "body": {"provider": "openfootball", "name": team.upper()},
            }
        )
    store.now = lambda: to_ms("2099-08-11")
    store.apply(
        "global",
        {
            "contract": ACQUISITION_CONTRACT,
            "provider": "openfootball",
            "format": "openfootball-json",
            "file_sha256": "a" * 64,
            "record_count": len(records),
            "published_at": "2099-08-11",
            "attribution": "fixture",
            "evidence_origin": "fixture",
        },
        records,
        run_id="r",
        source_id="s",
    )
    answer = SportsQueries(conn).standings_as_of(
        "global", season, "2099-08-12", scopes=h.SCOPES
    )
    rows = [
        (r["position"], r["team_name"], r["points"], r.get("tie_unresolved", False))
        for r in answer["table"]
    ]
    assert rows[:3] == [(1, "S", 3, False), (2, "P", 3, False), (3, "Q", 3, False)]
    assert rows[3:] == [(4, "E", 1, True), (4, "F", 1, True), (6, "R", 0, False)]
    assert answer["comparison"]["status"] == "no-published-table"


def test_match_history_keeps_every_revision_and_other_sources_side_by_side(conn):
    h.apply(conn, "openfootball", "openfootball_exl_2099.json", at="2099-08-20")
    identity = SportsIdentity(conn, now=h.Clock("2099-09-10"))
    proposed = identity.propose("global", principal_id="alice", scopes=h.REVIEW_SCOPES)[
        "candidates"
    ]
    for candidate in proposed:
        if candidate["basis"] == "name-jurisdiction" and all(
            ":team:" in r for r in candidate["records"]
        ):
            identity.review(
                "global",
                candidate["candidate_id"],
                "accept",
                "same club",
                principal_id="rev",
                scopes=h.REVIEW_SCOPES,
            )
    history = SportsQueries(conn).match_history(
        "global", h.fixture_key(103), scopes=h.SCOPES, identity=identity
    )
    assert [r["status"] for r in history["results"]] == ["official", "corrected"]
    (other,) = history["other_sources"]
    assert (
        other["result"]["score"] == {"home": 1, "away": 1} and other["agrees"] is False
    )
    schedule = SportsQueries(conn).team_schedule(
        "global",
        h.team_key(9001),
        scopes=h.SCOPES,
        date_from="2099-08-01",
        date_to="2099-08-31",
        identity=identity,
    )
    assert {f["provider"] for f in schedule["fixtures"]} == {
        "football-data",
        "openfootball",
    }
    forfeited = SportsQueries(conn).match_history(
        "global", h.fixture_key(105), scopes=h.SCOPES
    )
    assert forfeited["status"] == "forfeit_awarded"
    assert forfeited["results"][-1]["body"]["deciding_body"] == h.FA


def test_queries_are_not_ready_before_a_source_ran_and_need_their_scope():
    conn = h.connection()
    with pytest.raises(SportsError) as caught:
        SportsQueries(conn).standings_as_of(
            "global", h.SEASON, "2099-08-20", scopes=h.SCOPES
        )
    assert caught.value.code == "not_ready"
    h.load_league(conn)
    with pytest.raises(SportsError) as denied:
        SportsQueries(conn).standings_as_of(
            "global", h.SEASON, "2099-08-20", scopes={"knowledge:read"}
        )
    assert denied.value.code == "unauthorized"


def test_licence_gated_exports_refuse_commercial_use_and_relicensing(conn):
    h.apply(conn, "atp", "sackmann_atp_matches_2099.csv", at="2099-01-21")
    refs = [
        {
            "record_type": "match_result_revision",
            "record_key": "sports:sackmann-tennis:fixture:atp:2099-9001:3",
        }
    ]
    queries = SportsQueries(conn)
    allowed = queries.export_records(
        "global", refs, scopes=h.SCOPES, purpose="non-commercial"
    )
    assert (
        allowed["licences"][0]["id"] == "CC-BY-NC-SA-4.0"
        and "Jeff Sackmann" in allowed["attributions"][0]
    )
    for kwargs in (
        {"purpose": "commercial"},
        {"purpose": "non-commercial", "relicense_as": "CC-BY-4.0"},
    ):
        with pytest.raises(SportsError) as caught:
            queries.export_records("global", refs, scopes=h.SCOPES, **kwargs)
        assert caught.value.code == "licence_refused"
    assert SportsStore(conn, initialize=False).current(
        "global", "match_result_revision", refs[0]["record_key"]
    )
