"""Sports acquisition: football-data.org, StatsBomb, openfootball, Sackmann and Olympic results (#2138-#2140)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.ingestion.sports_sources import (
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    SACKMANN_LICENCE,
    SportsFormatError,
    SportsResultsAdapter,
    fixture_transport,
    olympic_acquisition,
    to_ms,
)
from src.kb.sports_records import forbidden_keys
from src.kb.sports_store import SportsStore
from tests.unit.sports import harness as h


@pytest.fixture()
def conn():
    connection = h.connection()
    yield connection
    connection.close()


def test_every_candidate_source_has_an_audited_decision_and_live_evidence_is_separate():
    for provider in (
        "football-data",
        "statsbomb-open-data",
        "openfootball",
        "sackmann-tennis",
        "olympics-com",
        "olympic-results",
        "transfers-official",
        "commercial-transfer-databases",
        "bookmakers",
    ):
        contract = PROVIDER_CONTRACTS[provider]
        assert contract["access_decision"] in {
            "implement",
            "link-only",
            "not implemented",
        }
        assert contract["reason"]
        assert LIVE_VERIFICATION[provider]["status"] != "verified-live"
    assert PROVIDER_CONTRACTS["bookmakers"]["access_decision"] == "not implemented"
    audit = (h.ROOT / "docs/development/sports-evidence/source-audit.md").read_text()
    assert (
        "CC BY-NC-SA 4.0" in audit
        and "(verify" in audit
        and "Bounded v1 coverage" in audit
    )
    assert all(
        s["auth"]["kind"] == "required-secret"
        and s["auth"]["secret_ref"] == "NOESIS_FOOTBALL_DATA_API_KEY"
        for s in h.manifest()["sources"]
        if s["sports"]["provider"] == "football-data"
    )


def test_the_pack_replays_its_pinned_fixtures_offline():
    report = SourcePackConformance(h.ROOT).offline(json.loads(h.PACK.read_text()))
    assert report["valid"] and report["coverage"]["configured"] == 7


def test_football_data_polls_append_schedule_and_result_revisions_with_reschedule_history(
    conn,
):
    h.load_league(conn)
    store = SportsStore(conn, initialize=False)
    schedule = store.labelled_history(
        "global", "fixture_schedule_revision", h.fixture_key(104)
    )
    assert [(r["status"], r["body"]["kickoff"]) for r in schedule] == [
        ("postponed", "2099-08-17T14:00:00Z"),
        ("rescheduled", "2099-09-03T18:30:00Z"),
    ]
    results = store.labelled_history(
        "global", "match_result_revision", h.fixture_key(103)
    )
    assert [(r["status"], r["body"]["score"]) for r in results] == [
        ("official", {"home": 1, "away": 1}),
        ("corrected", {"home": 2, "away": 1}),
    ]
    # An unchanged poll adds nothing; odds and referees never reach a record.
    before = conn.execute("SELECT count(*) FROM sports_revisions").fetchone()[0]
    h.apply(conn, "fd-matches", "fd_exl_2099_poll3.json", at="2099-09-05")
    assert conn.execute("SELECT count(*) FROM sports_revisions").fetchone()[0] == before
    bodies = [
        json.loads(b)
        for (b,) in conn.execute("SELECT body_json FROM sports_revisions").fetchall()
    ]
    assert forbidden_keys(bodies) == [] and "Rita Referee" not in json.dumps(bodies)
    receipt = h.page("fd-matches", "fd_exl_2099_poll1.json")[0][0].receipt
    assert receipt["final_page"] and receipt["evidence_origin"] == "fixture"


def test_published_standings_are_snapshots_with_their_as_of_time_never_derived(conn):
    h.load_league(conn)
    store = SportsStore(conn, initialize=False)
    (key,) = store.records("global", "standing_snapshot")
    snapshot = store.current("global", "standing_snapshot", key)
    assert snapshot["body"]["as_of"] == "2099-09-04T21:00:00Z"
    assert [r["team_name"] for r in snapshot["body"]["rows"]][:2] == [
        "Northbridge Rovers",
        "Southport United",
    ]
    with pytest.raises(SourcePackError, match="HTTP Date"):
        h.page(
            "fd-standings",
            "fd_exl_2099_standings.json",
            headers={"Content-Type": "application/json"},
        )


def test_football_data_teams_keep_founding_country_venue_and_published_birth_dates_only(
    conn,
):
    h.apply(conn, "fd-teams", "fd_exl_2099_teams.json", at="2099-08-01")
    store = SportsStore(conn, initialize=False)
    team = store.body("global", "team", h.team_key(9001))
    assert (
        team["founded"] == 1888
        and team["country"] == "Exampleland"
        and team["venue_key"]
    )
    player = store.body("global", "player", "sports:football-data:player:50001")
    assert player == {
        "birth_date": "2075-04-12",
        "name": "Jan Example",
        "native_id": "50001",
        "provider": "football-data",
    }
    assert "Carl Coach" not in json.dumps(
        [
            json.loads(b)
            for (b,) in conn.execute(
                "SELECT body_json FROM sports_revisions"
            ).fetchall()
        ]
    )


def test_a_missing_credential_blocks_football_data_and_a_wrong_competition_is_drift():
    item = h.source("fd-matches")
    adapter = SportsResultsAdapter(item, transport=fixture_transport([]), secret=None)
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page(
            {"operation": "matches", "parameters": {}, "limit": 10}, cursor=None
        )
    assert caught.value.code == "credential_missing"
    other = h.source("fd-matches")
    other["sports"]["competition"]["code"] = "PL"
    with pytest.raises(SourcePackError, match="EXL"):
        h.page("fd-matches", "fd_exl_2099_poll1.json", item=other)


def test_statsbomb_matches_and_lineups_keep_positions_and_shirt_numbers_with_attribution(
    conn,
):
    h.apply(
        conn,
        "statsbomb",
        "statsbomb_competitions.json,statsbomb_matches_9_99.json,statsbomb_lineups_7001.json",
        at="2099-09-02",
    )
    store = SportsStore(conn, initialize=False)
    fixture = "sports:statsbomb-open-data:fixture:7001"
    lineup = store.current(
        "global", "lineup", f"{fixture}|lineup|sports:statsbomb-open-data:team:801"
    )
    assert [
        (p["name"], p["shirt_number"], p["position"])
        for p in lineup["body"]["starters"]
    ] == [
        ("Jan Example", 8, "Center Midfield"),
        ("Ola Keeper", 1, "Goalkeeper"),
    ]
    assert [p["name"] for p in lineup["body"]["substitutes"]] == [
        "Sam Bench",
        "Una Unused",
    ]
    assert (
        lineup["source"]["attribution"]
        == "Data provided by StatsBomb (StatsBomb open data)"
    )
    assert lineup["source"]["release"] == "0" * 40
    assert store.current("global", "match_result_revision", fixture)["body"][
        "score"
    ] == {"home": 2, "away": 1}
    text = json.dumps(
        [
            json.loads(b)
            for (b,) in conn.execute(
                "SELECT body_json FROM sports_revisions"
            ).fetchall()
        ]
    )
    assert "Carl Coach" not in text and "Rita Referee" not in text


def _later_commit(commit: str) -> dict:
    item = h.source("openfootball")
    item["sports"]["release"] = {
        "commit": commit * 40,
        "committed_at": "2099-09-05T00:00:00Z",
    }
    item["endpoint"] = item["endpoint"].replace("0" * 40, commit * 40)
    return item


def test_openfootball_commit_is_the_release_and_a_moved_date_keeps_the_fixture_key(
    conn,
):
    h.apply(conn, "openfootball", "openfootball_exl_2099.json", at="2099-08-20")
    data = json.loads((h.FIXTURES / "openfootball_exl_2099.json").read_text())
    data["matches"][3].update(date="2099-09-03", time="19:30", score={"ft": [3, 0]})
    h.apply(
        conn,
        "openfootball",
        None,
        at="2099-09-06",
        item=_later_commit("1"),
        body=json.dumps(data),
    )
    store = SportsStore(conn, initialize=False)
    key = "sports:openfootball:fixture:exl-1:2099-00:matchday-2:eastfield-athletic:westvale-town"
    history = store.labelled_history("global", "fixture_schedule_revision", key)
    assert [(r["status"], r["body"]["kickoff"]) for r in history] == [
        ("scheduled", "2099-08-17"),
        ("rescheduled", "2099-09-03T19:30"),
    ]
    assert history[-1]["source"]["release"] == "1" * 40
    assert (
        store.current("global", "match_result_revision", key)["source"]["release"]
        == "1" * 40
    )


def test_a_renamed_club_is_a_new_published_name_never_merged_silently(conn):
    h.apply(conn, "openfootball", "openfootball_exl_2099.json", at="2099-08-20")
    h.apply(
        conn,
        "openfootball",
        "openfootball_exl_2099_later.json",
        at="2099-09-06",
        item=_later_commit("2"),
    )
    store = SportsStore(conn, initialize=False)
    teams = store.records("global", "team", provider="openfootball")
    assert "sports:openfootball:team:exl-1:westvale-town" in teams
    assert "sports:openfootball:team:exl-1:westvale-city" in teams


def test_different_results_from_two_sources_stay_side_by_side(conn):
    h.load_league(conn)
    h.apply(conn, "openfootball", "openfootball_exl_2099.json", at="2099-09-06")
    store = SportsStore(conn, initialize=False)
    fd = store.current("global", "match_result_revision", h.fixture_key(103))
    of = store.current(
        "global",
        "match_result_revision",
        "sports:openfootball:fixture:exl-1:2099-00:matchday-2:northbridge-rovers-fc:southport-united",
    )
    assert fd["body"]["score"] == {"home": 2, "away": 1} and of["body"]["score"] == {
        "home": 1,
        "away": 1,
    }
    assert fd["record_key"] != of["record_key"]


def test_sackmann_keeps_match_level_fields_and_the_licence_on_every_record(conn):
    h.apply(conn, "atp", "sackmann_atp_matches_2099.csv", at="2099-01-21")
    store = SportsStore(conn, initialize=False)
    rows = conn.execute(
        "SELECT record_type, record_key FROM sports_revisions WHERE provider='sackmann-tennis'"
    ).fetchall()
    assert rows
    for record_type, key in rows:
        revision = store.current("global", record_type, key)
        assert revision["source"]["licence"]["id"] == "CC-BY-NC-SA-4.0"
        assert (
            revision["source"]["licence"]["share_alike"]
            and revision["source"]["licence"]["non_commercial"]
        )
        assert revision["source"]["attribution"] == SACKMANN_LICENCE["attribution"]
    text = json.dumps(
        [
            json.loads(b)
            for (b,) in conn.execute(
                "SELECT body_json FROM sports_revisions"
            ).fetchall()
        ]
    )
    assert not re.search(r'"(hand|ht|age|ioc|height)"', text)
    final = store.current(
        "global",
        "match_result_revision",
        "sports:sackmann-tennis:fixture:atp:2099-9001:3",
    )
    assert final["body"] == {
        "score_text": "6-2 6-2",
        "status": "official",
        "winner": "p1",
    }
    retired = store.current(
        "global",
        "match_result_revision",
        "sports:sackmann-tennis:fixture:atp:2099-9001:2",
    )
    assert retired["body"]["retired"] is True
    bad = h.source("atp")
    bad["sports"]["licence"] = {"id": "CC-BY-4.0"}
    with pytest.raises(SourcePackError, match="share-alike"):
        SportsResultsAdapter(bad)


def test_olympic_reallocation_is_a_result_revision_citing_the_deciding_body(conn):
    store = SportsStore(conn)
    store.now = lambda: to_ms("2096-08-06")
    url = "https://www.olympics.example.org/results/example-2096/athletics/mens-800m"
    store.apply(
        "global",
        *olympic_acquisition(
            (h.FIXTURES / "olympic_2096_results.csv").read_text(), source_url=url
        ),
        run_id="r1",
        source_id="operator:olympic-results",
    )
    store.now = lambda: to_ms("2099-03-02")
    store.apply(
        "global",
        *olympic_acquisition(
            (h.FIXTURES / "olympic_2096_reallocation.csv").read_text(), source_url=url
        ),
        run_id="r2",
        source_id="operator:olympic-results",
    )
    key = "sports:olympic-results:fixture:example-2096:athletics:men-s-800m:final"
    history = store.labelled_history("global", "match_result_revision", key)
    assert [r["status"] for r in history] == ["official", "corrected"]
    realloc = history[-1]["body"]
    assert realloc["deciding_body"] == "IOC Executive Board"
    assert realloc["decision"]["url"].startswith(
        "https://www.olympics.example.org/news/"
    )
    assert [(r.get("rank"), r["participant"], r["status"]) for r in realloc["ranking"]][
        :2
    ] == [
        (1, "Fay Stride", "reallocated"),
        (2, "Gus Pace", "reallocated"),
    ]
    assert realloc["ranking"][-1]["status"] == "disqualified"
    body = (
        (h.FIXTURES / "olympic_2096_reallocation.csv")
        .read_text()
        .replace("IOC Executive Board", "")
    )
    with pytest.raises(SportsFormatError, match="deciding body"):
        olympic_acquisition(body, source_url=url)


def test_declarations_pin_commits_and_bounded_file_lists():
    item = h.source("statsbomb")
    item["sports"]["files"] = ["../../secrets"]
    with pytest.raises(SourcePackError, match="relative paths"):
        SportsResultsAdapter(item)
    item = h.source("openfootball")
    item["endpoint"] = item["endpoint"].replace("0" * 40, "master")
    with pytest.raises(SourcePackError, match="pinned commit"):
        SportsResultsAdapter(item)
    assert Path(h.PACK).exists()
