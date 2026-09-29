"""Agriculture and Food Systems entry points: acquisition, series as of a date, crosswalks, citations, monitors (#2213).

Every tool except the source contracts checks the bundle's enablement first;
Economics, Climate & Environment, Products, Geospatial, subscriptions and the
source runtime are never gated by it. No tool forecasts, projects or scores; a
commodity and place without series is reported as having none on record.
"""

from src.kb.agrifood_bundle import BUNDLE, readiness, require_enabled, set_enabled

AGRIFOOD_WRITES = {
    "set_agrifood_bundle_enabled", "acquire_agrifood_sources", "publish_agrifood_codelists",
    "register_agrifood_places", "propose_agrifood_crosswalks", "propose_agrifood_crosswalk_manual",
    "review_agrifood_crosswalk", "revert_agrifood_crosswalk", "link_agrifood_citations", "create_agrifood_monitor",
    "run_agrifood_monitor",
}
AGRIFOOD_READS = {
    "agrifood_bundle_status", "agrifood_source_contracts", "agrifood_series_as_of", "agrifood_revision_history",
    "resolve_agrifood_commodity", "resolve_agrifood_place", "list_agrifood_crosswalks",
    "list_unmapped_agrifood_codes", "list_agrifood_links", "poll_agrifood_monitor",
}
AGRIFOOD_TOOLS = AGRIFOOD_WRITES | AGRIFOOD_READS
AGRIFOOD_SCOPES = {
    "set_agrifood_bundle_enabled": ["operator"],
    "acquire_agrifood_sources": ["knowledge:agrifood:write", "knowledge:ingestion:execute"],
    "publish_agrifood_codelists": ["knowledge:agrifood:write", "knowledge:schema:register"],
    "register_agrifood_places": ["knowledge:agrifood:write", "knowledge:geospatial:write"],
    "propose_agrifood_crosswalk_manual": ["knowledge:agrifood:review"],
    "review_agrifood_crosswalk": ["knowledge:agrifood:review"],
    "revert_agrifood_crosswalk": ["knowledge:agrifood:review"],
    "link_agrifood_citations": ["knowledge:agrifood:write", "knowledge:products:read"],
    "create_agrifood_monitor": ["knowledge:agrifood:read", "knowledge:subscriptions:write"],
    "run_agrifood_monitor": ["knowledge:agrifood:read", "knowledge:subscriptions:write"],
    "poll_agrifood_monitor": ["knowledge:agrifood:read", "knowledge:subscriptions:read"],
}


def required_scopes(tool_name, mutability):
    if tool_name == "agrifood_source_contracts":
        return []
    return AGRIFOOD_SCOPES.get(
        tool_name, ["knowledge:agrifood:write" if mutability == "write" else "knowledge:agrifood:read"])


def _also(scopes, required):
    missing = [scope for scope in required if scope not in scopes]
    if missing and "operator" not in scopes:
        from src.kb.agrifood_records import AgrifoodError

        raise AgrifoodError("unauthorized", f"{missing[0]} scope is required")


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def gated(namespace, operation, *, write=False, scope="knowledge:agrifood:read", also=()):
        def run(conn):
            require_enabled(conn, namespace)
            _also(who()[1], also)
            return operation(conn)
        return safe(run, write=write, required_scope=scope)

    @mcp.tool()
    def agrifood_bundle_status(namespace: str) -> dict:
        """Declared contributions plus ready/fixture-only/unavailable per provider; live verification kept separate."""
        return safe(lambda conn: {**readiness(conn, namespace, scopes=who()[1]), "declaration": BUNDLE},
                    required_scope="knowledge:agrifood:read")

    @mcp.tool()
    def set_agrifood_bundle_enabled(namespace: str, enabled: bool) -> dict:
        """Enable/disable Agriculture and Food Systems (a coordinator selection change once composed)."""
        return safe(lambda conn: set_enabled(conn, namespace, enabled, principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="operator")

    @mcp.tool()
    def agrifood_source_contracts() -> dict:
        """Per-provider access contracts, flag vocabularies, revision behaviour and exclusions."""
        from src.ingestion.agrifood_sources import EXCLUSIONS, LIVE_VERIFICATION, PROVIDER_CONTRACTS
        from src.kb.agrifood_records import FLAG_VOCABULARIES
        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "flag_vocabularies": {k: {c: {"label": v[0], "classes": list(v[1])} for c, v in codes.items()}
                                      for k, codes in FLAG_VOCABULARIES.items()},
                "exclusions": list(EXCLUSIONS), "audit": "docs/roadmaps/agrifood-source-audit.md"}

    @mcp.tool()
    def acquire_agrifood_sources(namespace: str, run_key: str, source_ids: list[str] | None = None) -> dict:
        """Bounded, receipted run of the agrifood source pack's explicit selection (secrets from NOESIS_*)."""
        def run(conn):
            from src.config.env import resolve_env
            from src.ingestion.source_pack_runtime import SourcePackRuntime

            request = {"pack_id": "agrifood", "run_key": run_key, "operation": "series",
                       **({"source_ids": list(source_ids)} if source_ids else {})}
            return SourcePackRuntime(conn).run(request, principal_id=who()[0], secret_resolver=resolve_env)
        return gated(namespace, run, write=True, scope="knowledge:agrifood:write",
                     also=("knowledge:ingestion:execute",))

    @mcp.tool()
    def agrifood_series_as_of(namespace: str, commodity: str, place: str, as_of: str | None = None,
                              measures: list[str] | None = None) -> dict:
        """Per-source series for a commodity and place as published at a date, with vintages, flags and citations."""
        from src.kb.agrifood_queries import AgrifoodQueries
        return gated(namespace, lambda conn: AgrifoodQueries(conn).series_as_of(
            namespace, commodity=commodity, place=place, scopes=who()[1], as_of=as_of, measures=measures))

    @mcp.tool()
    def agrifood_revision_history(namespace: str, series_id: str, period: str | None = None) -> dict:
        """Every vintage of a series' figures with release dates and the differences between vintages."""
        from src.kb.agrifood_queries import AgrifoodQueries
        return gated(namespace, lambda conn: AgrifoodQueries(conn).revisions(
            namespace, series_id, scopes=who()[1], period=period))

    @mcp.tool()
    def resolve_agrifood_commodity(namespace: str, query: str) -> dict:
        """A scheme:code or exact published name to acquired codes and those accepted crosswalks reach."""
        from src.kb.agrifood_identity import AgrifoodIdentity
        return gated(namespace, lambda conn: AgrifoodIdentity(conn, initialize=False).resolve_commodity(
            namespace, query, scopes=who()[1]))

    @mcp.tool()
    def resolve_agrifood_place(namespace: str, query: str) -> dict:
        """A place code (FAO area, ISO, FIPS, PSD, Eurostat, member state) or pack place name to its geospatial place."""
        from src.kb.agrifood_identity import AgrifoodIdentity
        return gated(namespace, lambda conn: AgrifoodIdentity(conn, initialize=False).resolve_place(
            namespace, query, scopes=who()[1], save=False))

    @mcp.tool()
    def publish_agrifood_codelists(namespace: str) -> dict:
        """Publish each publisher's acquired commodity codes as an ontology module."""
        from src.kb.agrifood_identity import AgrifoodIdentity
        return gated(namespace, lambda conn: AgrifoodIdentity(conn).publish_codelists(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True, scope="knowledge:agrifood:write",
            also=("knowledge:schema:register",))

    @mcp.tool()
    def register_agrifood_places(namespace: str) -> dict:
        """Register the pack's bounded places as geospatial places carrying every source's code."""
        from src.kb.agrifood_identity import AgrifoodIdentity
        return gated(namespace, lambda conn: AgrifoodIdentity(conn).register_places(
            principal_id=who()[0], scopes=who()[1]), write=True, scope="knowledge:agrifood:write",
            also=("knowledge:geospatial:write",))

    @mcp.tool()
    def propose_agrifood_crosswalks(namespace: str) -> dict:
        """Equivalent candidates between publisher codes whose labels state the same exact name (none accepted)."""
        from src.kb.agrifood_identity import AgrifoodIdentity
        return gated(namespace, lambda conn: AgrifoodIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True, scope="knowledge:agrifood:write")

    @mcp.tool()
    def propose_agrifood_crosswalk_manual(namespace: str, left: str, right: str, kind: str, evidence: str) -> dict:
        """A reviewer's mapping: left is equivalent/broader/narrower than right (scheme:code), with evidence."""
        from src.kb.agrifood_identity import AgrifoodIdentity
        return gated(namespace, lambda conn: AgrifoodIdentity(conn).propose_manual(
            namespace, left, right, kind, evidence, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:agrifood:review")

    @mcp.tool()
    def review_agrifood_crosswalk(namespace: str, crosswalk_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a proposed crosswalk with a reason; series stay separate either way."""
        from src.kb.agrifood_identity import AgrifoodIdentity
        return gated(namespace, lambda conn: AgrifoodIdentity(conn).review(
            namespace, crosswalk_id, decision, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:agrifood:review")

    @mcp.tool()
    def revert_agrifood_crosswalk(namespace: str, crosswalk_id: str, reason: str) -> dict:
        """Undo an accepted or rejected crosswalk decision; its history is kept."""
        from src.kb.agrifood_identity import AgrifoodIdentity
        return gated(namespace, lambda conn: AgrifoodIdentity(conn).revert(
            namespace, crosswalk_id, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:agrifood:review")

    @mcp.tool()
    def list_agrifood_crosswalks(namespace: str, state: str | None = None, code: str | None = None) -> dict:
        """Commodity crosswalks with kind, evidence and review state."""
        from src.kb.agrifood_identity import AgrifoodIdentity
        return gated(namespace, lambda conn: {"crosswalks": AgrifoodIdentity(conn, initialize=False).crosswalks(
            namespace, scopes=who()[1], state=state, code=code)})

    @mcp.tool()
    def list_unmapped_agrifood_codes(namespace: str) -> dict:
        """Commodity codes no accepted mapping connects, partial matches, and place codes no place carries."""
        from src.kb.agrifood_identity import AgrifoodIdentity

        def run(conn):
            identity = AgrifoodIdentity(conn, initialize=False)
            return {"commodities": identity.unmapped(namespace, scopes=who()[1]),
                    "places": identity.unmapped_places(namespace, scopes=who()[1])}
        return gated(namespace, run)

    @mcp.tool()
    def link_agrifood_citations(namespace: str, targets: list[str] | None = None) -> dict:
        """Link RASFF notices, climate/weather records and trade flows by explicit citation only."""
        from src.kb.agrifood_links import AgrifoodLinks

        def run(conn):
            links = AgrifoodLinks(conn)
            chosen = set(targets or ["rasff", "environment", "trade"])
            result = {}
            if "rasff" in chosen:
                result["rasff"] = links.link_rasff(namespace, scopes=who()[1], principal_id=who()[0])
            if "environment" in chosen:
                result["environment"] = links.link_environment(namespace, scopes=who()[1], principal_id=who()[0])
            if "trade" in chosen:
                result["trade"] = links.link_trade_flows(namespace, None, scopes=who()[1], principal_id=who()[0])
            return result
        return gated(namespace, run, write=True, scope="knowledge:agrifood:write", also=("knowledge:products:read",))

    @mcp.tool()
    def list_agrifood_links(namespace: str, owner: str | None = None) -> dict:
        """Citation links (trade, climate, weather, RASFF) with their basis and citing text."""
        from src.kb.agrifood_links import AgrifoodLinks
        return gated(namespace, lambda conn: {"links": AgrifoodLinks(conn, initialize=False).links(
            namespace, scopes=who()[1], owner=owner)})

    @mcp.tool()
    def create_agrifood_monitor(namespace: str, request_key: str, commodity: str, place: str,
                                measures: list[str] | None = None, thresholds: dict | None = None,
                                delivery: dict | None = None) -> dict:
        """Watch a commodity and place for new releases, revisions and linked RASFF alerts (user thresholds only)."""
        from src.kb.agrifood_monitoring import AgrifoodMonitor
        return gated(namespace, lambda conn: AgrifoodMonitor(conn).create(
            namespace, request_key, commodity=commodity, place=place, principal_id=who()[0], scopes=who()[1],
            measures=measures, thresholds=thresholds, delivery=delivery), write=True,
            scope="knowledge:agrifood:read", also=("knowledge:subscriptions:write",))

    @mcp.tool()
    def run_agrifood_monitor(namespace: str, subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a monitor at a committed complete run (latest when omitted); replay creates no new events."""
        from src.kb.agrifood_monitoring import AgrifoodMonitor
        return gated(namespace, lambda conn: AgrifoodMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:agrifood:read", also=("knowledge:subscriptions:write",))

    @mcp.tool()
    def poll_agrifood_monitor(namespace: str, subscription_id: str, cursor: str = "") -> dict:
        """Poll delivered monitor events after a cursor."""
        from src.kb.agrifood_monitoring import AgrifoodMonitor
        return gated(namespace, lambda conn: AgrifoodMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor),
            also=("knowledge:subscriptions:read",))
