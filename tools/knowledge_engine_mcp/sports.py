"""Sports pack entry points: tables as of a date, histories, identity, links, forecasts and monitors (#2146).

Acquisition runs through the shared source-pack tools (pack ``sports-records``:
football-data.org, openfootball, StatsBomb open data and the Sackmann tennis
archives); Olympic result transcriptions and governing-body decisions are
recorded per publication with their citation. Every answer cites the revision
behind each figure. No tool returns odds, betting advice, tips or a
Noesis-produced prediction, and no tool returns medical, biometric or tracking
data. Before a source has run, every entry point answers ``not_ready``.
"""

READ = "knowledge:sports:read"
WRITE = "knowledge:sports:write"
REVIEW = "knowledge:sports:review"
OWNERSHIP_READ = "knowledge:ownership:read"
OWNERSHIP_WRITE = "knowledge:ownership:write"
OWNERSHIP_REVIEW = "knowledge:ownership:review"
GEO_WRITE = "knowledge:geospatial:write"
FORECAST_READ = "knowledge:forecasts:read"
FORECAST_WRITE = "knowledge:forecasts:write"
SUBS_READ = "knowledge:subscriptions:read"
SUBS_WRITE = "knowledge:subscriptions:write"
NEWS_READ = "knowledge:read"

# Every scope each tool always reads or writes; each tool checks exactly these at call time.
SPORTS_SCOPES = {
    "sports_source_contracts": [],
    "sports_readiness": [READ],
    "sports_standings_as_of": [READ],
    "sports_match_history": [READ, OWNERSHIP_READ],
    "sports_fixture_history": [READ],
    "sports_team_schedule": [READ, OWNERSHIP_READ],
    "sports_player_appearances": [READ, OWNERSHIP_READ],
    "sports_transfers": [READ, OWNERSHIP_READ],
    "sports_tennis_matches": [READ],
    "sports_olympic_event_results": [READ],
    "export_sports_records": [READ],
    "list_sports_identity_candidates": [READ, OWNERSHIP_READ],
    "sports_venue_place": [READ],
    "sports_news_links": [READ],
    "score_sports_forecasts": [READ, FORECAST_READ],
    "poll_sports_monitor": [READ, SUBS_READ],
    "propose_sports_identity_matches": [READ, OWNERSHIP_READ, OWNERSHIP_WRITE],
    "review_sports_identity_match": [OWNERSHIP_REVIEW],
    "revert_sports_identity_match": [OWNERSHIP_REVIEW],
    "record_sports_club_lineage": [REVIEW],
    "link_sports_venue_place": [WRITE, GEO_WRITE],
    "record_sports_decision": [WRITE],
    "record_sports_table_rule": [WRITE],
    "record_sports_transfer": [WRITE],
    "import_sports_olympic_results": [WRITE],
    "refresh_sports_news_links": [READ, WRITE, NEWS_READ],
    "review_sports_news_link": [REVIEW],
    "revert_sports_news_link": [REVIEW],
    "register_sports_forecast": [READ, FORECAST_WRITE],
    "create_sports_monitor": [READ, SUBS_WRITE],
    "run_sports_monitor": [READ, SUBS_WRITE],
}
SPORTS_WRITES = {
    "propose_sports_identity_matches",
    "review_sports_identity_match",
    "revert_sports_identity_match",
    "record_sports_club_lineage",
    "link_sports_venue_place",
    "record_sports_decision",
    "record_sports_table_rule",
    "record_sports_transfer",
    "import_sports_olympic_results",
    "refresh_sports_news_links",
    "review_sports_news_link",
    "revert_sports_news_link",
    "register_sports_forecast",
    "create_sports_monitor",
    "run_sports_monitor",
}
SPORTS_TOOLS = set(SPORTS_SCOPES)


def required_scopes(tool_name, mutability):
    return SPORTS_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def call(name, operation, *, write=False):
        """Run with exactly the tool's declared scopes checked; not_ready before any source ran."""
        declared = SPORTS_SCOPES[name]

        def run(conn):
            from src.kb.sports_records import SportsError

            scopes = who()[1]
            missing = [s for s in declared if s not in scopes]
            if missing and "operator" not in scopes:
                raise SportsError("unauthorized", f"{missing[0]} scope is required")
            return operation(conn)

        return safe(run, write=write, required_scope=declared[0] if declared else None)

    def identity(conn):
        from src.kb.sports_identity import SportsIdentity

        return SportsIdentity(conn, initialize=False)

    @mcp.tool()
    def sports_source_contracts() -> dict:
        """Per-source access and licence decisions (SP01), attribution, bounds and live-verification status."""
        from src.ingestion.sports_sources import (
            LIVE_VERIFICATION,
            PROVIDER_CONTRACTS,
            REVIEW_BOUNDARY,
        )

        return {
            "contracts": PROVIDER_CONTRACTS,
            "live_verification": LIVE_VERIFICATION,
            "review_boundary": REVIEW_BOUNDARY,
        }

    @mcp.tool()
    def sports_readiness() -> dict:
        """Whether the Sports store is ready, which optional features are selected, and per-provider acquisitions."""
        from src.kb.sports_bundle import readiness

        return call("sports_readiness", readiness)

    @mcp.tool()
    def sports_standings_as_of(
        namespace: str,
        season_key: str,
        date: str,
        knowledge_cutoff: str | None = None,
        acquired_by_ms: int | None = None,
    ) -> dict:
        """A competition table on a date: replayed from results published by the knowledge cutoff (default the end
        of the date) under the stated rule and deductions, beside the nearest source-published table; disagreements
        are listed, never resolved. Every counted result and correction is cited."""
        from src.kb.sports_queries import SportsQueries

        return call(
            "sports_standings_as_of",
            lambda conn: SportsQueries(conn).standings_as_of(
                namespace,
                season_key,
                date,
                scopes=who()[1],
                knowledge_cutoff=knowledge_cutoff,
                acquired_by_ms=acquired_by_ms,
            ),
        )

    @mcp.tool()
    def sports_match_history(
        namespace: str, fixture_key: str, knowledge_cutoff: str | None = None
    ) -> dict:
        """Every result revision of a match (provisional, official, corrected, forfeit, annulled) with the deciding
        body's citation, its schedule revisions, lineups as published, and other sources' results side by side."""
        from src.kb.sports_queries import SportsQueries

        return call(
            "sports_match_history",
            lambda conn: SportsQueries(conn).match_history(
                namespace,
                fixture_key,
                scopes=who()[1],
                knowledge_cutoff=knowledge_cutoff,
                identity=identity(conn),
            ),
        )

    @mcp.tool()
    def sports_fixture_history(namespace: str, fixture_key: str) -> dict:
        """A fixture's reschedule history: postponements, new kickoffs and cancellations, each move citing both
        revisions."""
        from src.kb.sports_queries import SportsQueries

        return call(
            "sports_fixture_history",
            lambda conn: SportsQueries(conn).fixture_history(
                namespace, fixture_key, scopes=who()[1]
            ),
        )

    @mcp.tool()
    def sports_team_schedule(
        namespace: str, team_key: str, date_from: str, date_to: str
    ) -> dict:
        """A team's fixtures and results in a window from every source its reviewed identity joins, per source."""
        from src.kb.sports_queries import SportsQueries

        return call(
            "sports_team_schedule",
            lambda conn: SportsQueries(conn).team_schedule(
                namespace,
                team_key,
                scopes=who()[1],
                date_from=date_from,
                date_to=date_to,
                identity=identity(conn),
            ),
        )

    @mcp.tool()
    def sports_player_appearances(namespace: str, player_key: str) -> dict:
        """A player's published appearances in lineups (starter or substitute, position and shirt number)."""
        from src.kb.sports_queries import SportsQueries

        return call(
            "sports_player_appearances",
            lambda conn: SportsQueries(conn).player_appearances(
                namespace, player_key, scopes=who()[1], identity=identity(conn)
            ),
        )

    @mcp.tool()
    def sports_transfers(namespace: str, player_key: str) -> dict:
        """Openly published transfers of a player with their announcement citation."""
        from src.kb.sports_queries import SportsQueries

        return call(
            "sports_transfers",
            lambda conn: SportsQueries(conn).transfers(
                namespace, player_key, scopes=who()[1], identity=identity(conn)
            ),
        )

    @mcp.tool()
    def sports_tennis_matches(
        namespace: str, tour: str | None = None, tourney_id: str | None = None
    ) -> dict:
        """Tennis match results from the licence-gated Sackmann archives, every record carrying CC BY-NC-SA 4.0
        attribution and the share-alike flag."""

        def run(conn):
            from src.kb.sports_queries import SportsQueries

            queries = SportsQueries(conn)
            queries._ready(namespace, who()[1])
            prefix = (
                "sports:sackmann-tennis:fixture:"
                + (f"{tour}:" if tour else "")
                + (f"{tourney_id}:" if tour and tourney_id else "")
            )
            matches = []
            for key in queries.store.records(
                namespace, "fixture", provider="sackmann-tennis"
            ):
                if key.startswith(prefix):
                    matches.append(
                        {
                            "fixture": queries.store.current(namespace, "fixture", key),
                            "results": queries.store.labelled_history(
                                namespace, "match_result_revision", key
                            ),
                        }
                    )
            return {
                "matches": matches,
                "coverage": {"n": len(matches)},
                "licence": "CC BY-NC-SA 4.0: non-commercial use, attribution, share-alike",
            }

        return call("sports_tennis_matches", run)

    @mcp.tool()
    def sports_olympic_event_results(namespace: str, fixture_key: str) -> dict:
        """An Olympic event phase's results as published, with every disqualification or medal reallocation kept as
        a revision naming the deciding body."""
        from src.kb.sports_queries import SportsQueries

        return call(
            "sports_olympic_event_results",
            lambda conn: SportsQueries(conn).match_history(
                namespace, fixture_key, scopes=who()[1]
            ),
        )

    @mcp.tool()
    def export_sports_records(
        namespace: str, refs: list[dict], purpose: str, relicense_as: str | None = None
    ) -> dict:
        """Current revisions with their licences and attributions; refused for commercial use of non-commercial
        records or relicensing of share-alike records."""
        from src.kb.sports_queries import SportsQueries

        return call(
            "export_sports_records",
            lambda conn: SportsQueries(conn).export_records(
                namespace,
                refs,
                scopes=who()[1],
                purpose=purpose,
                relicense_as=relicense_as,
            ),
        )

    @mcp.tool()
    def list_sports_identity_candidates(
        namespace: str, record_key: str | None = None
    ) -> dict:
        """Proposed, accepted, rejected and reverted identity candidates between sports records, with evidence."""
        return call(
            "list_sports_identity_candidates",
            lambda conn: {
                "candidates": identity(conn).candidates(
                    namespace, scopes=who()[1], record_key=record_key
                )
            },
        )

    @mcp.tool()
    def propose_sports_identity_matches(namespace: str) -> dict:
        """Deterministic candidates from cross ids, name + published birth date (players), name + country
        (teams) and competition + season; nothing is merged and every candidate waits for review."""
        from src.kb.sports_identity import SportsIdentity

        return call(
            "propose_sports_identity_matches",
            lambda conn: SportsIdentity(conn).propose(
                namespace, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
        )

    @mcp.tool()
    def review_sports_identity_match(
        namespace: str, candidate_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject a sports identity candidate as an entity identity decision; a name alone never passes."""
        from src.kb.sports_identity import SportsIdentity

        return call(
            "review_sports_identity_match",
            lambda conn: SportsIdentity(conn).review(
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
    def revert_sports_identity_match(
        namespace: str, candidate_id: str, reason: str
    ) -> dict:
        """Undo an accepted or rejected sports identity decision; records stay intact."""
        from src.kb.sports_identity import SportsIdentity

        return call(
            "revert_sports_identity_match",
            lambda conn: SportsIdentity(conn).revert(
                namespace, candidate_id, reason, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
        )

    @mcp.tool()
    def record_sports_club_lineage(
        namespace: str,
        kind: str,
        subject_key: str,
        object_key: str,
        valid_on: str,
        source: dict,
        reason: str,
    ) -> dict:
        """A cited club rename, merger or phoenix club as a reversible identity-history event; never a merge."""
        from src.kb.sports_identity import SportsIdentity

        return call(
            "record_sports_club_lineage",
            lambda conn: SportsIdentity(conn).record_lineage(
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
        )

    @mcp.tool()
    def link_sports_venue_place(
        namespace: str, venue_key: str, geo_namespace: str
    ) -> dict:
        """Save a reviewable Geospatial place resolution for a venue with its city or country; never by name alone."""
        from src.kb.sports_identity import SportsIdentity

        return call(
            "link_sports_venue_place",
            lambda conn: SportsIdentity(conn).link_venue_place(
                namespace,
                venue_key,
                geo_namespace=geo_namespace,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
        )

    @mcp.tool()
    def sports_venue_place(namespace: str, venue_key: str) -> dict:
        """A venue's place reference: accepted through Geospatial review, or unresolved."""
        return call(
            "sports_venue_place",
            lambda conn: identity(conn).place(namespace, venue_key, scopes=who()[1]),
        )

    @mcp.tool()
    def record_sports_decision(
        namespace: str,
        fixture_key: str,
        status: str,
        deciding_body: str,
        decision: dict,
        published_at: str,
        score: dict | None = None,
    ) -> dict:
        """A governing body's forfeit, annulment or official result as a revision citing its decision."""
        from src.kb.sports_store import record_result_decision

        return call(
            "record_sports_decision",
            lambda conn: record_result_decision(
                conn,
                namespace,
                fixture_key,
                status=status,
                deciding_body=deciding_body,
                decision=decision,
                published_at=published_at,
                principal_id=who()[0],
                score=score,
            ),
            write=True,
        )

    @mcp.tool()
    def record_sports_table_rule(
        namespace: str,
        season_key: str,
        rule_id: str,
        rule: dict,
        published_at: str,
        citation_url: str,
        attribution: str,
    ) -> dict:
        """A season's stated scoring rule and tie-breakers, or a points deduction naming the deciding body."""
        from src.kb.sports_store import record_table_rule

        return call(
            "record_sports_table_rule",
            lambda conn: record_table_rule(
                conn,
                namespace,
                season_key,
                rule_id,
                rule,
                published_at=published_at,
                citation_url=citation_url,
                attribution=attribution,
                principal_id=who()[0],
            ),
            write=True,
        )

    @mcp.tool()
    def record_sports_transfer(
        namespace: str,
        player_name: str,
        to_team_key: str,
        date: str,
        source: dict,
        from_team_key: str | None = None,
        birth_date: str | None = None,
        fee: dict | None = None,
    ) -> dict:
        """An openly published transfer (governing body, league or club announcement) with its citation; a transfer
        reported only by news is refused and stays a news claim."""
        from src.kb.sports_links import SportsLinks

        return call(
            "record_sports_transfer",
            lambda conn: SportsLinks(conn).record_transfer(
                namespace,
                player_name=player_name,
                to_team_key=to_team_key,
                date=date,
                source=source,
                from_team_key=from_team_key,
                birth_date=birth_date,
                fee=fee,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
        )

    @mcp.tool()
    def import_sports_olympic_results(
        namespace: str, csv_text: str, source_url: str
    ) -> dict:
        """Import one transcription of an official Olympic results publication, cited to it; a reallocation is a
        later publication naming the deciding body."""

        def run(conn):
            from src.ingestion.sports_sources import (
                SportsFormatError,
                olympic_acquisition,
            )
            from src.kb.sports_records import WRITE_SCOPE, SportsError, authorize
            from src.kb.sports_store import SportsStore

            authorize(namespace, who()[1], WRITE_SCOPE, write=True)
            try:
                header, records = olympic_acquisition(csv_text, source_url=source_url)
            except SportsFormatError as exc:
                raise SportsError("invalid_publication", str(exc)) from exc
            return SportsStore(conn).apply(
                namespace,
                header,
                records,
                run_id=f"import:{who()[0]}",
                source_id="operator:olympic-results",
            )

        return call("import_sports_olympic_results", run, write=True)

    @mcp.tool()
    def refresh_sports_news_links(
        namespace: str, target_type: str, target_key: str, window_days: int = 2
    ) -> dict:
        """Offer reviewable news candidates for a match or transfer from explicit mentions of both sides, the
        competition and the date window; keywords alone are never linked."""
        from src.kb.sports_identity import SportsIdentity
        from src.kb.sports_links import SportsLinks

        return call(
            "refresh_sports_news_links",
            lambda conn: SportsLinks(conn).refresh_news_links(
                namespace,
                target_type=target_type,
                target_key=target_key,
                principal_id=who()[0],
                scopes=who()[1],
                identity=SportsIdentity(conn),
                window_days=window_days,
            ),
            write=True,
        )

    @mcp.tool()
    def review_sports_news_link(
        namespace: str, link_id: str, decision: str, reason: str
    ) -> dict:
        """Accept or reject a news candidate; the evidence stays with the link."""
        from src.kb.sports_links import SportsLinks

        return call(
            "review_sports_news_link",
            lambda conn: SportsLinks(conn).review(
                namespace,
                link_id,
                decision,
                reason,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
        )

    @mcp.tool()
    def revert_sports_news_link(namespace: str, link_id: str, reason: str) -> dict:
        """Revert a reviewed news link (a new revision; nothing is deleted)."""
        from src.kb.sports_links import SportsLinks

        return call(
            "revert_sports_news_link",
            lambda conn: SportsLinks(conn).revert(
                namespace, link_id, reason, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
        )

    @mcp.tool()
    def sports_news_links(namespace: str, target_key: str) -> dict:
        """News candidates and reviewed links of a match or transfer with their evidence and article revision."""

        def run(conn):
            from src.kb.sports_links import SportsLinks

            return {
                "links": SportsLinks(conn, initialize=False).links(
                    namespace, target_key, scopes=who()[1]
                )
            }

        return call("sports_news_links", run)

    @mcp.tool()
    def register_sports_forecast(
        namespace: str,
        forecast_namespace: str,
        request_key: str,
        rule: dict,
        probability: float,
        resolution_at_ms: int,
        evidence: list[dict] | None = None,
        question: str | None = None,
    ) -> dict:
        """Register your own binary forecast in the ledger with a structured sports rule (match outcome, final
        position, advancement); it resolves only on official results. Noesis produces no forecast, odds or tip."""
        from src.kb.sports_forecasts import SportsForecasts

        return call(
            "register_sports_forecast",
            lambda conn: SportsForecasts(conn).register(
                namespace,
                forecast_namespace,
                request_key,
                rule=rule,
                probability=probability,
                resolution_at_ms=resolution_at_ms,
                evidence=list(evidence or []),
                principal_id=who()[0],
                scopes=who()[1],
                question=question,
            ),
            write=True,
        )

    @mcp.tool()
    def score_sports_forecasts(
        forecast_namespace: str, forecast_ids: list[str]
    ) -> dict:
        """score_binary_forecasts with the cutoff stated: the earliest official result publication of the cohort."""
        from src.kb.sports_forecasts import SportsForecasts

        return call(
            "score_sports_forecasts",
            lambda conn: SportsForecasts(conn, initialize=False).score(
                forecast_namespace, forecast_ids, principal_id=who()[0], scopes=who()[1]
            ),
        )

    @mcp.tool()
    def create_sports_monitor(
        namespace: str, request_key: str, watch: str, key: str
    ) -> dict:
        """Watch a competition season, team or match for reschedules, results, corrections, forfeits, table changes
        and resolvable forecasts through subscriptions."""
        from src.kb.sports_monitoring import SportsMonitor

        return call(
            "create_sports_monitor",
            lambda conn: SportsMonitor(conn).create(
                namespace,
                request_key,
                watch=watch,
                key=key,
                principal_id=who()[0],
                scopes=who()[1],
            ),
            write=True,
        )

    @mcp.tool()
    def run_sports_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a sports monitor at a committed watermark; notifications cite the old and new revision."""
        from src.kb.sports_monitoring import SportsMonitor

        return call(
            "run_sports_monitor",
            lambda conn: SportsMonitor(conn).run(
                subscription_id, watermark, principal_id=who()[0], scopes=who()[1]
            ),
            write=True,
        )

    @mcp.tool()
    def poll_sports_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Delivered sports monitor events after a cursor."""

        def run(conn):
            from src.kb.sports_monitoring import SportsMonitor
            from src.kb.sports_records import SportsError, table_exists

            if not table_exists(conn, "knowledge_subscriptions"):
                raise SportsError("not_ready", "no sports monitor has been created yet")
            return SportsMonitor(conn, initialize=False).poll(
                subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor
            )

        return call("poll_sports_monitor", run)
