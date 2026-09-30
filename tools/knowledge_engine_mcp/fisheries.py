"""Fisheries and Maritime Activity entry points: acquisition, identity, citations, answers and monitors (#2222, FI12).

Every tool except the source contracts checks the bundle's enablement first;
sanctions, geospatial, entity identity, subscriptions and the source runtime
are never gated by it. Optional features (sanctions, geospatial) and citing
providers (OSINT movements, Agriculture & Food Systems) degrade to
``provider_unavailable`` when absent. No tool infers illegal fishing, derives
a legality status or recommends enforcement.
"""

from src.kb.fisheries_bundle import BUNDLE, readiness, require_enabled, set_enabled

FISHERIES_WRITES = {
    "set_fisheries_bundle_enabled", "acquire_fisheries_sources", "propose_fisheries_identities",
    "review_fisheries_identity", "revert_fisheries_identity", "link_fisheries_citations", "project_fisheries_areas",
    "create_fisheries_monitor", "run_fisheries_monitor",
}
FISHERIES_READS = {
    "fisheries_bundle_status", "fisheries_source_contracts", "fisheries_vessel_status", "fisheries_area_aggregates",
    "fisheries_vessel_identity", "list_fisheries_identity_matches", "list_fisheries_links", "poll_fisheries_monitor",
}
FISHERIES_TOOLS = FISHERIES_WRITES | FISHERIES_READS
FISHERIES_SCOPES = {
    "set_fisheries_bundle_enabled": ["operator"],
    "acquire_fisheries_sources": ["knowledge:fisheries:write", "knowledge:ingestion:execute"],
    "review_fisheries_identity": ["knowledge:fisheries:review"],
    "revert_fisheries_identity": ["knowledge:fisheries:review"],
    "project_fisheries_areas": ["knowledge:fisheries:write", "knowledge:geospatial:write"],
    "create_fisheries_monitor": ["knowledge:fisheries:read", "knowledge:subscriptions:write"],
    "run_fisheries_monitor": ["knowledge:fisheries:read", "knowledge:subscriptions:write"],
    "poll_fisheries_monitor": ["knowledge:fisheries:read", "knowledge:subscriptions:read"],
}


def required_scopes(tool_name, mutability):
    if tool_name == "fisheries_source_contracts":
        return []
    return FISHERIES_SCOPES.get(
        tool_name, ["knowledge:fisheries:write" if mutability == "write" else "knowledge:fisheries:read"])


def _also(scopes, required):
    missing = [scope for scope in required if scope not in scopes]
    if missing and "operator" not in scopes:
        from src.kb.fisheries_records import FisheriesError

        raise FisheriesError("unauthorized", f"{missing[0]} scope is required")


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def gated(namespace, operation, *, write=False, scope="knowledge:fisheries:read", also=()):
        def run(conn):
            require_enabled(conn, namespace)
            _also(who()[1], also)
            return operation(conn)
        return safe(run, write=write, required_scope=scope)

    @mcp.tool()
    def fisheries_bundle_status(namespace: str) -> dict:
        """Declared contributions, per-provider readiness and contract status, optional features; live kept separate."""
        return safe(lambda conn: {**readiness(conn, namespace, scopes=who()[1]), "declaration": BUNDLE},
                    required_scope="knowledge:fisheries:read")

    @mcp.tool()
    def set_fisheries_bundle_enabled(namespace: str, enabled: bool) -> dict:
        """Enable/disable Fisheries and Maritime Activity (a coordinator selection change once composed)."""
        return safe(lambda conn: set_enabled(conn, namespace, enabled, principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="operator")

    @mcp.tool()
    def fisheries_source_contracts() -> dict:
        """Per-provider access contracts, the GFW licence decision, bounded coverage and excluded RFMOs."""
        from src.ingestion.fisheries_sources import (
            EXCLUDED_FIELDS,
            EXCLUDED_RFMOS,
            LIVE_VERIFICATION,
            PROVIDER_CONTRACTS,
        )
        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "excluded_fields": list(EXCLUDED_FIELDS), "excluded_rfmos": EXCLUDED_RFMOS,
                "audit": "docs/development/fisheries-evidence/source-audit.md"}

    @mcp.tool()
    def acquire_fisheries_sources(namespace: str, run_key: str, source_ids: list[str] | None = None) -> dict:
        """Bounded, receipted run of the fisheries-maritime source pack's explicit selection (secrets from NOESIS_*)."""
        def run(conn):
            from src.config.env import resolve_env
            from src.ingestion.source_pack_runtime import SourcePackRuntime

            request = {"pack_id": "fisheries-maritime", "run_key": run_key, "operation": "fisheries",
                       **({"source_ids": list(source_ids)} if source_ids else {})}
            return SourcePackRuntime(conn).run(request, principal_id=who()[0], secret_resolver=resolve_env)
        return gated(namespace, run, write=True, scope="knowledge:fisheries:write",
                     also=("knowledge:ingestion:execute",))

    @mcp.tool()
    def fisheries_vessel_status(namespace: str, query: str | None = None, subject_key: str | None = None,
                                as_of: str | None = None, evidence_bundle: bool = False) -> dict:
        """A vessel's authorisations and IUU listings on a date with snapshots, identity matches and coverage."""
        from src.kb.fisheries_queries import FisheriesQueries

        def run(conn):
            queries = FisheriesQueries(conn)
            answer = queries.vessel_status_as_of(namespace, scopes=who()[1], as_of=as_of, query=query,
                                                 subject_key=subject_key)
            if evidence_bundle:
                answer["evidence_bundle"] = queries.export_bundle(namespace, answer, scopes=who()[1])["bundle"]
            return answer
        return gated(namespace, run)

    @mcp.tool()
    def fisheries_area_aggregates(namespace: str, area: str | None = None, flag: str | None = None,
                                  species: str | None = None, period_from: str | None = None,
                                  period_to: str | None = None, evidence_bundle: bool = False) -> dict:
        """GFW effort and FishStat catch side by side for an area, flag or species, with releases and gaps."""
        from src.kb.fisheries_queries import FisheriesQueries

        def run(conn):
            queries = FisheriesQueries(conn)
            answer = queries.aggregates(namespace, scopes=who()[1], area=area, flag=flag, species=species,
                                        period_from=period_from, period_to=period_to)
            if evidence_bundle:
                answer["evidence_bundle"] = queries.export_bundle(namespace, answer, scopes=who()[1])["bundle"]
            return answer
        return gated(namespace, run)

    @mcp.tool()
    def fisheries_vessel_identity(namespace: str, subject_key: str, as_of: str | None = None) -> dict:
        """One vessel's member records, accepted matches, pending candidates, identity history and parties."""
        from src.kb.fisheries_identity import FisheriesIdentity
        return gated(namespace, lambda conn: FisheriesIdentity(conn, initialize=False).vessel(
            namespace, subject_key, scopes=who()[1], as_of=as_of))

    @mcp.tool()
    def propose_fisheries_identities(namespace: str) -> dict:
        """Record IMO matches with evidence and offer name/flag/call-sign review candidates (records never merged)."""
        from src.kb.fisheries_identity import FisheriesIdentity
        return gated(namespace, lambda conn: FisheriesIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True, scope="knowledge:fisheries:write")

    @mcp.tool()
    def review_fisheries_identity(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a candidate as an entity identity decision, with a reason."""
        from src.kb.fisheries_identity import FisheriesIdentity
        return gated(namespace, lambda conn: FisheriesIdentity(conn).review(
            namespace, match_id, decision, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:fisheries:review")

    @mcp.tool()
    def revert_fisheries_identity(namespace: str, match_id: str, reason: str) -> dict:
        """Undo an accepted or rejected match (IMO matches included); both records stay intact."""
        from src.kb.fisheries_identity import FisheriesIdentity
        return gated(namespace, lambda conn: FisheriesIdentity(conn).revert(
            namespace, match_id, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:fisheries:review")

    @mcp.tool()
    def list_fisheries_identity_matches(namespace: str, state: str | None = None,
                                        subject_key: str | None = None) -> dict:
        """Identity matches and candidates with basis, evidence and review state."""
        from src.kb.fisheries_identity import FisheriesIdentity
        return gated(namespace, lambda conn: {"matches": FisheriesIdentity(conn, initialize=False).matches(
            namespace, scopes=who()[1], state=state, subject_key=subject_key)})

    @mcp.tool()
    def link_fisheries_citations(namespace: str, targets: list[str] | None = None) -> dict:
        """Link by explicit citation: sanctions (stated IMO), areas (published codes), OSINT movements, Agri-food."""
        from src.kb.fisheries_identity import FisheriesLinks
        from src.kb.fisheries_records import FisheriesError

        def run(conn):
            links = FisheriesLinks(conn)
            chosen = targets or ["sanctions", "areas", "osint.vessel-movements", "agrifood.food-systems"]
            result = {}
            for target in chosen:
                try:
                    if target == "sanctions":
                        result[target] = links.link_sanctions(namespace, scopes=who()[1], principal_id=who()[0])
                    elif target == "areas":
                        result[target] = links.link_areas(namespace, scopes=who()[1], principal_id=who()[0])
                    else:
                        result[target] = links.link_cited(namespace, target, scopes=who()[1], principal_id=who()[0])
                except FisheriesError as exc:
                    if exc.code != "unauthorized":
                        raise
                    result[target] = {"status": "not_authorized", "reason": exc.message, "linked": []}
            return result
        return gated(namespace, run, write=True, scope="knowledge:fisheries:write")

    @mcp.tool()
    def project_fisheries_areas(namespace: str) -> dict:
        """Register published area codes and grid cells as geospatial places keyed by exactly those codes."""
        from src.kb.fisheries_identity import FisheriesLinks
        return gated(namespace, lambda conn: FisheriesLinks(conn).project_areas(
            namespace, scopes=who()[1], principal_id=who()[0]), write=True, scope="knowledge:fisheries:write",
            also=("knowledge:geospatial:write",))

    @mcp.tool()
    def list_fisheries_links(namespace: str, subject_key: str, owner: str | None = None) -> dict:
        """Citation links (sanctions, geospatial, citing providers) of one vessel or area and its matched records."""
        from src.kb.fisheries_identity import FisheriesIdentity, FisheriesLinks

        def run(conn):
            members = FisheriesIdentity(conn, initialize=False).members(namespace, subject_key)
            return {"members": members, "links": FisheriesLinks(conn, initialize=False).links(
                namespace, members, scopes=who()[1], owner=owner)}
        return gated(namespace, run)

    @mcp.tool()
    def create_fisheries_monitor(namespace: str, request_key: str, vessels: list[str] | None = None,
                                 lists: list[str] | None = None, areas: list[str] | None = None,
                                 species: list[str] | None = None, delivery: dict | None = None) -> dict:
        """Watch vessels, lists, areas or species for authorisations, listings, delistings and new releases."""
        from src.kb.fisheries_monitoring import FisheriesMonitor
        return gated(namespace, lambda conn: FisheriesMonitor(conn).create(
            namespace, request_key, principal_id=who()[0], scopes=who()[1], vessels=vessels, lists=lists,
            areas=areas, species=species, delivery=delivery), write=True, scope="knowledge:fisheries:read",
            also=("knowledge:subscriptions:write",))

    @mcp.tool()
    def run_fisheries_monitor(namespace: str, subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a monitor at a committed complete run (latest when omitted); replay creates no new events."""
        from src.kb.fisheries_monitoring import FisheriesMonitor
        return gated(namespace, lambda conn: FisheriesMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:fisheries:read", also=("knowledge:subscriptions:write",))

    @mcp.tool()
    def poll_fisheries_monitor(namespace: str, subscription_id: str, cursor: str = "") -> dict:
        """Poll delivered monitor events after a cursor."""
        from src.kb.fisheries_monitoring import FisheriesMonitor
        return gated(namespace, lambda conn: FisheriesMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor),
            also=("knowledge:subscriptions:read",))
