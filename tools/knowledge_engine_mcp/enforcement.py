"""Legal regulatory enforcement entry points (#2651): actions against an entity, by authority or legal basis.

Registered through :mod:`tools.knowledge_engine_mcp.legal` (the Legal pack's
tools); acquisition runs through the shared source-pack tools (pack
``legal-research`` 1.5.0: ``sec-litigation-releases``,
``sec-administrative-proceedings``, ``fca-final-notices``, ``epa-echo-cases``,
``edpb-art60-decisions``). Coverage is four optional Legal features, one per
regulator: ``enforcement-sec``, ``enforcement-fca``, ``enforcement-epa`` and
``enforcement-edpb``.

Exclusions: no risk or compliance scoring, no inference of wrongdoing from an
initiated action, no merging of a settled "neither admit nor deny" outcome
into a finding, no summing of penalties across currencies or authorities and
no profiling of named individuals (natural persons are pseudonymised and are
never a query key or monitor target); no legal advice.
"""

ENFORCEMENT_WRITES = {
    "propose_enforcement_respondent_matches",
    "review_enforcement_respondent_match",
    "revert_enforcement_respondent_match",
    "link_enforcement_records",
    "create_enforcement_monitor",
    "run_enforcement_monitor",
}
ENFORCEMENT_READS = {
    "enforcement_source_contracts",
    "enforcement_readiness",
    "enforcement_actions_for_entity",
    "enforcement_actions_for_identifier",
    "enforcement_actions_by_authority",
    "enforcement_action_history",
    "list_enforcement_identity_candidates",
    "list_enforcement_links",
    "export_enforcement_evidence_bundle",
    "poll_enforcement_monitor",
}
ENFORCEMENT_TOOLS = ENFORCEMENT_WRITES | ENFORCEMENT_READS
READ = "knowledge:legal:read"
WRITE = "knowledge:legal:write"
OWNERSHIP_READ = "knowledge:ownership:read"
# Every scope each tool always reads or writes.
ENFORCEMENT_SCOPES = {
    "enforcement_source_contracts": [],
    "enforcement_readiness": [READ],
    "enforcement_actions_for_entity": [READ, OWNERSHIP_READ],
    "enforcement_actions_for_identifier": [READ],
    "enforcement_actions_by_authority": [READ],
    "enforcement_action_history": [READ],
    "list_enforcement_identity_candidates": [READ, OWNERSHIP_READ],
    "list_enforcement_links": [READ],
    "export_enforcement_evidence_bundle": [READ],
    "poll_enforcement_monitor": [READ, "knowledge:subscriptions:read"],
    "propose_enforcement_respondent_matches": [READ, OWNERSHIP_READ, "knowledge:ownership:write"],
    "review_enforcement_respondent_match": [OWNERSHIP_READ, "knowledge:ownership:review"],
    "revert_enforcement_respondent_match": [OWNERSHIP_READ, "knowledge:ownership:review"],
    "link_enforcement_records": [READ, WRITE],
    "create_enforcement_monitor": [READ, "knowledge:subscriptions:write"],
    "run_enforcement_monitor": [READ, "knowledge:subscriptions:write"],
}
EXCLUSIONS = ("no risk or compliance score, no inference of wrongdoing from an initiated action, settled outcomes "
              "keep their admission wording, penalties never summed, natural persons pseudonymised, no legal advice")


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
        """Access decisions, licences, rate limits, revision models, the natural-person minimisation decision,
        declined sources, bounded coverage and LIVE_VERIFICATION for SEC, FCA, EPA ECHO and EDPB enforcement
        sources. No risk or compliance scoring and no legal advice."""
        from src.ingestion.enforcement_sources import (
            BOUNDED_COVERAGE,
            DECLINED,
            IDENTIFIERS,
            LIVE_VERIFICATION,
            MINIMISATION,
            PROVIDER_CONTRACTS,
            REVIEW_BOUNDARY,
        )

        return {"contracts": PROVIDER_CONTRACTS, "declined": DECLINED, "identifiers": IDENTIFIERS,
                "bounded_coverage": BOUNDED_COVERAGE, "minimisation": MINIMISATION,
                "live_verification": LIVE_VERIFICATION, "review_boundary": REVIEW_BOUNDARY}

    @mcp.tool()
    def enforcement_readiness(namespace: str = "enforcement") -> dict:
        """Which enforcement features (SEC, FCA, EPA, EDPB) are selected, what each provider has acquired, and how
        ownership, market, court and Legal links degrade when those packs are absent."""
        from src.kb.enforcement import readiness

        return safe(lambda conn: readiness(conn, namespace), required_scope=READ)

    @mcp.tool()
    def enforcement_actions_for_entity(namespace: str, entity: str, ownership_namespace: str,
                                       as_of: str | None = None, group: bool = False,
                                       include_unmatched: bool = False) -> dict:
        """Enforcement actions against a company (or its group through accepted ownership matches, which the
        answer lists) as of a date: outcomes as published - a settlement 'without admitting or denying' keeps that
        wording - with decisions, penalties and appeals published by the date, each action revision cited.
        'no_action_on_record' is never a clean bill. No risk or compliance score, no inference of wrongdoing, no
        legal advice; natural persons are never a query key."""
        from src.kb.enforcement_queries import actions_for_entity

        return safe(lambda conn: actions_for_entity(
            conn, namespace, entity, ownership_namespace=ownership_namespace, scopes=who()[1], as_of=as_of,
            group=group, principal_id=who()[0], include_unmatched=include_unmatched), required_scope=READ)

    @mcp.tool()
    def enforcement_actions_for_identifier(namespace: str, scheme: str, value: str, as_of: str | None = None) -> dict:
        """Enforcement actions whose respondents carry an identifier the regulator published (sec-cik, fca-frn,
        lei); a lookup of published text for deployments without the ownership store, not an identity match. No
        risk or compliance score and no legal advice."""
        from src.kb.enforcement_queries import actions_for_identifier

        return safe(lambda conn: actions_for_identifier(conn, namespace, scheme, value, scopes=who()[1], as_of=as_of),
                    required_scope=READ)

    @mcp.tool()
    def enforcement_actions_by_authority(namespace: str, authority: str | None = None, legal_basis: str | None = None,
                                         date_from: str | None = None, date_to: str | None = None) -> dict:
        """Actions of an authority (us-sec, uk-fca, us-epa, eu-sa-xx) and/or citing a legal basis over a period, with
        outcomes and penalties as published; penalties are listed per authority and currency and never summed;
        undisclosed figures stay explicit unknowns. No ranking, risk or compliance score and no legal advice."""
        from src.kb.enforcement_queries import actions_by_authority

        return safe(lambda conn: actions_by_authority(conn, namespace, scopes=who()[1], authority=authority,
                                                      legal_basis=legal_basis, date_from=date_from, date_to=date_to),
                    required_scope=READ)

    @mcp.tool()
    def enforcement_action_history(namespace: str, action_key: str, as_of: str | None = None,
                                   known_at_ms: int | None = None) -> dict:
        """One action as published by a date (or as recorded at a record time) with every revision of it and of its
        decisions, penalties, appeals and notice documents (corrections and removals by the source are revisions)
        and its links. No legal characterisation."""
        from src.kb.enforcement_queries import action_history

        return safe(lambda conn: action_history(conn, namespace, action_key, scopes=who()[1], as_of=as_of,
                                                known_at_ms=known_at_ms), required_scope=READ)

    @mcp.tool()
    def list_enforcement_identity_candidates(namespace: str, subject_key: str | None = None) -> dict:
        """Reviewable identity candidates of organisation respondents (method, evidence, confidence, state) and the
        respondents kept unmatched as published; natural persons are never candidates."""
        def run(conn):
            item = identity(conn)
            return {"candidates": item.candidates(namespace, scopes=who()[1], subject_key=subject_key),
                    "unmatched": item.unmatched(namespace, scopes=who()[1])}
        return safe(run, required_scope=READ)

    @mcp.tool()
    def propose_enforcement_respondent_matches(namespace: str, ownership_namespace: str) -> dict:
        """Propose organisation respondents against ownership entities: published identifiers (CIK, LEI, company
        numbers) first, names as low evidence; nothing is accepted automatically or merged."""
        return safe(lambda conn: identity(conn, True).propose(namespace, ownership_namespace=ownership_namespace,
                                                              principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="knowledge:ownership:write")

    @mcp.tool()
    def review_enforcement_respondent_match(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a respondent identity candidate with a reason (an entity identity decision with reviewer
        and time; records are never merged)."""
        return safe(lambda conn: identity(conn, True).review(namespace, candidate_id, decision, reason,
                                                             principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def revert_enforcement_respondent_match(namespace: str, candidate_id: str, reason: str) -> dict:
        """Revert a reviewed respondent identity decision."""
        return safe(lambda conn: identity(conn, True).revert(namespace, candidate_id, reason,
                                                             principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def link_enforcement_records(namespace: str, legal_namespace: str = "global",
                                 competition_namespace: str = "competition",
                                 ownership_namespace: str | None = None) -> dict:
        """Link actions to cited legislation and rules, competition cases, court dockets of related cases and
        appeals, Market issuers and filings by published CIK, and ownership entities by accepted match; each link
        records its basis and revisions; missing providers and targets are reported. No causal inference."""
        def run(conn):
            from src.kb.enforcement_links import EnforcementLinks

            return EnforcementLinks(conn).link(namespace, legal_namespace=legal_namespace,
                                               competition_namespace=competition_namespace,
                                               ownership_namespace=ownership_namespace, scopes=who()[1])
        return safe(run, write=True, required_scope=WRITE)

    @mcp.tool()
    def list_enforcement_links(namespace: str, status: str | None = None, citing_record_key: str | None = None,
                               target_kind: str | None = None) -> dict:
        """Enforcement links with their basis (citation, shared identifier, accepted match) and revisions; links not
        resolved (unresolved, ambiguous, provider_unavailable) are listed, never dropped."""
        def run(conn):
            from src.kb.enforcement_links import EnforcementLinks

            return EnforcementLinks(conn, initialize=False).list_links(
                namespace, scopes=who()[1], status=status, citing_record_key=citing_record_key,
                target_kind=target_kind)
        return safe(run, required_scope=READ)

    @mcp.tool()
    def export_enforcement_evidence_bundle(namespace: str, entity: str | None = None,
                                           ownership_namespace: str | None = None, authority: str | None = None,
                                           legal_basis: str | None = None, as_of: str | None = None,
                                           group: bool = False, date_from: str | None = None,
                                           date_to: str | None = None) -> dict:
        """A noesis-evidence-bundle-v1 of an entity answer (entity + ownership_namespace) or an authority/basis
        answer, citing every action, decision, penalty, appeal and respondent revision with source, record revision
        and as-of time; natural persons appear as pseudonyms only."""
        from src.kb.enforcement_queries import (
            actions_by_authority,
            actions_for_entity,
            export_bundle,
        )

        def run(conn):
            if entity:
                answer = actions_for_entity(conn, namespace, entity, ownership_namespace=ownership_namespace or "",
                                            scopes=who()[1], as_of=as_of, group=group, principal_id=who()[0])
            else:
                answer = actions_by_authority(conn, namespace, scopes=who()[1], authority=authority,
                                              legal_basis=legal_basis, date_from=date_from, date_to=date_to)
            return {"answer_status": answer["status"], "bundle": export_bundle(answer)}
        return safe(run, required_scope=READ)

    @mcp.tool()
    def create_enforcement_monitor(namespace: str, request_key: str, watch: str, key: str,
                                   ownership_namespace: str | None = None, group: bool = False) -> dict:
        """Subscribe to an entity (optionally its group), a published identifier (scheme:value), an authority, a
        legal basis or one action for new actions, decisions, penalties, appeals and corrections; a natural person
        is never a monitor target."""
        return safe(lambda conn: monitor(conn, True).create(
            namespace, request_key, watch=watch, key=key, principal_id=who()[0], scopes=who()[1],
            ownership_namespace=ownership_namespace, group=group),
            write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def run_enforcement_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate an enforcement monitor at a committed watermark; each notification cites its before and after
        revision and states which published fields changed; notices are record changes, not assessments."""
        return safe(lambda conn: monitor(conn, True).run(subscription_id, watermark, principal_id=who()[0],
                                                         scopes=who()[1]),
                    write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def poll_enforcement_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll an enforcement monitor's delivered events."""
        return safe(lambda conn: monitor(conn).poll(subscription_id, principal_id=who()[0], scopes=who()[1],
                                                    cursor=cursor), required_scope="knowledge:subscriptions:read")
