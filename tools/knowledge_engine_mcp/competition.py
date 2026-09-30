"""Corporate Ownership competition entry points (#2217): cases, stage history and state aid for a company.

Registered through :mod:`tools.knowledge_engine_mcp.ownership` (the Corporate
Ownership pack's tools); acquisition runs through ``acquire_ownership_sources``
with the ``corporate-ownership`` 1.1.0 competition sources
(``ec-competition-cases``, ``eu-state-aid-tam``, ``uk-cma-cases``,
``us-ftc-cases``, ``us-doj-atr-cases``). Coverage is the optional
``competition`` feature of the Corporate Ownership bundle (default off).

Exclusions: no prediction of case outcomes, no assessment of market power or
market definition, no assessment of the compatibility or legality of aid and
no legal advice. Parties reach companies only through reviewed identity
matches; nothing is merged across authorities.
"""

COMPETITION_WRITES = {
    "propose_competition_identity_matches",
    "review_competition_identity_match",
    "revert_competition_identity_match",
    "link_competition_citations",
    "create_competition_monitor",
    "run_competition_monitor",
}
COMPETITION_READS = {
    "competition_source_contracts",
    "competition_readiness",
    "lookup_competition_cases",
    "competition_case_history",
    "state_aid_awards_for_beneficiary",
    "build_competition_dossier",
    "list_competition_identity_candidates",
    "list_competition_citation_links",
    "poll_competition_monitor",
}
COMPETITION_TOOLS = COMPETITION_WRITES | COMPETITION_READS
READ = "knowledge:ownership:read"
WRITE = "knowledge:ownership:write"
REVIEW = "knowledge:ownership:review"
# Every scope each tool always reads or writes.
COMPETITION_SCOPES = {
    "competition_source_contracts": [],
    "competition_readiness": [READ],
    "lookup_competition_cases": [READ],
    "competition_case_history": [READ],
    "state_aid_awards_for_beneficiary": [READ],
    "build_competition_dossier": [READ],
    "list_competition_identity_candidates": [READ],
    "list_competition_citation_links": [READ],
    "poll_competition_monitor": [READ, "knowledge:subscriptions:read"],
    "propose_competition_identity_matches": [READ, WRITE],
    "review_competition_identity_match": [READ, REVIEW],
    "revert_competition_identity_match": [READ, REVIEW],
    "link_competition_citations": [READ, WRITE, "knowledge:legal:read"],
    "create_competition_monitor": [READ, "knowledge:subscriptions:write"],
    "run_competition_monitor": [READ, "knowledge:subscriptions:write"],
}
EXCLUSIONS = ("no outcome prediction, no market-power or aid-compatibility assessment, no legal advice")


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def identity(conn, write=False):
        from src.kb.competition_identity import CompetitionIdentity

        return CompetitionIdentity(conn, initialize=write)

    def monitor(conn, write=False):
        from src.kb.competition_monitoring import CompetitionMonitor

        return CompetitionMonitor(conn, initialize=write)

    @mcp.tool()
    def competition_source_contracts() -> dict:
        """Access decisions, reuse terms, rate limits, instruments in and out of scope, stable identifiers, bounded
        coverage and LIVE_VERIFICATION for the EC case search, TAM, GOV.UK CMA, FTC and DOJ sources."""
        from src.ingestion.competition_sources import (
            BOUNDED_COVERAGE,
            DECLINED,
            IDENTIFIERS,
            INSTRUMENTS,
            LIVE_VERIFICATION,
            PROVIDER_CONTRACTS,
            REVIEW_BOUNDARY,
        )

        return {"contracts": PROVIDER_CONTRACTS, "instruments": INSTRUMENTS, "declined": DECLINED,
                "identifiers": IDENTIFIERS, "bounded_coverage": BOUNDED_COVERAGE,
                "live_verification": LIVE_VERIFICATION, "review_boundary": REVIEW_BOUNDARY}

    @mcp.tool()
    def competition_readiness(namespace: str = "competition") -> dict:
        """Whether the optional competition feature is selected and what each provider has acquired."""
        from src.kb.competition import readiness

        return safe(lambda conn: readiness(conn, namespace), required_scope=READ)

    @mcp.tool()
    def lookup_competition_cases(namespace: str, entity: str, ownership_namespace: str, as_of: str | None = None,
                                 group: bool = False, include_unknowns: bool = False, authority: str | None = None,
                                 instrument: str | None = None) -> dict:
        """Competition cases (merger, antitrust, state aid, market investigation) naming a company - or its group
        through the ownership graph as of the date - with the party role as published, the stage in force at the
        date and the reviewed identity match; authorities side by side; 'no_case_on_record' is never a clean bill.
        No outcome prediction, market-power assessment or legal advice."""
        from src.kb.competition_queries import cases_for_company

        return safe(lambda conn: cases_for_company(
            conn, namespace, entity, ownership_namespace=ownership_namespace, scopes=who()[1], as_of=as_of,
            group=group, include_unknowns=include_unknowns, principal_id=who()[0], authority=authority,
            instrument=instrument), required_scope=READ)

    @mcp.tool()
    def competition_case_history(namespace: str, case_key: str, include_superseded: bool = False) -> dict:
        """A case's dated stage history (as published, with source and revision; superseded revisions on request),
        decision documents (linked, not mirrored), parties as published and exact citation links. No legal
        characterisation or outcome prediction."""
        from src.kb.competition_queries import case_history

        return safe(lambda conn: case_history(conn, namespace, case_key, scopes=who()[1],
                                              include_superseded=include_superseded), required_scope=READ)

    @mcp.tool()
    def state_aid_awards_for_beneficiary(namespace: str, entity: str, ownership_namespace: str,
                                         include_superseded: bool = False) -> dict:
        """State-aid awards received by a company through reviewed matches: granting authority, measure reference,
        amount and currency as published and the SA case where cited; totals are a computed view listing their
        inputs; unknowns explicit. No assessment of the compatibility or legality of aid."""
        from src.kb.competition_queries import awards_for_beneficiary

        return safe(lambda conn: awards_for_beneficiary(
            conn, namespace, entity, ownership_namespace=ownership_namespace, scopes=who()[1],
            include_superseded=include_superseded, principal_id=who()[0]), required_scope=READ)

    @mcp.tool()
    def build_competition_dossier(ownership_namespace: str, scheme: str, value: str, namespace: str = "competition",
                                  as_of: str | None = None, group: bool = False) -> dict:
        """The ownership dossier for an identifier with a competition section: cases and aid awards naming the
        company (or its group), as published and cited. No outcome prediction or legal advice."""
        from src.kb.ownership_bundle import dossier_with_competition

        return safe(lambda conn: dossier_with_competition(
            conn, ownership_namespace, scheme, value, competition_namespace=namespace, principal_id=who()[0],
            scopes=who()[1], as_of=as_of, group=group), required_scope=READ)

    @mcp.tool()
    def list_competition_identity_candidates(namespace: str, ownership_namespace: str | None = None,
                                             subject_key: str | None = None) -> dict:
        """Reviewable identity candidates of case parties and aid beneficiaries, the unmatched subjects (kept as
        published) and, with an ownership namespace, the conflicts (one party, several entities)."""
        def run(conn):
            item = identity(conn)
            out = {"candidates": item.candidates(namespace, scopes=who()[1], subject_key=subject_key),
                   "unmatched": item.unmatched(namespace, scopes=who()[1])}
            if ownership_namespace:
                out["conflicts"] = item.conflicts(namespace, ownership_namespace=ownership_namespace,
                                                  scopes=who()[1])
            return out
        return safe(run, required_scope=READ)

    @mcp.tool()
    def propose_competition_identity_matches(namespace: str, ownership_namespace: str) -> dict:
        """Propose case parties and aid beneficiaries against ownership entities: published identifiers first,
        names as low evidence; nothing is accepted automatically or merged."""
        return safe(lambda conn: identity(conn, True).propose(namespace, ownership_namespace=ownership_namespace,
                                                              principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope=WRITE)

    @mcp.tool()
    def review_competition_identity_match(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a party or beneficiary identity candidate with a reason (an entity identity decision
        with reviewer and time; records are never merged)."""
        return safe(lambda conn: identity(conn, True).review(namespace, candidate_id, decision, reason,
                                                             principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope=REVIEW)

    @mcp.tool()
    def revert_competition_identity_match(namespace: str, candidate_id: str, reason: str) -> dict:
        """Revert a reviewed party or beneficiary identity decision."""
        return safe(lambda conn: identity(conn, True).revert(namespace, candidate_id, reason,
                                                             principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope=REVIEW)

    @mcp.tool()
    def link_competition_citations(namespace: str, legal_namespace: str = "global") -> dict:
        """Link cases, decision documents and aid awards to the legal acts (CELEX/ELI, TFEU articles, UK Acts, US
        Code) and cases (SA measures, explicit cross-authority references) they cite, by exact citation only."""
        def run(conn):
            from src.kb.competition_citations import CompetitionCitations

            return CompetitionCitations(conn).link(namespace, legal_namespace=legal_namespace, scopes=who()[1])
        return safe(run, write=True, required_scope=WRITE)

    @mcp.tool()
    def list_competition_citation_links(namespace: str, status: str | None = None,
                                        citing_record_key: str | None = None) -> dict:
        """Exact citation links with unresolved references kept as source text; never characterised."""
        def run(conn):
            from src.kb.competition_citations import CompetitionCitations

            return CompetitionCitations(conn, initialize=False).list_links(
                namespace, scopes=who()[1], status=status, citing_record_key=citing_record_key)
        return safe(run, required_scope=READ)

    @mcp.tool()
    def create_competition_monitor(namespace: str, request_key: str, watch: str, key: str,
                                   ownership_namespace: str | None = None, group: bool = False,
                                   instrument: str | None = None) -> dict:
        """Subscribe to a company (optionally its group), a case, a beneficiary or an authority/instrument for new
        cases, stage changes, decision documents and new or corrected aid awards."""
        return safe(lambda conn: monitor(conn, True).create(
            namespace, request_key, watch=watch, key=key, principal_id=who()[0], scopes=who()[1],
            ownership_namespace=ownership_namespace, group=group, instrument=instrument),
            write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def run_competition_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a competition monitor at a committed watermark; each notification cites its before and after
        revision."""
        return safe(lambda conn: monitor(conn, True).run(subscription_id, watermark, principal_id=who()[0],
                                                         scopes=who()[1]),
                    write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def poll_competition_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a competition monitor's delivered events."""
        return safe(lambda conn: monitor(conn).poll(subscription_id, principal_id=who()[0], scopes=who()[1],
                                                    cursor=cursor), required_scope="knowledge:subscriptions:read")
