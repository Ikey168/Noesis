"""Economics tourism-statistics feature's entry points (``economics.tourism``, #2739): a tourism indicator for a place as
of a release, series history with comparability notes, evidence-bundle export, reviewable place and NUTS-version
identity, links to Geospatial boundaries and Labour series, and subscription monitors.

Acquisition runs through the shared source-pack tools (pack ``economic-statistics-and-filings``: sources
``eurostat-tourism-occupancy`` and ``eurostat-tourism-capacity``). Values sit in the Economics series storage; every
value is cited with its source, series key, vintage, release and retrieval time, and flags, residence, accommodation
type, frequency and NUTS version are always shown. UN Tourism is ``not-implemented`` and reported as such.

Exclusions (declared by every answering tool): no nowcasting, no filled months or regions, no seasonal adjustment of
our own, no blending of Eurostat and UN Tourism figures, no occupancy rates, averages, per-capita or per-bed figures of
our own, no annual totals from months, no derived indicators and no forecasts.
"""

READ = "knowledge:tourism:read"
WRITE = "knowledge:tourism:write"
REVIEW = "knowledge:tourism:review"
GEO_READ = "knowledge:geospatial:read"
LABOUR_READ = "knowledge:labour:read"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
EXCLUSIONS_NOTE = (
    "Exclusions: no nowcasting, no filled months or regions, no own seasonal adjustment, no blending of Eurostat and "
    "UN Tourism figures, no occupancy rates, averages, per-capita or per-bed figures, no derived figures and no "
    "forecasts."
)

TOURISM_WRITES = {
    "import_tourism_nuts_correspondence",
    "propose_tourism_place_matches",
    "propose_tourism_nuts_links",
    "review_tourism_identity_match",
    "revert_tourism_identity_match",
    "link_tourism_series",
    "record_tourism_comparability",
    "review_tourism_comparability",
    "create_tourism_monitor",
    "run_tourism_monitor",
}
TOURISM_READS = {
    "tourism_source_contracts",
    "tourism_readiness",
    "list_tourism_series",
    "tourism_indicator_for_place",
    "tourism_series_history",
    "export_tourism_evidence_bundle",
    "tourism_comparability_notes",
    "list_tourism_identity_assertions",
    "list_tourism_links",
    "poll_tourism_monitor",
}
TOURISM_TOOLS = TOURISM_WRITES | TOURISM_READS
TOURISM_SCOPES = {
    "tourism_source_contracts": [],
    "tourism_readiness": [READ],
    "list_tourism_series": [READ],
    "tourism_indicator_for_place": [READ],
    "tourism_series_history": [READ],
    "export_tourism_evidence_bundle": [READ],
    "tourism_comparability_notes": [READ],
    "list_tourism_identity_assertions": [READ],
    "list_tourism_links": [READ],
    "poll_tourism_monitor": [READ, SUBSCRIPTIONS_READ],
    "import_tourism_nuts_correspondence": [WRITE],
    "propose_tourism_place_matches": [WRITE, GEO_READ],
    "propose_tourism_nuts_links": [WRITE],
    "review_tourism_identity_match": [REVIEW],
    "revert_tourism_identity_match": [REVIEW],
    "link_tourism_series": [WRITE, GEO_READ, LABOUR_READ],
    "record_tourism_comparability": [WRITE],
    "review_tourism_comparability": [REVIEW],
    "create_tourism_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_tourism_monitor": [READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return TOURISM_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _require(scopes, *required):
    from src.kb.tourism_records import TourismError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise TourismError("unauthorized", f"{', '.join(missing)} required")


def _declared(answer):
    """Every answer leaves with the exclusions declared and is checked against the TO01 minimisation decision."""
    from src.ingestion.tourism_sources import EXCLUSIONS
    from src.kb.tourism_records import (
        TourismError,
        forbidden_paths,
        personal_data_paths,
    )

    if not isinstance(answer, dict):
        return answer
    if personal_data_paths(answer) or forbidden_paths(answer):
        raise TourismError("minimisation", "an answer would carry a personal, establishment-level or derived field")
    return {**answer, "exclusions": list(EXCLUSIONS)}


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def run_tool(tool, operation, *, write=False):
        """Checks every declared scope, runs the operation and declares the exclusions on the answer."""
        scopes = TOURISM_SCOPES[tool]

        def run(conn):
            _require(who()[1], *scopes)
            return _declared(operation(conn))

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def tourism_source_contracts() -> dict:
        """Per-source access decisions (Eurostat tourism occupancy and capacity; UN Tourism not-implemented), limits,
        terms, revision models, caps and the bounded coverage; every Eurostat source is unverified-live until a dated
        live run.
        Exclusions: no nowcasting, no filled months or regions, no own seasonal adjustment, no blending of Eurostat and
        UN Tourism figures, no occupancy rates, averages, per-capita or per-bed figures, no derived figures and no
        forecasts."""
        from src.ingestion.tourism_sources import (
            BOUNDED_COVERAGE,
            CAPS,
            EXCLUSIONS,
            LIVE_VERIFICATION,
            NEVER_SENTENCE,
            PROVIDER_CONTRACTS,
        )
        from src.kb.tourism_records import MINIMISATION

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION, "caps": CAPS,
                "bounded_coverage": BOUNDED_COVERAGE, "minimisation": MINIMISATION, "never": NEVER_SENTENCE,
                "exclusions": list(EXCLUSIONS)}

    @mcp.tool()
    def tourism_readiness() -> dict:
        """Which tourism features (one per source) are selected, the stores, per-source releases and staleness."""
        from src.kb.tourism_records import readiness

        return run_tool("tourism_readiness", readiness)

    @mcp.tool()
    def list_tourism_series(namespace: str, provider: str | None = None, concept: str | None = None,
                            frequency: str | None = None, area: dict | None = None) -> dict:
        """Tourism series with dataset, indicator, residence of guest, accommodation type, unit, frequency and place
        with its NUTS version; monthly national and annual NUTS 2 series are separate."""
        from src.kb.tourism_records import authorize
        from src.kb.tourism_store import TourismStore

        def op(conn):
            authorize(namespace, who()[1], READ)
            codes = [(area["scheme"], str(area["code"]), area.get("nuts_version"))] if area else None
            return {"series": TourismStore(conn, initialize=False).find_series(
                namespace, provider=provider, concept=concept, frequency=frequency, area_codes=codes)}

        return run_tool("list_tourism_series", op)

    @mcp.tool()
    def tourism_indicator_for_place(namespace: str, place: str | dict, concept: str | None = None,
                                    frequency: str | None = None, residence: str | None = None,
                                    period: str | None = None, as_of: str | int | None = None,
                                    providers: list[str] | None = None) -> dict:
        """Published tourism statistics for a place (place id or {scheme, code, nuts_version}) as released by a date:
        one row per series with definition, national threshold, flags, confidential cells as their status, vintage
        and citation; monthly and annual series kept apart; UN Tourism reported not-implemented.
        Exclusions: no nowcasting, no filled months or regions, no own seasonal adjustment, no blending of Eurostat and
        UN Tourism figures, no occupancy rates, averages, per-capita or per-bed figures, no derived figures and no
        forecasts."""
        from src.kb.tourism_queries import TourismQueries
        from src.kb.tourism_records import enabled_providers

        return run_tool("tourism_indicator_for_place", lambda conn: TourismQueries(conn).indicator_for_place(
            namespace, scopes=who()[1], place=place, concept=concept, frequency=frequency, residence=residence,
            period=period, as_of=as_of, providers=providers, enabled_providers=enabled_providers(conn)))

    @mcp.tool()
    def tourism_series_history(namespace: str, series_id: str) -> dict:
        """Every retained vintage of a tourism series with what each release added, revised or dropped (values and
        flags before and after, provisional months revised), definition changes, removals, and the source-stated
        comparability notes per release pair (breaks, definition differences, thresholds, NUTS versions).
        Exclusions: no nowcasting, no filled months or regions, no own seasonal adjustment, no blending of Eurostat and
        UN Tourism figures, no occupancy rates, averages, per-capita or per-bed figures, no derived figures and no
        forecasts."""
        from src.kb.tourism_queries import TourismQueries

        return run_tool("tourism_series_history", lambda conn: TourismQueries(conn).series_history(
            namespace, series_id, scopes=who()[1]))

    @mcp.tool()
    def export_tourism_evidence_bundle(namespace: str, place: str | dict, concept: str | None = None,
                                       frequency: str | None = None, as_of: str | int | None = None) -> dict:
        """An evidence bundle for a place's tourism figures: every item cites its source, record revision (vintage)
        and as-of time; confidential cells appear as their status.
        Exclusions: no nowcasting, no filled months or regions, no own seasonal adjustment, no blending of Eurostat and
        UN Tourism figures, no occupancy rates, averages, per-capita or per-bed figures, no derived figures and no
        forecasts."""
        from src.kb.tourism_queries import TourismQueries
        from src.kb.tourism_records import enabled_providers

        def op(conn):
            queries = TourismQueries(conn)
            answer = queries.indicator_for_place(namespace, scopes=who()[1], place=place, concept=concept,
                                                 frequency=frequency, as_of=as_of,
                                                 enabled_providers=enabled_providers(conn))
            return {"status": answer["status"], "evidence_bundle": queries.evidence_bundle(answer)}

        return run_tool("export_tourism_evidence_bundle", op)

    @mcp.tool()
    def tourism_comparability_notes(namespace: str, series_id: str, include_inactive: bool = False) -> dict:
        """Comparability notes of a series: source-stated breaks, provisional periods and definition differences,
        and reviewer notes; notes never merge series."""
        from src.kb.tourism_records import authorize
        from src.kb.tourism_store import TourismStore

        def op(conn):
            authorize(namespace, who()[1], READ)
            return {"notes": TourismStore(conn, initialize=False).notes(namespace, series_id,
                                                                        active_only=not include_inactive)}

        return run_tool("tourism_comparability_notes", op)

    @mcp.tool()
    def list_tourism_identity_assertions(namespace: str, kind: str | None = None, state: str | None = None) -> dict:
        """Place matches and NUTS correspondence links in force (proposed, ambiguous, unmatched, accepted, rejected,
        reverted), each citing its basis and reviewer."""
        from src.kb.tourism_identity import TourismIdentity

        return run_tool("list_tourism_identity_assertions", lambda conn: {
            "assertions": TourismIdentity(conn, initialize=False).assertions(namespace, scopes=who()[1], kind=kind,
                                                                             state=state)})

    @mcp.tool()
    def list_tourism_links(namespace: str, series_id: str | None = None, kind: str | None = None,
                           state: str | None = None) -> dict:
        """Links of tourism series to Geospatial boundary features and Labour section I series (shared code or
        accepted place match), each pinning both revisions; absent providers and targets reported."""
        from src.kb.tourism_links import TourismLinks

        return run_tool("list_tourism_links", lambda conn: {
            "links": TourismLinks(conn, initialize=False).links(namespace, scopes=who()[1], series_id=series_id,
                                                                kind=kind, state=state)})

    @mcp.tool()
    def poll_tourism_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a tourism monitor's events (new releases, new periods, revisions, definition changes, removals)."""
        from src.kb.tourism_monitoring import TourismMonitor

        return run_tool("poll_tourism_monitor", lambda conn: TourismMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))

    # ------------------------------------------------------------------ writes

    @mcp.tool()
    def import_tourism_nuts_correspondence(namespace: str, table: dict) -> dict:
        """Record Eurostat's published NUTS correspondence (e.g. NUTS 2021 to 2024) with its URL, publisher,
        publication date and file digest; each row's relation as published."""
        from src.kb.tourism_identity import TourismIdentity

        return run_tool("import_tourism_nuts_correspondence", lambda conn: TourismIdentity(
            conn).import_correspondence(namespace, table, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def propose_tourism_place_matches(namespace: str, geo_namespace: str = "global") -> dict:
        """Offer every stated place key (code and NUTS version) to Geospatial places by the published code;
        ambiguous keys stay review candidates; nothing is used until reviewed."""
        from src.kb.tourism_identity import TourismIdentity

        return run_tool("propose_tourism_place_matches", lambda conn: TourismIdentity(conn).propose_places(
            namespace, principal_id=who()[0], scopes=who()[1], geo_namespace=geo_namespace), write=True)

    @mcp.tool()
    def propose_tourism_nuts_links(namespace: str) -> dict:
        """Propose links between place keys of different NUTS versions through the imported correspondence only;
        links are reviewable and never merge series."""
        from src.kb.tourism_identity import TourismIdentity

        return run_tool("propose_tourism_nuts_links", lambda conn: TourismIdentity(
            conn).propose_correspondence_links(namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_tourism_identity_match(namespace: str, assertion_id: str, decision: str, reason: str,
                                      place_id: str | None = None) -> dict:
        """Accept or reject a proposed place match or NUTS correspondence link with a reason (an ambiguous place by
        choosing a cited candidate)."""
        from src.kb.tourism_identity import TourismIdentity

        return run_tool("review_tourism_identity_match", lambda conn: TourismIdentity(conn).review(
            namespace, assertion_id, decision, reason, principal_id=who()[0], scopes=who()[1], place_id=place_id),
            write=True)

    @mcp.tool()
    def revert_tourism_identity_match(namespace: str, assertion_id: str, reason: str) -> dict:
        """Revert a reviewed place match or NUTS correspondence link; queries stop using it."""
        from src.kb.tourism_identity import TourismIdentity

        return run_tool("revert_tourism_identity_match", lambda conn: TourismIdentity(conn).revert(
            namespace, assertion_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def link_tourism_series(namespace: str, geo_namespace: str = "global", labour_namespace: str = "global") -> dict:
        """Link tourism series to the Geospatial boundary of their NUTS code (in their NUTS version) and to Labour
        series for NACE Rev.2 section I for the same place (shared code or accepted match); never a ratio.
        Exclusions: no nowcasting, no filled months or regions, no own seasonal adjustment, no blending of Eurostat and
        UN Tourism figures, no occupancy rates, averages, per-capita or per-bed figures, no derived figures and no
        forecasts."""
        from src.kb.tourism_links import TourismLinks

        def op(conn):
            links = TourismLinks(conn)
            return {"boundary": links.link_boundaries(namespace, principal_id=who()[0], scopes=who()[1],
                                                      geo_namespace=geo_namespace),
                    "labour": links.link_labour(namespace, principal_id=who()[0], scopes=who()[1],
                                                labour_namespace=labour_namespace)}

        return run_tool("link_tourism_series", op, write=True)

    @mcp.tool()
    def record_tourism_comparability(namespace: str, series_id: str, relation: str, statement: str,
                                     cited: list, other_series_id: str | None = None,
                                     periods: list[str] | None = None) -> dict:
        """Propose a typed, cited comparability note on a series or between two series; notes never merge series."""
        from src.kb.tourism_store import TourismStore

        return run_tool("record_tourism_comparability", lambda conn: TourismStore(conn).record_note(
            namespace, series_id, relation, statement, other_series_id=other_series_id, periods=periods or [],
            cited=cited, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_tourism_comparability(namespace: str, note_id: str, decision: str, reason: str) -> dict:
        """Accept, reject or revert a proposed comparability note with a reason (reviewed by another principal)."""
        from src.kb.tourism_store import TourismStore

        return run_tool("review_tourism_comparability", lambda conn: TourismStore(conn).review_note(
            namespace, note_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def create_tourism_monitor(namespace: str, request_key: str, target: dict, delivery: dict | None = None) -> dict:
        """Subscribe to a tourism series, provider, concept, frequency or place: notices for new releases, new
        periods, revised provisional months, definition changes and removals (record changes, no forecasts)."""
        from src.kb.tourism_monitoring import TourismMonitor

        return run_tool("create_tourism_monitor", lambda conn: TourismMonitor(conn).create(
            namespace, request_key, target=target, principal_id=who()[0], scopes=who()[1], delivery=delivery),
            write=True)

    @mcp.tool()
    def run_tourism_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a tourism monitor at a committed watermark; notices cite the vintages before and after."""
        from src.kb.tourism_monitoring import TourismMonitor

        return run_tool("run_tourism_monitor", lambda conn: TourismMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True)
