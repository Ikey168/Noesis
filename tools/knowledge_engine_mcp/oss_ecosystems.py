"""Open-source Software Ecosystems entry points: release and licence history, graphs as of a date, identity, monitors.

Acquisition runs through the shared source-pack tools (pack ``oss-ecosystems``:
``pypi-json``, ``npm-registry``, ``crates-io``, ``maven-central``,
``deps-dev-npm``, ``spdx-license-list``, ``software-heritage``). Every answer
cites source, revision and observation time, and states its knowledge cutoff,
generation and gaps. No tool returns a quality, health, popularity or trust
verdict, profiles a person, or interprets a licence; advisories are cited from
``technology.vulnerabilities``.
"""

OSS_READ, OSS_WRITE, OSS_REVIEW = (
    "knowledge:oss:read",
    "knowledge:oss:write",
    "knowledge:oss:review",
)
TECHNICAL_READ = "knowledge:technical:read"
OSS_WRITES = {
    "propose_repository_package_matches",
    "propose_publisher_organisation_matches",
    "propose_oss_identity_link",
    "review_oss_identity_match",
    "revert_oss_identity_match",
    "create_package_monitor",
    "run_package_monitor",
    "register_oss_schemas",
}
OSS_READS = {
    "oss_source_contracts",
    "oss_ecosystems_readiness",
    "package_release_history",
    "licence_history",
    "packages_by_organisation",
    "dependency_graph_as_of",
    "replay_dependency_graph",
    "package_advisories",
    "compare_inventory_with_graph",
    "list_oss_identity_candidates",
    "list_oss_archive_provenance",
    "poll_package_monitor",
}
OSS_TOOLS = OSS_WRITES | OSS_READS
# Every scope a tool always reads or writes (the catalog's required scopes; asserted by a test).
OSS_SCOPES = {
    "oss_source_contracts": [],
    "oss_ecosystems_readiness": [OSS_READ],
    "package_release_history": [OSS_READ],
    "licence_history": [OSS_READ],
    "packages_by_organisation": [OSS_READ],
    "dependency_graph_as_of": [OSS_READ],
    "replay_dependency_graph": [OSS_READ],
    # Advisories are read through technology.vulnerabilities; the inventory through the technical owner.
    "package_advisories": [OSS_READ, TECHNICAL_READ],
    "compare_inventory_with_graph": [OSS_READ, TECHNICAL_READ],
    "list_oss_identity_candidates": [OSS_READ],
    "list_oss_archive_provenance": [OSS_READ],
    # Polling re-checks current access to what the monitor evaluated (the OSS read scope).
    "poll_package_monitor": ["knowledge:subscriptions:read", OSS_READ],
    # Proposing reads the store and writes candidates.
    "propose_repository_package_matches": [OSS_READ, OSS_WRITE],
    "propose_publisher_organisation_matches": [OSS_READ, OSS_WRITE],
    "propose_oss_identity_link": [OSS_WRITE],
    # Reviews record entity identity decisions.
    "review_oss_identity_match": [OSS_REVIEW],
    "revert_oss_identity_match": [OSS_REVIEW],
    "create_package_monitor": [OSS_READ, "knowledge:subscriptions:write"],
    # Running reads the subscription and the store and writes subscription events.
    "run_package_monitor": [
        OSS_READ,
        "knowledge:subscriptions:read",
        "knowledge:subscriptions:write",
    ],
    "register_oss_schemas": [OSS_WRITE, "knowledge:schema:register"],
}
QUERY_EXAMPLES = {
    "package_release_history": {
        "arguments": {
            "namespace": "global",
            "package": "pkg:pypi:fixture-parser",
            "as_of": "2026-07-01",
        },
        "semantics": "each release's state on the date as its registry stated it, reasons verbatim",
    },
    "dependency_graph_as_of": {
        "arguments": {
            "namespace": "global",
            "package": "pkg:cargo:fixture-codec",
            "version": "0.1.0",
            "date": "2026-04-01",
        },
        "semantics": "declared-constraint resolution, not an observed lockfile; unresolved edges listed",
    },
    "licence_history": {
        "arguments": {"namespace": "global", "package": "pkg:pypi:fixture-parser"},
        "semantics": "declarations and SPDX expressions per release with the list version; changes highlighted",
    },
    "packages_by_organisation": {
        "arguments": {"namespace": "global", "organisation": "fixture-labs"},
        "semantics": "packages an organisation-level publisher declares; never a person",
    },
}


def required_scopes(tool_name, mutability):
    return OSS_SCOPES.get(tool_name, [OSS_WRITE if mutability == "write" else OSS_READ])


def _require(scopes, needed):
    missing = [s for s in needed if s not in scopes]
    if missing and "operator" not in scopes:
        from src.kb.oss_ecosystem_store import OssStoreError

        raise OssStoreError("unauthorized", f"{missing[0]} scope is required")


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def call(name, operation, *, write=False):
        """Run with the tool's declared scopes checked up front, then the owners' own checks."""

        def run(conn):
            _require(who()[1], OSS_SCOPES[name])
            return operation(conn)

        return safe(
            run,
            write=write,
            required_scope=OSS_SCOPES[name][0] if OSS_SCOPES[name] else None,
        )

    @mcp.tool()
    def oss_source_contracts() -> dict:
        """Per-source access decisions, terms, limits, dropped personal fields and live-verification state (OS01)."""
        from src.ingestion.oss_ecosystem_sources import (
            LIVE_VERIFICATION,
            PROVIDER_CONTRACTS,
        )

        return {
            "contracts": PROVIDER_CONTRACTS,
            "live_verification": LIVE_VERIFICATION,
            "audit": "docs/development/oss-ecosystems-evidence/source-audit.md",
        }

    @mcp.tool()
    def oss_ecosystems_readiness(namespace: str) -> dict:
        """Bundle selection, optional features and ready/fixture-only/unavailable per source; live kept separate."""
        from src.kb.oss_ecosystem_bundle import BUNDLE, readiness

        return call(
            "oss_ecosystems_readiness",
            lambda conn: {
                **readiness(conn, namespace, scopes=who()[1]),
                "declaration": BUNDLE,
            },
        )

    @mcp.tool()
    def package_release_history(
        namespace: str,
        package: str,
        ecosystem: str | None = None,
        as_of: str | None = None,
        acquired_by: str | None = None,
        include_revisions: bool = False,
    ) -> dict:
        """Releases of a package with their state on a date (yank, deprecation, unpublish reasons verbatim).

        The registry's listing first, deps.dev beside it; publishers are organisations only; the repository is
        the package's only through a reviewed decision. Knowledge cutoff, generation and gaps are stated.
        """
        from src.kb.oss_ecosystem_queries import package_release_history as query

        return call(
            "package_release_history",
            lambda conn: query(
                conn,
                namespace,
                package,
                scopes=who()[1],
                ecosystem=ecosystem,
                as_of=as_of,
                acquired_by=acquired_by,
                include_revisions=include_revisions,
            ),
        )

    @mcp.tool()
    def licence_history(
        namespace: str,
        package: str,
        ecosystem: str | None = None,
        list_version: str | None = None,
        acquired_by: str | None = None,
    ) -> dict:
        """Declared licence per release as raw text and SPDX expression with the list version; changes highlighted.

        Registry and deps.dev disagreements are shown side by side. Quoted and normalised, never interpreted.
        """
        from src.kb.oss_ecosystem_queries import licence_history as query

        return call(
            "licence_history",
            lambda conn: query(
                conn,
                namespace,
                package,
                scopes=who()[1],
                ecosystem=ecosystem,
                list_version=list_version,
                acquired_by=acquired_by,
            ),
        )

    @mcp.tool()
    def packages_by_organisation(
        namespace: str, organisation: str, acquired_by: str | None = None
    ) -> dict:
        """Packages an organisation-level publisher declares (or entity:<id> through reviewed matches); never a person."""
        from src.kb.oss_ecosystem_queries import packages_by_organisation as query

        return call(
            "packages_by_organisation",
            lambda conn: query(
                conn, namespace, organisation, scopes=who()[1], acquired_by=acquired_by
            ),
        )

    @mcp.tool()
    def dependency_graph_as_of(
        namespace: str,
        package: str,
        version: str,
        date: str,
        ecosystem: str | None = None,
        depth: int = 3,
        max_nodes: int = 200,
        scopes: list[str] | None = None,
        environment: dict | None = None,
        acquired_by: str | None = None,
    ) -> dict:
        """A release's dependency graph resolved from declared constraints against releases known on the date.

        Declared-constraint resolution, not an observed lockfile. Unresolved edges are listed with reasons, other
        scopes and markers are reported, deps.dev's graph is shown beside, and a replayable receipt pins revisions.
        """
        from src.kb.oss_ecosystem_graph import dependency_graph_as_of as query

        return call(
            "dependency_graph_as_of",
            lambda conn: query(
                conn,
                namespace,
                package,
                version,
                date,
                scopes=who()[1],
                ecosystem=ecosystem,
                depth=depth,
                max_nodes=max_nodes,
                include_scopes=scopes or ["runtime"],
                environment=environment,
                acquired_by=acquired_by,
            ),
        )

    @mcp.tool()
    def replay_dependency_graph(receipt: dict) -> dict:
        """Recompute a graph from its receipt; reports whether the pinned revisions and the result still match."""
        from src.kb.oss_ecosystem_graph import DependencyGraphs

        return call(
            "replay_dependency_graph",
            lambda conn: DependencyGraphs(conn).replay(receipt, scopes=who()[1]),
        )

    @mcp.tool()
    def package_advisories(
        namespace: str,
        package: str,
        ecosystem: str | None = None,
        version: str | None = None,
        vulnerability_namespace: str | None = None,
        as_of: str | None = None,
    ) -> dict:
        """Advisories naming a package, cited from technology.vulnerabilities by revision; nothing is copied.

        No exploitability or risk verdict; the version's position against a cited range is not a verdict.
        """
        from src.kb.oss_ecosystem_links import OssLinks

        return call(
            "package_advisories",
            lambda conn: OssLinks(conn).package_advisories(
                namespace,
                package,
                scopes=who()[1],
                ecosystem=ecosystem,
                version=version,
                vulnerability_namespace=vulnerability_namespace,
                as_of=as_of,
            ),
        )

    @mcp.tool()
    def compare_inventory_with_graph(
        namespace: str,
        inventory_id: str,
        package: str,
        version: str,
        ecosystem: str | None = None,
        inventory_date: str | None = None,
        depth: int = 3,
    ) -> dict:
        """A pinned technical inventory beside the as-of graph of the same root; pins yanked or deprecated then."""
        from src.kb.oss_ecosystem_links import OssLinks

        return call(
            "compare_inventory_with_graph",
            lambda conn: OssLinks(conn).compare_inventory(
                namespace,
                inventory_id,
                package,
                version,
                owner_id=who()[0],
                scopes=who()[1],
                ecosystem=ecosystem,
                inventory_date=inventory_date,
                depth=depth,
            ),
        )

    @mcp.tool()
    def list_oss_identity_candidates(
        namespace: str,
        kind: str | None = None,
        key: str | None = None,
        state: str | None = None,
    ) -> dict:
        """Repository and organisation candidates, found from either side; shared repository claims are flagged."""
        from src.kb.oss_ecosystem_identity import OssIdentity

        def run(conn):
            from src.kb.oss_ecosystem_store import OssEcosystemStore

            OssEcosystemStore(conn, initialize=False).require_ready(namespace)
            return {
                "candidates": OssIdentity(conn, initialize=False).candidates(
                    namespace, scopes=who()[1], kind=kind, key=key, state=state
                )
            }

        return call("list_oss_identity_candidates", run)

    @mcp.tool()
    def list_oss_archive_provenance(
        namespace: str, repository: str | None = None
    ) -> dict:
        """Software Heritage visits and snapshot tags for asserted origins, cited by revision and SWHID."""
        from src.kb.oss_ecosystem_store import OssEcosystemStore, authorize

        def run(conn):
            store = OssEcosystemStore(conn, initialize=False)
            authorize(namespace, who()[1], OSS_READ)
            store.require_ready(namespace, sources=["software-heritage"])
            key = None
            if repository:
                from src.kb.oss_ecosystem_records import repository_key

                key = repository_key(repository)
            items = []
            for record in store.records(
                namespace, record_type="archive_provenance", repository_key=key
            ):
                revision = store.current(record["record_id"])
                items.append(
                    {
                        **revision["statement"],
                        "revision_id": revision["revision_id"],
                        "observed_at_ms": revision["observed_at_ms"],
                    }
                )
            return {
                "namespace": namespace,
                "archive": items,
                "generation": store.generation(namespace),
            }

        return call("list_oss_archive_provenance", run)

    @mcp.tool()
    def propose_repository_package_matches(namespace: str) -> dict:
        """Propose repository candidates from registry links, deps.dev related projects and archive tags. Idempotent."""
        from src.kb.oss_ecosystem_identity import OssIdentity

        return call(
            "propose_repository_package_matches",
            lambda conn: OssIdentity(conn).propose_repositories(
                namespace, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
        )

    @mcp.tool()
    def propose_publisher_organisation_matches(namespace: str) -> dict:
        """Propose organisation entities for organisation-level publishers; npm scopes and groupIds never."""
        from src.kb.oss_ecosystem_identity import OssIdentity

        return call(
            "propose_publisher_organisation_matches",
            lambda conn: OssIdentity(conn).propose_organisations(
                namespace, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
        )

    @mcp.tool()
    def propose_oss_identity_link(
        namespace: str, left_key: str, right_key: str, reason: str
    ) -> dict:
        """A reviewer's own candidate: oss-package:<coordinate> with oss-repository:<key>, or a publisher with entity:<id>."""
        from src.kb.oss_ecosystem_identity import OssIdentity

        return call(
            "propose_oss_identity_link",
            lambda conn: OssIdentity(conn).propose_link(
                namespace,
                left_key=left_key,
                right_key=right_key,
                reason=reason,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
        )

    @mcp.tool()
    def review_oss_identity_match(
        namespace: str, candidate_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject a candidate as an entity identity decision; records are never merged."""
        from src.kb.oss_ecosystem_identity import OssIdentity

        return call(
            "review_oss_identity_match",
            lambda conn: OssIdentity(conn).review(
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
    def revert_oss_identity_match(
        namespace: str, candidate_id: str, reason: str
    ) -> dict:
        """Undo a decision; the candidate becomes reverted and no earlier decision is reactivated."""
        from src.kb.oss_ecosystem_identity import OssIdentity

        return call(
            "revert_oss_identity_match",
            lambda conn: OssIdentity(conn).revert(
                namespace, candidate_id, reason, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
        )

    @mcp.tool()
    def create_package_monitor(
        namespace: str,
        request_key: str,
        watch: str,
        key: str,
        version: str | None = None,
        depth: int = 2,
    ) -> dict:
        """Follow a package, a graph root (with a depth) or an organisation publisher through a subscription."""
        from src.kb.oss_ecosystem_monitoring import OssPackageMonitor

        return call(
            "create_package_monitor",
            lambda conn: OssPackageMonitor(conn).create(
                namespace,
                request_key,
                watch=watch,
                key=key,
                version=version,
                depth=depth,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
        )

    @mcp.tool()
    def run_package_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a package monitor at a committed watermark; notifications cite old and new revisions."""
        from src.kb.oss_ecosystem_monitoring import OssPackageMonitor

        return call(
            "run_package_monitor",
            lambda conn: OssPackageMonitor(conn).run(
                subscription_id, watermark, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
        )

    @mcp.tool()
    def poll_package_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Delivered package-monitor events after a cursor."""
        from src.kb.oss_ecosystem_monitoring import OssPackageMonitor

        return call(
            "poll_package_monitor",
            lambda conn: OssPackageMonitor(conn, initialize=False).poll(
                subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor
            ),
        )

    @mcp.tool()
    def register_oss_schemas() -> dict:
        """Register noesis-oss-ecosystem-record-v1 and noesis-oss-ecosystem-answer-v1 in the schema registry."""
        from src.kb.oss_ecosystem_records import register_schemas

        return call(
            "register_oss_schemas",
            lambda conn: {
                "modules": register_schemas(
                    conn, principal_id=who()[0], scopes=who()[1]
                )
            },
            write=True,
        )
