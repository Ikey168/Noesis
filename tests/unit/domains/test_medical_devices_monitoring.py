"""Monitor devices, manufacturers and product codes through subscriptions: new, revised, unchanged (MD11)."""

from __future__ import annotations

import pytest

from src.kb.medical_devices_monitoring import MedicalDeviceMonitor
from src.kb.medical_devices_records import MedicalDeviceError
from src.kb.subscriptions import SubscriptionStore
from tests.unit import medical_devices_harness as h


@pytest.fixture()
def env():
    conn = h.connection()
    clock = h.Clock()
    h.load_all(conn, now=clock)
    monitor = MedicalDeviceMonitor(conn, now=clock)
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    return conn, clock, monitor


def run(monitor, subscription, watermark=None):
    return monitor.run(subscription["subscription_id"], watermark, principal_id="analyst", scopes=h.SCOPES)


def test_new_revised_and_unchanged_records(env):
    conn, clock, monitor = env
    code = monitor.create(h.NS, "code", watch="product-code", key="ZXA", principal_id="analyst", scopes=h.SCOPES)
    device = monitor.create(h.NS, "lead", watch="device", key="P999001", principal_id="analyst", scopes=h.SCOPES)
    cert = monitor.create(h.NS, "pump", watch="device", key="0899999EXFLOWSENSEZ7", principal_id="analyst",
                          scopes=h.SCOPES)
    first = run(monitor, code)
    assert sorted(n["kind"] for n in first["notifications"]) == ["clearance_published", "clearance_published",
                                                                "recall_published"]
    assert all(n["cites"]["revision_id"] and n["cites"]["previous_revision_id"] is None
               for n in first["notifications"])
    run(monitor, device)
    run(monitor, cert)
    # Unchanged: a replayed acquisition and a new watermark deliver nothing.
    h.load_all(conn, now=clock)
    SubscriptionStore(conn).commit_watermark(h.NS, 2)
    assert run(monitor, code)["notifications"] == []
    # Revised: the recall terminated, a supplement published, the certificate suspended, K999002 removed.
    h.load_all(conn, v2=True, now=clock)
    SubscriptionStore(conn).commit_watermark(h.NS, 3)
    notes = {n["kind"]: n for n in run(monitor, code)["notifications"]}
    assert set(notes) == {"recall_status_changed", "record_removed_by_source"}
    status = notes["recall_status_changed"]
    assert (status["before"], status["after"]) == ("Open, Classified", "Terminated")
    assert status["cites"]["previous_revision_id"] and status["cites"]["revision_id"] != status["cites"][
        "previous_revision_id"]
    assert [n["kind"] for n in run(monitor, device)["notifications"]] == ["supplement_published"]
    (suspended,) = run(monitor, cert)["notifications"]
    assert suspended["kind"] == "certificate_status_changed" and suspended["after"] == "Suspended"
    # A replayed watermark is idempotent.
    assert monitor.run(code["subscription_id"], 3, principal_id="analyst", scopes=h.SCOPES)["status"] == "replayed"
    polled = monitor.poll(code["subscription_id"], principal_id="analyst", scopes=h.SCOPES)
    assert len(polled["events"]) == 5


def test_manufacturer_watch_and_validation(env):
    _, _, monitor = env
    subscription = monitor.create(h.NS, "maker", watch="manufacturer",
                                  key="medical-devices:manufacturer:openfda-device:exampla-medical-devices",
                                  principal_id="analyst", scopes=h.SCOPES)
    kinds = sorted(n["kind"] for n in run(monitor, subscription)["notifications"])
    assert kinds == ["approval_published", "clearance_published", "clearance_published", "recall_published"]
    with pytest.raises(MedicalDeviceError):
        monitor.create(h.NS, "bad", watch="manufacturer", key="Exampla", principal_id="analyst", scopes=h.SCOPES)
    with pytest.raises(MedicalDeviceError):
        monitor.create(h.NS, "bad", watch="device", key="Exampla FlowSense", principal_id="analyst",
                       scopes=h.SCOPES)


def test_refresh_is_bounded_idempotent_and_receipted(env):
    _, _, monitor = env
    source = h.source("clinical-devices-openfda-recalls")
    from src.ingestion.medical_devices_sources import fixture_transport

    result = monitor.refresh(source, run_id="run:refresh", principal_id="operator", scopes=h.SCOPES,
                             transport=fixture_transport(h.native_pages("clinical-devices-openfda-recalls")))
    assert result["complete"] and result["units"] == 1 and result["counts"]["unchanged"] == 1
    assert result["receipts"] and result["receipts"][0]["receipt"]["requests"]
    revised = monitor.refresh(source, run_id="run:refresh-2", principal_id="operator", scopes=h.SCOPES,
                              transport=fixture_transport(h.native_pages("clinical-devices-openfda-recalls",
                                                                         v2=True)))
    assert revised["counts"]["revised"] == 1
