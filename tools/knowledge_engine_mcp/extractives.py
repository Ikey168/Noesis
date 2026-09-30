"""Economics extractives entry points: EITI payments per report version for a company, its group or a country,
commodity production and reserves side by side, record history, evidence bundles, reviewable identity, cross-pack
links and subscription monitors.

Acquisition runs through the shared source-pack tools (pack ``economic-extractives``: EITI summary data, USGS
Mineral Commodity Summaries, BGS World Mineral Statistics; features ``extractives-eiti``, ``extractives-usgs`` and
``extractives-bgs``). Every item is cited with its source, record revision and as-of time; sources stay side by
side and currencies as reported.

Exclusions (declared by every tool): no reconciliation of payment discrepancies beyond the EITI reports, no own
reserve estimates, no corruption or governance risk scoring, no price forecasts. Outputs follow the minimisation
decision: no contact person, beneficial owner or individual payer's name is stored or returned.
"""

READ = "knowledge:extractives:read"
WRITE = "knowledge:extractives:write"
REVIEW = "knowledge:extractives:review"
OWNERSHIP_READ = "knowledge:ownership:read"
OWNERSHIP_REVIEW = "knowledge:ownership:review"
TRADE_READ = "knowledge:trade:read"
ENERGY_READ = "knowledge:energy:read"
PUBLIC_FINANCE_READ = "knowledge:economic:public-finance:read"
INFRASTRUCTURE_READ = "knowledge:infrastructure:read"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
EXCLUSIONS_NOTE = (
    "Exclusions: no reconciliation of payment discrepancies beyond the EITI reports, no own reserve estimates, no "
    "corruption or governance risk scoring, no price forecasts."
)

EXTRACTIVES_WRITES = {
    "propose_extractives_company_matches",
    "review_extractives_company_match",
    "revert_extractives_company_match",
    "import_extractives_concordance",
    "propose_extractives_commodity_matches",
    "propose_extractives_project_matches",
    "review_extractives_match",
    "revert_extractives_match",
    "link_extractives_public_finance",
    "link_extractives_trade_flows",
    "link_extractives_energy",
    "link_extractives_infrastructure",
    "create_extractives_monitor",
    "run_extractives_monitor",
}
EXTRACTIVES_READS = {
    "extractives_source_contracts",
    "extractives_readiness",
    "list_extractives_series",
    "extractives_series_values",
    "query_extractives_production",
    "query_extractives_company_payments",
    "query_extractives_country_payments",
    "extractives_record_history",
    "export_extractives_evidence_bundle",
    "list_extractives_matches",
    "list_extractives_links",
    "poll_extractives_monitor",
}
EXTRACTIVES_TOOLS = EXTRACTIVES_WRITES | EXTRACTIVES_READS
EXTRACTIVES_SCOPES = {
    "extractives_source_contracts": [],
    "extractives_readiness": [READ],
    "list_extractives_series": [READ],
    "extractives_series_values": [READ],
    "query_extractives_production": [READ],
    "query_extractives_company_payments": [READ, OWNERSHIP_READ],
    "query_extractives_country_payments": [READ],
    "extractives_record_history": [READ],
    "export_extractives_evidence_bundle": [READ],
    "list_extractives_matches": [READ],
    "list_extractives_links": [READ],
    "poll_extractives_monitor": [READ, SUBSCRIPTIONS_READ],
    "propose_extractives_company_matches": [WRITE, OWNERSHIP_READ],
    "review_extractives_company_match": [REVIEW, OWNERSHIP_REVIEW],
    "revert_extractives_company_match": [REVIEW, OWNERSHIP_REVIEW],
    "import_extractives_concordance": [WRITE],
    "propose_extractives_commodity_matches": [WRITE],
    "propose_extractives_project_matches": [WRITE, INFRASTRUCTURE_READ],
    "review_extractives_match": [REVIEW],
    "revert_extractives_match": [REVIEW],
    "link_extractives_public_finance": [WRITE],
    "link_extractives_trade_flows": [WRITE],
    "link_extractives_energy": [WRITE],
    "link_extractives_infrastructure": [WRITE],
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
    """Every answer declares the exclusions and is checked against the minimisation decision before it leaves."""
    from src.ingestion.extractives_sources import EXCLUSIONS, personal_keys
    from src.kb.extractives_records import ExtractivesError

    if not isinstance(answer, dict):
        return answer
    found = personal_keys(answer)
    if found:
        raise ExtractivesError("personal_data_refused", "an answer would carry personal fields", paths=found)
    return {**answer, "exclusions": list(EXCLUSIONS)}


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def run_tool(tool, operation, *, write=False):
        scopes = EXTRACTIVES_SCOPES[tool]

        def run(conn):
            _require(who()[1], *scopes)
            return _declared(operation(conn))

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def extractives_source_contracts() -> dict:
        """Per-provider access decisions (EITI summary data, USGS Mineral Commodity Summaries, BGS World Mineral
        Statistics), formats, identifiers, licences, rate limits, revision models, the personal-data minimisation
        decision and the bounded coverage; terms not re-verified live.
        Exclusions: no reconciliation of payment discrepancies beyond the EITI reports, no own reserve estimates, no
        corruption or governance risk scoring, no price forecasts."""
        from src.ingestion.extractives_sources import (
            EXCLUSIONS,
            LIVE_VERIFICATION,
            NEVER_SENTENCE,
            PROVIDER_CONTRACTS,
            coverage_report,
        )

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "coverage": coverage_report(), "never": NEVER_SENTENCE, "exclusions": list(EXCLUSIONS)}

    @mcp.tool()
    def extractives_readiness() -> dict:
        """Which extractives features are selected, whether the stores hold releases per provider, which link
        targets (ownership, trade, Energy, public finance, infrastructure) are held, and the minimisation decision."""
        from src.kb.extractives_records import readiness

        return run_tool("extractives_readiness", readiness)

    @mcp.tool()
    def list_extractives_series(namespace: str, provider: str | None = None, commodity: str | None = None,
                                statistic: str | None = None, country: str | None = None) -> dict:
        """Commodity series (production, reserves, imports, exports) keyed by source, commodity, statistic, unit and
        country, with definition, licence and vintage count."""
        from src.kb.extractives_store import read_store

        return run_tool("list_extractives_series", lambda conn: {"series": read_store(
            conn, namespace, who()[1]).find_series(namespace, provider=provider, commodity=commodity,
                                                   statistic=statistic, country=country)})

    @mcp.tool()
    def extractives_series_values(namespace: str, series_id: str, as_of: str | None = None) -> dict:
        """One series' values in the vintage published by the date (latest by default) with status, estimated and
        revised markers and notes as published, every vintage and the release citation."""
        from src.kb.extractives_records import ExtractivesError, as_of_ms
        from src.kb.extractives_store import read_store

        def op(conn):
            store = read_store(conn, namespace, who()[1])
            vintage = store.select_vintage(namespace, series_id, as_of_ms(as_of))
            if vintage is None:
                raise ExtractivesError("not_found", "no vintage of this series was published by the date")
            return {"series": store.series(namespace, series_id), "vintage": vintage,
                    "values": store.values(namespace, vintage["vintage_id"]),
                    "citation": store.source_revision(namespace, vintage["release_id"]),
                    "vintages": store.vintage_rows(namespace, series_id)}

        return run_tool("extractives_series_values", op)

    @mcp.tool()
    def query_extractives_production(namespace: str, commodity: str, country: str, as_of: str | None = None,
                                     all_vintages: bool = False, statistic: str | None = None) -> dict:
        """A commodity (source code, or hs:<heading> through accepted concordance matches) and a country to
        production and reserves per source and vintage; USGS and BGS side by side, never blended; withheld and
        estimated values stay marked.
        Exclusions: no reconciliation of payment discrepancies beyond the EITI reports, no own reserve estimates, no
        corruption or governance risk scoring, no price forecasts."""
        from src.kb.extractives_queries import ExtractivesQueries

        return run_tool("query_extractives_production", lambda conn: ExtractivesQueries(conn).production_and_reserves(
            namespace, commodity, country, scopes=who()[1], as_of=as_of, all_vintages=all_vintages,
            statistic=statistic))

    @mcp.tool()
    def query_extractives_company_payments(namespace: str, entity: str, ownership_namespace: str,
                                           as_of: str | None = None, group: bool = False,
                                           all_versions: bool = False, include_unknowns: bool = False) -> dict:
        """A company (ownership entity) and optionally its group as of a date to EITI payments per report version and
        revenue stream through reviewed matches: government- and company-reported figures side by side with the
        report's discrepancies, currencies as reported, each citing its report revision; no payment on record is
        not a clean bill.
        Exclusions: no reconciliation of payment discrepancies beyond the EITI reports, no own reserve estimates, no
        corruption or governance risk scoring, no price forecasts."""
        from src.kb.extractives_queries import ExtractivesQueries

        return run_tool("query_extractives_company_payments",
                        lambda conn: ExtractivesQueries(conn).payments_for_company(
                            namespace, entity, ownership_namespace=ownership_namespace, scopes=who()[1], as_of=as_of,
                            group=group, all_versions=all_versions, include_unknowns=include_unknowns,
                            principal_id=who()[0]))

    @mcp.tool()
    def query_extractives_country_payments(namespace: str, country: str, as_of: str | None = None,
                                           all_versions: bool = False) -> dict:
        """A country's EITI reports as of a date: versions, revenue streams with government-reported totals as
        published, and payments with each company's match status (individual payers' names withheld).
        Exclusions: no reconciliation of payment discrepancies beyond the EITI reports, no own reserve estimates, no
        corruption or governance risk scoring, no price forecasts."""
        from src.kb.extractives_queries import ExtractivesQueries

        return run_tool("query_extractives_country_payments",
                        lambda conn: ExtractivesQueries(conn).payments_for_country(
                            namespace, country, scopes=who()[1], as_of=as_of, all_versions=all_versions))

    @mcp.tool()
    def extractives_record_history(namespace: str, record_key: str) -> dict:
        """Every revision of one EITI record (report version, state, as-of time) with its citation."""
        from src.kb.extractives_queries import ExtractivesQueries

        return run_tool("extractives_record_history", lambda conn: ExtractivesQueries(conn).record_history(
            namespace, record_key, scopes=who()[1]))

    @mcp.tool()
    def export_extractives_evidence_bundle(namespace: str, query: str, key: str, country: str | None = None,
                                           ownership_namespace: str | None = None, as_of: str | None = None,
                                           group: bool = False) -> dict:
        """An evidence bundle for 'company' (key = ownership entity), 'country' (key = ISO code) or 'commodity'
        (key = commodity, with country): every assertion cites source, record revision and as-of time.
        Exclusions: no reconciliation of payment discrepancies beyond the EITI reports, no own reserve estimates, no
        corruption or governance risk scoring, no price forecasts."""
        from src.kb.extractives_queries import ExtractivesQueries

        def op(conn):
            ask = ExtractivesQueries(conn)
            if query == "company":
                if not ownership_namespace:
                    from src.kb.extractives_records import ExtractivesError

                    raise ExtractivesError("invalid_request", "a company bundle names its ownership namespace")
                answer = ask.payments_for_company(namespace, key, ownership_namespace=ownership_namespace,
                                                  scopes=who()[1], as_of=as_of, group=group, principal_id=who()[0])
            elif query == "country":
                answer = ask.payments_for_country(namespace, key, scopes=who()[1], as_of=as_of)
            elif query == "commodity" and country:
                answer = ask.production_and_reserves(namespace, key, country, scopes=who()[1], as_of=as_of)
            else:
                from src.kb.extractives_records import ExtractivesError

                raise ExtractivesError("invalid_request", "query is company, country or commodity (with country)")
            return {"status": answer["status"], "evidence_bundle": ask.evidence_bundle(answer)}

        return run_tool("export_extractives_evidence_bundle", op)

    @mcp.tool()
    def list_extractives_matches(namespace: str, kind: str | None = None, state: str | None = None) -> dict:
        """Company matches (with method, confidence, reviewer and evidence) and commodity/project matches, and the
        companies that stay unmatched."""
        from src.kb.extractives_identity import read_identity

        def op(conn):
            identity = read_identity(conn, namespace, who()[1])
            out = {"matches": identity.matches(namespace, kind=kind, state=state)}
            if kind in (None, "company"):
                out["company_matches"] = [c for c in identity.company_candidates(namespace, scopes=who()[1])
                                          if state is None or c["state"] == state]
                out["unmatched_companies"] = identity.unmatched_companies(namespace, scopes=who()[1])
            return out

        return run_tool("list_extractives_matches", op)

    @mcp.tool()
    def list_extractives_links(namespace: str, subject_id: str | None = None, target_kind: str | None = None) -> dict:
        """Links to public finance, trade flows, Energy and infrastructure with basis, status and revisions."""
        from src.kb.extractives_links import read_links

        return run_tool("list_extractives_links", lambda conn: {"links": read_links(conn, namespace, who()[1]).links(
            namespace, subject_id=subject_id, target_kind=target_kind)})

    @mcp.tool()
    def poll_extractives_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll an extractives monitor's delivered notices."""
        from src.kb.extractives_monitoring import ExtractivesMonitor

        return run_tool("poll_extractives_monitor", lambda conn: ExtractivesMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))

    @mcp.tool()
    def propose_extractives_company_matches(namespace: str, ownership_namespace: str) -> dict:
        """Offer EITI companies to ownership entities: published identifiers first, equal names as low-evidence
        candidates; nothing accepted automatically, individual payers never offered."""
        from src.kb.extractives_identity import ExtractivesIdentity

        return run_tool("propose_extractives_company_matches", lambda conn: ExtractivesIdentity(conn).propose_companies(
            namespace, ownership_namespace=ownership_namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_extractives_company_match(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a company candidate with a reason (an entity identity decision)."""
        from src.kb.extractives_identity import ExtractivesIdentity

        return run_tool("review_extractives_company_match", lambda conn: ExtractivesIdentity(conn).review_company(
            namespace, candidate_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def revert_extractives_company_match(namespace: str, candidate_id: str, reason: str) -> dict:
        """Revert a company decision with a reason; records stay intact."""
        from src.kb.extractives_identity import ExtractivesIdentity

        return run_tool("revert_extractives_company_match", lambda conn: ExtractivesIdentity(conn).revert_company(
            namespace, candidate_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def import_extractives_concordance(namespace: str, table: dict) -> dict:
        """Record a published commodity-to-HS correspondence with its citation (URL and publication date)."""
        from src.kb.extractives_identity import ExtractivesIdentity

        return run_tool("import_extractives_concordance", lambda conn: ExtractivesIdentity(conn).import_concordance(
            namespace, table, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def propose_extractives_commodity_matches(namespace: str) -> dict:
        """Propose commodity-to-HS matches from recorded published concordances only; no code is guessed."""
        from src.kb.extractives_identity import ExtractivesIdentity

        return run_tool("propose_extractives_commodity_matches",
                        lambda conn: ExtractivesIdentity(conn).propose_commodities(
                            namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def propose_extractives_project_matches(namespace: str, infrastructure_namespace: str) -> dict:
        """Propose project-to-infrastructure matches by shared published identifiers or equal published
        coordinates; never by name."""
        from src.kb.extractives_identity import ExtractivesIdentity

        return run_tool("propose_extractives_project_matches",
                        lambda conn: ExtractivesIdentity(conn).propose_projects(
                            namespace, infrastructure_namespace=infrastructure_namespace, principal_id=who()[0],
                            scopes=who()[1]), write=True)

    @mcp.tool()
    def review_extractives_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a proposed commodity or project match with a reason."""
        from src.kb.extractives_identity import ExtractivesIdentity

        return run_tool("review_extractives_match", lambda conn: ExtractivesIdentity(conn).review(
            namespace, match_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def revert_extractives_match(namespace: str, match_id: str, reason: str) -> dict:
        """Revert an accepted or rejected commodity or project match with a reason."""
        from src.kb.extractives_identity import ExtractivesIdentity

        return run_tool("revert_extractives_match", lambda conn: ExtractivesIdentity(conn).revert(
            namespace, match_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def link_extractives_public_finance(namespace: str, record_key: str, line_id: str, citation: dict,
                                        public_finance_namespace: str | None = None) -> dict:
        """Link an EITI payment or revenue stream to a public-finance budget line by explicit citation; an absent
        store or line is recorded as provider_absent or target_not_found."""
        from src.kb.extractives_links import ExtractivesLinks

        return run_tool("link_extractives_public_finance", lambda conn: ExtractivesLinks(conn).link_public_finance(
            namespace, record_key, line_id, citation, principal_id=who()[0], scopes=who()[1],
            public_finance_namespace=public_finance_namespace), write=True)

    @mcp.tool()
    def link_extractives_trade_flows(namespace: str, trade_namespace: str | None = None) -> dict:
        """Link commodity series to trade series within an accepted HS heading for the same published country
        code; values side by side, never combined."""
        from src.kb.extractives_links import ExtractivesLinks

        return run_tool("link_extractives_trade_flows", lambda conn: ExtractivesLinks(conn).link_trade_flows(
            namespace, principal_id=who()[0], scopes=who()[1], trade_namespace=trade_namespace), write=True)

    @mcp.tool()
    def link_extractives_energy(namespace: str, energy_namespace: str = "energy") -> dict:
        """Link hydrocarbon series to Energy balance series by shared SIEC and country codes."""
        from src.kb.extractives_links import ExtractivesLinks

        return run_tool("link_extractives_energy", lambda conn: ExtractivesLinks(conn).link_energy(
            namespace, principal_id=who()[0], scopes=who()[1], energy_namespace=energy_namespace), write=True)

    @mcp.tool()
    def link_extractives_infrastructure(namespace: str) -> dict:
        """Link projects to infrastructure assets through accepted project matches; no ownership inferred."""
        from src.kb.extractives_links import ExtractivesLinks

        return run_tool("link_extractives_infrastructure", lambda conn: ExtractivesLinks(conn).link_infrastructure(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def create_extractives_monitor(namespace: str, request_key: str, watch: dict,
                                   delivery: dict | None = None) -> dict:
        """Subscribe to companies, countries or commodities: notices of new EITI report versions, payment revisions
        and new or revised commodity releases (record changes, not assessments)."""
        from src.kb.extractives_monitoring import ExtractivesMonitor

        return run_tool("create_extractives_monitor", lambda conn: ExtractivesMonitor(conn).create(
            namespace, request_key, watch=watch, principal_id=who()[0], scopes=who()[1], delivery=delivery),
            write=True)

    @mcp.tool()
    def run_extractives_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate an extractives monitor at a committed watermark; notices cite the record revision and release."""
        from src.kb.extractives_monitoring import ExtractivesMonitor

        return run_tool("run_extractives_monitor", lambda conn: ExtractivesMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True)
