"""Matches, teams and transfers linked to News, Geospatial and entity records by citation (#2144, SP09)."""

from __future__ import annotations

import json

import pytest

from src.database.local_warehouse_seed import _SCHEMA as WAREHOUSE
from src.ingestion.document_store import _SCHEMA as DOCUMENTS
from src.ingestion.sports_sources import to_ms
from src.kb.sports_identity import SportsIdentity
from src.kb.sports_links import SportsLinks
from src.kb.sports_queries import SportsQueries
from src.kb.sports_records import SportsError
from tests.unit.sports import harness as h

SCOPES = h.REVIEW_SCOPES | {"knowledge:read"}
ANNOUNCEMENT = {
    "kind": "club",
    "url": "https://rovers.example.org/news/jan-example-joins-athletic",
    "title": "Jan Example joins Eastfield Athletic",
    "publisher": "Northbridge Rovers",
    "published_at": "2099-09-01",
}


def article(conn, document_id, day, title, content, actors=()):
    conn.execute(
        "INSERT INTO documents (document_id, source_type, language, ingested_at, created_at, source_id, url, "
        "content_hash, title, content, metadata) VALUES (?, 'news', 'en', ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            document_id,
            to_ms(day),
            to_ms(day),
            "Example Daily",
            f"https://news.example.org/{document_id}",
            f"sha-{document_id}",
            title,
            content,
            json.dumps({"outlet": "Example Daily"}),
        ],
    )
    for name in actors:
        conn.execute(
            "INSERT INTO document_actors VALUES (?, 'news', ?, NULL, 'subject', 0.9, NULL)",
            [document_id, name],
        )


@pytest.fixture()
def env():
    conn = h.connection()
    conn.execute(DOCUMENTS)
    conn.execute(WAREHOUSE)
    h.load_league(conn)
    article(
        conn,
        "doc-report",
        "2099-08-17",
        "Rovers and United share the points",
        "Example League report from Harbour Park.",
        actors=("Northbridge Rovers", "Southport United"),
    )
    article(
        conn,
        "doc-keyword",
        "2099-08-17",
        "Northbridge Rovers v Southport United preview",
        "Example League preview without extracted mentions.",
    )
    article(
        conn,
        "doc-other-league",
        "2099-08-17",
        "Rovers and United in a friendly",
        "A friendly match.",
        actors=("Northbridge Rovers", "Southport United"),
    )
    article(
        conn,
        "doc-late",
        "2099-08-30",
        "Rovers and United rematch talk",
        "Example League.",
        actors=("Northbridge Rovers", "Southport United"),
    )
    yield conn, SportsLinks(conn, now=h.Clock("2099-09-10"))
    conn.close()


def test_news_is_linked_only_by_reviewable_candidates_from_mentions_date_and_competition(
    env,
):
    conn, links = env
    refreshed = links.refresh_news_links(
        "global",
        target_type="fixture",
        target_key=h.fixture_key(103),
        principal_id="alice",
        scopes=SCOPES,
    )
    (candidate,) = refreshed["links"]
    assert (
        candidate["document_id"] == "doc-report" and candidate["state"] == "candidate"
    )
    assert refreshed["keyword_only_not_linked"] == 1
    assert candidate["evidence"]["competition"] == "example league"
    assert (
        candidate["evidence"]["article"]["source_revision"]["content_hash"]
        == "sha-doc-report"
    )
    # Refreshing again offers nothing new; review, then revert, both keep the evidence.
    assert (
        links.refresh_news_links(
            "global",
            target_type="fixture",
            target_key=h.fixture_key(103),
            principal_id="alice",
            scopes=SCOPES,
        )["created"]
        == []
    )
    linked = links.review(
        "global",
        candidate["link_id"],
        "accept",
        "match report",
        principal_id="rev",
        scopes=SCOPES,
    )
    assert linked["state"] == "linked" and linked["evidence"] == candidate["evidence"]
    reverted = links.revert(
        "global", candidate["link_id"], "wrong match", principal_id="rev", scopes=SCOPES
    )
    assert reverted["state"] == "reverted" and reverted["revision_no"] == 3
    with pytest.raises(SportsError) as caught:
        links.refresh_news_links(
            "global",
            target_type="fixture",
            target_key=h.fixture_key(103),
            principal_id="alice",
            scopes=h.REVIEW_SCOPES,
        )
    assert caught.value.code == "unauthorized"


def test_transfers_come_only_from_open_announcements_and_news_reports_stay_news_claims(
    env,
):
    conn, links = env
    with pytest.raises(SportsError) as caught:
        links.record_transfer(
            "global",
            player_name="Jan Example",
            to_team_key=h.team_key(9002),
            date="2099-09-01",
            source={**ANNOUNCEMENT, "kind": "news"},
            principal_id="alice",
            scopes=SCOPES,
        )
    assert caught.value.code == "not_openly_published"
    with pytest.raises(SportsError, match="fee"):
        links.record_transfer(
            "global",
            player_name="Jan Example",
            to_team_key=h.team_key(9002),
            date="2099-09-01",
            source=ANNOUNCEMENT,
            fee={"amount": "1000000", "currency": "EUR"},
            principal_id="alice",
            scopes=SCOPES,
        )
    recorded = links.record_transfer(
        "global",
        player_name="Jan Example",
        birth_date="2075-04-12",
        from_team_key=h.team_key(9001),
        to_team_key=h.team_key(9002),
        date="2099-09-01",
        source=ANNOUNCEMENT,
        principal_id="alice",
        scopes=SCOPES,
    )
    again = links.record_transfer(
        "global",
        player_name="Jan Example",
        birth_date="2075-04-12",
        from_team_key=h.team_key(9001),
        to_team_key=h.team_key(9002),
        date="2099-09-01",
        source=ANNOUNCEMENT,
        principal_id="alice",
        scopes=SCOPES,
    )
    assert again["status"] == "unchanged"
    (transfer,) = SportsQueries(conn).transfers(
        "global", recorded["player_key"], scopes=SCOPES
    )["transfers"]
    assert transfer["source"]["url"] == ANNOUNCEMENT["url"] and "fee" not in transfer
    # The announced player and the football-data squad member are one person only after review.
    identity = SportsIdentity(conn, now=h.Clock("2099-09-10"))
    candidates = identity.propose("global", principal_id="alice", scopes=SCOPES)[
        "candidates"
    ]
    pair = [
        c
        for c in candidates
        if set(c["records"])
        == {recorded["player_key"], "sports:football-data:player:50001"}
    ]
    assert pair and pair[0]["basis"] == "name-jurisdiction"
    article(
        conn,
        "doc-transfer",
        "2099-09-01",
        "Jan Example signs for Athletic",
        "Transfer news.",
        actors=("Jan Example", "Eastfield Athletic"),
    )
    offered = links.refresh_news_links(
        "global",
        target_type="transfer",
        target_key=recorded["transfer_key"],
        principal_id="alice",
        scopes=SCOPES,
    )
    assert [link["document_id"] for link in offered["links"]] == ["doc-transfer"]
