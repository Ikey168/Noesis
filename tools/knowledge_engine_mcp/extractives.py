"""Economics extractives features' entry points: extractive payments for a company (and its group) per EITI report
version, a country's reports, commodity production and reserves with sources side by side, series and report
history, reviewable identity, cross-pack links, evidence bundles and subscription monitors.

Acquisition runs through the shared source-pack tools (pack ``economic-statistics-and-filings``: sources
``eiti-summary-data``, ``usgs-mineral-commodity-summaries`` and ``bgs-world-mineral-statistics``). Every figure is
cited with its source, report revision or series vintage and as-of time.

Exclusions (declared by every answering tool): no reconciliation of payment discrepancies beyond those EITI
reports, no own reserve estimates, no corruption or governance risk scoring, no price forecasts, no currency
conversion or sums across reports, no blending of USGS and BGS series. Minimisation (EX01): no natural-person field
is stored or returned; every answer is checked before it leaves the tool.
"""

READ = "knowledge:extractives:read"
WRITE = "knowledge:extractives:write"
REVIEW = "knowledge:extractives:review"
OWNERSHIP_READ = "knowledge:ownership:read"
OWNERSHIP_REVIEW = "knowledge:ownership:review"
INFRA_READ = "knowledge:infrastructure:read"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
EXCLUSIONS_NOTE = (
    "Exclusions: no reconciliation of payment discrepancies beyond those EITI reports, no own reserve estimates, "
    "no corruption or governance risk scoring, no price forecasts; currencies never converted or summed across "
    "reports; USGS and BGS never blended."
)

EXTRACTIVES_WRITES = {
    "import_extractives_concordance",
    "propose_extractives_identity",
    "propose_extractives_company_matches",
    "review_extractives_identity",
    "revert_extractives_identity",
    "review_extractives_company_match",
    "revert_extractives_company_match",
    "link_extractives_records",
    "create_extractives_monitor",
    "run_extractives_monitor",
}
EXTRACTIVES_READS = {
    "extractives_source_contracts",
    "extractives_readiness",
    "list_extractives_series",
    "extractive_payments_for_company",
    "extractive_payments_for_country",
    "commodity_production_side_by_side",
    "extractives_series_history",
    "eiti_report_history",
    "list_extractives_identity",
    "list_extractives_company_matches",
    "list_extractives_links",
    "export_extractives_evidence_bundle",
    "poll_extractives_monitor",
}
EXTRACTIVES_TOOLS = EXTRACTIVES_WRITES | EXTRACTIVES_READS
EXTRACTIVES_SCOPES = {
    "extractives_source_contracts": [],
    "extractives_readiness": [READ],
    "list_extractives_series": [READ],
    "extractive_payments_for_company": [READ],
    "extractive_payments_for_country": [READ],
    "commodity_production_side_by_side": [READ],
    "extractives_series_history": [READ],
    "eiti_report_history": [READ],
    "list_extractives_identity": [READ],
    "list_extractives_company_matches": [READ, OWNERSHIP_READ],
    "list_extractives_links": [READ],
    "export_extractives_evidence_bundle": [READ],
    "poll_extractives_monitor": [READ, SUBSCRIPTIONS_READ],
    "import_extractives_concordance": [WRITE],
    "propose_extractives_identity": [WRITE],
    "propose_extractives_company_matches": [WRITE, OWNERSHIP_READ],
    "review_extractives_identity": [REVIEW],
    "revert_extractives_identity": [REVIEW],
    "review_extractives_company_match": [REVIEW, OWNERSHIP_REVIEW],
    "revert_extractives_company_match": [REVIEW, OWNERSHIP_REVIEW],
    "link_extractives_records": [WRITE],
    "create_extractives_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_extractives_monitor": [READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return EXTRACTIVES_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _require(scopes, *required):
    from src.kb.extractives_records import ExtractivesError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise ExtractivesError("unauthorized", f"{', '.join(missing)} required")


def _declared(answer):
    """Declare the exclusions and enforce the EX01 minimisation decision on every answer."""
    from src.ingestion.extractives_sources import (
        EXCLUSIONS,
        MINIMISATION,
        personal_keys,
    )
    from src.kb.extractives_records import ExtractivesError, forbidden_keys

    if personal_keys(answer) or forbidden_keys(answer):
        raise ExtractivesError("minimisation_violation", "an answer may not carry a natural-person field or a "
                                                         "derived, converted, scored or forecast value")
    if isinstance(answer, dict):
        return {**answer, "exclusions": list(EXCLUSIONS), "minimisation": MINIMISATION["excluded"]}
    return answer


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def run_tool(tool, operation, *, write=False):
        """Checks every declared scope, runs the operation, declares exclusions and enforces minimisation."""
        scopes = EXTRACTIVES_SCOPES[tool]

        def run(conn):
            _require(who()[1], *scopes)
            return _declared(operation(conn))

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def extractives_source_contracts() -> dict:
        """Per-provider access decisions (EITI summary data, USGS Mineral Commodity Summaries, BGS World Mineral
        Statistics), endpoints, terms, revision models, the minimisation decision and the bounded coverage.
        Exclusions: no reconciliation of payment discrepancies beyond those EITI reports, no own reserve estimates,
        no corruption or governance risk scoring, no price forecasts; currencies never converted or summed across
        reports; USGS and BGS never blended."""
        from src.ingestion.extractives_sources import (
            BOUNDED_COVERAGE,
            EXCLUSIONS,
            LIVE_VERIFICATION,
            MINIMISATION,
            NEVER_SENTENCE,
            PROVIDER_CONTRACTS,
        )

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "bounded_coverage": BOUNDED_COVERAGE, "minimisation": MINIMISATION, "never": NEVER_SENTENCE,
                "exclusions": list(EXCLUSIONS)}

    @mcp.tool()
    def extractives_readiness() -> dict:
        """Which extractives features are selected, the stores and per-provider releases."""
        from src.kb.extractives_store import readiness

        return run_tool("extractives_readiness", readiness)

    @mcp.tool()
    def list_extractives_series(namespace: str, provider: str | None = None, statistic: str | None = None) -> dict:
        """Commodity series with source, commodity and form as published, statistic, unit, country and vintages;
        USGS and BGS series are always separate."""
        from src.kb.extractives_records import authorize
        from src.kb.extractives_store import ExtractivesStore

        def op(conn):
            authorize(namespace, who()[1], READ)
            return {"series": ExtractivesStore(conn, initialize=False).find_series(
                namespace, provider=provider, statistic=statistic)}

        return run_tool("list_extractives_series", op)

    @mcp.tool()
    def extractive_payments_for_company(namespace: str, company: str, ownership_namespace: str | None = None,
                                        group: bool = False, as_of_ms: int | None = None, as_of: str | None = None,
                                        history: bool = False) -> dict:
        """Payments by a company (an extractives company key, or an ownership entity - with its group through
        accepted ownership links when group=true) per EITI report revision and revenue stream: government- and
        company-reported figures side by side with EITI's own discrepancies, each citing its report revision.
        Exclusions: no reconciliation of payment discrepancies beyond those EITI reports, no own reserve estimates,
        no corruption or governance risk scoring, no price forecasts; currencies never converted or summed across
        reports; USGS and BGS never blended."""
        from src.kb.extractives_queries import ExtractivesQueries

        return run_tool("extractive_payments_for_company", lambda conn: ExtractivesQueries(conn).payments_for_company(
            namespace, company, scopes=who()[1], ownership_namespace=ownership_namespace, group=group,
            as_of_ms=as_of_ms, as_of=as_of, history=history, principal_id=who()[0]))

    @mcp.tool()
    def extractive_payments_for_country(namespace: str, country: str, as_of_ms: int | None = None,
                                        history: bool = False) -> dict:
        """A country's EITI reports (ISO alpha-3) as of a date: government revenues by stream, company lines and
        discrepancies per report revision, cited.
        Exclusions: no reconciliation of payment discrepancies beyond those EITI reports, no own reserve estimates,
        no corruption or governance risk scoring, no price forecasts; currencies never converted or summed across
        reports; USGS and BGS never blended."""
        from src.kb.extractives_queries import ExtractivesQueries

        return run_tool("extractive_payments_for_country", lambda conn: ExtractivesQueries(conn).payments_for_country(
            namespace, country, scopes=who()[1], as_of_ms=as_of_ms, history=history))

    @mcp.tool()
    def commodity_production_side_by_side(namespace: str, commodity: str | dict, country: str | dict,
                                          statistic: str | None = None, as_of_ms: int | None = None,
                                          history: bool = False) -> dict:
        """Production, reserves, capacity, imports or exports of a commodity (name as published or {hs_code})
        for a country (name or ISO alpha-3) from each source by vintage; withheld, unavailable and estimated values
        stay marked and each figure cites its vintage.
        Exclusions: no reconciliation of payment discrepancies beyond those EITI reports, no own reserve estimates,
        no corruption or governance risk scoring, no price forecasts; currencies never converted or summed across
        reports; USGS and BGS never blended."""
        from src.kb.extractives_queries import ExtractivesQueries

        return run_tool("commodity_production_side_by_side", lambda conn: ExtractivesQueries(conn).production(
            namespace, commodity=commodity, country=country, scopes=who()[1], statistic=statistic, as_of_ms=as_of_ms,
            history=history))

    @mcp.tool()
    def extractives_series_history(namespace: str, series_id: str) -> dict:
        """Every retained vintage of a commodity series with its new, revised and removed years."""
        from src.kb.extractives_queries import ExtractivesQueries

        return run_tool("extractives_series_history", lambda conn: ExtractivesQueries(conn).series_history(
            namespace, series_id, scopes=who()[1]))

    @mcp.tool()
    def eiti_report_history(namespace: str, report_key: str) -> dict:
        """Every retained revision of an EITI report (a withdrawal or correction is a revision) with its lines."""
        from src.kb.extractives_queries import ExtractivesQueries

        return run_tool("eiti_report_history", lambda conn: ExtractivesQueries(conn).report_history(
            namespace, report_key, scopes=who()[1]))

    @mcp.tool()
    def list_extractives_identity(namespace: str, kind: str | None = None, state: str | None = None) -> dict:
        """Commodity, country and project mappings (proposed, unmatched, accepted, rejected, reverted), each with
        method, evidence and confidence."""
        from src.kb.extractives_identity import ExtractivesIdentity

        return run_tool("list_extractives_identity", lambda conn: {
            "assertions": ExtractivesIdentity(conn, initialize=False).assertions(namespace, scopes=who()[1],
                                                                                 kind=kind, state=state)})

    @mcp.tool()
    def list_extractives_company_matches(namespace: str) -> dict:
        """Reporting-company candidates against ownership entities (exact identifier first, names as low evidence)
        and the companies that stay unmatched; nothing is auto-merged."""
        from src.kb.extractives_identity import ExtractivesIdentity

        def op(conn):
            identity = ExtractivesIdentity(conn, initialize=False)
            return {"candidates": identity.company_candidates(namespace, scopes=who()[1]),
                    "unmatched": identity.unmatched_companies(namespace, scopes=who()[1])}

        return run_tool("list_extractives_company_matches", op)

    @mcp.tool()
    def list_extractives_links(namespace: str, source_id: str | None = None, target_owner: str | None = None,
                               state: str | None = None) -> dict:
        """Links to public finance, trade, energy and infrastructure with their basis (citation, shared identifier
        or accepted match) and target revision; missing providers or targets are listed as unresolved."""
        from src.kb.extractives_links import ExtractivesLinks

        return run_tool("list_extractives_links", lambda conn: {"links": ExtractivesLinks(conn, initialize=False).links(
            namespace, scopes=who()[1], source_id=source_id, target_owner=target_owner, state=state)})

    @mcp.tool()
    def export_extractives_evidence_bundle(namespace: str, question: str, company: str | None = None,
                                           country: str | dict | None = None, commodity: str | dict | None = None,
                                           ownership_namespace: str | None = None, group: bool = False,
                                           as_of_ms: int | None = None) -> dict:
        """An evidence bundle for a payments or production answer, citing every item with source, record revision
        and as-of time (question: payments-for-company, payments-for-country or commodity-production).
        Exclusions: no reconciliation of payment discrepancies beyond those EITI reports, no own reserve estimates,
        no corruption or governance risk scoring, no price forecasts; currencies never converted or summed across
        reports; USGS and BGS never blended."""
        from src.kb.extractives_queries import ExtractivesQueries
        from src.kb.extractives_records import ExtractivesError

        def op(conn):
            queries = ExtractivesQueries(conn)
            if question == "payments-for-company":
                answer = queries.payments_for_company(namespace, str(company), scopes=who()[1],
                                                      ownership_namespace=ownership_namespace, group=group,
                                                      as_of_ms=as_of_ms, principal_id=who()[0])
            elif question == "payments-for-country":
                answer = queries.payments_for_country(namespace, str(country), scopes=who()[1], as_of_ms=as_of_ms)
            elif question == "commodity-production":
                answer = queries.production(namespace, commodity=commodity, country=country, scopes=who()[1],
                                            as_of_ms=as_of_ms)
            else:
                raise ExtractivesError("invalid_query", "question is payments-for-company, payments-for-country or "
                                                        "commodity-production")
            return {"bundle": queries.export_bundle(answer)}

        return run_tool("export_extractives_evidence_bundle", op)

    @mcp.tool()
    def poll_extractives_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll an extractives monitor's events (new reports, report revisions, new releases, revised values)."""
        from src.kb.extractives_monitoring import ExtractivesMonitor

        return run_tool("poll_extractives_monitor", lambda conn: ExtractivesMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))

    # ------------------------------------------------------------------ writes

    @mcp.tool()
    def import_extractives_concordance(namespace: str, table: dict) -> dict:
        """Record a published commodity-to-HS or country-name-to-code table with its URL, publisher, publication
        date and file digest; each row exact, partial or one-to-many as published."""
        from src.kb.extractives_identity import ExtractivesIdentity

        return run_tool("import_extractives_concordance", lambda conn: ExtractivesIdentity(conn).import_concordance(
            namespace, table, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def propose_extractives_identity(namespace: str, kind: str, infra_namespace: str | None = None) -> dict:
        """Propose commodity (stated HS code, else a cited concordance), country (published code, else a cited code
        list) or project (published identifier or coordinates of an infrastructure asset) mappings; unmatched
        subjects stay visible and nothing is used until reviewed."""
        from src.kb.extractives_identity import ExtractivesIdentity
        from src.kb.extractives_records import ExtractivesError

        def op(conn):
            identity = ExtractivesIdentity(conn)
            if kind == "commodity":
                return identity.propose_commodities(namespace, principal_id=who()[0], scopes=who()[1])
            if kind == "country":
                return identity.propose_countries(namespace, principal_id=who()[0], scopes=who()[1])
            if kind == "project":
                return identity.propose_projects(namespace, infra_namespace=infra_namespace or "global",
                                                 principal_id=who()[0], scopes=who()[1])
            raise ExtractivesError("invalid_query", "kind is commodity, country or project")

        return run_tool("propose_extractives_identity", op, write=True)

    @mcp.tool()
    def propose_extractives_company_matches(namespace: str, ownership_namespace: str) -> dict:
        """Offer reporting companies to ownership entities: published identifiers first, names as low evidence;
        never accepted automatically; redacted natural persons are never proposed."""
        from src.kb.extractives_identity import ExtractivesIdentity

        return run_tool("propose_extractives_company_matches", lambda conn: ExtractivesIdentity(conn).propose_companies(
            namespace, ownership_namespace=ownership_namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_extractives_identity(namespace: str, assertion_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a proposed commodity, country or project mapping with a reason."""
        from src.kb.extractives_identity import ExtractivesIdentity

        return run_tool("review_extractives_identity", lambda conn: ExtractivesIdentity(conn).review(
            namespace, assertion_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def revert_extractives_identity(namespace: str, assertion_id: str, reason: str) -> dict:
        """Revert a reviewed commodity, country or project mapping; the subject is unmapped again."""
        from src.kb.extractives_identity import ExtractivesIdentity

        return run_tool("revert_extractives_identity", lambda conn: ExtractivesIdentity(conn).revert(
            namespace, assertion_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_extractives_company_match(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a company candidate as an entity identity decision (records are never merged)."""
        from src.kb.extractives_identity import ExtractivesIdentity

        return run_tool("review_extractives_company_match", lambda conn: ExtractivesIdentity(conn).review_company(
            namespace, candidate_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def revert_extractives_company_match(namespace: str, candidate_id: str, reason: str) -> dict:
        """Revert a company identity decision; payments stay as reported and the company is unmatched again."""
        from src.kb.extractives_identity import ExtractivesIdentity

        return run_tool("revert_extractives_company_match", lambda conn: ExtractivesIdentity(conn).revert_company(
            namespace, candidate_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def link_extractives_records(namespace: str, finance_namespace: str = "global", trade_namespace: str = "global",
                                 energy_namespace: str = "global") -> dict:
        """Link payments to public-finance lines, commodities to trade series, hydrocarbon series to Energy series
        and projects to infrastructure assets by shared identifier or accepted match; missing providers or targets
        are reported, never dropped.
        Exclusions: no reconciliation of payment discrepancies beyond those EITI reports, no own reserve estimates,
        no corruption or governance risk scoring, no price forecasts; currencies never converted or summed across
        reports; USGS and BGS never blended."""
        from src.kb.extractives_links import ExtractivesLinks

        def op(conn):
            links = ExtractivesLinks(conn)
            principal, scopes = who()
            return {"public_finance": links.link_public_finance(namespace, principal_id=principal, scopes=scopes,
                                                                finance_namespace=finance_namespace),
                    "trade": links.link_trade(namespace, principal_id=principal, scopes=scopes,
                                              trade_namespace=trade_namespace),
                    "energy": links.link_energy(namespace, principal_id=principal, scopes=scopes,
                                                energy_namespace=energy_namespace),
                    "infrastructure": links.link_infrastructure(namespace, principal_id=principal, scopes=scopes)}

        return run_tool("link_extractives_records", op, write=True)

    @mcp.tool()
    def create_extractives_monitor(namespace: str, request_key: str, target: dict, delivery: dict | None = None) -> dict:
        """Subscribe to a company, a country or a commodity: notices of new EITI reports, report revisions and new
        commodity releases, each citing the new or revised record (record changes, never assessments)."""
        from src.kb.extractives_monitoring import ExtractivesMonitor

        return run_tool("create_extractives_monitor", lambda conn: ExtractivesMonitor(conn).create(
            namespace, request_key, target=target, principal_id=who()[0], scopes=who()[1], delivery=delivery),
            write=True)

    @mcp.tool()
    def run_extractives_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate an extractives monitor at a committed watermark; notices cite the records before and after."""
        from src.kb.extractives_monitoring import ExtractivesMonitor

        return run_tool("run_extractives_monitor", lambda conn: ExtractivesMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True)
