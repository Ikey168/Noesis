"""Funding application workspaces, requirement checklists and cited drafts.

A workspace is an existing research project (``ResearchProjectStore``) with
pinned links to the opportunity revision, profile revision and shortlist that
justified it, plus a checklist of authoritative requirements, documents,
deadlines and gaps. When the call is amended the checklist keeps every
preparation status but flags items whose source requirement changed.

Nothing here submits an application or contacts a funder. Outcomes separate
*prepared*, *user-reported submitted* and *provider-confirmed* (which needs an
explicit evidence reference supplied by the owner).

Drafts are authored reports (``AuthoredReportStore``): every sourced
statement depends on an owner-reviewed profile fact, a call requirement or a
quantitative calculation receipt; anything not backed by those is emitted as
an explicit *unanswered* commentary item. No achievement, affiliation,
partner, quotation or cost is generated.
"""

from __future__ import annotations

import json
import time
from decimal import ROUND_HALF_EVEN, Decimal

from src.kb.funding_records import READ_SCOPE, WRITE_SCOPE, canonical, digest

CONTRACT = "noesis-funding-workspace-v1"
ITEM_STATUSES = ("todo", "in_progress", "prepared", "not_applicable")
OUTCOMES = ("preparing", "prepared", "user_reported_submitted", "provider_confirmed", "abandoned")
_DDL = """
CREATE TABLE IF NOT EXISTS funding_workspaces(
 workspace_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 project_id TEXT NOT NULL, request_hash TEXT NOT NULL, revision BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS funding_workspace_revisions(
 workspace_id TEXT NOT NULL, revision BIGINT NOT NULL, content_json TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL, PRIMARY KEY(workspace_id, revision));
CREATE TABLE IF NOT EXISTS funding_workspace_commands(
 workspace_id TEXT NOT NULL, command_key TEXT NOT NULL, request_hash TEXT NOT NULL,
 result_revision BIGINT NOT NULL, PRIMARY KEY(workspace_id, command_key));
CREATE TABLE IF NOT EXISTS funding_drafts(
 draft_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL, workspace_id TEXT NOT NULL,
 workspace_revision BIGINT NOT NULL, report_id TEXT NOT NULL, generated_revision BIGINT NOT NULL,
 content_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
"""


class WorkspaceError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _requirement_items(opportunity):
    record, items = opportunity["record"], []
    for requirement in record.get("requirements") or []:
        items.append({"item_id": "req:" + requirement["requirement_id"], "kind": "requirement",
                      "text": requirement["text"], "category": requirement["category"],
                      "source": {"opportunity_revision": opportunity["revision"], "locator": requirement["locator"],
                                 "text_hash": digest(requirement["text"])}})
    for document in record.get("documents") or []:
        items.append({"item_id": "doc:" + digest(document["url"])[:16], "kind": "document",
                      "text": f"Review {document['kind']}: {document['title']}",
                      "source": {"opportunity_revision": opportunity["revision"], "url": document["url"],
                                 "text_hash": digest(document)}})
    for deadline in record.get("deadlines") or []:
        if deadline["kind"] == "opening":
            continue
        key = deadline.get("stage") or deadline["kind"]
        items.append({"item_id": "deadline:" + key, "kind": "milestone",
                      "text": f"{key}: {deadline['text']}" + ("" if deadline.get("instant") else " (exact cutoff time unknown)"),
                      "due": deadline.get("instant") or deadline.get("date"),
                      "source": {"opportunity_revision": opportunity["revision"], "locator": deadline.get("locator"),
                                 "text_hash": digest(deadline)}})
    return items


def _gap_items(assessment):
    items = []
    for finding in assessment["clarifications"]:
        if not finding.get("requirement_id"):
            continue
        label = {"affiliation": "affiliation gap", "consortium": "partner gap"}.get(finding.get("category"), "clarification")
        missing = ", ".join(finding.get("missing_facts") or []) or finding.get("reason") or finding["result"]
        items.append({"item_id": "gap:" + finding["requirement_id"], "kind": "gap",
                      "text": f"{label}: {finding['text'][:300]} — {missing}", "category": finding.get("category"),
                      # Hash the substance, not the citation, so a new call revision
                      # alone does not make an unchanged gap look stale.
                      "source": {"assessment_id": assessment.get("assessment_id"), "text_hash": digest(
                          [finding.get("text"), finding["result"], finding.get("missing_facts"), finding.get("reason")])}})
    return items


class FundingWorkspaceStore:
    def __init__(self, conn, *, initialize=True, now=None):
        from src.kb.funding_ranking import ShortlistService
        from src.kb.research_projects import ResearchProjectStore

        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)
        self.shortlists = ShortlistService(conn, initialize=initialize, now=self.now)
        self.eligibility = self.shortlists.eligibility
        self.projects = ResearchProjectStore(conn, initialize=initialize, now=self.now)

    @staticmethod
    def _authorize(state, principal_id, scopes, *, write=False):
        if not principal_id or (WRITE_SCOPE if write else READ_SCOPE) not in scopes or state["owner"] != principal_id:
            raise WorkspaceError("unauthorized", "only the workspace owner with current funding scope can access it")
        namespace = state["namespace"]
        if f"namespace:{namespace}:write" not in scopes and (write or f"namespace:{namespace}:read" not in scopes):
            raise WorkspaceError("unauthorized", "current namespace access is required")

    def _state(self, namespace, workspace_id, revision=None):
        row = self.conn.execute(
            """SELECT r.content_json FROM funding_workspaces w JOIN funding_workspace_revisions r
               ON r.workspace_id=w.workspace_id AND r.revision=coalesce(?, w.revision)
               WHERE w.workspace_id=? AND w.namespace=?""", [revision, workspace_id, namespace]).fetchone()
        if not row:
            raise WorkspaceError("workspace_not_found", "workspace is unavailable")
        return json.loads(row[0])

    def create(self, namespace, request_key, *, shortlist_id, opportunity_id, principal_id, scopes):
        shortlist = self.shortlists.inspect(namespace, shortlist_id, principal_id=principal_id, scopes=scopes)
        item = next((i for i in shortlist["items"] if i["opportunity_id"] == opportunity_id), None)
        if item is None:
            raise WorkspaceError("not_shortlisted", "opportunity is not in the shortlist")
        if item["bucket"] == "excluded":
            raise WorkspaceError("excluded_opportunity", "ineligible or closed opportunities cannot start a workspace")
        workspace_id = "funding-workspace:" + digest([namespace, principal_id, request_key])[:32]
        request_hash = digest([namespace, principal_id, shortlist_id, opportunity_id])
        prior = self.conn.execute("SELECT request_hash FROM funding_workspaces WHERE workspace_id=?", [workspace_id]).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise WorkspaceError("idempotency_conflict", "request_key identifies another workspace")
            return {**self.inspect(namespace, workspace_id, principal_id=principal_id, scopes=scopes), "idempotent": True}
        opportunity = self.eligibility.opportunities.get(namespace, opportunity_id, revision=item["revision"], scopes=scopes)
        assessment = self.eligibility.assess(namespace, shortlist["profile_id"], opportunity_id, principal_id=principal_id,
                                             scopes=scopes, profile_revision=shortlist["profile_revision"],
                                             opportunity_revision=item["revision"])
        pins = {"opportunity": {"id": opportunity_id, "revision": item["revision"]},
                "profile": {"id": shortlist["profile_id"], "revision": shortlist["profile_revision"]},
                "shortlist": {"id": shortlist_id, "bucket": item["bucket"], "match_score": item["match_score"]}}
        project = self.projects.create(
            namespace, "funding:" + request_key,
            questions=[f"Prepare an application for: {opportunity['record']['title']}"],
            success_criteria=["All authoritative requirements are addressed or explicitly waived",
                              "Required documents and budget are prepared by the applicant"],
            scope={"domains": [], "namespaces": [namespace]}, budget={},
            principal_id=principal_id, scopes=scopes,
            origin={"kind": "funding-workspace", "workspace_id": workspace_id},
            _initial_links=[
                {"kind": "funding_opportunity", "id": opportunity_id, "namespace": namespace, "revision": item["revision"]},
                {"kind": "funding_profile", "id": shortlist["profile_id"], "namespace": namespace, "revision": shortlist["profile_revision"]},
                {"kind": "funding_shortlist", "id": shortlist_id, "namespace": namespace, "revision": shortlist["profile_revision"]},
            ])
        items = [{**i, "status": "todo", "stale": False, "note": None}
                 for i in _requirement_items(opportunity) + _gap_items(assessment)]
        state = {"contract": CONTRACT, "workspace_id": workspace_id, "namespace": namespace, "owner": principal_id,
                 "project_id": project["project_id"], "pins": pins, "assessment_id": assessment["assessment_id"],
                 "verdict": assessment["verdict"], "items": items, "outcome": {"state": "preparing", "evidence": None},
                 "revision": 1, "history": [{"revision": 1, "change": "created"}],
                 "notice": "Preparation only. Noesis never submits applications or contacts funders."}
        self._authorize(state, principal_id, scopes, write=True)
        state["updated_at_ms"] = self.now()
        self.conn.execute("INSERT INTO funding_workspaces VALUES (?,?,?,?,?,1)",
                          [workspace_id, namespace, principal_id, project["project_id"], request_hash])
        self.conn.execute("INSERT INTO funding_workspace_revisions VALUES (?,1,?,?)",
                          [workspace_id, canonical(state), state["updated_at_ms"]])
        self.eligibility.opportunities.register_view(namespace, workspace_id, principal_id, {opportunity_id: item["revision"]})
        return {**state, "idempotent": False}

    def inspect(self, namespace, workspace_id, *, principal_id, scopes, revision=None):
        current = self._state(namespace, workspace_id)
        self._authorize(current, principal_id, scopes)
        state = current if revision is None else self._state(namespace, workspace_id, revision)
        # Profile withdrawal or revoked profile access makes the workspace unreadable too.
        self.eligibility.profiles.inspect(namespace, state["pins"]["profile"]["id"], principal_id=principal_id, scopes=scopes)
        opportunity = self.eligibility.opportunities.get(namespace, state["pins"]["opportunity"]["id"], scopes=scopes)
        return {**state, "call_status": opportunity["status"],
                "call_revision_current": opportunity["current_revision"] == state["pins"]["opportunity"]["revision"],
                "freshness": self.eligibility.opportunities.view_status(workspace_id),
                "progress": {s: sum(1 for i in state["items"] if i["status"] == s) for s in ITEM_STATUSES},
                "stale_items": [i["item_id"] for i in state["items"] if i["stale"]]}

    def _command(self, namespace, workspace_id, command_key, expected_revision, request, mutate, *, principal_id, scopes):
        request_hash = digest(request)
        self.conn.execute("BEGIN")
        try:
            state = self._state(namespace, workspace_id)
            self._authorize(state, principal_id, scopes, write=True)
            prior = self.conn.execute(
                "SELECT request_hash, result_revision FROM funding_workspace_commands WHERE workspace_id=? AND command_key=?",
                [workspace_id, command_key]).fetchone()
            if prior:
                if prior[0] != request_hash:
                    raise WorkspaceError("idempotency_conflict", "command_key was used for another change")
                self.conn.execute("COMMIT")
                return {**self._state(namespace, workspace_id, prior[1]), "idempotent": True}
            if state["revision"] != expected_revision:
                raise WorkspaceError("revision_conflict", "workspace changed; inspect the current revision")
            change = mutate(state)
            state["revision"] += 1
            state["updated_at_ms"] = self.now()
            state["history"].append({"revision": state["revision"], "change": change, "command_key": command_key})
            if not self.conn.execute("UPDATE funding_workspaces SET revision=? WHERE workspace_id=? AND revision=? RETURNING revision",
                                     [state["revision"], workspace_id, expected_revision]).fetchone():
                raise WorkspaceError("revision_conflict", "workspace changed concurrently")
            self.conn.execute("INSERT INTO funding_workspace_revisions VALUES (?,?,?,?)",
                              [workspace_id, state["revision"], canonical(state), state["updated_at_ms"]])
            self.conn.execute("INSERT INTO funding_workspace_commands VALUES (?,?,?,?)",
                              [workspace_id, command_key, request_hash, state["revision"]])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**state, "idempotent": False}

    def update_items(self, namespace, workspace_id, command_key, expected_revision, updates, *, principal_id, scopes):
        """Owner-reviewed status updates: ``{item_id: {"status":..., "note":...}}``."""
        if not isinstance(updates, dict) or not updates:
            raise WorkspaceError("invalid_update", "item updates are required")

        def mutate(state):
            items = {i["item_id"]: i for i in state["items"]}
            for item_id, update in updates.items():
                if item_id not in items:
                    raise WorkspaceError("item_not_found", f"{item_id} is not in this workspace")
                if not isinstance(update, dict) or set(update) - {"status", "note", "acknowledge_change"}:
                    raise WorkspaceError("invalid_update", "updates set status, note or acknowledge_change")
                if "status" in update:
                    if update["status"] not in ITEM_STATUSES:
                        raise WorkspaceError("invalid_update", "unsupported item status")
                    items[item_id]["status"] = update["status"]
                if "note" in update:
                    items[item_id]["note"] = update["note"]
                if update.get("acknowledge_change"):
                    items[item_id]["stale"] = False
            return {"kind": "items", "items": sorted(updates)}

        return self._command(namespace, workspace_id, command_key, expected_revision, {"items": updates}, mutate,
                             principal_id=principal_id, scopes=scopes)

    def record_outcome(self, namespace, workspace_id, command_key, expected_revision, outcome, *, principal_id, scopes, evidence=None):
        """Record an owner-reported outcome. This never submits anything."""
        if outcome not in OUTCOMES:
            raise WorkspaceError("invalid_outcome", "unsupported outcome")
        if outcome == "provider_confirmed" and not (isinstance(evidence, dict) and evidence.get("reference")):
            raise WorkspaceError("evidence_required", "provider confirmation needs an owner-supplied evidence reference")

        def mutate(state):
            if outcome == "prepared" and any(i["status"] not in {"prepared", "not_applicable"} for i in state["items"]):
                raise WorkspaceError("incomplete_checklist", "every checklist item must be prepared or not applicable")
            state["outcome"] = {"state": outcome, "evidence": evidence,
                                "reported_by": principal_id, "semantics": {
                                    "prepared": "applicant finished preparation in Noesis",
                                    "user_reported_submitted": "applicant states they submitted outside Noesis",
                                    "provider_confirmed": "applicant supplied a funder confirmation reference",
                                }.get(outcome, outcome)}
            return {"kind": "outcome", "outcome": outcome}

        return self._command(namespace, workspace_id, command_key, expected_revision,
                             {"outcome": outcome, "evidence": evidence}, mutate, principal_id=principal_id, scopes=scopes)

    def refresh(self, namespace, workspace_id, command_key, expected_revision, *, principal_id, scopes):
        """Adopt the current call revision; keep preparation, flag changed requirements."""
        state = self._state(namespace, workspace_id)
        self._authorize(state, principal_id, scopes, write=True)
        opportunity_id = state["pins"]["opportunity"]["id"]
        opportunity = self.eligibility.opportunities.get(namespace, opportunity_id, scopes=scopes)
        profile = self.eligibility.profiles.inspect(namespace, state["pins"]["profile"]["id"], principal_id=principal_id, scopes=scopes)
        assessment = self.eligibility.assess(namespace, profile["profile_id"], opportunity_id, principal_id=principal_id,
                                             scopes=scopes, profile_revision=profile["revision"],
                                             opportunity_revision=opportunity["revision"])
        fresh = {i["item_id"]: i for i in _requirement_items(opportunity) + _gap_items(assessment)}

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
                    items.append({**old, "stale": True, "removed_from_call": True})
                    changes["removed"].append(item_id)
            current["items"] = items
            current["pins"]["opportunity"]["revision"] = opportunity["revision"]
            current["pins"]["profile"]["revision"] = profile["revision"]
            current["assessment_id"], current["verdict"] = assessment["assessment_id"], assessment["verdict"]
            return {"kind": "refresh", "opportunity_revision": opportunity["revision"], **changes}

        result = self._command(namespace, workspace_id, command_key, expected_revision,
                               {"refresh": [opportunity["revision"], profile["revision"]]}, mutate,
                               principal_id=principal_id, scopes=scopes)
        if not result.get("idempotent"):
            project = self.projects.inspect(namespace, state["project_id"], principal_id=principal_id, scopes=scopes)
            links = [link for link in project["links"] if link["kind"] not in {"funding_opportunity", "funding_profile"}]
            links += [{"kind": "funding_opportunity", "id": opportunity_id, "namespace": namespace, "revision": opportunity["revision"]},
                      {"kind": "funding_profile", "id": profile["profile_id"], "namespace": namespace, "revision": profile["revision"]}]
            self.projects.revise(namespace, state["project_id"], project["revision"], principal_id=principal_id,
                                 scopes=scopes, replace_links=[{k: v for k, v in link.items() if k != "question_revision"} for link in links])
            self.eligibility.opportunities.register_view(namespace, workspace_id, principal_id, {opportunity_id: opportunity["revision"]})
        return result


# ------------------------------------------------------------------ drafts


def _artifact(profile, key):
    return {"kind": "artifact", "id": profile["profile_id"], "revision": str(profile["revision"]),
            "namespace": profile["namespace"], "locator": {"section": key}}


def _source(opportunity, requirement_id, namespace):
    return {"kind": "source", "id": opportunity["opportunity_id"], "revision": str(opportunity["revision"]),
            "namespace": namespace, "locator": {"section": requirement_id}}


class FundingDraftService:
    """Generate editable, cited drafts as authored reports; never fabricate."""

    def __init__(self, conn, *, initialize=True, now=None):
        from src.kb.authored_reports import AuthoredReportStore
        from src.kb.quantitative import QuantitativeStore

        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        self.workspaces = FundingWorkspaceStore(conn, initialize=initialize, now=self.now)
        self.reports = AuthoredReportStore(conn, initialize=initialize, now=self.now)
        self.quantitative = QuantitativeStore(conn, initialize=initialize, now=self.now)

    def _budget(self, namespace, opportunity, facts, *, principal_id):
        terms = opportunity["record"].get("financial_terms") or {}
        lines = facts.get("project.budget_lines")
        if not lines:
            return None, ["No owner-estimated budget lines; budget left unanswered."]
        currency = terms.get("currency")
        issues, total, eligible = [], Decimal(0), Decimal(0)
        currencies = {line["currency"] for line in lines}
        if len(currencies) != 1:
            return None, ["Budget lines use several currencies; no conversion is applied."]
        line_currency = currencies.pop()
        if currency and currency != line_currency:
            issues.append(f"Budget currency {line_currency} differs from call currency {currency}; no conversion applied.")
        costs = terms.get("eligible_costs")
        annotated = []
        for line in lines:
            amount = Decimal(line["amount"])
            total += amount
            if costs is None:
                state = "unknown"
            else:
                state = "eligible" if any(line["category"].lower() in c.lower() or c.lower() in line["category"].lower() for c in costs) else "not-listed"
            if state == "eligible":
                eligible += amount
            annotated.append({**line, "eligibility": state})
            if state != "eligible":
                issues.append(f"Cost category '{line['category']}' is {'not confirmed' if state == 'unknown' else 'not listed'} as eligible.")
        rate = (terms.get("funding_rate") or {}).get("max_percent")
        award_max = (terms.get("award_range") or {}).get("max") if (terms.get("award_range") or {}).get("basis") == "per-project" else None
        requested = eligible * Decimal(rate) / 100 if rate else eligible
        if award_max is not None:
            requested = min(requested, Decimal(award_max))
        quant = Decimal("0.01")
        result = {"currency": line_currency, "total": str(total.quantize(quant, ROUND_HALF_EVEN)),
                  "eligible_total": str(eligible.quantize(quant, ROUND_HALF_EVEN)),
                  "requested": str(requested.quantize(quant, ROUND_HALF_EVEN)),
                  "co_financing": str((total - requested).quantize(quant, ROUND_HALF_EVEN)),
                  "assumptions": {"funding_rate_max_percent": rate, "award_max_per_project": award_max,
                                  "matching_funds_available": facts.get("project.matching_funds_available")}}
        own = facts.get("project.matching_funds_available")
        if own and own["currency"] == line_currency and Decimal(own["amount"]) < total - requested:
            issues.append("Stated matching funds do not cover the co-financing share.")
        receipt = self.quantitative._calculation(
            namespace, "funding-budget", {"lines": annotated, "funding_rate_max_percent": rate, "award_max": award_max},
            result, input_ids=[opportunity["opportunity_id"] + "@" + str(opportunity["revision"])],
            principal_id=principal_id, formula_revision_id=None)
        # Independent re-verification of the arithmetic before it is cited.
        if Decimal(result["total"]) != sum(Decimal(line["amount"]) for line in lines).quantize(quant) or \
                Decimal(result["co_financing"]) + Decimal(result["requested"]) != Decimal(result["total"]):
            raise WorkspaceError("arithmetic_mismatch", "budget arithmetic failed verification")
        return {**result, "lines": annotated, "calculation_id": receipt["calculation_id"]}, issues

    def generate(self, namespace, workspace_id, request_key, *, principal_id, scopes, questions=None):
        workspace = self.workspaces.inspect(namespace, workspace_id, principal_id=principal_id, scopes=scopes)
        eligibility = self.workspaces.eligibility
        profile = eligibility.profiles.inspect(namespace, workspace["pins"]["profile"]["id"], principal_id=principal_id, scopes=scopes)
        facts = eligibility.profiles.pinned_facts(namespace, profile["profile_id"], profile["revision"],
                                                  principal_id=principal_id, scopes=scopes)["facts"]
        opportunity = eligibility.opportunities.get(namespace, workspace["pins"]["opportunity"]["id"],
                                                    revision=workspace["pins"]["opportunity"]["revision"], scopes=scopes)
        record = opportunity["record"]
        bibliography = {
            "call": {"id": "call", "text": f"{record['title']} ({record['provider']}), revision {opportunity['revision']}, {record['source_url']}"},
            "profile": {"id": "profile", "text": f"Applicant profile {profile['label']}, owner-reviewed revision {profile['revision']}"},
        }
        unanswered, sections = [], []

        def fact_assertion(section_id, key, template):
            if key not in facts:
                unanswered.append(key)
                return {"id": f"{section_id}:{key}", "kind": "commentary", "dependencies": [], "citations": [],
                        "text": f"UNANSWERED: no owner-reviewed fact for {key}; supply it before submission."}
            value = facts[key]
            rendered = ", ".join(value) if isinstance(value, list) and all(isinstance(v, str) for v in value) else \
                f"{value['amount']} {value['currency']}" if isinstance(value, dict) and "amount" in value else str(value)
            return {"id": f"{section_id}:{key}", "kind": "sourced", "text": template.format(rendered),
                    "dependencies": [_artifact(profile, key)], "citations": ["profile"]}

        questions = questions or [
            {"id": "summary", "text": "Project summary", "facts": [("project.summary", "{}")]},
            {"id": "relevance", "text": "Relevance to the call", "facts": [("project.themes", "The project addresses: {}.")]},
            {"id": "applicant", "text": "Applicant and team", "facts": [
                ("applicant.kind", "Applicant type: {}."), ("applicant.team_size", "Team size: {}."),
                ("applicant.university_name", "Affiliated institution: {}.")]},
            {"id": "track-record", "text": "Achievements to date", "facts": [("project.achievements", "Stated achievements: {}.")]},
        ]
        for question in questions:
            if not isinstance(question, dict) or not question.get("id") or not question.get("text"):
                raise WorkspaceError("invalid_question", "questions need id and text")
            assertions = [fact_assertion(question["id"], key, template) for key, template in question.get("facts") or []]
            if not assertions:
                assertions = [{"id": f"{question['id']}:open", "kind": "commentary", "dependencies": [], "citations": [],
                               "text": "UNANSWERED: this question has no mapped profile facts; the applicant must write it."}]
                unanswered.append(question["id"])
            sections.append({"id": question["id"], "title": question["text"], "assertions": assertions})
        requirement_assertions = []
        findings = {f.get("requirement_id"): f for f in eligibility.assess(
            namespace, profile["profile_id"], opportunity["opportunity_id"], principal_id=principal_id, scopes=scopes,
            profile_revision=profile["revision"], opportunity_revision=opportunity["revision"])["findings"]}
        for requirement in record.get("requirements") or []:
            finding = findings.get(requirement["requirement_id"], {})
            requirement_assertions.append({
                "id": "req:" + requirement["requirement_id"], "kind": "sourced",
                "text": f"Requirement: {requirement['text']} — assessment: {finding.get('result', 'not assessed')}.",
                "dependencies": [_source(opportunity, requirement["requirement_id"], namespace)], "citations": ["call"]})
        sections.append({"id": "requirements", "title": "Call requirements and how they are met",
                         "assertions": requirement_assertions or [{"id": "req:none", "kind": "commentary", "dependencies": [],
                                                                   "citations": [], "text": "The call revision lists no parsed requirements."}]})
        milestones = facts.get("project.milestones")
        sections.append({"id": "milestones", "title": "Milestones", "assertions": [
            {"id": f"milestone:{i}", "kind": "sourced", "citations": ["profile"],
             "dependencies": [_artifact(profile, "project.milestones")],
             "text": f"Month {m.get('month', '?')}: {m['title']}" + (f" — deliverable: {m['deliverable']}" if m.get("deliverable") else "")}
            for i, m in enumerate(milestones or [])] or [
            {"id": "milestone:none", "kind": "commentary", "dependencies": [], "citations": [],
             "text": "UNANSWERED: no owner-planned milestones."}]})
        if not milestones:
            unanswered.append("project.milestones")
        budget, issues = self._budget(namespace, opportunity, facts, principal_id=principal_id)
        budget_assertions = [{"id": f"budget-issue:{i}", "kind": "commentary", "dependencies": [], "citations": [], "text": issue}
                             for i, issue in enumerate(issues)]
        if budget:
            bibliography["budget"] = {"id": "budget", "text": f"Budget calculation receipt {budget['calculation_id']}"}
            budget_assertions.insert(0, {
                "id": "budget:totals", "kind": "sourced", "citations": ["budget", "profile"],
                "dependencies": [{"kind": "calculation", "id": budget["calculation_id"], "revision": "1", "namespace": namespace, "locator": {}},
                                 _artifact(profile, "project.budget_lines")],
                "text": (f"Total {budget['total']} {budget['currency']}; eligible {budget['eligible_total']}; "
                         f"requested {budget['requested']}; co-financing {budget['co_financing']}.")})
        else:
            unanswered.append("project.budget_lines")
        sections.append({"id": "budget", "title": "Budget", "assertions": budget_assertions})
        missing_documents = [i["text"] for i in workspace["items"] if i["kind"] == "document" and i["status"] != "prepared"]
        limitations = [
            "Draft generated only from owner-reviewed profile facts and cited call text; nothing was invented.",
            "Not submitted. Noesis does not submit applications or contact funders.",
        ] + [f"Unanswered: {u}" for u in unanswered] + [f"Missing document: {d}" for d in missing_documents]
        content = {"title": f"Application draft — {record['title']}", "sections": sections,
                   "snapshot": {"id": f"{workspace_id}@{workspace['revision']}", "generations": {namespace: opportunity["revision"]}},
                   "bibliography": list(bibliography.values()), "limitations": limitations}
        report = self.reports.create(namespace, "funding-draft:" + request_key, content, principal_id=principal_id, scopes=scopes)
        draft_id = "funding-draft:" + digest([namespace, principal_id, request_key])[:32]
        summary = {"draft_id": draft_id, "report_id": report["report_id"], "workspace_id": workspace_id,
                   "workspace_revision": workspace["revision"], "opportunity_revision": opportunity["revision"],
                   "unanswered": unanswered, "missing_documents": missing_documents, "budget": budget,
                   "budget_issues": issues, "provenance": {"generated_revision": report["revision"]}}
        self.conn.execute("INSERT INTO funding_drafts VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [draft_id, namespace, principal_id, workspace_id, workspace["revision"], report["report_id"],
                           report["revision"], canonical(summary), self.now()])
        return summary

    def edit(self, namespace, draft_id, expected_revision, content, *, principal_id, scopes):
        """Owner edits become new report revisions; generation never overwrites them."""
        row = self.conn.execute("SELECT owner, report_id FROM funding_drafts WHERE draft_id=? AND namespace=?",
                                [draft_id, namespace]).fetchone()
        if not row or row[0] != principal_id:
            raise WorkspaceError("draft_not_found", "draft is unavailable")
        return self.reports.revise(namespace, row[1], expected_revision, content, principal_id=principal_id, scopes=scopes)

    def export(self, namespace, draft_id, *, principal_id, scopes, revision=None):
        row = self.conn.execute("SELECT owner, report_id, workspace_id, generated_revision FROM funding_drafts WHERE draft_id=? AND namespace=?",
                                [draft_id, namespace]).fetchone()
        if not row or row[0] != principal_id:
            raise WorkspaceError("draft_not_found", "draft is unavailable")
        # Current access to the workspace (and therefore the private profile) is re-checked.
        self.workspaces.inspect(namespace, row[2], principal_id=principal_id, scopes=scopes)
        exported = self.reports.export(namespace, row[1], principal_id=principal_id, scopes=scopes, revision=revision)
        state = self.reports.inspect(namespace, row[1], principal_id=principal_id, scopes=scopes, revision=revision)
        return {"draft_id": draft_id, "export": exported,
                "provenance": {"generated_revision": row[3], "exported_revision": state["revision"],
                               "owner_edited": state["revision"] > row[3]}}
