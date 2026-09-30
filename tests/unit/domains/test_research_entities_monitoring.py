"""Research-entities monitors: new, revised and unchanged registry records through subscriptions (#2634)."""

from __future__ import annotations

import copy

import pytest

from src.ingestion.research_entities_sources import fixture_transport
from src.ingestion.source_packs import SourcePackError
from src.kb.research_entities_links import ResearchEntityLinks
from src.kb.research_entities_monitoring import ResearchEntityMonitor
from src.kb.research_entities_records import ResearchEntityError, ResearchEntityStore
from src.kb.subscriptions import SubscriptionError, SubscriptionStore
from tests.unit import research_entities_harness as h


def kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def second_acquisition(conn):
    h.load_second(conn)
    ResearchEntityLinks(conn, initialize=False).link(h.NS, principal_id="alice", scopes=h.SCOPES,
                                                     ownership_namespace=h.OWN_NS)


def test_organisation_monitor_reports_new_revised_and_unchanged_records_with_citations():
    conn = h.accepted_world()
    monitor = ResearchEntityMonitor(conn)
    watch = monitor.create(h.NS, "northwind", watch="organisation", key=h.NORTHWIND_POLY, principal_id="alice",
                           scopes=h.SCOPES)
    assert "no new scheduler" in watch["refresh"]
    with pytest.raises(ResearchEntityError):
        monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)  # no committed watermark
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    first = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(first) == ["new_dataset", "new_project_participation", "new_record"]
    second_acquisition(conn)
    SubscriptionStore(conn).commit_watermark(h.NS, 2)
    second = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(second) == ["dataset_revised", "record_revised", "status_changed", "status_changed",
                             "successor_published"]
    status = next(n for n in second["notifications"] if n["kind"] == "status_changed"
                  and n["record_key"] == "research-entities:ror:0zznwd303")
    assert (status["before"], status["after"]) == ("active", "withdrawn")
    assert status["cites"]["revision_id"] != status["cites"]["previous_revision_id"] is not None
    assert status["cites"]["source_as_of"] == "2099-06-01"
    successor = next(n for n in second["notifications"] if n["kind"] == "successor_published")
    assert successor["successor"] == h.NORTHWIND_TECH
    revised = next(n for n in second["notifications"] if n["kind"] == "record_revised")
    assert set(revised["changed_fields"]) >= {"status", "relationships", "release"}
    SubscriptionStore(conn).commit_watermark(h.NS, 3)
    assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    assert monitor.poll(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["events"]


def test_researcher_monitor_notifies_new_asserted_works_with_minimised_fields_only():
    conn = h.accepted_world()
    monitor = ResearchEntityMonitor(conn)
    with pytest.raises(ResearchEntityError):
        monitor.create(h.NS, "ada", watch="researcher", key=h.ADA, principal_id="bob", scopes=h.NO_RESEARCHERS)
    watch = monitor.create(h.NS, "ada", watch="researcher", key=h.ADA, principal_id="alice", scopes=h.SCOPES)
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    first = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(first) == ["new_asserted_work", "new_asserted_work", "new_record"]
    second_acquisition(conn)
    SubscriptionStore(conn).commit_watermark(h.NS, 2)
    second = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(second) == ["new_asserted_work", "record_revised"]
    work = next(n for n in second["notifications"] if n["kind"] == "new_asserted_work")
    assert work["work"]["dois"] == [h.PAPER2] and "not verified authorship" in work["message"]
    assert not [p for p in h.PERSONAL if p in str(second)]
    with pytest.raises((ResearchEntityError, SubscriptionError)):
        monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.NO_RESEARCHERS)


def test_project_monitor_reports_revised_contributions_and_datasets_no_longer_served():
    conn = h.accepted_world()
    monitor = ResearchEntityMonitor(conn)
    watch = monitor.create(h.NS, "northwave", watch="project", key=f"HORIZON:{h.NORTHWAVE}", principal_id="alice",
                           scopes=h.SCOPES)
    examplar = monitor.create(h.NS, "examplar", watch="project", key=h.EXAMPLAR, principal_id="alice",
                              scopes=h.SCOPES)
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    assert kinds(monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)) == [
        "new_dataset", "new_record"]
    monitor.run(examplar["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    second_acquisition(conn)
    SubscriptionStore(conn).commit_watermark(h.NS, 2)
    northwave = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(northwave) == ["dataset_revised", "status_changed"]  # ds.002 is no longer served
    gone = next(n for n in northwave["notifications"] if n["kind"] == "status_changed")
    assert (gone["before"], gone["after"], gone["record_key"]) == ("findable", "unavailable",
                                                                   f"research-entities:doi:{h.DS2}")
    changed = monitor.run(examplar["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert kinds(changed) == ["dataset_revised", "record_revised"]
    revised = next(n for n in changed["notifications"] if n["kind"] == "record_revised")
    assert revised["changed_fields"] == ["content_update_date", "participants"]
    assert ["999999903", "participant", "750000", "EUR"] in revised["after"]["participants"]


def test_refresh_is_bounded_idempotent_with_receipts_and_live_unverified_revisions_are_withheld():
    conn = h.connection()
    monitor = ResearchEntityMonitor(conn)
    item = h.source(h.DATACITE_SOURCE)
    first = monitor.refresh(item, run_id="refresh-1", principal_id="alice", scopes=h.SCOPES,
                            transport=fixture_transport(h.native_pages(h.DATACITE_SOURCE)))
    assert first["counts"]["new"] == 2 and first["complete"] and len(first["receipts"]) == 2
    again = monitor.refresh(item, run_id="refresh-2", principal_id="alice", scopes=h.SCOPES,
                            transport=fixture_transport(h.native_pages(h.DATACITE_SOURCE)))
    assert again["counts"] == {"new": 0, "revised": 0, "unchanged": 2, "older-observation": 0}
    bounded = copy.deepcopy(item)
    bounded["budgets"]["max_pages"] = 1
    with pytest.raises(SourcePackError):  # the declaration refuses more units than the page budget
        monitor.refresh(bounded, run_id="refresh-3", principal_id="alice", scopes=h.SCOPES,
                        transport=fixture_transport(h.native_pages(h.DATACITE_SOURCE)))
    watch = monitor.create(h.NS, "ds1-project", watch="project", key=h.EXAMPLAR, principal_id="alice",
                           scopes=h.SCOPES)
    store = ResearchEntityStore(conn)
    record = h.fetch(h.CORDIS_SOURCE)[0][0]
    store.project(h.NS, [{**record, "evidence_origin": "live"}], run_id="live", source_id=h.CORDIS_SOURCE)
    SubscriptionStore(conn).commit_watermark(h.NS, 1)
    result = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    assert result["notifications"] == [] and result["withheld_unverified_live_revisions"] == 1
