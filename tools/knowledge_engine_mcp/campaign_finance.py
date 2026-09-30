"""Political campaign-finance feature entry points: filings with amendment history, totals as reported, affiliate
donations, contest filings, reviewable identity, links and monitors.

Acquisition runs through the shared source-pack tools (pack
``official-political-records`` 1.4.0: ``us-fec-committees``,
``us-fec-candidates``, ``us-fec-filings``, ``us-fec-schedule-a``,
``us-fec-schedule-b``, ``us-fec-schedule-e``, ``uk-ec-donations``,
``uk-ec-spending``). US coverage is the ``campaign-finance-us`` feature and UK
coverage ``campaign-finance-uk``; elections, lobbying and ownership links
degrade to an ``unavailable`` / ``missing_targets`` report when those providers
are absent. Every answer cites each item with its source, record revision,
filing revision and observation time.

Exclusions: no influence scoring, no "dark money" or undisclosed-funding
inference, and no profiling of individual donors. The CF01 minimisation
decision applies to every output: individual contributions are minimised and
returned only with ``knowledge:political:campaign-finance:individual-items:read``
(otherwise counted); natural-person payees are reduced to their kind.
"""

CAMPAIGN_FINANCE_WRITES = {
    "propose_campaign_finance_identity_matches",
    "review_campaign_finance_identity_match",
    "revert_campaign_finance_identity_match",
    "link_campaign_finance_contests",
    "link_campaign_finance_lobbying",
    "link_campaign_finance_ownership",
    "create_campaign_finance_monitor",
    "run_campaign_finance_monitor",
}
CAMPAIGN_FINANCE_READS = {
    "campaign_finance_source_contracts",
    "campaign_finance_readiness",
    "campaign_finance_totals_as_of",
    "campaign_finance_amendment_chain",
    "campaign_finance_filing_items",
    "campaign_finance_affiliate_donations",
    "campaign_finance_contest_filings",
    "export_campaign_finance_evidence_bundle",
    "list_campaign_finance_identity_candidates",
    "list_campaign_finance_links",
    "poll_campaign_finance_monitor",
}
CAMPAIGN_FINANCE_TOOLS = CAMPAIGN_FINANCE_WRITES | CAMPAIGN_FINANCE_READS
READ = "knowledge:political:campaign-finance:read"
WRITE = "knowledge:political:campaign-finance:write"
INDIVIDUAL = "knowledge:political:campaign-finance:individual-items:read"
OWNERSHIP_READ = "knowledge:ownership:read"
# Every scope each tool always reads or writes: campaign-finance records, the ownership identity state machine
# (affiliate paths, identity views and links rest on accepted decisions), lobbying registers and subscriptions.
# The individual-items scope is conditional: without it individual contributions are counted, never returned.
CAMPAIGN_FINANCE_SCOPES = {
    "campaign_finance_source_contracts": [],
    "campaign_finance_readiness": [READ],
    "campaign_finance_totals_as_of": [READ],
    "campaign_finance_amendment_chain": [READ],
    "campaign_finance_filing_items": [READ],
    "campaign_finance_affiliate_donations": [READ, OWNERSHIP_READ],
    "campaign_finance_contest_filings": [READ],
    "export_campaign_finance_evidence_bundle": [READ, OWNERSHIP_READ],
    "list_campaign_finance_identity_candidates": [READ, OWNERSHIP_READ],
    "list_campaign_finance_links": [READ],
    "poll_campaign_finance_monitor": [READ, "knowledge:subscriptions:read"],
    "propose_campaign_finance_identity_matches": [READ, OWNERSHIP_READ, "knowledge:ownership:write"],
    "review_campaign_finance_identity_match": [OWNERSHIP_READ, "knowledge:ownership:review"],
    "revert_campaign_finance_identity_match": [OWNERSHIP_READ, "knowledge:ownership:review"],
    "link_campaign_finance_contests": [READ, WRITE, OWNERSHIP_READ],
    "link_campaign_finance_lobbying": [READ, WRITE, OWNERSHIP_READ, "knowledge:political:lobbying:read"],
    "link_campaign_finance_ownership": [READ, WRITE, OWNERSHIP_READ],
    "create_campaign_finance_monitor": [READ, "knowledge:subscriptions:write"],
    "run_campaign_finance_monitor": [READ, "knowledge:subscriptions:write"],
}
EXCLUSIONS_NOTE = ("No influence score, no 'dark money' or undisclosed-funding inference and no individual-donor "
                   "profiling; individual donors are minimised (CF01).")


def required_scopes(tool_name, mutability):
    return CAMPAIGN_FINANCE_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def queries(conn):
        from src.kb.campaign_finance_queries import CampaignFinanceQueries

        return CampaignFinanceQueries(conn)

    @mcp.tool()
    def campaign_finance_source_contracts() -> dict:
        """Per-provider endpoints, API-key handling, licences (including the FEC's statutory limit on using
        contributor names), rate limits, amendment models, bounded coverage, LIVE_VERIFICATION status and the donor
        data-minimisation decision for the OpenFEC API, FEC bulk data and the Electoral Commission search."""
        from src.ingestion.campaign_finance_sources import (
            BOUNDED_COVERAGE,
            LIVE_VERIFICATION,
            MINIMISATION,
            PROVIDER_CONTRACTS,
            REVIEW_BOUNDARY,
        )

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "bounded_coverage": BOUNDED_COVERAGE, "minimisation": MINIMISATION,
                "review_boundary": REVIEW_BOUNDARY}

    @mcp.tool()
    def campaign_finance_readiness() -> dict:
        """Whether the campaign-finance-us / campaign-finance-uk features are selected, and records per provider."""
        from src.kb.campaign_finance_records import readiness

        return safe(lambda conn: readiness(conn), required_scope=READ)

    @mcp.tool()
    def campaign_finance_totals_as_of(namespace: str, committee: str, as_of: str | None = None,
                                      period_start: str | None = None, period_end: str | None = None,
                                      derive: bool = False) -> dict:
        """A committee's (FEC id) or regulated entity's (Commission id) reported receipts, disbursements and cash on
        hand per report as of a date, naming the filing version used, its amendment chain and the differences
        between versions. Figures are as reported; derive=true adds a cross-period sum labelled derived with the
        versions used. No filing on record is none_on_record. No influence score or ranking."""
        return safe(lambda conn: queries(conn).reported_totals(
            namespace, committee, scopes=who()[1], as_of=as_of, period_start=period_start, period_end=period_end,
            derive=derive), required_scope=READ)

    @mcp.tool()
    def campaign_finance_amendment_chain(namespace: str, filing: str, as_of: str | None = None) -> dict:
        """Every version of one filing (original, amendments, termination) with receipt dates, the regulator's
        most-recent flag as published and the version available as of a date. filing: a file number or key."""
        from src.kb.campaign_finance_records import CampaignFinanceStore

        def run(conn):
            store = CampaignFinanceStore(conn, initialize=False)
            key = filing if filing.startswith("campaign-finance:") else f"campaign-finance:fec:filing:{filing}"
            head = store.records(namespace, scopes=who()[1], kinds=["filing"], record_keys=[key])
            if not head:
                return {"status": "none_on_record", "filing_key": key}
            return {"status": "answered", **store.filing_chain(namespace, head[0]["filing_group"], scopes=who()[1],
                                                               as_of=as_of)}

        return safe(run, required_scope=READ)

    @mcp.tool()
    def campaign_finance_filing_items(namespace: str, filing: str) -> dict:
        """Line items reported in one filing version, amounts and dates as reported, memo items labelled. Individual
        contributions are minimised and returned only with the individual-items scope, otherwise counted."""
        return safe(lambda conn: queries(conn).filing_items(namespace, filing, scopes=who()[1]), required_scope=READ)

    @mcp.tool()
    def campaign_finance_affiliate_donations(namespace: str, organisation: str, ownership_namespace: str | None = None,
                                             as_of: str | None = None) -> dict:
        """Contributions reported from an organisation and its affiliated entities, reached only through accepted
        identity matches, cited ownership relations and connected organisations stated on registrations; each
        result lists its path and the filing version it was reported in. Individual donors are never expanded; no
        'dark money' inference."""
        return safe(lambda conn: queries(conn).affiliate_donations(
            namespace, organisation, principal_id=who()[0], scopes=who()[1], ownership_namespace=ownership_namespace,
            as_of=as_of), required_scope=READ)

    @mcp.tool()
    def campaign_finance_contest_filings(namespace: str, contest_id: str, elections_namespace: str | None = None
                                         ) -> dict:
        """Candidate-committee filings, party spending returns and independent expenditures (support/oppose as
        reported) linked to an elections contest; none_on_record when nothing is linked. No influence reading."""
        return safe(lambda conn: queries(conn).contest_filings(namespace, contest_id, scopes=who()[1],
                                                               elections_namespace=elections_namespace),
                    required_scope=READ)

    @mcp.tool()
    def export_campaign_finance_evidence_bundle(namespace: str, query: str, key: str, as_of: str | None = None,
                                                ownership_namespace: str | None = None) -> dict:
        """An evidence bundle for a totals, affiliate or contest answer (query: totals | affiliates | contest):
        assertions each citing the record revision, source, filing revision and observation time behind them."""
        def run(conn):
            ask = queries(conn)
            if query == "totals":
                answer = ask.reported_totals(namespace, key, scopes=who()[1], as_of=as_of)
            elif query == "affiliates":
                answer = ask.affiliate_donations(namespace, key, principal_id=who()[0], scopes=who()[1],
                                                 ownership_namespace=ownership_namespace, as_of=as_of)
            elif query == "contest":
                answer = ask.contest_filings(namespace, key, scopes=who()[1])
            else:
                return {"ok": False, "error": {"code": "invalid_request",
                                               "message": "query is totals, affiliates or contest"}}
            return {"status": answer["status"], "query": query, "key": key, "as_of": as_of,
                    "evidence_bundle": ask.evidence_bundle(answer), "exclusions": answer["exclusions"]}

        return safe(run, required_scope=READ)

    @mcp.tool()
    def list_campaign_finance_identity_candidates(namespace: str, record_key: str | None = None) -> dict:
        """Identity candidates and decisions for committees, candidates, parties and organisational donors, with
        method, evidence and confidence. Individual donors are never candidates."""
        from src.kb.campaign_finance_identity import CampaignFinanceIdentity
        from src.kb.campaign_finance_records import authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            identity = CampaignFinanceIdentity(conn, initialize=False)
            return {"candidates": identity.candidates(namespace, scopes=who()[1], record_key=record_key),
                    "unmatched": identity.unmatched(namespace, scopes=who()[1])}

        return safe(run, required_scope=READ)

    @mcp.tool()
    def list_campaign_finance_links(namespace: str, kind: str | None = None, record_key: str | None = None,
                                    target_key: str | None = None) -> dict:
        """Contest, lobbying and ownership links with the record and filing revisions they point at and their
        matching basis. A link is not evidence of influence."""
        from src.kb.campaign_finance_links import CampaignFinanceLinks

        return safe(lambda conn: {"links": CampaignFinanceLinks(conn, initialize=False).links(
            namespace, scopes=who()[1], kind=kind, record_key=record_key, target_key=target_key)},
            required_scope=READ)

    @mcp.tool()
    def propose_campaign_finance_identity_matches(namespace: str, ownership_namespace: str | None = None,
                                                  lobbying_namespace: str | None = None,
                                                  elections_namespace: str | None = None) -> dict:
        """Propose reviewable matches (official FEC ids, company numbers, name+address, name+office+cycle) to
        committees, election records, lobbying registrants and Corporate Ownership entities; nothing is merged or
        accepted automatically, individuals are never proposed and absent providers are reported."""
        from src.kb.campaign_finance_identity import CampaignFinanceIdentity

        return safe(lambda conn: CampaignFinanceIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1], ownership_namespace=ownership_namespace,
            lobbying_namespace=lobbying_namespace, elections_namespace=elections_namespace),
            write=True, required_scope="knowledge:ownership:write")

    @mcp.tool()
    def review_campaign_finance_identity_match(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a campaign-finance identity candidate as an entity identity decision."""
        from src.kb.campaign_finance_identity import CampaignFinanceIdentity

        return safe(lambda conn: CampaignFinanceIdentity(conn).review(
            namespace, candidate_id, decision, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def revert_campaign_finance_identity_match(namespace: str, candidate_id: str, reason: str) -> dict:
        """Revert an accepted or rejected campaign-finance identity decision; records stay intact."""
        from src.kb.campaign_finance_identity import CampaignFinanceIdentity

        return safe(lambda conn: CampaignFinanceIdentity(conn).revert(
            namespace, candidate_id, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def link_campaign_finance_contests(namespace: str, elections_namespace: str | None = None) -> dict:
        """Link filing versions and independent expenditures to election contests through accepted candidate and
        party matches; degrades when the elections feature is absent and reports missing targets."""
        from src.kb.campaign_finance_links import CampaignFinanceLinks

        return safe(lambda conn: CampaignFinanceLinks(conn).link_contests(
            namespace, principal_id=who()[0], scopes=who()[1], elections_namespace=elections_namespace),
            write=True, required_scope=WRITE)

    @mcp.tool()
    def link_campaign_finance_lobbying(namespace: str, lobbying_namespace: str | None = None) -> dict:
        """Link organisational donors' contributions to the lobbying register revision in force through accepted
        matches; degrades when the lobbying feature is absent. No influence or quid-pro-quo inference."""
        from src.kb.campaign_finance_links import CampaignFinanceLinks

        return safe(lambda conn: CampaignFinanceLinks(conn).link_lobbying(
            namespace, principal_id=who()[0], scopes=who()[1], lobbying_namespace=lobbying_namespace),
            write=True, required_scope=WRITE)

    @mcp.tool()
    def link_campaign_finance_ownership(namespace: str, ownership_namespace: str) -> dict:
        """Link organisational donors and connected organisations to Corporate Ownership records through accepted
        matches; degrades when the ownership store is absent and reports missing targets."""
        from src.kb.campaign_finance_links import CampaignFinanceLinks

        return safe(lambda conn: CampaignFinanceLinks(conn).link_ownership(
            namespace, ownership_namespace, principal_id=who()[0], scopes=who()[1]), write=True, required_scope=WRITE)

    @mcp.tool()
    def create_campaign_finance_monitor(namespace: str, request_key: str, watch: str, key: str,
                                        ownership_namespace: str | None = None) -> dict:
        """Watch a committee (FEC or Commission id), an organisation (with its affiliates) or a contest for new
        filings, amendments and independent expenditures."""
        from src.kb.campaign_finance_monitoring import CampaignFinanceMonitor

        return safe(lambda conn: CampaignFinanceMonitor(conn).create(
            namespace, request_key, watch=watch, key=key, principal_id=who()[0], scopes=who()[1],
            ownership_namespace=ownership_namespace), write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def run_campaign_finance_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a campaign-finance monitor at a committed watermark; notices cite the new and previous revision
        and never name an individual donor."""
        from src.kb.campaign_finance_monitoring import CampaignFinanceMonitor

        return safe(lambda conn: CampaignFinanceMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def poll_campaign_finance_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Delivered campaign-finance monitor events after a cursor."""
        from src.kb.campaign_finance_monitoring import CampaignFinanceMonitor

        return safe(lambda conn: CampaignFinanceMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor),
            required_scope="knowledge:subscriptions:read")
