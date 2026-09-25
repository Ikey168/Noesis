"""Funding & Grants entry points: discovery, profile-to-shortlist, preparation, monitoring.

Every tool checks the namespace's bundle enablement first; shared providers and
other workflows are not gated by it. No tool submits an application or
contacts a funder.
"""

from src.kb.funding_bundle import BUNDLE, readiness, require_enabled, set_enabled

FUNDING_WRITES = {
    "set_funding_bundle_enabled", "acquire_funding_source",
    "create_funding_profile", "update_funding_profile", "withdraw_funding_profile",
    "assess_funding_eligibility", "propose_funding_rule_interpretation",
    "review_funding_rule_interpretation", "build_funding_shortlist",
    "create_funding_workspace", "update_funding_workspace_items",
    "refresh_funding_workspace", "record_funding_workspace_outcome",
    "draft_funding_application", "create_funding_monitor", "run_funding_monitor",
}
FUNDING_TOOLS = FUNDING_WRITES | {
    "funding_bundle_status", "funding_provider_contracts", "list_funding_opportunities",
    "inspect_funding_opportunity", "funding_opportunity_history", "inspect_funding_profile",
    "inspect_funding_shortlist", "inspect_funding_workspace",
    "export_funding_application_draft", "poll_funding_monitor",
}
FUNDING_SCOPES = {
    "set_funding_bundle_enabled": ["operator"],
    "acquire_funding_source": ["knowledge:funding:write", "knowledge:ingestion:execute"],
    "review_funding_rule_interpretation": ["knowledge:funding:review"],
    "build_funding_shortlist": ["knowledge:funding:write"],
    "create_funding_workspace": ["knowledge:funding:write", "knowledge:projects:write"],
    "refresh_funding_workspace": ["knowledge:funding:write", "knowledge:projects:write"],
    "draft_funding_application": ["knowledge:funding:write", "knowledge:reports:write"],
    "export_funding_application_draft": ["knowledge:funding:read", "knowledge:reports:read"],
    "create_funding_monitor": ["knowledge:funding:write", "knowledge:subscriptions:write"],
    "run_funding_monitor": ["knowledge:funding:write", "knowledge:subscriptions:write"],
    "poll_funding_monitor": ["knowledge:funding:read", "knowledge:subscriptions:read"],
}
_SELECTIONS = {
    "nlnet_calls": ("nlnet", ()),
    "nlnet_fund": ("nlnet", ("url",)),
    "eu_search": ("eu-ft", ("text",)),
    "eu_topic": ("eu-ft", ("identifier",)),
    "foerderdatenbank_programme": ("foerderdatenbank", ("url",)),
    "exist_programme": ("exist", ("url",)),
}


def required_scopes(tool_name, mutability):
    if tool_name in {"funding_provider_contracts"}:
        return []
    return FUNDING_SCOPES.get(tool_name, ["knowledge:funding:write" if mutability == "write" else "knowledge:funding:read"])


def register(mcp, safe, context):
    def gated(namespace, operation, *, write=False, scope="knowledge:funding:read"):
        def run(conn):
            require_enabled(conn, namespace)
            return operation(conn)
        return safe(run, write=write, required_scope=scope)

    def who():
        return context()[0], context()[1]

    @mcp.tool()
    def funding_bundle_status(namespace: str) -> dict:
        """Declared contributions plus actual ready/fixture-only/unavailable state per provider and entry point."""
        return safe(lambda conn: {**readiness(conn, namespace, scopes=who()[1]), "declaration": BUNDLE},
                    required_scope="knowledge:funding:read")

    @mcp.tool()
    def set_funding_bundle_enabled(namespace: str, enabled: bool) -> dict:
        """Enable/disable Funding & Grants for a namespace without affecting shared providers or Research."""
        return safe(lambda conn: set_enabled(conn, namespace, enabled, principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="operator")

    @mcp.tool()
    def funding_provider_contracts() -> dict:
        """Per-provider access contracts, coverage limits and live-verification state."""
        from src.ingestion.funding_providers import LIVE_VERIFICATION, PROVIDER_CONTRACTS
        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION}

    @mcp.tool()
    def acquire_funding_source(namespace: str, selection: dict, observation: str, budget_id: str,
                               reuse_notice: str, max_requests: int = 10) -> dict:
        """Bounded live acquisition of one explicitly selected provider page, topic or search."""
        def run(conn):
            from src.ingestion.funding_providers import PROVIDER_HOSTS, FundingClient, acquire
            from src.ingestion.provider_execution import DurableHTTP

            kind = selection.get("kind")
            if kind not in _SELECTIONS:
                raise ValueError("selection.kind must be one of " + ", ".join(sorted(_SELECTIONS)))
            provider, fields = _SELECTIONS[kind]
            principal, scopes = who()
            http = DurableHTTP(conn, budget_id=budget_id, provider=provider, principal_id=principal,
                               allowed_hosts=PROVIDER_HOSTS[provider], reuse_notice=reuse_notice,
                               max_requests=max_requests)
            client = FundingClient(http, principal_id=principal)
            args = [selection[f] for f in fields if f != "text"]
            method = {"eu_search": "eu_search_all"}.get(kind, kind)
            kwargs = {"text": selection["text"]} if kind == "eu_search" else {}
            return acquire(client, provider, lambda: getattr(client, method)(*args, observation, **kwargs),
                           namespace=namespace, scopes=scopes, reuse_notice=reuse_notice, observation=observation)
        return gated(namespace, run, write=True, scope="knowledge:funding:write")

    @mcp.tool()
    def list_funding_opportunities(namespace: str, providers: list[str] | None = None,
                                   kinds: list[str] | None = None) -> dict:
        """Current opportunities with derived status, freshness, conflicts and links."""
        from src.kb.funding_opportunities import FundingOpportunityStore
        return gated(namespace, lambda conn: {"opportunities": FundingOpportunityStore(conn, initialize=False).list(
            namespace, scopes=who()[1], providers=providers, kinds=kinds)})

    @mcp.tool()
    def inspect_funding_opportunity(namespace: str, opportunity_id: str, revision: int | None = None) -> dict:
        """One opportunity revision with cited requirements, deadlines and financial terms."""
        from src.kb.funding_opportunities import FundingOpportunityStore
        return gated(namespace, lambda conn: FundingOpportunityStore(conn, initialize=False).get(
            namespace, opportunity_id, scopes=who()[1], revision=revision))

    @mcp.tool()
    def funding_opportunity_history(namespace: str, opportunity_id: str) -> dict:
        """Revision history with classified amendments."""
        from src.kb.funding_opportunities import FundingOpportunityStore
        return gated(namespace, lambda conn: {"revisions": FundingOpportunityStore(conn, initialize=False).history(
            namespace, opportunity_id, scopes=who()[1])})

    @mcp.tool()
    def create_funding_profile(namespace: str, request_key: str, label: str) -> dict:
        """Create a private, owner-scoped applicant/project profile with no default facts."""
        from src.kb.funding_profiles import FundingProfileStore
        return gated(namespace, lambda conn: FundingProfileStore(conn).create(
            namespace, request_key, label=label, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:funding:write")

    @mcp.tool()
    def update_funding_profile(namespace: str, profile_id: str, command_key: str, expected_revision: int,
                               set_facts: dict | None = None, propose_facts: dict | None = None,
                               clear_facts: list[str] | None = None, review: list[str] | None = None,
                               reject: list[str] | None = None) -> dict:
        """Owner-reviewed fact changes; proposals stay unknown until reviewed."""
        from src.kb.funding_profiles import FundingProfileStore
        return gated(namespace, lambda conn: FundingProfileStore(conn).update(
            namespace, profile_id, command_key, expected_revision, set_facts=set_facts,
            propose_facts=propose_facts, clear_facts=clear_facts, review=review, reject=reject,
            principal_id=who()[0], scopes=who()[1]), write=True, scope="knowledge:funding:write")

    @mcp.tool()
    def inspect_funding_profile(namespace: str, profile_id: str, revision: int | None = None) -> dict:
        """Owner-only profile view with unknown and unreviewed facts listed."""
        from src.kb.funding_profiles import FundingProfileStore
        return gated(namespace, lambda conn: FundingProfileStore(conn, initialize=False).inspect(
            namespace, profile_id, revision=revision, principal_id=who()[0], scopes=who()[1]))

    @mcp.tool()
    def withdraw_funding_profile(namespace: str, profile_id: str) -> dict:
        """Withdraw a profile; it and every view pinned to it become unreadable."""
        from src.kb.funding_profiles import FundingProfileStore
        return gated(namespace, lambda conn: FundingProfileStore(conn, initialize=False).withdraw(
            namespace, profile_id, principal_id=who()[0], scopes=who()[1]), write=True, scope="knowledge:funding:write")

    @mcp.tool()
    def assess_funding_eligibility(namespace: str, profile_id: str, opportunity_id: str) -> dict:
        """Requirements assessment with rule citations, unknowns and fact dependencies (not a funder decision)."""
        from src.kb.funding_eligibility import EligibilityService
        return gated(namespace, lambda conn: EligibilityService(conn).assess(
            namespace, profile_id, opportunity_id, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:funding:write")

    @mcp.tool()
    def propose_funding_rule_interpretation(namespace: str, opportunity_id: str, requirement_id: str,
                                            rule: dict, note: str) -> dict:
        """Propose a versioned machine rule for a free-text requirement; applies only after review."""
        from src.kb.funding_eligibility import InterpretationStore
        from src.kb.funding_opportunities import FundingOpportunityStore

        def run(conn):
            opportunity = FundingOpportunityStore(conn, initialize=False).get(namespace, opportunity_id, scopes=who()[1])
            return InterpretationStore(conn).propose(namespace, opportunity, requirement_id, rule, note=note,
                                                     principal_id=who()[0], scopes=who()[1])
        return gated(namespace, run, write=True, scope="knowledge:funding:write")

    @mcp.tool()
    def review_funding_rule_interpretation(namespace: str, interpretation_id: str, revision: int, approve: bool) -> dict:
        """Approve or reject another principal's interpretation."""
        from src.kb.funding_eligibility import InterpretationStore
        return gated(namespace, lambda conn: InterpretationStore(conn).review(
            namespace, interpretation_id, revision, approve=approve, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:funding:review")

    @mcp.tool()
    def build_funding_shortlist(namespace: str, profile_id: str, weights: dict | None = None,
                                providers: list[str] | None = None) -> dict:
        """Explained shortlist: buckets, match score (not a probability), unknowns and sensitivity."""
        from src.kb.funding_ranking import ShortlistService
        return gated(namespace, lambda conn: ShortlistService(conn).build(
            namespace, profile_id, principal_id=who()[0], scopes=who()[1], weights=weights, providers=providers),
            write=True, scope="knowledge:funding:write")

    @mcp.tool()
    def inspect_funding_shortlist(namespace: str, shortlist_id: str) -> dict:
        """A stored shortlist and whether call amendments have invalidated it."""
        from src.kb.funding_ranking import ShortlistService
        return gated(namespace, lambda conn: ShortlistService(conn, initialize=False).inspect(
            namespace, shortlist_id, principal_id=who()[0], scopes=who()[1]))

    @mcp.tool()
    def create_funding_workspace(namespace: str, request_key: str, shortlist_id: str, opportunity_id: str) -> dict:
        """Start application preparation as a research project with a requirement checklist."""
        from src.kb.funding_workspaces import FundingWorkspaceStore
        return gated(namespace, lambda conn: FundingWorkspaceStore(conn).create(
            namespace, request_key, shortlist_id=shortlist_id, opportunity_id=opportunity_id,
            principal_id=who()[0], scopes=who()[1]), write=True, scope="knowledge:funding:write")

    @mcp.tool()
    def inspect_funding_workspace(namespace: str, workspace_id: str, revision: int | None = None) -> dict:
        """Checklist progress, stale items and the call's current status."""
        from src.kb.funding_workspaces import FundingWorkspaceStore
        return gated(namespace, lambda conn: FundingWorkspaceStore(conn, initialize=False).inspect(
            namespace, workspace_id, revision=revision, principal_id=who()[0], scopes=who()[1]))

    @mcp.tool()
    def update_funding_workspace_items(namespace: str, workspace_id: str, command_key: str,
                                       expected_revision: int, updates: dict) -> dict:
        """Owner-reviewed checklist status updates."""
        from src.kb.funding_workspaces import FundingWorkspaceStore
        return gated(namespace, lambda conn: FundingWorkspaceStore(conn, initialize=False).update_items(
            namespace, workspace_id, command_key, expected_revision, updates, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:funding:write")

    @mcp.tool()
    def refresh_funding_workspace(namespace: str, workspace_id: str, command_key: str, expected_revision: int) -> dict:
        """Adopt the current call revision, keep preparation and flag changed requirements."""
        from src.kb.funding_workspaces import FundingWorkspaceStore
        return gated(namespace, lambda conn: FundingWorkspaceStore(conn, initialize=False).refresh(
            namespace, workspace_id, command_key, expected_revision, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:funding:write")

    @mcp.tool()
    def record_funding_workspace_outcome(namespace: str, workspace_id: str, command_key: str,
                                         expected_revision: int, outcome: str, evidence: dict | None = None) -> dict:
        """Record prepared / user-reported submitted / provider-confirmed. Never submits anything."""
        from src.kb.funding_workspaces import FundingWorkspaceStore
        return gated(namespace, lambda conn: FundingWorkspaceStore(conn, initialize=False).record_outcome(
            namespace, workspace_id, command_key, expected_revision, outcome, evidence=evidence,
            principal_id=who()[0], scopes=who()[1]), write=True, scope="knowledge:funding:write")

    @mcp.tool()
    def draft_funding_application(namespace: str, workspace_id: str, request_key: str,
                                  questions: list[dict] | None = None) -> dict:
        """Editable cited draft from owner-reviewed facts; gaps stay explicitly unanswered."""
        from src.kb.funding_workspaces import FundingDraftService
        return gated(namespace, lambda conn: FundingDraftService(conn).generate(
            namespace, workspace_id, request_key, principal_id=who()[0], scopes=who()[1], questions=questions),
            write=True, scope="knowledge:funding:write")

    @mcp.tool()
    def export_funding_application_draft(namespace: str, draft_id: str, revision: int | None = None) -> dict:
        """Export a draft revision after re-checking current access; includes edit provenance."""
        from src.kb.funding_workspaces import FundingDraftService
        return gated(namespace, lambda conn: FundingDraftService(conn, initialize=False).export(
            namespace, draft_id, revision=revision, principal_id=who()[0], scopes=who()[1]))

    @mcp.tool()
    def create_funding_monitor(namespace: str, profile_id: str, request_key: str, providers: list[str],
                               timezone: str | None = None, local_time: str = "08:00",
                               delivery: dict | None = None) -> dict:
        """Watch new calls, rule/eligibility changes and deadlines via a knowledge subscription."""
        from src.kb.funding_monitoring import FundingMonitor
        return gated(namespace, lambda conn: FundingMonitor(conn).create(
            namespace, profile_id, request_key, providers=providers, principal_id=who()[0], scopes=who()[1],
            timezone=timezone, local_time=local_time, delivery=delivery), write=True, scope="knowledge:funding:write")

    @mcp.tool()
    def run_funding_monitor(namespace: str, subscription_id: str, watermark: int) -> dict:
        """Evaluate a monitor at a committed watermark; replaying a watermark creates no new events."""
        from src.kb.funding_monitoring import FundingMonitor
        return gated(namespace, lambda conn: FundingMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True, scope="knowledge:funding:write")

    @mcp.tool()
    def poll_funding_monitor(namespace: str, subscription_id: str, cursor: str = "") -> dict:
        """Poll monitor events after a cursor."""
        from src.kb.funding_monitoring import FundingMonitor
        return gated(namespace, lambda conn: FundingMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))
