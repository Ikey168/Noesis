"""Economics labour-statistics feature's entry points: labour indicators for a place, sector or occupation as of a
release vintage, series history, comparability notes, reviewable identity, citation links and subscription monitors.

Acquisition runs through the shared source-pack tools (pack ``economic-statistics-and-filings``: sources
``ilostat-labour-indicators``, ``oecd-labour-statistics``, ``eurostat-lfs-labour`` and ``bls-public-data-api``).
Values sit in the Economics series storage; every value is cited with its source, series key, vintage and retrieval
time, and seasonal adjustment and flags are always shown.

Exclusions (declared by every answering tool): no nowcasting, no labour-market forecasts, no filled periods, no
blending or re-harmonisation of series across sources or definition bases, and no derived indicators.
"""

READ = "knowledge:labour:read"
WRITE = "knowledge:labour:write"
REVIEW = "knowledge:labour:review"
GEO_READ = "knowledge:geospatial:read"
DEMOGRAPHICS_READ = "knowledge:demographics:read"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
EXCLUSIONS_NOTE = (
    "Exclusions: no nowcasting and no labour-market forecasts, no filled periods, no blending or "
    "re-harmonisation across sources or definition bases."
)

LABOUR_WRITES = {
    "import_labour_concordance",
    "propose_labour_place_matches",
    "propose_labour_classification_matches",
    "review_labour_identity_match",
    "revert_labour_identity_match",
    "link_labour_citations",
    "record_labour_comparability",
    "review_labour_comparability",
    "create_labour_monitor",
    "run_labour_monitor",
}
LABOUR_READS = {
    "labour_source_contracts",
    "labour_readiness",
    "list_labour_series",
    "labour_indicators_for_place",
    "labour_series_history",
    "labour_comparability_notes",
    "list_labour_identity_assertions",
    "list_labour_links",
    "poll_labour_monitor",
}
LABOUR_TOOLS = LABOUR_WRITES | LABOUR_READS
LABOUR_SCOPES = {
    "labour_source_contracts": [],
    "labour_readiness": [READ],
    "list_labour_series": [READ],
    "labour_indicators_for_place": [READ],
    "labour_series_history": [READ],
    "labour_comparability_notes": [READ],
    "list_labour_identity_assertions": [READ],
    "list_labour_links": [READ],
    "poll_labour_monitor": [READ, SUBSCRIPTIONS_READ],
    "import_labour_concordance": [WRITE],
    "propose_labour_place_matches": [WRITE, GEO_READ],
    "propose_labour_classification_matches": [WRITE],
    "review_labour_identity_match": [REVIEW],
    "revert_labour_identity_match": [REVIEW],
    "link_labour_citations": [WRITE, DEMOGRAPHICS_READ],
    "record_labour_comparability": [WRITE],
    "review_labour_comparability": [REVIEW],
    "create_labour_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_labour_monitor": [READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return LABOUR_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _require(scopes, *required):
    from src.kb.labour_statistics import LabourError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise LabourError("unauthorized", f"{', '.join(missing)} required")


def _declared(answer):
    from src.ingestion.labour_sources import EXCLUSIONS

    return {**answer, "exclusions": list(EXCLUSIONS)} if isinstance(answer, dict) else answer


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def run_tool(tool, operation, *, write=False):
        """Checks every declared scope, runs the operation and declares the exclusions on the answer."""
        scopes = LABOUR_SCOPES[tool]

        def run(conn):
            _require(who()[1], *scopes)
            return _declared(operation(conn))

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def labour_source_contracts() -> dict:
        """Per-provider access decisions (ILOSTAT, OECD, Eurostat LFS, BLS), keys, limits, terms, indicators in
        scope, definition basis, seasonal adjustment, release signals and the bounded coverage.
        Exclusions: no nowcasting and no labour-market forecasts, no filled periods, no blending or
        re-harmonisation across sources or definition bases."""
        from src.ingestion.labour_sources import (
            BOUNDED_COVERAGE,
            EXCLUSIONS,
            LIVE_VERIFICATION,
            NEVER_SENTENCE,
            PROVIDER_CONTRACTS,
        )

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "bounded_coverage": BOUNDED_COVERAGE, "never": NEVER_SENTENCE, "exclusions": list(EXCLUSIONS)}

    @mcp.tool()
    def labour_readiness() -> dict:
        """Whether the labour-statistics feature is selected, the stores and per-provider releases."""
        from src.kb.labour_statistics import readiness

        return run_tool("labour_readiness", readiness)

    @mcp.tool()
    def list_labour_series(namespace: str, provider: str | None = None, concept: str | None = None,
                           area_code: str | None = None, seasonal_adjustment: str | None = None,
                           definition_basis: str | None = None) -> dict:
        """Labour series with source, native key, concept, seasonal adjustment, definition basis, estimate type,
        place and sector/occupation codes. ILO modelled estimates and national series are separate series."""
        from src.kb.labour_statistics import LabourStore, authorize

        def op(conn):
            authorize(namespace, who()[1], READ)
            return {"series": LabourStore(conn, initialize=False).find_series(
                namespace, provider=provider, concept=concept, area_codes=[area_code] if area_code else None,
                seasonal_adjustment=seasonal_adjustment, definition_basis=definition_basis)}

        return run_tool("list_labour_series", op)

    @mcp.tool()
    def labour_indicators_for_place(namespace: str, place: str | dict | None = None, sector: dict | None = None,
                                    occupation: dict | None = None, concept: str | None = None,
                                    as_of_ms: int | None = None, history: bool = False,
                                    period_from: str | None = None, period_to: str | None = None) -> dict:
        """Published labour indicators for a place (place id or {scheme, code}), sector or occupation ({scheme,
        version, code}) as of a release date: each source side by side with definition, seasonal adjustment,
        vintage, flags, gaps and comparability notes; every value cites source, series key, vintage and retrieval.
        Exclusions: no nowcasting and no labour-market forecasts, no filled periods, no blending or
        re-harmonisation across sources or definition bases."""
        from src.kb.labour_statistics import LabourQueries

        return run_tool("labour_indicators_for_place", lambda conn: LabourQueries(conn).indicators(
            namespace, scopes=who()[1], place=place, sector=sector, occupation=occupation, concept=concept,
            as_of_ms=as_of_ms, history=history, period_from=period_from, period_to=period_to))

    @mcp.tool()
    def labour_series_history(namespace: str, series_id: str) -> dict:
        """Every retained vintage of a labour series with its changes (new periods, revisions, benchmark
        revisions, definition changes), release and retrieval clocks and values as published.
        Exclusions: no nowcasting and no labour-market forecasts, no filled periods, no blending or
        re-harmonisation across sources or definition bases."""
        from src.kb.labour_statistics import LabourQueries

        return run_tool("labour_series_history", lambda conn: LabourQueries(conn).history(
            namespace, series_id, scopes=who()[1]))

    @mcp.tool()
    def labour_comparability_notes(namespace: str, series_id: str | None = None,
                                   definition_id: str | None = None) -> dict:
        """Comparability notes (definition basis, age bounds, survey coverage, seasonal adjustment, breaks and
        source notes) attached to series, definitions and periods; notes never merge series.
        Exclusions: no nowcasting and no labour-market forecasts, no filled periods, no blending or
        re-harmonisation across sources or definition bases."""
        from src.kb.labour_statistics import LabourComparability, table_exists

        def op(conn):
            if not table_exists(conn, "labour_comparability"):
                return {"notes": []}
            return {"notes": LabourComparability(conn, initialize=False).notes(
                namespace, scopes=who()[1], series_id=series_id, definition_id=definition_id)}

        return run_tool("labour_comparability_notes", op)

    @mcp.tool()
    def list_labour_identity_assertions(namespace: str, kind: str | None = None, state: str | None = None) -> dict:
        """Place, sector and occupation mappings in force (proposed, ambiguous, unmatched, accepted, rejected,
        reverted), each citing its basis, classification versions and reviewer."""
        from src.kb.labour_identity import LabourIdentity

        return run_tool("list_labour_identity_assertions", lambda conn: {
            "assertions": LabourIdentity(conn, initialize=False).assertions(namespace, scopes=who()[1], kind=kind,
                                                                            state=state)})

    @mcp.tool()
    def list_labour_links(namespace: str, series_id: str | None = None, kind: str | None = None,
                          state: str | None = None) -> dict:
        """Citation links of labour series to definitions, methodology documents and demographic denominators,
        linked by exact identifier or kept as unresolved citations."""
        from src.kb.labour_links import LabourLinks

        return run_tool("list_labour_links", lambda conn: {
            "links": LabourLinks(conn, initialize=False).links(namespace, scopes=who()[1], series_id=series_id,
                                                               kind=kind, state=state)})

    @mcp.tool()
    def poll_labour_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a labour monitor's events (new periods, revisions, benchmark revisions, definition changes)."""
        from src.kb.labour_monitoring import LabourMonitor

        return run_tool("poll_labour_monitor", lambda conn: LabourMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))

    # ------------------------------------------------------------------ writes

    @mcp.tool()
    def import_labour_concordance(namespace: str, table: dict) -> dict:
        """Record a published classification concordance (NACE-ISIC, NAICS-ISIC, SOC-ISCO, ...) with its URL,
        publisher, publication date and file digest; each row exact, partial or one-to-many as published."""
        from src.kb.labour_identity import LabourIdentity

        return run_tool("import_labour_concordance", lambda conn: LabourIdentity(conn).import_concordance(
            namespace, table, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def propose_labour_place_matches(namespace: str, geo_namespace: str = "global") -> dict:
        """Offer every stated area code to Geospatial places by the published code; ambiguous codes stay review
        candidates; nothing is used until reviewed."""
        from src.kb.labour_identity import LabourIdentity

        return run_tool("propose_labour_place_matches", lambda conn: LabourIdentity(conn).propose_places(
            namespace, principal_id=who()[0], scopes=who()[1], geo_namespace=geo_namespace), write=True)

    @mcp.tool()
    def propose_labour_classification_matches(namespace: str, kind: str, target: dict) -> dict:
        """Map every stated sector or occupation code to a target classification and version through published
        concordances only; unmapped codes stay queryable by native code."""
        from src.kb.labour_identity import LabourIdentity

        return run_tool("propose_labour_classification_matches", lambda conn: LabourIdentity(
            conn).propose_classifications(namespace, kind, target, principal_id=who()[0], scopes=who()[1]),
            write=True)

    @mcp.tool()
    def review_labour_identity_match(namespace: str, assertion_id: str, decision: str, reason: str,
                                     place_id: str | None = None) -> dict:
        """Accept or reject a proposed mapping with a reason (an ambiguous place by choosing a cited candidate)."""
        from src.kb.labour_identity import LabourIdentity

        return run_tool("review_labour_identity_match", lambda conn: LabourIdentity(conn).review(
            namespace, assertion_id, decision, reason, principal_id=who()[0], scopes=who()[1], place_id=place_id),
            write=True)

    @mcp.tool()
    def revert_labour_identity_match(namespace: str, assertion_id: str, reason: str) -> dict:
        """Revert a reviewed mapping; the code is unmapped again."""
        from src.kb.labour_identity import LabourIdentity

        return run_tool("revert_labour_identity_match", lambda conn: LabourIdentity(conn).revert(
            namespace, assertion_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def link_labour_citations(namespace: str, legal_namespace: str = "global",
                              demographic_namespace: str = "global") -> dict:
        """Resolve stated references (exact URL or CELEX) and named demographic denominators; unresolvable ones
        stay as citations; never linked by name similarity.
        Exclusions: no nowcasting and no labour-market forecasts, no filled periods, no blending or
        re-harmonisation across sources or definition bases."""
        from src.kb.labour_links import LabourLinks

        def op(conn):
            links = LabourLinks(conn)
            return {"references": links.link_references(namespace, principal_id=who()[0], scopes=who()[1],
                                                        legal_namespace=legal_namespace),
                    "denominators": links.link_denominators(namespace, principal_id=who()[0], scopes=who()[1],
                                                            demographic_namespace=demographic_namespace)}

        return run_tool("link_labour_citations", op, write=True)

    @mcp.tool()
    def record_labour_comparability(namespace: str, left: dict, relation: str, statement: str,
                                    right: dict | None = None, periods: list[str] | None = None) -> dict:
        """Propose a typed comparability note between series or definitions (a break or source note may name one
        series), optionally for periods; notes never merge series."""
        from src.kb.labour_statistics import LabourComparability

        return run_tool("record_labour_comparability", lambda conn: LabourComparability(conn).record(
            namespace, left, right, relation, statement, principal_id=who()[0], scopes=who()[1],
            periods=periods or []), write=True)

    @mcp.tool()
    def review_labour_comparability(namespace: str, note_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a proposed comparability note with a reason."""
        from src.kb.labour_statistics import LabourComparability

        return run_tool("review_labour_comparability", lambda conn: LabourComparability(conn).review(
            namespace, note_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def create_labour_monitor(namespace: str, request_key: str, target: dict, delivery: dict | None = None) -> dict:
        """Subscribe to a labour series, or a place, sector or occupation with an indicator filter: notices for
        new periods, revised values, benchmark revisions and definition changes (record changes, no forecasts)."""
        from src.kb.labour_monitoring import LabourMonitor

        return run_tool("create_labour_monitor", lambda conn: LabourMonitor(conn).create(
            namespace, request_key, target=target, principal_id=who()[0], scopes=who()[1], delivery=delivery),
            write=True)

    @mcp.tool()
    def run_labour_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a labour monitor at a committed watermark; notices cite the vintages before and after."""
        from src.kb.labour_monitoring import LabourMonitor

        return run_tool("run_labour_monitor", lambda conn: LabourMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True)
