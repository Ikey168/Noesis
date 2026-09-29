"""Monitor new docket entries, decisions and statistic releases through subscriptions (#2422)."""

from __future__ import annotations

import pytest

from src.ingestion.courts_justice_sources import FIXTURE_SECRET, fixture_transport
from src.kb.courts_justice import CourtsJusticeError
from src.kb.courts_justice_monitoring import CourtsJusticeMonitor
from src.kb.legal_court_citations import CourtCitations
from src.kb.subscriptions import SubscriptionStore
from tests.unit import courts_justice_harness as h


def kinds(result):
    return sorted({n["kind"] for n in result["notifications"]})


@pytest.fixture()
def conn():
    connection = h.connection()
    h.apply(connection, "courtlistener-dockets")
    for source_id in h.STAT_SOURCES:
        h.apply(connection, source_id)
    h.seed_us_code(connection)
    CourtCitations(connection).link(h.NS, scopes=h.SCOPES)
    yield connection
    connection.close()


def test_every_event_type_is_notified_with_before_and_after_revisions(conn):
    monitor = CourtsJusticeMonitor(conn, now=h.Clock())
    subs = SubscriptionStore(conn)
    subs.commit_watermark(h.NS, 1)
    ids = {watch: monitor.create(h.NS, watch, watch=watch, key=key, principal_id="alice", scopes=h.SCOPES)[
        "subscription_id"] for watch, key in (("docket", h.DOCKET), ("court", "dcd"),
                                              ("provision", "42 U.S.C. § 1983"), ("series", "us-state:EX"))}
    first = {w: monitor.run(i, 1, principal_id="alice", scopes=h.SCOPES) for w, i in ids.items()}
    assert "new_docket_entry" in kinds(first["docket"]) and "new_vintage" in kinds(first["series"])
    assert kinds(first["provision"]) == ["new_citing_entry"]
    h.apply(conn, "courtlistener-dockets", v2=True)
    h.apply(conn, "courtlistener-opinions")
    h.apply(conn, "fbi-cde-summarized", v2=True)
    CourtCitations(conn).link(h.NS, scopes=h.SCOPES)
    subs.commit_watermark(h.NS, 2)
    second = {w: monitor.run(i, 2, principal_id="alice", scopes=h.SCOPES) for w, i in ids.items()}
    entries = [n for n in second["docket"]["notifications"] if n["kind"] == "new_docket_entry"]
    assert sorted(n["summary"]["entry_number"] for n in entries) == [3, 4]
    assert entries[0]["cites"]["revision_no"] == 2 and entries[0]["evidence_origin"] == "fixture"
    assert {"docket_revised", "new_opinion"} <= set(kinds(second["docket"]))
    revised = next(n for n in second["docket"]["notifications"] if n["kind"] == "docket_revised")
    assert revised["cites"]["previous"]["revision_no"] == 1
    assert "new_opinion" in kinds(second["court"])
    assert {"new_citing_decision", "new_citing_entry"} <= set(kinds(second["provision"]))
    series = second["series"]
    assert {"new_vintage", "revised_observation"} <= set(kinds(series))
    march = next(n for n in series["notifications"] if n["kind"] == "revised_observation"
                 and n["object"].endswith("actuals:Examplestate Offenses:2098-03"))
    assert (march["previous_summary"]["value"], march["summary"]["value"]) == (402, 399)
    assert monitor.run(ids["docket"], 2, principal_id="alice", scopes=h.SCOPES)["status"] == "replayed"
    polled = monitor.poll(ids["docket"], principal_id="alice", scopes=h.SCOPES)
    assert polled["events"]


def test_natural_persons_cannot_be_targets_and_live_unverified_revisions_are_withheld(conn):
    monitor = CourtsJusticeMonitor(conn)
    for key in ("natural person 1 (Defendant)", "Jane Roe"):
        with pytest.raises(CourtsJusticeError) as caught:
            monitor.create(h.NS, key, watch="party", key=key, principal_id="alice", scopes=h.SCOPES)
        assert caught.value.code == "natural_person_not_a_target"
    pages = h.native_pages("courtlistener-dockets", h.V2["courtlistener-dockets"])

    def live(**kwargs):
        response = fixture_transport(pages)(**kwargs)
        return {**response, "origin": "live"}

    refreshed = monitor.refresh(h.source("courtlistener-dockets"), run_id="refresh-1", principal_id="op",
                                scopes=h.SCOPES, transport=live, secret=FIXTURE_SECRET)
    assert refreshed["counts"] == {"docket_revisions": 1, "opinion_revisions": 0, "unchanged": 0}
    assert refreshed["receipts"][0]["evidence_origin"] == "live"
    again = monitor.refresh(h.source("courtlistener-dockets"), run_id="refresh-2", principal_id="op",
                            scopes=h.SCOPES, transport=live, secret=FIXTURE_SECRET)
    assert again["counts"]["unchanged"] == 1 and again["receipts"]
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    sub = monitor.create(h.NS, "d", watch="docket", key=h.DOCKET, principal_id="alice", scopes=h.SCOPES)
    result = monitor.run(sub["subscription_id"], 1, principal_id="alice", scopes=h.SCOPES)
    assert result["withheld_unverified_live_revisions"] == 1
    assert {n["cites"]["revision_no"] for n in result["notifications"]} == {1}
