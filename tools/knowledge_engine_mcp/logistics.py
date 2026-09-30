"""Economics logistics feature's entry points: port, country and route series as of a vintage, freight indices,
port identity review, citation links to trade flows and Economics series, and subscription monitors.

Acquisition runs through the shared source-pack tools (pack ``economic-shipping-and-logistics``: UN/LOCODE,
UNCTADstat, Eurostat maritime and the BLS deep sea freight PPI); an extracted UNCTADstat bulk CSV is an operator
import. Every value is cited with its source release, vintage, definition, unit and licence; sources stay side by
side.

Exclusions (declared by every tool): no freight-rate forecasting, no derived, rebased or interpolated index, no route
inferred from port totals, no merged series and no commercial index redistributed without a licence.
"""

READ = "knowledge:logistics:read"
WRITE = "knowledge:logistics:write"
REVIEW = "knowledge:logistics:review"
TRADE_READ = "knowledge:trade:read"
GEO_WRITE = "knowledge:geospatial:write"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
EXCLUSIONS_NOTE = (
    "Exclusions: no freight-rate forecasting, no derived or rebased index, no route inferred from port totals, "
    "no merged series."
)

LOGISTICS_WRITES = {
    "import_logistics_crosswalk",
    "propose_logistics_port_matches",
    "review_logistics_port_match",
    "revert_logistics_port_match",
    "project_logistics_port_places",
    "link_logistics_trade_flows",
    "link_logistics_economic_series",
    "link_logistics_citation",
    "create_logistics_monitor",
    "run_logistics_monitor",
}
LOGISTICS_READS = {
    "logistics_source_contracts",
    "logistics_readiness",
    "list_logistics_series",
    "logistics_series_values",
    "query_port_logistics",
    "query_country_logistics",
    "query_route_logistics",
    "logistics_value_history",
    "logistics_freight_indices",
    "list_logistics_ports",
    "list_logistics_port_matches",
    "list_logistics_links",
    "poll_logistics_monitor",
}
LOGISTICS_TOOLS = LOGISTICS_WRITES | LOGISTICS_READS
LOGISTICS_SCOPES = {
    "logistics_source_contracts": [],
    "logistics_readiness": [READ],
    "list_logistics_series": [READ],
    "logistics_series_values": [READ],
    "query_port_logistics": [READ],
    "query_country_logistics": [READ],
    "query_route_logistics": [READ],
    "logistics_value_history": [READ],
    "logistics_freight_indices": [READ],
    "list_logistics_ports": [READ],
    "list_logistics_port_matches": [READ],
    "list_logistics_links": [READ],
    "poll_logistics_monitor": [READ, SUBSCRIPTIONS_READ],
    "import_logistics_crosswalk": [WRITE],
    "propose_logistics_port_matches": [WRITE],
    "review_logistics_port_match": [REVIEW],
    "revert_logistics_port_match": [REVIEW],
    "project_logistics_port_places": [WRITE, GEO_WRITE],
    "link_logistics_trade_flows": [WRITE, TRADE_READ],
    "link_logistics_economic_series": [WRITE],
    "link_logistics_citation": [WRITE],
    "create_logistics_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_logistics_monitor": [READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return LOGISTICS_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _require(scopes, *required):
    from src.kb.logistics_records import LogisticsError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise LogisticsError("unauthorized", f"{', '.join(missing)} required")


def _declared(answer):
    from src.ingestion.logistics_sources import EXCLUSIONS

    return {**answer, "exclusions": list(EXCLUSIONS)} if isinstance(answer, dict) else answer


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def run_tool(tool, operation, *, write=False):
        scopes = LOGISTICS_SCOPES[tool]

        def run(conn):
            _require(who()[1], *scopes)
            return _declared(operation(conn))

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def logistics_source_contracts() -> dict:
        """Per-provider access decisions (UN/LOCODE, UNCTADstat, Eurostat maritime, BLS PPI), formats, identifiers,
        licences, rate limits, release and revision models, per-index freight licence decisions and bounded coverage.
        Exclusions: no freight-rate forecasting, no derived or rebased index, no route inferred from port totals,
        no merged series."""
        from src.ingestion.logistics_sources import (
            EXCLUSIONS,
            FREIGHT_INDEX_DECISIONS,
            LIVE_VERIFICATION,
            NEVER_SENTENCE,
            PROVIDER_CONTRACTS,
            coverage_report,
        )

        return {"contracts": PROVIDER_CONTRACTS, "freight_index_decisions": FREIGHT_INDEX_DECISIONS,
                "live_verification": LIVE_VERIFICATION, "coverage": coverage_report(), "never": NEVER_SENTENCE,
                "exclusions": list(EXCLUSIONS)}

    @mcp.tool()
    def logistics_readiness() -> dict:
        """Whether the logistics feature is selected, whether its stores hold releases, per-provider releases and
        the freight-index licence decisions (excluded indices named)."""
        from src.kb.logistics_records import readiness

        return run_tool("logistics_readiness", readiness)

    @mcp.tool()
    def list_logistics_series(namespace: str, provider: str | None = None, geo_kind: str | None = None,
                              concept: str | None = None, code: str | None = None) -> dict:
        """Logistics series (indicator mappings) with source series id, concept, unit, frequency, geography kind
        and published codes, definition reference, licence and freight-index decision."""
        from src.kb.logistics_series import read_store

        def op(conn):
            codes = None
            if code:
                scheme, _, value = code.rpartition(":")
                codes = [(scheme or None, value)]
            return {"series": read_store(conn, namespace, who()[1]).find_series(
                namespace, provider=provider, geo_kind=geo_kind, concept=concept, codes=codes)}

        return run_tool("list_logistics_series", op)

    @mcp.tool()
    def logistics_series_values(namespace: str, series_id: str, as_of_ms: int | None = None) -> dict:
        """One series' values in the vintage released by as_of_ms (latest by default), status, flags, footnotes and
        licence as published, with every vintage and the release citation."""
        from src.kb.logistics_records import LogisticsError
        from src.kb.logistics_series import read_store

        def op(conn):
            store = read_store(conn, namespace, who()[1])
            vintage = store.select_vintage(namespace, series_id, as_of_ms)
            if vintage is None:
                raise LogisticsError("not_found", "no vintage of this series was released by the date")
            return {"series": store.series(namespace, series_id), "vintage": vintage,
                    "values": store.values(namespace, vintage["vintage_id"]),
                    "citation": store.source_revision(namespace, vintage["release_id"]),
                    "vintages": store.vintage_rows(namespace, series_id)}

        return run_tool("logistics_series_values", op)

    @mcp.tool()
    def query_port_logistics(namespace: str, port: str, as_of_ms: int | None = None,
                             all_vintages: bool = False) -> dict:
        """A port (UN/LOCODE, or a source code scheme:code) to its logistics series as of a date: the vintage known
        then, match basis, definitions, units, licences and citations, sources side by side.
        Exclusions: no freight-rate forecasting, no derived or rebased index, no route inferred from port totals,
        no merged series."""
        from src.kb.logistics_queries import LogisticsQueries

        return run_tool("query_port_logistics", lambda conn: LogisticsQueries(conn).port(
            namespace, port, scopes=who()[1], as_of_ms=as_of_ms, all_vintages=all_vintages))

    @mcp.tool()
    def query_country_logistics(namespace: str, country: str, as_of_ms: int | None = None,
                                all_vintages: bool = False) -> dict:
        """A country (M49, Eurostat GEO or ISO alpha-2 code, optionally scheme:code) to its country-level logistics
        series as of a date; ports are listed as context, never summed.
        Exclusions: no freight-rate forecasting, no derived or rebased index, no route inferred from port totals,
        no merged series."""
        from src.kb.logistics_queries import LogisticsQueries

        return run_tool("query_country_logistics", lambda conn: LogisticsQueries(conn).country(
            namespace, country, scopes=who()[1], as_of_ms=as_of_ms, all_vintages=all_vintages))

    @mcp.tool()
    def query_route_logistics(namespace: str, origin: str, destination: str, as_of_ms: int | None = None) -> dict:
        """A published port-to-partner route as of a date; answered only from route-level series, else
        none_on_record.
        Exclusions: no freight-rate forecasting, no derived or rebased index, no route inferred from port totals,
        no merged series."""
        from src.kb.logistics_queries import LogisticsQueries

        return run_tool("query_route_logistics", lambda conn: LogisticsQueries(conn).route(
            namespace, origin, destination, scopes=who()[1], as_of_ms=as_of_ms))

    @mcp.tool()
    def logistics_value_history(namespace: str, series_id: str, period: str) -> dict:
        """Every vintage of one period's value, each with its release citation."""
        from src.kb.logistics_queries import LogisticsQueries

        return run_tool("logistics_value_history", lambda conn: LogisticsQueries(conn).value_history(
            namespace, series_id, period, scopes=who()[1]))

    @mcp.tool()
    def logistics_freight_indices(namespace: str, as_of_ms: int | None = None) -> dict:
        """Openly licensed freight-index series as of a date with licence and base, and every index excluded by
        licence decision with the reason.
        Exclusions: no freight-rate forecasting, no derived or rebased index, no route inferred from port totals,
        no merged series."""
        from src.kb.logistics_queries import LogisticsQueries

        return run_tool("logistics_freight_indices", lambda conn: LogisticsQueries(conn).freight_indices(
            namespace, scopes=who()[1], as_of_ms=as_of_ms))

    @mcp.tool()
    def list_logistics_ports(namespace: str, country: str | None = None, include_removed: bool = False) -> dict:
        """UN/LOCODE port records (current revision, release version and state; removed codes on request)."""
        from src.kb.logistics_ports import read_ports

        return run_tool("list_logistics_ports", lambda conn: {"ports": read_ports(conn, namespace, who()[1]).ports(
            namespace, country=country, include_removed=include_removed)})

    @mcp.tool()
    def list_logistics_port_matches(namespace: str, unlocode: str | None = None, state: str | None = None) -> dict:
        """Source port code to UN/LOCODE matches with basis, exactness, state, evidence and re-matches."""
        from src.kb.logistics_ports import read_ports

        return run_tool("list_logistics_port_matches", lambda conn: {
            "matches": read_ports(conn, namespace, who()[1]).matches(namespace, unlocode=unlocode, state=state)})

    @mcp.tool()
    def list_logistics_links(namespace: str, series_id: str | None = None, target_kind: str | None = None) -> dict:
        """Links from logistics series to trade-flow and Economics series with basis and side-by-side units."""
        from src.kb.logistics_ports import read_ports

        return run_tool("list_logistics_links", lambda conn: {
            "links": read_ports(conn, namespace, who()[1]).links(namespace, series_id=series_id,
                                                                  target_kind=target_kind)})

    @mcp.tool()
    def poll_logistics_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a logistics monitor's delivered notices."""
        from src.kb.logistics_monitoring import LogisticsMonitor

        return run_tool("poll_logistics_monitor", lambda conn: LogisticsMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))

    @mcp.tool()
    def import_logistics_crosswalk(namespace: str, crosswalk: dict) -> dict:
        """Record a published port code list or crosswalk (source code to UN/LOCODE) with its citation."""
        from src.kb.logistics_ports import LogisticsPorts

        return run_tool("import_logistics_crosswalk", lambda conn: LogisticsPorts(conn).import_crosswalk(
            namespace, crosswalk, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def propose_logistics_port_matches(namespace: str) -> dict:
        """Match source port codes: embedded UN/LOCODEs and published crosswalks exact, name matches as candidates
        for review, the rest unmatched."""
        from src.kb.logistics_ports import LogisticsPorts

        return run_tool("propose_logistics_port_matches", lambda conn: LogisticsPorts(conn).propose_matches(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_logistics_port_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a proposed port match with a reason."""
        from src.kb.logistics_ports import LogisticsPorts

        return run_tool("review_logistics_port_match", lambda conn: LogisticsPorts(conn).review(
            namespace, match_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def revert_logistics_port_match(namespace: str, match_id: str, reason: str) -> dict:
        """Revert an accepted port match with a reason."""
        from src.kb.logistics_ports import LogisticsPorts

        return run_tool("revert_logistics_port_match", lambda conn: LogisticsPorts(conn).revert(
            namespace, match_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def project_logistics_port_places(namespace: str, geo_namespace: str = "global") -> dict:
        """Register current UN/LOCODE ports as Geospatial places; a point only where coordinates are published."""
        from src.kb.logistics_ports import LogisticsPorts

        return run_tool("project_logistics_port_places", lambda conn: LogisticsPorts(conn).project_places(
            namespace, principal_id=who()[0], scopes=who()[1], geo_namespace=geo_namespace), write=True)

    @mcp.tool()
    def link_logistics_trade_flows(namespace: str, trade_namespace: str | None = None) -> dict:
        """Link logistics series to trade-flow series through shared published codes only; none_on_record without
        trade records. Units and frequencies are listed side by side, never combined.
        Exclusions: no freight-rate forecasting, no derived or rebased index, no route inferred from port totals,
        no merged series."""
        from src.kb.logistics_ports import LogisticsPorts

        return run_tool("link_logistics_trade_flows", lambda conn: LogisticsPorts(conn).link_trade_flows(
            namespace, principal_id=who()[0], scopes=who()[1], trade_namespace=trade_namespace), write=True)

    @mcp.tool()
    def link_logistics_economic_series(namespace: str) -> dict:
        """Link country-level logistics series to other Economics series of the same country code, with the
        Economics comparability check; never combined."""
        from src.kb.logistics_ports import LogisticsPorts

        return run_tool("link_logistics_economic_series", lambda conn: LogisticsPorts(conn).link_economic_series(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def link_logistics_citation(namespace: str, series_id: str, target_kind: str, target_id: str, citation: dict,
                                target_namespace: str | None = None) -> dict:
        """Record an explicit citation (quoted text and locator) joining a logistics series to a trade-flow or
        Economics series."""
        from src.kb.logistics_ports import LogisticsPorts

        return run_tool("link_logistics_citation", lambda conn: LogisticsPorts(conn).link_by_citation(
            namespace, series_id, target_kind, target_id, citation, principal_id=who()[0], scopes=who()[1],
            target_namespace=target_namespace), write=True)

    @mcp.tool()
    def create_logistics_monitor(namespace: str, request_key: str, watch: dict, delivery: dict | None = None) -> dict:
        """Subscribe to ports, countries or series: notices for new vintages, revised values, series breaks and
        UN/LOCODE changes (record changes, no forecast or trend verdict)."""
        from src.kb.logistics_monitoring import LogisticsMonitor

        return run_tool("create_logistics_monitor", lambda conn: LogisticsMonitor(conn).create(
            namespace, request_key, watch=watch, principal_id=who()[0], scopes=who()[1], delivery=delivery),
            write=True)

    @mcp.tool()
    def run_logistics_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a logistics monitor at a committed watermark; notices cite the source release."""
        from src.kb.logistics_monitoring import LogisticsMonitor

        return run_tool("run_logistics_monitor", lambda conn: LogisticsMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True)
