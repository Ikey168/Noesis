"""Monitoring of new notices, corrigenda, deadline changes, cancellations and awards (P12)."""

import pytest

from src.kb.procurement_monitoring import MonitorError, ProcurementMonitor
from src.kb.procurement_ranking import ShortlistService
from src.kb.procurement_workspaces import ProcurementWorkspaceStore
from tests.unit.procurement.harness import BUYER, NS, SCOPES, TED_N1, Env, supplier_profile

PROVIDERS = ["ted", "uk-fts", "uk-cf", "sam-gov"]


@pytest.fixture
def watched():
    env = Env()
    env.acquire()
    profile = supplier_profile(env)
    monitor = ProcurementMonitor(env.conn, now=env.now)
    created = monitor.create(NS, profile["profile_id"], "m1", providers=PROVIDERS, principal_id="alice", scopes=SCOPES,
                             watch={"buyers": [BUYER], "cpv": ["72250000"]})
    return env, profile, monitor, created


def test_monitor_is_a_subscription_driven_by_source_pack_watermarks(watched):
    env, _, monitor, created = watched
    assert created["cadence"]["trigger"] == "watermark" and created["cadence"]["source_pack"] == "procurement"
    assert "no separate scheduler" in created["refresh"]
    first = monitor.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert first["watermark"] == 1
    kinds = [n["kind"] for n in first["notifications"]]
    assert kinds.count("award") >= 1 and "new_matching_notice" in kinds
    award = next(n for n in first["notifications"] if n["kind"] == "award")
    assert "does not open any procedure" in award["message"] and award["notice_id"]
    assert all(n["deliver_not_before"] and n["timezone"] == "Europe/Berlin" for n in first["notifications"])
    replay = monitor.run(created["subscription_id"], 1, principal_id="alice", scopes=SCOPES)
    assert replay["status"] == "replayed" and replay["notifications"] == []


def test_corrigenda_deadline_changes_cancellations_cite_the_changed_revision(watched):
    env, _, monitor, created = watched
    monitor.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    env.acquire(2)
    second = monitor.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    by_kind = {}
    for note in second["notifications"]:
        by_kind.setdefault(note["kind"], []).append(note)
    assert {"corrigendum", "deadline_change", "cancellation", "eligibility_changed", "requirements_changed"} <= set(by_kind)
    corrigendum = next(n for n in by_kind["corrigendum"] if n["notice_id"] == "00650001-2026")
    assert corrigendum["revision"] == 2 and corrigendum["procedure_key"] == env.key("ted", TED_N1)
    deadline = next(n for n in by_kind["deadline_change"] if n["notice_id"] == "00650001-2026")
    assert "2026-11-03+01:00 12:00:00+01:00" in deadline["message"] and "2026-11-17+01:00 12:00:00+01:00" in deadline["message"]
    assert by_kind["cancellation"][0]["notice_id"] == "00650002-2026"
    assert second["reassessed"] == [] or all(r["reassessed"] for r in second["reassessed"])


def test_changes_invalidate_rankings_and_checklists_until_recomputed(watched):
    env, profile, monitor, created = watched
    shortlist = ShortlistService(env.conn, now=env.now).build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    workspace = ProcurementWorkspaceStore(env.conn, now=env.now).create(
        NS, "w", shortlist_id=shortlist["shortlist_id"], item_id=f"{env.key('ted', TED_N1)}#LOT-0001", principal_id="alice", scopes=SCOPES)
    monitor.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    env.acquire(2)
    second = monitor.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    stale = {v["view_id"] for v in second["stale_views"]}
    assert shortlist["shortlist_id"] in stale and workspace["workspace_id"] in second["workspaces_to_refresh"]
    assert second["reassessed"] and all("notice revision" in " ".join(r["reasons"]) for r in second["reassessed"])


def test_failed_refresh_degrades_coverage_and_marks_uncertain_not_closed(watched):
    env, _, monitor, created = watched
    monitor.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    env.acquire(fail=["ted-notices"])
    result = monitor.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert result["coverage"] == {"complete": False, "stale_providers": ["ted"]}
    assert "stale_source" in {n["kind"] for n in result["notifications"]}
    assert not any(n["kind"] in {"cancellation", "no_longer_listed"} for n in result["notifications"])


def test_monitors_are_owner_scoped_and_need_a_timezone(watched):
    env, profile, monitor, created = watched
    with pytest.raises(MonitorError):
        monitor.run(created["subscription_id"], principal_id="mallory", scopes=SCOPES)
    with pytest.raises(MonitorError):
        monitor.create(NS, profile["profile_id"], "m2", providers=PROVIDERS, principal_id="alice", scopes=SCOPES, timezone="Mars/Olympus")
    polled = monitor.poll(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert "events" in polled
