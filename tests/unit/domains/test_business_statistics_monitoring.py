"""IB10 (#2738): business monitors through subscriptions; new, revised, redefined, removed and unchanged releases."""

from __future__ import annotations

import pytest

from src.ingestion.business_statistics_sources import FIXTURE_SECRET, fixture_transport
from src.kb.business_statistics_monitoring import BusinessMonitor
from src.kb.business_statistics_records import BusinessError
from tests.unit import business_statistics_harness as h


class Clock:
    def __init__(self, start: int = h.SECOND_RETRIEVAL) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value


def _monitor(conn):
    return BusinessMonitor(conn, now=Clock())


def test_new_revised_and_unchanged_releases_notify_once_with_citations():
    conn = h.connection()
    monitor = _monitor(conn)
    first = monitor.refresh(h.NS, h.source("bd"), principal_id="svc", scopes=h.SCOPES,
                            transport=fixture_transport(h.pages("bd")), retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert first["status"] == "complete" and first["new_releases"] == 1
    sub = monitor.create(h.NS, "bd-de", target={"area": {"scheme": "eurostat-geo", "code": "DE"},
                                                "provider": "eurostat-business-demography"},
                         principal_id="analyst", scopes=h.SCOPES)["subscription_id"]
    run = monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)
    assert {n["kind"] for n in run["notifications"]} == {"new_release"} and len(run["notifications"]) == 3
    assert all(n["citation"]["vintage_id"] == n["vintage_id"] for n in run["notifications"])
    # Unchanged: re-reading the same file adds nothing and a replay emits nothing.
    again = monitor.refresh(h.NS, h.source("bd"), principal_id="svc", scopes=h.SCOPES,
                            transport=fixture_transport(h.pages("bd")), retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert again["new_releases"] == 0 and again["unchanged_releases"] == 1
    assert monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)["notifications"] == []
    # Revised: provisional deaths restated and a new reference year.
    monitor.refresh(h.NS, h.revision_source("bd"), principal_id="svc", scopes=h.SCOPES,
                    transport=fixture_transport(h.pages("bd", revision=True)), retrieved_at_ms=h.SECOND_RETRIEVAL)
    revised = monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)
    assert {n["kind"] for n in revised["notifications"]} == {"new_period", "revised_value"}
    change = next(n for n in revised["notifications"] if n["kind"] == "revised_value")
    (row,) = change["what_changed"]["revised"]
    assert row["period"] == "2095" and row["before"]["flags"] == {"OBS_FLAG": "p"} and change["previous_vintage_id"]
    assert "assess" not in change["message"].casefold()


def test_a_rebase_notifies_removals_with_their_successors_and_the_new_series():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    monitor = _monitor(conn)
    sub = monitor.create(h.NS, "sts", target={"concept": "production_index",
                                              "classification": {"scheme": "NACE", "version": "Rev.2", "code": "C"}},
                         principal_id="analyst", scopes=h.SCOPES)["subscription_id"]
    monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)
    h.apply(conn, "rebase", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    notices = monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)["notifications"]
    kinds = sorted(n["kind"] for n in notices)
    assert kinds == ["new_release", "new_release", "removed_by_source", "removed_by_source"]
    removal = next(n for n in notices if n["kind"] == "removed_by_source")
    (successor,) = removal["what_changed"]["successors"]
    assert successor["relation"] == "base_year_change" and successor["other_series_id"]


def test_a_correction_and_a_definition_change_are_notified_for_a_place_watch():
    conn = h.connection()
    h.load_all(conn)
    monitor = _monitor(conn)
    sub = monitor.create(h.NS, "ca", target={"area": {"scheme": "us-fips-state", "code": "06"}},
                         principal_id="analyst", scopes=h.SCOPES)["subscription_id"]
    monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)
    h.apply(conn, "cbp", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    notices = monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)["notifications"]
    assert [n["kind"] for n in notices] == ["revised_value"]
    assert notices[0]["what_changed"]["revised"][0]["after"]["flags"] == {"EMP_N": "H"}
    # A changed definition the source states is a definition_change notice.
    item = h.source("cbp")
    item["business_statistics"]["documents"][1]["definition"] = {
        **item["business_statistics"]["documents"][1]["definition"], "source_text": "restated scope note"}
    item["business_statistics"]["documents"][1]["release"] = {"published_on": "2024-11-20", "label": "restated"}
    monitor.refresh(h.NS, item, principal_id="svc", scopes=h.SCOPES, transport=fixture_transport(h.pages("cbp")),
                    secret=FIXTURE_SECRET, retrieved_at_ms=h.SECOND_RETRIEVAL)
    changed = monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)["notifications"]
    assert changed and {n["kind"] for n in changed} == {"definition_change"}


def test_refresh_is_bounded_receipted_and_waits_after_a_rate_limit():
    conn = h.connection()
    monitor = _monitor(conn)
    bounded = monitor.refresh(h.NS, h.source("cbp"), principal_id="svc", scopes=h.SCOPES,
                              transport=fixture_transport(h.pages("cbp")), max_documents=1,
                              retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert bounded["status"] == "bounded" and len(bounded["releases"]) == 1

    def limited(*, url, params, headers, timeout):
        return {"status": 429, "headers": {"Retry-After": "3600"}, "content": b""}

    before = conn.execute("SELECT count(*) FROM business_vintages").fetchone()[0]
    stopped = monitor.refresh(h.NS, h.source("cbp"), principal_id="svc", scopes=h.SCOPES, transport=limited)
    assert stopped["status"] == "stopped" and stopped["stopped"]["code"] == "rate_limited" and stopped["retry_at"]
    assert conn.execute("SELECT count(*) FROM business_vintages").fetchone()[0] == before  # nothing removed
    assert monitor.store.provider_state(h.NS, "us-census-cbp")["stale"] is True
    waiting = monitor.refresh(h.NS, h.source("cbp"), principal_id="svc", scopes=h.SCOPES,
                              transport=fixture_transport(h.pages("cbp")))
    assert waiting["status"] == "rate_limited_wait" and waiting["releases"] == []
    with pytest.raises(BusinessError):
        monitor.create(h.NS, "bad", target={"concept": "gdp"}, principal_id="analyst", scopes=h.SCOPES)
    with pytest.raises(BusinessError):
        monitor.create(h.NS, "bad", target={"weather": "x"}, principal_id="analyst", scopes=h.SCOPES)
