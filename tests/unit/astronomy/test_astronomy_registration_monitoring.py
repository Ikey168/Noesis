"""Registration monitors through subscriptions: each event type, cited and dated (#2224, SO11)."""

from __future__ import annotations

import duckdb
import pytest

from src.kb.astronomy_records import AstronomyError
from src.kb.astronomy_registration_monitoring import RegistrationMonitor
from src.kb.subscriptions import SubscriptionStore
from tests.unit.astronomy import harness as ah
from tests.unit.astronomy import registration_harness as h

WATCHES = {
    "fictsat1": ("object", "2099-001A"),
    "fictsat3": ("object", "99904"),
    "examplia": ("registering_state", "Republic of Examplia"),
    "operator": ("operator", "Examplia Orbital Services"),
}


def poll(conn, monitor, subs, watermark):
    SubscriptionStore(conn).commit_watermark(ah.NS, watermark, kind="ingestion")
    return {name: monitor.run(sid, principal_id=ah.PRINCIPAL, scopes=ah.SCOPES) for name, sid in subs.items()}


def events(result):
    return sorted(n["event"] for n in result["notifications"])


def test_each_event_type_is_notified_once_with_cited_dated_revisions():
    conn = duckdb.connect(":memory:")
    h.acquire_all(conn, until="2099-04-02")
    monitor = RegistrationMonitor(conn)
    subs = {name: monitor.create(ah.NS, name, watch=watch, target=target, principal_id=ah.PRINCIPAL,
                                 scopes=ah.SCOPES)["subscription_id"]
            for name, (watch, target) in WATCHES.items()}
    first = poll(conn, monitor, subs, 1)
    assert all(r["baseline"] and r["notifications"] == [] for r in first.values())

    h.acquire_all(conn, until="2099-06-19")
    second = poll(conn, monitor, subs, 2)
    assert "supervision_transferred" in events(second["fictsat1"])
    assert "operator_changed" in events(second["fictsat1"])
    assert "reentry_predicted" in events(second["fictsat1"])
    assert "registration_published" in events(second["fictsat3"])
    assert "status_changed" in events(second["fictsat3"])
    assert events(second["examplia"]) == ["supervision_transferred"]
    assert events(second["operator"]) == ["operator_changed"]
    transfer = next(n for n in second["fictsat1"]["notifications"] if n["event"] == "supervision_transferred")
    assert transfer["dated"] == "2099-05-15" and transfer["new_revision"]["revision_id"]
    assert transfer["source"] == "unoosa-registration-documents" and second["fictsat1"]["refresh"]["receipts"]

    h.acquire_all(conn)
    third = poll(conn, monitor, subs, 3)
    got = events(third["fictsat1"])
    assert "reentry_confirmed" in got and "status_changed" in got  # confirmed report, notice and index decay
    confirmed = [n for n in third["fictsat1"]["notifications"] if n["event"] == "reentry_confirmed"]
    assert any(n["old_revision"] and n["source"] == "aerospace-reentry" for n in confirmed)

    # Replaying the watermark or an unchanged refresh notifies nothing.
    again = poll(conn, monitor, subs, 3)
    assert all(r["notifications"] == [] for r in again.values())
    h.acquire(conn, "aerospace", "2099-06-22")
    fourth = poll(conn, monitor, subs, 4)
    assert all(r["notifications"] == [] for r in fourth.values())
    polled = monitor.poll(subs["fictsat1"], principal_id=ah.PRINCIPAL, scopes=ah.SCOPES)
    assert polled


def test_invalid_watches_are_refused():
    conn = duckdb.connect(":memory:")
    h.acquire(conn, "index", "2099-03-20")
    monitor = RegistrationMonitor(conn)
    with pytest.raises(AstronomyError):
        monitor.create(ah.NS, "x", watch="launch", target="y", principal_id=ah.PRINCIPAL, scopes=ah.SCOPES)
    with pytest.raises(AstronomyError):
        monitor.create(ah.NS, "x", watch="object", target=" ", principal_id=ah.PRINCIPAL, scopes=ah.SCOPES)
