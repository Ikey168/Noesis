"""Natural Hazards entry points: place events, revision history, alerts in force, correspondences, links, monitors.

Acquisition runs through the source-pack runtime (``natural-hazards`` source
pack, ``noesis-hazard-record-v1`` projector). Every tool except the provider
contracts checks the bundle's enablement first; geospatial, entity identity,
subscriptions and the source runtime are never gated by it. Answers quote the
issuing body with source, revision and as-of time. Declared exclusions: no
hazard prediction, risk scores, derived damage estimates, attribution of causes,
or evacuation, safety or protective-action advice. Place/alert queries and
correspondence proposals persist spatial receipts, so they are writes.
"""

from src.kb.hazards_bundle import BUNDLE, export_bundle, readiness, require_enabled, set_enabled

READ = "knowledge:hazards:read"
WRITE = "knowledge:hazards:write"
REVIEW = "knowledge:hazards:review"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
SCHEMA_REGISTER = "knowledge:schema:register"

HAZARDS_WRITES = {
    "set_natural_hazards_bundle_enabled", "register_hazard_schemas", "hazard_events_for_place",
    "hazard_alerts_in_force", "export_hazard_bundle", "propose_hazard_correspondences",
    "review_hazard_correspondence", "revert_hazard_correspondence", "resolve_hazard_places", "discover_hazard_links",
    "create_hazard_monitor", "run_hazard_monitor",
}
HAZARDS_READS = {
    "natural_hazards_bundle_status", "hazard_provider_contracts", "hazard_event_revisions",
    "list_hazard_correspondences", "list_hazard_links", "poll_hazard_monitor",
}
HAZARDS_TOOLS = HAZARDS_WRITES | HAZARDS_READS
HAZARDS_SCOPES = {
    "hazard_provider_contracts": [],
    "set_natural_hazards_bundle_enabled": ["operator"],
    "register_hazard_schemas": [WRITE, SCHEMA_REGISTER],
    "hazard_events_for_place": [READ],
    "hazard_alerts_in_force": [READ],
    "export_hazard_bundle": [READ],
    "review_hazard_correspondence": [REVIEW],
    "revert_hazard_correspondence": [REVIEW],
    "create_hazard_monitor": [WRITE, SUBSCRIPTIONS_WRITE],
    "run_hazard_monitor": [WRITE, SUBSCRIPTIONS_WRITE],
    "poll_hazard_monitor": [READ, SUBSCRIPTIONS_READ],
}
EXCLUSIONS = list(BUNDLE["never"])


def required_scopes(tool_name, mutability):
    return HAZARDS_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _also(scopes, required):
    missing = [scope for scope in required if scope not in scopes]
    if missing and "operator" not in scopes:
        from src.kb.hazards_store import HazardStoreError

        raise HazardStoreError("unauthorized", f"{missing[0]} scope is required")


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def gated(namespace, operation, *, write=False, scope=READ, also=()):
        def run(conn):
            require_enabled(conn, namespace)
            _also(who()[1], also)
            return operation(conn)
        return safe(run, write=write, required_scope=scope)

    @mcp.tool()
    def natural_hazards_bundle_status(namespace: str) -> dict:
        """Declared contributions, per-provider fixture-only/ready/stale/unavailable/key-gated state and exclusions."""
        return safe(lambda conn: {**readiness(conn, namespace, scopes=who()[1]), "declaration": BUNDLE},
                    required_scope=READ)

    @mcp.tool()
    def set_natural_hazards_bundle_enabled(namespace: str, enabled: bool) -> dict:
        """Enable/disable Natural Hazards (a coordinator selection change once composed); shared providers keep working."""
        return safe(lambda conn: set_enabled(conn, namespace, enabled, principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="operator")

    @mcp.tool()
    def hazard_provider_contracts() -> dict:
        """Per-provider access contracts (licence, rate limits, revision markers), bounded coverage and live state."""
        from src.ingestion.hazard_sources import BOUNDED_COVERAGE, LIVE_VERIFICATION, PROVIDER_CONTRACTS
        return {"contracts": PROVIDER_CONTRACTS, "coverage": BOUNDED_COVERAGE, "live_verification": LIVE_VERIFICATION,
                "exclusions": EXCLUSIONS}

    @mcp.tool()
    def register_hazard_schemas(namespace: str) -> dict:
        """Register noesis-hazard-record-v1 in the schema registry."""
        from src.kb.hazards_records import register_schemas
        return gated(namespace, lambda conn: {"modules": register_schemas(conn, principal_id=who()[0], scopes=who()[1])},
                     write=True, scope=WRITE, also=(SCHEMA_REGISTER,))

    @mcp.tool()
    def hazard_events_for_place(namespace: str, start: str, end: str, place_id: str | None = None,
                                point: list[float] | None = None, radius_m: float | None = None,
                                bbox: list[float] | None = None, as_of: str | None = None, basis: str = "publisher",
                                hazard_types: list[str] | None = None) -> dict:
        """Events whose published geometry relates to the place in the window, as known at as_of, with revision history."""
        from src.kb.hazards_queries import events_affecting
        return gated(namespace, lambda conn: events_affecting(
            conn, namespace, start=start, end=end, scopes=who()[1], principal_id=who()[0], place_id=place_id,
            point=point, radius_m=radius_m, bbox=bbox, as_of=as_of, basis=basis, hazard_types=hazard_types),
            write=True, scope=READ)

    @mcp.tool()
    def hazard_event_revisions(namespace: str, record_id: str) -> dict:
        """Every published revision of one hazard record with the parameter changes each made, source and update time."""
        from src.kb.hazards_store import HazardStore
        return gated(namespace, lambda conn: HazardStore(conn, initialize=False).revisions(namespace, record_id,
                                                                                          scopes=who()[1]))

    @mcp.tool()
    def hazard_alerts_in_force(namespace: str, at: str, place_id: str | None = None, point: list[float] | None = None,
                               radius_m: float | None = None, country: str | None = None,
                               as_of: str | None = None) -> dict:
        """Alerts and advisories whose published validity covered the place at a time, quoted with supersession chain."""
        from src.kb.hazards_queries import alerts_in_force
        return gated(namespace, lambda conn: alerts_in_force(
            conn, namespace, at=at, scopes=who()[1], principal_id=who()[0], place_id=place_id, point=point,
            radius_m=radius_m, country=country, as_of=as_of), write=True, scope=READ)

    @mcp.tool()
    def export_hazard_bundle(namespace: str, start: str, end: str, place_id: str | None = None,
                             point: list[float] | None = None, radius_m: float | None = None,
                             as_of: str | None = None, alerts_at: str | None = None) -> dict:
        """A noesis-evidence-bundle-v1 citing every record with source, revision and as-of time."""
        from src.kb.hazards_queries import alerts_in_force, events_affecting

        def run(conn):
            principal, scopes = who()
            answer = events_affecting(conn, namespace, start=start, end=end, scopes=scopes, principal_id=principal,
                                      place_id=place_id, point=point, radius_m=radius_m, as_of=as_of)
            alerts = alerts_in_force(conn, namespace, at=alerts_at, scopes=scopes, principal_id=principal,
                                     place_id=place_id, point=point, radius_m=radius_m) if alerts_at else None
            return export_bundle(answer, alerts=alerts)
        return gated(namespace, run, write=True, scope=READ)

    @mcp.tool()
    def propose_hazard_correspondences(namespace: str) -> dict:
        """Correspondence candidates across publishers (shared ID, GLIDE, origin proximity); nothing is merged."""
        from src.kb.hazards_identity import HazardIdentity
        return gated(namespace, lambda conn: HazardIdentity(conn).propose(namespace, principal_id=who()[0],
                                                                          scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def review_hazard_correspondence(namespace: str, correspondence_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a correspondence (another principal; recorded as an entity-identity decision, merge=false)."""
        from src.kb.hazards_identity import HazardIdentity
        return gated(namespace, lambda conn: HazardIdentity(conn).review(namespace, correspondence_id, decision, reason,
                                                                         principal_id=who()[0], scopes=who()[1]),
                     write=True, scope=REVIEW)

    @mcp.tool()
    def revert_hazard_correspondence(namespace: str, correspondence_id: str, reason: str) -> dict:
        """Revert an accepted or rejected correspondence (undoes the identity decision)."""
        from src.kb.hazards_identity import HazardIdentity
        return gated(namespace, lambda conn: HazardIdentity(conn).revert(namespace, correspondence_id, reason,
                                                                         principal_id=who()[0], scopes=who()[1]),
                     write=True, scope=REVIEW)

    @mcp.tool()
    def list_hazard_correspondences(namespace: str, record_id: str | None = None, state: str | None = None) -> dict:
        """Correspondences with method, evidence, confidence and review history, plus unmatched events."""
        from src.kb.hazards_identity import HazardIdentity

        def run(conn):
            identity = HazardIdentity(conn, initialize=False)
            return {"correspondences": identity.correspondences(namespace, scopes=who()[1], record_id=record_id,
                                                                state=state),
                    "unmatched": identity.unmatched(namespace, scopes=who()[1])}
        return gated(namespace, run)

    @mcp.tool()
    def resolve_hazard_places(namespace: str, record_id: str) -> dict:
        """Relate a record's published geometry to geospatial places, recording the boundary vintage used."""
        from src.kb.hazards_identity import HazardIdentity
        return gated(namespace, lambda conn: HazardIdentity(conn).resolve_places(namespace, record_id, principal_id=who()[0],
                                                                                 scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def discover_hazard_links(namespace: str, record_id: str) -> dict:
        """Links from explicit citations, shared identifiers or accepted correspondences; missing providers reported."""
        from src.kb.hazards_links import HazardLinks
        return gated(namespace, lambda conn: HazardLinks(conn).discover(namespace, record_id, principal_id=who()[0],
                                                                        scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def list_hazard_links(namespace: str, record_id: str | None = None) -> dict:
        """Recorded links with basis and pinned revisions; no causal claims."""
        from src.kb.hazards_links import HazardLinks
        return gated(namespace, lambda conn: {"links": HazardLinks(conn, initialize=False).links(
            namespace, scopes=who()[1], record_id=record_id)})

    @mcp.tool()
    def create_hazard_monitor(namespace: str, request_key: str, place_id: str | None = None,
                              point: list[float] | None = None, radius_m: float | None = None,
                              bbox: list[float] | None = None, watch: list[str] | None = None) -> dict:
        """Subscribe to record changes (new events, revisions, advisories, alerts) for an area; not a warning channel."""
        from src.kb.hazards_monitoring import WATCHES, HazardMonitor
        return gated(namespace, lambda conn: HazardMonitor(conn).create(
            namespace, request_key, principal_id=who()[0], scopes=who()[1], place_id=place_id, point=point,
            radius_m=radius_m, bbox=bbox, watch=watch or WATCHES), write=True, scope=WRITE, also=(SUBSCRIPTIONS_WRITE,))

    @mcp.tool()
    def run_hazard_monitor(namespace: str, subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a monitor at a committed watermark; returns cited record-change notices (idempotent)."""
        from src.kb.hazards_monitoring import HazardMonitor
        return gated(namespace, lambda conn: HazardMonitor(conn).run(subscription_id, watermark, principal_id=who()[0],
                                                                     scopes=who()[1]),
                     write=True, scope=WRITE, also=(SUBSCRIPTIONS_WRITE,))

    @mcp.tool()
    def poll_hazard_monitor(namespace: str, subscription_id: str, cursor: str = "") -> dict:
        """Poll a monitor's delivered record-change events."""
        from src.kb.hazards_monitoring import HazardMonitor
        return gated(namespace, lambda conn: HazardMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor), also=(SUBSCRIPTIONS_READ,))
