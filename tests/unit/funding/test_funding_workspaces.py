"""Application workspaces, checklists and cited drafts (#1772, #1773)."""

import copy

import pytest

from src.kb.funding_profiles import FundingProfileError, FundingProfileStore
from src.kb.funding_ranking import ShortlistService
from src.kb.funding_workspaces import FundingDraftService, FundingWorkspaceStore, WorkspaceError
from src.kb.quantitative import QuantitativeStore
from src.kb.research_projects import ResearchProjectStore
from tests.unit.funding.harness import NS, SCOPES, Env, founder_profile

EU_TOPIC = "https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/horizon-fixture-2026-01-01.json"


def _setup(env, title):
    env.acquire_all()
    profile = founder_profile(env)
    shortlist = ShortlistService(env.conn, now=env.now).build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    item = next(i for i in shortlist["items"] if title in i["title"])
    return profile, shortlist, item


def test_workspace_links_existing_project_state_and_builds_checklist():
    env = Env()
    profile, shortlist, item = _setup(env, "EXIST Business Start-up")
    store = FundingWorkspaceStore(env.conn, now=env.now)
    workspace = store.create(NS, "exist", shortlist_id=shortlist["shortlist_id"], opportunity_id=item["opportunity_id"],
                             principal_id="alice", scopes=SCOPES)
    project = ResearchProjectStore(env.conn).inspect(NS, workspace["project_id"], principal_id="alice", scopes=SCOPES)
    assert {(link["kind"], link["revision"]) for link in project["links"]} == {
        ("funding_opportunity", 1), ("funding_profile", profile["revision"]), ("funding_shortlist", profile["revision"])}
    kinds = {i["kind"] for i in workspace["items"]}
    assert {"requirement", "document"} <= kinds
    assert "never submits" in workspace["notice"]
    assert store.create(NS, "exist", shortlist_id=shortlist["shortlist_id"], opportunity_id=item["opportunity_id"],
                        principal_id="alice", scopes=SCOPES)["idempotent"]
    excluded = next(i for i in shortlist["items"] if i["bucket"] == "excluded")
    with pytest.raises(WorkspaceError):
        store.create(NS, "closed", shortlist_id=shortlist["shortlist_id"], opportunity_id=excluded["opportunity_id"],
                     principal_id="alice", scopes=SCOPES)
    with pytest.raises(Exception):
        store.inspect(NS, workspace["workspace_id"], principal_id="mallory", scopes=SCOPES)


def test_gaps_deadlines_status_updates_and_outcome_semantics():
    env = Env()
    _, shortlist, item = _setup(env, "Open-source internet infrastructure")
    store = FundingWorkspaceStore(env.conn, now=env.now)
    workspace = store.create(NS, "eu", shortlist_id=shortlist["shortlist_id"], opportunity_id=item["opportunity_id"],
                             principal_id="alice", scopes=SCOPES)
    items = {i["item_id"]: i for i in workspace["items"]}
    assert any(i["kind"] == "gap" and i["text"].startswith("partner gap") for i in items.values())
    assert any(i["kind"] == "milestone" and i["due"] == "2026-11-18T16:00:00+00:00" for i in items.values())
    with pytest.raises(WorkspaceError):
        store.record_outcome(NS, workspace["workspace_id"], "early", 1, "prepared", principal_id="alice", scopes=SCOPES)
    updated = store.update_items(NS, workspace["workspace_id"], "all", 1,
                                 {i: {"status": "prepared"} for i in items}, principal_id="alice", scopes=SCOPES)
    prepared = store.record_outcome(NS, workspace["workspace_id"], "done", updated["revision"], "prepared",
                                    principal_id="alice", scopes=SCOPES)
    with pytest.raises(WorkspaceError) as exc:
        store.record_outcome(NS, workspace["workspace_id"], "conf", prepared["revision"], "provider_confirmed",
                             principal_id="alice", scopes=SCOPES)
    assert exc.value.code == "evidence_required"
    reported = store.record_outcome(NS, workspace["workspace_id"], "sub", prepared["revision"], "user_reported_submitted",
                                    principal_id="alice", scopes=SCOPES)
    assert reported["outcome"]["semantics"].startswith("applicant states")
    confirmed = store.record_outcome(NS, workspace["workspace_id"], "conf2", reported["revision"], "provider_confirmed",
                                     evidence={"reference": "portal receipt #fixture"}, principal_id="alice", scopes=SCOPES)
    assert confirmed["outcome"]["state"] == "provider_confirmed"


def test_call_revisions_keep_preparation_and_flag_stale_requirements():
    env = Env()
    _, shortlist, item = _setup(env, "Open-source internet infrastructure")
    store = FundingWorkspaceStore(env.conn, now=env.now)
    workspace = store.create(NS, "eu", shortlist_id=shortlist["shortlist_id"], opportunity_id=item["opportunity_id"],
                             principal_id="alice", scopes=SCOPES)
    deadline = next(i["item_id"] for i in workspace["items"] if i["kind"] == "milestone")
    requirement = next(i["item_id"] for i in workspace["items"] if i["kind"] == "requirement")
    store.update_items(NS, workspace["workspace_id"], "prep", 1, {deadline: {"status": "in_progress"}, requirement: {"status": "prepared"}},
                       principal_id="alice", scopes=SCOPES)
    env.web.routes[EU_TOPIC] = "eu_topic_open_amended.json"
    env.acquire("eu-ft", "eu_topic", "HORIZON-FIXTURE-2026-01-01", observation="amend")
    view = store.inspect(NS, workspace["workspace_id"], principal_id="alice", scopes=SCOPES)
    assert not view["freshness"]["current"] and not view["call_revision_current"]
    refreshed = store.refresh(NS, workspace["workspace_id"], "refresh", 2, principal_id="alice", scopes=SCOPES)
    items = {i["item_id"]: i for i in refreshed["items"]}
    assert items[deadline]["stale"] and items[deadline]["status"] == "in_progress"
    assert items[requirement]["status"] == "prepared" and not items[requirement]["stale"]
    assert refreshed["history"][-1]["change"]["changed"] == [deadline]
    view = store.inspect(NS, workspace["workspace_id"], principal_id="alice", scopes=SCOPES)
    assert view["freshness"]["current"] and view["stale_items"] == [deadline]
    project = ResearchProjectStore(env.conn).inspect(NS, refreshed["project_id"], principal_id="alice", scopes=SCOPES)
    assert {link["revision"] for link in project["links"] if link["kind"] == "funding_opportunity"} == {refreshed["pins"]["opportunity"]["revision"]}


def test_drafts_cite_facts_leave_gaps_unanswered_and_verify_budget_arithmetic():
    env = Env()
    _, shortlist, item = _setup(env, "EXIST Business Start-up")
    workspace = FundingWorkspaceStore(env.conn, now=env.now).create(
        NS, "exist", shortlist_id=shortlist["shortlist_id"], opportunity_id=item["opportunity_id"], principal_id="alice", scopes=SCOPES)
    service = FundingDraftService(env.conn, now=env.now)
    draft = service.generate(NS, workspace["workspace_id"], "d1", principal_id="alice", scopes=SCOPES)
    assert draft["unanswered"] == ["project.achievements"]
    assert draft["budget"]["total"] == "60000.00" and draft["budget"]["eligible_total"] == "58000.00"
    assert draft["budget"]["requested"] == "58000.00" and draft["budget"]["co_financing"] == "2000.00"
    assert any("Office rent" in issue for issue in draft["budget_issues"])
    replay = QuantitativeStore(env.conn).replay_calculation(NS, draft["budget"]["calculation_id"], scopes={"knowledge:quantitative:read"})
    assert replay["deterministic"]
    exported = service.export(NS, draft["draft_id"], principal_id="alice", scopes=SCOPES)
    content = exported["export"]["report"]["content"]
    sourced = [a for s in content["sections"] for a in s["assertions"] if a["kind"] == "sourced"]
    assert sourced and all(a["dependencies"] and a["citations"] for a in sourced)
    track = next(s for s in content["sections"] if s["id"] == "track-record")["assertions"][0]
    assert track["kind"] == "commentary" and track["text"].startswith("UNANSWERED")
    assert any("Not submitted" in limitation for limitation in content["limitations"])
    assert not exported["provenance"]["owner_edited"]


def test_owner_edits_are_preserved_and_exports_respect_current_access():
    env = Env()
    profile, shortlist, item = _setup(env, "EXIST Business Start-up")
    workspace = FundingWorkspaceStore(env.conn, now=env.now).create(
        NS, "exist", shortlist_id=shortlist["shortlist_id"], opportunity_id=item["opportunity_id"], principal_id="alice", scopes=SCOPES)
    service = FundingDraftService(env.conn, now=env.now)
    draft = service.generate(NS, workspace["workspace_id"], "d1", principal_id="alice", scopes=SCOPES)
    content = copy.deepcopy(service.export(NS, draft["draft_id"], principal_id="alice", scopes=SCOPES)["export"]["report"]["content"])
    content["sections"][3]["assertions"][0] = {"id": "track-record:own", "kind": "commentary", "dependencies": [], "citations": [],
                                               "text": "Owner-written: we ran two pilot deployments."}
    service.edit(NS, draft["draft_id"], 1, content, principal_id="alice", scopes=SCOPES)
    regenerated = service.generate(NS, workspace["workspace_id"], "d2", principal_id="alice", scopes=SCOPES)
    assert regenerated["report_id"] != draft["report_id"]
    edited = service.export(NS, draft["draft_id"], principal_id="alice", scopes=SCOPES)
    assert edited["provenance"]["owner_edited"] and "pilot deployments" in str(edited["export"])
    with pytest.raises(Exception):
        service.export(NS, draft["draft_id"], principal_id="alice", scopes=SCOPES - {"knowledge:reports:read", "knowledge:reports:write"})
    FundingProfileStore(env.conn).withdraw(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    with pytest.raises(FundingProfileError):
        service.export(NS, draft["draft_id"], principal_id="alice", scopes=SCOPES)
