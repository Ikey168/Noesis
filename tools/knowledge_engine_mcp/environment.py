"""Climate and Environment entry points: acquisition, place dossiers, vintages, identity, obligations, monitoring.

Every tool except the provider contracts checks the bundle's enablement first;
Geospatial, LEI, source runtime and subscriptions are never gated by it. No
tool infers attributions, projections or compliance, and forecasts or model
output are never returned as observations.
"""

from src.kb.environment_bundle import BUNDLE, readiness, require_enabled, set_enabled

ENVIRONMENT_WRITES = {
    "set_climate_environment_bundle_enabled", "acquire_environment_source", "register_environment_schemas",
    "build_environment_place_dossier", "propose_environment_operator_links", "review_environment_operator_link",
    "attach_environment_obligation_evidence", "create_environment_monitor", "run_environment_monitor",
}
ENVIRONMENT_TOOLS = ENVIRONMENT_WRITES | {
    "climate_environment_bundle_status", "environment_provider_contracts", "lookup_environment_series",
    "list_environment_grid_events", "list_environment_facilities", "compare_environment_vintages",
    "inspect_environment_dossier", "replay_environment_dossier", "export_environment_dossier",
    "list_environment_operator_links", "inspect_environment_obligation_evidence", "poll_environment_monitor",
}
ENVIRONMENT_SCOPES = {
    "set_climate_environment_bundle_enabled": ["operator"],
    "acquire_environment_source": ["knowledge:environment:write", "knowledge:ingestion:execute"],
    "register_environment_schemas": ["knowledge:environment:write", "knowledge:schema:register"],
    "review_environment_operator_link": ["knowledge:environment:review"],
    "create_environment_monitor": ["knowledge:environment:write", "knowledge:subscriptions:write"],
    "run_environment_monitor": ["knowledge:environment:write", "knowledge:subscriptions:write"],
    "poll_environment_monitor": ["knowledge:environment:read", "knowledge:subscriptions:read"],
}


def required_scopes(tool_name, mutability):
    if tool_name == "environment_provider_contracts":
        return []
    return ENVIRONMENT_SCOPES.get(
        tool_name, ["knowledge:environment:write" if mutability == "write" else "knowledge:environment:read"])


def _also(scopes, required):
    missing = [scope for scope in required if scope not in scopes]
    if missing and "operator" not in scopes:
        from src.kb.environment_store import EnvironmentStoreError

        raise EnvironmentStoreError("unauthorized", f"{missing[0]} scope is required")


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def gated(namespace, operation, *, write=False, scope="knowledge:environment:read", also=()):
        def run(conn):
            require_enabled(conn, namespace)
            _also(who()[1], also)
            return operation(conn)
        return safe(run, write=write, required_scope=scope)

    @mcp.tool()
    def climate_environment_bundle_status(namespace: str) -> dict:
        """Declared contributions plus ready/fixture-only/stale/unavailable per provider; live verification kept separate."""
        return safe(lambda conn: {**readiness(conn, namespace, scopes=who()[1]), "declaration": BUNDLE},
                    required_scope="knowledge:environment:read")

    @mcp.tool()
    def set_climate_environment_bundle_enabled(namespace: str, enabled: bool) -> dict:
        """Enable/disable Climate and Environment (a coordinator selection change once composed); Geospatial keeps working."""
        return safe(lambda conn: set_enabled(conn, namespace, enabled, principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="operator")

    @mcp.tool()
    def environment_provider_contracts() -> dict:
        """Per-provider access contracts (terms, auth, limits, kinds, identifiers, units, CRS) and live-verification state."""
        from src.ingestion.environment_providers import LIVE_VERIFICATION, PROVIDER_CONTRACTS
        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION}

    @mcp.tool()
    def acquire_environment_source(namespace: str, provider: str, selection: dict, observation: str, budget_id: str,
                                   reuse_notice: str, max_requests: int = 20) -> dict:
        """Bounded acquisition of one explicit provider selection through DurableHTTP (credentials from NOESIS_* env)."""
        def run(conn):
            from src.config.env import resolve_env
            from src.ingestion.environment_providers import PROVIDER_HOSTS, SECRETS, EnvironmentClient, acquire
            from src.ingestion.provider_execution import DurableHTTP

            if provider not in PROVIDER_HOSTS:
                raise ValueError("unknown environment provider")
            principal, scopes = who()
            secret = resolve_env(SECRETS[provider][2]) if provider in SECRETS else None
            http = DurableHTTP(conn, budget_id=budget_id, provider=provider, principal_id=principal,
                               allowed_hosts=PROVIDER_HOSTS[provider], reuse_notice=reuse_notice,
                               max_requests=max_requests, account_ref="configured" if secret else "public")
            client = EnvironmentClient(http, principal_id=principal, secret=secret)
            return acquire(client, selection, namespace=namespace, scopes=scopes, reuse_notice=reuse_notice,
                           observation=observation, principal_id=principal)
        return gated(namespace, run, write=True, scope="knowledge:environment:write", also=("knowledge:ingestion:execute",))

    @mcp.tool()
    def register_environment_schemas(namespace: str) -> dict:
        """Register noesis-environment-record-v1 and noesis-environment-dossier-v1 in the schema registry."""
        from src.kb.environment_records import register_schemas
        return gated(namespace, lambda conn: {"modules": register_schemas(conn, principal_id=who()[0], scopes=who()[1])},
                     write=True, scope="knowledge:environment:write", also=("knowledge:schema:register",))

    @mcp.tool()
    def lookup_environment_series(namespace: str, record_id: str | None = None, provider: str | None = None,
                                  kind: str | None = None, as_of_ms: int | None = None, vintage_id: str | None = None,
                                  unit: str | None = None) -> dict:
        """One series (values from the vintage valid at as_of, optional pint conversion) or the series list; kind always shown."""
        from src.kb.environment_store import EnvironmentStore

        def run(conn):
            store = EnvironmentStore(conn, initialize=False)
            if record_id:
                return store.series(namespace, record_id, scopes=who()[1], as_of_ms=as_of_ms, vintage_id=vintage_id,
                                    unit=unit)
            return {"series": [{"record_id": r["record_id"], "title": r["content"]["title"], "provider": r["provider"],
                                "kind": r["kind"], "indicator": r["content"].get("indicator"), "unit": r["content"].get("unit")}
                               for r in store.records(namespace, scopes=who()[1], record_type="observation_series",
                                                      provider=provider, kind=kind)]}
        return gated(namespace, run)

    @mcp.tool()
    def list_environment_grid_events(namespace: str, event_type: str | None = None, as_of_ms: int | None = None) -> dict:
        """Grid generation/load/unavailability records as published, with zone, resolution and kind (actual vs forecast)."""
        from src.kb.environment_store import EnvironmentStore

        def run(conn):
            store = EnvironmentStore(conn, initialize=False)
            events = []
            for record in store.records(namespace, scopes=who()[1], record_type="grid_event"):
                content = store.record(namespace, record["record_id"], scopes=who()[1], as_of_ms=as_of_ms)["content"] \
                    if as_of_ms else record["content"]
                if event_type and content["event_type"] != event_type:
                    continue
                events.append({"record_id": record["record_id"], "title": content["title"], "provider": content["provider"],
                               "event_type": content["event_type"], "kind": content["kind"], "zone": content["bidding_zone"],
                               "resolution": content.get("resolution"), "unavailability": content.get("unavailability"),
                               "document": content.get("document"), "source_url": content["source_url"]})
            return {"events": events, "notice": "no outage is inferred from news or load data"}
        return gated(namespace, run)

    @mcp.tool()
    def list_environment_facilities(namespace: str, provider: str | None = None) -> dict:
        """Facilities/installations with operator as published, identity-link state, permits and links."""
        from src.kb.environment_identity import operator_view
        from src.kb.environment_store import EnvironmentStore

        def run(conn):
            store = EnvironmentStore(conn, initialize=False)
            return {"facilities": [{"record_id": r["record_id"], "title": r["content"]["title"], "provider": r["provider"],
                                    "operator": operator_view(conn, namespace, r["record_id"], r["content"]["operator"]),
                                    "permits": r["content"].get("permits"), "identifiers": r["content"].get("identifiers"),
                                    "links": store.links(namespace, scopes=who()[1], record=r["record_id"]),
                                    "revision_id": r["revision_id"]}
                                   for r in store.records(namespace, scopes=who()[1], record_type="facility",
                                                          provider=provider)],
                    "notice": "reported data as published; no compliance determination"}
        return gated(namespace, run)

    @mcp.tool()
    def compare_environment_vintages(namespace: str, record_id: str, left_vintage_id: str | None = None,
                                     right_vintage_id: str | None = None) -> dict:
        """Changed values between two vintages with both cited and provisional/validated status per value."""
        from src.kb.environment_vintages import compare
        return gated(namespace, lambda conn: compare(conn, namespace, record_id, scopes=who()[1], left=left_vintage_id,
                                                     right=right_vintage_id))

    @mcp.tool()
    def build_environment_place_dossier(namespace: str, request_key: str, place_id: str | None = None,
                                        mention: str | None = None, as_of_ms: int | None = None,
                                        radius_m: float = 5000) -> dict:
        """Cited dossier for one place: stations within its boundary, grid events for its zone, nearby facilities, layers, climate series."""
        from src.kb.environment_places import EnvironmentDossiers
        return gated(namespace, lambda conn: EnvironmentDossiers(conn).build(
            namespace, request_key, principal_id=who()[0], scopes=who()[1], place_id=place_id, mention=mention,
            as_of_ms=as_of_ms, radius_m=radius_m), write=True, scope="knowledge:environment:write")

    @mcp.tool()
    def inspect_environment_dossier(namespace: str, dossier_id: str) -> dict:
        """A stored dossier with its pins and whether newer vintages/revisions make it stale."""
        from src.kb.environment_places import EnvironmentDossiers
        return gated(namespace, lambda conn: EnvironmentDossiers(conn, initialize=False).inspect(
            namespace, dossier_id, scopes=who()[1], principal_id=who()[0]))

    @mcp.tool()
    def replay_environment_dossier(namespace: str, dossier_id: str) -> dict:
        """Recompute every spatial receipt from pinned geometry and verify the dossier hash."""
        from src.kb.environment_places import EnvironmentDossiers
        return gated(namespace, lambda conn: EnvironmentDossiers(conn, initialize=False).replay(
            namespace, dossier_id, scopes=who()[1], principal_id=who()[0]))

    @mcp.tool()
    def export_environment_dossier(namespace: str, dossier_id: str) -> dict:
        """Cited Markdown plus the structured dossier; source, kind and as-of visible for every item."""
        from src.kb.environment_places import EnvironmentDossiers
        return gated(namespace, lambda conn: EnvironmentDossiers(conn, initialize=False).export(
            namespace, dossier_id, scopes=who()[1], principal_id=who()[0]))

    @mcp.tool()
    def propose_environment_operator_links(namespace: str) -> dict:
        """Candidate identity decisions from facility operators to canonical entities and LEI records (none accepted)."""
        from src.kb.environment_identity import EnvironmentIdentity
        return gated(namespace, lambda conn: EnvironmentIdentity(conn).propose_operator_links(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True, scope="knowledge:environment:write")

    @mcp.tool()
    def review_environment_operator_link(namespace: str, link_id: str, decision: str, reason: str) -> dict:
        """Accept or reject another principal's operator identity candidate, with a reason."""
        from src.kb.environment_identity import EnvironmentIdentity
        return gated(namespace, lambda conn: EnvironmentIdentity(conn).review(
            namespace, link_id, decision, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:environment:review")

    @mcp.tool()
    def list_environment_operator_links(namespace: str, facility_record: str | None = None) -> dict:
        """Operator identity decisions (candidate/accepted/rejected) with their basis."""
        from src.kb.environment_identity import EnvironmentIdentity
        return gated(namespace, lambda conn: {"links": EnvironmentIdentity(conn, initialize=False).links(
            namespace, scopes=who()[1], facility_record=facility_record)})

    @mcp.tool()
    def attach_environment_obligation_evidence(namespace: str, subject_id: str, assertion_key: str, facility_record: str,
                                               series_record: str, period_start: str) -> dict:
        """Show a facility release (pinned vintage) beside a policy-monitor obligation; no determination is produced."""
        from src.kb.environment_identity import EnvironmentIdentity
        return gated(namespace, lambda conn: EnvironmentIdentity(conn).attach_evidence(
            namespace, subject_id=subject_id, assertion_key=assertion_key, facility_record=facility_record,
            series_record=series_record, period_start=period_start, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:environment:write")

    @mcp.tool()
    def inspect_environment_obligation_evidence(namespace: str, evidence_id: str) -> dict:
        """Obligation, evidence, sources and staleness; determination is always null."""
        from src.kb.environment_identity import EnvironmentIdentity
        return gated(namespace, lambda conn: EnvironmentIdentity(conn, initialize=False).evidence(
            namespace, evidence_id, scopes=who()[1]))

    @mcp.tool()
    def create_environment_monitor(namespace: str, request_key: str, place_id: str, thresholds: list[dict] | None = None,
                                   watch: list[str] | None = None, radius_m: float = 5000,
                                   delivery: dict | None = None) -> dict:
        """Watch a place: user thresholds, unavailability, permits/releases and new vintages via a knowledge subscription."""
        from src.kb.environment_monitoring import WATCHES, EnvironmentMonitor
        return gated(namespace, lambda conn: EnvironmentMonitor(conn).create(
            namespace, request_key, place_id=place_id, principal_id=who()[0], scopes=who()[1],
            thresholds=thresholds or [], watch=watch or list(WATCHES), radius_m=radius_m, delivery=delivery),
            write=True, scope="knowledge:environment:write", also=("knowledge:subscriptions:write",))

    @mcp.tool()
    def run_environment_monitor(namespace: str, subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a monitor at a committed watermark (latest when omitted); replay creates no new events."""
        from src.kb.environment_monitoring import EnvironmentMonitor
        return gated(namespace, lambda conn: EnvironmentMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:environment:write", also=("knowledge:subscriptions:write",))

    @mcp.tool()
    def poll_environment_monitor(namespace: str, subscription_id: str, cursor: str = "") -> dict:
        """Poll monitor events after a cursor."""
        from src.kb.environment_monitoring import EnvironmentMonitor
        return gated(namespace, lambda conn: EnvironmentMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor),
            also=("knowledge:subscriptions:read",))
