"""HR11 (#2278): subscriptions notice new, revised and unchanged records with citations; idempotent runs."""

from __future__ import annotations

import pytest

from src.kb.humanitarian_identity import HumanitarianIdentity
from src.kb.humanitarian_monitoring import HumanitarianMonitor
from src.kb.humanitarian_records import HumanitarianError
from tests.unit.humanitarian.harness import NS, REVIEWER_SCOPES, SCOPES, World


@pytest.fixture()
def monitored():
    world = World()
    world.install()
    for source_id in ("reliefweb-disasters-sdn", "reliefweb-reports-sdn", "hdx-sdn-datasets", "ucdp-candidate-sdn"):
        world.run(source_id)
    world.boundaries()
    identity = HumanitarianIdentity(world.conn)
    identity.propose(NS, principal_id="alice", scopes=SCOPES)
    for subject in ("place-ref:iso3:SDN", "place-ref:name:admin1:khartoum"):
        for a in identity.assertions(NS, scopes=SCOPES, subject_key=subject, target_kind="geospatial-place"):
            identity.review(NS, a["assertion_id"], "accept", "reviewed", principal_id="bob", scopes=REVIEWER_SCOPES)
    monitor = HumanitarianMonitor(world.conn)
    country = monitor.create(NS, "sdn", pcode="SDN", watch=("reports", "datasets"), principal_id="alice", scopes=SCOPES)
    khartoum = monitor.create(NS, "sd01", pcode="SD01", watch=("events",), principal_id="alice", scopes=SCOPES,
                              events={"start": "2098-01-01", "end": "2098-12-31"})
    return world, monitor, country["subscription_id"], khartoum["subscription_id"]


def kinds(result):
    return sorted((n["kind"], n["record_key"]) for n in result["notifications"])


def test_new_revised_and_unchanged_reports_and_datasets(monitored):
    world, monitor, country, _ = monitored
    first = monitor.run(country, principal_id="alice", scopes=SCOPES)
    assert ("new_report", "reliefweb:situation_report:9900001") in kinds(first)
    assert ("new_dataset", "hdx:dataset:hdx-fixture-0001") in kinds(first)
    assert all(n["cites"]["revision_id"] and n["cites"]["source"] for n in first["notifications"])
    replay = monitor.run(country, principal_id="alice", scopes=SCOPES)
    assert replay["status"] == "replayed" and replay["notifications"] == []
    world.run("reliefweb-reports-sdn")  # unchanged re-acquisition: same generation, same watermark
    again = monitor.run(country, principal_id="alice", scopes=SCOPES)
    assert again["watermark"] == first["watermark"] and again["notifications"] == []
    world.run("reliefweb-reports-sdn", revised=True)
    world.run("hdx-sdn-datasets", revised=True)
    revised = monitor.run(country, principal_id="alice", scopes=SCOPES)
    assert revised["watermark"] > first["watermark"]
    assert kinds(revised) == [("dataset_revised", "hdx:dataset:hdx-fixture-0001"),
                              ("new_report", "reliefweb:situation_report:9900004"),
                              ("report_revised", "reliefweb:situation_report:9900001")]
    report = next(n for n in revised["notifications"] if n["kind"] == "report_revised")
    assert report["cites"]["changed_fields"] == ["as_of", "revision", "title"]
    assert report["cites"]["previous_revision_id"] and report["cites"]["revision_id"] != report["cites"]["previous_revision_id"]
    assert all("alert" not in n["kind"] and "warning" not in n["kind"] for n in revised["notifications"])


def test_event_releases_add_and_change_events_in_the_bounded_area(monitored):
    world, monitor, _, khartoum = monitored
    first = monitor.run(khartoum, principal_id="alice", scopes=SCOPES)
    assert kinds(first) == [("new_event", "ucdp:conflict_event:990001")]
    world.run("ucdp-ged-sdn")
    release = monitor.run(khartoum, principal_id="alice", scopes=SCOPES)
    assert kinds(release) == [("event_release_changed", "ucdp:conflict_event:990001"),
                              ("new_event", "ucdp:conflict_event:990010")]
    changed = next(n for n in release["notifications"] if n["kind"] == "event_release_changed")
    assert {"counts", "coding_status", "dataset_version"} <= set(changed["cites"]["changed_fields"])
    polled = monitor.poll(khartoum, principal_id="alice", scopes=SCOPES)
    assert len(polled["events"]) == 3


def test_monitors_reuse_subscriptions_and_enforce_bounds(monitored):
    world, monitor, country, _ = monitored
    with pytest.raises(HumanitarianError):
        monitor.create(NS, "bad", pcode="SDN", watch=("events",), principal_id="alice", scopes=SCOPES,
                       events={"start": "2098-01-01", "end": "2098-12-31"})  # a country is not a bounded area
    with pytest.raises(HumanitarianError):
        monitor.run(country, principal_id="mallory", scopes=SCOPES)
    row = world.conn.execute("SELECT domain FROM knowledge_subscriptions WHERE subscription_id=?", [country]).fetchone()
    assert row == ("humanitarian",)
