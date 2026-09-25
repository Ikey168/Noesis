"""Monitoring new calls, eligibility changes and deadlines (#1774)."""

import pytest

from src.kb.funding_monitoring import FundingMonitor, MonitorError
from src.kb.funding_profiles import FundingProfileStore
from src.kb.funding_ranking import ShortlistService
from src.kb.funding_workspaces import FundingWorkspaceStore
from tests.unit.funding.harness import NS, SCOPES, Env, founder_profile

PROVIDERS = ["nlnet", "eu-ft", "exist", "foerderdatenbank"]
EU_TOPIC = "https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/horizon-fixture-2026-01-01.json"


def _monitor(env):
    env.acquire_all()
    profile = founder_profile(env)
    monitor = FundingMonitor(env.conn, now=env.now)
    created = monitor.create(NS, profile["profile_id"], "m", providers=PROVIDERS, principal_id="alice", scopes=SCOPES)
    return profile, monitor, created


def test_first_run_reports_matching_calls_and_replay_is_idempotent():
    env = Env()
    _, monitor, created = _monitor(env)
    assert created["schedule"] == {"timezone": "Europe/Berlin", "local_time": "08:00"}
    first = monitor.run(created["subscription_id"], 1, principal_id="alice", scopes=SCOPES)
    kinds = [n["kind"] for n in first["notifications"]]
    assert kinds and set(kinds) == {"new_matching_call"}
    assert all(n["deliver_not_before"].endswith("+02:00") for n in first["notifications"])
    replay = monitor.run(created["subscription_id"], 1, principal_id="alice", scopes=SCOPES)
    assert replay["status"] == "replayed" and replay["notifications"] == []
    polled = monitor.poll(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    # Every opportunity is an event; only matching ones become notifications. Replay added none.
    assert {n["event_id"] for n in first["notifications"]} <= {e["event_id"] for e in polled["events"]}
    assert len({e["event_id"] for e in polled["events"]}) == len(polled["events"]) == 9


def test_rule_deadline_eligibility_and_stale_source_changes_are_notified():
    env = Env()
    profile, monitor, created = _monitor(env)
    shortlist = ShortlistService(env.conn, now=env.now).build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    eu = next(i for i in shortlist["items"] if "Open-source internet" in i["title"])
    workspace = FundingWorkspaceStore(env.conn, now=env.now).create(
        NS, "eu", shortlist_id=shortlist["shortlist_id"], opportunity_id=eu["opportunity_id"], principal_id="alice", scopes=SCOPES)
    monitor.run(created["subscription_id"], 1, principal_id="alice", scopes=SCOPES)
    env.web.routes[EU_TOPIC] = "eu_topic_open_amended.json"
    env.acquire("eu-ft", "eu_topic", "HORIZON-FIXTURE-2026-01-01", observation="amend")
    changed = monitor.run(created["subscription_id"], 2, principal_id="alice", scopes=SCOPES)
    assert {"deadline_shift"} <= {n["kind"] for n in changed["notifications"]}
    assert changed["workspaces_to_reassess"] == [workspace["workspace_id"]]
    # Profile-driven eligibility change: the applicant incorporates.
    FundingProfileStore(env.conn, now=env.now).update(NS, profile["profile_id"], "founded", profile["revision"], principal_id="alice",
                                                      scopes=SCOPES, set_facts={"applicant.incorporated": {"value": True}})
    eligibility = monitor.run(created["subscription_id"], 3, principal_id="alice", scopes=SCOPES)
    assert any(n["kind"] == "eligibility_changed" and "eligible → ineligible" in n["message"] for n in eligibility["notifications"])
    # A failed refresh degrades coverage and marks items uncertain, never closed.
    env.web.routes["https://nlnet.nl/propose/"] = "nlnet_propose_broken.html"
    env.acquire("nlnet", "nlnet_calls", observation="broken")
    stale = monitor.run(created["subscription_id"], 4, principal_id="alice", scopes=SCOPES)
    kinds = {n["kind"] for n in stale["notifications"]}
    assert "stale_source" in kinds and "closed" not in kinds
    assert stale["coverage"] == {"complete": False, "stale_providers": ["nlnet"]}


def test_closures_and_timezone_requirements():
    env = Env()
    profile, monitor, created = _monitor(env)
    monitor.run(created["subscription_id"], 1, principal_id="alice", scopes=SCOPES)
    env.clock += 70 * 86400 * 1000  # the NLnet round deadline passes
    later = monitor.run(created["subscription_id"], 2, principal_id="alice", scopes=SCOPES)
    assert any(n["kind"] == "closed" for n in later["notifications"])
    no_tz = founder_profile(env, key="no-tz", overrides={"preferences.timezone": {"value": "UTC"}})
    FundingProfileStore(env.conn).update(NS, no_tz["profile_id"], "clear", no_tz["revision"], principal_id="alice", scopes=SCOPES,
                                         clear_facts=["preferences.timezone"])
    with pytest.raises(MonitorError) as exc:
        monitor.create(NS, no_tz["profile_id"], "m2", providers=PROVIDERS, principal_id="alice", scopes=SCOPES)
    assert exc.value.code == "timezone_required"


def test_monitoring_stops_on_revocation_and_is_owner_scoped():
    env = Env()
    profile, monitor, created = _monitor(env)
    with pytest.raises(MonitorError):
        monitor.run(created["subscription_id"], 1, principal_id="mallory", scopes=SCOPES)
    FundingProfileStore(env.conn).withdraw(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    with pytest.raises(Exception):
        monitor.run(created["subscription_id"], 1, principal_id="alice", scopes=SCOPES)
