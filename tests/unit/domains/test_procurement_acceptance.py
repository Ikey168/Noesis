"""Offline Public Procurement acceptance: profile -> shortlist -> workspace -> monitoring (#1890, P14).

The real source-pack runtime, native TED/OCDS/SAM adapters, document store,
procurement projector, profile/eligibility/ranking/workspace machinery
(shared with Funding & Grants), research projects, authored reports and
subscriptions run against pinned *authored* provider fixtures
(tests/fixtures/procurement/README.md). No network connection is opened.
This is offline evidence only; it is never live provider coverage. One test
per acceptance row of #1848/#1890.
"""

from __future__ import annotations

import socket

import pytest

from src.kb.procurement_bundle import readiness
from src.kb.procurement_eligibility import ProcurementEligibilityService
from src.kb.procurement_identity import ProcurementIdentityService
from src.kb.procurement_monitoring import ProcurementMonitor
from src.kb.procurement_profiles import ProcurementProfileStore
from src.kb.procurement_ranking import ShortlistService
from src.kb.procurement_workspaces import ProcurementBidDraftService, ProcurementWorkspaceStore
from tests.unit.procurement.harness import (
    BUYER,
    NS,
    SCOPES,
    TED_CANTEEN,
    TED_N1,
    UK_TENDER,
    Env,
    supplier_profile,
)

PROVIDERS = ["ted", "uk-fts", "uk-cf", "sam-gov"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("offline acceptance must not open network connections")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def item(env, shortlist, provider, procedure_id, lot):
    return next(i for i in shortlist["items"] if i["item_id"] == f"{env.key(provider, procedure_id)}#{lot}")


def test_profile_to_shortlist_to_workspace_journey_with_monitoring_and_restart(tmp_path):
    path = str(tmp_path / "journey.duckdb")
    env = Env(path=path)
    # 1. Acquisition through the source-pack runtime from authored fixtures.
    receipt = env.acquire()
    assert receipt["status"] == "complete" and all(s["status"] == "complete" for s in receipt["sources"])
    # 2. A private synthetic supplier profile.
    profile = supplier_profile(env)
    # 3. Normalised procedures across four providers.
    notices = env.notices().list(NS, scopes=SCOPES, as_of_ms=env.now())
    assert {p["provider"] for p in notices} == set(PROVIDERS)
    # 4-5. Eligibility and ranking.
    shortlist = ShortlistService(env.conn, now=env.now).build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    target = item(env, shortlist, "ted", TED_N1, "LOT-0001")
    assert target["bucket"] == "apply_now" and target["verdict"] == "eligible"
    assert all(f["citation"]["notice_revision"] == 1 for f in ProcurementEligibilityService(env.conn, now=env.now).assess(
        NS, profile["profile_id"], target["procedure_key"], principal_id="alice", scopes=SCOPES)["lots"]["LOT-0001"]["findings"])
    # 6. Bid workspace and cited draft; nothing is submitted.
    workspace = ProcurementWorkspaceStore(env.conn, now=env.now).create(
        NS, "bid", shortlist_id=shortlist["shortlist_id"], item_id=target["item_id"], principal_id="alice", scopes=SCOPES)
    draft = ProcurementBidDraftService(env.conn, now=env.now).generate(NS, workspace["workspace_id"], "draft",
                                                                         principal_id="alice", scopes=SCOPES)
    assert draft["notice_revision"] == 1 and "never submits" in workspace["notice"]
    # 7. Monitoring over the subscription owner.
    monitor = ProcurementMonitor(env.conn, now=env.now)
    created = monitor.create(NS, profile["profile_id"], "watch", providers=PROVIDERS, principal_id="alice", scopes=SCOPES,
                             watch={"buyers": [BUYER]})
    assert monitor.run(created["subscription_id"], principal_id="alice", scopes=SCOPES)["notifications"]
    # 8. Restart: durable state survives, the replay is identical and a new observation is detected.
    env.conn.close()
    reopened = Env(path=path, restart_of=env)
    service = ShortlistService(reopened.conn, now=reopened.now)
    assert service.replay(NS, shortlist["shortlist_id"], principal_id="alice", scopes=SCOPES)["identical"]
    reopened.acquire(2)
    second = ProcurementMonitor(reopened.conn, now=reopened.now).run(created["subscription_id"], principal_id="alice", scopes=SCOPES)
    assert {"corrigendum", "deadline_change", "cancellation"} <= {n["kind"] for n in second["notifications"]}
    assert workspace["workspace_id"] in second["workspaces_to_refresh"]


def test_corrigendum_that_moves_a_deadline_invalidates_recommendations_and_checklist_items():
    env = Env()
    env.acquire()
    profile = supplier_profile(env)
    service = ShortlistService(env.conn, now=env.now)
    shortlist = service.build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    store = ProcurementWorkspaceStore(env.conn, now=env.now)
    workspace = store.create(NS, "bid", shortlist_id=shortlist["shortlist_id"], item_id=f"{env.key('ted', TED_N1)}#LOT-0001",
                             principal_id="alice", scopes=SCOPES)
    env.acquire(2)
    assert not service.inspect(NS, shortlist["shortlist_id"], principal_id="alice", scopes=SCOPES)["freshness"]["current"]
    fresh = service.build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    moved = item(env, fresh, "ted", TED_N1, "LOT-0001")
    assert moved["next_deadline"]["text"] == "2026-11-17+01:00 12:00:00+01:00" and moved["notice"]["stage"] == "corrigendum"
    assert moved["verdict"] == "needs_clarification" and moved["bucket"] == "consider"  # "or equivalent" is not auto-passed
    uk = item(env, fresh, "uk-fts", UK_TENDER, "1")
    assert uk["next_deadline"]["text"] == "2026-11-24T12:00:00Z"
    refreshed = store.refresh(NS, workspace["workspace_id"], "r", 1, principal_id="alice", scopes=SCOPES)
    stale = {i["item_id"] for i in refreshed["items"] if i["stale"]}
    assert {"deadline:submission:LOT-0001", "req:ted:LOT-0001:criterion:3"} <= stale
    assert "req:ted:LOT-0001:criterion:1" not in stale


def test_cancelled_procedure_is_excluded_and_its_workspace_cannot_start():
    env = Env()
    env.acquire()
    env.acquire(2)
    profile = supplier_profile(env)
    shortlist = ShortlistService(env.conn, now=env.now).build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    canteen = item(env, shortlist, "ted", TED_CANTEEN, "LOT-0001")
    assert canteen["state"] == "cancelled" and canteen["bucket"] == "excluded"
    assert any("cancellation notice 00650002-2026" in r for r in canteen["state_reasons"])
    with pytest.raises(Exception) as caught:
        ProcurementWorkspaceStore(env.conn, now=env.now).create(NS, "no", shortlist_id=shortlist["shortlist_id"],
                                                                 item_id=canteen["item_id"], principal_id="alice", scopes=SCOPES)
    assert getattr(caught.value, "code", "") == "excluded_opportunity"


def test_lots_with_different_criteria_get_separate_verdicts_fit_and_checklists():
    env = Env()
    env.acquire()
    profile = supplier_profile(env)
    shortlist = ShortlistService(env.conn, now=env.now).build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    lot1, lot2 = item(env, shortlist, "ted", TED_N1, "LOT-0001"), item(env, shortlist, "ted", TED_N1, "LOT-0002")
    assert (lot1["verdict"], lot2["verdict"]) == ("eligible", "ineligible")
    assert lot2["disqualifiers"] == ["ted:LOT-0002:criterion:1", "ted:LOT-0002:criterion:2"]
    assert lot1["estimated_value"]["amount"] == "400000" and lot2["estimated_value"]["amount"] == "900000"
    uk1, uk2 = item(env, shortlist, "uk-fts", UK_TENDER, "1"), item(env, shortlist, "uk-fts", UK_TENDER, "2")
    assert uk1["verdict"] == "needs_clarification" and uk2["verdict"] == "ineligible"  # GBP turnover unknown; certificate missing


def test_unknown_profile_facts_remain_unknown_and_are_named():
    env = Env()
    env.acquire()
    profile = supplier_profile(env, drop=("exclusion.insolvency", "supplier.annual_turnover", "supplier.jurisdictions",
                                          "preferences.max_contract_value", "preferences.min_contract_value"))
    shortlist = ShortlistService(env.conn, now=env.now).build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    lot1 = item(env, shortlist, "ted", TED_N1, "LOT-0001")
    assert lot1["verdict"] == "needs_clarification" and lot1["bucket"] != "apply_now"
    assert {"exclusion.insolvency", "supplier.annual_turnover.EUR"} <= set(lot1["missing_facts"])
    assert {"jurisdiction", "value_fit"} <= set(lot1["unknown_criteria"])
    assert {"supplier.annual_turnover", "supplier.jurisdictions"} <= set(shortlist["unknown_profile_facts"])


def test_award_history_is_context_and_never_implies_that_a_procedure_is_open():
    env = Env()
    env.acquire()
    profile = supplier_profile(env)
    shortlist = ShortlistService(env.conn, now=env.now).build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    awards = ProcurementIdentityService(env.conn, now=env.now).award_history(NS, scopes=SCOPES)
    award_procedures = {a["procedure_key"] for a in awards}
    assert len(awards) == 5 and not award_procedures & {i["procedure_key"] for i in shortlist["items"]}
    assert all(a["semantics"] == "award history: context only, never evidence that a procedure is open" for a in awards)
    for procedure in env.notices().list(NS, scopes=SCOPES, as_of_ms=env.now()):
        assert procedure["procedure_key"] not in award_procedures
    lot1 = item(env, shortlist, "ted", TED_N1, "LOT-0001")
    assert lot1["award_context"] and lot1["state"] == "open"  # open because of its own deadline, not the award
    assert all(a["date"] < "2026" for a in lot1["award_context"] if a["relation"] != "same CPV branch")


def test_private_profiles_stay_owner_scoped_across_every_view():
    env = Env()
    env.acquire()
    profile = supplier_profile(env)
    shortlist = ShortlistService(env.conn, now=env.now).build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    workspace = ProcurementWorkspaceStore(env.conn, now=env.now).create(
        NS, "bid", shortlist_id=shortlist["shortlist_id"], item_id=f"{env.key('ted', TED_N1)}#LOT-0001", principal_id="alice", scopes=SCOPES)
    operator = SCOPES | {"operator"}
    for principal, scopes in (("mallory", SCOPES), ("root", operator)):
        for call in (lambda: ProcurementProfileStore(env.conn).inspect(NS, profile["profile_id"], principal_id=principal, scopes=scopes),
                     lambda: ShortlistService(env.conn).inspect(NS, shortlist["shortlist_id"], principal_id=principal, scopes=scopes),
                     lambda: ProcurementWorkspaceStore(env.conn).inspect(NS, workspace["workspace_id"], principal_id=principal, scopes=scopes),
                     lambda: ProcurementEligibilityService(env.conn).assess(NS, profile["profile_id"], env.key("ted", TED_N1),
                                                                             principal_id=principal, scopes=scopes)):
            with pytest.raises(Exception):
                call()
    records = env.conn.execute("SELECT record_json FROM procurement_notice_assertions").fetchall()
    assert not any("Nordlicht" in r[0] and "exclusion." in r[0] for r in records)  # no profile data in source records
    ProcurementProfileStore(env.conn).withdraw(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    with pytest.raises(Exception):
        ShortlistService(env.conn).inspect(NS, shortlist["shortlist_id"], principal_id="alice", scopes=SCOPES)


def test_offline_and_live_evidence_are_reported_separately():
    env = Env()
    receipt = env.acquire()
    executions = {s["adapter"].get("procurement", {}).get("provider"): s for s in receipt["sources"]}
    assert executions
    states = {p: env.notices().provider_state(NS, p)["last_execution"] for p in PROVIDERS}
    assert set(states.values()) == {"injected"}
    status = readiness(env.conn, NS, scopes=SCOPES)
    assert {status["providers"][p]["status"] for p in PROVIDERS} == {"fixture-only"}
    assert status["evidence"]["live"]["result"] == "blocked" and "never live coverage" in status["evidence"]["offline"]
    assert {status["providers"][p]["live_verification"]["status"] for p in PROVIDERS} == {"unverified-live"}
