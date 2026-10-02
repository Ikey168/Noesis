"""Society bundle ``society.income`` entry points (IP11, #2637): income, poverty and inequality series.

Acquisition through the ``society-income-distribution`` source pack (World Bank PIP, Eurostat EU-SILC, OECD IDD),
an indicator for a place as of a release, a series' history with comparability notes, reviewable identity, citation
links to Demographics and Labour, monitors and a cited evidence-bundle export. Every tool except the source
contracts checks the bundle's enablement first; Geospatial, Demographics, Labour and platform providers are never
gated by it.

Declared exclusions (returned by every answering tool): no nowcasting, no filled years, no poverty lines of our own,
no blending of PIP, EU-SILC and OECD figures, no re-harmonisation of welfare concepts, no derived indicators and no
person- or household-level data (minimisation decision IP01).
"""

from src.kb.society_bundle import BUNDLE, readiness, require_enabled, set_enabled

INCOME_WRITES = {
    "set_society_bundle_enabled", "acquire_income_source", "register_income_schemas", "propose_income_place_matches",
    "propose_related_income_indicators", "review_income_identity", "revert_income_identity", "link_income_series",
    "record_income_comparability_note", "review_income_comparability_note", "create_income_monitor",
    "run_income_monitor",
}
INCOME_TOOLS = INCOME_WRITES | {
    "society_bundle_status", "income_source_contracts", "list_income_series", "income_indicator_for_place",
    "income_series_history", "income_profile", "export_income_profile", "list_income_identity",
    "list_income_links", "poll_income_monitor",
}
READ = "knowledge:income:read"
WRITE = "knowledge:income:write"
REVIEW = "knowledge:income:review"
INCOME_SCOPES = {
    "set_society_bundle_enabled": ["operator"],
    "acquire_income_source": [WRITE, "knowledge:ingestion:execute"],
    "register_income_schemas": [WRITE, "knowledge:schema:register"],
    "review_income_identity": [REVIEW],
    "revert_income_identity": [REVIEW],
    "review_income_comparability_note": [REVIEW],
    "create_income_monitor": [WRITE, "knowledge:subscriptions:write"],
    "run_income_monitor": [WRITE, "knowledge:subscriptions:write"],
    "poll_income_monitor": [READ, "knowledge:subscriptions:read"],
}
EXCLUSIONS = BUNDLE["exclusions"]


def required_scopes(tool_name, mutability):
    if tool_name == "income_source_contracts":
        return []
    return INCOME_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _also(scopes, required):
    missing = [scope for scope in required if scope not in scopes]
    if missing and "operator" not in scopes:
        from src.kb.income_distribution_records import IncomeError

        raise IncomeError("unauthorized", f"{missing[0]} scope is required")


def _answer(value):
    """Every answer declares the exclusions and the minimisation decision (an evidence bundle carries them in its
    root receipt, so its sealed contract is left untouched)."""
    if isinstance(value, dict) and value.get("contract") != "noesis-evidence-bundle-v1":
        value.setdefault("exclusions", EXCLUSIONS)
        value.setdefault("minimisation", BUNDLE["contributions"]["minimisation"]["decision"])
    return value


def register(mcp, safe, context):
    def who():
        return context()[0], set(context()[1])

    def gated(namespace, operation, *, write=False, scope=READ, also=()):
        def run(conn):
            require_enabled(conn, namespace)
            _also(who()[1], also)
            return _answer(operation(conn))
        return safe(run, write=write, required_scope=scope)

    @mcp.tool()
    def society_bundle_status(namespace: str) -> dict:
        """Declared contributions plus ready/fixture-only/stale/unavailable/feature-disabled per source; live separate."""
        return safe(lambda conn: {**readiness(conn, namespace, scopes=who()[1]), "declaration": BUNDLE},
                    required_scope=READ)

    @mcp.tool()
    def set_society_bundle_enabled(namespace: str, enabled: bool) -> dict:
        """Enable/disable Society (a coordinator selection change once composed); shared providers keep working."""
        return safe(lambda conn: set_enabled(conn, namespace, enabled, principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="operator")

    @mcp.tool()
    def income_source_contracts() -> dict:
        """Per-source contract, licence, limits, revision model, bounded coverage, live state and minimisation."""
        from src.ingestion.income_distribution_sources import (
            BOUNDED_COVERAGE,
            CAPS,
            LIVE_VERIFICATION,
            PROVIDER_CONTRACTS,
        )
        from src.kb.income_distribution_records import MINIMISATION
        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "bounded_coverage": BOUNDED_COVERAGE, "caps": CAPS, "minimisation": MINIMISATION,
                "audit": "docs/development/income-distribution-evidence/source-audit.md", "exclusions": EXCLUSIONS}

    @mcp.tool()
    def acquire_income_source(namespace: str, source_id: str, run_key: str, operation: str = "release") -> dict:
        """Run one declared source of the society-income-distribution pack (bounded, receipted, no credentials)."""
        def run(conn):
            from src.ingestion.source_pack_runtime import SourcePackRuntime

            return SourcePackRuntime(conn).run(
                {"pack_id": "society-income-distribution", "run_key": run_key, "operation": operation,
                 "source_ids": [source_id]}, principal_id=who()[0])
        return gated(namespace, run, write=True, scope=WRITE, also=("knowledge:ingestion:execute",))

    @mcp.tool()
    def register_income_schemas(namespace: str) -> dict:
        """Register noesis-income-distribution-record-v2 in the schema registry."""
        from src.kb.income_distribution_records import register_schemas
        return gated(namespace, lambda conn: {"modules": register_schemas(conn, principal_id=who()[0],
                                                                          scopes=who()[1])},
                     write=True, scope=WRITE, also=("knowledge:schema:register",))

    @mcp.tool()
    def list_income_series(namespace: str, provider: str | None = None, concept: str | None = None) -> dict:
        """Series with their keys (source, welfare concept, equivalence scale, line, PPP round, place, methodology)."""
        from src.kb.income_distribution_store import IncomeDistributionStore

        def run(conn):
            from src.kb.income_distribution_records import authorize
            authorize(namespace, who()[1], READ)
            return {"series": IncomeDistributionStore(conn, initialize=False).find_series(
                namespace, provider=provider, concept=concept)}
        return gated(namespace, run)

    @mcp.tool()
    def income_indicator_for_place(namespace: str, concept: str, as_of: str | None = None,
                                   place_id: str | None = None, area: dict | None = None,
                                   reference_year: str | None = None) -> dict:
        """Each source's figure for a place as released by a date, side by side with definitions; never combined."""
        from src.kb.income_distribution_queries import IncomeQueries
        from src.kb.society_bundle import enabled_providers
        return gated(namespace, lambda conn: IncomeQueries(conn).indicator_for_place(
            namespace, scopes=who()[1], concept=concept, as_of=as_of, place_id=place_id, area=area,
            reference_year=reference_year, enabled_providers=enabled_providers(conn)))

    @mcp.tool()
    def income_series_history(namespace: str, series_id: str) -> dict:
        """Every vintage of a series with changed periods, PPP revisions, breaks and comparability_unknown pairs."""
        from src.kb.income_distribution_queries import IncomeQueries
        return gated(namespace, lambda conn: IncomeQueries(conn).series_history(namespace, series_id,
                                                                                scopes=who()[1]))

    @mcp.tool()
    def income_profile(namespace: str, as_of: str | None = None, place_id: str | None = None,
                       area: dict | None = None) -> dict:
        """A place's poverty headcounts and Gini from each source side by side as of a date, with links."""
        from src.kb.society_bundle import income_profile as profile
        return gated(namespace, lambda conn: profile(conn, namespace, scopes=who()[1], as_of=as_of,
                                                     place_id=place_id, area=area))

    @mcp.tool()
    def export_income_profile(namespace: str, as_of: str | None = None, place_id: str | None = None,
                              area: dict | None = None) -> dict:
        """The profile as a noesis-evidence-bundle-v1: every figure cited with source, revision and as-of time."""
        from src.kb.society_bundle import export_profile
        from src.kb.society_bundle import income_profile as profile
        return gated(namespace, lambda conn: export_profile(profile(conn, namespace, scopes=who()[1], as_of=as_of,
                                                                    place_id=place_id, area=area)))

    @mcp.tool()
    def propose_income_place_matches(namespace: str, geo_namespace: str = "geo") -> dict:
        """Propose place matches by published identifier (ISO, NUTS, World Bank region); none accepted."""
        from src.kb.income_distribution_identity import IncomeIdentity
        return gated(namespace, lambda conn: IncomeIdentity(conn).propose_places(
            namespace, geo_namespace=geo_namespace, principal_id=who()[0], scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def propose_related_income_indicators(namespace: str) -> dict:
        """Propose the same concept for the same accepted place across sources as related (never merged)."""
        from src.kb.income_distribution_identity import IncomeIdentity
        return gated(namespace, lambda conn: IncomeIdentity(conn).propose_related(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def review_income_identity(namespace: str, assertion_id: str, decision: str, reason: str,
                               place_id: str | None = None) -> dict:
        """Accept or reject another principal's assertion with a reason (an entity identity decision)."""
        from src.kb.income_distribution_identity import IncomeIdentity
        return gated(namespace, lambda conn: IncomeIdentity(conn).review(
            namespace, assertion_id, decision, reason, principal_id=who()[0], scopes=who()[1], place_id=place_id),
            write=True, scope=REVIEW)

    @mcp.tool()
    def revert_income_identity(namespace: str, assertion_id: str, reason: str) -> dict:
        """Revert an accepted or rejected assertion; the series stay as published."""
        from src.kb.income_distribution_identity import IncomeIdentity
        return gated(namespace, lambda conn: IncomeIdentity(conn).revert(
            namespace, assertion_id, reason, principal_id=who()[0], scopes=who()[1]), write=True, scope=REVIEW)

    @mcp.tool()
    def list_income_identity(namespace: str, kind: str | None = None, state: str | None = None) -> dict:
        """Assertions with their review state, and every area still unmatched."""
        from src.kb.income_distribution_identity import IncomeIdentity

        def run(conn):
            identity = IncomeIdentity(conn, initialize=False)
            return {"assertions": identity.assertions(namespace, scopes=who()[1], kind=kind, state=state),
                    "unmatched": identity.unmatched(namespace, scopes=who()[1])}
        return gated(namespace, run)

    @mcp.tool()
    def link_income_series(namespace: str, demographic_namespace: str = "global",
                           labour_namespace: str = "global") -> dict:
        """Link series to methodology documents, Demographics denominators and Labour series; absences reported."""
        from src.kb.income_distribution_links import IncomeLinks

        def run(conn):
            links = IncomeLinks(conn)
            return {"methodology": links.link_methodology(namespace, principal_id=who()[0], scopes=who()[1]),
                    "denominators": links.link_demographics(namespace, principal_id=who()[0], scopes=who()[1],
                                                            demographic_namespace=demographic_namespace),
                    "labour": links.link_labour(namespace, principal_id=who()[0], scopes=who()[1],
                                                labour_namespace=labour_namespace)}
        return gated(namespace, run, write=True, scope=WRITE)

    @mcp.tool()
    def list_income_links(namespace: str, series_id: str | None = None, kind: str | None = None) -> dict:
        """Links with their basis and pinned revisions; provider_absent and target_not_held reported, never dropped."""
        from src.kb.income_distribution_links import IncomeLinks
        return gated(namespace, lambda conn: {"links": IncomeLinks(conn, initialize=False).links(
            namespace, scopes=who()[1], series_id=series_id, kind=kind)})

    @mcp.tool()
    def record_income_comparability_note(namespace: str, series_id: str, relation: str, statement: str,
                                         cited: list[dict], other_series_id: str | None = None,
                                         periods: list[str] | None = None) -> dict:
        """Propose a cited comparability note for a series or a pair; nothing is re-harmonised."""
        from src.kb.income_distribution_store import IncomeDistributionStore
        return gated(namespace, lambda conn: IncomeDistributionStore(conn).record_note(
            namespace, series_id, relation, statement, other_series_id=other_series_id, periods=periods or [],
            cited=cited, principal_id=who()[0], scopes=who()[1]), write=True, scope=WRITE)

    @mcp.tool()
    def review_income_comparability_note(namespace: str, note_id: str, decision: str, reason: str) -> dict:
        """Accept, reject or revert a recorded comparability note (another principal reviews)."""
        from src.kb.income_distribution_store import IncomeDistributionStore
        return gated(namespace, lambda conn: IncomeDistributionStore(conn).review_note(
            namespace, note_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True, scope=REVIEW)

    @mcp.tool()
    def create_income_monitor(namespace: str, request_key: str, series_id: str | None = None,
                              place_id: str | None = None, area: dict | None = None, concept: str | None = None,
                              delivery: dict | None = None) -> dict:
        """Subscribe to a series, a concept or a place: new releases, PPP revisions, definition changes, removals."""
        from src.kb.income_distribution_monitoring import IncomeMonitor
        target = {k: v for k, v in {"series_id": series_id, "place_id": place_id, "area": area,
                                    "concept": concept}.items() if v is not None}
        return gated(namespace, lambda conn: IncomeMonitor(conn).create(
            namespace, request_key, target=target, delivery=delivery, principal_id=who()[0], scopes=who()[1]),
            write=True, scope=WRITE, also=("knowledge:subscriptions:write",))

    @mcp.tool()
    def run_income_monitor(namespace: str, subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a monitor at a committed watermark (newest by default); a replay creates no new notices."""
        from src.kb.income_distribution_monitoring import IncomeMonitor
        return gated(namespace, lambda conn: IncomeMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]),
            write=True, scope=WRITE, also=("knowledge:subscriptions:write",))

    @mcp.tool()
    def poll_income_monitor(namespace: str, subscription_id: str, cursor: str = "") -> dict:
        """Poll monitor events after a cursor."""
        from src.kb.income_distribution_monitoring import IncomeMonitor
        return gated(namespace, lambda conn: IncomeMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor),
            also=("knowledge:subscriptions:read",))
