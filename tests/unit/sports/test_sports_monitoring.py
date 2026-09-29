"""Sports monitors on subscriptions: reschedules, results, corrections, forfeits and table changes (#2145, SP10)."""

from __future__ import annotations

import pytest

from src.ingestion.sports_sources import to_ms
from src.kb.sports_forecasts import SportsForecasts
from src.kb.sports_monitoring import SportsMonitor
from src.kb.sports_records import SportsError, forbidden_keys
from src.kb.sports_store import SportsStore
from src.kb.subscriptions import SubscriptionStore
from tests.unit.sports import harness as h

FNS = "forecasts"
SCOPES = h.SCOPES | {f"namespace:{FNS}:read", f"namespace:{FNS}:write"}


@pytest.fixture()
def env():
    conn = h.connection()
    h.apply(conn, "fd-teams", "fd_exl_2099_teams.json", at="2099-08-01")
    h.apply(conn, "fd-matches", "fd_exl_2099_poll1.json", at="2099-08-18")
    clock = h.Clock("2099-08-18T12:00:00Z")
    yield conn, clock, SportsMonitor(conn, now=clock)
    conn.close()


def run(monitor, watch, watermark):
    SubscriptionStore(monitor.conn).commit_watermark("global", watermark)
    return monitor.run(watch["subscription_id"], principal_id="alice", scopes=SCOPES)


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def test_two_polls_notify_the_reschedule_and_the_correction_citing_both_revisions(env):
    conn, clock, monitor = env
    watch = monitor.create(
        "global",
        "match-103",
        watch="match",
        key=h.fixture_key(103),
        principal_id="alice",
        scopes=SCOPES,
    )
    moved = monitor.create(
        "global",
        "match-104",
        watch="match",
        key=h.fixture_key(104),
        principal_id="alice",
        scopes=SCOPES,
    )
    assert kinds(run(monitor, watch, 1)) == ["result_published"]
    assert kinds(run(monitor, moved, 1)) == []
    h.apply(conn, "fd-matches", "fd_exl_2099_poll2.json", at="2099-08-25")
    (corrected,) = run(monitor, watch, 2)["notifications"]
    assert corrected["kind"] == "result_corrected"
    assert corrected["cites"]["old"]["score"] == {"home": 1, "away": 1}
    assert corrected["cites"]["new"]["score"] == {"home": 2, "away": 1}
    assert corrected["cites"]["new"]["source"]["provider"] == "football-data"
    (rescheduled,) = run(monitor, moved, 2)["notifications"]
    assert (
        rescheduled["kind"] == "fixture_rescheduled"
        and rescheduled["status"] == "rescheduled"
    )
    assert (rescheduled["from"], rescheduled["to"]) == (
        "2099-08-17T14:00:00Z",
        "2099-09-03T18:30:00Z",
    )
    assert rescheduled["cites"]["old"]["status"] == "postponed"
    assert forbidden_keys(corrected) == [] and forbidden_keys(rescheduled) == []


def test_competition_watch_reports_table_changes_forfeits_and_resolvable_forecasts(env):
    conn, clock, monitor = env
    forecasts = SportsForecasts(conn, now=h.Clock("2099-08-18"))
    forecast = forecasts.register(
        "global",
        FNS,
        "rovers-105",
        rule={
            "kind": "match_outcome",
            "fixture_key": h.fixture_key(105),
            "outcome": "home_win",
        },
        probability=0.4,
        resolution_at_ms=to_ms("2099-08-31T18:00:00Z"),
        evidence=[],
        principal_id="alice",
        scopes=SCOPES,
    )
    watch = monitor.create(
        "global",
        "league",
        watch="competition",
        key=h.SEASON,
        principal_id="alice",
        scopes=SCOPES,
    )
    run(monitor, watch, 1)
    h.apply(conn, "fd-matches", "fd_exl_2099_poll3.json", at="2099-09-04T21:00:00Z")
    clock.set("2099-09-05")
    second = run(monitor, watch, 2)
    assert "table_changed" in kinds(second)
    resolvable = [
        n for n in second["notifications"] if n["kind"] == "forecast_resolvable"
    ]
    assert resolvable[0]["forecasts"] == [
        {"forecast_namespace": FNS, "forecast_id": forecast["forecast_id"]}
    ]
    h.record_decisions(conn)
    clock.set("2099-09-11")
    third = run(monitor, watch, 3)
    assert "forfeit_awarded" in kinds(third) and "table_changed" in kinds(third)
    forfeit = next(n for n in third["notifications"] if n["kind"] == "forfeit_awarded")
    assert forfeit["cites"]["new"]["deciding_body"] == h.FA and forfeit["cites"]["old"][
        "score"
    ] == {"home": 0, "away": 2}
    table = next(n for n in third["notifications"] if n["kind"] == "table_changed")
    assert {r["team_name"] for r in table["changed_rows"]} >= {
        "Northbridge Rovers",
        "Southport United",
    }


def test_a_late_older_publication_is_history_never_a_correction():
    fresh = h.connection()
    h.apply(fresh, "fd-teams", "fd_exl_2099_teams.json", at="2099-08-01")
    h.apply(fresh, "fd-matches", "fd_exl_2099_poll2.json", at="2099-08-25")
    late = SportsMonitor(fresh, now=h.Clock("2099-08-26"))
    watch = late.create(
        "global",
        "match-103",
        watch="match",
        key=h.fixture_key(103),
        principal_id="alice",
        scopes=SCOPES,
    )
    assert kinds(run(late, watch, 1)) == ["result_published"]
    h.apply(
        fresh, "fd-matches", "fd_exl_2099_poll1.json", at="2099-08-27"
    )  # the older poll arrives last
    result = run(late, watch, 2)
    # The older 1-1 lands as history (backfilled, never a correction). The older schedule states what the later
    # one restated, so the history now starts with it and nothing moved: no notification either way.
    assert result["notifications"] == [] and result["backfilled_history"] == 1
    history = SportsStore(fresh, initialize=False).labelled_history(
        "global", "match_result_revision", h.fixture_key(103)
    )
    assert [r["status"] for r in history] == ["official", "corrected"]


def test_monitors_need_a_known_target_and_an_acquired_source():
    conn = h.connection()
    with pytest.raises(SportsError) as caught:
        SportsMonitor(conn).create(
            "global",
            "x",
            watch="match",
            key="sports:x",
            principal_id="alice",
            scopes=SCOPES,
        )
    assert caught.value.code == "not_ready"
