"""Sports forecasts in the binary forecast ledger, resolved only against official results (#2143, SP08)."""

from __future__ import annotations

import json

import pytest

from src.ingestion.sports_sources import to_ms
from src.kb.forecasts import ForecastStore
from src.kb.sports_forecasts import SportsForecasts
from src.kb.sports_records import SportsError, forbidden_keys
from tests.unit.sports import harness as h

FNS = "forecasts"
SCOPES = h.SCOPES | {f"namespace:{FNS}:read", f"namespace:{FNS}:write"}


@pytest.fixture()
def env():
    conn = h.connection()
    h.apply(conn, "fd-teams", "fd_exl_2099_teams.json", at="2099-08-01")
    clock = h.Clock("2099-08-01")
    yield conn, clock, SportsForecasts(conn, now=clock)
    conn.close()


def provisional_poll() -> str:
    data = json.loads((h.FIXTURES / "fd_exl_2099_poll1.json").read_text())
    match = data["matches"][2]
    match.update(status="IN_PLAY", lastUpdated="2099-08-17T15:00:00Z")
    match["score"]["fullTime"] = {"home": 1, "away": 0}
    return json.dumps(data)


def register(forecasts, key, rule, resolution="2099-08-17T18:00:00Z", probability=0.6):
    return forecasts.register(
        "global",
        FNS,
        key,
        rule=rule,
        probability=probability,
        resolution_at_ms=to_ms(resolution),
        evidence=[],
        principal_id="alice",
        scopes=SCOPES,
    )


def propose(conn, clock, forecast, at):
    clock.set(at)
    return ForecastStore(conn, now=clock).propose_resolution(
        FNS, forecast["forecast_id"], principal_id="alice", scopes=SCOPES
    )


def test_provisional_then_official_then_a_correction_after_resolution_is_a_reviewable_re_resolution(
    env,
):
    conn, clock, forecasts = env
    h.apply(
        conn, "fd-matches", None, at="2099-08-17T15:10:00Z", body=provisional_poll()
    )
    forecast = register(
        forecasts,
        "rovers-beat-united",
        {
            "kind": "match_outcome",
            "fixture_key": h.fixture_key(103),
            "outcome": "home_win",
        },
    )
    assert (
        forecast["sports_rule"]["rule"]["outcome"] == "home_win"
        and forecast["probability"] == 0.6
    )
    assert (
        propose(conn, clock, forecast, "2099-08-17T17:00:00Z")["reason"]
        == "resolution-not-due"
    )
    pending = propose(conn, clock, forecast, "2099-08-17T20:00:00Z")
    assert (
        pending["status"] == "unresolved" and pending["reason"] == "no-official-result"
    )
    assert "provisional result never" in pending["note"]
    h.apply(conn, "fd-matches", "fd_exl_2099_poll1.json", at="2099-08-18")
    official = propose(conn, clock, forecast, "2099-08-18T10:00:00Z")
    assert (
        official["status"] == "proposed"
        and official["proposed_outcome"] == 0
        and official["requires_review"]
    )
    ledger = ForecastStore(conn, now=clock)
    ledger.resolve(
        FNS,
        forecast["forecast_id"],
        0,
        status="resolved",
        outcome=0,
        evidence=official["evidence"],
        rationale="official 1-1",
        forecast_revision=1,
        principal_id="alice",
        scopes=SCOPES,
    )
    h.apply(conn, "fd-matches", "fd_exl_2099_poll2.json", at="2099-08-25")
    corrected = propose(conn, clock, forecast, "2099-08-25T10:00:00Z")
    assert (
        corrected["proposed_outcome"] == 1
        and corrected["official_revisions"][-1]["status"] == "official"
    )
    assert corrected["re_resolution"]["resolved_outcome"] == 0
    # Nothing flipped: the ledger keeps the reviewed outcome until a reviewer re-resolves.
    assert (
        ledger.inspect(
            FNS, forecast["forecast_id"], principal_id="alice", scopes=SCOPES
        )["outcome"]["outcome"]
        == 0
    )
    assert forbidden_keys(corrected) == []


def test_postponed_matches_stay_unresolved_with_the_reason(env):
    conn, clock, forecasts = env
    h.apply(conn, "fd-matches", "fd_exl_2099_poll1.json", at="2099-08-18")
    forecast = register(
        forecasts,
        "athletic-town",
        {"kind": "match_outcome", "fixture_key": h.fixture_key(104), "outcome": "draw"},
        resolution="2099-08-18T12:00:00Z",
    )
    assert propose(conn, clock, forecast, "2099-08-19")["reason"] == "fixture-postponed"


def test_advancement_and_final_position_resolve_on_forfeits_and_the_official_table(env):
    conn, clock, forecasts = env
    h.apply(conn, "fd-matches", "fd_exl_2099_poll1.json", at="2099-08-18")
    advance = register(
        forecasts,
        "rovers-win-105",
        {
            "kind": "advancement",
            "fixture_key": h.fixture_key(105),
            "team_key": h.team_key(9001),
        },
        resolution="2099-09-01T00:00:00Z",
    )
    top = register(
        forecasts,
        "rovers-top",
        {
            "kind": "final_position",
            "season_key": h.SEASON,
            "team_key": h.team_key(9001),
            "position_at_most": 1,
        },
        resolution="2099-09-01T00:00:00Z",
        probability=0.5,
    )
    assert propose(conn, clock, top, "2099-09-02")["reason"].startswith(
        "season-incomplete"
    )
    for name, at in (
        ("fd_exl_2099_poll2.json", "2099-08-25"),
        ("fd_exl_2099_poll3.json", "2099-09-04T21:00:00Z"),
    ):
        h.apply(conn, "fd-matches", name, at=at)
    assert (
        propose(conn, clock, advance, "2099-09-05")["proposed_outcome"] == 0
    )  # lost 0-2 on the pitch
    h.record_decisions(conn)
    awarded = propose(conn, clock, advance, "2099-09-11")
    assert awarded["proposed_outcome"] == 1
    assert awarded["official_revisions"][-1]["deciding_body"] == h.FA
    final = propose(conn, clock, top, "2099-09-11")
    assert final["status"] == "proposed" and final["proposed_outcome"] == 1


def test_noesis_never_produces_a_forecast_and_scoring_states_its_cutoff(env):
    conn, clock, forecasts = env
    h.apply(conn, "fd-matches", "fd_exl_2099_poll1.json", at="2099-08-18")
    with pytest.raises(SportsError) as caught:
        register(
            forecasts,
            "none",
            {
                "kind": "match_outcome",
                "fixture_key": h.fixture_key(101),
                "outcome": "home_win",
            },
            probability=None,
        )
    assert caught.value.code == "forecast_refused"
    with pytest.raises(SportsError):
        register(
            forecasts,
            "odds",
            {
                "kind": "match_outcome",
                "fixture_key": h.fixture_key(101),
                "outcome": "home_win",
                "odds": 1.5,
            },
        )
    forecast = register(
        forecasts,
        "rovers-101",
        {
            "kind": "match_outcome",
            "fixture_key": h.fixture_key(101),
            "outcome": "home_win",
        },
        resolution="2099-08-10T12:00:00Z",
        probability=0.7,
    )
    proposal = propose(conn, clock, forecast, "2099-08-19")
    ForecastStore(conn, now=clock).resolve(
        FNS,
        forecast["forecast_id"],
        0,
        status="resolved",
        outcome=1,
        evidence=proposal["evidence"],
        rationale="official",
        forecast_revision=1,
        principal_id="alice",
        scopes=SCOPES,
    )
    scored = forecasts.score(
        FNS, [forecast["forecast_id"]], principal_id="alice", scopes=SCOPES
    )
    assert (
        scored["cutoff_ms"] == to_ms("2099-08-10T16:10:00Z")
        and scored["scored_count"] == 1
    )
    assert "official result publication" in scored["cutoff_basis"]
    assert abs(scored["mean_brier"] - 0.09) < 1e-9
