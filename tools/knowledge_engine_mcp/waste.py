"""Climate and Environment waste and circular-economy entry points (``environment.waste``, #2740 WC11).

A waste or circularity indicator for a place as of a release (Eurostat and OECD side by side), a facility's waste
transfers across reporting years (EEA Industrial Reporting, attached to the ``environment.core`` facility records),
reviewable place, facility and indicator identity, links to Chemicals and Products by identifier or citation,
evidence bundles and subscription monitors.

Acquisition runs through the shared source-pack tools (pack ``climate-environment-waste``: sources ``eurostat-waste``,
``eurostat-circular-economy``, ``eea-industry-waste-transfers`` and ``oecd-municipal-waste``), each an optional,
default-off feature of the Climate and Environment bundle. Every value is cited with its source, record revision and
as-of time; Chemicals, Products and ``environment.core`` facility links degrade to ``provider_absent`` when those
providers are not composed.

Exclusions (declared by every answering tool): no nowcasting, no filled years (biennial odd years stay absent), no
blending of Eurostat, OECD and EEA figures, no summing of facility transfers into national totals, no recycling rates,
per-capita or material-flow figures of our own, no derived indicators, no forecasts. Every answer is checked against
the WC01 minimisation decision (no operator, parent-company, address, contact or authority field).
"""

READ = "knowledge:waste:read"
WRITE = "knowledge:waste:write"
REVIEW = "knowledge:waste:review"
ENVIRONMENT_READ = "knowledge:environment:read"
GEO_READ = "knowledge:geospatial:read"
SUBSTANCES_READ = "knowledge:substances:read"
PRODUCTS_READ = "knowledge:products:read"
SCHEMA_REGISTER = "knowledge:schema:register"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
FEATURES = {
    "eurostat-waste": "waste-eurostat",
    "eurostat-circular-economy": "waste-eurostat-circular-economy",
    "eea-industry-waste-transfers": "waste-eea-transfers",
    "oecd-municipal-waste": "waste-oecd",
}
EXCLUSIONS_NOTE = (
    "Exclusions: no nowcasting, no filled years (biennial odd years stay absent), no blending of Eurostat, OECD and "
    "EEA figures, no summing of facility transfers into national totals, no own rates, per-capita or material-flow "
    "figures, no derived indicators and no forecasts."
)

WASTE_WRITES = {
    "register_waste_schemas",
    "propose_waste_place_matches",
    "propose_waste_facility_matches",
    "propose_waste_related_indicators",
    "review_waste_identity_match",
    "revert_waste_identity_match",
    "link_waste_records",
    "create_waste_monitor",
    "run_waste_monitor",
}
WASTE_READS = {
    "waste_source_contracts",
    "waste_readiness",
    "list_waste_series",
    "waste_indicator_for_place",
    "waste_series_history",
    "waste_facility_transfers",
    "list_waste_identity_assertions",
    "list_waste_links",
    "export_waste_bundle",
    "poll_waste_monitor",
}
WASTE_TOOLS = WASTE_WRITES | WASTE_READS
WASTE_SCOPES = {
    "waste_source_contracts": [],
    "waste_readiness": [READ],
    "list_waste_series": [READ],
    "waste_indicator_for_place": [READ],
    "waste_series_history": [READ],
    "waste_facility_transfers": [READ, ENVIRONMENT_READ],
    "list_waste_identity_assertions": [READ],
    "list_waste_links": [READ],
    "export_waste_bundle": [READ, ENVIRONMENT_READ],
    "poll_waste_monitor": [READ, SUBSCRIPTIONS_READ],
    "register_waste_schemas": [WRITE, SCHEMA_REGISTER],
    "propose_waste_place_matches": [WRITE, GEO_READ],
    "propose_waste_facility_matches": [WRITE, ENVIRONMENT_READ],
    "propose_waste_related_indicators": [WRITE],
    "review_waste_identity_match": [REVIEW],
    "revert_waste_identity_match": [REVIEW],
    "link_waste_records": [WRITE, SUBSTANCES_READ, PRODUCTS_READ],
    "create_waste_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_waste_monitor": [READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return WASTE_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def selected_features(conn) -> list[str]:
    """The optional waste features selected in the active composition plan."""
    from src.kb.waste_records import selected_features as selected

    return selected(conn)


def readiness(conn, namespace="environment"):
    from src.kb.waste_records import readiness as report

    return report(conn, namespace)


def _require(scopes, *required):
    from src.kb.waste_records import WasteError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise WasteError("unauthorized", f"{', '.join(missing)} required")


def _declared(answer):
    """Every answer leaves with the exclusions declared and is checked against the minimisation decision."""
    from src.ingestion.waste_sources import EXCLUSIONS
    from src.kb.waste_records import WasteError, forbidden_paths, personal_data_paths

    if not isinstance(answer, dict):
        return answer
    if personal_data_paths(answer) or forbidden_paths(answer):
        raise WasteError("minimisation", "an answer would carry an operator, contact, address or authority field or "
                                         "a derived value")
    return {**answer, "exclusions": list(EXCLUSIONS)}


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def run_tool(tool, operation, *, write=False):
        """Checks every declared scope, runs the operation and declares the exclusions on the answer."""
        scopes = WASTE_SCOPES[tool]

        def run(conn):
            _require(who()[1], *scopes)
            return _declared(operation(conn))

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def waste_source_contracts() -> dict:
        """Per-source access decisions (Eurostat waste, Eurostat circular economy, EEA waste transfers, OECD
        municipal waste), terms, limits, revision models, caps, the bounded coverage and the minimisation decision;
        every source is unverified-live until a dated live run.
        Exclusions: no nowcasting, no filled years (biennial odd years stay absent), no blending of Eurostat, OECD and
        EEA figures, no summing of facility transfers into national totals, no own rates, per-capita or
        material-flow figures, no derived indicators and no forecasts."""
        from src.ingestion.waste_sources import (
            BOUNDED_COVERAGE,
            CAPS,
            EXCLUSIONS,
            LIVE_VERIFICATION,
            NEVER_SENTENCE,
            PROVIDER_CONTRACTS,
        )
        from src.kb.waste_records import MINIMISATION

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION, "caps": CAPS,
                "bounded_coverage": BOUNDED_COVERAGE, "minimisation": MINIMISATION, "never": NEVER_SENTENCE,
                "features": FEATURES, "exclusions": list(EXCLUSIONS)}

    @mcp.tool()
    def waste_readiness(namespace: str = "environment") -> dict:
        """Which optional waste features are selected, the stores, per-source releases and staleness, and whether
        the Chemicals, Products and environment.core facility links are available or provider_absent."""
        return run_tool("waste_readiness", lambda conn: readiness(conn, namespace))

    @mcp.tool()
    def list_waste_series(namespace: str = "environment", provider: str | None = None,
                          concept: str | None = None, area: dict | None = None) -> dict:
        """Waste and circular-economy series with source, dataset, waste category, hazardousness, activity,
        treatment operation, unit as published, place and periodicity."""
        from src.kb.waste_records import authorize
        from src.kb.waste_store import WasteStore

        def op(conn):
            authorize(namespace, who()[1], READ)
            codes = [(area["scheme"], str(area["code"]))] if area else None
            return {"series": WasteStore(conn, initialize=False).find_series(namespace, provider=provider,
                                                                              concept=concept, area_codes=codes)}

        return run_tool("list_waste_series", op)

    @mcp.tool()
    def waste_indicator_for_place(place: str | dict, namespace: str = "environment", concept: str | None = None,
                                  as_of: str | int | None = None) -> dict:
        """Waste and circularity figures for a place (place id or {scheme, code}) as released by a date: each source
        side by side with its definition, series key, flags, vintage and citation; biennial odd years absent.
        Exclusions: no nowcasting, no filled years (biennial odd years stay absent), no blending of Eurostat, OECD and
        EEA figures, no summing of facility transfers into national totals, no own rates, per-capita or
        material-flow figures, no derived indicators and no forecasts."""
        from src.kb.waste_queries import WasteQueries

        return run_tool("waste_indicator_for_place", lambda conn: WasteQueries(conn).indicator_for_place(
            namespace, scopes=who()[1], place=place, concept=concept, as_of=as_of))

    @mcp.tool()
    def waste_series_history(series_id: str, namespace: str = "environment") -> dict:
        """Every retained vintage of a waste series with what each release added, revised, dropped or removed, each
        vintage cited.
        Exclusions: no nowcasting, no filled years (biennial odd years stay absent), no blending of Eurostat, OECD and
        EEA figures, no summing of facility transfers into national totals, no own rates, per-capita or
        material-flow figures, no derived indicators and no forecasts."""
        from src.kb.waste_queries import WasteQueries

        return run_tool("waste_series_history", lambda conn: WasteQueries(conn).series_history(
            namespace, series_id, scopes=who()[1]))

    @mcp.tool()
    def waste_facility_transfers(facility: str, namespace: str = "environment",
                                 as_of: str | int | None = None) -> dict:
        """A facility's published off-site waste transfers per reporting year (INSPIRE id or the environment.core
        facility record id): hazardousness, recovery or disposal, domestic or transboundary, quantity in tonnes as
        published and method code, every vintage cited, truncated acquisitions and the reporting-threshold note.
        Exclusions: no nowcasting, no filled years (biennial odd years stay absent), no blending of Eurostat, OECD and
        EEA figures, no summing of facility transfers into national totals, no own rates, per-capita or
        material-flow figures, no derived indicators and no forecasts."""
        from src.kb.waste_queries import WasteQueries

        return run_tool("waste_facility_transfers", lambda conn: WasteQueries(conn).facility_transfers(
            namespace, scopes=who()[1], facility=facility, as_of=as_of))

    @mcp.tool()
    def list_waste_identity_assertions(namespace: str = "environment", kind: str | None = None,
                                       state: str | None = None) -> dict:
        """Place, facility and indicator identity assertions in force (proposed, ambiguous, unmatched,
        provider_absent, accepted, rejected, reverted), each with method, evidence, confidence and reviewer."""
        from src.kb.waste_identity import WasteIdentity

        return run_tool("list_waste_identity_assertions", lambda conn: {
            "assertions": WasteIdentity(conn, initialize=False).assertions(namespace, scopes=who()[1], kind=kind,
                                                                           state=state)})

    @mcp.tool()
    def list_waste_links(namespace: str = "environment", subject_id: str | None = None, kind: str | None = None,
                         state: str | None = None) -> dict:
        """Links of waste records to Chemicals (published CAS number), Products (citation) and environment.core
        facilities (accepted match), each pinning both revisions; absent providers and targets reported."""
        from src.kb.waste_links import WasteLinks

        return run_tool("list_waste_links", lambda conn: {
            "links": WasteLinks(conn, initialize=False).links(namespace, scopes=who()[1], subject_id=subject_id,
                                                              kind=kind, state=state)})

    @mcp.tool()
    def export_waste_bundle(namespace: str = "environment", place: str | dict | None = None,
                            facility: str | None = None, as_of: str | int | None = None) -> dict:
        """An evidence bundle for a place and/or a facility; every item cites its source, record revision and
        as-of time.
        Exclusions: no nowcasting, no filled years (biennial odd years stay absent), no blending of Eurostat, OECD and
        EEA figures, no summing of facility transfers into national totals, no own rates, per-capita or
        material-flow figures, no derived indicators and no forecasts."""
        from src.kb.waste_queries import WasteQueries

        return run_tool("export_waste_bundle", lambda conn: WasteQueries(conn).export_bundle(
            namespace, scopes=who()[1], place=place, facility=facility, as_of=as_of))

    @mcp.tool()
    def poll_waste_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a waste monitor's notices (new releases, new years, revisions, removals by the source)."""
        from src.kb.waste_monitoring import WasteMonitor

        return run_tool("poll_waste_monitor", lambda conn: WasteMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))

    # ------------------------------------------------------------------ writes

    @mcp.tool()
    def register_waste_schemas() -> dict:
        """Register the noesis-waste-record-v2 schema in the schema registry (idempotent per version)."""
        from src.kb.waste_records import register_schemas

        return run_tool("register_waste_schemas", lambda conn: {"registered": register_schemas(
            conn, principal_id=who()[0], scopes=who()[1])}, write=True)

    @mcp.tool()
    def propose_waste_place_matches(namespace: str = "environment", geo_namespace: str = "global") -> dict:
        """Offer every stated area code to Geospatial places by its published code; ambiguous codes stay review
        candidates; nothing is used until reviewed and nothing is merged."""
        from src.kb.waste_identity import WasteIdentity

        return run_tool("propose_waste_place_matches", lambda conn: WasteIdentity(conn).propose_places(
            namespace, principal_id=who()[0], scopes=who()[1], geo_namespace=geo_namespace), write=True)

    @mcp.tool()
    def propose_waste_facility_matches(namespace: str = "environment") -> dict:
        """Match each stated INSPIRE id to the environment.core facility record carrying it; unknown ids stay
        unmatched and no facility is created."""
        from src.kb.waste_identity import WasteIdentity

        return run_tool("propose_waste_facility_matches", lambda conn: WasteIdentity(conn).propose_facilities(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def propose_waste_related_indicators(namespace: str = "environment") -> dict:
        """Record the same or a related indicator published by Eurostat and the OECD for one accepted place as
        related (never merged or reconciled)."""
        from src.kb.waste_identity import WasteIdentity

        return run_tool("propose_waste_related_indicators", lambda conn: WasteIdentity(
            conn).propose_related_indicators(namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_waste_identity_match(assertion_id: str, decision: str, reason: str, namespace: str = "environment",
                                    place_id: str | None = None) -> dict:
        """Accept or reject a proposed place, facility or indicator assertion with a reason (an ambiguous place by
        choosing a cited candidate)."""
        from src.kb.waste_identity import WasteIdentity

        return run_tool("review_waste_identity_match", lambda conn: WasteIdentity(conn).review(
            namespace, assertion_id, decision, reason, principal_id=who()[0], scopes=who()[1], place_id=place_id),
            write=True)

    @mcp.tool()
    def revert_waste_identity_match(assertion_id: str, reason: str, namespace: str = "environment") -> dict:
        """Revert a reviewed assertion; queries stop using it."""
        from src.kb.waste_identity import WasteIdentity

        return run_tool("revert_waste_identity_match", lambda conn: WasteIdentity(conn).revert(
            namespace, assertion_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def link_waste_records(namespace: str = "environment") -> dict:
        """Link waste records to Chemicals by a published CAS number (never by name), to Products by citation of the
        packaging and WEEE datasets, and transfer rows to their accepted environment.core facility.
        Exclusions: no nowcasting, no filled years (biennial odd years stay absent), no blending of Eurostat, OECD and
        EEA figures, no summing of facility transfers into national totals, no own rates, per-capita or
        material-flow figures, no derived indicators and no forecasts."""
        from src.kb.waste_links import WasteLinks

        def op(conn):
            links = WasteLinks(conn)
            return {"chemicals": links.link_chemicals(namespace, principal_id=who()[0], scopes=who()[1]),
                    "products": links.link_products(namespace, principal_id=who()[0], scopes=who()[1]),
                    "facilities": links.link_facilities(namespace, principal_id=who()[0], scopes=who()[1])}

        return run_tool("link_waste_records", op, write=True)

    @mcp.tool()
    def create_waste_monitor(request_key: str, target: dict, namespace: str = "environment",
                             delivery: dict | None = None) -> dict:
        """Subscribe to a place, an indicator, a facility (INSPIRE id), a series or a source: notices for new
        releases, new years, revised values and resubmitted past years, corrected transfer rows and removals."""
        from src.kb.waste_monitoring import WasteMonitor

        return run_tool("create_waste_monitor", lambda conn: WasteMonitor(conn).create(
            namespace, request_key, target=target, principal_id=who()[0], scopes=who()[1], delivery=delivery),
            write=True)

    @mcp.tool()
    def run_waste_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a waste monitor at a committed watermark; notices cite the revisions before and after."""
        from src.kb.waste_monitoring import WasteMonitor

        return run_tool("run_waste_monitor", lambda conn: WasteMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True)
