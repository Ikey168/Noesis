"""IP10 (#2633): income monitors through subscriptions; new, revised and unchanged releases; bounded refreshes."""

from __future__ import annotations

import pytest

from src.ingestion.income_distribution_sources import fixture_transport
from src.kb.income_distribution_monitoring import IncomeMonitor
from src.kb.income_distribution_records import IncomeError
from tests.unit import income_distribution_harness as h


class Clock:
    def __init__(self, start: int = h.FIRST_RETRIEVAL) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value


def _monitor(conn):
    return IncomeMonitor(conn, now=Clock())


def test_new_revised_and_unchanged_releases_notify_once_with_citations():
    conn = h.connection()
    monitor = _monitor(conn)
    first = monitor.refresh(h.NS, h.source("silc"), principal_id="svc", scopes=h.SCOPES,
                            transport=fixture_transport(h.pages("silc")), retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert first["status"] == "complete" and first["new_releases"] == 3
    created = monitor.create(h.NS, "silc-de", target={"area": {"scheme": "eurostat-geo", "code": "DE"}},
                             principal_id="analyst", scopes=h.SCOPES)
    sub = created["subscription_id"]
    run = monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)
    assert {n["kind"] for n in run["notifications"]} == {"new_release"}
    assert all(n["citation"]["vintage_id"] == n["vintage_id"] for n in run["notifications"])
    # Unchanged: re-reading the same files adds nothing and a replay emits nothing.
    again = monitor.refresh(h.NS, h.source("silc"), principal_id="svc", scopes=h.SCOPES,
                            transport=fixture_transport(h.pages("silc")), retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert again["new_releases"] == 0 and again["unchanged_releases"] == 3
    assert monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)["notifications"] == []
    # Revised: a later release with a revised value and a new reference year.
    monitor.refresh(h.NS, h.source("silc", revision=True), principal_id="svc", scopes=h.SCOPES,
                    transport=fixture_transport(h.pages("silc", revision=True)), retrieved_at_ms=h.SECOND_RETRIEVAL)
    revised = monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)
    kinds = {n["kind"] for n in revised["notifications"]}
    assert kinds == {"new_period", "revised_value"}
    change = next(n for n in revised["notifications"] if n["kind"] == "revised_value")
    assert change["what_changed"]["revised"][0]["period"] == "2096" and change["previous_vintage_id"]
    assert "assess" not in change["message"].casefold()


def test_ppp_revisions_and_removals_are_notified_for_a_concept_watch():
    conn = h.connection()
    h.load_all(conn)
    monitor = _monitor(conn)
    head = monitor.create(h.NS, "headcount", target={"concept": "poverty_headcount"}, principal_id="analyst",
                          scopes=h.SCOPES)["subscription_id"]
    monitor.run(head, principal_id="analyst", scopes=h.SCOPES)
    h.apply(conn, "pip", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    h.apply(conn, "oecd", revision=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    kinds = {(n["series"]["provider"], n["kind"]) for n in monitor.run(head, principal_id="analyst",
                                                                        scopes=h.SCOPES)["notifications"]}
    assert ("pip", "ppp_revision") in kinds and ("oecd-idd", "removed_by_source") in kinds


def test_refresh_is_bounded_receipted_and_waits_after_a_rate_limit():
    conn = h.connection()
    monitor = _monitor(conn)
    bounded = monitor.refresh(h.NS, h.source("silc"), principal_id="svc", scopes=h.SCOPES,
                              transport=fixture_transport(h.pages("silc")), max_documents=1,
                              retrieved_at_ms=h.FIRST_RETRIEVAL)
    assert bounded["status"] == "bounded" and len(bounded["releases"]) == 1

    def limited(*, url, params, headers, timeout):
        return {"status": 429, "headers": {"Retry-After": "3600"}, "content": b""}

    stopped = monitor.refresh(h.NS, h.source("oecd"), principal_id="svc", scopes=h.SCOPES, transport=limited)
    assert stopped["status"] == "stopped" and stopped["stopped"]["code"] == "rate_limited" and stopped["retry_at"]
    waiting = monitor.refresh(h.NS, h.source("oecd"), principal_id="svc", scopes=h.SCOPES,
                              transport=fixture_transport(h.pages("oecd")))
    assert waiting["status"] == "rate_limited_wait" and waiting["releases"] == []
    with pytest.raises(IncomeError):
        monitor.create(h.NS, "bad", target={"concept": "happiness"}, principal_id="analyst", scopes=h.SCOPES)
