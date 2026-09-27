"""Political elections feature entry points: contest results, poll series, identity, places, forecasts, news, monitors.

Acquisition of official results runs through the shared source-pack tools
(pack ``official-political-records``: ``de-bundeswahlleiterin-btw-results``,
``de-berlin-agh-results``, ``gb-electoral-commission-ge-results``,
``us-mit-election-lab-countypres``); poll releases are imported per publisher.
Every answer cites the release or poll release behind each figure; preliminary
and certified results stay separate vintages; a poll is never a result. No
tool predicts seats or outcomes, aggregates polls into one number or reads news
framing as a cause.
"""

READ = "knowledge:political:elections:read"
WRITE = "knowledge:political:elections:write"
OWNERSHIP_READ = "knowledge:ownership:read"
GEO_READ = "knowledge:geospatial:read"
GEO_WRITE = "knowledge:geospatial:write"
GEO_CALCULATE = "knowledge:geospatial:calculate"
FORECAST_READ = "knowledge:forecasts:read"
FORECAST_WRITE = "knowledge:forecasts:write"
NEWS_READ = "knowledge:read"

ELECTION_WRITES = {
    "import_election_poll_release",
    "propose_election_identity_matches",
    "review_election_identity_match",
    "revert_election_identity_match",
    "record_election_assertion",
    "retract_election_assertion",
    "project_election_boundaries",
    "register_election_boundary_collection",
    "election_results_at_place",
    "link_election_constituency_place",
    "register_election_forecast",
    "refresh_election_news_links",
    "assert_election_news_link",
    "revert_election_news_link",
    "create_election_monitor",
    "run_election_monitor",
}
ELECTION_READS = {
    "election_source_contracts",
    "elections_readiness",
    "list_election_contests",
    "election_contest_results",
    "election_contest_dossier",
    "list_election_poll_series",
    "election_party_results",
    "list_election_identity_candidates",
    "list_election_boundary_vintages",
    "election_constituency_place",
    "election_news_evidence",
    "score_election_forecasts",
    "poll_election_monitor",
}
ELECTION_TOOLS = ELECTION_WRITES | ELECTION_READS
# Every scope each tool always reads or writes. Scopes needed only for an optional argument (the dossier's places,
# forecasts and news sections) are checked when that argument is passed and documented as conditional.
ELECTION_SCOPES = {
    "election_source_contracts": [],
    "elections_readiness": [READ],
    "list_election_contests": [READ],
    "election_contest_results": [READ],
    "election_contest_dossier": [READ, OWNERSHIP_READ],
    "list_election_poll_series": [READ],
    "election_party_results": [READ, OWNERSHIP_READ],
    "list_election_identity_candidates": [READ, OWNERSHIP_READ],
    "list_election_boundary_vintages": [READ],
    "election_constituency_place": [READ, GEO_READ],
    "election_news_evidence": [READ, NEWS_READ],
    "score_election_forecasts": [READ, FORECAST_READ],
    "poll_election_monitor": [READ, "knowledge:subscriptions:read"],
    "import_election_poll_release": [WRITE],
    "propose_election_identity_matches": [
        READ,
        OWNERSHIP_READ,
        "knowledge:ownership:write",
    ],
    "review_election_identity_match": ["knowledge:ownership:review"],
    "revert_election_identity_match": ["knowledge:ownership:review"],
    "record_election_assertion": [WRITE],
    "retract_election_assertion": [WRITE],
    "project_election_boundaries": [WRITE, GEO_WRITE],
    "register_election_boundary_collection": [WRITE, GEO_READ],
    "election_results_at_place": [READ, GEO_CALCULATE, GEO_READ],
    "link_election_constituency_place": [WRITE, GEO_WRITE],
    "register_election_forecast": [READ, FORECAST_WRITE],
    "refresh_election_news_links": [WRITE, NEWS_READ],
    "assert_election_news_link": [WRITE, NEWS_READ],
    "revert_election_news_link": [WRITE],
    "create_election_monitor": [READ, "knowledge:subscriptions:write"],
    "run_election_monitor": [READ, "knowledge:subscriptions:write"],
}


def required_scopes(tool_name, mutability):
    return ELECTION_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    @mcp.tool()
    def election_source_contracts() -> dict:
        """Per-provider access decisions, formats, identifiers, vintages, jurisdiction rules and terms."""
        from src.ingestion.election_sources import PROVIDER_CONTRACTS, REVIEW_BOUNDARY

        return {"contracts": PROVIDER_CONTRACTS, "review_boundary": REVIEW_BOUNDARY}

    @mcp.tool()
    def elections_readiness() -> dict:
        """Whether the Political elections feature is selected, and per-provider releases and access decisions."""
        from src.kb.elections import readiness

        return safe(lambda conn: readiness(conn), required_scope=READ)

    @mcp.tool()
    def list_election_contests(
        namespace: str,
        election_id: str | None = None,
        constituency_id: str | None = None,
        unit_scheme: str | None = None,
        unit_native_id: str | None = None,
    ) -> dict:
        """Contests (one election, reporting unit and ballot) with their cited jurisdiction rules."""
        from src.kb.elections import ElectionStore, authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            store = ElectionStore(conn, initialize=False)
            return {
                "elections": store.elections(namespace),
                "contests": store.contests(
                    namespace,
                    election_id=election_id,
                    constituency_id=constituency_id,
                    unit_scheme=unit_scheme,
                    unit_native_id=unit_native_id,
                ),
            }

        return safe(run, required_scope=READ)

    @mcp.tool()
    def election_contest_results(
        namespace: str, contest_id: str, as_of: str | None = None
    ) -> dict:
        """A contest's result vintage in force on a date (by the authority's publication date), every vintage and
        the figures that changed between them, each cited; preliminary and certified are never merged."""
        from src.kb.elections import ElectionStore, authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            return ElectionStore(conn, initialize=False).results(
                namespace, contest_id, as_of=as_of
            )

        return safe(run, required_scope=READ)

    @mcp.tool()
    def election_contest_dossier(
        namespace: str,
        contest_id: str,
        as_of: str | None = None,
        include_places: bool = False,
        forecast_namespace: str | None = None,
        include_news: bool = False,
    ) -> dict:
        """A contest's cited dossier: result vintages and changes, candidates and lists with reviewable identity,
        poll series apart from results, boundary vintages, and unknowns. Conditional scope: include_places needs
        knowledge:geospatial:read, forecast_namespace knowledge:forecasts:read, include_news knowledge:read. No
        prediction, poll aggregate or causal claim."""
        from src.kb.elections_queries import contest_dossier

        return safe(
            lambda conn: contest_dossier(
                conn,
                namespace,
                contest_id,
                principal_id=who()[0],
                scopes=who()[1],
                as_of=as_of,
                include_places=include_places,
                forecast_namespace=forecast_namespace,
                include_news=include_news,
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def list_election_poll_series(
        namespace: str,
        election_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        """Poll series per publisher, question and option with every reading's fieldwork, sample and source;
        never averaged and never a result."""
        from src.kb.elections_polls import ElectionPolls

        return safe(
            lambda conn: {
                "series": ElectionPolls(conn, initialize=False).series(
                    namespace,
                    scopes=who()[1],
                    election_id=election_id,
                    date_from=date_from,
                    date_to=date_to,
                )
            },
            required_scope=READ,
        )

    @mcp.tool()
    def import_election_poll_release(
        namespace: str,
        publisher: str,
        election_id: str,
        source_url: str,
        csv_text: str,
        redistribution: str,
        terms_url: str | None = None,
    ) -> dict:
        """Import one publisher's poll release through the polls connector; figures are kept only when the
        publisher's terms allow redistribution, otherwise link-only metadata."""
        from src.kb.elections_polls import ElectionPolls

        return safe(
            lambda conn: ElectionPolls(conn).import_release(
                namespace,
                publisher=publisher,
                election_id=election_id,
                source_url=source_url,
                csv_text=csv_text,
                redistribution=redistribution,
                terms_url=terms_url,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def election_party_results(
        namespace: str, record_key: str, as_of: str | None = None
    ) -> dict:
        """A party list's results across the elections its accepted identity decisions join, per contest as
        published, with its dated succession assertions (conflicts side by side)."""
        from src.kb.elections_identity import ElectionIdentity

        return safe(
            lambda conn: ElectionIdentity(conn, initialize=False).party_results(
                namespace, record_key, scopes=who()[1], as_of=as_of
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def list_election_identity_candidates(
        namespace: str, record_key: str | None = None
    ) -> dict:
        """Identity candidates and decisions involving election records, with basis, evidence and reviewer."""
        from src.kb.elections import authorize
        from src.kb.elections_identity import ElectionIdentity

        def run(conn):
            authorize(namespace, who()[1], READ)
            return {
                "candidates": ElectionIdentity(conn, initialize=False).candidates(
                    namespace, scopes=who()[1], record_key=record_key
                )
            }

        return safe(run, required_scope=READ)

    @mcp.tool()
    def propose_election_identity_matches(
        namespace: str, political_jurisdictions: dict | None = None
    ) -> dict:
        """Propose party, candidate, poll-option and constituency candidates within one country; nothing merges."""
        from src.kb.elections_identity import ElectionIdentity

        return safe(
            lambda conn: ElectionIdentity(conn).propose(
                namespace,
                principal_id=who()[0],
                scopes=who()[1],
                political_jurisdictions=political_jurisdictions,
            ),
            write=True,
            required_scope="knowledge:ownership:write",
        )

    @mcp.tool()
    def review_election_identity_match(
        namespace: str, candidate_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject a candidate as an entity identity decision; records are linked, never rewritten."""
        from src.kb.elections_identity import ElectionIdentity

        def run(conn):
            identity = ElectionIdentity(conn)
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

        return safe(run, write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def revert_election_identity_match(
        namespace: str, candidate_id: str, reason: str
    ) -> dict:
        """Undo an accepted or rejected decision; answers return to the unmatched source strings."""
        from src.kb.elections_identity import ElectionIdentity

        def run(conn):
            identity = ElectionIdentity(conn)
            return identity.view(
                identity.service.revert(
                    namespace,
                    candidate_id,
                    reason,
                    principal_id=who()[0],
                    scopes=who()[1],
                )
            )

        return safe(run, write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def record_election_assertion(
        namespace: str,
        kind: str,
        subject_key: str,
        object_key: str,
        valid_on: str,
        source: dict,
        reason: str | None = None,
    ) -> dict:
        """A dated, cited successor/merger/renaming or redistricting assertion; kept beside conflicting ones."""
        from src.kb.elections_identity import ElectionIdentity

        return safe(
            lambda conn: ElectionIdentity(conn).assert_relation(
                namespace,
                kind=kind,
                subject_key=subject_key,
                object_key=object_key,
                valid_on=valid_on,
                source=source,
                reason=reason,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def retract_election_assertion(
        namespace: str, assertion_id: str, reason: str
    ) -> dict:
        """Retract an assertion as a new revision."""
        from src.kb.elections_identity import ElectionIdentity

        return safe(
            lambda conn: ElectionIdentity(conn).retract(
                namespace, assertion_id, reason, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def project_election_boundaries(
        namespace: str,
        scheme: str,
        boundary_vintage: str,
        feature_collection: dict,
        id_property: str,
        valid_from: str,
        valid_to: str | None = None,
        source_crs: str | None = None,
        title_property: str | None = None,
        feature_namespace: str = "global",
    ) -> dict:
        """Project one constituency boundary vintage through the Geospatial projector as its own collection."""
        from src.kb.elections_geo import ElectionGeography

        return safe(
            lambda conn: ElectionGeography(conn).project_boundaries(
                namespace,
                scheme=scheme,
                boundary_vintage=boundary_vintage,
                feature_collection=feature_collection,
                id_property=id_property,
                valid_from=valid_from,
                valid_to=valid_to,
                source_crs=source_crs,
                title_property=title_property,
                feature_namespace=feature_namespace,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def register_election_boundary_collection(
        namespace: str,
        scheme: str,
        boundary_vintage: str,
        collection: str,
        provider: str,
        valid_from: str,
        valid_to: str | None = None,
        feature_namespace: str = "global",
    ) -> dict:
        """Use a boundary collection the Geospatial store already holds for a vintage; nothing is copied."""
        from src.kb.elections_geo import ElectionGeography

        return safe(
            lambda conn: ElectionGeography(conn).register_collection(
                namespace,
                scheme=scheme,
                boundary_vintage=boundary_vintage,
                collection=collection,
                provider=provider,
                valid_from=valid_from,
                valid_to=valid_to,
                feature_namespace=feature_namespace,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def list_election_boundary_vintages(
        namespace: str, scheme: str | None = None
    ) -> dict:
        """Boundary vintages per unit scheme with their validity windows and Geospatial collections."""
        from src.kb.elections_geo import ElectionGeography

        return safe(
            lambda conn: {
                "vintages": ElectionGeography(conn, initialize=False).vintages(
                    namespace, scopes=who()[1], scheme=scheme
                )
            },
            required_scope=READ,
        )

    @mcp.tool()
    def election_results_at_place(
        namespace: str, scheme: str, point: list[float], as_of: str
    ) -> dict:
        """Which constituency contained a point ([lon, lat]) on a date, from the boundary vintage valid then, and
        its result vintages in force; boundary and result evidence are cited separately (records a receipt)."""
        from src.kb.elections_geo import ElectionGeography

        return safe(
            lambda conn: ElectionGeography(conn, initialize=False).results_at_point(
                namespace,
                scheme=scheme,
                point=point,
                as_of=as_of,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=READ,
        )

    @mcp.tool()
    def link_election_constituency_place(
        namespace: str, constituency_id: str, geo_namespace: str
    ) -> dict:
        """Record a reviewable place resolution for a constituency (review with review_geospatial_resolution)."""
        from src.kb.elections_geo import ElectionGeography

        return safe(
            lambda conn: ElectionGeography(conn).link_place(
                namespace,
                constituency_id,
                geo_namespace=geo_namespace,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def election_constituency_place(namespace: str, constituency_id: str) -> dict:
        """A constituency's place link: accepted by review, otherwise unresolved."""
        from src.kb.elections import require_scope
        from src.kb.elections_geo import ElectionGeography

        def run(conn):
            require_scope(who()[1], GEO_READ)
            return ElectionGeography(conn, initialize=False).place(
                namespace, constituency_id, scopes=who()[1]
            )

        return safe(run, required_scope=READ)

    @mcp.tool()
    def register_election_forecast(
        namespace: str,
        forecast_namespace: str,
        request_key: str,
        contest_id: str,
        rule: dict,
        probability: float,
        resolution_at_ms: int,
        evidence: list[dict],
        question: str | None = None,
    ) -> dict:
        """Register your forecast in the binary forecast ledger with a rule resolved only on the certified vintage;
        the probability is yours - the pack makes none."""
        from src.kb.elections_forecasts import ElectionForecasts

        return safe(
            lambda conn: ElectionForecasts(conn).register(
                namespace,
                forecast_namespace,
                request_key,
                contest_id=contest_id,
                rule=rule,
                probability=probability,
                resolution_at_ms=resolution_at_ms,
                evidence=evidence,
                question=question,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=FORECAST_WRITE,
        )

    @mcp.tool()
    def score_election_forecasts(
        forecast_namespace: str, forecast_ids: list[str]
    ) -> dict:
        """score_binary_forecasts with the cutoff taken from the certified publication time; refused before it."""
        from src.kb.elections_forecasts import ElectionForecasts

        return safe(
            lambda conn: ElectionForecasts(conn, initialize=False).score(
                forecast_namespace, forecast_ids, principal_id=who()[0], scopes=who()[1]
            ),
            required_scope=FORECAST_READ,
        )

    @mcp.tool()
    def refresh_election_news_links(
        namespace: str,
        contest_id: str,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict:
        """Link articles by explicit mentions of a contest's published parties or candidates; shared words only
        yield keyword candidates. No causal reading."""
        from src.kb.elections_news import ElectionNews

        return safe(
            lambda conn: ElectionNews(conn).refresh_links(
                namespace,
                contest_id,
                principal_id=who()[0],
                scopes=who()[1],
                date_from=date_from,
                date_to=date_to,
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def assert_election_news_link(
        namespace: str, contest_id: str, document_id: str, target_key: str, reason: str
    ) -> dict:
        """A reviewer's assertion that an article concerns the contest or one of its parties."""
        from src.kb.elections_news import ElectionNews

        return safe(
            lambda conn: ElectionNews(conn).assert_link(
                namespace,
                contest_id,
                document_id,
                target_key=target_key,
                reason=reason,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def revert_election_news_link(namespace: str, link_id: str, reason: str) -> dict:
        """Revert a news link or candidate as a new revision."""
        from src.kb.elections_news import ElectionNews

        return safe(
            lambda conn: ElectionNews(conn).revert(
                namespace, link_id, reason, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope=WRITE,
        )

    @mcp.tool()
    def election_news_evidence(
        namespace: str,
        contest_id: str,
        date_from: str | None = None,
        date_to: str | None = None,
        include_candidates: bool = True,
    ) -> dict:
        """Frames and sentiment of linked articles (date, outlet, frame label) beside the contest's results and
        polls for the same period, never merged; no correlation or causal effect is stated."""
        from src.kb.elections_news import ElectionNews

        return safe(
            lambda conn: ElectionNews(conn, initialize=False).evidence(
                namespace,
                contest_id,
                scopes=who()[1],
                date_from=date_from,
                date_to=date_to,
                include_candidates=include_candidates,
            ),
            required_scope=READ,
        )

    @mcp.tool()
    def create_election_monitor(
        namespace: str,
        request_key: str,
        watch: str,
        key: str,
        delivery: dict | None = None,
    ) -> dict:
        """A subscription on a contest, constituency, poll series or an election's polls; no new scheduler."""
        from src.kb.elections_monitoring import ElectionMonitor

        return safe(
            lambda conn: ElectionMonitor(conn).create(
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
    def run_election_monitor(
        subscription_id: str, watermark: int | None = None
    ) -> dict:
        """Evaluate a monitor at a committed watermark: new vintages (certified with changed figures, recounts,
        corrections) and new poll readings, each cited."""
        from src.kb.elections_monitoring import ElectionMonitor

        return safe(
            lambda conn: ElectionMonitor(conn).run(
                subscription_id, watermark, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
            required_scope="knowledge:subscriptions:write",
        )

    @mcp.tool()
    def poll_election_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll an elections monitor's events through the subscription delivery path."""
        from src.kb.elections_monitoring import ElectionMonitor

        return safe(
            lambda conn: ElectionMonitor(conn, initialize=False).poll(
                subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor
            ),
            required_scope="knowledge:subscriptions:read",
        )
