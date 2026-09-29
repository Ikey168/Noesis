"""Offline acceptance: a competition and a date to a cited table as of that date (#2147, SP12).

The pinned ``sports-records`` fixtures run through the real source-pack runtime
(installed, licence-accepted, composed); the fictional Example League season is
then acquired as three football-data.org polls, a team list, a published table
and an openfootball export, with a governing-body forfeit and a points
deduction. The journey reviews identity for a team and a player across two
sources, answers the table as of three dates (the correction and the forfeit
change it only from their publication), compares it with the published table,
registers and resolves a forecast only on the official vintage, raises monitor
events, and checks that tennis records carry their licence and that no answer
has odds or a Noesis prediction. Nothing touches the network; every club,
player and match is fictional: offline fixture evidence, never live coverage.
"""

from __future__ import annotations

import json
import socket

import pytest

from src.composition.adapter import adapt_all
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.domains import registry as domain_registry
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore
from src.ingestion.sports_sources import to_ms
from src.kb.forecasts import ForecastStore
from src.kb.sports_forecasts import SportsForecasts
from src.kb.sports_identity import SportsIdentity
from src.kb.sports_links import SportsLinks
from src.kb.sports_monitoring import SportsMonitor
from src.kb.sports_queries import SportsQueries
from src.kb.sports_records import forbidden_keys
from src.kb.subscriptions import SubscriptionStore
from tests.unit.sports import harness as h

PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - preflight resolver; no request is ever sent
FNS = "forecasts"
SCOPES = h.REVIEW_SCOPES | {
    f"namespace:{FNS}:read",
    f"namespace:{FNS}:write",
    "knowledge:read",
}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError(
            "the acceptance journey must not open a network connection"
        )

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (
        dict(domain_registry._REGISTRY),
        set(domain_registry._ENABLED),
        domain_registry._AUTHORITY,
    )
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def run_pack(conn):
    manifest = h.manifest()
    SourcePackStore(conn).install(
        manifest, principal_id="operator", enable=True, now_ms=10
    )
    clock = iter(range(2_000, 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _d: None)
    by_operation: dict[str, list[str]] = {}
    for source in manifest["sources"]:
        runtime.accept_license(
            manifest["pack_id"], source["source_id"], principal_id="operator"
        )
        by_operation.setdefault(source["operations"][0], []).append(source["source_id"])
    return [
        runtime.run(
            {
                "pack_id": manifest["pack_id"],
                "run_key": f"acceptance:{operation}",
                "operation": operation,
                "source_ids": sorted(source_ids),
                "max_results": 5000,
                "max_bytes": 20_000_000,
                "timeout_ms": 60_000,
            },
            principal_id="operator",
            adapters=runtime.fixture_adapters(manifest["pack_id"], h.ROOT),
            secret_resolver=lambda _ref: "fixture-credential",
            dns_resolver=PUBLIC_DNS,
        )
        for operation, source_ids in sorted(by_operation.items())
    ]


def points(answer):
    return {row["team_name"]: row["points"] for row in answer["table"]}


def test_competition_and_date_to_a_cited_table_with_revisions_identity_forecast_and_monitor():
    conn = h.connection()
    # The composed bundle with every optional feature on.
    bundles = adapt_all()
    plan = resolve(
        [
            {
                "pack": "sports",
                "version": bundles["sports"]["version"],
                "features": ["sports-identity", "sports-tennis", "sports-forecasts"],
            }
        ],
        list(bundles.values()),
        provider_descriptors(),
    )
    assert plan.ok and set(plan.plan["features"]["sports"]) == {
        "sports-identity",
        "sports-tennis",
        "sports-forecasts",
    }

    # 1. The pinned source pack through the real runtime and projector (fixture evidence).
    receipts = run_pack(conn)
    assert [r["status"] for r in receipts] == ["complete"] * 4, receipts
    tennis = SportsQueries(conn).export_records(
        "global",
        [
            {
                "record_type": "match_result_revision",
                "record_key": "sports:sackmann-tennis:fixture:atp:2099-9001:3",
            }
        ],
        scopes=SCOPES,
        purpose="non-commercial",
    )
    assert (
        tennis["licences"][0]["id"] == "CC-BY-NC-SA-4.0"
        and tennis["licences"][0]["share_alike"] is True
    )
    assert "Jeff Sackmann" in tennis["attributions"][0]

    # 2. The Example League season: polls, table, a second source, governing-body decisions.
    h.load_league(conn)
    h.apply(conn, "openfootball", "openfootball_exl_2099.json", at="2099-08-20")
    before = conn.execute("SELECT count(*) FROM sports_revisions").fetchone()[0]
    h.load_league(conn)  # re-acquiring unchanged publications adds nothing
    assert conn.execute("SELECT count(*) FROM sports_revisions").fetchone()[0] == before

    # 3. Identity review: one team and one player across two sources; the renamed club is not merged.
    clock = h.Clock("2099-09-01")
    identity = SportsIdentity(conn, now=clock)
    links = SportsLinks(conn, now=clock)
    transfer = links.record_transfer(
        "global",
        player_name="Jan Example",
        birth_date="2075-04-12",
        from_team_key=h.team_key(9001),
        to_team_key=h.team_key(9002),
        date="2099-09-01",
        source={
            "kind": "club",
            "url": "https://rovers.example.org/news/jan-example",
            "title": "Jan Example leaves",
        },
        principal_id="alice",
        scopes=SCOPES,
    )
    candidates = identity.propose("global", principal_id="alice", scopes=SCOPES)[
        "candidates"
    ]
    team_pair = {
        h.team_key(9001),
        "sports:openfootball:team:exl-1:northbridge-rovers-fc",
    }
    player_pair = {"sports:football-data:player:50001", transfer["player_key"]}
    for pair in (team_pair, player_pair):
        (candidate,) = [c for c in candidates if set(c["records"]) == pair]
        assert (
            candidate["state"] == "proposed"
            and candidate["basis"] == "name-jurisdiction"
        )
        identity.review(
            "global",
            candidate["candidate_id"],
            "accept",
            "reviewed",
            principal_id="rev",
            scopes=SCOPES,
        )
    assert not [
        c
        for c in candidates
        if set(c["records"])
        == {"sports:football-data:player:50001", "sports:football-data:player:50003"}
    ]
    assert set(identity.linked("global", h.team_key(9001))) == team_pair
    united = {h.team_key(9003), "sports:openfootball:team:exl-1:southport-united"}
    (candidate,) = [c for c in candidates if set(c["records"]) == united]
    identity.review(
        "global",
        candidate["candidate_id"],
        "accept",
        "reviewed",
        principal_id="rev",
        scopes=SCOPES,
    )

    # 4. A forecast registered before the season's decisions.
    forecasts = SportsForecasts(conn, now=h.Clock("2099-08-01"))
    forecast = forecasts.register(
        "global",
        FNS,
        "rovers-top",
        rule={
            "kind": "final_position",
            "season_key": h.SEASON,
            "team_key": h.team_key(9001),
            "position_at_most": 1,
        },
        probability=0.55,
        resolution_at_ms=to_ms("2099-09-01"),
        evidence=[],
        principal_id="alice",
        scopes=SCOPES,
    )
    ledger_clock = h.Clock("2099-09-11")
    ledger = ForecastStore(conn, now=ledger_clock)
    monitor_clock = h.Clock("2099-09-05")
    monitor = SportsMonitor(conn, now=monitor_clock)
    watch = monitor.create(
        "global",
        "league",
        watch="competition",
        key=h.SEASON,
        principal_id="alice",
        scopes=SCOPES,
    )
    SubscriptionStore(conn).commit_watermark("global", 1)
    monitor.run(watch["subscription_id"], principal_id="alice", scopes=SCOPES)
    h.record_decisions(conn)

    # 5. The table as of three dates, each change only from its publication.
    queries = SportsQueries(conn)
    on_20 = queries.standings_as_of("global", h.SEASON, "2099-08-20", scopes=SCOPES)
    on_25 = queries.standings_as_of("global", h.SEASON, "2099-08-25", scopes=SCOPES)
    on_9 = queries.standings_as_of("global", h.SEASON, "2099-09-09", scopes=SCOPES)
    on_12 = queries.standings_as_of("global", h.SEASON, "2099-09-12", scopes=SCOPES)
    assert points(on_20)["Northbridge Rovers"] == 4  # the 1-1 as first published
    assert (
        points(on_25)["Northbridge Rovers"] == 6
    )  # the correction published on 24 August
    assert (
        points(on_9)["Northbridge Rovers"] == 6
        and points(on_9)["Southport United"] == 1
    )  # deduction, no forfeit
    assert (
        points(on_12)["Northbridge Rovers"] == 9
    )  # the forfeit published on 10 September
    forfeit = next(c for c in on_12["corrections"] if c["status"] == "forfeit_awarded")
    assert (
        forfeit["deciding_body"] == h.FA and forfeit["decision"]["url"] == h.FORFEIT_URL
    )
    # 6. Compared with the source-published table, never silently resolved.
    assert (
        queries.standings_as_of("global", h.SEASON, "2099-09-04", scopes=SCOPES)[
            "comparison"
        ]["status"]
        == "agrees"
    )
    assert (
        on_12["comparison"]["status"] == "disagrees"
        and on_12["comparison"]["differences"]
    )
    # The rescheduled fixture keeps its history; the second source's result stays side by side.
    history = queries.fixture_history("global", h.fixture_key(104), scopes=SCOPES)
    assert [r["status"] for r in history["revisions"]] == ["postponed", "rescheduled"]
    match = queries.match_history(
        "global", h.fixture_key(103), scopes=SCOPES, identity=identity
    )
    assert match["other_sources"][0]["agrees"] is False

    # 7. The forecast resolves only on the official vintage.
    proposal = ledger.propose_resolution(
        FNS, forecast["forecast_id"], principal_id="alice", scopes=SCOPES
    )
    assert (
        proposal["status"] == "proposed"
        and proposal["proposed_outcome"] == 1
        and proposal["requires_review"]
    )
    assert all(
        r["status"] in {"official", "forfeit_awarded"}
        for r in proposal["official_revisions"]
    )
    ledger.resolve(
        FNS,
        forecast["forecast_id"],
        0,
        status="resolved",
        outcome=1,
        evidence=proposal["evidence"],
        rationale="official table",
        forecast_revision=1,
        principal_id="alice",
        scopes=SCOPES,
    )

    # 8. Monitor events cite both revisions.
    monitor_clock.set("2099-09-12")
    SubscriptionStore(conn).commit_watermark("global", 2)
    events = monitor.run(watch["subscription_id"], principal_id="alice", scopes=SCOPES)[
        "notifications"
    ]
    kinds = {n["kind"] for n in events}
    assert {"forfeit_awarded", "table_changed", "forecast_resolvable"} <= kinds
    forfeited = next(n for n in events if n["kind"] == "forfeit_awarded")
    assert (
        forfeited["cites"]["old"]["revision_id"]
        and forfeited["cites"]["new"]["deciding_body"] == h.FA
    )

    # 9. No answer carries odds or a Noesis-produced prediction.
    for answer in (on_20, on_25, on_9, on_12, history, match, proposal, events):
        assert forbidden_keys(answer) == [] and "None" not in json.dumps(
            answer, default=str
        )
