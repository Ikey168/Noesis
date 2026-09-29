"""Lobbying monitors on subscriptions: registrations, deregistrations, spend and client revisions, meetings (#1999)."""

from __future__ import annotations

import json

import pytest

from src.kb import lobbying_monitoring
from src.kb.lobbying import LobbyingError, forbidden_keys
from src.kb.lobbying_links import LobbyingDossierLinks
from src.kb.lobbying_monitoring import LobbyingMonitor
from src.kb.subscriptions import SubscriptionError, SubscriptionStore
from tests.unit import lobbying_harness as h


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


def test_spend_client_and_lifecycle_changes_cite_both_revisions(conn):
    h.apply(conn, "eu-tr", "eu_tr_2099-01-15.xml")
    monitor = LobbyingMonitor(conn)
    assoc = monitor.create(
        "global",
        "assoc",
        watch="registrant",
        key=h.entry_id(conn, h.EU_ASSOC),
        principal_id="alice",
        scopes=h.SCOPES,
    )
    consultancy = monitor.create(
        "global",
        "consultancy",
        watch="registrant",
        key=h.entry_id(conn, h.EU_CONSULTANCY),
        principal_id="alice",
        scopes=h.SCOPES,
    )
    forum = monitor.create(
        "global",
        "forum",
        watch="registrant",
        key=h.entry_id(conn, h.EU_FORUM),
        principal_id="alice",
        scopes=h.SCOPES,
    )
    assert [n["kind"] for n in run(monitor, assoc, 1)["notifications"]] == [
        "registration"
    ]
    run(monitor, consultancy, 1)
    run(monitor, forum, 1)
    h.apply(conn, "eu-tr", "eu_tr_2099-03-01.xml")
    (spend,) = run(monitor, assoc, 2)["notifications"]
    assert spend["kind"] == "spend_range_revised"
    assert (spend["old_ranges"][0]["lower"], spend["old_ranges"][0]["upper"]) == (
        "100000",
        "199999",
    )
    assert (spend["new_ranges"][0]["lower"], spend["new_ranges"][0]["upper"]) == (
        "200000",
        "299999",
    )
    assert (
        spend["cites"]["previous_revision_id"]
        and spend["cites"]["revision_id"] != spend["cites"]["previous_revision_id"]
    )
    assert forbidden_keys(spend) == []
    consultancy_notes = {
        n["kind"]: n for n in run(monitor, consultancy, 2)["notifications"]
    }
    # The new client comes with its own declared revenue range: both are reported as filed.
    assert set(consultancy_notes) == {"spend_range_revised", "clients_revised"}
    assert consultancy_notes["clients_revised"]["clients_added"] == [
        "Sample Charging Operator SA"
    ]
    added = [
        r
        for r in consultancy_notes["spend_range_revised"]["new_ranges"]
        if r not in consultancy_notes["spend_range_revised"]["old_ranges"]
    ]
    assert [(r["party"], r["lower"], r["upper"]) for r in added] == [
        ("Sample Charging Operator SA", "25000", "49999")
    ]
    (gone,) = run(monitor, forum, 2)["notifications"]
    assert gone["kind"] == "deregistration" and gone["cites"]["previous_revision_id"]
    assert run(monitor, assoc, 2)["notifications"] == []  # same watermark: nothing new
    assert run(monitor, assoc, 3)["notifications"] == []  # nothing changed


def test_registers_about_one_organisation_notify_separately_and_meetings_are_declared(
    conn,
):
    h.load_all(conn)
    dossiers = h.dossiers(conn)
    scopes = h.SCOPES | dossiers["scopes"]
    LobbyingDossierLinks(conn).link_dossier(
        "global",
        h.DOSSIER_NS,
        dossiers["eu"]["dossier_id"],
        principal_id="alice",
        scopes=scopes,
    )
    monitor = LobbyingMonitor(conn)
    client = monitor.create(
        "global",
        "client",
        watch="client",
        key="Fictional Grid Association",
        principal_id="alice",
        scopes=h.SCOPES,
    )
    notes = run(monitor, client, 1)["notifications"]
    assert sorted(n["register"] for n in notes) == [
        "eu-tr",
        "uk-orcl",
    ]  # one per register record
    official = monitor.create(
        "global",
        "mep",
        watch="official",
        key="ep-mep:990001",
        principal_id="alice",
        scopes=h.SCOPES,
    )
    assert [n["kind"] for n in run(monitor, official, 1)["notifications"]] == [
        "meeting_declared"
    ] * 2
    dossier = monitor.create(
        "global",
        "dossier",
        watch="dossier",
        key=dossiers["eu"]["dossier_id"],
        dossier_namespace=h.DOSSIER_NS,
        principal_id="alice",
        scopes=h.SCOPES,
    )
    kinds = sorted(n["kind"] for n in run(monitor, dossier, 1)["notifications"])
    assert kinds == ["interest_declared", "meeting_declared", "meeting_declared"]
    with pytest.raises(LobbyingError):
        monitor.create(
            "global",
            "bad",
            watch="dossier",
            key="x",
            principal_id="alice",
            scopes=h.SCOPES,
        )


def test_live_revisions_from_unverified_sources_are_withheld(conn, monkeypatch):
    item = h.source("eu-transparency-register")
    from src.ingestion.lobbying_sources import LobbyingRegisterAdapter
    from src.kb.lobbying import LobbyingProjector

    def acquire(name):
        adapter = LobbyingRegisterAdapter(
            item,
            transport=lambda **_: {
                "status": 200,
                "content": (h.FIXTURES / name).read_bytes(),
            },
        )
        page = adapter.fetch_page(
            {"operation": "export", "parameters": {}, "limit": 10}, cursor=None
        )
        LobbyingProjector(conn).project_page(
            run_id=name,
            manifest=None,
            source=item,
            records=page.records,
            documents=[],
            page_receipt=page.receipt,
            principal_id="operator",
        )

    acquire("eu_tr_2099-01-15.xml")
    monitor = LobbyingMonitor(conn)
    watch = monitor.create(
        "global",
        "assoc",
        watch="registrant",
        key=h.entry_id(conn, h.EU_ASSOC),
        principal_id="alice",
        scopes=h.SCOPES,
    )
    first = run(monitor, watch, 1)
    assert (
        first["notifications"] == []
        and first["withheld_unverified_live_revisions"] == 1
    )
    monkeypatch.setitem(lobbying_monitoring._DECISIONS, "eu-tr", "verified-live")
    assert [n["kind"] for n in run(monitor, watch, 2)["notifications"]] == [
        "registration"
    ]


def test_notifications_survive_a_restart_and_poll_through_the_subscription_path(
    tmp_path,
):
    import duckdb

    path = str(tmp_path / "monitor.duckdb")
    conn = duckdb.connect(path)
    h.apply(conn, "eu-tr", "eu_tr_2099-01-15.xml")
    monitor = LobbyingMonitor(conn)
    watch = monitor.create(
        "global",
        "assoc",
        watch="registrant",
        key=h.entry_id(conn, h.EU_ASSOC),
        principal_id="alice",
        scopes=h.SCOPES,
    )
    run(monitor, watch, 1)
    conn.close()
    conn = duckdb.connect(path)
    h.apply(conn, "eu-tr", "eu_tr_2099-03-01.xml")
    monitor = LobbyingMonitor(conn)
    assert [n["kind"] for n in run(monitor, watch, 2)["notifications"]] == [
        "spend_range_revised"
    ]
    polled = monitor.poll(
        watch["subscription_id"], principal_id="alice", scopes=h.SCOPES
    )
    assert [e["event_type"] for e in polled["events"]] == [
        "added",
        "changed",
    ] and json.dumps(polled)
    with pytest.raises((LobbyingError, SubscriptionError)):
        monitor.poll(
            watch["subscription_id"],
            principal_id="alice",
            scopes={"knowledge:subscriptions:read", "namespace:global:read"},
        )
    conn.close()
