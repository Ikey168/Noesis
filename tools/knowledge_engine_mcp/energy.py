"""Energy Systems entry points: acquisition, identity, links, as-of queries and monitors.

Every tool except the source contracts checks each scope it declares and the
bundle's enablement first; Climate and Environment, Market, Geospatial and the
platform providers are never gated by it. Answers keep sources side by side as
published per release; nothing forecasts, models dispatch, estimates emissions
or advises on trading.
"""

READ = "knowledge:energy:read"
WRITE = "knowledge:energy:write"
REVIEW = "knowledge:energy:review"
INGEST = "knowledge:ingestion:execute"
SCHEMA_REGISTER = "knowledge:schema:register"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
ENVIRONMENT_READ = "knowledge:environment:read"
PRICES_WRITE = "market:prices:write"

ENERGY_WRITES = {
    "set_energy_bundle_enabled", "acquire_energy_source", "register_energy_schemas", "publish_energy_prices_to_market",
    "propose_energy_identity_matches", "review_energy_identity_match", "link_energy_records", "link_energy_facility",
    "create_energy_monitor", "run_energy_monitor",
}
ENERGY_READS = {
    "energy_source_contracts", "energy_bundle_status", "list_energy_series", "energy_generation_mix",
    "energy_observations", "energy_revision_history", "energy_capacity_as_of", "list_energy_identity_matches",
    "list_energy_links", "poll_energy_monitor",
}
ENERGY_TOOLS = ENERGY_WRITES | ENERGY_READS
ENERGY_SCOPES = {
    "energy_source_contracts": [],
    "energy_bundle_status": [READ],
    "set_energy_bundle_enabled": ["operator"],
    "acquire_energy_source": [WRITE, INGEST],
    "register_energy_schemas": [WRITE, SCHEMA_REGISTER],
    "publish_energy_prices_to_market": [WRITE, PRICES_WRITE],
    "list_energy_series": [READ],
    "energy_generation_mix": [READ],
    "energy_observations": [READ],
    "energy_revision_history": [READ],
    "energy_capacity_as_of": [READ],
    "propose_energy_identity_matches": [WRITE],
    "review_energy_identity_match": [REVIEW],
    "list_energy_identity_matches": [READ],
    "link_energy_records": [WRITE],
    "link_energy_facility": [WRITE],
    "list_energy_links": [READ],
    "create_energy_monitor": [WRITE, SUBSCRIPTIONS_WRITE],
    "run_energy_monitor": [WRITE, SUBSCRIPTIONS_WRITE],
    "poll_energy_monitor": [READ, SUBSCRIPTIONS_READ],
}


def required_scopes(tool_name, mutability):
    return ENERGY_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _require(scopes, *required):
    from src.kb.energy_store import EnergyStoreError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise EnergyStoreError("unauthorized", f"{', '.join(missing)} required")


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def gated(namespace, tool, operation, *, write=False, extra=()):
        """Checks every declared scope (and any needed for optional arguments), then the bundle, then runs."""
        scopes = ENERGY_SCOPES[tool]

        def run(conn):
            from src.kb.energy_bundle import require_enabled

            _require(who()[1], *scopes, *extra)
            require_enabled(conn, namespace)
            return operation(conn)

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def energy_source_contracts() -> dict:
        """Per-source access decisions (ENTSO-E, EIA, Ember, Eurostat, Energy-Charts), bounded coverage and live state."""
        from src.ingestion.energy_sources import BOUNDED_COVERAGE, LIVE_VERIFICATION, NEVER, PROVIDER_CONTRACTS

        return {"contracts": PROVIDER_CONTRACTS, "bounded_coverage": BOUNDED_COVERAGE,
                "live_verification": LIVE_VERIFICATION, "boundary": NEVER}

    @mcp.tool()
    def energy_bundle_status(namespace: str) -> dict:
        """Declared contributions plus ready/fixture-only/stale/unavailable per provider; live verification separate."""
        from src.kb.energy_bundle import BUNDLE, readiness

        return safe(lambda conn: {**readiness(conn, namespace, scopes=who()[1]), "declaration": BUNDLE},
                    required_scope=READ)

    @mcp.tool()
    def set_energy_bundle_enabled(namespace: str, enabled: bool) -> dict:
        """Enable/disable Energy Systems (a coordinator selection change once composed); shared providers keep working."""
        from src.kb.energy_bundle import set_enabled

        return safe(lambda conn: set_enabled(conn, namespace, enabled, principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="operator")

    @mcp.tool()
    def acquire_energy_source(namespace: str, provider: str, selection: dict, observation: str, budget_id: str,
                              reuse_notice: str, max_requests: int = 20) -> dict:
        """Bounded acquisition of one explicit selection through DurableHTTP, one receipt per step (keys from NOESIS_*)."""

        def run(conn):
            from src.config.env import resolve_env
            from src.ingestion.energy_sources import PROVIDER_HOSTS, SECRETS, acquire, durable_fetch
            from src.ingestion.provider_execution import DurableHTTP

            if provider not in PROVIDER_HOSTS:
                raise ValueError("unknown energy provider")
            principal, scopes = who()
            secret = resolve_env(SECRETS[provider][2]) if provider in SECRETS else None
            http = DurableHTTP(conn, budget_id=budget_id, provider=provider, principal_id=principal,
                               allowed_hosts=PROVIDER_HOSTS[provider], reuse_notice=reuse_notice,
                               max_requests=max_requests, account_ref="configured" if secret else "public")
            fetch = durable_fetch(http, principal_id=principal, observation=observation)
            return acquire(conn, provider, selection, namespace=namespace, scopes=scopes, principal_id=principal,
                           fetch=fetch, secret=secret, execution="network")

        return gated(namespace, "acquire_energy_source", run, write=True)

    @mcp.tool()
    def register_energy_schemas(namespace: str) -> dict:
        """Register noesis-energy-record-v1 (schema version 1.0.0) in the schema registry."""
        from src.kb.energy_records import register_schemas

        return gated(namespace, "register_energy_schemas",
                     lambda conn: {"modules": register_schemas(conn, principal_id=who()[0], scopes=who()[1])},
                     write=True)

    @mcp.tool()
    def publish_energy_prices_to_market(namespace: str, vintage_id: str, listing_id: str, entitlement_id: str) -> dict:
        """Write one price vintage through Market storage (market bars citing the vintage); negative prices refused."""
        from src.kb.energy_market import publish_prices

        return gated(namespace, "publish_energy_prices_to_market",
                     lambda conn: publish_prices(conn, namespace, vintage_id, listing_id=listing_id,
                                                 entitlement_id=entitlement_id, principal_id=who()[0],
                                                 scopes=who()[1]), write=True)

    @mcp.tool()
    def list_energy_series(namespace: str, record_type: str | None = None, provider: str | None = None) -> dict:
        """Stored series (provider, dataset, subject, unit); every release is a vintage of one of them."""
        from src.kb.energy_store import EnergyStore

        return gated(namespace, "list_energy_series", lambda conn: {"series": EnergyStore(conn, initialize=False).series(
            namespace, scopes=who()[1], record_type=record_type, provider=provider)})

    @mcp.tool()
    def energy_generation_mix(namespace: str, subject: str, start: str | None = None, end: str | None = None,
                              as_of_ms: int | None = None) -> dict:
        """Generation by fuel for a zone, country, balancing area or plant, per source, as published at as_of_ms."""
        from src.kb.energy_queries import EnergyQueries

        return gated(namespace, "energy_generation_mix", lambda conn: EnergyQueries(conn).generation_mix(
            namespace, subject, scopes=who()[1], start=start, end=end, as_of_ms=as_of_ms))

    @mcp.tool()
    def energy_observations(namespace: str, subject: str, record_types: list[str], start: str | None = None,
                            end: str | None = None, as_of_ms: int | None = None) -> dict:
        """Load, price, flow, capacity or balance series for a subject, per source, as published at as_of_ms."""
        from src.kb.energy_queries import EnergyQueries

        return gated(namespace, "energy_observations", lambda conn: EnergyQueries(conn).observations(
            namespace, subject, set(record_types), scopes=who()[1], start=start, end=end, as_of_ms=as_of_ms))

    @mcp.tool()
    def energy_revision_history(namespace: str, series_id: str, period_start: str | None = None) -> dict:
        """Every vintage of a series (or of one figure) with release dates, status, citations and what changed."""
        from src.kb.energy_queries import EnergyQueries

        return gated(namespace, "energy_revision_history", lambda conn: EnergyQueries(conn).revision_history(
            namespace, series_id, scopes=who()[1], period_start=period_start))

    @mcp.tool()
    def energy_capacity_as_of(namespace: str, subject: str, date: str, as_of_ms: int | None = None) -> dict:
        """Zone, plant or unit capacity effective on a date, per source; missing data is reported as unknown."""
        from src.kb.energy_queries import EnergyQueries

        return gated(namespace, "energy_capacity_as_of", lambda conn: EnergyQueries(conn).capacity_as_of(
            namespace, subject, date, scopes=who()[1], as_of_ms=as_of_ms))

    @mcp.tool()
    def propose_energy_identity_matches(namespace: str) -> dict:
        """Candidate matches of zones, areas, countries and plants to places/entities; ambiguous and unmatched listed."""
        from src.kb.energy_identity import EnergyIdentity

        return gated(namespace, "propose_energy_identity_matches", lambda conn: EnergyIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_energy_identity_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept, reject or revert one identity match (recorded with the geospatial or entity-history owner)."""
        from src.kb.energy_identity import EnergyIdentity

        return gated(namespace, "review_energy_identity_match", lambda conn: EnergyIdentity(conn).review(
            namespace, match_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def list_energy_identity_matches(namespace: str, state: str | None = None, subject_code: str | None = None) -> dict:
        """Identity matches with method, evidence and review history."""
        from src.kb.energy_identity import EnergyIdentity

        return gated(namespace, "list_energy_identity_matches", lambda conn: {"matches": EnergyIdentity(
            conn, initialize=False).matches(namespace, scopes=who()[1], state=state, subject_code=subject_code)})

    @mcp.tool()
    def link_energy_records(namespace: str, target: str, environment_namespace: str | None = None) -> dict:
        """Link by shared identifier or citation: target 'climate-environment' (same ENTSO-E document) or 'market'."""
        from src.kb.energy_links import EnergyLinks

        def run(conn):
            links = EnergyLinks(conn)
            principal, scopes = who()
            if target == "climate-environment":
                _require(scopes, ENVIRONMENT_READ)
                return links.link_environment(namespace, environment_namespace=environment_namespace or "environment",
                                              principal_id=principal, scopes=scopes, environment_scopes=scopes)
            if target == "market":
                return links.link_market(namespace, principal_id=principal, scopes=scopes)
            raise ValueError("target is climate-environment or market")

        return gated(namespace, "link_energy_records", run, write=True)

    @mcp.tool()
    def link_energy_facility(namespace: str, series_id: str, facility: dict, citation: dict) -> dict:
        """Link a plant/unit series to a facility record only through an accepted identity match (else blocked)."""
        from src.kb.energy_links import EnergyLinks

        return gated(namespace, "link_energy_facility", lambda conn: EnergyLinks(conn).link_facility(
            namespace, series_id, facility, citation=citation, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def list_energy_links(namespace: str, series_id: str | None = None) -> dict:
        """Links of a series with their basis, citation and identity state; 'no links on record' when none."""
        from src.kb.energy_links import EnergyLinks

        return gated(namespace, "list_energy_links", lambda conn: EnergyLinks(conn, initialize=False).links(
            namespace, series_id, scopes=who()[1]))

    @mcp.tool()
    def create_energy_monitor(namespace: str, request_key: str, subject: str, record_types: list[str] | None = None,
                              thresholds: list[dict] | None = None, watch: list[str] | None = None) -> dict:
        """Subscribe to a zone, country or plant: new releases, revisions, capacity changes, user thresholds."""
        from src.kb.energy_monitoring import WATCHES, EnergyMonitor

        return gated(namespace, "create_energy_monitor", lambda conn: EnergyMonitor(conn).create(
            namespace, request_key, subject=subject, principal_id=who()[0], scopes=who()[1], record_types=record_types,
            thresholds=thresholds or (), watch=watch or WATCHES), write=True)

    @mcp.tool()
    def run_energy_monitor(namespace: str, subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a monitor at a committed watermark; notifications cite old and new vintages."""
        from src.kb.energy_monitoring import EnergyMonitor

        return gated(namespace, "run_energy_monitor", lambda conn: EnergyMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def poll_energy_monitor(namespace: str, subscription_id: str, cursor: str = "") -> dict:
        """Poll a monitor's delivered subscription events."""
        from src.kb.energy_monitoring import EnergyMonitor

        return gated(namespace, "poll_energy_monitor", lambda conn: EnergyMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))
