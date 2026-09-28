"""Materials entry points: lookup, cited property dossier, comparison, range search, identity and releases.

Every tool except the source contracts checks the bundle's enablement; reads
answer ``not_ready`` until a materials source has run. No tool averages,
merges or predicts a value, and a computed value is never presented as a
measurement.
"""

from src.kb.materials_bundle import BUNDLE, readiness, require_enabled

READ = "knowledge:materials:read"
WRITE = "knowledge:materials:write"
MATERIALS_WRITES = {
    "register_material_schemas",
    "propose_material_matches",
    "review_material_match",
    "revert_material_match",
    "create_material_watch",
    "run_material_watch",
}
MATERIALS_TOOLS = MATERIALS_WRITES | {
    "materials_source_contracts",
    "materials_bundle_status",
    "lookup_material",
    "material_properties",
    "compare_material_property",
    "search_materials_by_property",
    "material_release_changes",
    "poll_material_watch",
}
MATERIALS_SCOPES = {
    "materials_source_contracts": [],
    "materials_bundle_status": [READ],
    "register_material_schemas": [WRITE, "knowledge:schema:register"],
    "lookup_material": [READ],
    # standards_namespace additionally needs knowledge:standards:read, checked at call time.
    "material_properties": [READ],
    "compare_material_property": [READ],
    "search_materials_by_property": [READ],
    # Proposing reads material records and writes candidates into the shared reviewable identity state machine.
    "propose_material_matches": [READ, "knowledge:ownership:write"],
    "review_material_match": ["knowledge:ownership:review"],
    "revert_material_match": ["knowledge:ownership:review"],
    "material_release_changes": [READ],
    # A watch reads materials values and writes only subscription state.
    "create_material_watch": [READ, "knowledge:subscriptions:write"],
    "run_material_watch": [READ, "knowledge:subscriptions:write"],
    "poll_material_watch": [READ, "knowledge:subscriptions:read"],
}


def required_scopes(tool_name, mutability):
    return list(
        MATERIALS_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])
    )


def _also(scopes, required):
    missing = [scope for scope in required if scope not in scopes]
    if missing and "operator" not in scopes:
        from src.kb.materials_store import MaterialsError

        raise MaterialsError("unauthorized", f"{missing[0]} scope is required")


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def gated(name, operation, *, write=False):
        declared = MATERIALS_SCOPES[name]

        def run(conn):
            _also(who()[1], declared)
            require_enabled(conn)
            return operation(conn)

        return safe(run, write=write, required_scope=declared[0] if declared else None)

    @mcp.tool()
    def materials_source_contracts() -> dict:
        """Per-source access contracts, licences, bounded coverage and live-verification state (all unverified-live)."""
        from src.ingestion.materials_sources import (
            LIVE_VERIFICATION,
            PROVIDER_CONTRACTS,
        )

        return {
            "contracts": PROVIDER_CONTRACTS,
            "live_verification": LIVE_VERIFICATION,
            "audit": "docs/development/materials-evidence/source-audit.md",
        }

    @mcp.tool()
    def materials_bundle_status(namespace: str) -> dict:
        """Declared contributions plus unavailable/stale/fixture-only/ready per provider; live evidence kept separate."""
        return gated(
            "materials_bundle_status",
            lambda conn: {
                **readiness(conn, namespace, scopes=who()[1]),
                "declaration": BUNDLE,
            },
        )

    @mcp.tool()
    def register_material_schemas(namespace: str) -> dict:
        """Register noesis-material-record-v1 and noesis-material-comparison-v1 in the schema registry."""
        from src.kb.materials_records import register_schemas

        del namespace
        return gated(
            "register_material_schemas",
            lambda conn: {
                "modules": register_schemas(
                    conn, principal_id=who()[0], scopes=who()[1]
                )
            },
            write=True,
        )

    @mcp.tool()
    def lookup_material(
        namespace: str, query: str, as_of_ms: int | None = None
    ) -> dict:
        """Acquired records by formula, record key or source ID, with composition group, phase group and pending matches."""
        from src.kb.materials_queries import MaterialsQueries

        return gated(
            "lookup_material",
            lambda conn: MaterialsQueries(conn).lookup(
                namespace, query, scopes=who()[1], as_of_ms=as_of_ms
            ),
        )

    @mcp.tool()
    def material_properties(
        namespace: str,
        material: str,
        property: str | None = None,
        as_of_ms: int | None = None,
        standards_namespace: str | None = None,
    ) -> dict:
        """Cited property dossier: value, unit, conditions, method, uncertainty, release, locator and citations.

        Conditional scope: standards_namespace also needs knowledge:standards:read and read access to it."""
        from src.kb.materials_queries import MaterialsQueries

        return gated(
            "material_properties",
            lambda conn: MaterialsQueries(conn).properties(
                namespace,
                material,
                scopes=who()[1],
                prop=property,
                as_of_ms=as_of_ms,
                standards_namespace=standards_namespace,
            ),
        )

    @mcp.tool()
    def compare_material_property(
        namespace: str, material: str, property: str, as_of_ms: int | None = None
    ) -> dict:
        """Align only comparable values (property, method class, reviewed phase, conditions); the rest side by side."""
        from src.kb.materials_comparison import MaterialsComparison

        return gated(
            "compare_material_property",
            lambda conn: MaterialsComparison(conn).compare(
                namespace, material, property, scopes=who()[1], as_of_ms=as_of_ms
            ),
        )

    @mcp.tool()
    def search_materials_by_property(
        namespace: str,
        ranges: list[dict],
        conditions: dict | None = None,
        method_class: str | None = None,
        as_of_ms: int | None = None,
        limit: int = 50,
    ) -> dict:
        """Bounded search over acquired records: each hit cites the value(s) meeting every range; semantics stated."""
        from src.kb.materials_comparison import MaterialsComparison

        return gated(
            "search_materials_by_property",
            lambda conn: MaterialsComparison(conn).search(
                namespace,
                ranges,
                scopes=who()[1],
                conditions=conditions,
                method_class=method_class,
                as_of_ms=as_of_ms,
                limit=limit,
            ),
        )

    @mcp.tool()
    def propose_material_matches(namespace: str) -> dict:
        """Phase-level candidates from stated cross-references or equal space group and volume; never polymorphs."""
        from src.kb.materials_identity import MaterialsIdentity

        return gated(
            "propose_material_matches",
            lambda conn: MaterialsIdentity(conn).propose(
                namespace, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
        )

    @mcp.tool()
    def review_material_match(
        namespace: str, candidate_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject a materials identity candidate with a reason (an entity identity decision)."""
        from src.kb.materials_identity import MaterialsIdentity

        return gated(
            "review_material_match",
            lambda conn: MaterialsIdentity(conn).review(
                namespace,
                candidate_id,
                decision,
                reason,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
        )

    @mcp.tool()
    def revert_material_match(namespace: str, candidate_id: str, reason: str) -> dict:
        """Revert a reviewed materials identity decision; the records stay intact and ungrouped."""
        from src.kb.materials_identity import MaterialsIdentity

        return gated(
            "revert_material_match",
            lambda conn: MaterialsIdentity(conn).revert(
                namespace, candidate_id, reason, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
        )

    @mcp.tool()
    def material_release_changes(
        namespace: str,
        provider: str,
        from_release: str | None = None,
        to_release: str | None = None,
        material: str | None = None,
        property: str | None = None,
    ) -> dict:
        """Added, changed (both values), deprecated/withdrawn as stated, and absent values between two releases."""
        from src.kb.materials_releases import release_changes

        return gated(
            "material_release_changes",
            lambda conn: release_changes(
                conn,
                namespace,
                provider,
                scopes=who()[1],
                from_release=from_release,
                to_release=to_release,
                material=material,
                prop=property,
            ),
        )

    @mcp.tool()
    def create_material_watch(
        namespace: str,
        request_key: str,
        material: str | None = None,
        property: str | None = None,
        provider: str | None = None,
        delivery: dict | None = None,
    ) -> dict:
        """Watch a material, property and/or provider for new releases and corrections via a knowledge subscription."""
        from src.kb.materials_releases import MaterialsReleaseWatch

        return gated(
            "create_material_watch",
            lambda conn: MaterialsReleaseWatch(conn).create(
                namespace,
                request_key,
                targets={
                    "material": material,
                    "property": property,
                    "provider": provider,
                },
                principal_id=who()[0],
                scopes=who()[1],
                delivery=delivery,
            ),
            write=True,
        )

    @mcp.tool()
    def run_material_watch(
        namespace: str, subscription_id: str, watermark: int | None = None
    ) -> dict:
        """Evaluate a watch at a committed watermark (latest when omitted); replay creates no new events."""
        from src.kb.materials_releases import MaterialsReleaseWatch

        del namespace
        return gated(
            "run_material_watch",
            lambda conn: MaterialsReleaseWatch(conn).run(
                subscription_id, watermark, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
        )

    @mcp.tool()
    def poll_material_watch(
        namespace: str, subscription_id: str, cursor: str = ""
    ) -> dict:
        """Poll watch events after a cursor."""
        from src.kb.materials_releases import MaterialsReleaseWatch

        del namespace
        return gated(
            "poll_material_watch",
            lambda conn: MaterialsReleaseWatch(conn, initialize=False).poll(
                subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor
            ),
        )
