"""Offline harness for the Sports pack (#2135): authored publications replayed through the real adapter.

Every file under ``tests/fixtures/sports`` is authored in the provider's
documented shape and names fictional clubs, players, tournaments and Games
only; nothing here is live coverage. Publications go through
:class:`SportsResultsAdapter` (the connector the runtime compiles) and
:class:`SportsProjector`, with the retrieval time set explicitly so no test
depends on the wall clock.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from urllib.parse import urlsplit

import duckdb

from src.ingestion.sports_sources import (
    FIXTURE_SECRET,
    SportsResultsAdapter,
    fixture_transport,
    to_ms,
)
from src.ingestion.source_packs import validate_source_pack
from src.kb.sports_store import SportsProjector

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/sports"
PACK = ROOT / "config/source_packs/sports.json"
NS = "global"
READ = "knowledge:sports:read"
WRITE = "knowledge:sports:write"
REVIEW = "knowledge:sports:review"
SCOPES = {
    READ,
    WRITE,
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
    "knowledge:ownership:read",
    "knowledge:ownership:write",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:forecasts:read",
    "knowledge:forecasts:write",
}
REVIEW_SCOPES = SCOPES | {REVIEW, "knowledge:ownership:review"}
READ_ONLY = {READ, f"namespace:{NS}:read"}
SOURCES = {
    "fd-matches": "football-data-pl-matches",
    "fd-standings": "football-data-pl-standings",
    "fd-teams": "football-data-pl-teams",
    "openfootball": "openfootball-en1",
    "atp": "sackmann-atp-matches",
    "statsbomb": "statsbomb-open-data",
}
EXL = {
    "code": "EXL",
    "name": "Example League",
    "governing_body": "Example Football Association",
    "area": "Exampleland",
}
SEASON = "sports:football-data:season:exl:2099"
OF_SEASON = "sports:openfootball:season:exl-1:2099-00"


def fixture_key(match_id: int) -> str:
    return f"sports:football-data:fixture:{match_id}"


def team_key(team_id: int) -> str:
    return f"sports:football-data:team:{team_id}"


def connection():
    return duckdb.connect(":memory:")


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(key: str) -> dict:
    item = copy.deepcopy(
        next(s for s in manifest()["sources"] if s["source_id"] == SOURCES[key])
    )
    if key.startswith("fd-"):
        # The fictional fixtures belong to a fictional league, not to the manifest's live competition.
        item["sports"]["competition"] = dict(EXL)
        item["sports"]["season"] = "2099"
    if key == "openfootball":
        item["sports"]["competition"] = {
            "code": "exl.1",
            "name": "Example League",
            "area": "Exampleland",
        }
        item["sports"]["season"] = "2099-00"
    return item


def page(
    key: str,
    filename: str | None,
    *,
    headers: dict | None = None,
    item: dict | None = None,
    body=None,
):
    item = item or source(key)
    parts = urlsplit(item["endpoint"])
    files = item["sports"].get("files")
    if files:
        pages = [
            {
                "request": parts.path.rstrip("/") + "/" + rel,
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "body": (FIXTURES / name).read_text(),
            }
            for rel, name in zip(files, filename.split(","))
        ]
    else:
        pages = [
            {
                "request": parts.path + ("?" + parts.query if parts.query else ""),
                "status": 200,
                "headers": headers or {"Content-Type": "application/json"},
                "body": body if body is not None else (FIXTURES / filename).read_text(),
            }
        ]
    adapter = SportsResultsAdapter(
        item, transport=fixture_transport(pages), secret=FIXTURE_SECRET
    )
    out, cursor = [], None
    while True:
        fetched = adapter.fetch_page(
            {"operation": item["operations"][0], "parameters": {}, "limit": 5000},
            cursor=cursor,
        )
        out.append(fetched)
        cursor = fetched.next_cursor
        if cursor is None:
            return out, item


def apply(
    conn,
    key: str,
    filename: str | None,
    *,
    at: str,
    headers: dict | None = None,
    item=None,
    body=None,
):
    """Acquire one publication as if retrieved at ``at`` (ISO date or date-time)."""
    pages, item = page(key, filename, headers=headers, item=item, body=body)
    projector = SportsProjector(conn)
    projector.store.now = lambda: to_ms(at)
    results = []
    for fetched in pages:
        results += projector.project_page(
            run_id=f"run:{filename}:{at}",
            manifest=None,
            source=item,
            records=fetched.records,
            documents=[],
            page_receipt=fetched.receipt,
            principal_id="operator",
        )
    return results


def standings_headers(day: str) -> dict:
    from datetime import date

    stamp = date.fromisoformat(day).strftime("%a, %d %b %Y 21:00:00 GMT")
    return {"Content-Type": "application/json", "Date": stamp}


def load_league(conn) -> None:
    """The fictional Example League season 2099 as three football-data polls, teams and one published table."""
    apply(conn, "fd-teams", "fd_exl_2099_teams.json", at="2099-08-01")
    apply(conn, "fd-matches", "fd_exl_2099_poll1.json", at="2099-08-18")
    apply(conn, "fd-matches", "fd_exl_2099_poll2.json", at="2099-08-25")
    apply(conn, "fd-matches", "fd_exl_2099_poll3.json", at="2099-09-04T21:00:00Z")
    apply(
        conn,
        "fd-standings",
        "fd_exl_2099_standings.json",
        at="2099-09-04T21:05:00Z",
        headers=standings_headers("2099-09-04"),
    )


FA = "Example Football Association"
FORFEIT_URL = (
    "https://www.example-fa.example.org/decisions/2099-017-westvale-ineligible-player"
)
DEDUCTION_URL = (
    "https://www.example-fa.example.org/decisions/2099-015-southport-financial-rules"
)


def record_decisions(conn) -> None:
    """The governing body's points deduction (published 5 September) and forfeit (published 10 September)."""
    from src.kb.sports_store import record_result_decision, record_table_rule

    record_table_rule(
        conn,
        NS,
        SEASON,
        "deduction:southport-2099-015",
        {
            "kind": "deduction",
            "team_key": team_key(9003),
            "points_deducted": 3,
            "effective_on": "2099-09-05",
            "deciding_body": FA,
            "decision": {"url": DEDUCTION_URL, "title": "Decision 2099/015"},
        },
        published_at="2099-09-05T12:00:00Z",
        citation_url=DEDUCTION_URL,
        attribution=FA,
        principal_id="operator",
        now=lambda: to_ms("2099-09-05T13:00:00Z"),
    )
    record_result_decision(
        conn,
        NS,
        fixture_key(105),
        status="forfeit_awarded",
        score={"home": 3, "away": 0},
        deciding_body=FA,
        decision={"url": FORFEIT_URL, "title": "Decision 2099/017: match awarded 3-0"},
        published_at="2099-09-10T09:00:00Z",
        principal_id="operator",
        now=lambda: to_ms("2099-09-10T10:00:00Z"),
    )


class Clock:
    """A deterministic clock in the fixtures' fictional timeline (2099)."""

    def __init__(self, start: str = "2099-08-01") -> None:
        self.value = to_ms(start)

    def set(self, at: str) -> None:
        self.value = to_ms(at)

    def __call__(self) -> int:
        self.value += 1000
        return self.value
