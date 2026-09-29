"""Economics public-finance feature entry points: budget lines, comparisons, payments, findings, links, monitors.

Acquisition runs through the shared source-pack tools (pack
``economic-statistics-and-filings``: ``bundeshaushalt-open-data``,
``berlin-haushalt``, ``eu-financial-transparency-system``,
``eurostat-government-finance``); audit findings are recorded from operator
finding sheets. Every answer cites the source revision and accounting basis of
each figure; plan, outturn and payment records stay distinct. No tool
forecasts a fiscal outcome, determines waste or fraud, or nets figures across
accounting bases without a cited method.
"""

READ = "knowledge:economic:public-finance:read"
WRITE = "knowledge:economic:public-finance:write"
REVIEW = "knowledge:economic:public-finance:review"
OWNERSHIP_READ = "knowledge:ownership:read"
OWNERSHIP_WRITE = "knowledge:ownership:write"
OWNERSHIP_REVIEW = "knowledge:ownership:review"
ECONOMIC_READ = "knowledge:economic:read"
ECONOMIC_WRITE = "knowledge:economic:write"
LEGAL_READ = "knowledge:legal:read"
DOSSIER_READ = "knowledge:political:dossier:read"
PROCUREMENT_READ = "knowledge:procurement:read"
GEO_READ = "knowledge:geospatial:read"

PUBLIC_FINANCE_WRITES = {
    "import_audit_findings",
    "propose_public_finance_identity_matches",
    "review_public_finance_identity_match",
    "revert_public_finance_identity_match",
    "link_budget_acts",
    "link_budget_dossier",
    "review_public_finance_link",
    "revert_public_finance_link",
    "propose_public_finance_award_parties",
    "link_budget_districts",
    "record_public_finance_reconciliation",
    "compare_government_finance_vintages",
    "create_public_finance_monitor",
    "run_public_finance_monitor",
}
PUBLIC_FINANCE_READS = {
    "public_finance_source_contracts",
    "public_finance_readiness",
    "list_budget_lines",
    "inspect_budget_line",
    "compare_budget_line",
    "budget_line_dossier",
    "list_beneficiary_payments",
    "beneficiary_dossier",
    "list_audit_findings",
    "list_public_finance_identity_candidates",
    "list_public_finance_links",
    "budget_line_place",
    "list_government_finance_series",
    "poll_public_finance_monitor",
}
PUBLIC_FINANCE_TOOLS = PUBLIC_FINANCE_WRITES | PUBLIC_FINANCE_READS
# Every scope each tool always reads or writes. Scopes needed only for an optional argument (a GFS series beside a
# line, procurement award context, funding candidates) are checked when that argument is passed and documented as
# conditional.
PUBLIC_FINANCE_SCOPES = {
    "public_finance_source_contracts": [],
    "public_finance_readiness": [READ],
    "list_budget_lines": [READ],
    "inspect_budget_line": [READ],
    "compare_budget_line": [READ],
    "budget_line_dossier": [READ, OWNERSHIP_READ],
    "list_beneficiary_payments": [READ],
    "beneficiary_dossier": [READ, OWNERSHIP_READ],
    "list_audit_findings": [READ],
    "list_public_finance_identity_candidates": [READ, OWNERSHIP_READ],
    "list_public_finance_links": [READ],
    "budget_line_place": [READ],
    "list_government_finance_series": [READ],
    "poll_public_finance_monitor": [READ, "knowledge:subscriptions:read"],
    "import_audit_findings": [WRITE],
    "propose_public_finance_identity_matches": [READ, OWNERSHIP_READ, OWNERSHIP_WRITE],
    "review_public_finance_identity_match": [OWNERSHIP_REVIEW],
    "revert_public_finance_identity_match": [OWNERSHIP_REVIEW],
    "link_budget_acts": [WRITE, LEGAL_READ],
    "link_budget_dossier": [WRITE, DOSSIER_READ],
    "review_public_finance_link": [REVIEW],
    "revert_public_finance_link": [REVIEW],
    "propose_public_finance_award_parties": [
        READ,
        PROCUREMENT_READ,
        OWNERSHIP_READ,
        OWNERSHIP_WRITE,
    ],
    "link_budget_districts": [WRITE, GEO_READ],
    "record_public_finance_reconciliation": [WRITE],
    "compare_government_finance_vintages": [READ, ECONOMIC_READ, ECONOMIC_WRITE],
    "create_public_finance_monitor": [READ, "knowledge:subscriptions:write"],
    "run_public_finance_monitor": [READ, "knowledge:subscriptions:write"],
}


def required_scopes(tool_name, mutability):
    return PUBLIC_FINANCE_SCOPES.get(
        tool_name, [WRITE if mutability == "write" else READ]
    )


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    @mcp.tool()
    def public_finance_source_contracts() -> dict:
        """Per-provider access decisions, hierarchy, vintage, unit and accounting-basis semantics, and terms."""
        from src.ingestion.public_finance_sources import (
            PROVIDER_CONTRACTS,
            REVIEW_BOUNDARY,
        )

        return {"contracts": PROVIDER_CONTRACTS, "review_boundary": REVIEW_BOUNDARY}

    @mcp.tool()
    def public_finance_readiness() -> dict:
        """Whether the Economics public-finance feature is selected, and per-provider releases and decisions."""
        from src.kb.public_finance import readiness

        return safe(lambda conn: readiness(conn), required_scope=READ)

    @mcp.tool()
    def list_budget_lines(
        namespace: str,
        scheme: str | None = None,
        codes: dict | None = None,
        label: str | None = None,
        provider: str | None = None,
    ) -> dict:
        """Budget lines in each source's own hierarchy (codes and labels as published); no cross-source hierarchy."""
        from src.kb.public_finance import PublicFinanceStore, authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            return {
                "lines": PublicFinanceStore(conn, initialize=False).lines(
                    namespace,
                    scheme=scheme,
                    codes=codes,
                    label=label,
                    provider=provider,
                )
            }

        return safe(run, required_scope=READ)

    @mcp.tool()
    def inspect_budget_line(namespace: str, line_id: str) -> dict:
        """A line's plan, supplementary-plan and outturn history across fiscal years and vintages, and the audit
        findings that cite it, every figure cited with its accounting basis."""
        from src.kb.public_finance_queries import PublicFinanceQueries

        return safe(
            lambda conn: PublicFinanceQueries(
                conn, initialize=False
            ).inspect_budget_line(namespace, line_id, scopes=who()[1]),
            required_scope=READ,
        )

    @mcp.tool()
    def compare_budget_line(
        namespace: str, line_id: str, fiscal_year: str, gfs_series_id: str | None = None
    ) -> dict:
        """Plan against outturn and vintage against vintage for one line and year, side by side with citations; a
        difference only on the same accounting basis and currency (or under a cited method); conflicting sources
        flagged. Conditional scope: gfs_series_id needs knowledge:economic:read (shown beside, never netted)."""
        from src.kb.public_finance_queries import PublicFinanceQueries

        return safe(
            lambda conn: PublicFinanceQueries(
                conn, initialize=False
            ).compare_budget_line(
                namespace,
                line_id,
                fiscal_year,
                scopes=who()[1],
                gfs_series_id=gfs_series_id,
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def budget_line_dossier(
        namespace: str, line_id: str, procurement_namespace: str | None = None
    ) -> dict:
        """A line's cited budget dossier: figures by year, payments, audit findings, cited acts and dossiers, district
        place, beneficiaries with reviewed identity, and unknowns. Conditional scope: procurement_namespace needs
        knowledge:procurement:read (award history shown as context only)."""
        from src.kb.public_finance_queries import PublicFinanceQueries

        return safe(
            lambda conn: PublicFinanceQueries(conn, initialize=False).budget_dossier(
                namespace,
                line_id,
                scopes=who()[1],
                procurement_namespace=procurement_namespace,
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def list_beneficiary_payments(
        namespace: str,
        beneficiary_key: str | None = None,
        programme: str | None = None,
        budget_line: str | None = None,
        fiscal_year: str | None = None,
    ) -> dict:
        """Published commitment and payment rows (current revision), each cited; never a plan or outturn figure."""
        from src.kb.public_finance import PublicFinanceStore, authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            return {
                "payments": PublicFinanceStore(conn, initialize=False).payments(
                    namespace,
                    beneficiary_key=beneficiary_key,
                    programme=programme,
                    budget_line=budget_line,
                    fiscal_year=fiscal_year,
                )
            }

        return safe(run, required_scope=READ)

    @mcp.tool()
    def beneficiary_dossier(
        namespace: str, beneficiary_key: str, procurement_namespace: str | None = None
    ) -> dict:
        """A beneficiary's payments, grant candidates and reviewed corporate identity. Conditional scope:
        procurement_namespace needs knowledge:procurement:read (award history as context only)."""
        from src.kb.public_finance_queries import PublicFinanceQueries

        return safe(
            lambda conn: PublicFinanceQueries(
                conn, initialize=False
            ).beneficiary_dossier(
                namespace,
                beneficiary_key,
                scopes=who()[1],
                procurement_namespace=procurement_namespace,
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def list_audit_findings(
        namespace: str, line_id: str | None = None, report_id: str | None = None
    ) -> dict:
        """Audit report passages and the lines they cite, as recorded; no verdict."""
        from src.kb.public_finance import PublicFinanceStore, authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            return {
                "findings": PublicFinanceStore(conn, initialize=False).findings(
                    namespace, line_id=line_id, report_id=report_id
                )
            }

        return safe(run, required_scope=READ)

    @mcp.tool()
    def import_audit_findings(namespace: str, sheet: dict) -> dict:
        """Record an operator's finding sheet for one audit report (report, passage locators, quotes, cited lines);
        a sheet with a verdict or determination is refused."""
        from src.kb.public_finance import PublicFinanceStore

        return safe(
            lambda conn: PublicFinanceStore(conn).import_findings(
                namespace, sheet, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def list_public_finance_identity_candidates(
        namespace: str, record_key: str | None = None
    ) -> dict:
        """Beneficiary identity candidates with basis, evidence and review state; nothing is merged."""
        from src.kb.public_finance_identity import PublicFinanceIdentity

        return safe(
            lambda conn: {
                "candidates": PublicFinanceIdentity(conn, initialize=False).candidates(
                    namespace, scopes=who()[1], record_key=record_key
                )
            },
            required_scope=READ,
        )

    @mcp.tool()
    def propose_public_finance_identity_matches(
        namespace: str,
        ownership_namespace: str | None = None,
        funding_namespace: str | None = None,
    ) -> dict:
        """Offer beneficiaries to ownership entities, canonical entities and funding awards as reviewable
        candidates. Conditional scope: funding_namespace needs knowledge:funding:read."""
        from src.kb.public_finance_identity import PublicFinanceIdentity

        return safe(
            lambda conn: PublicFinanceIdentity(conn).propose(
                namespace,
                principal_id=who()[0],
                scopes=who()[1],
                ownership_namespace=ownership_namespace,
                funding_namespace=funding_namespace,
            ),
            write=True,
            required_scope=OWNERSHIP_WRITE,
        )

    @mcp.tool()
    def review_public_finance_identity_match(
        namespace: str, candidate_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject a beneficiary candidate as an entity identity decision; records are never rewritten."""
        from src.kb.public_finance_identity import PublicFinanceIdentity

        def run(conn):
            identity = PublicFinanceIdentity(conn)
            return identity.view(
                identity.service.review(
                    namespace,
                    candidate_id,
                    decision,
                    reason,
                    principal_id=who()[0],
                    scopes=who()[1],
                )
            )

        return safe(run, write=True, required_scope=OWNERSHIP_REVIEW)

    @mcp.tool()
    def revert_public_finance_identity_match(
        namespace: str, candidate_id: str, reason: str
    ) -> dict:
        """Undo an accepted or rejected beneficiary decision; the beneficiary is the source string again."""
        from src.kb.public_finance_identity import PublicFinanceIdentity

        def run(conn):
            identity = PublicFinanceIdentity(conn)
            return identity.view(
                identity.service.revert(
                    namespace,
                    candidate_id,
                    reason,
                    principal_id=who()[0],
                    scopes=who()[1],
                )
            )

        return safe(run, write=True, required_scope=OWNERSHIP_REVIEW)

    @mcp.tool()
    def link_budget_acts(namespace: str, legal_namespace: str) -> dict:
        """Link plans to budget acts by an exact stated citation (CELEX, ELI, BGBl, GVBl); title overlap is only a
        candidate; unmatched citations stay unresolved."""
        from src.kb.public_finance_links import PublicFinanceLinks

        return safe(
            lambda conn: PublicFinanceLinks(conn).link_acts(
                namespace,
                legal_namespace=legal_namespace,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def link_budget_dossier(
        namespace: str, dossier_namespace: str, dossier_id: str
    ) -> dict:
        """Link plans to a legislative dossier by the dossier's own identifier; shared keywords are candidates."""
        from src.kb.public_finance_links import PublicFinanceLinks

        return safe(
            lambda conn: PublicFinanceLinks(conn).link_dossier(
                namespace,
                dossier_namespace,
                dossier_id,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def list_public_finance_links(
        namespace: str, subject_id: str | None = None, target_kind: str | None = None
    ) -> dict:
        """Act and dossier links and candidates with basis, source revision, rule or reviewer."""
        from src.kb.public_finance_links import PublicFinanceLinks

        return safe(
            lambda conn: {
                "links": PublicFinanceLinks(conn, initialize=False).links(
                    namespace,
                    scopes=who()[1],
                    subject_id=subject_id,
                    target_kind=target_kind,
                )
            },
            required_scope=READ,
        )

    @mcp.tool()
    def review_public_finance_link(
        namespace: str, link_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject an act or dossier candidate with a reason."""
        from src.kb.public_finance_links import PublicFinanceLinks

        return safe(
            lambda conn: PublicFinanceLinks(conn).review(
                namespace,
                link_id,
                decision,
                reason,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=REVIEW,
        )

    @mcp.tool()
    def revert_public_finance_link(namespace: str, link_id: str, reason: str) -> dict:
        """Undo a reviewed act or dossier decision."""
        from src.kb.public_finance_links import PublicFinanceLinks

        return safe(
            lambda conn: PublicFinanceLinks(conn).revert(
                namespace, link_id, reason, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope=REVIEW,
        )

    @mcp.tool()
    def propose_public_finance_award_parties(
        namespace: str, procurement_namespace: str
    ) -> dict:
        """Offer beneficiaries and procurement suppliers as reviewable identity candidates."""
        from src.kb.public_finance_links import PublicFinanceLinks

        return safe(
            lambda conn: PublicFinanceLinks(conn).propose_award_parties(
                namespace, procurement_namespace, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope=OWNERSHIP_WRITE,
        )

    @mcp.tool()
    def link_budget_districts(namespace: str, geo_namespace: str = "global") -> dict:
        """Link Berlin district lines to the district boundaries and places by the published district code only."""
        from src.kb.public_finance_places import PublicFinancePlaces

        return safe(
            lambda conn: PublicFinancePlaces(conn).link_districts(
                namespace,
                principal_id=who()[0],
                scopes=who()[1],
                geo_namespace=geo_namespace,
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def budget_line_place(namespace: str, line_id: str) -> dict:
        """A Berlin line's district link (feature and place), or why it has none."""
        from src.kb.public_finance_places import PublicFinancePlaces

        return safe(
            lambda conn: PublicFinancePlaces(conn, initialize=False).place(
                namespace, line_id, scopes=who()[1]
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def record_public_finance_reconciliation(
        namespace: str,
        provider: str,
        from_basis: str,
        to_basis: str,
        citation: dict,
        description: str,
    ) -> dict:
        """Record a reconciliation method the source publishes, with its citation; without one, figures on
        different accounting bases are never differenced."""
        from src.kb.public_finance_queries import PublicFinanceQueries

        return safe(
            lambda conn: PublicFinanceQueries(conn).record_reconciliation(
                namespace,
                provider=provider,
                from_basis=from_basis,
                to_basis=to_basis,
                citation=citation,
                description=description,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def list_government_finance_series(
        namespace: str, dataset: str | None = None
    ) -> dict:
        """Acquired Eurostat GFS series (ESA 2010) with their vintages and release snapshots."""
        from src.kb.public_finance_gfs import GovernmentFinanceStatistics

        return safe(
            lambda conn: {
                "series": GovernmentFinanceStatistics(conn, initialize=False).series(
                    namespace, scopes=who()[1], dataset=dataset
                )
            },
            required_scope=READ,
        )

    @mcp.tool()
    def compare_government_finance_vintages(
        namespace: str,
        series_id: str,
        left_as_of: int | None = None,
        right_as_of: int | None = None,
    ) -> dict:
        """Two Eurostat publications of one GFS series through the existing economic release comparison."""
        from src.kb.public_finance_gfs import GovernmentFinanceStatistics

        return safe(
            lambda conn: GovernmentFinanceStatistics(conn).compare(
                namespace,
                series_id,
                scopes=who()[1],
                left_as_of=left_as_of,
                right_as_of=right_as_of,
            ),
            write=True,
            required_scope=ECONOMIC_READ,
        )

    @mcp.tool()
    def create_public_finance_monitor(
        namespace: str,
        request_key: str,
        watch: str,
        key: str,
        delivery: dict | None = None,
    ) -> dict:
        """A subscription on a budget line, programme or beneficiary; no new scheduler."""
        from src.kb.public_finance_monitoring import PublicFinanceMonitor

        return safe(
            lambda conn: PublicFinanceMonitor(conn).create(
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
    def run_public_finance_monitor(
        subscription_id: str, watermark: int | None = None
    ) -> dict:
        """Evaluate a monitor at a committed watermark: supplementary plans, outturn vintages, payment publications
        and audit findings, each with its source revision and the revision it supersedes."""
        from src.kb.public_finance_monitoring import PublicFinanceMonitor

        return safe(
            lambda conn: PublicFinanceMonitor(conn).run(
                subscription_id, watermark, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope="knowledge:subscriptions:write",
        )

    @mcp.tool()
    def poll_public_finance_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a public-finance monitor's events through the subscription delivery path."""
        from src.kb.public_finance_monitoring import PublicFinanceMonitor

        return safe(
            lambda conn: PublicFinanceMonitor(conn, initialize=False).poll(
                subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor
            ),
            required_scope="knowledge:subscriptions:read",
        )
