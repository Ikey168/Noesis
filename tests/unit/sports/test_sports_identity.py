"""Reviewable identity for players, teams, competitions and venues (#2141, SP06)."""

from __future__ import annotations

import pytest

from src.ingestion.sports_sources import to_ms
from src.kb.entities import add_manual_alias
from src.kb.ownership_store import OwnershipError
from src.kb.sports_identity import SportsIdentity
from src.kb.sports_records import SportsError
from src.kb.sports_store import SportsStore
from tests.unit.sports import harness as h

SOURCE = {
    "url": "https://www.example-fa.example.org/news/westvale-renamed",
    "title": "Westvale Town becomes City",
}


@pytest.fixture()
def env():
    conn = h.connection()
    h.load_league(conn)
    h.apply(conn, "openfootball", "openfootball_exl_2099.json", at="2099-08-20")
    h.apply(
        conn,
        "statsbomb",
        "statsbomb_competitions.json,statsbomb_matches_9_99.json,statsbomb_lineups_7001.json",
        at="2099-09-02",
    )
    clock = h.Clock("2099-09-10")
    yield conn, SportsIdentity(conn, now=clock)
    conn.close()


def by_records(candidates):
    return {tuple(sorted(c["records"])): c for c in candidates}


def test_teams_are_proposed_by_name_and_country_and_stay_proposed_until_reviewed(env):
    conn, identity = env
    result = identity.propose("global", principal_id="alice", scopes=h.REVIEW_SCOPES)
    found = by_records(result["candidates"])
    pair = tuple(
        sorted(
            [h.team_key(9001), "sports:openfootball:team:exl-1:northbridge-rovers-fc"]
        )
    )
    candidate = found[pair]
    assert (
        candidate["basis"] == "name-jurisdiction" and candidate["state"] == "proposed"
    )
    assert candidate["evidence"][0]["fields"] == ["name", "country"]
    assert identity.linked("global", h.team_key(9001)) == [
        h.team_key(9001)
    ]  # nothing grouped before review
    accepted = identity.review(
        "global",
        candidate["candidate_id"],
        "accept",
        "same club, same league",
        principal_id="rev",
        scopes=h.REVIEW_SCOPES,
    )
    assert accepted["state"] == "accepted" and accepted["decision_id"]
    assert identity.linked("global", h.team_key(9001)) == list(pair)
    reverted = identity.revert(
        "global",
        candidate["candidate_id"],
        "wrong",
        principal_id="rev",
        scopes=h.REVIEW_SCOPES,
    )
    assert reverted["state"] == "reverted" and identity.linked(
        "global", h.team_key(9001)
    ) == [h.team_key(9001)]
    # Re-proposing with nothing new never reactivates the reverted decision.
    again = by_records(
        identity.propose("global", principal_id="alice", scopes=h.REVIEW_SCOPES)[
            "candidates"
        ]
    )
    assert again[pair]["state"] == "reverted"


def test_two_same_named_players_are_never_merged_and_a_name_alone_is_never_acceptable(
    env,
):
    conn, identity = env
    candidates = identity.propose(
        "global", principal_id="alice", scopes=h.REVIEW_SCOPES
    )["candidates"]
    jan = [c for c in candidates if all(":player:" in r for r in c["records"])]
    fd_pair = tuple(
        sorted(
            ["sports:football-data:player:50001", "sports:football-data:player:50003"]
        )
    )
    # Same provider, different ids and different published birth dates: two people, never proposed.
    assert fd_pair not in by_records(jan)
    # StatsBomb publishes no date of birth: only a similar-name candidate, which review cannot accept.
    similar = [c for c in jan if c["basis"] == "similar-name"]
    assert similar
    with pytest.raises(OwnershipError) as caught:
        identity.review(
            "global",
            similar[0]["candidate_id"],
            "accept",
            "looks right",
            principal_id="rev",
            scopes=h.REVIEW_SCOPES,
        )
    assert caught.value.code == "insufficient_evidence"


def test_a_published_birth_date_and_name_propose_a_player_match_across_sources(env):
    conn, identity = env
    store = SportsStore(conn, initialize=False)
    store.record_manual(
        "global",
        "transfers-official",
        [
            {
                "record_type": "player",
                "record_key": "sports:transfers-official:player:jan-example:2075-04-12",
                "source_record_id": "announcement-1",
                "locator": "https://www.example-fa.example.org/transfers/1",
                "body": {
                    "provider": "transfers-official",
                    "name": "Jan Example",
                    "birth_date": "2075-04-12",
                },
            }
        ],
        attribution="Example Football Association",
        principal_id="op",
        citation={
            "url": "https://www.example-fa.example.org/transfers/1",
            "published_at": "2099-09-01",
        },
    )
    found = by_records(
        identity.propose("global", principal_id="alice", scopes=h.REVIEW_SCOPES)[
            "candidates"
        ]
    )
    pair = tuple(
        sorted(
            [
                "sports:football-data:player:50001",
                "sports:transfers-official:player:jan-example:2075-04-12",
            ]
        )
    )
    assert found[pair]["basis"] == "name-jurisdiction"
    assert found[pair]["evidence"][0]["fields"] == ["name", "birth_date"]
    other = tuple(
        sorted(
            [
                "sports:football-data:player:50003",
                "sports:transfers-official:player:jan-example:2075-04-12",
            ]
        )
    )
    assert other not in found  # same name, different published date of birth


def test_a_renamed_club_is_an_identity_history_event_never_an_automatic_merge(env):
    conn, identity = env
    h.apply(conn, "openfootball", "openfootball_exl_2099_later.json", at="2099-09-06")
    old, new = (
        "sports:openfootball:team:exl-1:westvale-town",
        "sports:openfootball:team:exl-1:westvale-city",
    )
    candidates = identity.propose(
        "global", principal_id="alice", scopes=h.REVIEW_SCOPES
    )["candidates"]
    assert tuple(sorted([old, new])) not in by_records(candidates)
    assert identity.linked("global", old) == [old]
    with pytest.raises(SportsError):
        identity.record_lineage(
            "global",
            kind="renamed",
            subject_key=old,
            object_key=new,
            valid_on="2099-09-01",
            source={"url": "http://x", "title": "t"},
            reason="r",
            principal_id="rev",
            scopes=h.REVIEW_SCOPES,
        )
    event = identity.record_lineage(
        "global",
        kind="renamed",
        subject_key=old,
        object_key=new,
        valid_on="2099-09-01",
        source=SOURCE,
        reason="club announcement",
        principal_id="rev",
        scopes=h.REVIEW_SCOPES,
    )
    assert event["decision_type"] == "alias" and event["relation"] == "renamed"
    (listed,) = identity.lineage("global", old, scopes=h.REVIEW_SCOPES)
    assert listed["source"] == SOURCE and listed["undone"] is False
    # Still two records; the lineage never groups them for queries.
    assert identity.linked("global", old) == [old]
    identity.undo_lineage(
        "global", event["decision_id"], principal_id="rev", scopes=h.REVIEW_SCOPES
    )
    assert identity.lineage("global", old, scopes=h.REVIEW_SCOPES)[0]["undone"] is True
    phoenix = identity.record_lineage(
        "global",
        kind="phoenix_of",
        subject_key=old,
        object_key=h.team_key(9004),
        valid_on="2099-09-02",
        source=SOURCE,
        reason="r",
        principal_id="rev",
        scopes=h.REVIEW_SCOPES,
    )
    assert phoenix["decision_type"] == "non-match"


def test_seasons_and_governing_bodies_propose_but_players_never_reach_canonical_entities(
    env,
):
    conn, identity = env
    for name, kind in (
        ("Example Football Association", "ORG"),
        ("Jan Example", "PERSON"),
        ("Northbridge Rovers", "ORG"),
    ):
        add_manual_alias(conn, name, name, kind)
    candidates = identity.propose(
        "global", principal_id="alice", scopes=h.REVIEW_SCOPES
    )["candidates"]
    canonical_links = [
        c for c in candidates if any(r.startswith("canonical:") for r in c["records"])
    ]
    subjects = {
        r for c in canonical_links for r in c["records"] if r.startswith("sports:")
    }
    assert h.team_key(9001) in subjects and h.SEASON in subjects
    assert not any(":player:" in s for s in subjects)
    assert all(c["basis"] == "similar-name" for c in canonical_links)
    seasons = [c for c in candidates if all(":season:" in r for r in c["records"])]
    assert {tuple(sorted(c["records"])) for c in seasons} >= {
        tuple(sorted([h.SEASON, h.OF_SEASON]))
    }


def test_venues_resolve_only_through_a_reviewable_place_resolution(env):
    conn, identity = env
    from src.kb.geospatial import GeospatialStore

    geo = GeospatialStore(conn, now=lambda: to_ms("2099-09-10"))
    scopes = h.REVIEW_SCOPES | {
        "knowledge:geospatial:read",
        "knowledge:geospatial:write",
        "knowledge:geospatial:review",
    }
    place = geo.register_place(
        "global",
        "Harbour Park",
        "venue",
        names=[{"value": "Harbour Park", "language": "en"}],
        source_ids={"fixture": "harbour-park"},
        parent_ids=[],
        principal_id="op",
        scopes=scopes,
    )
    venue = "sports:statsbomb-open-data:venue:5001"
    linked = identity.link_venue_place(
        "global", venue, geo_namespace="global", principal_id="alice", scopes=scopes
    )
    assert (
        linked["state"] == "unresolved"
        and linked["links"][0]["review_state"] == "unreviewed"
    )
    resolution = linked["resolution"]["resolution_id"]
    geo.review(
        "global",
        resolution,
        "accept",
        selected_place_id=place["place_id"],
        reason="same ground",
        principal_id="rev",
        scopes=scopes,
    )
    assert (
        identity.place("global", venue, scopes=scopes)["place_id"] == place["place_id"]
    )
    conn.execute(
        'UPDATE sports_revisions SET body_json=\'{"name":"Nowhere Ground","provider":"x"}\' '
        "WHERE record_type='venue' AND record_key=?",
        [venue],
    )
    with pytest.raises(SportsError, match="never by name alone"):
        identity.link_venue_place(
            "global", venue, geo_namespace="global", principal_id="alice", scopes=scopes
        )


def test_proposals_are_not_ready_before_any_source_ran():
    conn = h.connection()
    with pytest.raises(SportsError) as caught:
        SportsIdentity(conn).propose(
            "global", principal_id="alice", scopes=h.REVIEW_SCOPES
        )
    assert caught.value.code == "not_ready"
