"""Legal courts and justice-statistics entry points (#2218): dockets, decisions and statistics as of a date.

Registered through :mod:`tools.knowledge_engine_mcp.legal` (the Legal pack's
tools); acquisition runs through the shared source-pack tools (pack
``legal-research`` 1.4.0: ``courtlistener-dockets``, ``courtlistener-opinions``,
``fbi-cde-summarized``, ``police-uk-street-crime``, ``eurostat-crime-iccs``).
Court coverage is the optional ``courts`` feature and statistics the optional
``justice-statistics`` feature.

Exclusions: no personal profiles of private individuals (natural persons are
pseudonymised and never a query key or monitor target), no recidivism or risk
scoring, no neighbourhood safety ratings, no rankings of places, no derived
win/loss labels or outcome predictions, and no legal advice.
"""

COURTS_JUSTICE_WRITES = {
    "link_court_citations",
    "propose_court_party_matches",
    "review_court_party_match",
    "revert_court_party_match",
    "resolve_justice_places",
    "review_justice_place",
    "record_justice_comparability",
    "review_justice_comparability",
    "create_courts_justice_monitor",
    "run_courts_justice_monitor",
}
COURTS_JUSTICE_READS = {
    "courts_justice_source_contracts",
    "courts_justice_readiness",
    "lookup_dockets",
    "dockets_citing_provision",
    "docket_as_of",
    "justice_statistics_for_place",
    "compare_justice_statistics",
    "list_court_citation_links",
    "list_court_identity_candidates",
    "list_justice_place_resolutions",
    "poll_courts_justice_monitor",
}
COURTS_JUSTICE_TOOLS = COURTS_JUSTICE_WRITES | COURTS_JUSTICE_READS
READ = "knowledge:legal:read"
WRITE = "knowledge:legal:write"
REVIEW = "knowledge:legal:review"
OWNERSHIP_READ = "knowledge:ownership:read"
# Every scope each tool always reads or writes.
COURTS_JUSTICE_SCOPES = {
    "courts_justice_source_contracts": [],
    "courts_justice_readiness": [READ],
    "lookup_dockets": [READ, OWNERSHIP_READ],
    "dockets_citing_provision": [READ],
    "docket_as_of": [READ],
    "justice_statistics_for_place": [READ],
    "compare_justice_statistics": [READ],
    "list_court_citation_links": [READ],
    "list_court_identity_candidates": [READ, OWNERSHIP_READ],
    "list_justice_place_resolutions": [READ],
    "poll_courts_justice_monitor": [READ, "knowledge:subscriptions:read"],
    "link_court_citations": [READ, WRITE],
    "propose_court_party_matches": [READ, OWNERSHIP_READ, "knowledge:ownership:write"],
    "review_court_party_match": [OWNERSHIP_READ, "knowledge:ownership:review"],
    "revert_court_party_match": [OWNERSHIP_READ, "knowledge:ownership:review"],
    "resolve_justice_places": [READ, WRITE, "knowledge:geospatial:read"],
    "review_justice_place": [READ, REVIEW],
    "record_justice_comparability": [READ, WRITE],
    "review_justice_comparability": [READ, REVIEW],
    "create_courts_justice_monitor": [READ, "knowledge:subscriptions:write"],
    "run_courts_justice_monitor": [READ, "knowledge:subscriptions:write"],
}


def required_scopes(tool_name, mutability):
    return COURTS_JUSTICE_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def dockets(conn, write=False):
        from src.kb.legal_dockets import LegalDocketStore

        return LegalDocketStore(conn, initialize=write)

    def statistics(conn, write=False):
        from src.kb.justice_statistics import JusticeStatisticsStore

        return JusticeStatisticsStore(conn, initialize=write)

    def identity(conn, write=False):
        from src.kb.courts_justice_identity import CourtsIdentity

        return CourtsIdentity(conn, initialize=write)

    def places(conn, write=False):
        from src.kb.courts_justice_identity import JusticePlaces

        return JusticePlaces(conn, initialize=write)

    def monitor(conn, write=False):
        from src.kb.courts_justice_monitoring import CourtsJusticeMonitor

        return CourtsJusticeMonitor(conn, initialize=write)

    @mcp.tool()
    def courts_justice_source_contracts() -> dict:
        """Per-provider access, key handling, licences, rate limits, revision models, the party minimisation
        decision, bounded coverage and LIVE_VERIFICATION for CourtListener, FBI CDE, data.police.uk and Eurostat."""
        from src.ingestion.courts_justice_sources import (
            BOUNDED_COVERAGE,
            LIVE_VERIFICATION,
            MINIMISATION,
            PROVIDER_CONTRACTS,
            REVIEW_BOUNDARY,
        )

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "minimisation": MINIMISATION, "bounded_coverage": BOUNDED_COVERAGE, "review_boundary": REVIEW_BOUNDARY}

    @mcp.tool()
    def courts_justice_readiness() -> dict:
        """Whether the courts / justice-statistics features are selected and what each provider has acquired."""
        from src.kb.courts_justice import readiness

        return safe(lambda conn: readiness(conn), required_scope=READ)

    @mcp.tool()
    def lookup_dockets(namespace: str, court_id: str | None = None, entity_id: str | None = None,
                       provision: str | None = None, as_of: str | None = None) -> dict:
        """Dockets and decisions on record at a date for a court (CourtListener court id), an organisational party
        (canonical entity reached by an accepted identity match only) or a provision (exact citation), with docket
        entries verbatim and dispositions as quoted text with locators; 'no_docket_on_record' is explicit. Natural
        persons are never a query key. No win/loss labels, outcome predictions or legal advice."""
        def run(conn):
            store = dockets(conn)
            given = [v for v in (court_id, entity_id, provision) if v]
            if len(given) != 1:
                from src.kb.courts_justice import CourtsJusticeError

                raise CourtsJusticeError("invalid_request", "give exactly one of court_id, entity_id or provision")
            if court_id:
                return store.dockets_for_court(namespace, court_id, scopes=who()[1], as_of=as_of)
            if entity_id:
                return store.dockets_for_party(namespace, entity_id, scopes=who()[1], as_of=as_of)
            return store.dockets_for_provision(namespace, provision, scopes=who()[1], as_of=as_of)
        return safe(run, required_scope=READ)

    @mcp.tool()
    def dockets_citing_provision(namespace: str, provision: str, as_of: str | None = None) -> dict:
        """Dockets (citing entries) and decisions (citing passages) whose text cites a US Code or CFR provision,
        linked by exact citation parsing only, as of a date; quoted dispositions only, no legal advice."""
        return safe(lambda conn: dockets(conn).dockets_for_provision(namespace, provision, scopes=who()[1],
                                                                     as_of=as_of), required_scope=READ)

    @mcp.tool()
    def docket_as_of(namespace: str, record_key: str, as_of: str | None = None) -> dict:
        """One docket's revision current at a date: entries filed by then (verbatim, documents linked), parties
        minimised (organisations by name, natural persons pseudonymised) and the cited revision."""
        def run(conn):
            from src.kb.courts_justice import READ_SCOPE, authorize

            authorize(namespace, who()[1], READ_SCOPE)
            found = dockets(conn).docket_as_of(namespace, record_key, as_of)
            return {"status": "answered", "docket": found} if found else {"status": "no_docket_on_record"}
        return safe(run, required_scope=READ)

    @mcp.tool()
    def justice_statistics_for_place(namespace: str, place: str, as_of: str | None = None,
                                     indicator: str | None = None, period_from: str | None = None,
                                     period_to: str | None = None) -> dict:
        """Official crime and justice statistics for a place ('us-state:CA', 'fbi-ori:...', 'eurostat-geo:DE',
        'police-uk-neighbourhood:force/id' or a geospatial place_id) from each source side by side, with the
        definition, unit, flags, coverage notes and vintage in force at the date; gaps and suppressed values are
        explicit. No rankings, neighbourhood safety ratings or risk scores."""
        return safe(lambda conn: statistics(conn).statistics_for_place(
            namespace, place, scopes=who()[1], as_of=as_of, indicator=indicator, period_from=period_from,
            period_to=period_to), required_scope=READ)

    @mcp.tool()
    def compare_justice_statistics(namespace: str, places: list[str], indicator: str | None = None,
                                   as_of: str | None = None) -> dict:
        """Statistics of several jurisdictions, each series separately, with the comparability notes of every
        pair; the aligned view is refused unless every cross-jurisdiction pair has a stated comparability note.
        Nothing is ranked, merged or scored."""
        return safe(lambda conn: statistics(conn).compare_places(namespace, places, scopes=who()[1],
                                                                indicator=indicator, as_of=as_of),
                    required_scope=READ)

    @mcp.tool()
    def list_court_citation_links(namespace: str, status: str | None = None,
                                  citing_record_key: str | None = None) -> dict:
        """Exact citation links from docket entries and opinions to provisions and decisions, with unresolved
        citations kept as source text; the citing relationship is never characterised."""
        def run(conn):
            from src.kb.legal_court_citations import CourtCitations

            return CourtCitations(conn, initialize=False).list_links(namespace, scopes=who()[1], status=status,
                                                   citing_record_key=citing_record_key)
        return safe(run, required_scope=READ)

    @mcp.tool()
    def link_court_citations(namespace: str) -> dict:
        """Parse acquired docket entries and opinions for exact statute, regulation and reporter citations and
        link them to Legal works; idempotent."""
        def run(conn):
            from src.kb.legal_court_citations import CourtCitations

            return CourtCitations(conn).link(namespace, scopes=who()[1])
        return safe(run, write=True, required_scope=WRITE)

    @mcp.tool()
    def list_court_identity_candidates(namespace: str, party_key: str | None = None) -> dict:
        """Courts by CourtListener id and the reviewable identity candidates of organisational parties."""
        def run(conn):
            item = identity(conn)
            return {"courts": item.courts(namespace, scopes=who()[1]),
                    "candidates": item.candidates(namespace, scopes=who()[1], party_key=party_key)}
        return safe(run, required_scope=READ)

    @mcp.tool()
    def propose_court_party_matches(namespace: str, ownership_namespace: str | None = None) -> dict:
        """Propose organisational parties against ownership entities and canonical aliases (identifiers first;
        names are low evidence and never auto-accepted). Natural persons are never proposed."""
        return safe(lambda conn: identity(conn, True).propose(namespace, principal_id=who()[0], scopes=who()[1],
                                                        ownership_namespace=ownership_namespace),
                    write=True, required_scope="knowledge:ownership:write")

    @mcp.tool()
    def review_court_party_match(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept or reject an organisational party identity candidate with a reason (an entity identity
        decision; records are never merged)."""
        return safe(lambda conn: identity(conn, True).review(namespace, candidate_id, decision, reason,
                                                       principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def revert_court_party_match(namespace: str, candidate_id: str, reason: str) -> dict:
        """Revert a reviewed organisational party identity decision."""
        return safe(lambda conn: identity(conn, True).revert(namespace, candidate_id, reason, principal_id=who()[0],
                                                       scopes=who()[1]),
                    write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def resolve_justice_places(namespace: str, geo_namespace: str) -> dict:
        """Resolve statistic reporting areas (states, ORIs, police.uk neighbourhoods, GEO codes) to geospatial
        places by exact published code; ambiguous mappings stay review candidates."""
        return safe(lambda conn: places(conn, True).resolve(namespace, geo_namespace=geo_namespace, principal_id=who()[0],
                                                      scopes=who()[1]), write=True, required_scope=WRITE)

    @mcp.tool()
    def list_justice_place_resolutions(namespace: str) -> dict:
        """Place resolutions with their candidates, mapping source and review state."""
        return safe(lambda conn: {"resolutions": places(conn).resolutions(namespace, scopes=who()[1])},
                    required_scope=READ)

    @mcp.tool()
    def review_justice_place(namespace: str, resolution_id: str, decision: str, reason: str,
                             place_id: str | None = None) -> dict:
        """Accept (naming one candidate place) or reject a place resolution with a reason."""
        return safe(lambda conn: places(conn, True).review(namespace, resolution_id, decision, reason, principal_id=who()[0],
                                                     scopes=who()[1], place_id=place_id),
                    write=True, required_scope=REVIEW)

    @mcp.tool()
    def record_justice_comparability(namespace: str, left_series: str, right_series: str, relation: str,
                                     statement: str, cited: list[dict]) -> dict:
        """Propose a comparability note between two series (definition differences, reporting coverage,
        classification mappings or not comparable), citing the definitions or source texts it rests on."""
        return safe(lambda conn: statistics(conn, True).record_note(namespace, left_series, right_series, relation,
                                                              statement, cited, principal_id=who()[0],
                                                              scopes=who()[1]), write=True, required_scope=WRITE)

    @mcp.tool()
    def review_justice_comparability(namespace: str, note_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a comparability note (by someone other than its author) with a reason."""
        return safe(lambda conn: statistics(conn, True).review_note(namespace, note_id, decision, reason,
                                                              principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope=REVIEW)

    @mcp.tool()
    def create_courts_justice_monitor(namespace: str, request_key: str, watch: str, key: str,
                                      indicator: str | None = None) -> dict:
        """Subscribe to a docket, court, organisational party (entity id), provision or place series; natural
        persons cannot be monitored."""
        return safe(lambda conn: monitor(conn, True).create(namespace, request_key, watch=watch, key=key,
                                                      principal_id=who()[0], scopes=who()[1], indicator=indicator),
                    write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def run_courts_justice_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a monitor at a committed watermark: new entries, opinions, citing decisions, vintages and
        revised observations, each citing its before and after revision."""
        return safe(lambda conn: monitor(conn, True).run(subscription_id, watermark, principal_id=who()[0],
                                                   scopes=who()[1]),
                    write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def poll_courts_justice_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a courts-justice monitor's delivered events."""
        return safe(lambda conn: monitor(conn).poll(subscription_id, principal_id=who()[0], scopes=who()[1],
                                                    cursor=cursor), required_scope="knowledge:subscriptions:read")
