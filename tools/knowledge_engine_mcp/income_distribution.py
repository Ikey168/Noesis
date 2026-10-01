"""Society bundle ``society.income`` entry points: income, poverty and inequality figures for a place as of a release,
series history across releases and PPP revisions, comparability notes, reviewable identity, citation links,
subscription monitors and a cited evidence-bundle export.

Acquisition runs through the shared source-pack tools (pack ``society-statistics``: sources
``worldbank-pip-poverty-inequality``, ``eurostat-eu-silc-income`` and ``oecd-income-distribution-database``). Values
sit in the Economics series storage; every value is cited with its source, series key, vintage, definition revision
and as-of time, and the welfare concept, equivalence scale, poverty line and PPP round are always shown.

Exclusions (declared by every answering tool): no nowcasting, no filled years, no own poverty lines, no blending of
PIP, EU-SILC and OECD figures, no re-harmonisation of welfare concepts. Minimisation: aggregate published statistics
only; an output carrying a person- or household-level field is refused.
"""

READ = "knowledge:income:read"
WRITE = "knowledge:income:write"
REVIEW = "knowledge:income:review"
GEO_READ = "knowledge:geospatial:read"
DEMOGRAPHICS_READ = "knowledge:demographics:read"
LABOUR_READ = "knowledge:labour:read"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
EXCLUSIONS_NOTE = (
    "Exclusions: no nowcasting, no filled years, no own poverty lines, no blending of PIP, EU-SILC and OECD figures, "
    "no re-harmonisation of welfare concepts; aggregate statistics only (no person-level data)."
)

INCOME_WRITES = {
    "propose_income_place_matches",
    "propose_income_related_indicators",
    "review_income_identity_match",
    "revert_income_identity_match",
    "link_income_records",
    "record_income_comparability",
    "review_income_comparability",
    "create_income_monitor",
    "run_income_monitor",
}
INCOME_READS = {
    "income_source_contracts",
    "income_readiness",
    "list_income_series",
    "income_indicator_for_place",
    "income_series_history",
    "income_comparability_notes",
    "list_income_identity_assertions",
    "list_income_links",
    "poll_income_monitor",
    "export_income_evidence_bundle",
}
INCOME_TOOLS = INCOME_WRITES | INCOME_READS
INCOME_SCOPES = {
    "income_source_contracts": [],
    "income_readiness": [READ],
    "list_income_series": [READ],
    "income_indicator_for_place": [READ],
    "income_series_history": [READ],
    "income_comparability_notes": [READ],
    "list_income_identity_assertions": [READ],
    "list_income_links": [READ],
    "poll_income_monitor": [READ, SUBSCRIPTIONS_READ],
    "export_income_evidence_bundle": [READ],
    "propose_income_place_matches": [WRITE, GEO_READ],
    "propose_income_related_indicators": [WRITE],
    "review_income_identity_match": [REVIEW],
    "revert_income_identity_match": [REVIEW],
    "link_income_records": [WRITE, DEMOGRAPHICS_READ, LABOUR_READ],
    "record_income_comparability": [WRITE],
    "review_income_comparability": [REVIEW],
    "create_income_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_income_monitor": [READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return INCOME_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _require(scopes, *required):
    from src.kb.income_distribution_records import IncomeError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise IncomeError("unauthorized", f"{', '.join(missing)} required")


def _declared(answer):
    """Declare the exclusions and the minimisation decision, and refuse any person-level field."""
    from src.ingestion.income_distribution_sources import EXCLUSIONS, MINIMISATION
    from src.kb.income_distribution_records import minimised

    if not isinstance(answer, dict):
        return answer
    return minimised({**answer, "exclusions": list(EXCLUSIONS), "minimisation": MINIMISATION["decision"]})


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def run_tool(tool, operation, *, write=False):
        """Checks every declared scope, runs the operation and declares exclusions and minimisation."""
        scopes = INCOME_SCOPES[tool]

        def run(conn):
            _require(who()[1], *scopes)
            return _declared(operation(conn))

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def income_source_contracts() -> dict:
        """Per-provider access decisions (World Bank PIP, Eurostat EU-SILC, OECD IDD), endpoints, limits, terms,
        welfare concepts, equivalence scales, poverty lines, revision models, the bounded coverage and the
        data-minimisation decision.
        Exclusions: no nowcasting, no filled years, no own poverty lines, no blending of PIP, EU-SILC and OECD
        figures, no re-harmonisation of welfare concepts; aggregate statistics only (no person-level data)."""
        from src.ingestion.income_distribution_sources import (
            BOUNDED_COVERAGE,
            LIVE_VERIFICATION,
            MINIMISATION,
            NEVER_SENTENCE,
            PROVIDER_CONTRACTS,
            SOURCE_FEATURES,
        )

        return _declared({"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                          "features": SOURCE_FEATURES, "bounded_coverage": BOUNDED_COVERAGE,
                          "minimisation_decision": MINIMISATION, "never": NEVER_SENTENCE})

    @mcp.tool()
    def income_readiness() -> dict:
        """Stores, per-provider releases, optional-feature selection (pip, eu-silc, oecd-idd, demographics-links,
        labour-links) and live verification, offline and live evidence kept apart."""
        from src.kb.income_distribution_store import readiness

        return run_tool("income_readiness", readiness)

    @mcp.tool()
    def list_income_series(namespace: str, provider: str | None = None, concept: str | None = None,
                           welfare_concept: str | None = None) -> dict:
        """Income series with source, native key, welfare concept, equivalence scale, poverty line, PPP base year,
        reference-year basis, survey, coverage and area. Different sources, lines and PPP rounds are separate series."""
        from src.kb.income_distribution_records import authorize
        from src.kb.income_distribution_store import IncomeStore

        def op(conn):
            authorize(namespace, who()[1], READ)
            return {"series": IncomeStore(conn, initialize=False).find_series(
                namespace, provider=provider, concept=concept, welfare_concept=welfare_concept)}

        return run_tool("list_income_series", op)

    @mcp.tool()
    def income_indicator_for_place(namespace: str, place: str | dict, concept: str | None = None,
                                   provider: str | None = None, as_of_ms: int | None = None,
                                   period_from: str | None = None, period_to: str | None = None,
                                   history: bool = False) -> dict:
        """Published income, poverty and inequality figures for a place (place id or {scheme, code}) as of a release
        date: each source side by side with welfare concept, equivalence scale, poverty line, PPP round, definition
        and vintage; different lines, PPP rounds and welfare concepts are separate groups; every value cites source,
        vintage, definition and as-of time.
        Exclusions: no nowcasting, no filled years, no own poverty lines, no blending of PIP, EU-SILC and OECD
        figures, no re-harmonisation of welfare concepts; aggregate statistics only (no person-level data)."""
        from src.kb.income_distribution_queries import IncomeQueries

        return run_tool("income_indicator_for_place", lambda conn: IncomeQueries(conn).indicator_for_place(
            namespace, scopes=who()[1], place=place, concept=concept, provider=provider, as_of_ms=as_of_ms,
            period_from=period_from, period_to=period_to, history=history))

    @mcp.tool()
    def income_series_history(namespace: str, series_id: str) -> dict:
        """Every retained vintage of an income series with release dates, new, revised and removed reference years,
        PPP revisions, breaks and pairwise comparability (comparability_unknown where nothing is noted).
        Exclusions: no nowcasting, no filled years, no own poverty lines, no blending of PIP, EU-SILC and OECD
        figures, no re-harmonisation of welfare concepts; aggregate statistics only (no person-level data)."""
        from src.kb.income_distribution_queries import IncomeQueries

        return run_tool("income_series_history", lambda conn: IncomeQueries(conn).history(
            namespace, series_id, scopes=who()[1]))

    @mcp.tool()
    def income_comparability_notes(namespace: str, series_id: str | None = None,
                                   definition_id: str | None = None) -> dict:
        """Comparability notes (welfare concept, equivalence scale, poverty line, PPP round, survey, methodology,
        breaks and source notes) attached to series, definitions and periods; notes never merge series.
        Exclusions: no nowcasting, no filled years, no own poverty lines, no blending of PIP, EU-SILC and OECD
        figures, no re-harmonisation of welfare concepts; aggregate statistics only (no person-level data)."""
        from src.kb.income_distribution_records import table_exists
        from src.kb.income_distribution_store import IncomeComparability

        def op(conn):
            if not table_exists(conn, "income_comparability"):
                return {"notes": []}
            return {"notes": IncomeComparability(conn, initialize=False).notes(
                namespace, scopes=who()[1], series_id=series_id, definition_id=definition_id)}

        return run_tool("income_comparability_notes", op)

    @mcp.tool()
    def list_income_identity_assertions(namespace: str, kind: str | None = None, state: str | None = None) -> dict:
        """Place mappings (by ISO, NUTS and World Bank region codes) and related-indicator assertions in force
        (proposed, ambiguous, unmatched, accepted, rejected, reverted), each citing its basis and reviewer."""
        from src.kb.income_distribution_identity import IncomeIdentity
        from src.kb.income_distribution_records import table_exists

        def op(conn):
            if not table_exists(conn, "income_identity_assertions"):
                return {"assertions": []}
            return {"assertions": IncomeIdentity(conn, initialize=False).assertions(namespace, scopes=who()[1],
                                                                                    kind=kind, state=state)}

        return run_tool("list_income_identity_assertions", op)

    @mcp.tool()
    def list_income_links(namespace: str, series_id: str | None = None, kind: str | None = None,
                          state: str | None = None) -> dict:
        """Links of income series to methodology documents, Demographics denominators and Labour indicators, each
        with its basis (citation, shared identifier or accepted match) and the record revisions it points at."""
        from src.kb.income_distribution_links import IncomeLinks
        from src.kb.income_distribution_records import table_exists

        def op(conn):
            if not table_exists(conn, "income_links"):
                return {"links": []}
            return {"links": IncomeLinks(conn, initialize=False).links(namespace, scopes=who()[1],
                                                                       series_id=series_id, kind=kind, state=state)}

        return run_tool("list_income_links", op)

    @mcp.tool()
    def poll_income_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll an income monitor's events (new releases, new reference years, revisions, PPP revisions, definition
        changes, withdrawals)."""
        from src.kb.income_distribution_monitoring import IncomeMonitor

        return run_tool("poll_income_monitor", lambda conn: IncomeMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))

    @mcp.tool()
    def export_income_evidence_bundle(namespace: str, place: str | dict, concept: str | None = None,
                                      provider: str | None = None, as_of_ms: int | None = None) -> dict:
        """Export an indicator-for-place answer as a noesis-evidence-bundle-v1: every value cited with source, record
        revision (vintage and definition) and as-of time; withheld cells and unavailable series are omissions.
        Exclusions: no nowcasting, no filled years, no own poverty lines, no blending of PIP, EU-SILC and OECD
        figures, no re-harmonisation of welfare concepts; aggregate statistics only (no person-level data)."""
        from src.kb.income_distribution_queries import IncomeQueries

        def op(conn):
            queries = IncomeQueries(conn)
            answer = queries.indicator_for_place(namespace, scopes=who()[1], place=place, concept=concept,
                                                 provider=provider, as_of_ms=as_of_ms)
            return {"bundle": queries.export_bundle(answer), "status": answer["status"]}

        return run_tool("export_income_evidence_bundle", op)

    # ------------------------------------------------------------------ writes

    @mcp.tool()
    def propose_income_place_matches(namespace: str, geo_namespace: str = "global") -> dict:
        """Offer every stated area code (ISO, NUTS, World Bank region) to Geospatial places by the published code;
        ambiguous codes stay review candidates, unmatched codes stay visible; nothing is used until reviewed."""
        from src.kb.income_distribution_identity import IncomeIdentity

        return run_tool("propose_income_place_matches", lambda conn: IncomeIdentity(conn).propose_places(
            namespace, principal_id=who()[0], scopes=who()[1], geo_namespace=geo_namespace), write=True)

    @mcp.tool()
    def propose_income_related_indicators(namespace: str) -> dict:
        """Propose the same indicator from different sources for the same place as related, citing the recorded
        differences; related series are shown beside each other and never merged."""
        from src.kb.income_distribution_identity import IncomeIdentity

        return run_tool("propose_income_related_indicators", lambda conn: IncomeIdentity(conn).propose_related(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_income_identity_match(namespace: str, assertion_id: str, decision: str, reason: str,
                                     place_id: str | None = None) -> dict:
        """Accept or reject a proposed mapping or relation with a reason (not one's own; an ambiguous place by
        choosing a cited candidate)."""
        from src.kb.income_distribution_identity import IncomeIdentity

        return run_tool("review_income_identity_match", lambda conn: IncomeIdentity(conn).review(
            namespace, assertion_id, decision, reason, principal_id=who()[0], scopes=who()[1], place_id=place_id),
            write=True)

    @mcp.tool()
    def revert_income_identity_match(namespace: str, assertion_id: str, reason: str) -> dict:
        """Revert a reviewed mapping or relation; the code is unmapped again."""
        from src.kb.income_distribution_identity import IncomeIdentity

        return run_tool("revert_income_identity_match", lambda conn: IncomeIdentity(conn).revert(
            namespace, assertion_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def link_income_records(namespace: str, demographic_namespace: str = "global",
                            labour_namespace: str = "global") -> dict:
        """Resolve stated methodology references (exact URL), named Demographics denominators and Labour series of
        the same place and period; missing providers and targets are reported, never dropped.
        Exclusions: no nowcasting, no filled years, no own poverty lines, no blending of PIP, EU-SILC and OECD
        figures, no re-harmonisation of welfare concepts; aggregate statistics only (no person-level data)."""
        from src.kb.income_distribution_links import IncomeLinks

        def op(conn):
            links = IncomeLinks(conn)
            return {"references": links.link_references(namespace, principal_id=who()[0], scopes=who()[1]),
                    "demographics": links.link_demographics(namespace, principal_id=who()[0], scopes=who()[1],
                                                            demographic_namespace=demographic_namespace),
                    "labour": links.link_labour(namespace, principal_id=who()[0], scopes=who()[1],
                                                labour_namespace=labour_namespace)}

        return run_tool("link_income_records", op, write=True)

    @mcp.tool()
    def record_income_comparability(namespace: str, left: dict, relation: str, statement: str,
                                    right: dict | None = None, periods: list[str] | None = None) -> dict:
        """Propose a typed comparability note between series or definitions (a break, PPP revision or source note
        may name one series), optionally for periods; notes never merge series."""
        from src.kb.income_distribution_store import IncomeComparability

        return run_tool("record_income_comparability", lambda conn: IncomeComparability(conn).record(
            namespace, left, right, relation, statement, principal_id=who()[0], scopes=who()[1],
            periods=periods or []), write=True)

    @mcp.tool()
    def review_income_comparability(namespace: str, note_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a proposed comparability note with a reason (not one's own)."""
        from src.kb.income_distribution_store import IncomeComparability

        return run_tool("review_income_comparability", lambda conn: IncomeComparability(conn).review(
            namespace, note_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def create_income_monitor(namespace: str, request_key: str, target: dict, delivery: dict | None = None) -> dict:
        """Subscribe to an income series, a place or an indicator concept (optionally one provider): notices for new
        releases, new reference years, revisions, PPP revisions, definition changes and withdrawals (record
        changes, no assessments)."""
        from src.kb.income_distribution_monitoring import IncomeMonitor

        return run_tool("create_income_monitor", lambda conn: IncomeMonitor(conn).create(
            namespace, request_key, target=target, principal_id=who()[0], scopes=who()[1], delivery=delivery),
            write=True)

    @mcp.tool()
    def run_income_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate an income monitor at a committed watermark; notices cite the new and the revised vintage."""
        from src.kb.income_distribution_monitoring import IncomeMonitor

        return run_tool("run_income_monitor", lambda conn: IncomeMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True)
