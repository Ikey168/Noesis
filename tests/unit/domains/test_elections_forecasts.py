"""Election forecasts in the existing binary forecast ledger, resolved only against certified results (#1974)."""

from __future__ import annotations

import pytest

from src.ingestion.election_sources import day_ms
from src.kb.elections import ElectionError
from src.kb.elections_forecasts import ElectionForecasts
from src.kb.forecasts import ForecastError, ForecastStore
from tests.unit import elections_harness as h

FORECAST_NS = "forecasts"
SCOPES = h.SCOPES | {
    "knowledge:forecasts:read",
    "knowledge:forecasts:write",
    f"namespace:{FORECAST_NS}:read",
    f"namespace:{FORECAST_NS}:write",
}
EVIDENCE = [
    {"kind": "source", "id": "my-notes", "revision": "1", "namespace": FORECAST_NS}
]


class Clock:
    def __init__(self, day: str) -> None:
        self.value = day_ms(day)

    def __call__(self) -> int:
        return self.value

    def set(self, day: str) -> None:
        self.value = day_ms(day)


@pytest.fixture()
def env():
    conn = h.connection()
    h.apply(conn, "de-btw", h.DE_PRELIMINARY)
    clock = Clock("2099-02-01")
    forecasts = ElectionForecasts(conn, now=clock)
    contest = h.contest_id(conn, h.DE_ELECTION, "de-bt-wahlkreis", "001", "first-vote")
    yield conn, clock, forecasts, contest
    conn.close()


def register(forecasts, contest, key="f1", rule=None, probability=0.6):
    return forecasts.register(
        "global",
        FORECAST_NS,
        key,
        contest_id=contest,
        rule=rule or {"kind": "winner", "entry": "party:beispielpartei"},
        probability=probability,
        resolution_at_ms=day_ms("2099-03-25"),
        evidence=EVIDENCE,
        principal_id="alice",
        scopes=SCOPES,
    )


def test_forecasts_are_the_users_own_and_name_the_contest_and_certified_kind(env):
    conn, _, forecasts, contest = env
    created = register(forecasts, contest)
    assert (
        created["probability"] == 0.6 and created["owner"] == "alice"
    )  # the user's probability, not the pack's
    assert contest in created["outcome_rule"] and "certified" in created["outcome_rule"]
    assert created["election_rule"]["vintage_kind"] == "certified"
    again = register(forecasts, contest)
    assert again["idempotent"] is True
    with pytest.raises(ElectionError):
        register(
            forecasts,
            contest,
            key="bad",
            rule={"kind": "winner", "entry": "Beispielpartei"},
        )
    assert conn.execute("SELECT count(*) FROM research_forecasts").fetchone()[0] == 1


def test_preliminary_results_never_propose_and_certified_results_do(env):
    conn, clock, forecasts, contest = env
    created = register(forecasts, contest)
    ledger = ForecastStore(conn, now=clock)
    clock.set("2099-03-10")
    early = ledger.propose_resolution(
        FORECAST_NS, created["forecast_id"], principal_id="alice", scopes=SCOPES
    )
    assert early["reason"] == "resolution-not-due" and early["status"] == "unresolved"
    clock.set("2099-03-26")
    pending = ledger.propose_resolution(
        FORECAST_NS, created["forecast_id"], principal_id="alice", scopes=SCOPES
    )
    assert (
        pending["status"] == "unresolved"
        and pending["reason"] == "no-certified-vintage"
    )
    assert pending["preliminary_available"] is True and pending["evidence"] == []
    h.apply(conn, "de-btw", h.DE_FINAL)
    proposal = ledger.propose_resolution(
        FORECAST_NS, created["forecast_id"], principal_id="alice", scopes=SCOPES
    )
    assert proposal["status"] == "proposed" and proposal["proposed_outcome"] == 1
    assert proposal["certified_vintage"]["kind"] == "certified"
    (evidence,) = proposal["evidence"]
    assert (
        evidence["kind"] == "source"
        and evidence["id"] == proposal["certified_vintage"]["vintage_id"]
    )
    resolved = ledger.resolve(
        FORECAST_NS,
        created["forecast_id"],
        0,
        status="resolved",
        outcome=1,
        evidence=proposal["evidence"],
        rationale="certified result",
        forecast_revision=created["revision"],
        principal_id="alice",
        scopes=SCOPES,
    )
    assert resolved["status"] == "resolved"
    with pytest.raises(ForecastError):
        ledger.propose_resolution(
            FORECAST_NS,
            created["forecast_id"],
            principal_id="alice",
            scopes=SCOPES - {"knowledge:political:elections:read"},
        )


def test_revisions_before_the_certified_vintage_stay_user_actions(env):
    conn, clock, forecasts, contest = env
    created = register(forecasts, contest)
    ledger = ForecastStore(conn, now=clock)
    clock.set("2099-03-05")  # after the preliminary result, before certification
    revised = ledger.revise(
        FORECAST_NS,
        created["forecast_id"],
        1,
        probability=0.8,
        evidence=EVIDENCE,
        rationale="preliminary count",
        principal_id="alice",
        scopes=SCOPES,
    )
    assert revised["probability"] == 0.8 and revised["revision"] == 2
    clock.set("2099-03-26")
    with pytest.raises(ForecastError) as exc:
        ledger.revise(
            FORECAST_NS,
            created["forecast_id"],
            2,
            probability=0.9,
            evidence=EVIDENCE,
            rationale="late",
            principal_id="alice",
            scopes=SCOPES,
        )
    assert exc.value.code == "forecast_frozen"


def test_scoring_waits_for_certification_and_uses_its_publication_time(env):
    conn, clock, forecasts, contest = env
    winner = register(forecasts, contest)
    loser = register(
        forecasts,
        contest,
        key="f2",
        rule={
            "kind": "votes_at_least",
            "entry": "party:musterunion",
            "threshold": "40000",
        },
        probability=0.7,
    )
    ids = [winner["forecast_id"], loser["forecast_id"]]
    with pytest.raises(ElectionError) as exc:
        forecasts.score(FORECAST_NS, ids, principal_id="alice", scopes=SCOPES)
    assert exc.value.code == "not_certified"
    h.apply(conn, "de-btw", h.DE_FINAL)
    clock.set("2099-03-26")
    ledger = ForecastStore(conn, now=clock)
    for created in (winner, loser):
        proposal = ledger.propose_resolution(
            FORECAST_NS, created["forecast_id"], principal_id="alice", scopes=SCOPES
        )
        ledger.resolve(
            FORECAST_NS,
            created["forecast_id"],
            0,
            status="resolved",
            outcome=proposal["proposed_outcome"],
            evidence=proposal["evidence"],
            rationale="certified",
            forecast_revision=1,
            principal_id="alice",
            scopes=SCOPES,
        )
    assert forecasts.scoring_cutoff(FORECAST_NS, ids, scopes=SCOPES) == day_ms(
        "2099-03-20", "2099-03-20T10:00:00"
    )
    scored = forecasts.score(FORECAST_NS, ids, principal_id="alice", scopes=SCOPES)
    assert scored["scored_count"] == 2 and {s["outcome"] for s in scored["scores"]} == {
        0,
        1,
    }
    assert scored["cutoff_ms"] == day_ms("2099-03-20", "2099-03-20T10:00:00")


def test_split_modes_are_never_summed_to_resolve(env):
    conn, clock, forecasts, _ = env
    h.apply(conn, "us", h.US)
    county = h.contest_id(
        conn, "us-us-president:2099", "us-fips-county", "99003", "office"
    )
    created = register(
        forecasts, county, rule={"kind": "winner", "entry": "candidate:sam-demo"}
    )
    clock.set("2099-03-26")
    proposal = ForecastStore(conn, now=clock).propose_resolution(
        FORECAST_NS, created["forecast_id"], principal_id="alice", scopes=SCOPES
    )
    assert (
        proposal["status"] == "unresolved"
        and proposal["reason"] == "figures-split-by-mode-are-never-summed"
    )
