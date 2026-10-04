"""SS10 (#2794): social protection monitors through subscriptions; new, revised, removed and unchanged releases."""

from __future__ import annotations

import pytest

from src.ingestion.social_protection_sources import fixture_transport
from src.kb.social_protection_monitoring import SocialProtectionMonitor
from src.kb.social_protection_records import SocialProtectionError
from src.kb.social_protection_store import SocialProtectionStore
from tests.unit import social_protection_harness as h


class Clock:
    def __init__(self, start: int = h.SECOND_RETRIEVAL) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value


def _refresh(monitor, name, *, revised=False, transport=None):
    return monitor.refresh(h.NS, h.source(name, revised=revised), principal_id="svc", scopes=h.SCOPES,
                           transport=transport or fixture_transport(h.pages(name, revised=revised)),
                           retrieved_at_ms=h.SECOND_RETRIEVAL if revised else h.FIRST_RETRIEVAL)


def test_new_revised_removed_and_unchanged_releases_notify_once_with_citations():
    conn = h.connection()
    monitor = SocialProtectionMonitor(conn, now=Clock())
    first = _refresh(monitor, "esspros")
    assert first["status"] == "complete" and first["new_releases"] == 3 and first["receipt_id"]
    sub = monitor.create(h.NS, "fr", target={"area": {"scheme": "eurostat-geo", "code": "FR"}},
                         principal_id="analyst", scopes=h.SCOPES)["subscription_id"]
    run = monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)
    assert {n["kind"] for n in run["notifications"]} == {"new_release"} and len(run["notifications"]) == 5
    assert all(n["citation"]["vintage_id"] == n["vintage_id"] for n in run["notifications"])
    # Unchanged: re-reading the same files adds nothing and a replay emits nothing.
    again = _refresh(monitor, "esspros")
    assert again["new_releases"] == 0 and again["unchanged_releases"] == 3
    assert monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)["notifications"] == []
    # Revised, new periods, a new manual edition and a removal.
    _refresh(monitor, "esspros", revised=True)
    notices = monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)["notifications"]
    kinds = {n["kind"] for n in notices}
    assert {"new_period", "revised_value", "definition_change", "removed_by_source"} <= kinds
    confirmed = next(n for n in notices if n["kind"] == "revised_value"
                     and n["series"]["native_key"] == "A.TOTALNOREROUTE.PC_GDP.FR")
    (row,) = confirmed["what_changed"]["revised"]
    assert row["period"] == "2096" and row["before"]["flags"] == {"OBS_FLAG": "p"} and row["after"]["flags"] == {}
    assert confirmed["previous_vintage_id"] and confirmed["citation"]["vintage_id"] == confirmed["vintage_id"]
    removal = next(n for n in notices if n["kind"] == "removed_by_source")
    assert removal["series"]["native_key"] == "A.TOTAL.NR.FR" and "no longer states" in \
        removal["what_changed"]["statement"]
    assert all("assess" not in n["message"].casefold() for n in notices)


def test_a_restating_report_edition_and_an_estimate_replaced_are_notified_for_function_and_place_watches():
    conn = h.connection()
    h.load_all(conn)
    h.accept_places(conn, keys=("de",))
    place = h.register_places(conn, keys=("de",))["de"]
    monitor = SocialProtectionMonitor(conn, now=Clock())
    by_place = monitor.create(h.NS, "de", target={"place_id": place, "provider": "ilo-social-protection-coverage"},
                              principal_id="analyst", scopes=h.SCOPES)["subscription_id"]
    by_function = monitor.create(h.NS, "old", target={"function": {"scheme": "socx-branch", "code": "TP11"}},
                                 principal_id="analyst", scopes=h.SCOPES)["subscription_id"]
    monitor.run(by_place, principal_id="analyst", scopes=h.SCOPES)
    monitor.run(by_function, principal_id="analyst", scopes=h.SCOPES)
    h.apply(conn, "ilo", revised=True)
    h.apply(conn, "socx", revised=True)
    restated = monitor.run(by_place, principal_id="analyst", scopes=h.SCOPES)["notifications"]
    edition = [n for n in restated if n["kind"] == "edition_restatement"]
    assert edition and edition[0]["what_changed"]["restated_periods"] == ["2094"]
    assert {n["series"]["area"]["code"] for n in restated} == {"DEU"}
    socx = monitor.run(by_function, principal_id="analyst", scopes=h.SCOPES)["notifications"]
    revised = [n for n in socx if n["kind"] == "revised_value"]
    assert len(revised) == 2 and all(n["series"]["function"]["code"] == "TP11" for n in socx)
    replaced = next(r for r in revised[0]["what_changed"]["revised"] if r["period"] == "2097")
    assert replaced["before"]["publication_status"] == "estimated"


def test_a_failed_refresh_never_produces_a_removal_notice_and_a_rate_limit_waits():
    conn = h.connection()
    monitor = SocialProtectionMonitor(conn, now=Clock())
    _refresh(monitor, "socx")
    sub = monitor.create(h.NS, "socx", target={"provider": "oecd-socx"}, principal_id="analyst",
                         scopes=h.SCOPES)["subscription_id"]
    monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)
    store = SocialProtectionStore(conn)
    before = {s["series_id"]: s["current_vintage_id"] for s in store.find_series(h.NS)}
    broken = [dict(p, body="DATAFLOW,REF_AREA\nX,DEU\n") for p in h.pages("socx", revised=True)]
    failed = _refresh(monitor, "socx", revised=True, transport=fixture_transport(broken))
    assert failed["status"] == "stopped" and failed["stopped"]["code"] == "schema_drift"
    assert {s["series_id"]: s["current_vintage_id"] for s in store.find_series(h.NS)} == before
    assert monitor.run(sub, principal_id="analyst", scopes=h.SCOPES)["notifications"] == []
    assert store.provider_state(h.NS, "oecd-socx")["stale"] is True
    limited = [dict(p, status=429, headers={"Retry-After": "120"}) for p in h.pages("socx", revised=True)]
    stopped = _refresh(monitor, "socx", revised=True, transport=fixture_transport(limited))
    assert stopped["stopped"]["code"] == "rate_limited" and stopped["retry_at"]
    waited = _refresh(monitor, "socx", revised=True)
    assert waited["status"] == "rate_limited_wait" and waited["releases"] == []


def test_watch_targets_are_validated():
    conn = h.connection()
    h.load_all(conn)
    monitor = SocialProtectionMonitor(conn, now=Clock())
    for target in ({}, {"measure": "poverty"}, {"provider": "imf"}, {"cofog": "GF10"}, {"function": {"code": "OLD"}}):
        with pytest.raises(SocialProtectionError):
            monitor.create(h.NS, "bad", target=target, principal_id="analyst", scopes=h.SCOPES)
