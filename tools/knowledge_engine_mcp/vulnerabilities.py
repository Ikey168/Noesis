"""Technology vulnerabilities feature entry points: CVEs across sources, component advisories, identity, monitors.

Acquisition runs through the shared source-pack tools (pack
``technical-software-knowledge``: ``nvd-cve-api``, ``nvd-cve-history``,
``osv-api``, ``github-advisory-database``, ``cisa-kev``, ``first-epss``,
``cve-services``, ``nvd-cpe-dictionary``, ``cwe-downloads``). Every answer
quotes what each source stated, cited to its revision; sources stay side by
side. No tool returns an exploitability or risk verdict, a priority, patch or
remediation advice, or a severity where the source states none.
"""

VULNERABILITY_WRITES = {
    "propose_vulnerability_component_matches",
    "propose_vulnerability_product_matches",
    "propose_vulnerability_component_link",
    "review_vulnerability_component_match",
    "revert_vulnerability_component_match",
    "create_vulnerability_monitor",
    "run_vulnerability_monitor",
}
VULNERABILITY_READS = {
    "vulnerability_source_contracts",
    "vulnerability_readiness",
    "inspect_vulnerability",
    "search_component_advisories",
    "compare_advisory_revisions",
    "list_vulnerability_components",
    "list_vulnerability_component_matches",
    "vulnerability_standard_links",
    "poll_vulnerability_monitor",
}
VULNERABILITY_TOOLS = VULNERABILITY_WRITES | VULNERABILITY_READS
VULNERABILITY_SCOPES = {
    "vulnerability_source_contracts": [],
    # Proposing reads the owner's inventory and the vulnerability store and writes candidates.
    "propose_vulnerability_component_matches": [
        "knowledge:technical:read",
        "knowledge:technical:write",
    ],
    "propose_vulnerability_product_matches": [
        "knowledge:technical:read",
        "knowledge:technical:write",
        "knowledge:products:read",
    ],
    "propose_vulnerability_component_link": ["knowledge:technical:write"],
    # Reviews record entity identity decisions and may publish the reviewed CPE crosswalk.
    "review_vulnerability_component_match": ["knowledge:technical:review"],
    "revert_vulnerability_component_match": ["knowledge:technical:review"],
    "vulnerability_standard_links": [
        "knowledge:technical:read",
        "knowledge:standards:read",
    ],
    "create_vulnerability_monitor": [
        "knowledge:technical:read",
        "knowledge:subscriptions:write",
    ],
    # Running reads the store, writes subscription events and reads news documents for related context.
    "run_vulnerability_monitor": [
        "knowledge:technical:read",
        "knowledge:subscriptions:write",
        "knowledge:read",
    ],
    "poll_vulnerability_monitor": ["knowledge:subscriptions:read"],
}


def required_scopes(tool_name, mutability):
    return VULNERABILITY_SCOPES.get(
        tool_name,
        [
            "knowledge:technical:write"
            if mutability == "write"
            else "knowledge:technical:read"
        ],
    )


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def queries(conn):
        from src.kb.vulnerability_queries import VulnerabilityQueries

        return VulnerabilityQueries(conn)

    @mcp.tool()
    def vulnerability_source_contracts() -> dict:
        """Per-provider access decisions, formats, identifiers, revision semantics and licences (V01 audit)."""
        from src.ingestion.vulnerability_sources import PROVIDER_CONTRACTS

        return {"contracts": PROVIDER_CONTRACTS}

    @mcp.tool()
    def vulnerability_readiness() -> dict:
        """Whether the Technology vulnerabilities feature is selected, and acquired series per source."""
        from src.kb.vulnerabilities import readiness

        return safe(
            lambda conn: readiness(conn), required_scope="knowledge:technical:read"
        )

    @mcp.tool()
    def inspect_vulnerability(
        namespace: str, identifier: str, as_of: str | None = None
    ) -> dict:
        """One CVE (or GHSA/OSV id) as every source stated it on a date, side by side.

        Returns advisory revisions per source, aliases with the sources asserting them, affected ranges and
        their agreement, weaknesses with CWE version, CVSS quoted per publisher, dated EPSS observations and
        KEV listings. No exploitability or risk verdict, no remediation advice and no inferred severity.
        """
        return safe(
            lambda conn: queries(conn).vulnerability(
                namespace, identifier, scopes=who()[1], as_of=as_of
            ),
            required_scope="knowledge:technical:read",
        )

    @mcp.tool()
    def search_component_advisories(
        namespace: str,
        coordinate: str | None = None,
        cpe_product: str | None = None,
        as_of: str | None = None,
    ) -> dict:
        """Advisories naming a package coordinate or CPE vendor:product as of a date; none found reads unknown.

        No exploitability or risk verdict, no remediation advice and no inferred severity.
        """
        return safe(
            lambda conn: queries(conn).component_advisories(
                namespace,
                scopes=who()[1],
                coordinate=coordinate,
                cpe_product=cpe_product,
                as_of=as_of,
            ),
            required_scope="knowledge:technical:read",
        )

    @mcp.tool()
    def compare_advisory_revisions(
        namespace: str,
        series_id: str,
        left_revision_id: str | None = None,
        right_revision_id: str | None = None,
    ) -> dict:
        """Field-level change of one source's advisory between two revisions (source change, not its effect)."""
        return safe(
            lambda conn: queries(conn).compare_revisions(
                namespace,
                series_id,
                scopes=who()[1],
                left_revision_id=left_revision_id,
                right_revision_id=right_revision_id,
            ),
            required_scope="knowledge:technical:read",
        )

    @mcp.tool()
    def vulnerability_standard_links(
        namespace: str, revision_id: str, standards_namespace: str | None = None
    ) -> dict:
        """Standards editions an advisory revision references explicitly (exact catalogue URL only)."""
        return safe(
            lambda conn: {
                "links": queries(conn).standard_links(
                    namespace,
                    revision_id,
                    scopes=who()[1],
                    standards_namespace=standards_namespace,
                )
            },
            required_scope="knowledge:technical:read",
        )

    @mcp.tool()
    def list_vulnerability_components(namespace: str) -> dict:
        """Components advisories name, as written by each source, with their match status (candidate/accepted)."""
        from src.kb.vulnerability_identity import VulnerabilityIdentity

        return safe(
            lambda conn: VulnerabilityIdentity(conn, initialize=False).components(
                namespace, scopes=who()[1]
            ),
            required_scope="knowledge:technical:read",
        )

    @mcp.tool()
    def list_vulnerability_component_matches(
        namespace: str, state: str | None = None, key: str | None = None
    ) -> dict:
        """Component match candidates and decisions, with basis, evidence, decision id and history."""
        from src.kb.vulnerability_identity import VulnerabilityIdentity

        return safe(
            lambda conn: {
                "matches": VulnerabilityIdentity(conn, initialize=False).matches(
                    namespace, scopes=who()[1], state=state, key=key
                )
            },
            required_scope="knowledge:technical:read",
        )

    @mcp.tool()
    def propose_vulnerability_component_matches(
        namespace: str, inventory_id: str
    ) -> dict:
        """Candidates between your inventory entries and advisory packages with the same canonical coordinate."""
        from src.kb.vulnerability_identity import VulnerabilityIdentity

        return safe(
            lambda conn: VulnerabilityIdentity(conn).propose_inventory(
                namespace, inventory_id, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope="knowledge:technical:write",
        )

    @mcp.tool()
    def propose_vulnerability_product_matches(
        namespace: str, products_namespace: str
    ) -> dict:
        """Candidates from CPE names that product records carry as explicit identifiers."""
        from src.kb.vulnerability_identity import VulnerabilityIdentity

        return safe(
            lambda conn: VulnerabilityIdentity(conn).propose_products(
                namespace,
                products_namespace=products_namespace,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope="knowledge:technical:write",
        )

    @mcp.tool()
    def propose_vulnerability_component_link(
        namespace: str, component_key: str, target_key: str, evidence: dict
    ) -> dict:
        """A reviewer's candidate (package:/product: to cpe-product:/advisory-package:); it still needs review."""
        from src.kb.vulnerability_identity import VulnerabilityIdentity

        return safe(
            lambda conn: VulnerabilityIdentity(conn).propose_link(
                namespace,
                component_key=component_key,
                target_key=target_key,
                evidence=evidence,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope="knowledge:technical:write",
        )

    @mcp.tool()
    def review_vulnerability_component_match(
        namespace: str, match_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject a candidate as an entity identity decision; records are linked, never rewritten."""
        from src.kb.vulnerability_identity import VulnerabilityIdentity

        return safe(
            lambda conn: VulnerabilityIdentity(conn).review(
                namespace,
                match_id,
                decision,
                reason,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope="knowledge:technical:review",
        )

    @mcp.tool()
    def revert_vulnerability_component_match(
        namespace: str, match_id: str, reason: str
    ) -> dict:
        """Undo a decision; the match returns to candidate and no record changes."""
        from src.kb.vulnerability_identity import VulnerabilityIdentity

        return safe(
            lambda conn: VulnerabilityIdentity(conn).revert(
                namespace, match_id, reason, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope="knowledge:technical:review",
        )

    @mcp.tool()
    def create_vulnerability_monitor(
        namespace: str,
        request_key: str,
        watch: str,
        key: str,
        delivery: dict | None = None,
    ) -> dict:
        """A subscription on a component (package coordinate) or a CVE; no new scheduler or delivery path."""
        from src.kb.vulnerability_monitoring import VulnerabilityMonitor

        return safe(
            lambda conn: VulnerabilityMonitor(conn).create(
                namespace,
                request_key,
                watch=watch,
                key=key,
                principal_id=who()[0],
                scopes=who()[1],
                delivery=delivery,
            ),
            write=True,
            required_scope="knowledge:subscriptions:write",
        )

    @mcp.tool()
    def run_vulnerability_monitor(
        subscription_id: str, watermark: int | None = None
    ) -> dict:
        """Evaluate a monitor at a committed watermark: new advisories, KEV additions, score changes, withdrawals."""
        from src.kb.vulnerability_monitoring import VulnerabilityMonitor

        return safe(
            lambda conn: VulnerabilityMonitor(conn).run(
                subscription_id, watermark, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope="knowledge:subscriptions:write",
        )

    @mcp.tool()
    def poll_vulnerability_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a vulnerability monitor's events through the subscription delivery path."""
        from src.kb.vulnerability_monitoring import VulnerabilityMonitor

        return safe(
            lambda conn: VulnerabilityMonitor(conn, initialize=False).poll(
                subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor
            ),
            required_scope="knowledge:subscriptions:read",
        )
