"""Funding & Grants development-finance entry points: aid activities, publishers, CRS aggregates, identity, monitors.

IATI activities and World Bank projects are acquired through bounded
``DurableHTTP`` selections; OECD CRS aggregates through the
``economic-statistics-and-filings`` source pack (``oecd-crs-development-finance``).
Every answer lists activities per publisher with the revision, dataset and
publisher coverage it used; nothing is totalled across publishers, no currency
is converted without a cited rate and date, and no effectiveness or impact is
judged. Each tool declares every scope it always reads or writes; scopes needed
only for an optional argument are checked when that argument is passed.
"""

import os

READ = "knowledge:funding:development-finance:read"
WRITE = "knowledge:funding:development-finance:write"
REVIEW = "knowledge:funding:development-finance:review"
INGEST = "knowledge:ingestion:execute"
OWNERSHIP_READ = "knowledge:ownership:read"
OWNERSHIP_WRITE = "knowledge:ownership:write"
OWNERSHIP_REVIEW = "knowledge:ownership:review"
FUNDING_READ = "knowledge:funding:read"
GEO_READ = "knowledge:geospatial:read"
GEO_WRITE = "knowledge:geospatial:write"
GEO_REVIEW = "knowledge:geospatial:review"
SCHEMA_REGISTER = "knowledge:schema:register"
PROJECTS_WRITE = "knowledge:projects:write"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
IATI_KEY_ENV = "NOESIS_IATI_DATASTORE_KEY"

DEVELOPMENT_FINANCE_WRITES = {
    "acquire_iati_activities",
    "acquire_world_bank_projects",
    "publish_development_finance_codelists",
    "normalise_development_finance_activities",
    "resolve_development_finance_places",
    "review_development_finance_place",
    "convert_development_finance_amount",
    "propose_development_finance_identity_matches",
    "review_development_finance_identity_match",
    "revert_development_finance_identity_match",
    "record_development_finance_answer",
    "attach_development_finance_answer",
    "create_development_finance_monitor",
    "run_development_finance_monitor",
}
DEVELOPMENT_FINANCE_READS = {
    "development_finance_source_contracts",
    "development_finance_readiness",
    "list_development_finance_activities",
    "inspect_development_finance_activity",
    "search_development_finance_transactions",
    "development_finance_publisher_coverage",
    "lookup_crs_aggregates",
    "list_world_bank_project_links",
    "list_development_finance_identity_candidates",
    "development_finance_open_calls",
    "development_finance_report_citation",
    "poll_development_finance_monitor",
}
DEVELOPMENT_FINANCE_TOOLS = DEVELOPMENT_FINANCE_WRITES | DEVELOPMENT_FINANCE_READS
DEVELOPMENT_FINANCE_SCOPES = {
    "development_finance_source_contracts": [],
    "development_finance_readiness": [READ],
    "list_development_finance_activities": [READ, OWNERSHIP_READ],
    "inspect_development_finance_activity": [READ, OWNERSHIP_READ],
    "search_development_finance_transactions": [READ, OWNERSHIP_READ],
    "development_finance_publisher_coverage": [READ],
    "lookup_crs_aggregates": [READ],
    "list_world_bank_project_links": [READ],
    "list_development_finance_identity_candidates": [READ, OWNERSHIP_READ],
    "development_finance_open_calls": [READ, OWNERSHIP_READ, FUNDING_READ],
    "development_finance_report_citation": [READ],
    "poll_development_finance_monitor": [READ, SUBSCRIPTIONS_READ],
    "acquire_iati_activities": [WRITE, INGEST],
    "acquire_world_bank_projects": [WRITE, INGEST],
    "publish_development_finance_codelists": [SCHEMA_REGISTER],
    "normalise_development_finance_activities": [WRITE],
    "resolve_development_finance_places": [WRITE, GEO_READ, GEO_WRITE],
    "review_development_finance_place": [READ, GEO_REVIEW],
    "convert_development_finance_amount": [WRITE],
    "propose_development_finance_identity_matches": [
        READ,
        OWNERSHIP_READ,
        OWNERSHIP_WRITE,
    ],
    "review_development_finance_identity_match": [OWNERSHIP_REVIEW],
    "revert_development_finance_identity_match": [OWNERSHIP_REVIEW],
    "record_development_finance_answer": [WRITE, READ, OWNERSHIP_READ],
    "attach_development_finance_answer": [READ, PROJECTS_WRITE],
    "create_development_finance_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_development_finance_monitor": [READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return DEVELOPMENT_FINANCE_SCOPES.get(
        tool_name, [WRITE if mutability == "write" else READ]
    )


def _require(scopes, *required):
    from src.kb.development_finance import DevelopmentFinanceError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise DevelopmentFinanceError("unauthorized", f"{', '.join(missing)} required")


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def gated(namespace, tool, operation, *, write=False):
        """Checks every declared scope, then the Funding & Grants bundle, then runs the operation."""
        scopes = DEVELOPMENT_FINANCE_SCOPES[tool]

        def run(conn):
            from src.kb.funding_bundle import require_enabled

            _require(who()[1], *scopes)
            require_enabled(conn, namespace)
            return operation(conn)

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def development_finance_source_contracts() -> dict:
        """Per-provider access decisions (IATI, OECD CRS, World Bank, portals), identifiers, terms and coverage."""
        from src.ingestion.development_finance_sources import (
            BOUNDED_COVERAGE,
            LIVE_VERIFICATION,
            NEVER_SENTENCE,
            PROVIDER_CONTRACTS,
        )

        return {
            "contracts": PROVIDER_CONTRACTS,
            "live_verification": LIVE_VERIFICATION,
            "bounded_coverage": BOUNDED_COVERAGE,
            "boundary": NEVER_SENTENCE,
        }

    @mcp.tool()
    def development_finance_readiness(namespace: str) -> dict:
        """Whether the development-finance feature is selected, which stores exist and datasets per provider."""
        from src.kb.development_finance import readiness

        return gated(
            namespace,
            "development_finance_readiness",
            lambda conn: readiness(conn, namespace),
        )

    @mcp.tool()
    def acquire_iati_activities(
        namespace: str,
        selection: dict,
        observation: str,
        budget_id: str,
        reuse_notice: str,
        rows: int = 100,
        max_pages: int = 5,
        max_requests: int = 10,
    ) -> dict:
        """Bounded IATI Datastore acquisition for named publishers, recipient countries or sectors."""

        def run(conn):
            from src.ingestion.development_finance_sources import (
                PROVIDER_HOSTS,
                IATIDatastoreClient,
            )
            from src.ingestion.provider_execution import DurableHTTP
            from src.kb.development_finance_acquisition import acquire_iati

            principal, scopes = who()
            http = DurableHTTP(
                conn,
                budget_id=budget_id,
                provider="iati-datastore",
                principal_id=principal,
                allowed_hosts=PROVIDER_HOSTS["iati-datastore"],
                reuse_notice=reuse_notice,
                max_requests=max_requests,
            )
            client = IATIDatastoreClient(
                http,
                principal_id=principal,
                subscription_key=os.environ.get(IATI_KEY_ENV),
            )
            return acquire_iati(
                client,
                selection,
                namespace=namespace,
                scopes=scopes,
                observation=observation,
                reuse_notice=reuse_notice,
                rows=rows,
                max_pages=max_pages,
            )

        return gated(namespace, "acquire_iati_activities", run, write=True)

    @mcp.tool()
    def acquire_world_bank_projects(
        namespace: str,
        selection: dict,
        observation: str,
        budget_id: str,
        reuse_notice: str,
        rows: int = 50,
        max_pages: int = 5,
        max_requests: int = 10,
    ) -> dict:
        """Bounded World Bank Projects API acquisition for named countries or project IDs."""

        def run(conn):
            from src.ingestion.development_finance_sources import (
                PROVIDER_HOSTS,
                WorldBankProjectsClient,
            )
            from src.ingestion.provider_execution import DurableHTTP
            from src.kb.development_finance_acquisition import acquire_world_bank

            principal, scopes = who()
            http = DurableHTTP(
                conn,
                budget_id=budget_id,
                provider="world-bank-projects",
                principal_id=principal,
                allowed_hosts=PROVIDER_HOSTS["world-bank-projects"],
                reuse_notice=reuse_notice,
                max_requests=max_requests,
            )
            return acquire_world_bank(
                WorldBankProjectsClient(http, principal_id=principal),
                selection,
                namespace=namespace,
                scopes=scopes,
                observation=observation,
                reuse_notice=reuse_notice,
                rows=rows,
                max_pages=max_pages,
            )

        return gated(namespace, "acquire_world_bank_projects", run, write=True)

    def _queries(conn, record=False):
        from src.kb.development_finance_queries import DevelopmentFinanceQueries

        # Read tools run on a read-only connection: the receipt is computed there and stored by the record tool.
        return DevelopmentFinanceQueries(conn, record=record)

    @mcp.tool()
    def list_development_finance_activities(
        namespace: str,
        funder: str | None = None,
        organisation: str | None = None,
        country: str | None = None,
        region: str | None = None,
        sector: str | None = None,
        publisher: str | None = None,
        crs_recipient: str | None = None,
        crs_donor: str | None = None,
        as_of: str | None = None,
    ) -> dict:
        """Who funded what where as of a date: activities per publisher, cited, with coverage and CRS vintages."""
        request = {
            "funder": funder,
            "organisation": organisation,
            "country": country,
            "region": region,
            "sector": sector,
            "publisher": publisher,
            "crs_recipient": crs_recipient,
            "crs_donor": crs_donor,
            "as_of": as_of,
        }
        return gated(
            namespace,
            "list_development_finance_activities",
            lambda conn: _queries(conn).list_activities(
                namespace, request, principal_id=who()[0], scopes=who()[1]
            ),
        )

    @mcp.tool()
    def inspect_development_finance_activity(
        namespace: str, iati_identifier: str, as_of: str | None = None
    ) -> dict:
        """One IATI identifier as each publisher reports it, side by side, with history, transactions and results."""
        return gated(
            namespace,
            "inspect_development_finance_activity",
            lambda conn: _queries(conn).inspect_activity(
                namespace,
                iati_identifier,
                as_of=as_of,
                principal_id=who()[0],
                scopes=who()[1],
            ),
        )

    @mcp.tool()
    def search_development_finance_transactions(
        namespace: str,
        funder: str | None = None,
        organisation: str | None = None,
        country: str | None = None,
        sector: str | None = None,
        publisher: str | None = None,
        iati_identifier: str | None = None,
        transaction_type: str | None = None,
        as_of: str | None = None,
    ) -> dict:
        """Transactions per publisher with within-publisher totals by type and currency (never across publishers)."""
        request = {
            "funder": funder,
            "organisation": organisation,
            "country": country,
            "sector": sector,
            "publisher": publisher,
            "iati_identifier": iati_identifier,
            "transaction_type": transaction_type,
            "as_of": as_of,
        }
        return gated(
            namespace,
            "search_development_finance_transactions",
            lambda conn: _queries(conn).search_transactions(
                namespace, request, principal_id=who()[0], scopes=who()[1]
            ),
        )

    @mcp.tool()
    def development_finance_publisher_coverage(
        namespace: str, as_of: str | None = None
    ) -> dict:
        """What each publisher's selections requested and returned, where reading stopped, and staleness."""
        return gated(
            namespace,
            "development_finance_publisher_coverage",
            lambda conn: _queries(conn).publisher_coverage(
                namespace, as_of=as_of, principal_id=who()[0], scopes=who()[1]
            ),
        )

    @mcp.tool()
    def lookup_crs_aggregates(
        namespace: str,
        donor: str | None = None,
        recipient: str | None = None,
        sector: str | None = None,
        price_basis: str | None = None,
        as_of: str | None = None,
    ) -> dict:
        """OECD CRS cells with the vintage in force as of a date; statistics, never activities."""
        request = {
            k: v
            for k, v in {
                "donor": donor,
                "recipient": recipient,
                "sector": sector,
                "price_basis": price_basis,
                "as_of": as_of,
            }.items()
            if v is not None
        }
        return gated(
            namespace,
            "lookup_crs_aggregates",
            lambda conn: _queries(conn).crs_aggregates(
                namespace, request, principal_id=who()[0], scopes=who()[1]
            ),
        )

    @mcp.tool()
    def list_world_bank_project_links(
        namespace: str,
        project_id: str | None = None,
        iati_identifier: str | None = None,
        as_of: str | None = None,
    ) -> dict:
        """World Bank projects beside IATI activities: linked by a stated identifier, or a marked candidate."""

        def run(conn):
            from src.kb.development_finance import DevelopmentFinanceStore

            DevelopmentFinanceStore(conn, initialize=False).require_ready()
            return _queries(conn).world_bank_links(
                namespace,
                as_of=as_of,
                scopes=who()[1],
                project_id=project_id,
                iati_identifier=iati_identifier,
            )

        return gated(namespace, "list_world_bank_project_links", run)

    def _normaliser(conn):
        from src.kb.development_finance_normalise import DevelopmentFinanceNormaliser

        return DevelopmentFinanceNormaliser(conn)

    @mcp.tool()
    def publish_development_finance_codelists(namespace: str) -> dict:
        """Publish the bundled DAC and IATI code-list subsets as ontology modules, versioned by release."""
        return gated(
            namespace,
            "publish_development_finance_codelists",
            lambda conn: _normaliser(conn).publish_codelists(
                principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
        )

    @mcp.tool()
    def normalise_development_finance_activities(namespace: str) -> dict:
        """Side records of normalised codes and allocation checks for each publisher's current revisions."""
        return gated(
            namespace,
            "normalise_development_finance_activities",
            lambda conn: {
                "normalisations": _normaliser(conn).normalise_current(
                    namespace, scopes=who()[1]
                )
            },
            write=True,
        )

    @mcp.tool()
    def resolve_development_finance_places(
        namespace: str, geo_namespace: str = "global"
    ) -> dict:
        """Resolve recipient countries by ISO code; regions stay aggregates; locations wait for review."""
        return gated(
            namespace,
            "resolve_development_finance_places",
            lambda conn: _normaliser(conn).resolve_places(
                namespace,
                principal_id=who()[0],
                scopes=who()[1],
                geo_namespace=geo_namespace,
            ),
            write=True,
        )

    @mcp.tool()
    def review_development_finance_place(
        namespace: str,
        resolution_id: str,
        decision: str,
        reason: str,
        selected_place_id: str | None = None,
    ) -> dict:
        """Accept, reject or defer a saved place resolution through the Geospatial owner."""
        return gated(
            namespace,
            "review_development_finance_place",
            lambda conn: _normaliser(conn).review_place(
                namespace,
                resolution_id,
                decision,
                selected_place_id=selected_place_id,
                reason=reason,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
        )

    @mcp.tool()
    def convert_development_finance_amount(
        namespace: str,
        transaction_id: str,
        target_currency: str,
        rate: str,
        rate_date: str,
        rate_source: str,
        citation: str,
    ) -> dict:
        """Exact conversion with a cited rate and date, stored beside the unchanged original."""
        return gated(
            namespace,
            "convert_development_finance_amount",
            lambda conn: _normaliser(conn).convert(
                namespace,
                transaction_id,
                target_currency=target_currency,
                rate=rate,
                rate_date=rate_date,
                rate_source=rate_source,
                citation=citation,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
        )

    def _identity(conn, initialize=True):
        from src.kb.development_finance_identity import DevelopmentFinanceIdentity

        return DevelopmentFinanceIdentity(conn, initialize=initialize)

    @mcp.tool()
    def propose_development_finance_identity_matches(
        namespace: str,
        ownership_namespace: str | None = None,
        funding_namespace: str | None = None,
    ) -> dict:
        """Offer reported organisations to ownership, publisher, canonical and funder records; nothing is linked.
        Conditional scope: funding_namespace needs knowledge:funding:read."""
        return gated(
            namespace,
            "propose_development_finance_identity_matches",
            lambda conn: _identity(conn).propose(
                namespace,
                principal_id=who()[0],
                scopes=who()[1],
                ownership_namespace=ownership_namespace,
                funding_namespace=funding_namespace,
            ),
            write=True,
        )

    @mcp.tool()
    def list_development_finance_identity_candidates(
        namespace: str, record_key: str | None = None
    ) -> dict:
        """Identity candidates of reported organisations with their review state; or one subject's identity."""

        def run(conn):
            identity = _identity(conn, initialize=False)
            if record_key:
                return identity.identity(namespace, record_key, scopes=who()[1])
            return {"candidates": identity.candidates(namespace, scopes=who()[1])}

        return gated(namespace, "list_development_finance_identity_candidates", run)

    @mcp.tool()
    def review_development_finance_identity_match(
        namespace: str, candidate_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject a candidate as an entity identity decision (a similar name alone is never accepted)."""

        def run(conn):
            identity = _identity(conn)
            if not any(
                c["candidate_id"] == candidate_id
                for c in identity.candidates(namespace, scopes={"operator"})
            ):
                from src.kb.development_finance import DevelopmentFinanceError

                raise DevelopmentFinanceError(
                    "not_found", "not a development-finance identity candidate"
                )
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

        return gated(
            namespace, "review_development_finance_identity_match", run, write=True
        )

    @mcp.tool()
    def revert_development_finance_identity_match(
        namespace: str, candidate_id: str, reason: str
    ) -> dict:
        """Undo a reviewed decision; the organisation returns to its reported string."""

        def run(conn):
            identity = _identity(conn)
            if not any(
                c["candidate_id"] == candidate_id
                for c in identity.candidates(namespace, scopes={"operator"})
            ):
                from src.kb.development_finance import DevelopmentFinanceError

                raise DevelopmentFinanceError(
                    "not_found", "not a development-finance identity candidate"
                )
            return identity.view(
                identity.service.revert(
                    namespace,
                    candidate_id,
                    reason,
                    principal_id=who()[0],
                    scopes=who()[1],
                )
            )

        return gated(
            namespace, "revert_development_finance_identity_match", run, write=True
        )

    @mcp.tool()
    def development_finance_open_calls(
        namespace: str, record_key: str, funding_namespace: str
    ) -> dict:
        """Open calls of the funders a reported organisation is matched to: a cross-reference, never an activity."""
        return gated(
            namespace,
            "development_finance_open_calls",
            lambda conn: {
                "calls": _identity(conn, initialize=False).open_calls(
                    namespace,
                    record_key,
                    funding_namespace=funding_namespace,
                    scopes=who()[1],
                )
            },
        )

    @mcp.tool()
    def record_development_finance_answer(
        namespace: str, kind: str, request: dict
    ) -> dict:
        """Answer again and store the receipt (query, as-of date, generation, coverage) for projects and reports.
        kind: activities, transactions, activity, coverage or crs."""
        return gated(
            namespace,
            "record_development_finance_answer",
            lambda conn: _queries(conn, record=True).record_answer(
                namespace, kind, request, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
        )

    @mcp.tool()
    def attach_development_finance_answer(
        namespace: str, answer_id: str, project_id: str, expected_revision: int
    ) -> dict:
        """Pin a stored answer (query, as-of date, coverage) in a research project."""
        return gated(
            namespace,
            "attach_development_finance_answer",
            lambda conn: _queries(conn).attach_to_project(
                namespace,
                answer_id,
                project_id,
                expected_revision,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
        )

    @mcp.tool()
    def development_finance_report_citation(namespace: str, answer_id: str) -> dict:
        """The bibliography entry and dependency an authored report uses to cite a stored answer."""
        return gated(
            namespace,
            "development_finance_report_citation",
            lambda conn: _queries(conn).report_citation(
                namespace, answer_id, scopes=who()[1]
            ),
        )

    def _monitor(conn, initialize=True):
        from src.kb.development_finance_monitoring import DevelopmentFinanceMonitor

        return DevelopmentFinanceMonitor(conn, initialize=initialize)

    @mcp.tool()
    def create_development_finance_monitor(
        namespace: str, request_key: str, filters: dict, delivery: dict | None = None
    ) -> dict:
        """Watch publishers, funders, countries, sectors, organisations or CRS cells through a subscription."""
        return gated(
            namespace,
            "create_development_finance_monitor",
            lambda conn: _monitor(conn).create(
                namespace,
                request_key,
                filters=filters,
                principal_id=who()[0],
                scopes=who()[1],
                delivery=delivery,
            ),
            write=True,
        )

    @mcp.tool()
    def run_development_finance_monitor(
        namespace: str, subscription_id: str, watermark: int | None = None
    ) -> dict:
        """Evaluate a monitor at a committed watermark (default: the newest observation). Conditional scope:
        funder and organisation filters resolved through identity decisions need knowledge:ownership:read."""
        return gated(
            namespace,
            "run_development_finance_monitor",
            lambda conn: _monitor(conn).run(
                subscription_id, watermark, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
        )

    @mcp.tool()
    def poll_development_finance_monitor(
        namespace: str, subscription_id: str, cursor: str = ""
    ) -> dict:
        """Delivered monitor events through the subscription poll."""
        return gated(
            namespace,
            "poll_development_finance_monitor",
            lambda conn: _monitor(conn, initialize=False).poll(
                subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor
            ),
        )
