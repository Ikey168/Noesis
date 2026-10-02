"""Legal regulatory enforcement entry points (#2651): actions for a company or its group, by authority and basis.

Registered through :mod:`tools.knowledge_engine_mcp.legal` (the Legal pack's
tools); acquisition runs through the shared source-pack tools with the
``legal-research`` 1.5.0 enforcement sources (``sec-enforcement-releases``,
``fca-final-notices``, ``epa-echo-enforcement-cases``,
``edpb-art60-final-decisions``). Coverage is four optional Legal features
(``enforcement-sec``, ``enforcement-fca``, ``enforcement-epa``,
``enforcement-edpb``; default off) of the provider ``legal.enforcement``.

Exclusions: no risk or compliance scoring, no inference of wrongdoing from an
initiated action, no merging of settled "neither admit nor deny" outcomes into
findings, no profiling of named individuals and no legal advice. Every output
passes the EN01 minimisation guard: individuals are counted, never named, and
an answer carrying a score or personal attribute is refused.
"""

ENFORCEMENT_WRITES = {
    "propose_enforcement_identity_matches",
    "review_enforcement_identity_match",
    "revert_enforcement_identity_match",
    "link_enforcement_records",
    "create_enforcement_monitor",
    "run_enforcement_monitor",
}
ENFORCEMENT_READS = {
    "enforcement_source_contracts",
    "enforcement_readiness",
    "enforcement_actions_for_entity",
    "enforcement_actions_by_authority",
    "enforcement_action_history",
    "export_enforcement_evidence_bundle",
    "list_enforcement_identity_candidates",
    "list_enforcement_links",
    "poll_enforcement_monitor",
}
ENFORCEMENT_TOOLS = ENFORCEMENT_WRITES | ENFORCEMENT_READS
READ = "knowledge:legal:read"
WRITE = "knowledge:legal:write"
OWNERSHIP_READ = "knowledge:ownership:read"
# Every scope each tool always reads or writes: enforcement records (Legal), the ownership identity state machine
# (entity answers, identity and links rest on accepted decisions) and subscriptions.
ENFORCEMENT_SCOPES = {
    "enforcement_source_contracts": [],
    "enforcement_readiness": [READ],
    "enforcement_actions_for_entity": [READ, OWNERSHIP_READ],
    "enforcement_actions_by_authority": [READ],
    "enforcement_action_history": [READ],
    "export_enforcement_evidence_bundle": [READ, OWNERSHIP_READ],
    "list_enforcement_identity_candidates": [READ, OWNERSHIP_READ],
    "list_enforcement_links": [READ],
    "poll_enforcement_monitor": [READ, "knowledge:subscriptions:read"],
    "propose_enforcement_identity_matches": [READ, OWNERSHIP_READ, "knowledge:ownership:write"],
    "review_enforcement_identity_match": [OWNERSHIP_READ, "knowledge:ownership:review"],
    "revert_enforcement_identity_match": [OWNERSHIP_READ, "knowledge:ownership:review"],
    "link_enforcement_records": [READ, WRITE, OWNERSHIP_READ],
    "create_enforcement_monitor": [READ, "knowledge:subscriptions:write"],
    "run_enforcement_monitor": [READ, "knowledge:subscriptions:write"],
}


def guarded(result):
    """The EN01 minimisation and exclusion guard every tool output passes; exclusions travel with the answer."""
    from src.kb.enforcement import EXCLUSIONS, EnforcementError, forbidden_keys

    found = forbidden_keys(result)
    if found:
        raise EnforcementError("minimisation_violation", "an enforcement answer may not carry scores, inferred "
                                                         "findings or personal attributes: " + ", ".join(found))
    return {**result, "exclusions": EXCLUSIONS} if isinstance(result, dict) else result


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def identity(conn, write=False):
        from src.kb.enforcement_identity import EnforcementIdentity

        return EnforcementIdentity(conn, initialize=write)

    def monitor(conn, write=False):
        from src.kb.enforcement_monitoring import EnforcementMonitor

        return EnforcementMonitor(conn, initialize=write)

    @mcp.tool()
    def enforcement_source_contracts() -> dict:
        """Access decisions, key handling, reuse terms, rate limits, revision and removal models, the data-
        minimisation decision, declined sources, bounded coverage and LIVE_VERIFICATION for the SEC, FCA, EPA ECHO
        and EDPB Article 60 sources."""
        from src.ingestion.enforcement_sources import (
            BOUNDED_COVERAGE,
            DECLINED,
            IDENTIFIERS,
            LIVE_VERIFICATION,
            MINIMISATION,
            PROVIDER_CONTRACTS,
            REVIEW_BOUNDARY,
        )

        return guarded({"contracts": PROVIDER_CONTRACTS, "minimisation": MINIMISATION, "declined": DECLINED,
                        "identifiers": IDENTIFIERS, "bounded_coverage": BOUNDED_COVERAGE,
                        "live_verification": LIVE_VERIFICATION, "review_boundary": REVIEW_BOUNDARY})

    @mcp.tool()
    def enforcement_readiness(namespace: str = "global") -> dict:
        """Which enforcement features are selected and what each provider has acquired (unverified live stated)."""
        from src.kb.enforcement import readiness

        return safe(lambda conn: guarded(readiness(conn, namespace)), required_scope=READ)

    @mcp.tool()
    def enforcement_actions_for_entity(namespace: str, entity: str, ownership_namespace: str,
                                       as_of: str | None = None, group: bool = False, include_unknowns: bool = False,
                                       include_removed: bool = False, authority: str | None = None) -> dict:
        """Regulatory enforcement actions (SEC, FCA, EPA, EDPB lead authorities) against a company - or its group
        through accepted ownership matches as of the date - with outcomes, settlement admission wording, penalties
        and appeals as published, each citing its record revision and identity decision; 'no_action_on_record' is
        never a clean bill. No risk or compliance scoring, no inferred wrongdoing, no settled outcome merged into a
        finding, no profiling of individuals."""
        from src.kb.enforcement_queries import actions_for_entity

        return safe(lambda conn: guarded(actions_for_entity(
            conn, namespace, entity, ownership_namespace=ownership_namespace, scopes=who()[1], as_of=as_of,
            group=group, include_unknowns=include_unknowns, include_removed=include_removed, principal_id=who()[0],
            authority=authority)), required_scope=READ)

    @mcp.tool()
    def enforcement_actions_by_authority(namespace: str, authority: str | None = None,
                                         legal_basis: str | None = None, date_from: str | None = None,
                                         date_to: str | None = None, include_removed: bool = False) -> dict:
        """Actions of an authority and/or citing a legal basis over a period, with outcomes and penalties as
        published; penalties are never summed across currencies or authorities and undisclosed penalties stay
        explicit. No ranking, scoring or legal advice."""
        from src.kb.enforcement_queries import actions_by_authority

        return safe(lambda conn: guarded(actions_by_authority(
            conn, namespace, scopes=who()[1], authority=authority, legal_basis=legal_basis, date_from=date_from,
            date_to=date_to, include_removed=include_removed)), required_scope=READ)

    @mcp.tool()
    def enforcement_action_history(namespace: str, action_key: str) -> dict:
        """An action's revision chain (corrections and removals by the source included), notice versions,
        organisational respondents, penalties, appeals and links."""
        from src.kb.enforcement_queries import action_history

        return safe(lambda conn: guarded(action_history(conn, namespace, action_key, scopes=who()[1])),
                    required_scope=READ)

    @mcp.tool()
    def export_enforcement_evidence_bundle(namespace: str, ownership_namespace: str | None = None,
                                           entity: str | None = None, authority: str | None = None,
                                           legal_basis: str | None = None, as_of: str | None = None,
                                           group: bool = False) -> dict:
        """An evidence-bundle export of an entity or authority answer: every assertion cites its source, record
        revision and as-of time; exclusions travel with the bundle."""
        from src.kb.enforcement_queries import (
            actions_by_authority,
            actions_for_entity,
            evidence_bundle,
        )

        def run(conn):
            if entity:
                answer = actions_for_entity(conn, namespace, entity, ownership_namespace=ownership_namespace or
                                            "ownership", scopes=who()[1], as_of=as_of, group=group,
                                            principal_id=who()[0])
            else:
                answer = actions_by_authority(conn, namespace, scopes=who()[1], authority=authority,
                                              legal_basis=legal_basis)
            return guarded({"answer": answer, "bundle": evidence_bundle(answer)})
        return safe(run, required_scope=READ)

    @mcp.tool()
    def list_enforcement_identity_candidates(namespace: str, ownership_namespace: str | None = None,
                                             subject_key: str | None = None) -> dict:
        """Reviewable identity candidates of organisational respondents, the unmatched respondents (kept as
        published), the authorities as source identities and, with an ownership namespace, the conflicts."""
        def run(conn):
            item = identity(conn)
            out = {"candidates": item.candidates(namespace, scopes=who()[1], subject_key=subject_key),
                   "unmatched": item.unmatched(namespace, scopes=who()[1]),
                   "authorities": item.authorities(namespace, scopes=who()[1])}
            if ownership_namespace:
                out["conflicts"] = item.conflicts(namespace, ownership_namespace=ownership_namespace,
                                                  scopes=who()[1])
            return guarded(out)
        return safe(run, required_scope=READ)

    @mcp.tool()
    def propose_enforcement_identity_matches(namespace: str, ownership_namespace: str) -> dict:
        """Propose organisational respondents against ownership entities: published identifiers (CIK, FRN, LEI,
        company number) first, names as low evidence; nothing is accepted automatically or merged; individuals are
        never proposed."""
        return safe(lambda conn: guarded(identity(conn, True).propose(
            namespace, ownership_namespace=ownership_namespace, principal_id=who()[0], scopes=who()[1])),
            write=True, required_scope="knowledge:ownership:write")

    @mcp.tool()
    def review_enforcement_identity_match(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a respondent identity candidate with a reason (an entity identity decision with
        reviewer and time; records are never merged)."""
        return safe(lambda conn: guarded(identity(conn, True).review(namespace, candidate_id, decision, reason,
                                                                     principal_id=who()[0], scopes=who()[1])),
                    write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def revert_enforcement_identity_match(namespace: str, candidate_id: str, reason: str) -> dict:
        """Revert a reviewed respondent identity decision."""
        return safe(lambda conn: guarded(identity(conn, True).revert(namespace, candidate_id, reason,
                                                                     principal_id=who()[0], scopes=who()[1])),
                    write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def link_enforcement_records(namespace: str, legal_namespace: str = "global",
                                 competition_namespace: str = "competition", courts_namespace: str = "global",
                                 ownership_namespace: str | None = "ownership") -> dict:
        """Link actions to Legal works, competition cases and court dockets by exact citation, to SEC EDGAR filers by
        a published CIK and to ownership entities through accepted matches; missing providers and targets are
        reported, never dropped; no causal link to market events."""
        def run(conn):
            from src.kb.enforcement_links import EnforcementLinks

            return guarded(EnforcementLinks(conn).link(
                namespace, scopes=who()[1], legal_namespace=legal_namespace,
                competition_namespace=competition_namespace, courts_namespace=courts_namespace,
                ownership_namespace=ownership_namespace))
        return safe(run, write=True, required_scope=WRITE)

    @mcp.tool()
    def list_enforcement_links(namespace: str, status: str | None = None, citing_record_key: str | None = None
                               ) -> dict:
        """Enforcement links with their basis and revisions; unresolved and provider_unavailable links kept."""
        def run(conn):
            from src.kb.enforcement_links import EnforcementLinks

            return guarded(EnforcementLinks(conn, initialize=False).list_links(
                namespace, scopes=who()[1], status=status, citing_record_key=citing_record_key))
        return safe(run, required_scope=READ)

    @mcp.tool()
    def create_enforcement_monitor(namespace: str, request_key: str, watch: str, key: str,
                                   ownership_namespace: str | None = None, group: bool = False) -> dict:
        """Subscribe to an entity (optionally its group), an authority, a legal basis or one action for new actions,
        decisions, penalties, appeals, corrections and removals (record changes, not assessments)."""
        return safe(lambda conn: guarded(monitor(conn, True).create(
            namespace, request_key, watch=watch, key=key, principal_id=who()[0], scopes=who()[1],
            ownership_namespace=ownership_namespace, group=group)),
            write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def run_enforcement_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate an enforcement monitor at a committed watermark; each notice cites its before and after
        revision and states what changed."""
        return safe(lambda conn: guarded(monitor(conn, True).run(subscription_id, watermark, principal_id=who()[0],
                                                                 scopes=who()[1])),
                    write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def poll_enforcement_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll an enforcement monitor's delivered events."""
        return safe(lambda conn: guarded(monitor(conn).poll(subscription_id, principal_id=who()[0],
                                                            scopes=who()[1], cursor=cursor)),
                    required_scope="knowledge:subscriptions:read")
