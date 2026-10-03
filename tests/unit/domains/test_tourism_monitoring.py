"""TO09 (#2739): tourism monitors through subscriptions; new, revised, removed and unchanged releases."""

from __future__ import annotations

import pytest

from src.ingestion.tourism_sources import fixture_transport
from src.kb.tourism_monitoring import TourismMonitor
from src.kb.tourism_records import TourismError
from tests.unit import tourism_harness as h


class Clock:
    def __init__(self, start: int = h.SECOND_RETRIEVAL) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value


def _monitor(conn):
    return TourismMonitor(conn, now=Clock())


def test_new_revised_removed_and_unchanged_releases_notify_once_with_citations():
    conn = h.connection()
    monitor = _monitor(conn)
    first = monitor.refresh(h.NS, h.source("occupancy"), principal_id="svc", scopes=h.SCOPES,
                            transport=fixture_transport(h.pages("occupancy")), retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert first["status"] == "complete" and first["new_releases"] == 3
    sub = monitor.create(h.NS, "germany", target={"area": {"scheme": "eurostat-geo", "code": "DE"},
                                                  "frequency": "monthly"},
                         principal_id="analyst", scopes=h.SCOPES)["subscription_id"]
    run = monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)
    assert {n["kind"] for n in run["notifications"]} == {"new_release"} and len(run["notifications"]) == 6
    assert all(n["citation"]["vintage_id"] == n["vintage_id"] for n in run["notifications"])
    # Unchanged: re-reading the same files adds nothing and a replay emits nothing.
    again = monitor.refresh(h.NS, h.source("occupancy"), principal_id="svc", scopes=h.SCOPES,
                            transport=fixture_transport(h.pages("occupancy")), retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert again["new_releases"] == 0 and again["unchanged_releases"] == 3
    assert monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)["notifications"] == []
    # Revised: provisional months restated, a new month, and the foreign arrivals series removed by the source.
    monitor.refresh(h.NS, h.revision_source("occupancy"), principal_id="svc", scopes=h.SCOPES,
                    transport=fixture_transport(h.pages("occupancy", revision=True)),
                    retrieved_at_ms=h.SECOND_RETRIEVAL)
    revised = monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)["notifications"]
    kinds = sorted(n["kind"] for n in revised)
    assert kinds.count("removed_by_source") == 1 and kinds.count("new_period") == 5
    assert kinds.count("revised_value") == 5
    change = next(n for n in revised if n["kind"] == "revised_value")
    (row,) = change["what_changed"]["revised"]
    assert row["period"] == "2096-04" and row["before"]["flags"] == {"OBS_FLAG": "p"} and row["after"]["flags"] == {}
    assert change["previous_vintage_id"] and change["citation"]["vintage_id"] == change["vintage_id"]
    removal = next(n for n in revised if n["kind"] == "removed_by_source")
    assert removal["series"]["residence"] == "FOR" and "no longer states" in removal["what_changed"]["statement"]
    assert "assess" not in change["message"].casefold()
    assert monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)["notifications"] == []


def test_a_place_watch_hears_a_nuts_version_change_and_a_definition_change():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    monitor = _monitor(conn)
    places = h.register_places(conn, keys=("berlin", "berlin-2024"))
    from src.kb.tourism_identity import TourismIdentity

    h.apply(conn, "nuts2024", revision=True, retrieved_at_ms=h.THIRD_RETRIEVAL)
    identity = TourismIdentity(conn)
    for assertion in identity.propose_places(h.NS, principal_id="analyst", scopes=h.SCOPES)["assertions"]:
        if assertion["state"] == "proposed":
            identity.review(h.NS, assertion["assertion_id"], "accept", "code", principal_id="reviewer",
                            scopes=h.SCOPES)
    sub = monitor.create(h.NS, "berlin-2024", target={"place_id": places["berlin-2024"]}, principal_id="analyst",
                         scopes=h.SCOPES)["subscription_id"]
    notices = monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)["notifications"]
    assert {n["kind"] for n in notices} == {"new_release"}
    assert {n["series"]["area"]["nuts_version"] for n in notices} == {"2024"}
    old = monitor.create(h.NS, "berlin-2021", target={"area": {"scheme": "eurostat-geo", "code": "DE30",
                                                               "nuts_version": "2021"}, "concept": "bed_places"},
                         principal_id="analyst", scopes=h.SCOPES)["subscription_id"]
    kinds = [n["kind"] for n in monitor.run(old, principal_id="analyst", scopes=h.SCOPES)["notifications"]]
    assert "removed_by_source" in kinds
    # A changed national threshold the source states is a definition_change notice.
    item = h.revision_source("nuts2024")
    document = item["tourism_statistics"]["documents"][0]
    document["definition"] = {**document["definition"], "coverage_thresholds": {
        "DE": "establishments with 8 or more bed places (restated national threshold; fixture)"}}
    page = dict(h.pages("nuts2024", revision=True)[0])
    page["body"] = page["body"].replace("15/09/24 11:00:00", "20/09/24 11:00:00")
    monitor.refresh(h.NS, item, principal_id="svc", scopes=h.SCOPES, transport=fixture_transport([page]),
                    retrieved_at_ms=h.THIRD_RETRIEVAL)
    changed = monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)["notifications"]
    assert changed and {n["kind"] for n in changed} == {"definition_change"}
    assert "coverage_threshold" in changed[0]["what_changed"]["changed_fields"]


def test_a_failed_refresh_is_receipted_bounded_and_never_produces_a_removal_notice():
    conn = h.connection()
    monitor = _monitor(conn)
    bounded = monitor.refresh(h.NS, h.source("occupancy"), principal_id="svc", scopes=h.SCOPES,
                              transport=fixture_transport(h.pages("occupancy")), max_documents=1,
                              retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert bounded["status"] == "bounded" and len(bounded["releases"]) == 1
    monitor.refresh(h.NS, h.source("occupancy"), principal_id="svc", scopes=h.SCOPES,
                    transport=fixture_transport(h.pages("occupancy")), retrieved_at_ms=h.FIRST_RETRIEVAL)
    sub = monitor.create(h.NS, "all", target={"provider": "eurostat-tourism-occupancy"}, principal_id="analyst",
                         scopes=h.SCOPES)["subscription_id"]
    monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)

    def broken(*, url, params, headers, timeout):
        return {"status": 503, "headers": {}, "content": b""}

    before = conn.execute("SELECT count(*) FROM tourism_vintages").fetchone()[0]
    stopped = monitor.refresh(h.NS, h.revision_source("occupancy"), principal_id="svc", scopes=h.SCOPES,
                              transport=broken, retrieved_at_ms=h.SECOND_RETRIEVAL)
    assert stopped["status"] == "stopped" and stopped["stopped"]["code"] == "source_unavailable"
    assert stopped["stopped"]["failure_receipt_id"] and stopped["receipt_id"]
    assert conn.execute("SELECT count(*) FROM tourism_vintages").fetchone()[0] == before  # nothing removed
    assert monitor.store.provider_state(h.NS, "eurostat-tourism-occupancy")["stale"] is True
    assert monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)["notifications"] == []

    def limited(*, url, params, headers, timeout):
        return {"status": 429, "headers": {"Retry-After": "3600"}, "content": b""}

    waited = monitor.refresh(h.NS, h.source("occupancy"), principal_id="svc", scopes=h.SCOPES, transport=limited)
    assert waited["status"] == "stopped" and waited["retry_at"]
    waiting = monitor.refresh(h.NS, h.source("occupancy"), principal_id="svc", scopes=h.SCOPES,
                              transport=fixture_transport(h.pages("occupancy")))
    assert waiting["status"] == "rate_limited_wait" and waiting["releases"] == []
    for target in ({"concept": "occupancy_rate"}, {"weather": "x"}, {"frequency": "weekly"}):
        with pytest.raises(TourismError):
            monitor.create(h.NS, "bad", target=target, principal_id="analyst", scopes=h.SCOPES)
