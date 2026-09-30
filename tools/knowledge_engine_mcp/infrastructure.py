"""Geospatial infrastructure feature entry points: acquisition, as-of answers, identity, links and monitors.

Every answer cites each asset per source and release. Status keeps the publisher's vocabulary; capacities
keep units and directions; owner and operator assertions keep the published name and share. Nothing
assesses vulnerability or criticality or values an asset. Optional links degrade cleanly: without
Corporate Ownership, Energy Systems, Legal or Climate and Environment, the corresponding step reports
``skipped``. Scopes for an optional link target (energy, legal or environment namespace) are checked at
call time.
"""

READ = "knowledge:infrastructure:read"
WRITE = "knowledge:infrastructure:write"
REVIEW = "knowledge:infrastructure:review"
INGEST = "knowledge:ingestion:execute"
SCHEMA_REGISTER = "knowledge:schema:register"
GEO_READ = "knowledge:geospatial:read"
GEO_CALCULATE = "knowledge:geospatial:calculate"
OWNERSHIP_READ = "knowledge:ownership:read"
OWNERSHIP_WRITE = "knowledge:ownership:write"
OWNERSHIP_REVIEW = "knowledge:ownership:review"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
TARGET_SCOPES = {"energy": "knowledge:energy:read", "legal": "knowledge:legal:read",
                 "environment": "knowledge:environment:read"}

INFRASTRUCTURE_WRITES = {
    "acquire_infrastructure_source", "register_infrastructure_schemas", "infrastructure_assets_in_place",
    "propose_infrastructure_asset_matches", "review_infrastructure_asset_match",
    "propose_infrastructure_operator_matches", "review_infrastructure_operator_match",
    "link_infrastructure_records", "review_infrastructure_link", "create_infrastructure_monitor",
    "run_infrastructure_monitor",
}
INFRASTRUCTURE_READS = {
    "infrastructure_source_contracts", "infrastructure_readiness", "list_infrastructure_assets",
    "infrastructure_asset_history", "infrastructure_assets_of_operator", "list_infrastructure_asset_matches",
    "list_infrastructure_operator_candidates", "list_infrastructure_links", "poll_infrastructure_monitor",
}
INFRASTRUCTURE_TOOLS = INFRASTRUCTURE_WRITES | INFRASTRUCTURE_READS
INFRASTRUCTURE_SCOPES = {
    "infrastructure_source_contracts": [],
    "infrastructure_readiness": [READ],
    "acquire_infrastructure_source": [WRITE, INGEST],
    "register_infrastructure_schemas": [WRITE, SCHEMA_REGISTER],
    "list_infrastructure_assets": [READ],
    "infrastructure_asset_history": [READ],
    "infrastructure_assets_in_place": [READ, GEO_CALCULATE, GEO_READ],
    "infrastructure_assets_of_operator": [READ],
    "propose_infrastructure_asset_matches": [WRITE, GEO_CALCULATE],
    "review_infrastructure_asset_match": [REVIEW],
    "list_infrastructure_asset_matches": [READ],
    "propose_infrastructure_operator_matches": [WRITE, OWNERSHIP_READ, OWNERSHIP_WRITE],
    "review_infrastructure_operator_match": [REVIEW, OWNERSHIP_REVIEW],
    "list_infrastructure_operator_candidates": [READ],
    "link_infrastructure_records": [WRITE],
    "review_infrastructure_link": [REVIEW],
    "list_infrastructure_links": [READ],
    "create_infrastructure_monitor": [WRITE, SUBSCRIPTIONS_WRITE],
    "run_infrastructure_monitor": [WRITE, SUBSCRIPTIONS_WRITE],
    "poll_infrastructure_monitor": [READ, SUBSCRIPTIONS_READ],
}


def required_scopes(tool_name, mutability):
    return INFRASTRUCTURE_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _require(scopes, *required):
    from src.kb.infrastructure_assets import InfrastructureError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise InfrastructureError("unauthorized", f"{', '.join(missing)} required")


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def scoped(tool, operation, *, write=False, extra=()):
        """Checks every declared scope (and any needed for optional arguments), then runs."""
        scopes = INFRASTRUCTURE_SCOPES[tool]

        def run(conn):
            _require(who()[1], *scopes, *extra)
            return operation(conn)

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def infrastructure_source_contracts() -> dict:
        """Per-source access, licence and release decisions, bounded coverage, exclusions and live verification."""
        from src.ingestion.infrastructure_sources import (
            BOUNDED_COVERAGE,
            EXCLUDED,
            LIVE_VERIFICATION,
            NEVER,
            PROVIDER_CONTRACTS,
        )

        return {"contracts": PROVIDER_CONTRACTS, "bounded_coverage": BOUNDED_COVERAGE, "excluded": EXCLUDED,
                "live_verification": LIVE_VERIFICATION, "boundary": NEVER}

    @mcp.tool()
    def infrastructure_readiness(namespace: str) -> dict:
        """Whether the Geospatial infrastructure feature is selected, per-source state and optional links."""
        from src.kb.infrastructure_queries import readiness

        return scoped("infrastructure_readiness", lambda conn: readiness(conn, namespace, scopes=who()[1]))

    @mcp.tool()
    def acquire_infrastructure_source(namespace: str, provider: str, selection: dict, observation: str,
                                      budget_id: str, reuse_notice: str, max_requests: int = 5) -> dict:
        """Bounded acquisition of one explicit selection (GPPD, GEM release, Overpass, EIA layer, ENTSOG), receipted."""

        def run(conn):
            from src.ingestion.infrastructure_sources import PROVIDER_HOSTS, acquire, durable_fetch
            from src.ingestion.provider_execution import DurableHTTP

            if provider not in PROVIDER_HOSTS:
                raise ValueError("unknown infrastructure provider")
            principal, scopes = who()
            http = DurableHTTP(conn, budget_id=budget_id, provider=provider, principal_id=principal,
                               allowed_hosts=PROVIDER_HOSTS[provider], reuse_notice=reuse_notice,
                               max_requests=max_requests, account_ref="public")
            fetch = durable_fetch(http, principal_id=principal, observation=observation)
            return acquire(conn, provider, selection, namespace=namespace, scopes=scopes, principal_id=principal,
                           fetch=fetch, execution="network")

        return scoped("acquire_infrastructure_source", run, write=True)

    @mcp.tool()
    def register_infrastructure_schemas(namespace: str) -> dict:
        """Register noesis-infrastructure-asset-record-v1 (1.0.0) in the schema registry."""
        from src.kb.infrastructure_assets import register_schemas

        del namespace
        return scoped("register_infrastructure_schemas",
                      lambda conn: {"modules": register_schemas(conn, principal_id=who()[0], scopes=who()[1])},
                      write=True)

    @mcp.tool()
    def list_infrastructure_assets(namespace: str, provider: str | None = None, asset_class: str | None = None) -> dict:
        """Stored assets (provider, dataset, native id, class); each publication is a revision of one of them."""
        from src.kb.infrastructure_assets import InfrastructureStore

        return scoped("list_infrastructure_assets", lambda conn: {"assets": InfrastructureStore(
            conn, initialize=False).assets(namespace, scopes=who()[1], provider=provider, asset_class=asset_class)})

    @mcp.tool()
    def infrastructure_asset_history(namespace: str, asset_id: str) -> dict:
        """Every revision of one asset with its status, capacity and owner-assertion histories and citations."""
        from src.kb.infrastructure_queries import InfrastructureQueries

        return scoped("infrastructure_asset_history",
                      lambda conn: InfrastructureQueries(conn).asset_history(namespace, asset_id, scopes=who()[1]))

    @mcp.tool()
    def infrastructure_assets_in_place(namespace: str, place_id: str | None = None, geometry_id: str | None = None,
                                       place_name: str | None = None, bbox: list[float] | None = None,
                                       as_of: str | None = None, known_by: str | None = None,
                                       asset_classes: list[str] | None = None, evidence_bundle: bool = False) -> dict:
        """Assets located in a place (polygon or bbox) as of a date: status, history, capacity, owners per source."""
        from src.kb.infrastructure_queries import InfrastructureQueries

        def run(conn):
            queries = InfrastructureQueries(conn)
            answer = queries.assets_in_place(namespace, place_id=place_id, geometry_id=geometry_id,
                                             place_name=place_name, bbox=bbox, as_of=as_of, known_by=known_by,
                                             asset_classes=asset_classes, scopes=who()[1], principal_id=who()[0])
            return {**answer, "evidence_bundle": queries.export_bundle(answer)} if evidence_bundle else answer

        return scoped("infrastructure_assets_in_place", run, write=True)

    @mcp.tool()
    def infrastructure_assets_of_operator(namespace: str, operator: str, as_of: str | None = None,
                                          known_by: str | None = None, evidence_bundle: bool = False) -> dict:
        """Assets under an operator (ownership record key or entity id via accepted matches, or a published name)."""
        from src.kb.infrastructure_queries import InfrastructureQueries

        def run(conn):
            queries = InfrastructureQueries(conn)
            answer = queries.assets_of_operator(namespace, operator, as_of=as_of, known_by=known_by, scopes=who()[1])
            return {**answer, "evidence_bundle": queries.export_bundle(answer)} if evidence_bundle else answer

        return scoped("infrastructure_assets_of_operator", run)

    @mcp.tool()
    def propose_infrastructure_asset_matches(namespace: str) -> dict:
        """Identifier matches from published cross-references and proximity+name+class review candidates."""
        from src.kb.infrastructure_identity import InfrastructureIdentity

        return scoped("propose_infrastructure_asset_matches", lambda conn: InfrastructureIdentity(conn).
                      propose_asset_matches(namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_infrastructure_asset_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept, reject or revert an asset match (append-only; records stay intact)."""
        from src.kb.infrastructure_identity import InfrastructureIdentity

        return scoped("review_infrastructure_asset_match", lambda conn: InfrastructureIdentity(conn).
                      review_asset_match(namespace, match_id, decision, reason, principal_id=who()[0],
                                         scopes=who()[1]), write=True)

    @mcp.tool()
    def list_infrastructure_asset_matches(namespace: str, state: str | None = None, asset_id: str | None = None) -> dict:
        """Asset matches with basis, distance, score, evidence and review history."""
        from src.kb.infrastructure_identity import InfrastructureIdentity

        return scoped("list_infrastructure_asset_matches", lambda conn: {"matches": InfrastructureIdentity(
            conn, initialize=False).asset_matches(namespace, scopes=who()[1], state=state, asset_id=asset_id)})

    @mcp.tool()
    def propose_infrastructure_operator_matches(namespace: str, ownership_namespace: str) -> dict:
        """Offer published operators/owners to Corporate Ownership entities (skipped when not installed)."""
        from src.kb.infrastructure_identity import InfrastructureIdentity

        return scoped("propose_infrastructure_operator_matches", lambda conn: InfrastructureIdentity(conn).
                      propose_operator_matches(namespace, ownership_namespace=ownership_namespace,
                                               principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_infrastructure_operator_match(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept, reject or revert an operator candidate (entity identity decision); assertions are untouched."""
        from src.kb.infrastructure_identity import InfrastructureIdentity

        return scoped("review_infrastructure_operator_match", lambda conn: InfrastructureIdentity(conn).
                      review_operator_match(namespace, candidate_id, decision, reason, principal_id=who()[0],
                                            scopes=who()[1]), write=True)

    @mcp.tool()
    def list_infrastructure_operator_candidates(namespace: str, state: str | None = None) -> dict:
        """Operator/owner candidates from infrastructure parties in the shared ownership identity state machine."""
        from src.kb.infrastructure_identity import InfrastructureIdentity

        return scoped("list_infrastructure_operator_candidates", lambda conn: {"candidates": InfrastructureIdentity(
            conn, initialize=False).operator_candidates(namespace, scopes=who()[1], state=state)})

    @mcp.tool()
    def link_infrastructure_records(namespace: str, energy_namespace: str | None = None,
                                    legal_namespace: str | None = None, environment_namespace: str | None = None) -> dict:
        """Citation links to Energy Systems series, legal works and environment facilities; absent packs skipped."""
        from src.kb.infrastructure_identity import InfrastructureIdentity

        extra = [TARGET_SCOPES[k] for k, v in (("energy", energy_namespace), ("legal", legal_namespace),
                                               ("environment", environment_namespace)) if v]

        def run(conn):
            identity = InfrastructureIdentity(conn)
            principal, scopes = who()
            result = {}
            if energy_namespace:
                result["energy"] = identity.link_energy(namespace, energy_namespace=energy_namespace,
                                                        principal_id=principal, scopes=scopes)
            if legal_namespace:
                result["legal"] = identity.link_legal(namespace, legal_namespace=legal_namespace,
                                                      principal_id=principal, scopes=scopes)
            if environment_namespace:
                result["environment"] = identity.link_environment(namespace, environment_namespace=environment_namespace,
                                                                  principal_id=principal, scopes=scopes)
            return result

        return scoped("link_infrastructure_records", run, write=True, extra=extra)

    @mcp.tool()
    def review_infrastructure_link(namespace: str, link_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a candidate link (name or proximity overlap is never a link by itself)."""
        from src.kb.infrastructure_identity import InfrastructureIdentity

        return scoped("review_infrastructure_link", lambda conn: InfrastructureIdentity(conn).review_link(
            namespace, link_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def list_infrastructure_links(namespace: str, asset_id: str | None = None) -> dict:
        """Citation links and candidates with their citing revision and review state."""
        from src.kb.infrastructure_identity import InfrastructureIdentity

        return scoped("list_infrastructure_links", lambda conn: InfrastructureIdentity(
            conn, initialize=False).links(namespace, asset_id, scopes=who()[1]))

    @mcp.tool()
    def create_infrastructure_monitor(namespace: str, request_key: str, subject: dict) -> dict:
        """Watch a place, operator or asset for status, capacity and ownership changes (a knowledge subscription)."""
        from src.kb.infrastructure_monitoring import InfrastructureMonitor

        return scoped("create_infrastructure_monitor", lambda conn: InfrastructureMonitor(conn).create(
            namespace, request_key, subject=subject, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def run_infrastructure_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a monitor at a committed watermark; notifications cite the old and new revisions."""
        from src.kb.infrastructure_monitoring import InfrastructureMonitor

        return scoped("run_infrastructure_monitor", lambda conn: InfrastructureMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def poll_infrastructure_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a monitor's delivered events."""
        from src.kb.infrastructure_monitoring import InfrastructureMonitor

        return scoped("poll_infrastructure_monitor", lambda conn: InfrastructureMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))
