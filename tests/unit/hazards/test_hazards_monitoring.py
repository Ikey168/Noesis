"""NH12 (#2360): place monitors through subscriptions; cited record-change notices for new, revised and unchanged."""

from __future__ import annotations

import pytest

from src.kb.hazards_monitoring import HazardMonitor
from src.kb.hazards_store import HazardStoreError
from src.kb.subscriptions import SubscriptionStore
from tests.unit.hazards import harness as h
from tests.unit.hazards.test_hazards_queries import place

AEGEAN = {"type": "Polygon", "coordinates": [[[26.4, 37.5], [27.5, 37.5], [27.5, 38.5], [26.4, 38.5], [26.4, 37.5]]]}


def test_new_revised_and_unchanged_records_become_cited_notices_at_committed_watermarks():
    conn = h.connection()
    clock = {"now": h.ms("2099-08-10T04:00:00Z")}
    monitor = HazardMonitor(conn, now=lambda: clock["now"])
    area = place(monitor.store, "Eastern Aegean (authored)", AEGEAN, "GRC")
    watermarks = SubscriptionStore(conn, initialize=False)
    with pytest.raises(HazardStoreError):
        monitor.create(h.NS, "m1", principal_id="analyst", scopes=h.SCOPES, place_id=area, watch=["forecasts"])
    created = monitor.create(h.NS, "m1", principal_id="analyst", scopes=h.SCOPES, place_id=area)
    sid = created["subscription_id"]
    assert created["domain"] == "natural-hazards" and "not a warning" in created["label"]
    with pytest.raises(HazardStoreError) as caught:
        monitor.run(sid, principal_id="analyst", scopes=h.SCOPES)
    assert caught.value.code == "watermark_uncommitted"

    h.apply(conn, "usgs", h.EARLIER["usgs"], at="2099-08-10T03:50:00Z")
    watermarks.commit_watermark(h.NS, 1, kind="ingestion")
    first = monitor.run(sid, principal_id="analyst", scopes=h.SCOPES)
    kinds = sorted((n["kind"], n["cites"]["provider"]) for n in first["notices"])
    assert kinds == [("new_event", "usgs"), ("new_event", "usgs")]  # us7000zz01 and us7000zz02
    assert all(n["cites"]["revision_id"].startswith("hazard-rev:") and "not a warning" in n["label"]
               for n in first["notices"])
    assert first["receipt"]["result_hash"]

    replay = monitor.run(sid, 1, principal_id="analyst", scopes=h.SCOPES)
    assert replay["status"] == "replayed" and replay["notices"] == []
    watermarks.commit_watermark(h.NS, 2, kind="ingestion")
    unchanged = monitor.run(sid, principal_id="analyst", scopes=h.SCOPES)
    assert unchanged["status"] == "evaluated" and unchanged["notices"] == [] and unchanged["receipt"]["events"] == 0

    clock["now"] = h.ms("2099-08-11T01:00:00Z")
    h.apply(conn, "usgs", h.latest("usgs"), at="2099-08-11T00:00:00Z")
    h.apply(conn, "gdacs", h.EARLIER["gdacs"], at="2099-08-11T00:30:00Z")
    watermarks.commit_watermark(h.NS, 3, kind="ingestion")
    third = monitor.run(sid, principal_id="analyst", scopes=h.SCOPES)
    by_kind = {}
    for notice in third["notices"]:
        by_kind.setdefault(notice["kind"], []).append(notice)
    revised = {n["cites"]["revision_key"]: n for n in by_kind["parameter_revision"]}
    usgs = revised[str(h.ms("2099-08-10T09:00:00Z"))]
    assert {c["parameter"] for c in usgs["changes"]} >= {"magnitude", "depth", "status", "geometry"}
    assert "revised by the publisher" in usgs["message"]
    deleted = revised[str(h.ms("2099-08-10T06:00:00Z"))]
    assert [c for c in deleted["changes"] if c["parameter"] == "status"][0]["after"] == "deleted"
    assert [n["cites"]["provider"] for n in by_kind["new_event"]] == ["gdacs"]
    (alert,) = by_kind["new_alert"]
    assert "level Orange" in alert["message"] and alert["cites"]["revision_key"] == "episode:1500001"
    polled = monitor.poll(sid, principal_id="analyst", scopes=h.SCOPES)
    assert len(polled["events"]) == 2 + 4
    with pytest.raises(HazardStoreError):
        monitor.run(sid, principal_id="someone-else", scopes=h.SCOPES)
