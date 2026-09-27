"""Elections monitors on subscriptions: new vintages, recounts, corrections and poll readings (#1991)."""

from __future__ import annotations

import pytest

from src.kb.elections import ElectionError, forbidden_keys
from src.kb.elections_monitoring import ElectionMonitor
from src.kb.elections_polls import ElectionPolls
from src.kb.subscriptions import SubscriptionStore
from tests.unit import elections_harness as h

POLL_URL = "https://www.beispiel-institut.example/sonntagsfrage.csv"


@pytest.fixture()
def conn():
    connection = h.connection()
    yield connection
    connection.close()


def run(monitor, subscription, watermark):
    SubscriptionStore(monitor.conn).commit_watermark("global", watermark)
    return monitor.run(
        subscription["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )


def polls(conn, filename):
    return ElectionPolls(conn).import_release(
        "global",
        publisher="Beispiel Institut",
        election_id=h.DE_ELECTION,
        source_url=POLL_URL,
        csv_text=(h.FIXTURES / filename).read_text(),
        redistribution="allowed",
        principal_id="alice",
        scopes=h.SCOPES,
    )


def test_certified_result_lists_changed_figures_citing_both_vintages(conn):
    h.apply(conn, "de-btw", h.DE_PRELIMINARY)
    monitor = ElectionMonitor(conn)
    contest = h.contest_id(conn, h.DE_ELECTION, "de-bt-wahlkreis", "001", "first-vote")
    watch = monitor.create(
        "global",
        "wk1",
        watch="contest",
        key=contest,
        principal_id="alice",
        scopes=h.SCOPES,
    )
    first = run(monitor, watch, 1)
    assert [n["kind"] for n in first["notifications"]] == ["preliminary_result"]
    h.apply(conn, "de-btw", h.DE_FINAL)
    (certified,) = run(monitor, watch, 2)["notifications"]
    assert certified["kind"] == "certified_result"
    assert certified["compared_with"]["kind"] == "preliminary"
    changed = {(c["entry"], c["field"]): c for c in certified["changed_figures"]}
    votes = changed[("party:beispielpartei", "votes")]
    assert (votes["before"]["value"], votes["after"]["value"]) == (41000, 40990)
    assert votes["before"]["vintage_id"] == certified["compared_with"]["vintage_id"]
    assert certified["cites"]["source_revision"]["published_on"] == "2099-03-20"
    assert certified["compared_with"]["source_revision"]["published_on"] == "2099-03-02"
    assert forbidden_keys(certified) == []


def test_corrections_are_their_own_event_kind(conn):
    h.apply(conn, "de-btw", h.DE_FINAL)
    monitor = ElectionMonitor(conn)
    contest = h.contest_id(conn, h.DE_ELECTION, "de-bt-wahlkreis", "001", "first-vote")
    watch = monitor.create(
        "global",
        "wk1",
        watch="contest",
        key=contest,
        principal_id="alice",
        scopes=h.SCOPES,
    )
    run(monitor, watch, 1)
    body = (
        (h.FIXTURES / h.DE_FINAL)
        .read_text()
        .replace("Stand: 20.03.2099 10:00", "Stand: 02.05.2099 09:00")
        .replace(";Musterunion;2;1;39020;", ";Musterunion;2;1;39021;")
    )
    h.apply(conn, "de-btw", "corrected", body=body)
    (correction,) = run(monitor, watch, 2)["notifications"]
    assert (
        correction["kind"] == "correction"
        and correction["compared_with"]["kind"] == "certified"
    )
    assert [c["field"] for c in correction["changed_figures"]] == ["votes"]


def test_constituency_watch_covers_every_ballot(conn):
    h.apply(conn, "de-btw", h.DE_PRELIMINARY)
    monitor = ElectionMonitor(conn)
    (wk,) = monitor.store.find_constituency("global", "de-bt-wahlkreis", "1")
    watch = monitor.create(
        "global",
        "c",
        watch="constituency",
        key=wk["constituency_id"],
        principal_id="alice",
        scopes=h.SCOPES,
    )
    assert len(run(monitor, watch, 1)["notifications"]) == 2  # first and second vote


def test_new_poll_readings_are_poll_events_never_result_changes(conn):
    polls(conn, "poll_beispiel_institut_2099-02-21.csv")
    monitor = ElectionMonitor(conn)
    watch = monitor.create(
        "global",
        "p",
        watch="election_polls",
        key=h.DE_ELECTION,
        principal_id="alice",
        scopes=h.SCOPES,
    )
    assert len(run(monitor, watch, 1)["notifications"]) == 6
    polls(conn, "poll_beispiel_institut_2099-02-28.csv")
    notes = run(monitor, watch, 2)["notifications"]
    assert len(notes) == 3 and {n["kind"] for n in notes} == {"poll_reading"}
    note = notes[0]
    assert (
        note["typed_as"] == "poll"
        and note["sample_size"] == 1510
        and note["fieldwork_end"] == "2099-02-26"
    )
    assert (
        "not a result" in note["message"]
        and note["cites"]["source_revision"]["url"] == POLL_URL
    )


def test_replaying_the_same_payload_and_a_restart_deliver_nothing_twice(conn):
    h.apply(conn, "de-btw", h.DE_PRELIMINARY)
    monitor = ElectionMonitor(conn)
    contest = h.contest_id(conn, h.DE_ELECTION, "de-bt-wahlkreis", "001", "first-vote")
    watch = monitor.create(
        "global",
        "wk1",
        watch="contest",
        key=contest,
        principal_id="alice",
        scopes=h.SCOPES,
    )
    assert len(run(monitor, watch, 1)["notifications"]) == 1
    assert run(monitor, watch, 1)["status"] == "replayed"
    h.apply(
        conn, "de-btw", h.DE_PRELIMINARY, run_id="rerun"
    )  # a source-pack re-run of the same file
    restarted = ElectionMonitor(conn)  # a new process over the same store
    again = run(restarted, watch, 2)
    assert again["status"] == "evaluated" and again["notifications"] == []
    events = SubscriptionStore(conn).poll(
        watch["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    assert len(events["events"]) == 1


def test_live_vintages_of_unverified_providers_are_withheld(conn):
    fetched, item = h.page("de-btw", h.DE_PRELIMINARY)
    header = {**fetched.records[0]["election_release"], "evidence_origin": "live"}
    monitor = ElectionMonitor(conn)
    monitor.store.apply_release(
        "global",
        header,
        [r["election_contest"] for r in fetched.records],
        run_id="live",
        source_id=item["source_id"],
    )
    contest = h.contest_id(conn, h.DE_ELECTION, "de-bt-wahlkreis", "001", "first-vote")
    watch = monitor.create(
        "global",
        "wk1",
        watch="contest",
        key=contest,
        principal_id="alice",
        scopes=h.SCOPES,
    )
    result = run(monitor, watch, 1)
    assert (
        result["notifications"] == []
        and result["withheld_unverified_live_vintages"] == 1
    )
    with pytest.raises(ElectionError):
        monitor.create(
            "global", "x", watch="party", key="k", principal_id="alice", scopes=h.SCOPES
        )
