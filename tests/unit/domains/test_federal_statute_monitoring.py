"""Statute, provision and amendment-act monitors through platform.subscriptions (#2105, FL10)."""

from __future__ import annotations

import pytest

from src.kb.legal import LegalError
from src.kb.legal_federal import FederalStatutes
from src.kb.legal_statute_monitoring import StatuteMonitor
from src.kb.subscriptions import SubscriptionStore
from tests.unit import federal_statutes_harness as h


def commit(conn, watermark):
    SubscriptionStore(conn).commit_watermark(h.NS, watermark, kind="ingestion")


def run(monitor, subscription):
    return monitor.run(
        subscription["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


@pytest.fixture()
def setup():
    conn = h.connection()
    h.cellar_directive(conn)
    h.apply(conn, "gii", h.gii_pages("2030-03-01"), at="2030-03-01")
    h.apply(conn, "ris", h.ris_pages(("2029-06-01",)), at="2030-03-02")
    monitor = StatuteMonitor(conn)
    watches = {
        "provision": monitor.create(
            h.NS,
            "p",
            watch="provision",
            statute="MPHG",
            provision="§ 5 Abs. 2 MPHG",
            principal_id="alice",
            scopes=h.SCOPES,
        ),
        "statute": monitor.create(
            h.NS,
            "s",
            watch="statute",
            statute="MPHG",
            principal_id="alice",
            scopes=h.SCOPES,
        ),
        "act": monitor.create(
            h.NS,
            "a",
            watch="amendment-act",
            act="BGBl. 2030 I Nr. 45",
            principal_id="alice",
            scopes=h.SCOPES,
        ),
    }
    commit(conn, 1)
    return conn, monitor, watches


def refresh(conn):
    h.apply(conn, "bgbl", h.bgbl_pages(), at="2030-03-15")
    h.apply(conn, "ris", h.ris_pages(), at="2030-04-05")
    h.apply(conn, "gii", h.gii_pages("2030-06-01"), at="2030-06-01")
    h.apply(conn, "rii", h.rii_pages(), at="2031-06-01")


def test_first_evaluation_is_a_baseline_and_refreshes_produce_cited_events(setup):
    conn, monitor, watches = setup
    baseline = run(monitor, watches["provision"])
    assert baseline["baseline"] and set(kinds(baseline)) == {"in_view"}
    run(monitor, watches["statute"])
    assert run(monitor, watches["act"])["notifications"] == []
    refresh(conn)
    commit(conn, 2)
    events = run(monitor, watches["provision"])
    assert kinds(events) == [
        "new_amendment_act",
        "new_citing_decision",
        "new_citing_decision",
        "new_citing_decision",
        "new_observed_version",
        "new_source_stated_version",
    ]
    by_kind = {n["kind"]: n for n in events["notifications"]}
    observed = by_kind["new_observed_version"]["cites"]
    assert (
        observed["previous_version_id"] and observed["diff"][0]["change"] == "changed"
    )
    assert (
        "fünf" in observed["diff"][0]["after"]
        and "zehn" in observed["diff"][0]["before"]
    )
    stated = by_kind["new_source_stated_version"]["cites"]
    assert stated["eli"].endswith("2030-04-01/1/deu") and stated["diff"]
    act = by_kind["new_amendment_act"]["cites"]
    assert act["bgbl_key"] == "bgbl-1/2030/nr-45" and {
        i["provision"] for i in act["instructions"]
    } == {"§5", "§5/abs2"}
    citing = [
        n["cites"]
        for n in events["notifications"]
        if n["kind"] == "new_citing_decision"
    ]
    assert all(c["locator"] and c["provision"] == "§5/abs2" for c in citing)
    statute_events = kinds(run(monitor, watches["statute"]))
    assert (
        statute_events.count("new_citing_decision") == 5
        and "new_amendment_act" in statute_events
    )
    assert kinds(run(monitor, watches["act"])) == ["new_amendment_act"]


def test_unchanged_refreshes_and_replays_produce_nothing_and_dossier_links_notify(
    setup,
):
    conn, monitor, watches = setup
    refresh(conn)
    commit(conn, 2)
    for watch in watches.values():
        run(monitor, watch)
    assert run(monitor, watches["provision"])["status"] == "replayed"
    h.apply(
        conn, "gii", h.gii_pages("2030-06-01"), at="2030-07-01"
    )  # an unchanged observation
    h.dip_dossier(conn)
    FederalStatutes(conn).link_amendment_dossiers(
        h.NS, scopes=h.SCOPES, principal_id="alice", dossier_namespace=h.DOSSIER_NS
    )
    commit(conn, 3)
    assert run(monitor, watches["provision"])["notifications"] == []
    assert kinds(run(monitor, watches["act"])) == ["dossier_linked"]


def test_monitors_need_a_known_target_and_a_committed_watermark():
    conn = h.connection()
    h.apply(conn, "gii", h.gii_pages("2030-03-01"), at="2030-03-01")
    monitor = StatuteMonitor(conn)
    for kwargs in (
        {"watch": "paragraph", "statute": "MPHG"},
        {"watch": "statute", "statute": "FooG"},
        {"watch": "amendment-act", "act": "Gesetz vom Montag"},
    ):
        with pytest.raises(LegalError) as caught:
            monitor.create(h.NS, "x", principal_id="alice", scopes=h.SCOPES, **kwargs)
        assert caught.value.code == "invalid_watch"
    watch = monitor.create(
        h.NS,
        "w",
        watch="statute",
        statute="WpHG",
        principal_id="alice",
        scopes=h.SCOPES,
    )
    with pytest.raises(LegalError) as caught:
        run(monitor, watch)
    assert caught.value.code == "watermark_uncommitted"
