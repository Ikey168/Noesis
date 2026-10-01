"""Climate and Environment water and hydrology entry points (provider ``environment.water``, #2582 WA11 #2638).

Registered through :func:`tools.knowledge_engine_mcp.environment.register`, so
the tools live in the knowledge-engine server beside the other Climate and
Environment tools and share its bundle gate (Geospatial, subscriptions and the
source runtime are never gated by it). PEGELONLINE, USGS and EEA WISE coverage
are three separate optional features (``water-pegelonline``, ``water-usgs``,
``water-eea-wise``); hazard, weather and infrastructure links degrade to
``unavailable`` when those providers are absent. Exclusions (#2582): no flood
forecasting, no interpolation or gap filling, no own status assessment, no
flood-risk scoring; outputs carry no personal fields (WA01 minimisation).
"""

from __future__ import annotations

from src.kb.water_records import MINIMISATION, NEVER_SENTENCE, minimise

WATER_FEATURES = {"pegelonline": "water-pegelonline", "usgs": "water-usgs", "eea-wise": "water-eea-wise"}
WATER_WRITES = {
    "register_water_schemas", "propose_water_identity_matches", "review_water_identity_match",
    "revert_water_identity_match", "link_water_records", "create_water_monitor", "run_water_monitor",
}
WATER_TOOLS = WATER_WRITES | {
    "water_source_contracts", "water_readiness", "water_value_at", "water_series", "water_station_history",
    "water_for_place", "water_body_status_history", "list_water_identity_matches", "list_water_links",
    "export_water_bundle", "poll_water_monitor",
}
WATER_SCOPES = {
    "water_source_contracts": [],
    "register_water_schemas": ["knowledge:environment:write", "knowledge:schema:register"],
    "propose_water_identity_matches": ["knowledge:environment:write", "knowledge:geospatial:calculate"],
    "review_water_identity_match": ["knowledge:environment:review", "knowledge:entity-history:review"],
    "revert_water_identity_match": ["knowledge:environment:review", "knowledge:entity-history:execute"],
    "link_water_records": ["knowledge:environment:write"],
    "create_water_monitor": ["knowledge:environment:read", "knowledge:subscriptions:write"],
    "run_water_monitor": ["knowledge:environment:read", "knowledge:subscriptions:write"],
    "poll_water_monitor": ["knowledge:environment:read", "knowledge:subscriptions:read"],
}


def required_scopes(tool_name, mutability):
    return WATER_SCOPES.get(
        tool_name, ["knowledge:environment:write" if mutability == "write" else "knowledge:environment:read"])


def selected_features(conn) -> set[str]:
    """Which of the bundle's optional water features are in the active composition plan."""
    import json

    try:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN ('composition_authority', "
            "'composition_active', 'composition_generations', 'composition_plans')").fetchall()}
        if len(tables) < 4:
            return set()
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g ON g.generation_id="
            "a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1").fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return set()
    return set((plan.get("features") or {}).get("climate-environment") or []) & set(WATER_FEATURES.values())


def readiness(conn, namespace):
    from src.ingestion.water_sources import (
        LIVE_VERIFICATION,
        NOT_IMPLEMENTED,
        PROVIDER_CONTRACTS,
    )
    from src.kb.water_links import providers
    from src.kb.water_store import TABLES, WaterStore, table_exists

    store = WaterStore(conn, initialize=False)
    selected = selected_features(conn)
    return {"provider": "environment.water", "bundle": "climate-environment",
            "features": {feature: {"provider": p, "selected": feature in selected, "default": False}
                         for p, feature in WATER_FEATURES.items()},
            "stores": {t: table_exists(conn, t) for t in TABLES},
            "providers": {p: {"licence": c["licence"], "live_verification": LIVE_VERIFICATION[p]["status"],
                              "feature": WATER_FEATURES[p],
                              "state": store.provider_state(namespace, p) if store.ready() else None}
                          for p, c in PROVIDER_CONTRACTS.items()},
            "not_implemented": NOT_IMPLEMENTED, "linked_providers": providers(conn),
            "minimisation": MINIMISATION["decision"], "boundary": NEVER_SENTENCE}


def _require(scopes, required):
    from src.kb.water_records import require

    require(scopes, *required)


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def gated(namespace, tool, operation, *, write=False):
        """Declared scopes first, then the Climate and Environment bundle gate, then the operation."""
        required = required_scopes(tool, "write" if write else "read")

        def run(conn):
            from src.kb.environment_bundle import require_enabled

            _require(who()[1], required)
            require_enabled(conn, namespace)
            return minimise(operation(conn))

        return safe(run, write=write, required_scope=required[0] if required else None)

    @mcp.tool()
    def water_source_contracts() -> dict:
        """PEGELONLINE, USGS Water Data and EEA WISE access contracts, GRDC's not-implemented decision, bounded
        coverage, the no-personal-data decision and live state. No forecasting, interpolation or risk scoring."""
        from src.ingestion.water_sources import (
            BOUNDED_COVERAGE,
            LIVE_VERIFICATION,
            NOT_IMPLEMENTED,
            PROVIDER_CONTRACTS,
        )

        return {"contracts": PROVIDER_CONTRACTS, "not_implemented": NOT_IMPLEMENTED,
                "bounded_coverage": BOUNDED_COVERAGE, "live_verification": LIVE_VERIFICATION,
                "minimisation": MINIMISATION, "features": WATER_FEATURES, "boundary": NEVER_SENTENCE}

    @mcp.tool()
    def water_readiness(namespace: str) -> dict:
        """Which water features are selected, which stores exist, per-provider state and which linked providers
        (hazards, weather, infrastructure) are available."""
        return gated(namespace, "water_readiness", lambda conn: readiness(conn, namespace))

    @mcp.tool()
    def register_water_schemas(namespace: str) -> dict:
        """Register noesis-water-record-v1 in the schema registry."""
        from src.kb.water_records import register_schemas

        return gated(namespace, "register_water_schemas", lambda conn: {
            "modules": register_schemas(conn, principal_id=who()[0], scopes=who()[1])}, write=True)

    @mcp.tool()
    def water_value_at(namespace: str, station: str, parameter: str, time: str, as_of: str | None = None) -> dict:
        """The water level or discharge value on record at as_of for a station, parameter and time: unit and time
        as published, provisional or approved, qualifiers, the gauge zero or datum, the cited revision and later
        revisions. A time without a published value stays missing; nothing is interpolated or forecast."""
        from src.kb.water_queries import value_at

        return gated(namespace, "water_value_at", lambda conn: value_at(
            conn, namespace, station, parameter, time, scopes=who()[1], as_of=as_of))

    @mcp.tool()
    def water_series(namespace: str, station: str, parameter: str, start: str, end: str,
                     as_of: str | None = None) -> dict:
        """Published values in a window, each cited with its quality state, and the gaps the station's published
        interval shows; never resampled, interpolated or filled."""
        from src.kb.water_queries import series

        return gated(namespace, "water_series", lambda conn: series(
            conn, namespace, station, parameter, start, end, scopes=who()[1], as_of=as_of))

    @mcp.tool()
    def water_station_history(namespace: str, station: str) -> dict:
        """A station's revisions as location and gauge-zero/datum vintages, thresholds and withdrawals, each cited."""
        from src.kb.water_queries import station_history

        return gated(namespace, "water_station_history", lambda conn: station_history(
            conn, namespace, station, scopes=who()[1]))

    @mcp.tool()
    def water_for_place(namespace: str, place_id: str, as_of: str | None = None) -> dict:
        """Stations and water bodies of a place or river (accepted matches and containment with the stated geometry
        version), each water body's latest and historical WFD status per cycle, and unmatched records."""
        from src.kb.water_queries import for_place

        return gated(namespace, "water_for_place", lambda conn: for_place(
            conn, namespace, place_id, scopes=who()[1], as_of=as_of))

    @mcp.tool()
    def water_body_status_history(namespace: str, water_body: str, as_of: str | None = None) -> dict:
        """A water body's WFD ecological and chemical status per reporting cycle as published; cycles are never
        merged and Noesis makes no status assessment of its own."""
        from src.kb.water_queries import status_history

        return gated(namespace, "water_body_status_history", lambda conn: status_history(
            conn, namespace, water_body, scopes=who()[1], as_of=as_of))

    @mcp.tool()
    def propose_water_identity_matches(namespace: str) -> dict:
        """Propose station and water-body matches to places and rivers from published identifiers, coordinates and
        geometries (identifiers before names); nothing is accepted or merged; unmatched records are listed."""
        from src.kb.water_identity import WaterIdentity

        return gated(namespace, "propose_water_identity_matches", lambda conn: WaterIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_water_identity_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a station or water-body match with a reason; recorded as an entity identity decision."""
        from src.kb.water_identity import WaterIdentity

        return gated(namespace, "review_water_identity_match", lambda conn: WaterIdentity(conn).review(
            namespace, match_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def revert_water_identity_match(namespace: str, match_id: str, reason: str) -> dict:
        """Revert a reviewed station or water-body match (the entity identity decision is undone)."""
        from src.kb.water_identity import WaterIdentity

        return gated(namespace, "revert_water_identity_match", lambda conn: WaterIdentity(conn).revert(
            namespace, match_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def list_water_identity_matches(namespace: str, state: str | None = None, subject_key: str | None = None,
                                    place_id: str | None = None) -> dict:
        """Station and water-body matches with method, evidence, confidence, state and reviewer."""
        from src.kb.water_identity import WaterIdentity

        return gated(namespace, "list_water_identity_matches", lambda conn: {"matches": WaterIdentity(
            conn, initialize=False).matches(namespace, scopes=who()[1], state=state, subject_key=subject_key,
                                            place_id=place_id)})

    @mcp.tool()
    def link_water_records(namespace: str) -> dict:
        """Link stations and water bodies to hazard records and documents that cite them, weather stations that
        publish the relation, infrastructure assets sharing an identifier and places by accepted match; missing
        providers are reported; no causal links."""
        from src.kb.water_links import WaterLinks

        return gated(namespace, "link_water_records", lambda conn: WaterLinks(conn).discover(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def list_water_links(namespace: str, record_id: str | None = None, target_id: str | None = None) -> dict:
        """Water links with their basis and the record and target revisions they point at."""
        from src.kb.water_links import WaterLinks

        return gated(namespace, "list_water_links", lambda conn: {"links": WaterLinks(
            conn, initialize=False).links(namespace, scopes=who()[1], record_id=record_id, target_id=target_id)})

    @mcp.tool()
    def export_water_bundle(namespace: str, place_id: str | None = None, station: str | None = None,
                            water_body: str | None = None, as_of: str | None = None) -> dict:
        """The water evidence bundle for a place, station or water body; every item cites source, record revision
        and as-of time; no personal fields."""
        from src.kb.environment_bundle import water_section

        return gated(namespace, "export_water_bundle", lambda conn: water_section(
            conn, namespace, scopes=who()[1], place_id=place_id, station=station, water_body=water_body,
            as_of=as_of))

    @mcp.tool()
    def create_water_monitor(namespace: str, request_key: str, stations: list[str] | None = None,
                             rivers: list[str] | None = None, water_bodies: list[str] | None = None,
                             thresholds: list[str] | None = None, delivery: dict | None = None) -> dict:
        """Watch stations, rivers or water bodies for observations above the station's published thresholds,
        revisions (e.g. provisional to approved) and new assessment cycles; notices are record changes."""
        from src.kb.water_monitoring import WaterMonitor

        return gated(namespace, "create_water_monitor", lambda conn: WaterMonitor(conn).create(
            namespace, request_key, principal_id=who()[0], scopes=who()[1], stations=stations, rivers=rivers,
            water_bodies=water_bodies, thresholds=thresholds, delivery=delivery), write=True)

    @mcp.tool()
    def run_water_monitor(namespace: str, subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a water monitor at a complete source-pack watermark; replays deliver nothing."""
        from src.kb.water_monitoring import WaterMonitor

        return gated(namespace, "run_water_monitor", lambda conn: WaterMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def poll_water_monitor(namespace: str, subscription_id: str, cursor: str = "") -> dict:
        """Poll water monitor events after a cursor."""
        from src.kb.water_monitoring import WaterMonitor

        return gated(namespace, "poll_water_monitor", lambda conn: WaterMonitor(
            conn, initialize=False).poll(subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))
