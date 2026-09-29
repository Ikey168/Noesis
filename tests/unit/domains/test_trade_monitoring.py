"""Trade monitors: new releases, revisions and unchanged re-acquisitions through subscriptions (#2552)."""

from __future__ import annotations

import pytest

from src.ingestion.trade_sources import FIXTURE_SECRET, fixture_transport
from src.kb.trade_flows import TradeError
from src.kb.trade_monitoring import TradeMonitor
from tests.unit import trade_harness as h

CLOCK = {"now": h.FIRST_RETRIEVAL}


def monitor(conn):
    return TradeMonitor(conn, now=lambda: CLOCK["now"])


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def refresh(conn, name, *, revision=False, transport=None, at=None):
    CLOCK["now"] = at or CLOCK["now"]
    return monitor(conn).refresh(
        h.NS, h.source(name), principal_id="svc", scopes=h.SCOPES,
        transport=transport or fixture_transport(h.pages(name, revision)), secret=FIXTURE_SECRET,
    )


def test_new_revised_and_unchanged_releases_through_a_pair_monitor():
    conn = h.connection()
    first = refresh(conn, "comtrade", at=h.FIRST_RETRIEVAL)
    assert first["status"] == "complete" and first["new_releases"] == 2 and first["receipt_id"]
    watch = monitor(conn).create(h.NS, "de-cn-854143", flow_filter={"reporter": "276", "partner": "156",
                                                                     "products": ["854143", "854140"]},
                                 principal_id="alice", scopes=h.SCOPES)
    initial = monitor(conn).run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    # Both report directions: Germany's two series and China's four (two HS vintages) - all new releases.
    assert kinds(initial) == ["new_release"] * 6
    assert {n["series"]["role"] for n in initial["notifications"]} == {"reporter", "mirror"}
    # Unchanged: re-acquiring the same files adds nothing and a restart replays without events.
    again = refresh(conn, "comtrade", at=h.SECOND_RETRIEVAL)
    assert again["new_releases"] == 0 and again["unchanged_releases"] == 2
    assert monitor(conn).run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    replay = monitor(conn).run(watch["subscription_id"], initial["watermark"], principal_id="alice", scopes=h.SCOPES)
    assert replay["status"] in {"replayed", "ignored"} and replay["notifications"] == []
    # Revised: Germany's 2098 re-release changes one export value and repeats the import values.
    revised = refresh(conn, "comtrade", revision=True, at=h.SECOND_RETRIEVAL)
    assert revised["new_releases"] == 1 and revised["unchanged_releases"] == 1
    result = monitor(conn).run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(result) == ["new_release", "revision"]
    revision = next(n for n in result["notifications"] if n["kind"] == "revision")
    assert revision["changed_values"] == [
        {"period": "2098", "before": {"value": "5200000", "status": "reported"},
         "after": {"value": "5250000", "status": "reported"}}
    ]
    assert revision["previous_vintage_id"] and revision["source_revision"]["published_on"] == "2099-09-01"
    assert revision["series"]["flow"] == "export" and "not trade alerts" in result["note"]
    polled = monitor(conn).poll(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert len(polled["events"]) == 8


def test_refreshes_are_bounded_and_respect_the_provider_rate_limit():
    conn = h.connection()
    bounded = monitor(conn).refresh(h.NS, h.source("comtrade"), principal_id="svc", scopes=h.SCOPES,
                                    transport=fixture_transport(h.pages("comtrade")), secret=FIXTURE_SECRET,
                                    max_documents=1)
    assert bounded["status"] == "bounded" and len(bounded["releases"]) == 1
    calls = []

    def limited(*, url, params, headers, timeout):
        calls.append(url)
        return {"status": 429, "headers": {"Retry-After": "3600"}, "content": b"", "origin": "fixture"}

    stopped = refresh(conn, "comext", transport=limited, at=h.FIRST_RETRIEVAL)
    assert stopped["status"] == "stopped" and stopped["stopped"]["code"] == "rate_limited" and len(calls) == 1
    waiting = refresh(conn, "comext", transport=limited, at=h.FIRST_RETRIEVAL + 60_000)
    assert waiting["status"] == "rate_limited_wait" and len(calls) == 1  # nothing requested before Retry-After
    later = refresh(conn, "comext", at=h.FIRST_RETRIEVAL + 3_600_001)
    assert later["status"] == "complete" and later["new_releases"] == 2
    with pytest.raises(TradeError) as caught:
        monitor(conn).refresh(h.NS, h.source("comext"), principal_id="svc", scopes=h.READ_ONLY,
                              transport=fixture_transport(h.pages("comext")))
    assert caught.value.code == "unauthorized"


def test_monitor_filters_are_validated_and_unverified_live_releases_are_withheld():
    conn = h.connection()
    h.load_all(conn)
    with pytest.raises(TradeError):
        monitor(conn).create(h.NS, "bad", flow_filter={"reporter": "DE"}, principal_id="alice", scopes=h.SCOPES)
    conn.execute("UPDATE trade_releases SET evidence_origin='live' WHERE provider='eurostat-comext'")
    watch = monitor(conn).create(h.NS, "de-fr", flow_filter={"reporter": "DE", "partner": "FR"},
                                 principal_id="alice", scopes=h.SCOPES)
    result = monitor(conn).run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert result["notifications"] == [] and result["withheld_unverified_live_items"] == 8
