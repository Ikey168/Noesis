"""WC10 (#2796): waste monitors through subscriptions; new, revised, removed and unchanged cases, offline."""

from __future__ import annotations

import pytest

from src.ingestion.waste_sources import fixture_transport
from src.kb.waste_identity import WasteIdentity
from src.kb.waste_monitoring import WasteMonitor
from src.kb.waste_records import WasteError
from tests.unit import waste_harness as h


class Clock:
    def __init__(self, start: int = h.SECOND_RETRIEVAL) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value


def _refresh(monitor, provider, *, revision=False, retrieved):
    item = h.revision_source(provider) if revision else h.source(provider)
    return monitor.refresh(h.NS, item, principal_id="svc", scopes=h.SCOPES,
                           transport=fixture_transport(h.pages(provider, revision)), retrieved_at_ms=retrieved)


def test_place_and_indicator_watches_notify_new_revised_and_removed_records_once_with_citations():
    conn = h.connection()
    monitor = WasteMonitor(conn, now=Clock())
    first = _refresh(monitor, "eurostat-waste", retrieved=h.FIRST_RETRIEVAL)
    assert first["status"] == "complete" and first["new_releases"] == 2
    places = h.register_places(conn)
    identity = WasteIdentity(conn)
    identity.propose_places(h.NS, principal_id="svc", scopes=h.SCOPES)
    for a in identity.assertions(h.NS, scopes=h.SCOPES, state="proposed"):
        identity.review(h.NS, a["assertion_id"], "accept", "checked", principal_id=h.REVIEWER, scopes=h.SCOPES)
    place = monitor.create(h.NS, "fr", target={"place_id": places["fr"]}, principal_id="analyst",
                           scopes=h.SCOPES)["subscription_id"]
    indicator = monitor.create(h.NS, "gen", target={"concept": "waste_generated"}, principal_id="analyst",
                               scopes=h.SCOPES)["subscription_id"]
    initial = monitor.run(place, principal_id="analyst", scopes=h.SCOPES)["notifications"]
    assert {n["kind"] for n in initial} == {"new_release"} and len(initial) == 5  # 2 wasgen + 3 wastrt for FR
    assert all(n["citation"]["vintage_id"] == n["vintage_id"] for n in initial)
    assert len(monitor.run(indicator, principal_id="analyst", scopes=h.SCOPES)["notifications"]) == 4
    # Unchanged: re-reading the same files adds nothing and a replay emits nothing.
    again = _refresh(monitor, "eurostat-waste", retrieved=h.FIRST_RETRIEVAL)
    assert again["new_releases"] == 0 and again["unchanged_releases"] == 2
    assert monitor.run(place, principal_id="analyst", scopes=h.SCOPES)["notifications"] == []
    # Revised and removed.
    _refresh(monitor, "eurostat-waste", revision=True, retrieved=h.SECOND_RETRIEVAL)
    notices = monitor.run(place, principal_id="analyst", scopes=h.SCOPES)["notifications"]
    assert sorted(n["kind"] for n in notices) == ["removed_by_source"]  # FR landfill no longer stated
    removal = notices[0]
    assert removal["record"]["area"]["code"] == "FR" and removal["previous_vintage_id"]
    assert "assess" not in removal["message"].casefold()
    revised = monitor.run(indicator, principal_id="analyst", scopes=h.SCOPES)["notifications"]
    change = next(n for n in revised if n["kind"] == "revised_value")
    periods = {r["period"]: r for r in change["what_changed"]["revised"]}
    assert periods["2094"]["before"]["value"] == "401200000" and periods["2094"]["after"]["value"] == "403900000"
    assert periods["2096"]["before"]["flags"] == {"OBS_FLAG": "p"} and periods["2096"]["after"]["flags"] == {}


def test_a_facility_watch_notifies_corrected_new_and_removed_transfer_rows():
    conn = h.connection()
    h.load_facilities(conn)
    monitor = WasteMonitor(conn, now=Clock())
    _refresh(monitor, "eea-industry-waste-transfers", retrieved=h.FIRST_RETRIEVAL)
    sub = monitor.create(h.NS, "hkw", target={"inspire_id": h.FACILITY_1}, principal_id="analyst",
                         scopes=h.SCOPES)["subscription_id"]
    initial = monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)["notifications"]
    assert {n["kind"] for n in initial} == {"new_release"} and len(initial) == 3
    _refresh(monitor, "eea-industry-waste-transfers", revision=True, retrieved=h.SECOND_RETRIEVAL)
    notices = {n["kind"]: n for n in monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)["notifications"]}
    assert set(notices) == {"revised_value", "removed_by_source"}
    corrected = notices["revised_value"]["what_changed"]
    assert corrected["before"] == {"quantity": "840.25", "method": "C"} and corrected["after"] == {
        "quantity": "851.75", "method": "M"} and corrected["reporting_year"] == 2096
    assert notices["revised_value"]["citation"]["dataset_version"]["stated"] == "v13.0 (authored fixture)"
    assert "not zero" in notices["removed_by_source"]["what_changed"]["statement"]
    other = monitor.create(h.NS, "klingenberg", target={"inspire_id": h.FACILITY_2}, principal_id="analyst",
                           scopes=h.SCOPES)["subscription_id"]
    kinds = sorted(n["kind"] for n in monitor.run(other, principal_id="analyst", scopes=h.SCOPES)["notifications"])
    assert kinds == ["new_release", "new_release"]  # the original row and the new 2099-release row


def test_a_failed_run_never_produces_a_removal_notice_and_refresh_paces_oecd():
    conn = h.connection()
    monitor = WasteMonitor(conn, now=Clock())
    first = _refresh(monitor, "oecd-municipal-waste", retrieved=h.FIRST_RETRIEVAL)
    assert first["status"] == "complete" and first["retry_at"]  # one request per 60 seconds
    sub = monitor.create(h.NS, "oecd", target={"provider": "oecd-municipal-waste"}, principal_id="analyst",
                         scopes=h.SCOPES)["subscription_id"]
    assert len(monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)["notifications"]) == 2
    paced = _refresh(monitor, "oecd-municipal-waste", retrieved=h.FIRST_RETRIEVAL)
    assert paced["status"] == "rate_limited_wait" and paced["releases"] == []
    monitor.now = lambda: h.SECOND_RETRIEVAL + 10_000_000
    monitor.store.now = monitor.now

    def failing(*, url, params, headers, timeout):
        return {"status": 503, "headers": {}, "content": b""}

    before = conn.execute("SELECT count(*) FROM waste_vintages").fetchone()[0]
    stopped = monitor.refresh(h.NS, h.source("oecd-municipal-waste"), principal_id="svc", scopes=h.SCOPES,
                              transport=failing)
    assert stopped["status"] == "stopped" and stopped["stopped"]["code"] == "source_unavailable"
    assert conn.execute("SELECT count(*) FROM waste_vintages").fetchone()[0] == before
    assert monitor.store.provider_state(h.NS, "oecd-municipal-waste")["stale"] is True
    assert monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)["notifications"] == []
    with pytest.raises(WasteError):
        monitor.create(h.NS, "bad", target={"concept": "gdp"}, principal_id="analyst", scopes=h.SCOPES)
    with pytest.raises(WasteError):
        monitor.create(h.NS, "bad", target={"weather": "x"}, principal_id="analyst", scopes=h.SCOPES)
