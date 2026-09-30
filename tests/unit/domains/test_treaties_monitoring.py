"""Treaty and participant monitors through subscriptions (#2631)."""

from __future__ import annotations

import pytest

from src.ingestion.treaties_sources import fixture_transport
from src.kb.subscriptions import SubscriptionStore
from src.kb.treaties_monitoring import TreatiesMonitor, notifiable
from src.kb.treaties_records import TreatiesError
from tests.unit import treaties_harness as h


@pytest.fixture()
def env():
    conn = h.connection()
    clock = h.Clock()
    h.load_all(conn)
    subscriptions = SubscriptionStore(conn)
    subscriptions.commit_watermark(h.NS, 1)
    monitor = TreatiesMonitor(conn, now=clock)
    yield conn, monitor, subscriptions
    conn.close()


def run(monitor, subscription_id, watermark):
    return monitor.run(subscription_id, watermark, principal_id="alice", scopes=h.SCOPES)


def test_new_revised_removed_and_unchanged_cases(env):
    conn, monitor, subscriptions = env
    treaty = monitor.create(h.NS, "untc", watch="treaty", key="untc:XXIX-99", principal_id="alice",
                            scopes=h.SCOPES)["subscription_id"]
    first = run(monitor, treaty, 1)
    assert {n["kind"] for n in first["notifications"]} == {"new_treaty", "new_action"}
    h.load_all(conn)  # unchanged re-read
    subscriptions.commit_watermark(h.NS, 2)
    assert run(monitor, treaty, 2)["notifications"] == []
    h.load_all(conn, v2=True)
    subscriptions.commit_watermark(h.NS, 3)
    notices = {n["kind"]: n for n in run(monitor, treaty, 3)["notifications"]}
    assert {"depositary_correction", "action_removed_by_source", "new_action", "treaty_revised"} <= set(notices)
    correction = notices["depositary_correction"]
    assert correction["changed_fields"] == ["action_date"] and "'2098-09-03' -> '2098-09-04'" in correction["message"]
    assert correction["cites"]["revision_no"] == 2 and correction["cites"]["previous"]["revision_no"] == 1
    assert "not an assessment" in correction["notice"]
    assert "no longer listed" in notices["action_removed_by_source"]["message"]
    assert run(monitor, treaty, 3)["status"] == "replayed"


def test_participant_monitor_reports_entry_into_force_and_denunciation(env):
    conn, monitor, subscriptions = env
    participant = monitor.create(h.NS, "germany", watch="participant", key="Germany", principal_id="alice",
                                 scopes=h.SCOPES)["subscription_id"]
    initial = run(monitor, participant, 1)["notifications"]
    assert "entry_into_force" in {n["kind"] for n in initial}
    h.apply(conn, h.COE, v2=True)
    subscriptions.commit_watermark(h.NS, 2)
    later = run(monitor, participant, 2)["notifications"]
    denunciations = [n for n in later if n["summary"]["action_type"] == "denunciation"]
    assert denunciations and all(n["kind"] == "new_action" for n in denunciations)
    assert all(n["evidence_origin"] == "fixture" for n in later)
    assert monitor.poll(participant, principal_id="alice", scopes=h.SCOPES)["events"]


def test_refresh_is_bounded_idempotent_and_leaves_receipts(env):
    _conn, monitor, _ = env
    source = h.source(h.COE)
    transport = fixture_transport(h.native_pages(h.COE, v2=True))
    first = monitor.refresh(source, run_id="refresh-1", principal_id="operator", scopes=h.SCOPES,
                            transport=transport)
    assert first["complete"] and first["units"] == 1 and first["counts"]["action_revisions"] == 3
    assert first["receipts"][0]["source_id"] == h.COE
    second = monitor.refresh(source, run_id="refresh-2", principal_id="operator", scopes=h.SCOPES,
                             transport=transport)
    assert second["counts"]["action_revisions"] == 0 and second["counts"]["unchanged"] == 15


def test_watches_are_validated_and_live_unverified_revisions_are_withheld(env):
    _, monitor, _ = env
    with pytest.raises(TreatiesError):
        monitor.create(h.NS, "bad", watch="obligation", key="x", principal_id="alice", scopes=h.SCOPES)
    with pytest.raises(TreatiesError):
        monitor.create(h.NS, "bad", watch="treaty", key="Some Convention", principal_id="alice", scopes=h.SCOPES)
    assert notifiable("coe-treaty-office", "fixture") and not notifiable("coe-treaty-office", "live")
    assert not notifiable("untc", "live")
