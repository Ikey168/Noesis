"""Transit schedules and realtime observations over the Geospatial store (GTFS Schedule + GTFS Realtime).

Acquisition runs through the geospatial source pack (source ``vbb-gtfs``).
Scheduled times come from the current feed version; realtime is reported as
observed, stale or missing and never inferred from a timetable.
"""

TRANSIT_TOOLS = {"transit_source_contracts", "transit_feed_versions", "transit_departures", "transit_stops_in_bbox"}
TRANSIT_SCOPES = {"transit_source_contracts": []}


def required_scopes(tool_name, mutability):
    del mutability
    return TRANSIT_SCOPES.get(tool_name, ["knowledge:transit:read"])


def register(mcp, safe, context):
    def scopes():
        return context()[1]

    def store(conn):
        from src.kb.transit import TransitStore

        return TransitStore(conn)

    @mcp.tool()
    def transit_source_contracts() -> dict:
        """The named GTFS feed contract and why rail and flight status feeds are not wired yet."""
        from src.ingestion.transit_sources import PROVIDER_CONTRACTS

        return {"contracts": PROVIDER_CONTRACTS}

    @mcp.tool()
    def transit_feed_versions(namespace: str, feed: str) -> dict:
        """Acquired schedule versions of a feed, route shapes and stops relocated between versions."""
        return safe(lambda conn: store(conn).feed(namespace, feed, scopes=scopes()),
                    required_scope="knowledge:transit:read")

    @mcp.tool()
    def transit_departures(namespace: str, feed: str, stop_id: str, service_date: str,
                           realtime_max_age_s: int = 300) -> dict:
        """Scheduled departures at a stop on a service day, with realtime state (observed/stale/missing) and alerts."""
        return safe(lambda conn: store(conn).departures(namespace, feed, stop_id, service_date, scopes=scopes(),
                                                        realtime_max_age_s=realtime_max_age_s),
                    required_scope="knowledge:transit:read")

    @mcp.tool()
    def transit_stops_in_bbox(namespace: str, feed: str, bbox: list[float]) -> dict:
        """Stops of the current feed version inside west,south,east,north with their Geospatial geometry ids."""
        return safe(lambda conn: store(conn).stops_in_bbox(namespace, feed, bbox, scopes=scopes()),
                    required_scope="knowledge:transit:read")
