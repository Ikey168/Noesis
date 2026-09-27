"""Bid workspaces with cited checklists, document lists, milestones and drafts (P11)."""

import pytest

from src.kb.funding_workspaces import WorkspaceError
from src.kb.procurement_ranking import ShortlistService
from src.kb.procurement_workspaces import ProcurementBidDraftService, ProcurementWorkspaceStore
from src.kb.research_projects import ResearchProjectStore
from tests.unit.procurement.harness import NS, SCOPES, TED_N1, Env, supplier_profile


@pytest.fixture
def ready():
    env = Env()
    env.acquire()
    profile = supplier_profile(env)
    shortlist = ShortlistService(env.conn, now=env.now).build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
    store = ProcurementWorkspaceStore(env.conn, now=env.now)
    item_id = f"{env.key('ted', TED_N1)}#LOT-0001"
    workspace = store.create(NS, "bid-1", shortlist_id=shortlist["shortlist_id"], item_id=item_id, principal_id="alice", scopes=SCOPES)
    return env, shortlist, store, workspace


def test_checklist_items_cite_the_notice_passage_they_come_from(ready):
    env, _, _, workspace = ready
    assert workspace["contract"] == "noesis-procurement-workspace-v1" and "never submits" in workspace["notice"]
    kinds = {i["kind"] for i in workspace["items"]}
    assert kinds == {"requirement", "document", "milestone"}
    for item in workspace["items"]:
        source = item["source"]
        assert source["notice_revision"] == 1 and source["notice_id"] == "00612345-2026" and source["lot_id"] == "LOT-0001"
        assert source.get("locator") or source.get("url")
    requirement = next(i for i in workspace["items"] if i["item_id"] == "req:ted:LOT-0001:criterion:1")
    assert requirement["source"]["locator"]["field"] == "BT-750-Lot"
    assert not any("LOT-0002:criterion" in i["item_id"] for i in workspace["items"])


def test_required_documents_are_listed_with_source_and_status(ready):
    env, _, store, workspace = ready
    documents = store.inspect(NS, workspace["workspace_id"], principal_id="alice", scopes=SCOPES)["documents"]
    kinds = {d["document_kind"] for d in documents}
    assert {"declaration", "financial", "reference", "certificate", "procurement-documents"} <= kinds
    assert all(d["status"] == "todo" and d["source"]["notice_revision"] == 1 for d in documents)
    declaration = next(d for d in documents if d["document_kind"] == "declaration")
    assert len(declaration["source"]["requirement_ids"]) == 6


def test_milestones_keep_published_deadlines_and_label_derived_ones(ready):
    _, _, _, workspace = ready
    milestones = [i for i in workspace["items"] if i["kind"] == "milestone"]
    submission = next(m for m in milestones if m["item_id"] == "deadline:submission:LOT-0001")
    assert "2026-11-03+01:00 12:00:00+01:00" in submission["text"] and submission["due"] == "2026-11-03T12:00:00+01:00"
    derived = [m for m in milestones if m.get("derived")]
    assert derived and all(m["text"].startswith("Suggested internal milestone") for m in derived)


def test_workspace_is_a_research_project_pinned_to_notice_and_profile_revisions(ready):
    env, shortlist, _, workspace = ready
    project = ResearchProjectStore(env.conn, now=env.now).inspect(NS, workspace["project_id"], principal_id="alice", scopes=SCOPES)
    links = {link["kind"]: link for link in project["links"]}
    assert links["procurement_notice"]["revision"] == 1 and links["procurement_profile"]["revision"] == shortlist["profile_revision"]


def test_notice_revisions_mark_affected_items_stale_and_keep_preparation(ready):
    env, _, store, workspace = ready
    done = store.update_items(NS, workspace["workspace_id"], "u1", 1, {"doc:exclusion": {"status": "prepared"},
                                                                       "req:ted:LOT-0001:criterion:3": {"status": "in_progress"}},
                              principal_id="alice", scopes=SCOPES)
    env.acquire(2)
    inspected = store.inspect(NS, workspace["workspace_id"], principal_id="alice", scopes=SCOPES)
    assert not inspected["notice_revision_current"] and not inspected["freshness"]["current"]
    refreshed = store.refresh(NS, workspace["workspace_id"], "r1", done["revision"], principal_id="alice", scopes=SCOPES)
    items = {i["item_id"]: i for i in refreshed["items"]}
    assert items["req:ted:LOT-0001:criterion:3"]["stale"] and items["req:ted:LOT-0001:criterion:3"]["status"] == "in_progress"
    assert items["deadline:submission:LOT-0001"]["stale"] and "2026-11-17" in items["deadline:submission:LOT-0001"]["text"]
    assert items["doc:exclusion"]["status"] == "prepared" and not items["doc:exclusion"]["stale"]
    assert "gap:ted:LOT-0001:criterion:3" in items  # "or equivalent" now needs clarification
    assert refreshed["history"][-1]["change"]["notice_id"] == "00650001-2026"
    acknowledged = store.update_items(NS, workspace["workspace_id"], "u2", refreshed["revision"],
                                      {"req:ted:LOT-0001:criterion:3": {"acknowledge_change": True}}, principal_id="alice", scopes=SCOPES)
    assert not {i["item_id"]: i for i in acknowledged["items"]}["req:ted:LOT-0001:criterion:3"]["stale"]


def test_outcomes_never_submit_and_excluded_lots_cannot_start_a_workspace(ready):
    env, shortlist, store, workspace = ready
    with pytest.raises(WorkspaceError):
        store.record_outcome(NS, workspace["workspace_id"], "o1", 1, "prepared", principal_id="alice", scopes=SCOPES)
    with pytest.raises(WorkspaceError):
        store.record_outcome(NS, workspace["workspace_id"], "o2", 1, "buyer_confirmed", principal_id="alice", scopes=SCOPES)
    reported = store.record_outcome(NS, workspace["workspace_id"], "o3", 1, "user_reported_submitted", principal_id="alice", scopes=SCOPES)
    assert "outside Noesis" in reported["outcome"]["semantics"]
    with pytest.raises(WorkspaceError) as caught:
        store.create(NS, "bid-2", shortlist_id=shortlist["shortlist_id"], item_id=f"{env.key('ted', TED_N1)}#LOT-0002",
                     principal_id="alice", scopes=SCOPES)
    assert caught.value.code == "excluded_opportunity"
    with pytest.raises(WorkspaceError):
        store.inspect(NS, workspace["workspace_id"], principal_id="mallory", scopes=SCOPES)


def test_drafts_are_cited_authored_reports_pinned_to_the_notice_revision(ready):
    env, _, _, workspace = ready
    drafts = ProcurementBidDraftService(env.conn, now=env.now)
    draft = drafts.generate(NS, workspace["workspace_id"], "d1", principal_id="alice", scopes=SCOPES)
    assert draft["notice_revision"] == 1 and draft["lot_id"] == "LOT-0001" and draft["missing_documents"]
    exported = drafts.export(NS, draft["draft_id"], principal_id="alice", scopes=SCOPES)
    assert exported["export"]["contract"] == "noesis-report-export-v1" and exported["notice_revision_current"]
    env.acquire(2)
    assert drafts.export(NS, draft["draft_id"], principal_id="alice", scopes=SCOPES)["notice_revision_current"] is False
    sparse = supplier_profile(env, key="sparse", drop=("supplier.past_contracts", "supplier.size"))
    shortlist = ShortlistService(env.conn, now=env.now).build(NS, sparse["profile_id"], principal_id="alice", scopes=SCOPES)
    uk = next(i for i in shortlist["items"] if i["bucket"] != "excluded" and i["provider"] == "uk-fts")
    other = ProcurementWorkspaceStore(env.conn, now=env.now).create(NS, "bid-3", shortlist_id=shortlist["shortlist_id"],
                                                                    item_id=uk["item_id"], principal_id="alice", scopes=SCOPES)
    gaps = drafts.generate(NS, other["workspace_id"], "d2", principal_id="alice", scopes=SCOPES)
    assert {"supplier.past_contracts", "supplier.size"} <= set(gaps["unanswered"])
