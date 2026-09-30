"""Climate and Environment water and hydrology entry points (optional water features, #2582 WA11 #2638).

Registered through :func:`tools.knowledge_engine_mcp.environment.register`, so
the tools live in the knowledge-engine server beside the other Climate and
Environment tools and share its bundle gate (Geospatial, subscriptions and the
source runtime are never gated by it). PEGELONLINE, USGS and EEA WISE coverage
are the separate optional features ``water-pegelonline``, ``water-usgs`` and
``water-eea-wise``; hazard, weather and infrastructure links degrade to an
``unavailable`` report when those providers are absent.

Exclusions (#2582): no flood forecasting, no interpolation or gap filling, no
own status assessments and no flood-risk scoring. The WA01 minimisation
decision (no personal data) is enforced at write time and checked again on
every answer (:func:`minimised`).
"""

from __future__ import annotations

from src.kb.water_records import NEVER_SENTENCE

FEATURES = {"pegelonline": "water-pegelonline", "usgs": "water-usgs", "eea-wise": "water-eea-wise"}
WATER_WRITES = {
    "register_water_schemas", "propose_water_identity_matches", "review_water_identity_match",
    "revert_water_identity_match", "link_water_records", "create_water_monitor", "run_water_monitor",
}
WATER_TOOLS = WATER_WRITES | {
    "water_source_contracts", "water_readiness", "lookup_water_station", "water_value_at", "water_series",
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


def selected_features(conn) -> list[str]:
    """Which of the Climate and Environment bundle's optional water features are in the active plan."""
    import json

    try:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN ('composition_authority', "
            "'composition_active', 'composition_generations', 'composition_plans')").fetchall()}
        if len(tables) < 4:
            return []
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g ON g.generation_id="
            "a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1").fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return []
    chosen = set((plan.get("features") or {}).get("climate-environment") or [])
    return sorted(f for f in FEATURES.values() if f in chosen)


def minimised(answer):
    """Refuse to return an answer that carries a personal-data key (WA01 minimisation decision)."""
    from src.kb.water_records import WaterError, personal_keys

    found = personal_keys(answer)
    if found:
        raise WaterError("personal_field", f"answer would expose personal data: {found[:3]}")
    return answer


def readiness(conn, namespace):
    from src.ingestion.water_sources import (
        LIVE_VERIFICATION,
        NOT_IMPLEMENTED,
        PROVIDER_CONTRACTS,
    )
    from src.kb.water_links import linked_providers
    from src.kb.water_records import MINIMISATION
    from src.kb.water_store import TABLES, WaterStore, table_exists

    store = WaterStore(conn, initialize=False)
    selected = selected_features(conn)
    return {"bundle": "climate-environment", "features": {f: f in selected for f in FEATURES.values()},
            "default": False, "stores": {t: table_exists(conn, t) for t in TABLES},
            "providers": {p: {"feature": FEATURES[p], "selected": FEATURES[p] in selected, "licence": c["licence"],
                              "live_verification": LIVE_VERIFICATION[p]["status"],
                              "state": store.provider_state(namespace, p) if store.ready() else None}
                          for p, c in PROVIDER_CONTRACTS.items()},
            "not_implemented": NOT_IMPLEMENTED, "links": linked_providers(conn, {"operator"}),
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
            return minimised(operation(conn))

        return safe(run, write=write, required_scope=required[0] if required else None)

    @mcp.tool()
    def water_source_contracts() -> dict:
        """PEGELONLINE, USGS Water Data and EEA WISE access contracts, the GRDC not-implemented decision, the
        no-personal-data minimisation decision, bounded coverage and live state. No flood forecasting, gap filling,
        own status assessment or flood-risk scoring."""
        from src.ingestion.water_sources import (
            BOUNDED_COVERAGE,
            LIVE_VERIFICATION,
            NOT_IMPLEMENTED,
            PROVIDER_CONTRACTS,
        )
        from src.kb.water_records import MINIMISATION

        return {"contracts": PROVIDER_CONTRACTS, "not_implemented": NOT_IMPLEMENTED, "minimisation": MINIMISATION,
                "bounded_coverage": BOUNDED_COVERAGE, "live_verification": LIVE_VERIFICATION,
                "features": FEATURES, "boundary": NEVER_SENTENCE}

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
    def lookup_water_station(namespace: str, station: str, as_of: str | None = None) -> dict:
        """A gauging station (id, number or name) as published: river, location and gauge-zero/datum vintages,
        published time series and characteristic values, each vintage citing its revision."""
        from src.kb.water_identity import WaterIdentity
        from src.kb.water_queries import _ms
        from src.kb.water_store import WaterStore

        def run(conn):
            store = WaterStore(conn, initialize=False)
            store.require_ready()
            keys = [k for k in WaterIdentity(conn, initialize=False).find(namespace, station)
                    if not k.startswith("wfd:")]
            stations = []
            for record in store.records(namespace, record_type="station", subject_keys=keys):
                current = store.current(namespace, record["record_id"], as_of_ms=_ms(as_of))
                if current is None:
                    continue
                stations.append({"subject_key": record["subject_key"], "as_published": current["statement"][
                    "as_published"], "revision_id": current["revision_id"], "retrieved_at": current["retrieved_at"],
                    "source_url": current["statement"]["source"]["url"],
                    "vintages": store.station_vintages(namespace, record["record_id"], as_of_ms=_ms(as_of))})
            return {"query": station, "stations": stations,
                    "status": "stations on record" if stations else "no station on record", "notice": NEVER_SENTENCE}

        return gated(namespace, "lookup_water_station", run)

    @mcp.tool()
    def water_value_at(namespace: str, station: str, parameter: str, time: str, as_of: str | None = None) -> dict:
        """Water level or discharge at a station and time as on record at a date: the value, its quality state
        (provisional or approved) and qualifiers as published, the observation revision cited and later revisions.
        A time without a published value stays missing; nothing is interpolated or forecast."""
        from src.kb.water_queries import value_at

        return gated(namespace, "water_value_at", lambda conn: value_at(
            conn, namespace, station, parameter, time, scopes=who()[1], as_of=as_of))

    @mcp.tool()
    def water_series(namespace: str, station: str, parameter: str, start: str, end: str,
                     as_of: str | None = None) -> dict:
        """Published values in a window with quality per value; missing steps of the published interval are listed,
        never filled or resampled."""
        from src.kb.water_queries import series

        return gated(namespace, "water_series", lambda conn: series(
            conn, namespace, station, parameter, start=start, end=end, scopes=who()[1], as_of=as_of))

    @mcp.tool()
    def water_for_place(namespace: str, place_id: str, river: str | None = None, as_of: str | None = None) -> dict:
        """Stations (optionally on one river place) and water bodies of a place, by accepted identity matches or
        containment in the stated geometry version, with latest values and status per reporting cycle, all cited."""
        from src.kb.water_queries import place_water

        return gated(namespace, "water_for_place", lambda conn: place_water(
            conn, namespace, place_id, scopes=who()[1], river=river, as_of=as_of))

    @mcp.tool()
    def water_body_status_history(namespace: str, water_body: str, as_of: str | None = None) -> dict:
        """A water body's ecological and chemical status per WFD reporting cycle as reported; cycles are never
        merged and no trend or Noesis assessment is derived."""
        from src.kb.water_queries import status_history

        return gated(namespace, "water_body_status_history", lambda conn: status_history(
            conn, namespace, water_body, scopes=who()[1], as_of=as_of))

    @mcp.tool()
    def propose_water_identity_matches(namespace: str, place_ids: list[str] | None = None) -> dict:
        """Offer station and water-body to place/river candidates for review (none accepted): published identifiers
        first, then published coordinates or geometry with contains receipts; unmatched records are listed."""
        from src.kb.water_identity import WaterIdentity

        return gated(namespace, "propose_water_identity_matches", lambda conn: WaterIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1], place_ids=place_ids), write=True)

    @mcp.tool()
    def review_water_identity_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a water identity proposal with a reason; recorded as an entity identity decision."""
        from src.kb.water_identity import WaterIdentity

        return gated(namespace, "review_water_identity_match", lambda conn: WaterIdentity(conn).review(
            namespace, match_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def revert_water_identity_match(namespace: str, match_id: str, reason: str) -> dict:
        """Revert a reviewed water identity decision (the entity identity decision is undone)."""
        from src.kb.water_identity import WaterIdentity

        return gated(namespace, "revert_water_identity_match", lambda conn: WaterIdentity(conn).revert(
            namespace, match_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def list_water_identity_matches(namespace: str, state: str | None = None, subject_key: str | None = None,
                                    place_id: str | None = None) -> dict:
        """Water identity proposals and decisions with method, evidence class and evidence."""
        from src.kb.water_identity import WaterIdentity

        return gated(namespace, "list_water_identity_matches", lambda conn: {"matches": WaterIdentity(
            conn, initialize=False).matches(namespace, scopes=who()[1], state=state, subject_key=subject_key,
                                            place_id=place_id)})

    @mcp.tool()
    def link_water_records(namespace: str) -> dict:
        """Link stations and water bodies to flood events citing them, weather sources stating the gauge,
        infrastructure assets sharing an identifier and accepted places; absent providers are reported unavailable.
        No proximity or causal links."""
        from src.kb.water_links import WaterLinks

        return gated(namespace, "link_water_records", lambda conn: WaterLinks(conn).link(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def list_water_links(namespace: str, subject_key: str | None = None, target_kind: str | None = None) -> dict:
        """Water links with basis and both revisions."""
        from src.kb.water_links import WaterLinks

        return gated(namespace, "list_water_links", lambda conn: {"links": WaterLinks(
            conn, initialize=False).links(namespace, scopes=who()[1], subject_key=subject_key,
                                          target_kind=target_kind)})

    @mcp.tool()
    def export_water_bundle(namespace: str, place_id: str, as_of: str | None = None) -> dict:
        """The water section of a Climate and Environment place bundle; every item cites its source, record
        revision and retrieval time."""
        from src.kb.environment_bundle import water_section

        return gated(namespace, "export_water_bundle", lambda conn: water_section(
            conn, namespace, place_id, scopes=who()[1], as_of=as_of))

    @mcp.tool()
    def create_water_monitor(namespace: str, request_key: str, stations: list[str] | None = None,
                             rivers: list[str] | None = None, water_bodies: list[str] | None = None,
                             thresholds: list[str] | None = None, delivery: dict | None = None) -> dict:
        """Watch stations, rivers or water bodies for new values above published characteristic values, revised or
        withdrawn values, station changes and new reporting cycles. Notices are record changes, not assessments."""
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
