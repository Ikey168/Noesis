"""Economics business-statistics feature's entry points (``economics.business``, #2738): a business indicator for a
place as of a release, places side by side, series history, comparability notes, reviewable place and NACE/NAICS
identity, citation links to Labour and Trade, and subscription monitors.

Acquisition runs through the shared source-pack tools (pack ``economic-statistics-and-filings``: sources
``eurostat-sts``, ``eurostat-business-demography`` and ``us-census-cbp``). Values sit in the Economics series storage;
every value is cited with its source, series key, vintage, release and retrieval time, and flags, statistical unit,
classification, adjustment and base year are always shown.

Exclusions (declared by every answering tool): no nowcasting, no filled periods, no re-based indices, no own seasonal
adjustment, no blending of Eurostat and Census figures, no reconstruction of suppressed or noise-infused cells, no
rates, shares or per-establishment figures of our own, no derived indicators and no forecasts.
"""

READ = "knowledge:business:read"
WRITE = "knowledge:business:write"
REVIEW = "knowledge:business:review"
GEO_READ = "knowledge:geospatial:read"
LABOUR_READ = "knowledge:labour:read"
TRADE_READ = "knowledge:trade:read"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
EXCLUSIONS_NOTE = (
    "Exclusions: no nowcasting, no filled periods, no re-based indices or own seasonal adjustment, no blending of "
    "Eurostat and Census figures, no reconstructed suppressed cells, no derived figures and no forecasts."
)

BUSINESS_WRITES = {
    "import_business_concordance",
    "propose_business_place_matches",
    "propose_business_classification_links",
    "review_business_identity_match",
    "revert_business_identity_match",
    "link_business_series",
    "record_business_comparability",
    "review_business_comparability",
    "create_business_monitor",
    "run_business_monitor",
}
BUSINESS_READS = {
    "business_source_contracts",
    "business_readiness",
    "list_business_series",
    "business_indicator_for_place",
    "compare_business_places",
    "business_series_history",
    "business_comparability_notes",
    "list_business_identity_assertions",
    "list_business_links",
    "poll_business_monitor",
}
BUSINESS_TOOLS = BUSINESS_WRITES | BUSINESS_READS
BUSINESS_SCOPES = {
    "business_source_contracts": [],
    "business_readiness": [READ],
    "list_business_series": [READ],
    "business_indicator_for_place": [READ],
    "compare_business_places": [READ],
    "business_series_history": [READ],
    "business_comparability_notes": [READ],
    "list_business_identity_assertions": [READ],
    "list_business_links": [READ],
    "poll_business_monitor": [READ, SUBSCRIPTIONS_READ],
    "import_business_concordance": [WRITE],
    "propose_business_place_matches": [WRITE, GEO_READ],
    "propose_business_classification_links": [WRITE],
    "review_business_identity_match": [REVIEW],
    "revert_business_identity_match": [REVIEW],
    "link_business_series": [WRITE, LABOUR_READ, TRADE_READ],
    "record_business_comparability": [WRITE],
    "review_business_comparability": [REVIEW],
    "create_business_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_business_monitor": [READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return BUSINESS_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _require(scopes, *required):
    from src.kb.business_statistics_records import BusinessError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise BusinessError("unauthorized", f"{', '.join(missing)} required")


def _declared(answer):
    """Every answer leaves with the exclusions declared and is checked against the minimisation decision."""
    from src.ingestion.business_statistics_sources import EXCLUSIONS
    from src.kb.business_statistics_records import (
        BusinessError,
        forbidden_paths,
        personal_data_paths,
    )

    if not isinstance(answer, dict):
        return answer
    if personal_data_paths(answer) or forbidden_paths(answer):
        raise BusinessError("minimisation", "an answer would carry a personal, firm-level or derived field")
    return {**answer, "exclusions": list(EXCLUSIONS)}


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def run_tool(tool, operation, *, write=False):
        """Checks every declared scope, runs the operation and declares the exclusions on the answer."""
        scopes = BUSINESS_SCOPES[tool]

        def run(conn):
            _require(who()[1], *scopes)
            return _declared(operation(conn))

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def business_source_contracts() -> dict:
        """Per-provider access decisions (Eurostat STS, Eurostat business demography, US Census CBP), keys, limits,
        terms, revision models, CBP disclosure protection, caps and the bounded coverage; every provider is
        unverified-live until a dated live run.
        Exclusions: no nowcasting, no filled periods, no re-based indices or own seasonal adjustment, no blending of
        Eurostat and Census figures, no reconstructed suppressed cells, no derived figures and no forecasts."""
        from src.ingestion.business_statistics_sources import (
            BOUNDED_COVERAGE,
            CAPS,
            EXCLUSIONS,
            LIVE_VERIFICATION,
            NEVER_SENTENCE,
            PROVIDER_CONTRACTS,
        )
        from src.kb.business_statistics_records import MINIMISATION

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION, "caps": CAPS,
                "bounded_coverage": BOUNDED_COVERAGE, "minimisation": MINIMISATION, "never": NEVER_SENTENCE,
                "exclusions": list(EXCLUSIONS)}

    @mcp.tool()
    def business_readiness() -> dict:
        """Whether the business-statistics feature is selected, the stores, per-provider releases and staleness."""
        from src.kb.business_statistics_records import readiness

        return run_tool("business_readiness", readiness)

    @mcp.tool()
    def list_business_series(namespace: str, provider: str | None = None, concept: str | None = None,
                             area: dict | None = None, classification: dict | None = None) -> dict:
        """Business series with source, dataset, indicator, classification and version, size class, place,
        adjustment, unit and base year and statistical unit; adjusted, unadjusted and rebased series are separate."""
        from src.kb.business_statistics_records import authorize
        from src.kb.business_statistics_store import BusinessStatisticsStore

        def op(conn):
            authorize(namespace, who()[1], READ)
            codes = [(area["scheme"], str(area["code"]))] if area else None
            return {"series": BusinessStatisticsStore(conn, initialize=False).find_series(
                namespace, provider=provider, concept=concept, area_codes=codes, classification=classification)}

        return run_tool("list_business_series", op)

    @mcp.tool()
    def business_indicator_for_place(namespace: str, place: str | dict, concept: str | None = None,
                                     classification: dict | None = None, as_of: str | int | None = None,
                                     period_from: str | None = None, period_to: str | None = None) -> dict:
        """Published business statistics for a place (place id or {scheme, code}) and optional concept and
        classification ({scheme, version, code}) as released by a date: each source side by side with definition,
        statistical unit, classification, adjustment, base year, vintage, flags, withheld periods and comparability
        notes; every value cites source, series key, vintage and retrieval.
        Exclusions: no nowcasting, no filled periods, no re-based indices or own seasonal adjustment, no blending of
        Eurostat and Census figures, no reconstructed suppressed cells, no derived figures and no forecasts."""
        from src.kb.business_statistics_queries import BusinessQueries

        return run_tool("business_indicator_for_place", lambda conn: BusinessQueries(conn).indicator_for_place(
            namespace, scopes=who()[1], place=place, concept=concept, classification=classification, as_of=as_of,
            period_from=period_from, period_to=period_to))

    @mcp.tool()
    def compare_business_places(namespace: str, places: list, concept: str | None = None,
                                classification: dict | None = None, classifications: dict | None = None,
                                as_of: str | int | None = None) -> dict:
        """Several places side by side (e.g. Germany through Eurostat, California through CBP), each with its own
        sources, statistical units and classifications, and the recorded differences per pair; never blended.
        Exclusions: no nowcasting, no filled periods, no re-based indices or own seasonal adjustment, no blending of
        Eurostat and Census figures, no reconstructed suppressed cells, no derived figures and no forecasts."""
        from src.kb.business_statistics_queries import BusinessQueries

        return run_tool("compare_business_places", lambda conn: BusinessQueries(conn).compare_places(
            namespace, scopes=who()[1], places=places, concept=concept, classification=classification,
            classifications=classifications, as_of=as_of))

    @mcp.tool()
    def business_series_history(namespace: str, series_id: str) -> dict:
        """Every retained vintage of a business series with what each release added, revised or dropped (values
        and flags before and after), definition changes, removals, and the comparability notes per release pair
        (provisional periods confirmed, breaks, base-year changes, NAICS vintages).
        Exclusions: no nowcasting, no filled periods, no re-based indices or own seasonal adjustment, no blending of
        Eurostat and Census figures, no reconstructed suppressed cells, no derived figures and no forecasts."""
        from src.kb.business_statistics_queries import BusinessQueries

        return run_tool("business_series_history", lambda conn: BusinessQueries(conn).series_history(
            namespace, series_id, scopes=who()[1]))

    @mcp.tool()
    def business_comparability_notes(namespace: str, series_id: str, include_inactive: bool = False) -> dict:
        """Comparability notes of a series: source-stated breaks, provisional periods and base-year changes, and
        reviewer notes; notes never merge series."""
        from src.kb.business_statistics_records import authorize
        from src.kb.business_statistics_store import BusinessStatisticsStore

        def op(conn):
            authorize(namespace, who()[1], READ)
            return {"notes": BusinessStatisticsStore(conn, initialize=False).notes(
                namespace, series_id, active_only=not include_inactive)}

        return run_tool("business_comparability_notes", op)

    @mcp.tool()
    def list_business_identity_assertions(namespace: str, kind: str | None = None, state: str | None = None) -> dict:
        """Place matches and NACE/NAICS candidate links in force (proposed, ambiguous, unmatched, accepted,
        rejected, reverted), each citing its basis, concordance rows and reviewer."""
        from src.kb.business_statistics_identity import BusinessIdentity

        return run_tool("list_business_identity_assertions", lambda conn: {
            "assertions": BusinessIdentity(conn, initialize=False).assertions(namespace, scopes=who()[1], kind=kind,
                                                                              state=state)})

    @mcp.tool()
    def list_business_links(namespace: str, series_id: str | None = None, kind: str | None = None,
                            state: str | None = None) -> dict:
        """Links of business series to Labour and Trade series (shared code or accepted place match) and to
        methodology documents (exact URL), each pinning both revisions; absent providers and targets reported."""
        from src.kb.business_statistics_links import BusinessLinks

        return run_tool("list_business_links", lambda conn: {
            "links": BusinessLinks(conn, initialize=False).links(namespace, scopes=who()[1], series_id=series_id,
                                                                 kind=kind, state=state)})

    @mcp.tool()
    def poll_business_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a business monitor's events (new releases, new periods, revisions, definition changes, removals)."""
        from src.kb.business_statistics_monitoring import BusinessMonitor

        return run_tool("poll_business_monitor", lambda conn: BusinessMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))

    # ------------------------------------------------------------------ writes

    @mcp.tool()
    def import_business_concordance(namespace: str, table: dict) -> dict:
        """Record a published classification concordance (NACE-ISIC, NAICS-ISIC, 2017-2022 NAICS, ...) with its URL,
        publisher, publication date and file digest; each row exact, partial or one-to-many as published."""
        from src.kb.business_statistics_identity import BusinessIdentity

        return run_tool("import_business_concordance", lambda conn: BusinessIdentity(conn).import_concordance(
            namespace, table, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def propose_business_place_matches(namespace: str, geo_namespace: str = "global") -> dict:
        """Offer every stated area code to Geospatial places by the published code; ambiguous codes stay review
        candidates; nothing is used until reviewed."""
        from src.kb.business_statistics_identity import BusinessIdentity

        return run_tool("propose_business_place_matches", lambda conn: BusinessIdentity(conn).propose_places(
            namespace, principal_id=who()[0], scopes=who()[1], geo_namespace=geo_namespace), write=True)

    @mcp.tool()
    def propose_business_classification_links(namespace: str) -> dict:
        """Propose candidate links between stated NACE and NAICS codes (and NAICS vintages) through published
        concordances only; links are reviewable and never merge series."""
        from src.kb.business_statistics_identity import BusinessIdentity

        return run_tool("propose_business_classification_links", lambda conn: BusinessIdentity(
            conn).propose_classification_links(namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_business_identity_match(namespace: str, assertion_id: str, decision: str, reason: str,
                                       place_id: str | None = None) -> dict:
        """Accept or reject a proposed place match or candidate link with a reason (an ambiguous place by choosing a
        cited candidate)."""
        from src.kb.business_statistics_identity import BusinessIdentity

        return run_tool("review_business_identity_match", lambda conn: BusinessIdentity(conn).review(
            namespace, assertion_id, decision, reason, principal_id=who()[0], scopes=who()[1], place_id=place_id),
            write=True)

    @mcp.tool()
    def revert_business_identity_match(namespace: str, assertion_id: str, reason: str) -> dict:
        """Revert a reviewed place match or candidate link; queries stop using it."""
        from src.kb.business_statistics_identity import BusinessIdentity

        return run_tool("revert_business_identity_match", lambda conn: BusinessIdentity(conn).revert(
            namespace, assertion_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def link_business_series(namespace: str, labour_namespace: str = "global", trade_namespace: str = "global") -> dict:
        """Link business series to Labour and Trade series for the same place (shared code or accepted place match)
        and methodology references by exact URL; never by name similarity, nothing combined.
        Exclusions: no nowcasting, no filled periods, no re-based indices or own seasonal adjustment, no blending of
        Eurostat and Census figures, no reconstructed suppressed cells, no derived figures and no forecasts."""
        from src.kb.business_statistics_links import BusinessLinks

        def op(conn):
            links = BusinessLinks(conn)
            return {"labour": links.link_labour(namespace, principal_id=who()[0], scopes=who()[1],
                                                labour_namespace=labour_namespace),
                    "trade": links.link_trade(namespace, principal_id=who()[0], scopes=who()[1],
                                              trade_namespace=trade_namespace),
                    "methodology": links.link_methodology(namespace, principal_id=who()[0], scopes=who()[1])}

        return run_tool("link_business_series", op, write=True)

    @mcp.tool()
    def record_business_comparability(namespace: str, series_id: str, relation: str, statement: str,
                                      cited: list, other_series_id: str | None = None,
                                      periods: list[str] | None = None) -> dict:
        """Propose a typed, cited comparability note on a series or between two series; notes never merge series."""
        from src.kb.business_statistics_store import BusinessStatisticsStore

        return run_tool("record_business_comparability", lambda conn: BusinessStatisticsStore(conn).record_note(
            namespace, series_id, relation, statement, other_series_id=other_series_id, periods=periods or [],
            cited=cited, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_business_comparability(namespace: str, note_id: str, decision: str, reason: str) -> dict:
        """Accept, reject or revert a proposed comparability note with a reason (reviewed by another principal)."""
        from src.kb.business_statistics_store import BusinessStatisticsStore

        return run_tool("review_business_comparability", lambda conn: BusinessStatisticsStore(conn).review_note(
            namespace, note_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def create_business_monitor(namespace: str, request_key: str, target: dict,
                                delivery: dict | None = None) -> dict:
        """Subscribe to a business series, provider, concept, classification code or place: notices for new
        releases, new periods, revised values, definition changes and removals (record changes, no forecasts)."""
        from src.kb.business_statistics_monitoring import BusinessMonitor

        return run_tool("create_business_monitor", lambda conn: BusinessMonitor(conn).create(
            namespace, request_key, target=target, principal_id=who()[0], scopes=who()[1], delivery=delivery),
            write=True)

    @mcp.tool()
    def run_business_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a business monitor at a committed watermark; notices cite the vintages before and after."""
        from src.kb.business_statistics_monitoring import BusinessMonitor

        return run_tool("run_business_monitor", lambda conn: BusinessMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True)
