"""Labour releases and revisions monitored through subscriptions (#2483)."""

from __future__ import annotations

import copy

from src.ingestion.labour_sources import fixture_request, fixture_transport
from src.kb.labour_monitoring import LabourMonitor
from tests.unit import labour_harness as h


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def test_each_event_type_is_emitted_once_with_before_and_after_vintages():
    conn = h.connection()
    h.load_all(conn)
    monitor = LabourMonitor(conn, now=lambda: h.FIRST_RETRIEVAL + 1)
    ces = h.series_by_key(conn, "bls", "CES3000000001")
    watch_series = monitor.create(h.NS, "ces", target={"series_id": ces["series_id"]}, principal_id="alice",
                                  scopes=h.SCOPES)
    watch_place = monitor.create(h.NS, "germany", target={"place": {"scheme": "iso3166-1-alpha3", "code": "DEU"},
                                                         "concept": "unemployment_rate"},
                                 principal_id="alice", scopes=h.SCOPES)
    first = monitor.run(watch_series["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(first) == ["new_period"]
    assert kinds(monitor.run(watch_place["subscription_id"], principal_id="alice", scopes=h.SCOPES)) == [
        "new_period"] * 4  # ILO national and modelled, OECD SA and NSA

    # Re-publications: CES preliminary value revised, a new month and a benchmark change beyond the window.
    h.load_all(conn, revisions=True)
    later = LabourMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 1)
    ces_run = later.run(watch_series["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(ces_run) == ["benchmark_revision", "new_period"]
    benchmark = next(n for n in ces_run["notifications"] if n["kind"] == "benchmark_revision")
    assert benchmark["previous_vintage_id"] and benchmark["vintage_id"] != benchmark["previous_vintage_id"]
    revised = {r["period"]: r for r in benchmark["detail"]["revised"]}
    assert revised["2099-02"]["before"]["footnotes"] == [{"code": "P", "text": "preliminary"}]
    assert revised["2099-02"]["after"]["footnotes"] == [] and "2098-10" in benchmark["detail"]["basis"]
    place_run = later.run(watch_place["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(place_run) == ["revised_value"]
    assert place_run["notifications"][0]["series"]["native_key"].startswith("DEU.A.UNE_DEAP")

    # Unchanged data and restarts emit nothing.
    h.load_all(conn, revisions=True)
    assert LabourMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 2).run(
        watch_series["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []


def test_a_dataflow_version_change_is_a_definition_change_event():
    conn = h.connection()
    h.apply(conn, "oecd", retrieved_at_ms=h.FIRST_RETRIEVAL)
    series = h.series_by_key(conn, "oecd", "DEU.UNE_LF_M.PT_LF_SUB._Z.Y._T.Y_GE15._Z.M")
    monitor = LabourMonitor(conn, now=lambda: h.FIRST_RETRIEVAL + 1)
    watch = monitor.create(h.NS, "oecd-deu", target={"series_id": series["series_id"]}, principal_id="alice",
                           scopes=h.SCOPES)
    monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    item = copy.deepcopy(h.source("oecd"))
    document = item["labour_statistics"]["documents"][0]
    document["flow"] = document["flow"].replace(",1.0", ",1.1")
    body = h.pages("oecd")[0]["body"].replace("(1.0)", "(1.1)")
    pages = [{"request": fixture_request("oecd-sdmx-csv", document), "status": 200, "body": body}]
    result = LabourMonitor(conn, now=lambda: h.SECOND_RETRIEVAL).refresh(
        h.NS, item, principal_id="svc", scopes=h.SCOPES, transport=fixture_transport(pages))
    assert result["status"] == "complete" and result["new_releases"] == 1 and result["receipt_id"]
    run = LabourMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 1).run(
        watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(run) == ["definition_change"]
    assert run["notifications"][0]["detail"]["dataflow_version"] == {"before": "1.0", "after": "1.1"}


def test_refresh_is_bounded_idempotent_and_waits_after_a_rate_limit():
    conn = h.connection()
    monitor = LabourMonitor(conn, now=lambda: h.FIRST_RETRIEVAL)
    item = h.source("bls")
    first = monitor.refresh(h.NS, item, principal_id="svc", scopes=h.SCOPES,
                            transport=fixture_transport(h.pages("bls")), secret="k", max_documents=2)
    assert first["status"] == "bounded" and first["new_releases"] == 2
    again = monitor.refresh(h.NS, item, principal_id="svc", scopes=h.SCOPES,
                            transport=fixture_transport(h.pages("bls")), secret="k", max_documents=2)
    assert again["unchanged_releases"] == 2 and again["new_releases"] == 0
    limited = [dict(p, status=429, headers={"Retry-After": "3600"}) for p in h.pages("bls")]
    stopped = monitor.refresh(h.NS, item, principal_id="svc", scopes=h.SCOPES,
                              transport=fixture_transport(limited), secret="k")
    assert stopped["status"] == "stopped" and stopped["stopped"]["code"] == "rate_limited" and stopped["retry_at"]

    def refuse(**_):
        raise AssertionError("no request before Retry-After")

    waiting = monitor.refresh(h.NS, item, principal_id="svc", scopes=h.SCOPES, transport=refuse, secret="k")
    assert waiting["status"] == "rate_limited_wait"
