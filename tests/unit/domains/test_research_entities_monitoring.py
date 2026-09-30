"""Research-entities monitors through subscriptions: new, revised and unchanged cases, bounded refresh (#2634)."""

from __future__ import annotations

import pytest

from src.ingestion.research_entities_sources import fixture_transport
from src.kb.research_entities_identity import ResearchEntitiesIdentity
from src.kb.research_entities_monitoring import ResearchEntitiesMonitor
from src.kb.research_entities_records import ResearchEntitiesError
from tests.unit import research_entities_harness as h


@pytest.fixture()
def env():
    conn = h.connection()
    h.load_all(conn)
    identity = ResearchEntitiesIdentity(conn)
    for match in identity.propose(h.NS, principal_id="analyst", scopes=h.SCOPES)["matches"]:
        identity.review(h.NS, match["match_id"], "accept", "same website", principal_id="rev", scopes=h.SCOPES)
    monitor = ResearchEntitiesMonitor(conn, now=lambda: h.SECOND_RETRIEVAL + 10)
    return conn, monitor


def run(monitor, subscription):
    return monitor.run(subscription, principal_id="alice", scopes=h.SCOPES)


def test_new_revised_and_unchanged_notices_cite_records(env):
    conn, monitor = env
    uni = monitor.create(h.NS, "uni", watch={"ror": h.A1}, principal_id="alice", scopes=h.SCOPES)["subscription_id"]
    ada = monitor.create(h.NS, "ada", watch={"orcid": h.R1}, principal_id="alice",
                         scopes=h.SCOPES)["subscription_id"]
    first = run(monitor, uni)
    assert sorted(n["kind"] for n in first["notifications"]) == ["new_dataset", "new_dataset", "new_project",
                                                                 "new_record"]
    new_record = next(n for n in first["notifications"] if n["kind"] == "new_record")
    assert new_record["citation"]["revision_marker"] == "v9.1-2099-01-15" and new_record["record_ids"]
    assert run(monitor, ada)["notifications"]
    assert run(monitor, uni)["notifications"] == []  # a replay emits nothing
    for name in ("ror", "orcid", "datacite", "cordis"):
        h.apply(conn, name, later=True, retrieved_at_ms=h.SECOND_RETRIEVAL)
    revised = run(monitor, uni)["notifications"]
    assert [n["kind"] for n in revised] == ["registry_change"]
    assert revised[0]["changes"][0]["field"] == "relationships"
    assert revised[0]["previous_revision_id"] and "v9.2-2099-03-15" in revised[0]["message"]
    works = run(monitor, ada)["notifications"]
    assert {n["kind"] for n in works} == {"registry_change", "new_asserted_work"}
    work = next(n for n in works if n["kind"] == "new_asserted_work")
    assert work["work"]["value"] == "10.9999/rent.paper3" and "not verified authorship" in work["assertion"]
    change = next(n for n in works if n["kind"] == "registry_change")
    assert all(set(c) == {"field", "changed"} for c in change["changes"])  # personal fields named only
    # Re-acquiring the same documents changes nothing and emits nothing.
    for name in ("ror", "orcid"):
        h.apply(conn, name, later=True, retrieved_at_ms=h.SECOND_RETRIEVAL + 1)
    assert run(monitor, uni)["notifications"] == [] and run(monitor, ada)["notifications"] == []


def test_researcher_monitors_need_the_researchers_scope(env):
    _, monitor = env
    with pytest.raises(ResearchEntitiesError):
        monitor.create(h.NS, "x", watch={"orcid": h.R1}, principal_id="bob",
                       scopes={"knowledge:research-entities:read", "knowledge:subscriptions:write",
                               "namespace:global:read"})
    with pytest.raises(ResearchEntitiesError):
        monitor.create(h.NS, "y", watch={"orcid": h.R1, "ror": h.A1}, principal_id="alice", scopes=h.SCOPES)


def test_refresh_is_bounded_idempotent_receipted_and_honours_retry_after(env):
    conn, monitor = env
    source = h.source("orcid", later=True)
    first = monitor.refresh(h.NS, source, principal_id="svc", scopes=h.SCOPES,
                            transport=fixture_transport(h.pages("orcid", later=True)), max_documents=1)
    assert first["status"] == "bounded" and first["new_releases"] == 1
    again = monitor.refresh(h.NS, source, principal_id="svc", scopes=h.SCOPES,
                            transport=fixture_transport(h.pages("orcid", later=True)))
    assert again["status"] == "complete" and again["unchanged_releases"] == 1 and again["new_releases"] == 1
    limited = [{**p, "status": 429, "headers": {"Retry-After": "120"}} for p in h.pages("orcid", later=True)]
    stopped = monitor.refresh(h.NS, source, principal_id="svc", scopes=h.SCOPES, transport=fixture_transport(limited))
    assert stopped["status"] == "stopped" and stopped["stopped"]["code"] == "rate_limited"
    waiting = monitor.refresh(h.NS, source, principal_id="svc", scopes=h.SCOPES,
                              transport=fixture_transport(h.pages("orcid", later=True)))
    assert waiting["status"] == "rate_limited_wait"
    receipts = conn.execute("SELECT count(*) FROM rentity_refresh_receipts").fetchone()[0]
    assert receipts == 4
