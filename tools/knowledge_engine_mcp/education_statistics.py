"""Science education-statistics feature's entry points: institution and country statistics as of a vintage, ROR
identity review, citation links to Science and Funding records and subscription monitors.

Acquisition runs through the shared source-pack tools (pack ``primary-scientific-evidence``: sources
``ipeds-institution-statistics``, ``eter-institution-statistics``, ``unesco-uis-education-indicators``,
``oecd-eag-education-indicators`` and ``eurostat-rd-statistics``). Every value is cited with its source, definition,
unit, reference period, release vintage and as-of time; values of different sources stay side by side.

Exclusions (declared by every answering tool): no rankings, league tables, quality or composite scores, no merging,
averaging or harmonising of sources, no currency conversion and no derived per-student or per-staff ratios.
"""

READ = "knowledge:education:read"
WRITE = "knowledge:education:write"
REVIEW = "knowledge:education:review"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
EXCLUSIONS_NOTE = (
    "Exclusions: no rankings or quality scores, no merging or averaging of sources, no derived per-student ratios."
)

EDUCATION_WRITES = {
    "record_education_ror_records",
    "propose_education_ror_matches",
    "review_education_ror_match",
    "revert_education_ror_match",
    "link_education_record",
    "create_education_monitor",
    "run_education_monitor",
}
EDUCATION_READS = {
    "education_source_contracts",
    "education_statistics_readiness",
    "list_education_series",
    "education_series_values",
    "institution_statistics_as_of",
    "country_education_statistics_as_of",
    "education_value_vintages",
    "list_education_identity_matches",
    "list_education_links",
    "poll_education_monitor",
}
EDUCATION_TOOLS = EDUCATION_WRITES | EDUCATION_READS
EDUCATION_SCOPES = {
    "education_source_contracts": [],
    "education_statistics_readiness": [READ],
    "list_education_series": [READ],
    "education_series_values": [READ],
    "institution_statistics_as_of": [READ],
    "country_education_statistics_as_of": [READ],
    "education_value_vintages": [READ],
    "list_education_identity_matches": [READ],
    "list_education_links": [READ],
    "poll_education_monitor": [READ, SUBSCRIPTIONS_READ],
    "record_education_ror_records": [WRITE],
    "propose_education_ror_matches": [WRITE],
    "review_education_ror_match": [REVIEW],
    "revert_education_ror_match": [REVIEW],
    "link_education_record": [WRITE],
    "create_education_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_education_monitor": [READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return EDUCATION_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _require(scopes, *required):
    from src.kb.education_statistics import EducationError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise EducationError("unauthorized", f"{', '.join(missing)} required")


def _declared(answer):
    from src.ingestion.education_sources import EXCLUSIONS

    return {**answer, "exclusions": list(EXCLUSIONS)} if isinstance(answer, dict) else answer


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def run_tool(tool, operation, *, write=False):
        """Checks every declared scope, runs the operation and declares the exclusions on the answer."""
        scopes = EDUCATION_SCOPES[tool]

        def run(conn):
            _require(who()[1], *scopes)
            return _declared(operation(conn))

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def education_source_contracts() -> dict:
        """Per-source access decisions (US IPEDS, ETER, UNESCO UIS, OECD Education at a Glance, Eurostat R&D):
        endpoints, formats, identifiers, licences, rate limits, release vintages and the bounded coverage.
        Exclusions: no rankings or quality scores, no merging or averaging of sources, no derived per-student
        ratios."""
        from src.ingestion.education_sources import (
            BOUNDED_COVERAGE,
            EXCLUSIONS,
            LIVE_VERIFICATION,
            NEVER_SENTENCE,
            PROVIDER_CONTRACTS,
        )

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "bounded_coverage": BOUNDED_COVERAGE, "never": NEVER_SENTENCE, "exclusions": list(EXCLUSIONS)}

    @mcp.tool()
    def education_statistics_readiness() -> dict:
        """Whether the Science education-statistics feature is selected, its stores and per-provider releases."""
        from src.kb.education_statistics import readiness

        return run_tool("education_statistics_readiness", readiness)

    @mcp.tool()
    def list_education_series(namespace: str, provider: str | None = None, record_kind: str | None = None,
                              concept: str | None = None, indicator: str | None = None) -> dict:
        """Institution-statistic and education-indicator series with subject, indicator, concept, ISCED level, unit
        and vintage count. No ranked, scored or harmonised series exist."""
        from src.kb.education_statistics import EducationStatisticsStore, authorize

        def op(conn):
            authorize(namespace, who()[1], READ)
            return {"series": EducationStatisticsStore(conn, initialize=False).find_series(
                namespace, provider=provider, record_kind=record_kind, concept=concept, indicator=indicator)}

        return run_tool("list_education_series", op)

    @mcp.tool()
    def education_series_values(namespace: str, series_id: str, vintage_id: str | None = None,
                                as_of: str | None = None) -> dict:
        """One series' values in one vintage (or the vintage published by as_of), with its definition, status
        codes and comparability notes; every vintage listed.
        Exclusions: no rankings or quality scores, no merging or averaging of sources, no derived per-student
        ratios."""
        from src.kb.education_statistics import EducationStatisticsStore, as_of_ms, authorize

        def op(conn):
            authorize(namespace, who()[1], READ)
            store = EducationStatisticsStore(conn, initialize=False)
            return {**store.values(namespace, series_id, vintage_id=vintage_id, as_of=as_of_ms(as_of)),
                    "vintages": store.vintage_rows(namespace, series_id)}

        return run_tool("education_series_values", op)

    @mcp.tool()
    def institution_statistics_as_of(namespace: str, ror: str | None = None, scheme: str | None = None,
                                     code: str | None = None, as_of: str | None = None, concept: str | None = None,
                                     indicator: str | None = None) -> dict:
        """An institution's statistics as published by as_of - by ROR id (exact or accepted matches only) or by
        IPEDS UNITID / ETER ID - with definitions, units, vintages, comparability notes, citations, profiles,
        identity basis, reported ROR changes and linked Science and Funding records; none_on_record when empty.
        Exclusions: no rankings or quality scores, no merging or averaging of sources, no derived per-student
        ratios."""
        from src.kb.education_statistics import EducationQueries

        return run_tool("institution_statistics_as_of", lambda conn: EducationQueries(conn).institution(
            namespace, scopes=who()[1], ror=ror, scheme=scheme, code=code, as_of=as_of, concept=concept,
            indicator=indicator))

    @mcp.tool()
    def country_education_statistics_as_of(namespace: str, country: str, as_of: str | None = None,
                                           concept: str | None = None, indicator: str | None = None,
                                           isced: str | None = None) -> dict:
        """A country's education and R&D indicators (UIS, OECD EAG, Eurostat) as published by as_of: each source's
        value side by side with its definition, ISCED level, unit, vintage, notes and citation.
        Exclusions: no rankings or quality scores, no merging or averaging of sources, no derived per-student
        ratios."""
        from src.kb.education_statistics import EducationQueries

        return run_tool("country_education_statistics_as_of", lambda conn: EducationQueries(conn).country(
            namespace, scopes=who()[1], country=country, as_of=as_of, concept=concept, indicator=indicator,
            isced=isced))

    @mcp.tool()
    def education_value_vintages(namespace: str, series_id: str, period: str) -> dict:
        """Every published vintage of one value (provisional, final, revised), each with its source revision."""
        from src.kb.education_statistics import EducationQueries

        return run_tool("education_value_vintages", lambda conn: EducationQueries(conn).value_vintages(
            namespace, series_id, period, scopes=who()[1]))

    @mcp.tool()
    def list_education_identity_matches(namespace: str, state: str | None = None, ror: str | None = None) -> dict:
        """IPEDS/ETER-to-ROR matches (exact, candidate, accepted, rejected, reverted, unmatched) with basis and
        evidence; only exact and accepted matches answer ROR-keyed queries."""
        from src.kb.education_identity import EducationIdentity, ror_url
        from src.kb.education_statistics import authorize

        def op(conn):
            authorize(namespace, who()[1], READ)
            identity = EducationIdentity(conn, initialize=False)
            return {"matches": identity.matches(namespace, scopes=who()[1], state=state,
                                                ror_id=ror_url(ror) if ror else None)}

        return run_tool("list_education_identity_matches", op)

    @mcp.tool()
    def list_education_links(namespace: str, ror: str | None = None, side: str | None = None) -> dict:
        """Links from institutions to Science and Funding records with their basis and citation."""
        from src.kb.education_statistics import EducationLinks

        return run_tool("list_education_links", lambda conn: {"links": EducationLinks(conn, initialize=False).links(
            namespace, scopes=who()[1], ror=ror, side=side)})

    @mcp.tool()
    def poll_education_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll an education monitor's events (new vintages, revised values, identity-match changes)."""
        from src.kb.education_monitoring import EducationMonitor

        return run_tool("poll_education_monitor", lambda conn: EducationMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))

    # ------------------------------------------------------------------ writes

    @mcp.tool()
    def record_education_ror_records(namespace: str, records: list[dict] | None = None,
                                     ror_ids: list[str] | None = None) -> dict:
        """Keep ROR registry records as snapshots (given as ROR v2 JSON, or fetched by id through the ROR client);
        status changes and successors are reported."""
        from src.kb.education_identity import EducationIdentity

        def op(conn):
            identity = EducationIdentity(conn)
            result = identity.record_ror(namespace, list(records or []), principal_id=who()[0], scopes=who()[1])
            if ror_ids:
                from src.ingestion.ror import RORClient

                fetched = identity.fetch_ror(namespace, ror_ids, client=RORClient(), principal_id=who()[0],
                                             scopes=who()[1])
                result = {"recorded": result["recorded"] + fetched["recorded"],
                          "changes": result["changes"] + fetched["changes"]}
            return result

        return run_tool("record_education_ror_records", op, write=True)

    @mcp.tool()
    def propose_education_ror_matches(namespace: str) -> dict:
        """Offer every IPEDS and ETER institution to the held ROR records: published identifiers are exact, equal
        name and country are candidates for review; unmatched institutions stay addressable by source id."""
        from src.kb.education_identity import EducationIdentity

        return run_tool("propose_education_ror_matches", lambda conn: EducationIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_education_ror_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a candidate ROR match with a reason (an entity identity decision)."""
        from src.kb.education_identity import EducationIdentity

        return run_tool("review_education_ror_match", lambda conn: EducationIdentity(conn).review(
            namespace, match_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def revert_education_ror_match(namespace: str, match_id: str, reason: str) -> dict:
        """Revert an exact, accepted or rejected ROR match; it is not used until reviewed again."""
        from src.kb.education_identity import EducationIdentity

        return run_tool("revert_education_ror_match", lambda conn: EducationIdentity(conn).revert(
            namespace, match_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def link_education_record(namespace: str, subject: dict, target: dict, citation: dict) -> dict:
        """Link an institution (ROR id through a confirmed match, or IPEDS/ETER id) to a Funding record, scholarly
        work or methodology study whose citation states the identifier; no name or keyword joins."""
        from src.kb.education_statistics import EducationLinks

        return run_tool("link_education_record", lambda conn: EducationLinks(conn).link(
            namespace, subject=subject, target=target, citation=citation, principal_id=who()[0],
            scopes=who()[1]), write=True)

    @mcp.tool()
    def create_education_monitor(namespace: str, request_key: str, watch: dict, delivery: dict | None = None) -> dict:
        """Subscribe to a ROR id, an institution, a country or an indicator: notices for new vintages, revised
        values and identity-match changes (record changes, never judgements)."""
        from src.kb.education_monitoring import EducationMonitor

        return run_tool("create_education_monitor", lambda conn: EducationMonitor(conn).create(
            namespace, request_key, watch=watch, principal_id=who()[0], scopes=who()[1], delivery=delivery),
            write=True)

    @mcp.tool()
    def run_education_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate an education monitor at a committed watermark; notices cite the release and the record ids."""
        from src.kb.education_monitoring import EducationMonitor

        return run_tool("run_education_monitor", lambda conn: EducationMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True)
