"""Authorisation, IUU listing and statistics-release changes through subscriptions (FI11, #2337)."""

from __future__ import annotations

import pytest

from src.ingestion.fisheries_sources import fixture_transport
from src.kb.fisheries_identity import FisheriesIdentity
from src.kb.fisheries_monitoring import FisheriesMonitor
from src.kb.fisheries_records import FisheriesError
from tests.unit.fisheries import harness as h

NS = h.NS


@pytest.fixture()
def env(tmp_path):
    item = h.loaded_env(tmp_path)
    FisheriesIdentity(item.conn, now=lambda: next(item.clock)).propose(NS, principal_id="matcher", scopes=h.WRITE)
    item.monitor = FisheriesMonitor(item.conn, now=lambda: next(item.clock))
    item.subscription = item.monitor.create(
        NS, "watch-1", principal_id="analyst", scopes=h.ALL, vessels=[h.IMO_REFLAGGED],
        lists=["iccat:iuu-vessels"], areas=["34"])["subscription_id"]
    yield item
    item.conn.close()


def run(env):
    return env.monitor.run(env.subscription, principal_id="analyst", scopes=h.ALL)


def test_the_monitor_is_a_knowledge_subscription_and_the_first_run_is_a_baseline(env):
    subscription = env.monitor.subscriptions.inspect(env.subscription, principal_id="analyst", scopes=h.ALL)
    assert subscription["domain"] == "fisheries" and subscription["query"]["kind"] == "fisheries-monitor"
    assert subscription["cadence"] == {"trigger": "watermark", "source_pack": "fisheries-maritime"}
    first = run(env)
    assert first["baseline"]
    assert {n["kind"] for n in first["notifications"]} >= {"authorisation_new", "iuu_listing", "iuu_delisting",
                                                           "statistics_release"}
    assert run(env)["notifications"] == []  # a replay delivers nothing
    with pytest.raises(FisheriesError):
        env.monitor.create(NS, "empty", principal_id="analyst", scopes=h.ALL)


def test_removal_new_listing_and_new_release_are_dated_cited_events(env):
    run(env)
    assert env.run("second", source_ids=["iccat-vessel-lists"], overrides=h.LATER["second"])["status"] == "complete"
    second = run(env)
    kinds = {n["kind"]: n for n in second["notifications"]}
    assert set(kinds) == {"authorisation_removed", "iuu_listing"}
    removed = kinds["authorisation_removed"]
    assert removed["new"]["date"] == "2026-11-01" and removed["prior"]["state"] == "authorised"
    assert removed["new"]["citation"]["snapshot_date"] == "2026-11-01"
    assert removed["prior"]["citation"]["snapshot_date"] == "2026-09-01"
    listing = kinds["iuu_listing"]
    assert listing["record_key"] == "iuu-listing:20260007" and listing["prior"]["state"] == "none on record"
    assert listing["new"]["date"] == "2026-11-10" and listing["new"]["citation"]["url"].startswith("https://")
    assert all("enforce" not in n["message"].lower() and "illegal" not in n["message"].lower()
               for n in second["notifications"])
    assert env.run("release-2", source_ids=["fao-fishstat-capture"], overrides=h.LATER["release"])["status"] \
        == "complete"
    third = run(env)
    assert {n["kind"] for n in third["notifications"]} == {"statistics_release"}
    changed = {n["record_key"]: n for n in third["notifications"]}
    gha = changed["capture:GHA:SKJ:2021:Q_tlw"]
    assert (gha["prior"]["citation"]["release"], gha["new"]["citation"]["release"]) == ("2025.1", "2026.1")
    assert (gha["prior"]["as_published"]["quantity"], gha["new"]["as_published"]["quantity"]) == ("50000", "51200")
    assert run(env)["notifications"] == []


def test_a_partial_run_is_never_evaluated(env):
    run(env)
    installed = env.runtime._manifest(env.value["pack_id"])[0]
    source = next(s for s in installed["sources"] if s["source_id"] == "iccat-vessel-lists")
    native = h.pages("iccat-vessel-lists")
    native[-1]["status"] = 503
    adapters = {"iccat-vessel-lists": env.runtime.factory.compile(source, transport=fixture_transport(native))}
    assert env.run("partial", source_ids=["iccat-vessel-lists"], adapters=adapters)["status"] != "complete"
    mark = env.conn.execute("SELECT max(watermark) FROM source_pack_watermarks WHERE pack_id=?",
                            ["fisheries-maritime"]).fetchone()[0]
    with pytest.raises(FisheriesError) as caught:
        env.monitor.run(env.subscription, mark, principal_id="analyst", scopes=h.ALL)
    assert caught.value.code == "incomplete_run"
    assert run(env)["status"] == "replayed"
    polled = env.monitor.poll(env.subscription, principal_id="analyst", scopes=h.ALL)
    assert polled["events"] and all(e["event_type"] == "added" for e in polled["events"])
