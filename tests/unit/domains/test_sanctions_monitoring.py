"""Sanctions monitors are knowledge subscriptions replaying snapshots and annex editions (#1970)."""

from __future__ import annotations

import pytest

from src.kb.sanctions import SanctionsError
from src.kb.sanctions_monitoring import SanctionsMonitor
from src.kb.subscriptions import SubscriptionStore
from tests.unit import sanctions_harness as h


def designation(conn, list_id, entry_id):
    return conn.execute(
        "SELECT designation_id FROM sanctions_designations WHERE list_id=? AND list_entry_id=?",
        [list_id, entry_id],
    ).fetchone()[0]


def commit(conn, watermark):
    SubscriptionStore(conn).commit_watermark("global", watermark, kind="ingestion")


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def test_designation_and_programme_monitors_replay_two_snapshots():
    conn = h.connection()
    h.apply(conn, "eu", "eu_fsf_2026-03-01.xml")
    monitor = SanctionsMonitor(conn)
    vessel = monitor.create(
        "global",
        "vessel",
        watch="designation",
        key=designation(conn, "eu", "EU.9002.02"),
        principal_id="analyst",
        scopes=h.SCOPES,
    )
    programme = monitor.create(
        "global",
        "ukr",
        watch="programme",
        key="UKR",
        list_id="eu",
        principal_id="analyst",
        scopes=h.SCOPES,
        delivery={"kind": "queue", "destination_ref": "sanctions-events"},
    )
    commit(conn, 1)
    first = monitor.run(
        vessel["subscription_id"], 1, principal_id="analyst", scopes=h.SCOPES
    )
    assert kinds(first) == ["listed"]
    assert kinds(
        monitor.run(
            programme["subscription_id"], 1, principal_id="analyst", scopes=h.SCOPES
        )
    ) == ["listed", "listed", "listed"]
    # Re-processing the same snapshot at the same watermark produces nothing new.
    replay = monitor.run(
        vessel["subscription_id"], 1, principal_id="analyst", scopes=h.SCOPES
    )
    assert replay["status"] == "replayed" and replay["notifications"] == []
    assert (
        h.apply(conn, "eu", "eu_fsf_2026-03-01.xml", run_id="again")["status"]
        == "unchanged"
    )
    commit(conn, 2)
    assert (
        monitor.run(
            vessel["subscription_id"], 2, principal_id="analyst", scopes=h.SCOPES
        )["notifications"]
        == []
    )
    h.apply(conn, "eu", "eu_fsf_2026-06-01.xml")
    commit(conn, 3)
    delisted = monitor.run(
        vessel["subscription_id"], 3, principal_id="analyst", scopes=h.SCOPES
    )
    assert kinds(delisted) == ["delisted"]
    cites = delisted["notifications"][0]["cites"]["snapshots_compared"]
    assert [c["publication_date"] for c in cites] == ["2026-03-01", "2026-06-01"]
    changes = monitor.run(
        programme["subscription_id"], 3, principal_id="analyst", scopes=h.SCOPES
    )
    assert kinds(changes) == ["amended", "delisted", "listed"]
    for note in changes["notifications"]:
        assert h.forbidden_keys(note) == [] and set(note["cites"]) == {
            "revision_id",
            "snapshots_compared",
        }
    outbox = conn.execute(
        "SELECT count(*) FROM knowledge_subscription_outbox WHERE delivery_kind='queue'"
    ).fetchone()[0]
    assert (
        outbox == 6
    )  # 3 listed + amended + delisted + listed, through the existing outbox
    polled = monitor.poll(
        vessel["subscription_id"], principal_id="analyst", scopes=h.SCOPES
    )
    assert [e["event_type"] for e in polled["events"]] == ["added", "changed"]


def test_a_control_code_monitor_reports_a_new_edition_citing_both_versions():
    conn2 = h.connection()
    h.load_legal(conn2, "cellar-dual-use-2021-821")
    monitor2 = SanctionsMonitor(conn2)
    watch2 = monitor2.create(
        "global",
        "1c350",
        watch="control-code",
        key="1C350",
        principal_id="analyst",
        scopes=h.SCOPES,
    )
    edition = conn2.execute(
        "SELECT version_id FROM legal_versions WHERE record_json LIKE '%20251115%' AND "
        "content_coverage='captured-text'"
    ).fetchone()[0]
    passages = conn2.execute(
        "SELECT * FROM legal_passages WHERE version_id=?", [edition]
    ).fetchall()
    conn2.execute(
        "DELETE FROM legal_passages WHERE version_id=?", [edition]
    )  # the newer edition is not yet there
    commit(conn2, 1)
    first = monitor2.run(
        watch2["subscription_id"], 1, principal_id="analyst", scopes=h.SCOPES
    )
    assert kinds(first) == ["edition_in_view"]
    assert (
        first["notifications"][0]["message"]
        == "1C350 is stated in edition 02021R0821-20241115."
    )
    for row in passages:
        conn2.execute("INSERT INTO legal_passages VALUES (?,?,?,?,?)", list(row))
    commit(conn2, 2)
    new = monitor2.run(
        watch2["subscription_id"], 2, principal_id="analyst", scopes=h.SCOPES
    )
    assert kinds(new) == ["new_edition"]
    assert new["notifications"][0]["cites"]["text_changed"] is True
    assert len(new["notifications"][0]["cites"]["versions_compared"]) == 2


def test_monitors_need_a_valid_watch_and_a_committed_watermark():
    conn = h.connection()
    h.apply(conn, "un", "un_sc_2026-03-10.xml")
    monitor = SanctionsMonitor(conn)
    with pytest.raises(SanctionsError):
        monitor.create(
            "global",
            "x",
            watch="name",
            key="Ivan",
            principal_id="analyst",
            scopes=h.SCOPES,
        )
    with pytest.raises(SanctionsError):
        monitor.create(
            "global",
            "x",
            watch="programme",
            key="Fixture Regime",
            principal_id="analyst",
            scopes=h.SCOPES,
        )
    created = monitor.create(
        "global",
        "p",
        watch="programme",
        key="Fixture Regime",
        list_id="un",
        principal_id="analyst",
        scopes=h.SCOPES,
    )
    with pytest.raises(SanctionsError) as caught:
        monitor.run(created["subscription_id"], principal_id="analyst", scopes=h.SCOPES)
    assert caught.value.code == "watermark_uncommitted"
