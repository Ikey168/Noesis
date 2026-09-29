"""Bid workspaces: cited requirement checklists, document lists, milestones and bid drafts.

A bid workspace reuses the Funding & Grants application-workspace machinery
(:class:`src.kb.funding_workspaces.FundingWorkspaceStore`): an existing
research project (``ResearchProjectStore``) holds the pinned links, and the
revisioned, idempotent checklist commands (``update_items``) and the stale
flagging across revisions are the same code. What differs is where items
come from: one notice *lot* revision and the supplier profile.

Every checklist item cites the notice passage it comes from (notice id,
procedure revision, lot and locator/quote). Required documents
(declarations, certificates, references, financial evidence, forms and the
buyer's procurement documents) are listed with their source and status.
Deadlines keep their published text and offset; internal milestones derived
from them are labelled as suggestions. Drafts are authored reports pinned to
the notice revision; anything not backed by an owner-reviewed profile fact or
cited notice text is an explicit *unanswered* item.

A later notice revision (corrigendum, cancellation) marks affected checklist
items stale on refresh while keeping preparation status. Nothing here submits
a bid or contacts a buyer or portal.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

from src.kb.funding_records import canonical, digest
from src.kb.funding_workspaces import FundingWorkspaceStore, WorkspaceError, _gap_items
from src.kb.procurement_notices import lot_view
from src.kb.procurement_records import READ_SCOPE, WRITE_SCOPE

CONTRACT = "noesis-procurement-workspace-v1"
ITEM_STATUSES = ("todo", "in_progress", "prepared", "not_applicable")
OUTCOMES = ("preparing", "prepared", "user_reported_submitted", "buyer_confirmed", "abandoned")
NEVER = "Preparation only. Noesis never submits bids and never contacts buyers or procurement portals."
_DOCUMENTS = {
    "exclusion": ("declaration", "Self-declaration on exclusion grounds (e.g. ESPD Part III)"),
    "economic-financial": ("financial", "Evidence of economic and financial standing"),
    "technical-professional": ("reference", "Evidence of technical and professional ability (references, staff CVs)"),
    "certification": ("certificate", "Copy of the required certificate"),
    "suitability": ("registration", "Evidence of suitability (register entry or registration)"),
    "set-aside": ("representation", "Set-aside eligibility representations"),
}
_DDL = """
CREATE TABLE IF NOT EXISTS procurement_workspaces(
 workspace_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 project_id TEXT NOT NULL, request_hash TEXT NOT NULL, revision BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS procurement_workspace_revisions(
 workspace_id TEXT NOT NULL, revision BIGINT NOT NULL, content_json TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL, PRIMARY KEY(workspace_id, revision));
CREATE TABLE IF NOT EXISTS procurement_workspace_commands(
 workspace_id TEXT NOT NULL, command_key TEXT NOT NULL, request_hash TEXT NOT NULL,
 result_revision BIGINT NOT NULL, PRIMARY KEY(workspace_id, command_key));
CREATE TABLE IF NOT EXISTS procurement_bid_drafts(
 draft_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL, workspace_id TEXT NOT NULL,
 workspace_revision BIGINT NOT NULL, report_id TEXT NOT NULL, generated_revision BIGINT NOT NULL,
 content_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
"""


def _source(procedure, lot_id, **extra):
    return {"procedure_key": procedure["procedure_key"], "notice_revision": procedure["revision"],
            "notice_id": procedure["cause"]["notice_id"], "source_url": procedure["record"]["source_url"], "lot_id": lot_id, **extra}


def checklist(procedure, lot_id, assessment_lot, *, as_of_iso=None):
    """Requirement, document, milestone and gap items for one lot revision; each cites its source."""
    record = procedure["record"]
    view = lot_view(record, lot_id)
    items = []
    for requirement in view["requirements"]:
        items.append({"item_id": "req:" + requirement["requirement_id"], "kind": "requirement", "category": requirement["category"],
                      "text": requirement["text"], "language": requirement.get("language"),
                      "source": _source(procedure, lot_id, locator=requirement["locator"], text_hash=digest(requirement["text"]))})
    documents = {}
    for requirement in view["requirements"]:
        if requirement["category"] not in _DOCUMENTS:
            continue
        kind, title = _DOCUMENTS[requirement["category"]]
        key = "exclusion" if requirement["category"] == "exclusion" else requirement["requirement_id"]
        entry = documents.setdefault(key, {"item_id": "doc:" + key, "kind": "document", "document_kind": kind,
                                           "text": title if requirement["category"] == "exclusion" else f"{title}: {requirement['text'][:200]}",
                                           "requirements": [], "source": None})
        entry["requirements"].append(requirement["requirement_id"])
        entry["source"] = _source(procedure, lot_id, requirement_ids=sorted(entry["requirements"]),
                                  locator=requirement["locator"],
                                  text_hash=digest(sorted(r["text"] for r in view["requirements"] if r["requirement_id"] in entry["requirements"])))
    items.extend(documents.values())
    for document in record.get("documents") or []:
        items.append({"item_id": "doc:notice:" + digest(document["url"])[:16], "kind": "document", "document_kind": "procurement-documents",
                      "text": f"Obtain and review: {document.get('title') or document['kind']} ({document['url']})",
                      "source": _source(procedure, lot_id, url=document["url"], text_hash=digest(document))})
    for deadline in view["deadlines"]:
        key = f"{deadline['kind']}:{deadline.get('lot_id') or '_'}"
        items.append({"item_id": "deadline:" + key, "kind": "milestone", "deadline_kind": deadline["kind"],
                      "text": f"{deadline['kind']} deadline as published: {deadline['text']}" + ("" if deadline.get("instant") else " (no offset stated; exact cutoff unknown)"),
                      "due": deadline.get("instant") or deadline.get("date"), "timezone": deadline.get("timezone"),
                      "source": _source(procedure, lot_id, locator=deadline.get("locator"), text_hash=digest(deadline))})
        if deadline["kind"] == "submission" and deadline.get("instant"):
            due = datetime.fromisoformat(deadline["instant"])
            for label, days in (("go/no-go decision", 21), ("final internal review", 3)):
                items.append({"item_id": f"plan:{label.replace(' ', '-').replace('/', '-')}:{deadline.get('lot_id') or '_'}", "kind": "milestone",
                              "text": f"Suggested internal milestone: {label} ({days} days before the published submission deadline)",
                              "due": (due - timedelta(days=days)).isoformat(), "derived": True,
                              "source": _source(procedure, lot_id, locator=deadline.get("locator"), text_hash=digest(deadline))})
    for gap in _gap_items(assessment_lot):
        gap["source"] = {**gap["source"], "procedure_key": procedure["procedure_key"], "lot_id": lot_id}
        items.append(gap)
    return items


class ProcurementWorkspaceStore(FundingWorkspaceStore):
    TABLE = "procurement_workspace"
    READ_SCOPE = READ_SCOPE
    WRITE_SCOPE = WRITE_SCOPE
    SUBJECT = "procurement"

    def __init__(self, conn, *, initialize=True, now=None):  # noqa: D107 - different collaborators, same machinery
        from src.kb.procurement_ranking import ShortlistService
        from src.kb.research_projects import ResearchProjectStore

        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)
        self.shortlists = ShortlistService(conn, initialize=initialize, now=self.now)
        self.eligibility = self.shortlists.eligibility
        self.notices = self.eligibility.notices
        self.profiles = self.eligibility.profiles
        self.projects = ResearchProjectStore(conn, initialize=initialize, now=self.now)

    def create(self, namespace, request_key, *, shortlist_id, item_id, principal_id, scopes):
        shortlist = self.shortlists.inspect(namespace, shortlist_id, principal_id=principal_id, scopes=scopes)
        item = next((i for i in shortlist["items"] if i["item_id"] == item_id), None)
        if item is None:
            raise WorkspaceError("not_shortlisted", "lot is not in the shortlist")
        if item["bucket"] == "excluded":
            raise WorkspaceError("excluded_opportunity", "ineligible, closed, cancelled or awarded lots cannot start a bid workspace")
        workspace_id = "procurement-workspace:" + digest([namespace, principal_id, request_key])[:32]
        request_hash = digest([namespace, principal_id, shortlist_id, item_id])
        prior = self.conn.execute("SELECT request_hash FROM procurement_workspaces WHERE workspace_id=?", [workspace_id]).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise WorkspaceError("idempotency_conflict", "request_key identifies another workspace")
            return {**self.inspect(namespace, workspace_id, principal_id=principal_id, scopes=scopes), "idempotent": True}
        key, lot_id = item["procedure_key"], item["lot_id"]
        procedure = self.notices.get(namespace, key, revision=item["revision"], scopes=scopes)
        assessment = self.eligibility.assess(namespace, shortlist["profile_id"], key, principal_id=principal_id, scopes=scopes,
                                             profile_revision=shortlist["profile_revision"], notice_revision=item["revision"])
        lot_assessment = {**assessment["lots"][lot_id or "_"], "assessment_id": assessment["assessment_id"]}
        pins = {"procedure": {"id": key, "revision": item["revision"], "notice_id": procedure["cause"]["notice_id"], "lot_id": lot_id},
                "profile": {"id": shortlist["profile_id"], "revision": shortlist["profile_revision"]},
                "shortlist": {"id": shortlist_id, "bucket": item["bucket"], "match_score": item["match_score"]}}
        project = self.projects.create(
            namespace, "procurement:" + request_key,
            questions=[f"Prepare a bid for: {procedure['record']['title']}" + (f" — {item['lot_title']} ({lot_id})" if lot_id else "")],
            success_criteria=["Every cited requirement is addressed or explicitly marked not applicable",
                              "Every required document is prepared by the supplier", "The bid is ready before the published deadline"],
            scope={"domains": [], "namespaces": [namespace]}, budget={}, principal_id=principal_id, scopes=scopes,
            origin={"kind": "procurement-workspace", "workspace_id": workspace_id},
            _initial_links=[
                {"kind": "procurement_notice", "id": key, "namespace": namespace, "revision": item["revision"]},
                {"kind": "procurement_profile", "id": shortlist["profile_id"], "namespace": namespace, "revision": shortlist["profile_revision"]},
                {"kind": "procurement_shortlist", "id": shortlist_id, "namespace": namespace, "revision": shortlist["profile_revision"]},
            ])
        items = [{**i, "status": "todo", "stale": False, "note": None} for i in checklist(procedure, lot_id, lot_assessment)]
        state = {"contract": CONTRACT, "workspace_id": workspace_id, "namespace": namespace, "owner": principal_id,
                 "project_id": project["project_id"], "pins": pins, "assessment_id": assessment["assessment_id"],
                 "verdict": lot_assessment["verdict"], "items": items, "outcome": {"state": "preparing", "evidence": None},
                 "revision": 1, "history": [{"revision": 1, "change": "created"}], "notice": NEVER}
        self._authorize(state, principal_id, scopes, write=True)
        state["updated_at_ms"] = self.now()
        self.conn.execute("INSERT INTO procurement_workspaces VALUES (?,?,?,?,?,1)",
                          [workspace_id, namespace, principal_id, project["project_id"], request_hash])
        self.conn.execute("INSERT INTO procurement_workspace_revisions VALUES (?,1,?,?)", [workspace_id, canonical(state), state["updated_at_ms"]])
        self.notices.register_view(namespace, workspace_id, principal_id, {key: item["revision"]})
        return {**state, "idempotent": False}

    def inspect(self, namespace, workspace_id, *, principal_id, scopes, revision=None):
        current = self._state(namespace, workspace_id)
        self._authorize(current, principal_id, scopes)
        state = current if revision is None else self._state(namespace, workspace_id, revision)
        # Profile withdrawal or revoked profile access makes the workspace unreadable too.
        self.profiles.inspect(namespace, state["pins"]["profile"]["id"], principal_id=principal_id, scopes=scopes)
        procedure = self.notices.get(namespace, state["pins"]["procedure"]["id"], scopes=scopes)
        lot = state["pins"]["procedure"]["lot_id"] or "_"
        return {**state, "procedure_status": procedure["status"]["lots"].get(lot, procedure["status"]),
                "notice_revision_current": procedure["current_revision"] == state["pins"]["procedure"]["revision"],
                "freshness": self.notices.view_status(workspace_id),
                "progress": {s: sum(1 for i in state["items"] if i["status"] == s) for s in ITEM_STATUSES},
                "stale_items": [i["item_id"] for i in state["items"] if i["stale"]],
                "documents": [{"item_id": i["item_id"], "document_kind": i["document_kind"], "status": i["status"], "source": i["source"]}
                              for i in state["items"] if i["kind"] == "document"]}

    def refresh(self, namespace, workspace_id, command_key, expected_revision, *, principal_id, scopes):
        """Adopt the current notice revision; keep preparation, flag changed or removed items stale."""
        state = self._state(namespace, workspace_id)
        self._authorize(state, principal_id, scopes, write=True)
        key, lot_id = state["pins"]["procedure"]["id"], state["pins"]["procedure"]["lot_id"]
        procedure = self.notices.get(namespace, key, scopes=scopes)
        profile = self.profiles.inspect(namespace, state["pins"]["profile"]["id"], principal_id=principal_id, scopes=scopes)
        assessment = self.eligibility.assess(namespace, profile["profile_id"], key, principal_id=principal_id, scopes=scopes,
                                             profile_revision=profile["revision"], notice_revision=procedure["revision"])
        lot_assessment = {**assessment["lots"][lot_id or "_"], "assessment_id": assessment["assessment_id"]}
        fresh = {i["item_id"]: i for i in checklist(procedure, lot_id, lot_assessment)}
        lot_state = procedure["status"]["lots"].get(lot_id or "_", {}).get("state")

        def mutate(current):
            existing = {i["item_id"]: i for i in current["items"]}
            items, changes = [], {"changed": [], "added": [], "removed": []}
            for item_id, item in fresh.items():
                if item_id in existing:
                    old = existing[item_id]
                    changed = old["source"].get("text_hash") != item["source"].get("text_hash")
                    items.append({**item, "status": old["status"], "note": old["note"], "stale": old["stale"] or changed})
                    if changed:
                        changes["changed"].append(item_id)
                else:
                    items.append({**item, "status": "todo", "stale": False, "note": None})
                    changes["added"].append(item_id)
            for item_id, old in existing.items():
                if item_id not in fresh:
                    items.append({**old, "stale": True, "removed_from_notice": True})
                    changes["removed"].append(item_id)
            current["items"] = items
            current["pins"]["procedure"].update(revision=procedure["revision"], notice_id=procedure["cause"]["notice_id"])
            current["pins"]["profile"]["revision"] = profile["revision"]
            current["assessment_id"], current["verdict"] = assessment["assessment_id"], lot_assessment["verdict"]
            current["procedure_state"] = lot_state
            return {"kind": "refresh", "notice_revision": procedure["revision"], "notice_id": procedure["cause"]["notice_id"],
                    "lot_state": lot_state, **changes}

        result = self._command(namespace, workspace_id, command_key, expected_revision,
                               {"refresh": [procedure["revision"], profile["revision"]]}, mutate, principal_id=principal_id, scopes=scopes)
        if not result.get("idempotent"):
            project = self.projects.inspect(namespace, state["project_id"], principal_id=principal_id, scopes=scopes)
            links = [link for link in project["links"] if link["kind"] not in {"procurement_notice", "procurement_profile"}]
            links += [{"kind": "procurement_notice", "id": key, "namespace": namespace, "revision": procedure["revision"]},
                      {"kind": "procurement_profile", "id": profile["profile_id"], "namespace": namespace, "revision": profile["revision"]}]
            self.projects.revise(namespace, state["project_id"], project["revision"], principal_id=principal_id, scopes=scopes,
                                 replace_links=[{k: v for k, v in link.items() if k != "question_revision"} for link in links])
            self.notices.register_view(namespace, workspace_id, principal_id, {key: procedure["revision"]})
        return result

    def record_outcome(self, namespace, workspace_id, command_key, expected_revision, outcome, *, principal_id, scopes, evidence=None):
        """Record an owner-reported outcome. This never submits anything or contacts a buyer."""
        if outcome not in OUTCOMES:
            raise WorkspaceError("invalid_outcome", "unsupported outcome")
        if outcome == "buyer_confirmed" and not (isinstance(evidence, dict) and evidence.get("reference")):
            raise WorkspaceError("evidence_required", "a buyer confirmation needs an owner-supplied evidence reference")

        def mutate(state):
            if outcome == "prepared" and any(i["status"] not in {"prepared", "not_applicable"} for i in state["items"]):
                raise WorkspaceError("incomplete_checklist", "every checklist item must be prepared or not applicable")
            state["outcome"] = {"state": outcome, "evidence": evidence, "reported_by": principal_id, "semantics": {
                "prepared": "supplier finished preparation in Noesis",
                "user_reported_submitted": "supplier states they submitted through the buyer's portal outside Noesis",
                "buyer_confirmed": "supplier supplied the portal's receipt reference",
            }.get(outcome, outcome)}
            return {"kind": "outcome", "outcome": outcome}

        return self._command(namespace, workspace_id, command_key, expected_revision, {"outcome": outcome, "evidence": evidence},
                             mutate, principal_id=principal_id, scopes=scopes)


def _profile_dep(profile, key):
    return {"kind": "artifact", "id": profile["profile_id"], "revision": str(profile["revision"]),
            "namespace": profile["namespace"], "locator": {"section": key}}


class ProcurementBidDraftService:
    """Editable, cited bid drafts as authored reports pinned to the notice revision; never fabricated."""

    def __init__(self, conn, *, initialize=True, now=None):
        from src.kb.authored_reports import AuthoredReportStore

        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        self.workspaces = ProcurementWorkspaceStore(conn, initialize=initialize, now=self.now)
        self.reports = AuthoredReportStore(conn, initialize=initialize, now=self.now)

    def generate(self, namespace, workspace_id, request_key, *, principal_id, scopes):
        workspace = self.workspaces.inspect(namespace, workspace_id, principal_id=principal_id, scopes=scopes)
        profiles, notices = self.workspaces.profiles, self.workspaces.notices
        profile = profiles.inspect(namespace, workspace["pins"]["profile"]["id"], principal_id=principal_id, scopes=scopes)
        facts = profiles.pinned_facts(namespace, profile["profile_id"], profile["revision"], principal_id=principal_id, scopes=scopes)["facts"]
        pin = workspace["pins"]["procedure"]
        procedure = notices.get(namespace, pin["id"], revision=pin["revision"], scopes=scopes)
        record = procedure["record"]
        lot_id = pin["lot_id"]
        bibliography = {
            "notice": {"id": "notice", "text": f"{record['title']} ({record['provider']} notice {procedure['cause']['notice_id']}), "
                                               f"procedure revision {procedure['revision']}, {record['source_url']}"},
            "profile": {"id": "profile", "text": f"Supplier profile {profile['label']}, owner-reviewed revision {profile['revision']}"},
        }
        unanswered, sections = [], []

        def fact(section_id, key, template):
            if key not in facts:
                unanswered.append(key)
                return {"id": f"{section_id}:{key}", "kind": "commentary", "dependencies": [], "citations": [],
                        "text": f"UNANSWERED: no owner-reviewed fact for {key}; the supplier must supply it."}
            value = facts[key]
            rendered = ", ".join(value) if isinstance(value, list) and all(isinstance(v, str) for v in value) else \
                f"{value['amount']} {value['currency']}" if isinstance(value, dict) and "amount" in value else str(value)
            return {"id": f"{section_id}:{key}", "kind": "sourced", "text": template.format(rendered),
                    "dependencies": [_profile_dep(profile, key)], "citations": ["profile"]}

        sections.append({"id": "supplier", "title": "Supplier", "assertions": [
            fact("supplier", "supplier.legal_name", "Tenderer: {}."), fact("supplier", "supplier.establishment_country", "Established in {}."),
            fact("supplier", "supplier.size", "Enterprise size: {}."), fact("supplier", "supplier.certifications", "Certifications held: {}.")]})
        assessment = self.workspaces.eligibility.assess(namespace, profile["profile_id"], pin["id"], principal_id=principal_id, scopes=scopes,
                                                        profile_revision=profile["revision"], notice_revision=pin["revision"])
        findings = {f.get("requirement_id"): f for f in assessment["lots"][lot_id or "_"]["findings"]}
        compliance = []
        for requirement in lot_view(record, lot_id)["requirements"]:
            finding = findings.get(requirement["requirement_id"], {})
            compliance.append({
                "id": "req:" + requirement["requirement_id"], "kind": "sourced", "citations": ["notice"],
                "text": f"Requirement ({requirement['category']}): {requirement['text']} — assessment on stated facts: {finding.get('result', 'not assessed')}.",
                "dependencies": [{"kind": "source", "id": procedure["procedure_key"], "revision": str(procedure["revision"]),
                                  "namespace": namespace, "locator": {"section": requirement["requirement_id"]}}]})
        sections.append({"id": "compliance", "title": "Compliance matrix (cited notice requirements)",
                         "assertions": compliance or [{"id": "req:none", "kind": "commentary", "dependencies": [], "citations": [],
                                                       "text": "The notice revision states no exclusion grounds or selection criteria for this lot."}]})
        contracts = facts.get("supplier.past_contracts") or []
        sections.append({"id": "references", "title": "Comparable references", "assertions": [
            {"id": f"reference:{i}", "kind": "sourced", "citations": ["profile"], "dependencies": [_profile_dep(profile, "supplier.past_contracts")],
             "text": f"{c['title']}" + (f" for {c['buyer']}" if c.get("buyer") else "") + (f" ({c['year']})" if c.get("year") else "")}
            for i, c in enumerate(contracts)] or [{"id": "reference:none", "kind": "commentary", "dependencies": [], "citations": [],
                                                   "text": "UNANSWERED: no owner-stated past contracts."}]})
        if not contracts:
            unanswered.append("supplier.past_contracts")
        missing_documents = [i["text"] for i in workspace["items"] if i["kind"] == "document" and i["status"] != "prepared"]
        limitations = [NEVER, "Drafted only from owner-reviewed profile facts and cited notice text; nothing was invented.",
                       f"Pinned to procedure revision {procedure['revision']} (notice {procedure['cause']['notice_id']}); a corrigendum makes it stale."]
        limitations += [f"Unanswered: {u}" for u in unanswered] + [f"Missing document: {d}" for d in missing_documents]
        content = {"title": f"Bid draft — {record['title']}" + (f" ({lot_id})" if lot_id else ""), "sections": sections,
                   "snapshot": {"id": f"{workspace_id}@{workspace['revision']}", "generations": {namespace: procedure["revision"]}},
                   "bibliography": list(bibliography.values()), "limitations": limitations}
        report = self.reports.create(namespace, "procurement-draft:" + request_key, content, principal_id=principal_id, scopes=scopes)
        draft_id = "procurement-draft:" + digest([namespace, principal_id, request_key])[:32]
        summary = {"draft_id": draft_id, "report_id": report["report_id"], "workspace_id": workspace_id,
                   "workspace_revision": workspace["revision"], "notice_revision": procedure["revision"], "lot_id": lot_id,
                   "unanswered": unanswered, "missing_documents": missing_documents, "provenance": {"generated_revision": report["revision"]}}
        self.conn.execute("INSERT INTO procurement_bid_drafts VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [draft_id, namespace, principal_id, workspace_id, workspace["revision"], report["report_id"], report["revision"],
                           canonical(summary), self.now()])
        return summary

    def export(self, namespace, draft_id, *, principal_id, scopes, revision=None):
        row = self.conn.execute("SELECT owner, report_id, workspace_id, generated_revision FROM procurement_bid_drafts WHERE draft_id=? AND namespace=?",
                                [draft_id, namespace]).fetchone()
        if not row or row[0] != principal_id:
            raise WorkspaceError("draft_not_found", "draft is unavailable")
        workspace = self.workspaces.inspect(namespace, row[2], principal_id=principal_id, scopes=scopes)
        exported = self.reports.export(namespace, row[1], principal_id=principal_id, scopes=scopes, revision=revision)
        state = self.reports.inspect(namespace, row[1], principal_id=principal_id, scopes=scopes, revision=revision)
        return {"draft_id": draft_id, "export": exported, "notice_revision_current": workspace["notice_revision_current"],
                "provenance": {"generated_revision": row[3], "exported_revision": state["revision"], "owner_edited": state["revision"] > row[3]}}

    def list(self, namespace, workspace_id, *, principal_id, scopes):
        self.workspaces.inspect(namespace, workspace_id, principal_id=principal_id, scopes=scopes)
        return [json.loads(r[0]) for r in self.conn.execute(
            "SELECT content_json FROM procurement_bid_drafts WHERE namespace=? AND workspace_id=? AND owner=? ORDER BY created_at_ms",
            [namespace, workspace_id, principal_id]).fetchall()]
