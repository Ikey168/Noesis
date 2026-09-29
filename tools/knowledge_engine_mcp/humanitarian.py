"""Humanitarian Response and Conflict Events entry points (HR12, #2283).

Acquisition through the ``humanitarian-response`` source pack, place/crisis
dossiers as of a date, bounded conflict-event queries and event history,
reviewable identity, citation links and monitors. Every tool except the
source contracts checks the bundle's enablement first; geospatial, news,
OSINT, demographics and the platform providers are never gated by it.

Declared exclusions: no casualty estimation, no cross-coder deduplication or
merged counts, no early-warning scores or forecasts, no severity ranking or
operational advice, no personal data. ACLED is not acquired (licence).
"""

from src.kb.humanitarian_bundle import BUNDLE, readiness, require_enabled, set_enabled

HUMANITARIAN_WRITES = {
    "set_humanitarian_bundle_enabled", "acquire_humanitarian_source", "register_humanitarian_schemas",
    "import_humanitarian_admin_boundaries", "propose_humanitarian_identity", "review_humanitarian_identity",
    "revert_humanitarian_identity", "cite_humanitarian_link", "attach_humanitarian_population",
    "create_humanitarian_monitor", "run_humanitarian_monitor",
}
HUMANITARIAN_TOOLS = HUMANITARIAN_WRITES | {
    "humanitarian_bundle_status", "humanitarian_source_contracts", "humanitarian_dossier",
    "export_humanitarian_dossier", "query_humanitarian_conflict_events", "humanitarian_event_history",
    "list_humanitarian_identity", "list_humanitarian_links", "poll_humanitarian_monitor",
}
READ = "knowledge:humanitarian:read"
WRITE = "knowledge:humanitarian:write"
REVIEW = "knowledge:humanitarian:review"
HUMANITARIAN_SCOPES = {
    "set_humanitarian_bundle_enabled": ["operator"],
    "acquire_humanitarian_source": [WRITE, "knowledge:ingestion:execute"],
    "register_humanitarian_schemas": [WRITE, "knowledge:schema:register"],
    "import_humanitarian_admin_boundaries": [WRITE, "knowledge:geospatial:write"],
    "review_humanitarian_identity": [REVIEW],
    "revert_humanitarian_identity": [REVIEW],
    "create_humanitarian_monitor": [WRITE, "knowledge:subscriptions:write"],
    "run_humanitarian_monitor": [WRITE, "knowledge:subscriptions:write"],
    "poll_humanitarian_monitor": [READ, "knowledge:subscriptions:read"],
}
EXCLUSIONS = BUNDLE["exclusions"]


def required_scopes(tool_name, mutability):
    if tool_name == "humanitarian_source_contracts":
        return []
    return HUMANITARIAN_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _also(scopes, required):
    missing = [scope for scope in required if scope not in scopes]
    if missing and "operator" not in scopes:
        from src.kb.humanitarian_records import HumanitarianError

        raise HumanitarianError("unauthorized", f"{missing[0]} scope is required")


def register(mcp, safe, context):
    def who():
        return context()[0], set(context()[1])

    def gated(namespace, operation, *, write=False, scope=READ, also=()):
        def run(conn):
            require_enabled(conn, namespace)
            _also(who()[1], also)
            return operation(conn)
        return safe(run, write=write, required_scope=scope)

    @mcp.tool()
    def humanitarian_bundle_status(namespace: str) -> dict:
        """Declared contributions plus ready/fixture-only/stale/unavailable/declined per source; live verification separate."""
        return safe(lambda conn: {**readiness(conn, namespace, scopes=who()[1]), "declaration": BUNDLE},
                    required_scope=READ)

    @mcp.tool()
    def set_humanitarian_bundle_enabled(namespace: str, enabled: bool) -> dict:
        """Enable/disable Humanitarian (a coordinator selection change once composed); shared providers keep working."""
        return safe(lambda conn: set_enabled(conn, namespace, enabled, principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="operator")

    @mcp.tool()
    def humanitarian_source_contracts() -> dict:
        """Per-source contract, licence, limits, revision model, live-verification state and the ACLED decision."""
        from src.ingestion.humanitarian_sources import ACLED_DECISION, CAPS, LIVE_VERIFICATION, PROVIDER_CONTRACTS
        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION, "acled_decision": ACLED_DECISION,
                "caps": CAPS, "audit": "docs/development/humanitarian-evidence/source-audit.md", "exclusions": EXCLUSIONS}

    @mcp.tool()
    def acquire_humanitarian_source(namespace: str, source_id: str, run_key: str, operation: str) -> dict:
        """Run one declared source of the humanitarian-response pack (bounded, receipted; credentials from NOESIS_* env)."""
        def run(conn):
            from src.config.env import resolve_env
            from src.ingestion.source_pack_runtime import SourcePackRuntime

            return SourcePackRuntime(conn).run(
                {"pack_id": "humanitarian-response", "run_key": run_key, "operation": operation,
                 "source_ids": [source_id]}, principal_id=who()[0], secret_resolver=resolve_env)
        return gated(namespace, run, write=True, scope=WRITE, also=("knowledge:ingestion:execute",))

    @mcp.tool()
    def register_humanitarian_schemas(namespace: str) -> dict:
        """Register noesis-humanitarian-record-v1 in the schema registry."""
        from src.kb.humanitarian_records import register_schemas
        return gated(namespace, lambda conn: {"modules": register_schemas(conn, principal_id=who()[0], scopes=who()[1])},
                     write=True, scope=WRITE, also=("knowledge:schema:register",))

    @mcp.tool()
    def import_humanitarian_admin_boundaries(namespace: str, feature_collection: dict, vintage: str,
                                             source_url: str) -> dict:
        """Register a published COD-AB boundary file as geospatial admin places with its boundary vintage."""
        from src.kb.humanitarian_identity import HumanitarianIdentity
        return gated(namespace, lambda conn: HumanitarianIdentity(conn).import_admin_boundaries(
            namespace, feature_collection, vintage=vintage, source_url=source_url, principal_id=who()[0],
            scopes=who()[1]), write=True, scope=WRITE, also=("knowledge:geospatial:write",))

    @mcp.tool()
    def humanitarian_dossier(namespace: str, as_of: str | None = None, pcode: str | None = None,
                             place_id: str | None = None, crisis_key: str | None = None, events_area: dict | None = None,
                             events_start: str | None = None, events_end: str | None = None,
                             imprecise: str = "admin") -> dict:
        """Cited reports, appeals, crises and dataset revisions about a place or crisis as of a date, plus bounded events."""
        from src.kb.humanitarian_bundle import dossier

        events = {"area": events_area, "start": events_start, "end": events_end, "imprecise": imprecise} \
            if events_area else None
        return gated(namespace, lambda conn: dossier(conn, namespace, as_of=as_of, scopes=who()[1], pcode=pcode,
                                                     place_id=place_id, crisis_key=crisis_key, events=events))

    @mcp.tool()
    def export_humanitarian_dossier(namespace: str, as_of: str | None = None, pcode: str | None = None,
                                    place_id: str | None = None, crisis_key: str | None = None,
                                    events_area: dict | None = None, events_start: str | None = None,
                                    events_end: str | None = None) -> dict:
        """The dossier as a noesis-evidence-bundle-v1: every record revision cited; gaps and declined sources as omissions."""
        from src.kb.humanitarian_bundle import dossier, export_dossier

        events = {"area": events_area, "start": events_start, "end": events_end} if events_area else None
        return gated(namespace, lambda conn: export_dossier(dossier(
            conn, namespace, as_of=as_of, scopes=who()[1], pcode=pcode, place_id=place_id, crisis_key=crisis_key,
            events=events)))

    @mcp.tool()
    def query_humanitarian_conflict_events(namespace: str, area: dict, start: str, end: str, as_of: str | None = None,
                                           imprecise: str = "admin") -> dict:
        """Conflict events in a bounded area/window, each coder side by side with its precision codes and counts as published."""
        from src.kb.humanitarian_queries import HumanitarianQueries
        return gated(namespace, lambda conn: HumanitarianQueries(conn).conflict_events(
            namespace, area=area, start=start, end=end, as_of=as_of, imprecise=imprecise, scopes=who()[1]))

    @mcp.tool()
    def humanitarian_event_history(namespace: str, record_key: str) -> dict:
        """Every release revision of one conflict event with the fields that changed (dropped candidates included)."""
        from src.kb.humanitarian_queries import HumanitarianQueries
        return gated(namespace, lambda conn: HumanitarianQueries(conn).event_history(namespace, record_key,
                                                                                    scopes=who()[1]))

    @mcp.tool()
    def propose_humanitarian_identity(namespace: str) -> dict:
        """Propose place, crisis, organisation and actor match assertions (method, evidence, confidence); none accepted."""
        from src.kb.humanitarian_identity import HumanitarianIdentity
        return gated(namespace, lambda conn: HumanitarianIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def review_humanitarian_identity(namespace: str, assertion_id: str, decision: str, reason: str) -> dict:
        """Accept or reject another principal's assertion with a reason (an entity identity decision)."""
        from src.kb.humanitarian_identity import HumanitarianIdentity
        return gated(namespace, lambda conn: HumanitarianIdentity(conn).review(
            namespace, assertion_id, decision, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, scope=REVIEW)

    @mcp.tool()
    def revert_humanitarian_identity(namespace: str, assertion_id: str, reason: str) -> dict:
        """Revert an accepted or rejected assertion; the records stay as published."""
        from src.kb.humanitarian_identity import HumanitarianIdentity
        return gated(namespace, lambda conn: HumanitarianIdentity(conn).revert(
            namespace, assertion_id, reason, principal_id=who()[0], scopes=who()[1]), write=True, scope=REVIEW)

    @mcp.tool()
    def list_humanitarian_identity(namespace: str, state: str | None = None, subject_key: str | None = None) -> dict:
        """Assertions with their review state, and every subject still unmatched."""
        from src.kb.humanitarian_identity import HumanitarianIdentity

        def run(conn):
            identity = HumanitarianIdentity(conn, initialize=False)
            return {"assertions": identity.assertions(namespace, scopes=who()[1], state=state, subject_key=subject_key),
                    "unmatched": identity.unmatched(namespace, scopes=who()[1])}
        return gated(namespace, run)

    @mcp.tool()
    def cite_humanitarian_link(namespace: str, record_key: str, target_kind: str, target_id: str, citation: dict,
                               target_revision: str | None = None) -> dict:
        """Link a record revision to a news article, OSINT dossier or population series the source cites (with locator)."""
        from src.kb.humanitarian_links import HumanitarianLinks
        return gated(namespace, lambda conn: HumanitarianLinks(conn).cite(
            namespace, record_key, target_kind, target_id, citation=citation, target_revision=target_revision,
            principal_id=who()[0], scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def attach_humanitarian_population(namespace: str, record_key: str, series_id: str, vintage_id: str | None = None,
                                       citation: dict | None = None) -> dict:
        """Attach a population denominator with its vintage by citation or accepted place match; no rate is computed."""
        from src.kb.humanitarian_links import HumanitarianLinks
        return gated(namespace, lambda conn: HumanitarianLinks(conn).attach_population(
            namespace, record_key, series_id, vintage_id=vintage_id, citation=citation, principal_id=who()[0],
            scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def list_humanitarian_links(namespace: str, record_key: str | None = None) -> dict:
        """Citation links with their basis and pinned revisions; broken targets reported, never dropped."""
        from src.kb.humanitarian_links import HumanitarianLinks
        return gated(namespace, lambda conn: HumanitarianLinks(conn, initialize=False).links(
            namespace, scopes=who()[1], record_key=record_key))

    @mcp.tool()
    def create_humanitarian_monitor(namespace: str, request_key: str, pcode: str | None = None,
                                    place_id: str | None = None, crisis_key: str | None = None,
                                    watch: list[str] | None = None, events: dict | None = None,
                                    delivery: dict | None = None) -> dict:
        """Subscribe to a place or crisis: new reports, dataset revisions and event releases (a knowledge subscription)."""
        from src.kb.humanitarian_monitoring import WATCHES, HumanitarianMonitor
        return gated(namespace, lambda conn: HumanitarianMonitor(conn).create(
            namespace, request_key, pcode=pcode, place_id=place_id, crisis_key=crisis_key,
            watch=watch or [w for w in WATCHES if w != "events" or events], events=events, delivery=delivery,
            principal_id=who()[0], scopes=who()[1]), write=True, scope=WRITE, also=("knowledge:subscriptions:write",))

    @mcp.tool()
    def run_humanitarian_monitor(namespace: str, subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a monitor at a committed watermark (newest by default); a replay creates no new notices."""
        from src.kb.humanitarian_monitoring import HumanitarianMonitor
        return gated(namespace, lambda conn: HumanitarianMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]),
            write=True, scope=WRITE, also=("knowledge:subscriptions:write",))

    @mcp.tool()
    def poll_humanitarian_monitor(namespace: str, subscription_id: str, cursor: str = "") -> dict:
        """Poll monitor events after a cursor."""
        from src.kb.humanitarian_monitoring import HumanitarianMonitor
        return gated(namespace, lambda conn: HumanitarianMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor),
            also=("knowledge:subscriptions:read",))
