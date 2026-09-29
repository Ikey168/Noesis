"""Public Procurement entry points: discovery, profile-to-shortlist, award history, bid preparation, monitoring.

Every tool checks the namespace's bundle enablement first; shared providers,
Funding & Grants and other workflows are not gated by it. Acquisition runs
through the source-pack runtime (``run_source_pack_execution`` with pack
``procurement``). No tool submits a bid or contacts a buyer or portal.
"""

from src.kb.procurement_bundle import BUNDLE, readiness, require_enabled, set_enabled

PROCUREMENT_WRITES = {
    "set_procurement_bundle_enabled",
    "create_procurement_profile", "update_procurement_profile", "withdraw_procurement_profile",
    "assess_procurement_eligibility", "build_procurement_shortlist",
    "decide_procurement_party_link", "revert_procurement_party_link",
    "create_procurement_workspace", "update_procurement_workspace_items", "refresh_procurement_workspace",
    "record_procurement_workspace_outcome", "draft_procurement_bid",
    "create_procurement_monitor", "run_procurement_monitor",
}
PROCUREMENT_TOOLS = PROCUREMENT_WRITES | {
    "procurement_bundle_status", "procurement_provider_contracts", "list_procurement_notices",
    "inspect_procurement_notice", "procurement_notice_history", "inspect_procurement_profile",
    "inspect_procurement_shortlist", "replay_procurement_shortlist", "procurement_award_history",
    "procurement_incumbency", "procurement_party_candidates", "inspect_procurement_workspace",
    "export_procurement_bid_draft", "poll_procurement_monitor",
}
READ, WRITE, REVIEW = "knowledge:procurement:read", "knowledge:procurement:write", "knowledge:procurement:review"
PROCUREMENT_SCOPES = {
    "set_procurement_bundle_enabled": ["operator"],
    "decide_procurement_party_link": [REVIEW, "knowledge:entity-history:review", "knowledge:entity-history:write"],
    "revert_procurement_party_link": [REVIEW, "knowledge:entity-history:review", "knowledge:entity-history:write",
                                      "knowledge:entity-history:execute"],
    "create_procurement_workspace": [WRITE, "knowledge:projects:write"],
    "refresh_procurement_workspace": [WRITE, "knowledge:projects:write"],
    "draft_procurement_bid": [WRITE, "knowledge:reports:write"],
    "export_procurement_bid_draft": [READ, "knowledge:reports:read"],
    "create_procurement_monitor": [WRITE, "knowledge:subscriptions:write"],
    "run_procurement_monitor": [WRITE, "knowledge:subscriptions:write"],
    "poll_procurement_monitor": [READ, "knowledge:subscriptions:read"],
}


def required_scopes(tool_name, mutability):
    if tool_name == "procurement_provider_contracts":
        return []
    return PROCUREMENT_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def register(mcp, safe, context):
    def gated(namespace, operation, *, write=False, scope=READ):
        def run(conn):
            require_enabled(conn, namespace)
            return operation(conn)
        return safe(run, write=write, required_scope=scope)

    def who():
        return context()[0], context()[1]

    @mcp.tool()
    def procurement_bundle_status(namespace: str) -> dict:
        """Declared contributions plus ready/fixture-only/unavailable/not-implemented state per provider; offline and live evidence apart."""
        return safe(lambda conn: {**readiness(conn, namespace, scopes=who()[1]), "declaration": BUNDLE}, required_scope=READ)

    @mcp.tool()
    def set_procurement_bundle_enabled(namespace: str, enabled: bool) -> dict:
        """Enable/disable Public Procurement (a coordinator selection change once composed); Funding & Grants and shared providers stay usable."""
        return safe(lambda conn: set_enabled(conn, namespace, enabled, principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="operator")

    @mcp.tool()
    def procurement_provider_contracts() -> dict:
        """Per-provider access contracts (TED, Find a Tender, Contracts Finder, SAM.gov, German portals, OpenTender), eForms mapping and live state."""
        from src.ingestion.procurement_providers import EFORMS_MAPPING, LAST_LIVE_CHECK, LIVE_VERIFICATION, PROVIDER_CONTRACTS
        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION, "last_live_check": LAST_LIVE_CHECK,
                "eforms_mapping": EFORMS_MAPPING, "acquisition": "run_source_pack_execution with pack_id 'procurement'"}

    @mcp.tool()
    def list_procurement_notices(namespace: str, providers: list[str] | None = None) -> dict:
        """Current procedures with per-lot derived state, deadlines (original text), freshness and award links."""
        from src.kb.procurement_notices import ProcurementNoticeStore
        return gated(namespace, lambda conn: {"procedures": ProcurementNoticeStore(conn, initialize=False).list(
            namespace, scopes=who()[1], providers=providers)})

    @mcp.tool()
    def inspect_procurement_notice(namespace: str, procedure_key: str, revision: int | None = None) -> dict:
        """One procedure revision: the causing notice, lots, cited requirements, deadlines, values (estimated vs awarded) and awards."""
        from src.kb.procurement_notices import ProcurementNoticeStore
        return gated(namespace, lambda conn: ProcurementNoticeStore(conn, initialize=False).get(
            namespace, procedure_key, scopes=who()[1], revision=revision))

    @mcp.tool()
    def procurement_notice_history(namespace: str, procedure_key: str) -> dict:
        """Revision history: which notice (contract notice, corrigendum, cancellation) caused each revision and what changed."""
        from src.kb.procurement_notices import ProcurementNoticeStore
        return gated(namespace, lambda conn: {"revisions": ProcurementNoticeStore(conn, initialize=False).history(
            namespace, procedure_key, scopes=who()[1])})

    @mcp.tool()
    def create_procurement_profile(namespace: str, request_key: str, label: str, kind: str = "supplier") -> dict:
        """Create a private, owner-scoped supplier or buyer profile with no default facts."""
        from src.kb.procurement_profiles import ProcurementProfileStore
        return gated(namespace, lambda conn: ProcurementProfileStore(conn).create(
            namespace, request_key, label=label, kind=kind, principal_id=who()[0], scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def update_procurement_profile(namespace: str, profile_id: str, command_key: str, expected_revision: int,
                                   set_facts: dict | None = None, propose_facts: dict | None = None,
                                   clear_facts: list[str] | None = None, review: list[str] | None = None,
                                   reject: list[str] | None = None) -> dict:
        """Owner-reviewed fact changes (capabilities, turnover, certifications, exclusion self-declarations); proposals stay unknown."""
        from src.kb.procurement_profiles import ProcurementProfileStore
        return gated(namespace, lambda conn: ProcurementProfileStore(conn).update(
            namespace, profile_id, command_key, expected_revision, set_facts=set_facts, propose_facts=propose_facts,
            clear_facts=clear_facts, review=review, reject=reject, principal_id=who()[0], scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def inspect_procurement_profile(namespace: str, profile_id: str, revision: int | None = None) -> dict:
        """Owner-only profile view with unknown and unreviewed facts listed."""
        from src.kb.procurement_profiles import ProcurementProfileStore
        return gated(namespace, lambda conn: ProcurementProfileStore(conn, initialize=False).inspect(
            namespace, profile_id, revision=revision, principal_id=who()[0], scopes=who()[1]))

    @mcp.tool()
    def withdraw_procurement_profile(namespace: str, profile_id: str) -> dict:
        """Withdraw a profile; it and every view pinned to it become unreadable."""
        from src.kb.procurement_profiles import ProcurementProfileStore
        return gated(namespace, lambda conn: ProcurementProfileStore(conn, initialize=False).withdraw(
            namespace, profile_id, principal_id=who()[0], scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def assess_procurement_eligibility(namespace: str, profile_id: str, procedure_key: str) -> dict:
        """Per-lot requirements assessment with cited notice passages, profile facts used and unknowns (not the buyer's decision)."""
        from src.kb.procurement_eligibility import ProcurementEligibilityService
        return gated(namespace, lambda conn: ProcurementEligibilityService(conn).assess(
            namespace, profile_id, procedure_key, principal_id=who()[0], scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def build_procurement_shortlist(namespace: str, profile_id: str, weights: dict | None = None,
                                    providers: list[str] | None = None) -> dict:
        """Explained per-lot shortlist: eligibility, fit inputs with sources, award context, buckets and sensitivity (not a win probability)."""
        from src.kb.procurement_ranking import ShortlistService
        return gated(namespace, lambda conn: ShortlistService(conn).build(
            namespace, profile_id, principal_id=who()[0], scopes=who()[1], weights=weights, providers=providers), write=True, scope=WRITE)

    @mcp.tool()
    def inspect_procurement_shortlist(namespace: str, shortlist_id: str) -> dict:
        """A stored shortlist and whether corrigenda, cancellations or awards have invalidated it."""
        from src.kb.procurement_ranking import ShortlistService
        return gated(namespace, lambda conn: ShortlistService(conn, initialize=False).inspect(
            namespace, shortlist_id, principal_id=who()[0], scopes=who()[1]))

    @mcp.tool()
    def replay_procurement_shortlist(namespace: str, shortlist_id: str) -> dict:
        """Recompute a shortlist from its pinned profile, procedure and award revisions and compare digests."""
        from src.kb.procurement_ranking import ShortlistService
        return gated(namespace, lambda conn: ShortlistService(conn, initialize=False).replay(
            namespace, shortlist_id, principal_id=who()[0], scopes=who()[1]))

    @mcp.tool()
    def procurement_award_history(namespace: str, buyer: str | None = None, supplier: str | None = None,
                                  cpv: str | None = None, buyer_entity: str | None = None,
                                  supplier_entity: str | None = None) -> dict:
        """Award and modification history per buyer, supplier or CPV with sources and dates (context, never evidence of an open procedure)."""
        from src.kb.procurement_identity import ProcurementIdentityService
        return gated(namespace, lambda conn: {"awards": ProcurementIdentityService(conn, initialize=False).award_history(
            namespace, scopes=who()[1], buyer=buyer, supplier=supplier, cpv=cpv, buyer_entity=buyer_entity,
            supplier_entity=supplier_entity)})

    @mcp.tool()
    def procurement_incumbency(namespace: str, buyer: str, cpv: list[str]) -> dict:
        """Derived, explained incumbency for a buyer and CPV branch from acquired award history."""
        from src.kb.procurement_identity import ProcurementIdentityService
        return gated(namespace, lambda conn: ProcurementIdentityService(conn, initialize=False).incumbency(
            namespace, scopes=who()[1], buyer=buyer, cpv=cpv))

    @mcp.tool()
    def procurement_party_candidates(namespace: str, party: dict) -> dict:
        """Proposed canonical-entity / LEI identities for a buyer or supplier with evidence; never applied automatically."""
        from src.kb.procurement_identity import ProcurementIdentityService
        return gated(namespace, lambda conn: ProcurementIdentityService(conn, initialize=False).candidates(
            namespace, party, scopes=who()[1]))

    @mcp.tool()
    def decide_procurement_party_link(namespace: str, party: dict, entity_id: str, decision: str,
                                      evidence: dict | None = None, note: str | None = None) -> dict:
        """Record a reviewed match/non-match identity decision (auditable, reversible; never a merge)."""
        from src.kb.procurement_identity import ProcurementIdentityService
        return gated(namespace, lambda conn: ProcurementIdentityService(conn).decide(
            namespace, party, entity_id, decision=decision, principal_id=who()[0], scopes=who()[1], evidence=evidence, note=note),
            write=True, scope=REVIEW)

    @mcp.tool()
    def revert_procurement_party_link(namespace: str, link_id: str) -> dict:
        """Undo a party link through an auditable undo decision."""
        from src.kb.procurement_identity import ProcurementIdentityService
        return gated(namespace, lambda conn: ProcurementIdentityService(conn).revert(
            namespace, link_id, principal_id=who()[0], scopes=who()[1]), write=True, scope=REVIEW)

    @mcp.tool()
    def create_procurement_workspace(namespace: str, request_key: str, shortlist_id: str, item_id: str) -> dict:
        """Start bid preparation for one shortlisted lot: cited requirement checklist, document list and milestones."""
        from src.kb.procurement_workspaces import ProcurementWorkspaceStore
        return gated(namespace, lambda conn: ProcurementWorkspaceStore(conn).create(
            namespace, request_key, shortlist_id=shortlist_id, item_id=item_id, principal_id=who()[0], scopes=who()[1]),
            write=True, scope=WRITE)

    @mcp.tool()
    def inspect_procurement_workspace(namespace: str, workspace_id: str, revision: int | None = None) -> dict:
        """Checklist progress, documents with source and status, stale items and the lot's current state."""
        from src.kb.procurement_workspaces import ProcurementWorkspaceStore
        return gated(namespace, lambda conn: ProcurementWorkspaceStore(conn, initialize=False).inspect(
            namespace, workspace_id, revision=revision, principal_id=who()[0], scopes=who()[1]))

    @mcp.tool()
    def update_procurement_workspace_items(namespace: str, workspace_id: str, command_key: str, expected_revision: int,
                                           updates: dict) -> dict:
        """Owner-reviewed checklist status updates."""
        from src.kb.procurement_workspaces import ProcurementWorkspaceStore
        return gated(namespace, lambda conn: ProcurementWorkspaceStore(conn, initialize=False).update_items(
            namespace, workspace_id, command_key, expected_revision, updates, principal_id=who()[0], scopes=who()[1]),
            write=True, scope=WRITE)

    @mcp.tool()
    def refresh_procurement_workspace(namespace: str, workspace_id: str, command_key: str, expected_revision: int) -> dict:
        """Adopt the current notice revision, keep preparation and flag changed requirements stale."""
        from src.kb.procurement_workspaces import ProcurementWorkspaceStore
        return gated(namespace, lambda conn: ProcurementWorkspaceStore(conn, initialize=False).refresh(
            namespace, workspace_id, command_key, expected_revision, principal_id=who()[0], scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def record_procurement_workspace_outcome(namespace: str, workspace_id: str, command_key: str, expected_revision: int,
                                             outcome: str, evidence: dict | None = None) -> dict:
        """Record prepared / user-reported submitted / buyer-confirmed. Never submits anything."""
        from src.kb.procurement_workspaces import ProcurementWorkspaceStore
        return gated(namespace, lambda conn: ProcurementWorkspaceStore(conn, initialize=False).record_outcome(
            namespace, workspace_id, command_key, expected_revision, outcome, evidence=evidence, principal_id=who()[0],
            scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def draft_procurement_bid(namespace: str, workspace_id: str, request_key: str) -> dict:
        """Editable cited bid draft pinned to the notice revision; gaps stay explicitly unanswered."""
        from src.kb.procurement_workspaces import ProcurementBidDraftService
        return gated(namespace, lambda conn: ProcurementBidDraftService(conn).generate(
            namespace, workspace_id, request_key, principal_id=who()[0], scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def export_procurement_bid_draft(namespace: str, draft_id: str, revision: int | None = None) -> dict:
        """Export a bid draft revision after re-checking current access; includes edit provenance."""
        from src.kb.procurement_workspaces import ProcurementBidDraftService
        return gated(namespace, lambda conn: ProcurementBidDraftService(conn, initialize=False).export(
            namespace, draft_id, revision=revision, principal_id=who()[0], scopes=who()[1]))

    @mcp.tool()
    def create_procurement_monitor(namespace: str, profile_id: str, request_key: str, providers: list[str],
                                   watch: dict | None = None, timezone: str | None = None, local_time: str = "08:00",
                                   delivery: dict | None = None) -> dict:
        """Watch new notices, corrigenda, deadline changes, cancellations and awards via a knowledge subscription."""
        from src.kb.procurement_monitoring import ProcurementMonitor
        return gated(namespace, lambda conn: ProcurementMonitor(conn).create(
            namespace, profile_id, request_key, providers=providers, watch=watch, principal_id=who()[0], scopes=who()[1],
            timezone=timezone, local_time=local_time, delivery=delivery), write=True, scope=WRITE)

    @mcp.tool()
    def run_procurement_monitor(namespace: str, subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a monitor at a committed procurement source-pack watermark; replaying a watermark creates no new events."""
        from src.kb.procurement_monitoring import ProcurementMonitor
        return gated(namespace, lambda conn: ProcurementMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def poll_procurement_monitor(namespace: str, subscription_id: str, cursor: str = "") -> dict:
        """Poll monitor events after a cursor."""
        from src.kb.procurement_monitoring import ProcurementMonitor
        return gated(namespace, lambda conn: ProcurementMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))
