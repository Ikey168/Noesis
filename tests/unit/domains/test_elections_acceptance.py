"""Offline contest-to-results-and-polls acceptance journey (#2008).

A contest identifier is taken through acquisition (pinned, authored result
files replayed through the real adapter), poll import, reviewable identity,
boundary projection, a place-based query across a boundary change, a
user-registered forecast resolved only once the certified vintage exists, news
evidence and a subscription monitor - on fixtures only. The dossier cites a
source revision for every figure, keeps conflicting assertions and unknowns
visible, and carries no prediction, poll aggregate or causal field. Re-running
acquisition and restarting the store leave identical outputs and cursors.
Nothing here is live coverage.
"""

from __future__ import annotations

import json

import duckdb

from src.database.local_warehouse_seed import _SCHEMA as WAREHOUSE
from src.ingestion.document_store import _SCHEMA as DOCUMENTS
from src.ingestion.election_sources import day_ms
from src.kb.elections import forbidden_keys
from src.kb.elections_forecasts import ElectionForecasts
from src.kb.elections_geo import ElectionGeography
from src.kb.elections_identity import ElectionIdentity
from src.kb.elections_monitoring import ElectionMonitor
from src.kb.elections_news import ElectionNews
from src.kb.elections_polls import ElectionPolls
from src.kb.elections_queries import contest_dossier
from src.kb.forecasts import ForecastStore
from src.kb.geospatial import GeospatialStore
from src.kb.subscriptions import SubscriptionStore
from tests.unit import elections_harness as h
from tests.unit.domains.test_elections_geo import VINTAGE_2025, VINTAGE_2103, wgs84

FORECASTS = "forecasts"
SCOPES = h.REVIEW_SCOPES | {
    "knowledge:read",
    "knowledge:forecasts:read",
    "knowledge:forecasts:write",
    f"namespace:{FORECASTS}:read",
    f"namespace:{FORECASTS}:write",
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:geospatial:calculate",
    "knowledge:geospatial:review",
}
POLL_URL = "https://www.beispiel-institut.example/sonntagsfrage.csv"
MU_2099 = f"elections:{h.DE_ELECTION}:party:musterunion"


class Clock:
    def __init__(self, day: str) -> None:
        self.value = day_ms(day)

    def __call__(self) -> int:
        return self.value


def acquire(conn) -> list[dict]:
    """Every pinned release and poll release; re-running it changes nothing."""
    results = [
        h.apply(conn, "de-btw", h.DE_PRELIMINARY),
        h.apply(conn, "de-btw", h.DE_FINAL),
        h.apply_next_federal(conn),
    ]
    for name in (
        "poll_beispiel_institut_2099-02-21.csv",
        "poll_beispiel_institut_2099-02-28.csv",
    ):
        results.append(
            ElectionPolls(conn).import_release(
                "global",
                publisher="Beispiel Institut",
                election_id=h.DE_ELECTION,
                source_url=POLL_URL,
                csv_text=(h.FIXTURES / name).read_text(),
                redistribution="allowed",
                principal_id="alice",
                scopes=SCOPES,
            )
        )
    return results


def cited(value, path="$"):
    """Every result vintage and poll reading in an answer cites its source revision."""
    missing = []
    if isinstance(value, dict):
        if ("figures" in value and "vintage_id" in value) or value.get(
            "record_type"
        ) == "poll_reading":
            revision = value.get("source_revision") or {}
            if not (
                revision.get("file_sha256")
                and revision.get("release_id")
                and revision.get("published_on")
            ):
                missing.append(path)
        for key, item in value.items():
            missing += cited(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            missing += cited(item, f"{path}[{index}]")
    return missing


def journey(path):
    conn = duckdb.connect(path)
    conn.execute(DOCUMENTS)
    conn.execute(WAREHOUSE)
    clock = Clock("2099-02-01")
    # A user registers a forecast before the election; the probability is theirs.
    h.apply(conn, "de-btw", h.DE_PRELIMINARY)
    contest = h.contest_id(conn, h.DE_ELECTION, "de-bt-wahlkreis", "001", "first-vote")
    forecasts = ElectionForecasts(conn, now=clock)
    forecast = forecasts.register(
        "global",
        FORECASTS,
        "wk1-winner",
        contest_id=contest,
        rule={"kind": "winner", "entry": "party:beispielpartei"},
        probability=0.55,
        resolution_at_ms=day_ms("2099-03-25"),
        evidence=[
            {"kind": "source", "id": "notes", "revision": "1", "namespace": FORECASTS}
        ],
        principal_id="alice",
        scopes=SCOPES,
    )
    ledger = ForecastStore(conn, now=clock)
    clock.value = day_ms("2099-03-26")
    before_certified = ledger.propose_resolution(
        FORECASTS, forecast["forecast_id"], principal_id="alice", scopes=SCOPES
    )
    acquire(conn)
    proposal = ledger.propose_resolution(
        FORECASTS, forecast["forecast_id"], principal_id="alice", scopes=SCOPES
    )
    ledger.resolve(
        FORECASTS,
        forecast["forecast_id"],
        0,
        status="resolved",
        outcome=proposal["proposed_outcome"],
        evidence=proposal["evidence"],
        rationale="certified result",
        forecast_revision=1,
        principal_id="alice",
        scopes=SCOPES,
    )
    # Identity: proposals, one accepted match, conflicting succession assertions.
    identity = ElectionIdentity(conn, now=clock)
    identity.propose("global", principal_id="alice", scopes=SCOPES)
    bp = next(
        c
        for c in identity.candidates("global", scopes=SCOPES)
        if set(c["records"])
        == {
            f"elections:{h.DE_ELECTION}:party:beispielpartei",
            f"elections:{h.DE_NEXT}:party:beispielpartei",
        }
    )
    identity.service.review(
        "global",
        bp["candidate_id"],
        "accept",
        "same party",
        principal_id="bob",
        scopes=SCOPES,
    )
    for target, url in (
        (
            f"elections:{h.DE_NEXT}:party:neue-musterunion",
            "https://www.example.org/register",
        ),
        (
            f"elections:{h.DE_NEXT}:party:beispielpartei",
            "https://news.example.org/merger",
        ),
    ):
        identity.assert_relation(
            "global",
            kind="party_successor",
            subject_key=MU_2099,
            object_key=target,
            valid_on="2101-06-01",
            source={"url": url, "title": "fictional source"},
            principal_id="alice",
            scopes=SCOPES,
        )
    # Geometry: two boundary vintages; the constituency's place stays unresolved (two candidate places).
    geography = ElectionGeography(conn, now=clock)
    for vintage, collection, start, end in (
        ("btw2025", VINTAGE_2025, "2023-01-01", "2102-01-01"),
        ("btw2103", VINTAGE_2103, "2102-01-01", None),
    ):
        geography.project_boundaries(
            "global",
            scheme="de-bt-wahlkreis",
            boundary_vintage=vintage,
            feature_collection=collection,
            id_property="WKR_NR",
            valid_from=start,
            valid_to=end,
            principal_id="alice",
            scopes=SCOPES,
        )
    places = GeospatialStore(conn)
    for key in ("a", "b"):
        places.register_place(
            "geo",
            "Musterstadt",
            "district",
            names=[{"value": "Musterstadt", "language": "de"}],
            source_ids={"fixture": key},
            parent_ids=[],
            principal_id="alice",
            scopes=SCOPES,
            place_key=key,
        )
    (wk1,) = geography.store.find_constituency(
        "global", "de-bt-wahlkreis", "001", "btw2025"
    )
    geography.link_place(
        "global",
        wk1["constituency_id"],
        geo_namespace="geo",
        principal_id="alice",
        scopes=SCOPES,
    )
    at = {
        day: geography.results_at_point(
            "global",
            scheme="de-bt-wahlkreis",
            point=wgs84(507_500),
            as_of=day,
            principal_id="alice",
            scopes=SCOPES,
        )
        for day in ("2099-04-01", "2103-04-01")
    }
    # News evidence: an explicit mention with a frame.
    conn.execute(
        "INSERT INTO documents (document_id, source_type, language, ingested_at, created_at, source_id, url, "
        "content_hash, title, content, metadata) VALUES ('doc-1', 'news', 'de', ?, ?, 'Example Daily', "
        "'https://news.example.org/doc-1', 'sha-doc-1', 'Beispielpartei campaign', 'Interview.', '{}')",
        [day_ms("2099-02-20"), day_ms("2099-02-20")],
    )
    conn.execute(
        "INSERT INTO document_actors VALUES ('doc-1','news','Beispielpartei',NULL,'speaker',0.9,NULL)"
    )
    conn.execute(
        "INSERT INTO document_frames VALUES ('doc-1','news','campaign',0.7,'2099-02-21')"
    )
    ElectionNews(conn, now=clock).refresh_links(
        "global", contest, principal_id="alice", scopes=SCOPES
    )
    # A monitor on the contest.
    monitor = ElectionMonitor(conn, now=clock)
    watch = monitor.create(
        "global",
        "wk1",
        watch="contest",
        key=contest,
        principal_id="alice",
        scopes=SCOPES,
    )
    SubscriptionStore(conn).commit_watermark("global", 1)
    notes = monitor.run(watch["subscription_id"], principal_id="alice", scopes=SCOPES)[
        "notifications"
    ]
    dossier = contest_dossier(
        conn,
        "global",
        contest,
        principal_id="alice",
        scopes=SCOPES,
        include_places=True,
        forecast_namespace=FORECASTS,
        include_news=True,
    )
    conn.close()
    return {
        "contest": contest,
        "before_certified": before_certified,
        "proposal": proposal,
        "at": at,
        "notes": notes,
        "dossier": dossier,
        "watch": watch,
    }


def test_contest_to_cited_results_and_polls_dossier(tmp_path):
    run = journey(str(tmp_path / "journey.duckdb"))
    dossier = run["dossier"]
    # Preliminary and certified vintages of one contest, with their differences cited on both sides.
    assert [v["kind"] for v in dossier["results"]["history"]] == [
        "preliminary",
        "certified",
    ]
    (change,) = dossier["results"]["changes"]
    assert (
        change["transition"] == "preliminary->certified" and change["changed_figures"]
    )
    assert (
        change["from"]["source_revision"]["release_id"]
        != change["to"]["source_revision"]["release_id"]
    )
    # A poll series typed as a poll, apart from results.
    assert dossier["poll_series"] and {
        s["typed_as"] for s in dossier["poll_series"]
    } == {"poll"}
    assert all(
        len(s["readings"]) == 3 for s in dossier["poll_series"]
    )  # three readings, never averaged
    # The place-based query across a boundary change.
    first, later = run["at"]["2099-04-01"], run["at"]["2103-04-01"]
    assert first["constituencies"][0]["constituency"]["native_id"] == "001"
    assert later["constituencies"][0]["constituency"]["native_id"] == "002"
    assert (
        first["boundary"]["boundary_vintage"] == "btw2025"
        and later["boundary"]["boundary_vintage"] == "btw2103"
    )
    # The forecast resolved only after the certified vintage arrived.
    assert run["before_certified"]["reason"] == "no-certified-vintage"
    assert run["proposal"]["certified_vintage"]["kind"] == "certified"
    (forecast,) = dossier["forecasts"]
    assert forecast["resolution_status"] == "resolved" and forecast["outcome"] == 1
    # Conflicting succession assertions and an unresolved constituency stay visible.
    union = next(
        e for e in dossier["candidates_and_lists"] if e["entry"] == "party:musterunion"
    )
    assert union["lineage"]["conflicts"] and len(union["lineage"]["assertions"]) == 2
    assert "constituency place link unresolved" in dossier["unknowns"]
    assert dossier["boundaries"]["place"]["state"] == "unresolved"
    bp = next(
        e
        for e in dossier["candidates_and_lists"]
        if e["entry"] == "party:beispielpartei"
    )
    assert bp["identity"]["state"] == "matched"
    # News evidence beside results, with its frame; no causal field anywhere.
    (news,) = dossier["news_evidence"]["news_evidence"]
    assert (
        news["frames"][0]["frame"] == "campaign"
        and news["link"]["link_kind"] == "explicit-mention"
    )
    # The monitor delivered both vintages; the certified one lists changed figures.
    assert sorted(n["kind"] for n in run["notes"]) == [
        "certified_result",
        "preliminary_result",
    ]
    # Every figure cites a source revision; no prediction, aggregate poll number or causal field.
    assert cited(dossier) == [] and cited(run["at"]) == []
    assert (
        forbidden_keys(dossier) == []
        and forbidden_keys(run["at"]) == []
        and forbidden_keys(run["notes"]) == []
    )
    for field in (
        "prediction",
        "poll_average",
        "seat_projection",
        "correlation",
        "causation",
        "probability",
    ):
        assert f'"{field}"' not in json.dumps(dossier)


def test_re_acquisition_and_a_restart_leave_outputs_and_cursors_identical(tmp_path):
    path = str(tmp_path / "restart.duckdb")
    run = journey(path)
    conn = duckdb.connect(path)
    cursor = SubscriptionStore(conn).poll(
        run["watch"]["subscription_id"], principal_id="alice", scopes=SCOPES
    )
    tables = {
        t: conn.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall()
        for t in (
            "election_releases",
            "election_result_vintages",
            "election_poll_readings",
        )
    }
    conn.close()
    conn = duckdb.connect(path)  # a restart
    again = acquire(conn)
    assert {r["status"] for r in again} == {"unchanged"}
    assert {
        t: conn.execute(f"SELECT * FROM {t} ORDER BY ALL").fetchall() for t in tables
    } == tables
    monitor = ElectionMonitor(conn, now=Clock("2099-04-01"))
    SubscriptionStore(conn).commit_watermark("global", 2)
    assert (
        monitor.run(
            run["watch"]["subscription_id"], principal_id="alice", scopes=SCOPES
        )["notifications"]
        == []
    )
    replayed = SubscriptionStore(conn).poll(
        run["watch"]["subscription_id"], principal_id="alice", scopes=SCOPES
    )
    assert (
        replayed["cursor"] == cursor["cursor"]
        and replayed["events"] == cursor["events"]
    )
    dossier = contest_dossier(
        conn,
        "global",
        run["contest"],
        principal_id="alice",
        scopes=SCOPES,
        include_places=True,
        forecast_namespace=FORECASTS,
        include_news=True,
    )
    assert dossier == run["dossier"]
    conn.close()
