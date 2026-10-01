"""Monitor device changes through subscriptions: new, revised and unchanged (#2708, MD11)."""

from __future__ import annotations

import pytest

from src.ingestion.medical_devices_sources import FIXTURE_SECRET, fixture_transport
from src.kb.medical_devices_monitoring import MedicalDevicesMonitor
from src.kb.medical_devices_records import MedicalDevicesError
from src.kb.subscriptions import SubscriptionStore
from tests.unit import medical_devices_harness as h


def env():
    conn = h.connection()
    h.load_all(conn)
    return conn, MedicalDevicesMonitor(conn)


def run(conn, monitor, subscription, watermark):
    SubscriptionStore(conn).commit_watermark(h.NS, watermark)
    return monitor.run(subscription, principal_id="alice", scopes=h.SCOPES)


def test_new_revised_and_unchanged_records_with_cited_revisions():
    conn, monitor = env()
    device = monitor.create(h.NS, "pump", watch="device", key="K999901", principal_id="alice", scopes=h.SCOPES)
    valve = monitor.create(h.NS, "valve", watch="product-code", key="zzb", principal_id="alice", scopes=h.SCOPES)
    first = run(conn, monitor, device["subscription_id"], 1)
    kinds = sorted(n["kind"] for n in first["notifications"])
    assert kinds == ["classification_published", "clearance_published", "device_version_published",
                     "recall_published", "recall_published"]
    assert all(n["cites"]["revision_id"] and n["cites"]["previous_revision_id"] is None
               for n in first["notifications"])
    assert not [n for n in first["notifications"] if ":maude" in n["record_key"]]  # reports are never notified
    run(conn, monitor, valve["subscription_id"], 1)
    h.load_all(conn, version="v2", run_id="v2")
    revised = run(conn, monitor, device["subscription_id"], 2)
    changes = {(n["kind"], n["record_key"]) for n in revised["notifications"]}
    assert ("recall_status_changed", "medical-devices:fda:recall:Z-9901-2099") in changes
    assert ("device_version_published", f"medical-devices:gudid:di:{h.EXAMPLE_DI}") in changes
    status = [n for n in revised["notifications"] if n["kind"] == "recall_status_changed"]
    assert {(n["before"], n["after"]) for n in status} == {("Open, Classified", "Terminated"),
                                                           ("Ongoing", "Terminated")}
    assert all(n["cites"]["previous_revision_id"] and n["cites"]["revision_id"] != n["cites"]["previous_revision_id"]
               for n in revised["notifications"])
    valve_notes = run(conn, monitor, valve["subscription_id"], 2)["notifications"]
    assert {n["kind"] for n in valve_notes} == {"supplement_published", "supplement_listed"}
    assert run(conn, monitor, device["subscription_id"], 3)["notifications"] == []  # unchanged


def test_refresh_is_bounded_idempotent_and_leaves_receipts():
    conn, monitor = env()
    source = h.source("devices-eudamed-certificates")
    transport = fixture_transport(h.native_pages("devices-eudamed-certificates", "v2"))
    first = monitor.refresh(source, run_id="refresh-1", principal_id="alice", scopes=h.SCOPES, transport=transport,
                            secret=FIXTURE_SECRET)
    assert first["counts"]["revised"] == 1 and first["complete"] and len(first["receipts"]) == 1
    again = monitor.refresh(source, run_id="refresh-2", principal_id="alice", scopes=h.SCOPES, transport=transport)
    assert again["counts"]["unchanged"] == 1 and again["counts"].get("revised", 0) == 0
    watch = monitor.create(h.NS, "maker", watch="manufacturer", key="DE-MF-000099901", principal_id="alice",
                           scopes=h.SCOPES)
    notes = run(conn, monitor, watch["subscription_id"], 1)["notifications"]
    assert {n["kind"] for n in notes} >= {"certificate_published"}
    with pytest.raises(MedicalDevicesError):
        monitor.create(h.NS, "bad", watch="product-code", key="Z1", principal_id="alice", scopes=h.SCOPES)
    with pytest.raises(MedicalDevicesError):
        monitor.refresh(source, run_id="r", principal_id="alice", scopes=h.READ_ONLY, transport=transport)
