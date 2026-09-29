"""Economics demographics feature entry points: series, definitions, comparability, boundaries, links, monitors.

Acquisition runs through the shared source-pack tools (pack
``economic-statistics-and-filings``: ``eurostat-demography-migration``,
``unhcr-refugee-data-finder``, ``iom-dtm-idps``,
``destatis-genesis-population-migration``, ``statistik-bb-bezirke``); BAMF
figures from PDF-only reports are recorded from operator figure sheets. Every
answer cites each value's definition revision, unit, geography level and
source revision; series from different publishers, definitions or levels stay
separate. No tool projects a population, claims a cause of migration or a
policy effect, or merges, averages, nets or apportions values.
"""

READ = "knowledge:demographics:read"
WRITE = "knowledge:demographics:write"
REVIEW = "knowledge:demographics:review"
LEGAL_READ = "knowledge:legal:read"
DOSSIER_READ = "knowledge:political:dossier:read"
GEO_READ = "knowledge:geospatial:read"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"

DEMOGRAPHIC_WRITES = {
    "import_demographic_figure_sheet",
    "record_demographic_comparability",
    "review_demographic_comparability",
    "revert_demographic_comparability",
    "resolve_demographic_geographies",
    "review_demographic_resolution",
    "pin_demographic_query",
    "link_demographic_references",
    "link_demographic_dossier",
    "assert_demographic_link",
    "propose_demographic_link_candidates",
    "review_demographic_link",
    "revert_demographic_link",
    "create_demographic_monitor",
    "run_demographic_monitor",
}
DEMOGRAPHIC_READS = {
    "demographic_source_contracts",
    "demographic_readiness",
    "list_demographic_series",
    "inspect_demographic_series",
    "demographic_series_values",
    "compare_demographic_publishers",
    "convert_demographic_units",
    "list_demographic_comparability_notes",
    "query_demographic_boundary",
    "replay_demographic_query",
    "list_demographic_resolutions",
    "list_demographic_pins",
    "list_demographic_links",
    "demographic_series_citing",
    "poll_demographic_monitor",
}
DEMOGRAPHIC_TOOLS = DEMOGRAPHIC_WRITES | DEMOGRAPHIC_READS
# Every scope each tool always reads or writes. A scope needed only for one value of an argument (the target kind of
# an asserted link) is checked at call time and documented as conditional.
DEMOGRAPHIC_SCOPES = {
    "demographic_source_contracts": [],
    "demographic_readiness": [READ],
    "list_demographic_series": [READ],
    "inspect_demographic_series": [READ],
    "demographic_series_values": [READ],
    "compare_demographic_publishers": [READ],
    "convert_demographic_units": [READ],
    "list_demographic_comparability_notes": [READ],
    "query_demographic_boundary": [READ],
    "replay_demographic_query": [READ],
    "list_demographic_resolutions": [READ],
    "list_demographic_pins": [READ],
    "list_demographic_links": [READ],
    "demographic_series_citing": [READ],
    "poll_demographic_monitor": [READ, SUBSCRIPTIONS_READ],
    "import_demographic_figure_sheet": [WRITE],
    "record_demographic_comparability": [WRITE],
    "review_demographic_comparability": [REVIEW],
    "revert_demographic_comparability": [REVIEW],
    "resolve_demographic_geographies": [WRITE, GEO_READ],
    "review_demographic_resolution": [REVIEW],
    "pin_demographic_query": [WRITE],
    "link_demographic_references": [WRITE, LEGAL_READ],
    "link_demographic_dossier": [WRITE, DOSSIER_READ],
    "assert_demographic_link": [WRITE],
    "propose_demographic_link_candidates": [WRITE, LEGAL_READ],
    "review_demographic_link": [REVIEW],
    "revert_demographic_link": [REVIEW],
    "create_demographic_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_demographic_monitor": [READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return DEMOGRAPHIC_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    @mcp.tool()
    def demographic_source_contracts() -> dict:
        """Per-provider access decisions, definitions, geography levels, vintage semantics and terms."""
        from src.ingestion.demographic_sources import (
            PROVIDER_CONTRACTS,
            REVIEW_BOUNDARY,
        )

        return {"contracts": PROVIDER_CONTRACTS, "review_boundary": REVIEW_BOUNDARY}

    @mcp.tool()
    def demographic_readiness() -> dict:
        """Whether the Economics demographics feature is selected, and per-provider releases and access decisions."""
        from src.kb.demographics import readiness

        return safe(lambda conn: readiness(conn), required_scope=READ)

    @mcp.tool()
    def list_demographic_series(
        namespace: str,
        provider: str | None = None,
        concept: str | None = None,
        geography_code: str | None = None,
        scheme: str | None = None,
        level: str | None = None,
        series_code: str | None = None,
    ) -> dict:
        """Series with publisher, definition, geography level, unit, current vintage, notes and breaks."""
        from src.kb.demographics import DemographicStore, authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            return {
                "series": DemographicStore(conn, initialize=False).find_series(
                    namespace,
                    provider=provider,
                    concept=concept,
                    geography_code=geography_code,
                    scheme=scheme,
                    level=level,
                    series_code=series_code,
                )
            }

        return safe(run, required_scope=READ)

    @mcp.tool()
    def inspect_demographic_series(namespace: str, series_id: str) -> dict:
        """One series: every vintage (release and retrieval clocks, revision_of), its definition history, breaks,
        and the acts, decisions and dossiers it cites (citation only; no effect is asserted)."""
        from src.kb.demographics import DemographicStore, authorize
        from src.kb.demographics_links import DemographicLinks

        def run(conn):
            authorize(namespace, who()[1], READ)
            store = DemographicStore(conn, initialize=False)
            series = store.series(namespace, series_id)
            return {
                "series": series,
                "vintages": [
                    store.vintage(namespace, v["vintage_id"])
                    for v in store.vintage_rows(namespace, series_id)
                ],
                "definition_history": store.definition_history(
                    namespace, series["definition_key"]
                ),
                "links": DemographicLinks(conn, initialize=False).links(
                    namespace, scopes=who()[1], subject_id=series_id
                ),
            }

        return safe(run, required_scope=READ)

    @mcp.tool()
    def demographic_series_values(
        namespace: str,
        series_id: str,
        vintage_id: str | None = None,
        as_of_ms: int | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
    ) -> dict:
        """A series' values in one vintage (or the vintage released by as_of_ms) with definition, unit, level,
        flags, coverage notes and source revision; historical_vintage_unavailable when none was retained."""
        from src.kb.demographics import DemographicStore, authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            return DemographicStore(conn, initialize=False).values(
                namespace,
                series_id,
                vintage_id=vintage_id,
                as_of_ms=as_of_ms,
                period_from=period_from,
                period_to=period_to,
            )

        return safe(run, required_scope=READ)

    @mcp.tool()
    def compare_demographic_publishers(
        namespace: str,
        concept: str,
        geography_codes: list[str] | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
        as_of_ms: int | None = None,
    ) -> dict:
        """Each publisher's series for one concept, geography and period side by side with comparability notes;
        no merged, averaged or netted value; unnoted pairs are comparability_unknown."""
        from src.kb.demographics_comparability import DemographicComparability

        return safe(
            lambda conn: DemographicComparability(conn, initialize=False).side_by_side(
                namespace,
                concept,
                scopes=who()[1],
                geography_codes=geography_codes,
                period_from=period_from,
                period_to=period_to,
                as_of_ms=as_of_ms,
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def convert_demographic_units(
        namespace: str, series_id: str, target_unit: str, vintage_id: str | None = None
    ) -> dict:
        """A series' values in another count unit with a calculation receipt; never stored, never count <-> rate."""
        from src.kb.demographics_comparability import DemographicComparability

        return safe(
            lambda conn: DemographicComparability(conn, initialize=False).convert(
                namespace,
                series_id,
                target_unit,
                scopes=who()[1],
                vintage_id=vintage_id,
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def list_demographic_comparability_notes(
        namespace: str, record_id: str | None = None
    ) -> dict:
        """Comparability notes with relation, cited definitions and source revisions, and review state."""
        from src.kb.demographics_comparability import DemographicComparability

        return safe(
            lambda conn: {
                "notes": DemographicComparability(conn, initialize=False).notes(
                    namespace, scopes=who()[1], record_id=record_id
                )
            },
            required_scope=READ,
        )

    @mcp.tool()
    def record_demographic_comparability(
        namespace: str, left: dict, right: dict, relation: str, statement: str
    ) -> dict:
        """Propose a comparability note between two series or two definition revisions (reviewable, reversible)."""
        from src.kb.demographics_comparability import DemographicComparability

        return safe(
            lambda conn: DemographicComparability(conn).record(
                namespace,
                left,
                right,
                relation,
                statement,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def review_demographic_comparability(
        namespace: str, note_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject a proposed comparability note with a reason."""
        from src.kb.demographics_comparability import DemographicComparability

        return safe(
            lambda conn: DemographicComparability(conn).review(
                namespace,
                note_id,
                decision,
                reason,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=REVIEW,
        )

    @mcp.tool()
    def revert_demographic_comparability(
        namespace: str, note_id: str, reason: str
    ) -> dict:
        """Withdraw a reviewed comparability note; no earlier note is reactivated."""
        from src.kb.demographics_comparability import DemographicComparability

        return safe(
            lambda conn: DemographicComparability(conn).revert(
                namespace, note_id, reason, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope=REVIEW,
        )

    @mcp.tool()
    def import_demographic_figure_sheet(namespace: str, sheet: dict) -> dict:
        """Record an operator's figure sheet for a PDF-only publication (BAMF): report URL, publication date,
        definitions quoting the report and figures with page and table locators; never scraped."""
        from src.kb.demographics import DemographicStore

        return safe(
            lambda conn: DemographicStore(conn).import_sheet(
                namespace, sheet, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def resolve_demographic_geographies(
        namespace: str, geo_namespace: str = "global", collections: dict | None = None
    ) -> dict:
        """Resolve series geography codes to Geospatial boundary features and places by the published code only."""
        from src.kb.demographics_places import DemographicPlaces

        return safe(
            lambda conn: DemographicPlaces(conn).resolve_geographies(
                namespace,
                principal_id=who()[0],
                scopes=who()[1],
                geo_namespace=geo_namespace,
                collections=collections,
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def list_demographic_resolutions(namespace: str) -> dict:
        """Current code-to-boundary resolutions with code-list version, boundary vintage and review state."""
        from src.kb.demographics_places import DemographicPlaces

        return safe(
            lambda conn: {
                "resolutions": DemographicPlaces(conn, initialize=False).resolutions(
                    namespace, scopes=who()[1]
                )
            },
            required_scope=READ,
        )

    @mcp.tool()
    def review_demographic_resolution(
        namespace: str, resolution_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject a code-to-boundary resolution; a rejected resolution is not used."""
        from src.kb.demographics_places import DemographicPlaces

        return safe(
            lambda conn: DemographicPlaces(conn).review(
                namespace,
                resolution_id,
                decision,
                reason,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=REVIEW,
        )

    @mcp.tool()
    def query_demographic_boundary(
        namespace: str,
        feature_id: str,
        period_from: str | None = None,
        period_to: str | None = None,
        as_of_ms: int | None = None,
        concept: str | None = None,
    ) -> dict:
        """Series resolved to one boundary with values as of a release date, other levels listed (never aggregated
        or apportioned), comparability pairs and a replayable receipt."""
        from src.kb.demographics_places import DemographicPlaces

        return safe(
            lambda conn: DemographicPlaces(conn, initialize=False).boundary_series(
                namespace,
                feature_id,
                scopes=who()[1],
                period_from=period_from,
                period_to=period_to,
                as_of_ms=as_of_ms,
                concept=concept,
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def replay_demographic_query(receipt: dict) -> dict:
        """Re-run a boundary query receipt: reproduced when the same vintages are selected, else changed."""
        from src.kb.demographics_places import DemographicPlaces

        return safe(
            lambda conn: DemographicPlaces(conn, initialize=False).replay(
                receipt, scopes=who()[1]
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def pin_demographic_query(namespace: str, receipt: dict) -> dict:
        """Pin the vintages a boundary query used; a later revision marks the pin stale, never replaces it."""
        from src.kb.demographics_places import DemographicPlaces

        return safe(
            lambda conn: DemographicPlaces(conn).pin(
                namespace, receipt, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def list_demographic_pins(
        namespace: str, receipt_digest: str | None = None
    ) -> dict:
        """Pinned vintages with their own values and current or stale status."""
        from src.kb.demographics_places import DemographicPlaces

        return safe(
            lambda conn: DemographicPlaces(conn, initialize=False).pins(
                namespace, scopes=who()[1], receipt_digest=receipt_digest
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def link_demographic_references(namespace: str, legal_namespace: str) -> dict:
        """Link series to acts and court decisions by the publisher's stated CELEX, ELI, ECLI or BGBl reference in
        the issuing jurisdiction; unmatched references stay unresolved."""
        from src.kb.demographics_links import DemographicLinks

        return safe(
            lambda conn: DemographicLinks(conn).link_references(
                namespace,
                legal_namespace=legal_namespace,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def link_demographic_dossier(
        namespace: str, dossier_namespace: str, dossier_id: str
    ) -> dict:
        """Link series to a legislative dossier by a stated printed paper or EU procedure reference."""
        from src.kb.demographics_links import DemographicLinks

        return safe(
            lambda conn: DemographicLinks(conn).link_dossier(
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
    def assert_demographic_link(
        namespace: str,
        subject: dict,
        target: dict,
        relation: str,
        locator: str,
        statement: str,
    ) -> dict:
        """Assert a citation link with a passage locator; it counts only once another reviewer accepts it.
        Conditional scope: a legal-work target needs knowledge:legal:read, a dossier target
        knowledge:political:dossier:read."""
        from src.kb.demographics_links import DemographicLinks

        return safe(
            lambda conn: DemographicLinks(conn).assert_link(
                namespace,
                subject=subject,
                target=target,
                relation=relation,
                locator=locator,
                statement=statement,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def propose_demographic_link_candidates(
        namespace: str, legal_namespace: str
    ) -> dict:
        """Shared words between definitions and act titles as discovery candidates only, never links."""
        from src.kb.demographics_links import DemographicLinks

        return safe(
            lambda conn: DemographicLinks(conn).propose_candidates(
                namespace,
                legal_namespace=legal_namespace,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def review_demographic_link(
        namespace: str, link_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject an asserted link or a discovery candidate with a reason."""
        from src.kb.demographics_links import DemographicLinks

        return safe(
            lambda conn: DemographicLinks(conn).review(
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
    def revert_demographic_link(namespace: str, link_id: str, reason: str) -> dict:
        """Undo a reviewed link decision."""
        from src.kb.demographics_links import DemographicLinks

        return safe(
            lambda conn: DemographicLinks(conn).revert(
                namespace, link_id, reason, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope=REVIEW,
        )

    @mcp.tool()
    def list_demographic_links(
        namespace: str, subject_id: str | None = None, target_id: str | None = None
    ) -> dict:
        """Citation links, assertions and candidates with basis, relation, source revision and review state."""
        from src.kb.demographics_links import DemographicLinks

        return safe(
            lambda conn: {
                "links": DemographicLinks(conn, initialize=False).links(
                    namespace,
                    scopes=who()[1],
                    subject_id=subject_id,
                    target_id=target_id,
                )
            },
            required_scope=READ,
        )

    @mcp.tool()
    def demographic_series_citing(namespace: str, target_id: str) -> dict:
        """The series citing a legal work, court decision or dossier; the link asserts no effect."""
        from src.kb.demographics_links import DemographicLinks

        return safe(
            lambda conn: DemographicLinks(conn, initialize=False).series_citing(
                namespace, target_id, scopes=who()[1]
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def create_demographic_monitor(
        namespace: str,
        request_key: str,
        series_filter: dict,
        delivery: dict | None = None,
    ) -> dict:
        """A subscription on series matching a publisher, dataset, geography or definition filter; no new scheduler."""
        from src.kb.demographics_monitoring import DemographicMonitor

        return safe(
            lambda conn: DemographicMonitor(conn).create(
                namespace,
                request_key,
                series_filter=series_filter,
                principal_id=who()[0],
                scopes=who()[1],
                delivery=delivery,
            ),
            write=True,
            required_scope=SUBSCRIPTIONS_WRITE,
        )

    @mcp.tool()
    def run_demographic_monitor(
        subscription_id: str, watermark: int | None = None
    ) -> dict:
        """Evaluate a monitor at a committed watermark: new releases, vintage and definition revisions and breaks,
        plus overdue calendar releases (release_pending) and stale pins."""
        from src.kb.demographics_monitoring import DemographicMonitor

        return safe(
            lambda conn: DemographicMonitor(conn).run(
                subscription_id, watermark, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope=SUBSCRIPTIONS_WRITE,
        )

    @mcp.tool()
    def poll_demographic_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a demographic monitor's events through the subscription delivery path."""
        from src.kb.demographics_monitoring import DemographicMonitor

        return safe(
            lambda conn: DemographicMonitor(conn, initialize=False).poll(
                subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor
            ),
            required_scope=SUBSCRIPTIONS_READ,
        )
