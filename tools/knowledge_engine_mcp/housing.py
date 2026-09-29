"""Geospatial housing feature entry points: records, dossiers, zone comparisons, citation links and monitors.

Acquisition runs through the shared source-pack tools (pack
``geospatial-berlin`` 1.3.0: ``berlin-boris-bodenrichtwerte``,
``berlin-bebauungsplaene``, ``berlin-wohnlagen`` through the WFS path;
``berlin-mietspiegel``, ``statistik-bb-bautaetigkeit`` and
``destatis-genesis-bautaetigkeit`` through the housing connector). Every answer
cites each value's source, currency, unit and valuation date, edition or
vintage. No tool gives a property valuation, rent or investment advice or
tenancy legal advice, and no value is interpolated, averaged or merged between
zones, cells, reporting areas, sources or editions.
"""

READ = "knowledge:housing:read"
WRITE = "knowledge:housing:write"
REVIEW = "knowledge:housing:review"
GEO_READ = "knowledge:geospatial:read"
GEO_CALCULATE = "knowledge:geospatial:calculate"
LEGAL_READ = "knowledge:legal:read"
DOSSIER_READ = "knowledge:political:dossier:read"
TRANSIT_READ = "knowledge:transit:read"
NEWS_READ = "knowledge:read"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"

HOUSING_WRITES = {
    "housing_dossier",
    "replay_housing_dossier",
    "link_housing_citations",
    "link_housing_dossier",
    "propose_housing_link_candidates",
    "review_housing_link",
    "revert_housing_link",
    "create_housing_monitor",
    "run_housing_monitor",
}
HOUSING_READS = {
    "housing_source_contracts",
    "housing_readiness",
    "list_housing_records",
    "inspect_housing_record",
    "compare_land_value_revisions",
    "housing_rent_index_edition",
    "housing_statistics",
    "housing_indicator_values",
    "list_housing_projection_outcomes",
    "list_housing_links",
    "poll_housing_monitor",
}
HOUSING_TOOLS = HOUSING_WRITES | HOUSING_READS
# Every scope each tool always reads or writes. A scope needed only for an optional argument (the transit context,
# news context, or a place or district monitor selector) is checked at call time and documented as conditional.
HOUSING_SCOPES = {
    "housing_source_contracts": [],
    "housing_readiness": [READ],
    "list_housing_records": [READ],
    "inspect_housing_record": [READ],
    "compare_land_value_revisions": [READ],
    "housing_rent_index_edition": [READ],
    "housing_statistics": [READ],
    "housing_indicator_values": [READ],
    "list_housing_projection_outcomes": [READ],
    "list_housing_links": [READ],
    "poll_housing_monitor": [READ, SUBSCRIPTIONS_READ],
    "housing_dossier": [READ, GEO_CALCULATE, GEO_READ],
    "replay_housing_dossier": [READ, GEO_CALCULATE, GEO_READ],
    "link_housing_citations": [WRITE, LEGAL_READ],
    "link_housing_dossier": [WRITE, DOSSIER_READ],
    "propose_housing_link_candidates": [WRITE, LEGAL_READ],
    "review_housing_link": [REVIEW],
    "revert_housing_link": [REVIEW],
    "create_housing_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_housing_monitor": [READ, SUBSCRIPTIONS_READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return HOUSING_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    @mcp.tool()
    def housing_source_contracts() -> dict:
        """Per-provider access decisions, delivery shape, date semantics, currency and unit, and terms."""
        from src.ingestion.housing_sources import (
            NO_SURFACE,
            PROVIDER_CONTRACTS,
            REVIEW_BOUNDARY,
        )

        return {
            "contracts": PROVIDER_CONTRACTS,
            "review_boundary": REVIEW_BOUNDARY,
            "value_surface": NO_SURFACE,
        }

    @mcp.tool()
    def housing_readiness() -> dict:
        """Whether the Geospatial housing feature is selected, record counts and per-provider access decisions."""
        from src.kb.housing import readiness

        return safe(lambda conn: readiness(conn), required_scope=READ)

    @mcp.tool()
    def list_housing_records(
        namespace: str,
        record_type: str,
        current_only: bool = True,
        zone_id: str | None = None,
        plan_key: str | None = None,
        edition_id: str | None = None,
        series_id: str | None = None,
        source_id: str | None = None,
    ) -> dict:
        """Housing records of one type (land_value_revision, rent_index_edition, rent_index_cell, plan_stage,
        area_category, building_statistic, indicator_vintage) with their source revisions; corrections kept."""
        from src.kb.housing import HousingStore, authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            filters = {
                "zone_id": zone_id,
                "plan_key": plan_key,
                "edition_id": edition_id,
                "series_id": series_id,
                "source_id": source_id,
            }
            return {
                "records": HousingStore(conn, initialize=False).records(
                    record_type,
                    namespace,
                    current_only=current_only,
                    **{k: v for k, v in filters.items() if v},
                )
            }

        return safe(run, required_scope=READ)

    @mcp.tool()
    def inspect_housing_record(namespace: str, record_id: str) -> dict:
        """One housing record with its revision history and whether it is current."""
        from src.kb.housing import HousingStore, authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            return HousingStore(conn, initialize=False).record(namespace, record_id)

        return safe(run, required_scope=READ)

    @mcp.tool()
    def compare_land_value_revisions(
        namespace: str, zone_id: str | None = None, feature_id: str | None = None
    ) -> dict:
        """One zone's values across valuation dates and sources side by side; no change or trend is computed."""
        from src.kb.housing_places import HousingPlaces

        return safe(
            lambda conn: HousingPlaces(conn, initialize=False).compare_zone(
                namespace, scopes=who()[1], zone_id=zone_id, feature_id=feature_id
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def housing_rent_index_edition(namespace: str, edition_id: str) -> dict:
        """A Mietspiegel edition (qualifying date, valid from, publication URL and page) and its cells."""
        from src.kb.housing import HousingStore, authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            return HousingStore(conn, initialize=False).edition(namespace, edition_id)

        return safe(run, required_scope=READ)

    @mcp.tool()
    def housing_statistics(
        namespace: str,
        scheme: str | None = None,
        code: str | None = None,
        statistic: str | None = None,
        measure: str | None = None,
    ) -> dict:
        """Permit and completion statistics per reporting area, period, measure and vintage (every vintage)."""
        from src.kb.housing import HousingStore, authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            return {
                "statistics": HousingStore(conn, initialize=False).statistics(
                    namespace,
                    scheme=scheme,
                    code=code,
                    statistic=statistic,
                    measure=measure,
                )
            }

        return safe(run, required_scope=READ)

    @mcp.tool()
    def housing_indicator_values(namespace: str, series_id: str, as_of_ms: int) -> dict:
        """A Destatis housing series' values at the table vintage released on or before a time."""
        from src.kb.housing import HousingStore, authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            return HousingStore(conn, initialize=False).indicator_values(
                namespace, series_id, as_of_ms=as_of_ms
            )

        return safe(run, required_scope=READ)

    @mcp.tool()
    def list_housing_projection_outcomes(
        namespace: str, run_id: str | None = None, source_id: str | None = None
    ) -> dict:
        """Features or rows that were not projected into housing records, with the reason."""
        from src.kb.housing import HousingStore, authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            return {
                "outcomes": HousingStore(conn, initialize=False).outcomes(
                    namespace, run_id=run_id, source_id=source_id
                )
            }

        return safe(run, required_scope=READ)

    @mcp.tool()
    def housing_dossier(
        namespace: str,
        as_of: str,
        geo_namespace: str = "global",
        place_id: str | None = None,
        resolution_id: str | None = None,
        point: list[float] | None = None,
        district_code: str | None = None,
        include_transit_feed: str | None = None,
        include_news: bool = False,
    ) -> dict:
        """The cited housing dossier of an address or parcel (place or reviewed resolution), a point or a district
        as of a date: containing land-value zone with valuation date and prior revisions, plans with stage history,
        Wohnlage, rent-index edition and cells, district and Land statistics side by side, and citation links;
        boundary points and places outside all zones are reported, never resolved (records spatial receipts).
        Conditional scopes: knowledge:transit:read for include_transit_feed, knowledge:read for include_news."""
        from src.kb.housing_places import HousingPlaces

        return safe(
            lambda conn: HousingPlaces(conn).dossier(
                namespace,
                as_of=as_of,
                principal_id=who()[0],
                scopes=who()[1],
                geo_namespace=geo_namespace,
                place_id=place_id,
                resolution_id=resolution_id,
                point=point,
                district_code=district_code,
                include_transit_feed=include_transit_feed,
                include_news=include_news,
            ),
            write=True,
            required_scope=READ,
        )

    @mcp.tool()
    def replay_housing_dossier(receipt: dict) -> dict:
        """Recompute a dossier from its receipt: reproduced when the same records are selected, else changed."""
        from src.kb.housing_places import HousingPlaces

        return safe(
            lambda conn: HousingPlaces(conn).replay(
                receipt, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope=READ,
        )

    @mcp.tool()
    def link_housing_citations(namespace: str, legal_namespace: str) -> dict:
        """Link rent-index editions and plans to legal works by their published GVBl, Amtsblatt, juris, BGBl,
        CELEX or ELI references in the reference's jurisdiction; unmatched references stay unresolved."""
        from src.kb.housing_links import HousingLinks

        return safe(
            lambda conn: HousingLinks(conn).link_citations(
                namespace,
                legal_namespace=legal_namespace,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def link_housing_dossier(
        namespace: str, dossier_namespace: str, dossier_id: str
    ) -> dict:
        """Link editions and plans to a legislative dossier by a published printed-paper reference."""
        from src.kb.housing_links import HousingLinks

        return safe(
            lambda conn: HousingLinks(conn).link_dossier(
                namespace,
                dossier_namespace,
                dossier_id,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def propose_housing_link_candidates(namespace: str, legal_namespace: str) -> dict:
        """A plan number or edition name in a work title as a reviewable discovery candidate, never a link."""
        from src.kb.housing_links import HousingLinks

        return safe(
            lambda conn: HousingLinks(conn).propose_candidates(
                namespace,
                legal_namespace=legal_namespace,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def review_housing_link(
        namespace: str, link_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject a discovery candidate (by someone other than its proposer)."""
        from src.kb.housing_links import HousingLinks

        return safe(
            lambda conn: HousingLinks(conn).review(
                namespace,
                link_id,
                decision,
                reason,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=REVIEW,
        )

    @mcp.tool()
    def revert_housing_link(namespace: str, link_id: str, reason: str) -> dict:
        """Revert an accepted or rejected candidate; explicit citations are the source's own and stay."""
        from src.kb.housing_links import HousingLinks

        return safe(
            lambda conn: HousingLinks(conn).revert(
                namespace, link_id, reason, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope=REVIEW,
        )

    @mcp.tool()
    def list_housing_links(
        namespace: str,
        subject_kind: str | None = None,
        subject_id: str | None = None,
        target_id: str | None = None,
        states: list[str] | None = None,
    ) -> dict:
        """Citation links, unresolved references and candidates of editions and plans, each with its evidence."""
        from src.kb.housing_links import HousingLinks

        return safe(
            lambda conn: {
                "links": HousingLinks(conn, initialize=False).links(
                    namespace,
                    scopes=who()[1],
                    subject_kind=subject_kind,
                    subject_id=subject_id,
                    target_id=target_id,
                    states=states,
                )
            },
            required_scope=READ,
        )

    @mcp.tool()
    def create_housing_monitor(
        namespace: str, request_key: str, selector: dict, geo_namespace: str = "global"
    ) -> dict:
        """A subscription for new land-value publications, plan-stage changes and rent-index editions for one
        place_id, district_code, plan_id or zone_id. Conditional scope: knowledge:geospatial:read for a place or
        district selector."""
        from src.kb.housing_monitoring import HousingMonitor

        return safe(
            lambda conn: HousingMonitor(conn).create(
                namespace,
                request_key,
                selector=selector,
                principal_id=who()[0],
                scopes=who()[1],
                geo_namespace=geo_namespace,
            ),
            write=True,
            required_scope=READ,
        )

    @mcp.tool()
    def run_housing_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a housing monitor at a committed watermark; unchanged publications emit nothing."""
        from src.kb.housing_monitoring import HousingMonitor

        return safe(
            lambda conn: HousingMonitor(conn).run(
                subscription_id, watermark, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope=READ,
        )

    @mcp.tool()
    def poll_housing_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Events of a housing monitor after a cursor."""
        from src.kb.housing_monitoring import HousingMonitor

        return safe(
            lambda conn: HousingMonitor(conn, initialize=False).poll(
                subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor
            ),
            required_scope=READ,
        )
