"""Development-finance monitors on the subscription outbox (#2036)."""

from __future__ import annotations

import pytest

from src.kb.development_finance import DevelopmentFinanceError
from src.kb.development_finance_monitoring import DevelopmentFinanceMonitor
from tests.unit.funding import development_finance_harness as h


@pytest.fixture()
def env():
    env = h.Env().load()
    yield env
    env.conn.close()


def _cell(env, basis="current"):
    from src.kb.development_finance import DevelopmentFinanceStore

    (cell,) = DevelopmentFinanceStore(env.conn).crs_cells(
        h.NS, recipient="KEN", price_basis=basis
    )
    return cell["cell_id"]


def _kinds(run):
    return sorted(n["kind"] for n in run["notifications"])


def test_corrections_new_transactions_results_retractions_and_new_activities(env):
    monitor = DevelopmentFinanceMonitor(env.conn, now=env.now)
    created = monitor.create(
        h.NS,
        "fdpa",
        filters={"publishers": [h.FDPA]},
        principal_id="alice",
        scopes=h.SCOPES,
    )
    first = monitor.run(
        created["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    assert first["status"] == "evaluated"
    assert (
        _kinds(first).count("new_activity") == 2
    )  # the first evaluation lists what is already there
    env.at("2098-06-05T08:00:00").iati("iati_fdpa_2098-06.xml", observation="r2:fdpa")
    second = monitor.run(
        created["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    kinds = _kinds(second)
    assert (
        kinds.count("corrected_transaction") == 1
        and kinds.count("new_transaction") == 1
    )
    assert {"new_result_posting", "retracted_activity", "new_activity"} <= set(kinds)
    corrected = next(
        n for n in second["notifications"] if n["kind"] == "corrected_transaction"
    )
    assert (
        corrected["before_value"]["value_text"] == "250000"
        and corrected["after_value"]["value_text"] == "275000"
    )
    assert corrected["cites"]["before"] != corrected["cites"]["after"]
    retracted = next(
        n for n in second["notifications"] if n["kind"] == "retracted_activity"
    )
    assert "not ended" in retracted["message"]
    # Only publisher A is watched: nothing about the NGO, and nothing summed across publishers.
    assert all(
        n.get("publisher_id") in (None, "iati:ref:XM-DAC-99901")
        for n in second["notifications"]
    )
    polled = monitor.poll(
        created["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    assert len(polled["events"]) >= 3


def test_a_new_crs_vintage_of_a_watched_cell(env):
    monitor = DevelopmentFinanceMonitor(env.conn, now=env.now)
    cell = _cell(env)
    created = monitor.create(
        h.NS,
        "crs",
        filters={"crs_cells": [cell]},
        principal_id="alice",
        scopes=h.SCOPES,
    )
    monitor.run(created["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    env.at("2099-07-20T08:00:00").crs(
        "crs_deu_ken_140_2099-07.csv",
        release={"label": "CRS release 2099-07", "published_on": "2099-07-15"},
    )
    run = monitor.run(created["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    (vintage,) = run["notifications"]
    assert (
        vintage["kind"] == "new_crs_vintage" and vintage["published_on"] == "2099-07-15"
    )
    assert (
        vintage["cites"]["before"]
        and vintage["cites"]["before"] != vintage["cites"]["after"]
    )


def test_a_provider_failure_is_stale_coverage_and_never_an_ended_activity(env):
    monitor = DevelopmentFinanceMonitor(env.conn, now=env.now)
    created = monitor.create(
        h.NS,
        "fdpa",
        filters={"publishers": [h.FDPA]},
        principal_id="alice",
        scopes=h.SCOPES,
    )
    monitor.run(created["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    env.at("2098-06-05T08:00:00").iati(503, observation="r2:fdpa")
    run = monitor.run(created["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert run["coverage"]["complete"] is False
    assert (
        _kinds(run) == ["coverage_change"] and run["notifications"][0]["stale"] is True
    )
    assert not {"retracted_activity", "no_longer_matching"} & set(_kinds(run))
    events = monitor.poll(
        created["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )["events"]
    assert "coverage-degraded" in {e["event_type"] for e in events}


def test_a_replayed_watermark_and_a_restart_emit_nothing_new(env):
    monitor = DevelopmentFinanceMonitor(env.conn, now=env.now)
    created = monitor.create(
        h.NS,
        "fdpa",
        filters={"publishers": [h.FDPA]},
        principal_id="alice",
        scopes=h.SCOPES,
    )
    first = monitor.run(
        created["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    again = monitor.run(
        created["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    assert again["status"] == "replayed" and again["notifications"] == []
    # An unchanged re-acquisition changes no source, so a restarted monitor replays the recorded watermark.
    env.at("2099-03-20T08:00:00").iati("iati_fdpa_2098-03.xml", observation="r1b:fdpa")
    restarted = DevelopmentFinanceMonitor(env.conn, now=env.now)
    later = restarted.run(
        created["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    assert later["watermark"] == first["watermark"] and later["status"] == "replayed"
    assert later["notifications"] == []
    # A change after the restart gets a new watermark and is heard once.
    env.at("2099-03-21T08:00:00").iati("iati_fdpa_2098-06.xml", observation="r2:fdpa")
    changed = restarted.run(
        created["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    assert changed["watermark"] > first["watermark"] and changed["notifications"]
    # An older watermark than the recorded one is ignored.
    older = restarted.run(
        created["subscription_id"],
        first["watermark"] - 1,
        principal_id="alice",
        scopes=h.SCOPES,
    )
    assert older["status"] == "ignored"


def test_monitors_belong_to_their_owner_and_need_filters(env):
    monitor = DevelopmentFinanceMonitor(env.conn, now=env.now)
    with pytest.raises(DevelopmentFinanceError):
        monitor.create(h.NS, "none", filters={}, principal_id="alice", scopes=h.SCOPES)
    created = monitor.create(
        h.NS,
        "fdpa",
        filters={"countries": ["KE"]},
        principal_id="alice",
        scopes=h.SCOPES,
    )
    with pytest.raises(DevelopmentFinanceError):
        monitor.run(created["subscription_id"], principal_id="mallory", scopes=h.SCOPES)


def test_removed_and_redated_transactions_are_reported(env):
    """A dropped transaction is a removal; a ref-less transaction whose date changed is a correction (review)."""
    monitor = DevelopmentFinanceMonitor(env.conn, now=env.now)
    created = monitor.create(
        h.NS,
        "fdpa",
        filters={"publishers": [h.FDPA]},
        principal_id="alice",
        scopes=h.SCOPES,
    )
    monitor.run(created["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    edited = h.edited(
        "iati_fdpa_2098-03.xml",
        # FICT-0001-C1 is no longer reported.
        ('<transaction ref="FICT-0001-C1">', '<transaction ref="FICT-0001-C1" x="1">'),
        ("2098-03-01T10:00:00Z", "2098-07-01T10:00:00Z"),
        # The ref-less education disbursement is re-dated.
        ('<transaction ref="FICT-0002-D1">', "<transaction>"),
        (
            '<transaction-date iso-date="2098-02-05"/>',
            '<transaction-date iso-date="2098-02-06"/>',
        ),
        ("2098-02-10T09:00:00Z", "2098-07-01T09:00:00Z"),
    )
    text = edited.decode()
    start = text.index('<transaction ref="FICT-0001-C1" x="1">')
    text = (
        text[:start]
        + text[text.index("</transaction>", start) + len("</transaction>") :]
    )
    # Make the education disbursement ref-less in the first state too, so only its date differs.
    baseline = h.edited(
        "iati_fdpa_2098-03.xml",
        ('<transaction ref="FICT-0002-D1">', "<transaction>"),
        ("2098-02-10T09:00:00Z", "2098-02-11T09:00:00Z"),
    )
    env.at("2099-02-01T08:00:00").iati(baseline, observation="r1b")
    monitor.run(created["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    env.at("2099-03-01T08:00:00").iati(text.encode(), observation="r2")
    run = monitor.run(created["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    kinds = [(n["kind"], n.get("transaction_key")) for n in run["notifications"]]
    assert ("removed_transaction", "ref:FICT-0001-C1") in kinds
    corrected = [
        n for n in run["notifications"] if n["kind"] == "corrected_transaction"
    ]
    assert len(corrected) == 1 and corrected[0]["before_value"]["date"] == "2098-02-05"
    assert corrected[0]["after_value"]["date"] == "2098-02-06"
    assert not [k for k in kinds if k[0] == "new_transaction"]
    from src.kb.development_finance_queries import DevelopmentFinanceQueries

    inspected = DevelopmentFinanceQueries(env.conn, now=env.now).inspect_activity(
        h.NS, "XM-DAC-99901-FICT-0001", principal_id="a", scopes=h.SCOPES
    )
    changes = next(
        r for r in inspected["reports"] if r["publisher_id"].endswith("99901")
    )["transaction_changes"]
    assert any(
        c["change"] == "removed" and c["transaction_key"] == "ref:FICT-0001-C1"
        for c in changes
    )
