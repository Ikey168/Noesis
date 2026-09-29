"""Opportunity normalization, revisions, deadlines and invalidation (#1769)."""

import pytest

from src.kb.funding_opportunities import FundingOpportunityStore, OpportunityError, effective_status
from src.kb.funding_records import record
from tests.unit.funding.harness import NS, SCOPES, Env, ms

URL = "https://nlnet.nl/propose/"
AUTH = {"kind": "funder", "name": "NLnet"}


def _call(round_id="2026-10-01", deadlines=None, status="open", requirements=None):
    return record("nlnet", "call", "commonsfund", "Commons", source_url=URL, authority=AUTH, round_id=round_id,
                  status={"asserted": status}, requirements=requirements or [],
                  deadlines=deadlines if deadlines is not None else [
                      {"kind": "submission", "text": "October 1st 2026 12:00 CEST", "date": "2026-10-01",
                       "instant": "2026-10-01T12:00:00+02:00", "timezone": "Europe/Amsterdam"}])


def test_rounds_are_distinct_and_repeated_acquisition_deduplicates():
    env = Env()
    store = FundingOpportunityStore(env.conn, now=env.now)
    a = store.ingest(NS, "nlnet", [_call()], observation_id="o1", observed_at_ms=env.clock, scopes=SCOPES)
    b = store.ingest(NS, "nlnet", [_call()], observation_id="o2", observed_at_ms=env.clock + 1, scopes=SCOPES)
    c = store.ingest(NS, "nlnet", [_call(round_id="2026-12-01")], observation_id="o3", observed_at_ms=env.clock + 2, scopes=SCOPES)
    assert b["unchanged"] == a["created"] and c["created"] != a["created"]
    assert len(store.list(NS, scopes=SCOPES)) == 2


def test_status_is_conservative_and_timezone_unknowns_stay_explicit():
    call = _call()
    assert effective_status(call, as_of_ms=ms("2026-10-01T09:59:00+00:00"))["state"] == "open"
    closed = effective_status(call, as_of_ms=ms("2026-10-01T10:01:00+00:00"))
    assert closed["state"] == "closed" and "final submission deadline has passed" in closed["reasons"]
    dated = _call(deadlines=[{"kind": "submission", "text": "1 October 2026", "date": "2026-10-01"}])
    same_day = effective_status(dated, as_of_ms=ms("2026-10-01T20:00:00+00:00"))
    assert same_day["state"] == "open" and "no source timezone" in same_day["reasons"][0]
    unknown = _call(status="unknown", deadlines=[])
    assert effective_status(unknown, as_of_ms=ms("2026-09-01T00:00:00+00:00"))["state"] == "unknown"
    absent = effective_status(call, as_of_ms=ms("2026-09-01T00:00:00+00:00"), listing_state="absent_from_listing")
    assert absent["state"] == "unconfirmed"
    rolling = record("exist", "programme", "p", "EXIST", source_url="https://www.exist.de/x", authority={"kind": "funder", "name": "BMWE"},
                     status={"asserted": "rolling"})
    assert effective_status(rolling, as_of_ms=0)["state"] == "rolling"


def test_amendments_are_classified_and_invalidate_dependent_views():
    env = Env()
    store = FundingOpportunityStore(env.conn, now=env.now)
    [identity] = store.ingest(NS, "nlnet", [_call()], observation_id="o1", observed_at_ms=env.clock, scopes=SCOPES)["created"]
    store.register_view(NS, "view:a", "alice", {identity: 1})
    assert store.view_status("view:a")["current"]
    requirement = {"requirement_id": "lic", "category": "licensing", "text": "Must be open source", "hard": True,
                   "locator": {"quote": "Must be open source"}}
    changed = store.ingest(NS, "nlnet", [_call(requirements=[requirement])], observation_id="o2",
                           observed_at_ms=env.clock + 1, scopes=SCOPES)
    assert changed["invalidated_views"] == ["view:a"]
    status = store.view_status("view:a")
    assert not status["current"] and status["invalidations"][0]["reasons"] == ["rule_change"]
    store.register_view(NS, "view:a", "alice", {identity: 2})
    assert store.view_status("view:a")["current"]
    history = store.history(NS, identity, scopes=SCOPES)
    assert [h["revision"] for h in history] == [1, 2]


def test_missing_from_complete_listing_is_absence_not_closure_and_reappearance_restores():
    env = Env()
    store = FundingOpportunityStore(env.conn, now=env.now)
    other = record("nlnet", "call", "otherfund", "Other", source_url=URL, authority=AUTH, round_id="2026-10-01",
                   status={"asserted": "open"})
    store.ingest(NS, "nlnet", [_call(), other], observation_id="o1", observed_at_ms=env.clock, scopes=SCOPES,
                 coverage={"complete": True, "listing": "nlnet:propose"})
    result = store.ingest(NS, "nlnet", [_call()], observation_id="o2", observed_at_ms=env.clock + 1, scopes=SCOPES,
                          coverage={"complete": True, "listing": "nlnet:propose"})
    [gone] = result["absent"]
    assert store.get(NS, gone, scopes=SCOPES)["status"]["state"] == "unconfirmed"
    store.ingest(NS, "nlnet", [_call(), other], observation_id="o3", observed_at_ms=env.clock + 2, scopes=SCOPES,
                 coverage={"complete": True, "listing": "nlnet:propose"})
    assert store.get(NS, gone, scopes=SCOPES)["status"]["state"] == "open"
    partial = store.ingest(NS, "nlnet", [], observation_id="o4", observed_at_ms=env.clock + 3, scopes=SCOPES,
                           coverage={"complete": False, "listing": "nlnet:propose"})
    assert partial["absent"] == []


def test_directory_links_and_access_scopes():
    env = Env()
    store = FundingOpportunityStore(env.conn, now=env.now)
    with pytest.raises(OpportunityError):
        store.ingest(NS, "nlnet", [_call()], observation_id="o", observed_at_ms=0, scopes={"knowledge:funding:write"})
    with pytest.raises(OpportunityError):
        store.list(NS, scopes={"knowledge:funding:read"})
    with pytest.raises(OpportunityError):
        store.ingest(NS, "exist", [_call()], observation_id="o", observed_at_ms=0, scopes=SCOPES)
