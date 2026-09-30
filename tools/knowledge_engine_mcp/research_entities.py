"""Science research-entities features' entry points: ROR organisations, minimised public ORCID researchers, DataCite
datasets and CORDIS projects as of a date, reviewable organisation identity, links to other packs and monitors.

Acquisition runs through the shared source-pack tools (pack ``research-discovery``: sources ``ror-organisations``,
``orcid-public-records``, ``datacite-research-datasets`` and ``cordis-horizon-projects``). Every answer cites each
record with its source, record revision and as-of time.

Exclusions (declared by every answering tool and enforced on its output): no researcher rankings or metrics, no
affiliation inferred from co-authorship, no author matching by name, and no personal data beyond the public ORCID
fields of the data-minimisation decision; researcher records need the researchers scope.
"""

READ = "knowledge:research-entities:read"
WRITE = "knowledge:research-entities:write"
REVIEW = "knowledge:research-entities:review"
RESEARCHERS = "knowledge:research-entities:researchers"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
EXCLUSIONS_NOTE = (
    "Exclusions: no researcher rankings or metrics, no affiliation inferred from co-authorship, no author matching "
    "by name, minimised personal data only."
)

RESEARCH_ENTITY_WRITES = {
    "propose_research_entity_matches",
    "review_research_entity_match",
    "revert_research_entity_match",
    "build_research_entity_links",
    "create_research_entities_monitor",
    "run_research_entities_monitor",
}
RESEARCH_ENTITY_READS = {
    "research_entities_source_contracts",
    "research_entities_readiness",
    "research_organisation_as_of",
    "researcher_assertions_as_of",
    "research_datasets_for_paper",
    "research_record_history",
    "list_research_entity_matches",
    "list_research_entity_links",
    "export_research_entities_evidence_bundle",
    "poll_research_entities_monitor",
}
RESEARCH_ENTITY_TOOLS = RESEARCH_ENTITY_WRITES | RESEARCH_ENTITY_READS
RESEARCH_ENTITY_SCOPES = {
    "research_entities_source_contracts": [],
    "research_entities_readiness": [READ],
    "research_organisation_as_of": [READ],
    "researcher_assertions_as_of": [READ, RESEARCHERS],
    "research_datasets_for_paper": [READ],
    "research_record_history": [READ],
    "list_research_entity_matches": [READ],
    "list_research_entity_links": [READ],
    "export_research_entities_evidence_bundle": [READ],
    "poll_research_entities_monitor": [READ, SUBSCRIPTIONS_READ],
    "propose_research_entity_matches": [WRITE],
    "review_research_entity_match": [REVIEW],
    "revert_research_entity_match": [REVIEW],
    "build_research_entity_links": [WRITE],
    "create_research_entities_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_research_entities_monitor": [READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return RESEARCH_ENTITY_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _require(scopes, *required):
    from src.kb.research_entities_records import ResearchEntitiesError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise ResearchEntitiesError("unauthorized", f"{', '.join(missing)} required")


def _mask(value, allowed):
    """ORCID iDs of dataset creators are shown only to callers who may read researcher records."""
    if isinstance(value, dict):
        out = {k: _mask(v, allowed) for k, v in value.items()}
        if not allowed and "orcid" in out and "affiliation_identifiers" in out:
            out["orcid"], out["orcid_withheld"] = None, bool(value.get("orcid"))
        return out
    if isinstance(value, list):
        return [_mask(v, allowed) for v in value]
    return value


def _declared(answer, scopes):
    """Declare the exclusions and enforce them and the minimisation decision on the output."""
    from src.ingestion.research_entities_sources import EXCLUSIONS
    from src.kb.research_entities_records import (
        ResearchEntitiesError,
        forbidden_keys,
        may_read_researchers,
    )

    if not isinstance(answer, dict):
        return answer
    if forbidden_keys(answer):
        raise ResearchEntitiesError("forbidden_field", "an answer carried a ranking, metric or inferred field")
    return {**_mask(answer, may_read_researchers(scopes)), "exclusions": list(EXCLUSIONS)}


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def run_tool(tool, operation, *, write=False):
        """Checks every declared scope, runs the operation and declares the exclusions on the answer."""
        scopes = RESEARCH_ENTITY_SCOPES[tool]

        def run(conn):
            _require(who()[1], *scopes)
            return _declared(operation(conn), who()[1])

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def research_entities_source_contracts() -> dict:
        """Per-source access decisions (ROR data dump, ORCID Public API, DataCite REST API, CORDIS bulk files):
        endpoints, authentication, licences, rate limits, revision models, bounded coverage, the data-minimisation
        decision and the sources recorded as not implemented.
        Exclusions: no researcher rankings or metrics, no affiliation inferred from co-authorship, no author matching
        by name, minimised personal data only."""
        from src.ingestion.research_entities_sources import (
            BOUNDED_COVERAGE,
            EXCLUSIONS,
            LIVE_VERIFICATION,
            MINIMISATION,
            NEVER_SENTENCE,
            NOT_IMPLEMENTED,
            PROVIDER_CONTRACTS,
        )

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "bounded_coverage": BOUNDED_COVERAGE, "minimisation": MINIMISATION,
                "not_implemented": NOT_IMPLEMENTED, "never": NEVER_SENTENCE, "exclusions": list(EXCLUSIONS)}

    @mcp.tool()
    def research_entities_readiness() -> dict:
        """Which research-entities features (ROR, ORCID, DataCite, CORDIS) are selected, their stores, releases,
        records and live-verification status per provider."""
        from src.kb.research_entities_records import readiness

        return run_tool("research_entities_readiness", readiness)

    @mcp.tool()
    def research_organisation_as_of(namespace: str, ror: str, as_of: str | None = None) -> dict:
        """A ROR organisation as in the release in force at as_of: names, types, status, relationships as
        published, lineage along predecessors and successors, CORDIS projects of accepted participants
        (contributions per currency, never summed across currencies), DataCite datasets stating it as an
        affiliation, ownership links and identity state; every record version cited; none_on_record when not held.
        Exclusions: no researcher rankings or metrics, no affiliation inferred from co-authorship, no author matching
        by name, minimised personal data only."""
        from src.kb.research_entities_queries import ResearchEntitiesQueries

        return run_tool("research_organisation_as_of", lambda conn: ResearchEntitiesQueries(conn).organisation(
            namespace, ror, scopes=who()[1], as_of=as_of))

    @mcp.tool()
    def researcher_assertions_as_of(namespace: str, orcid: str, as_of: str | None = None) -> dict:
        """Employments and works asserted in the public ORCID record version in force at as_of, each labelled
        ORCID-asserted (never verified authorship), only minimisation-allowed fields, the record version cited,
        linked papers by asserted DOI and datasets naming the iD. Needs the researchers scope.
        Exclusions: no researcher rankings or metrics, no affiliation inferred from co-authorship, no author matching
        by name, minimised personal data only."""
        from src.kb.research_entities_queries import ResearchEntitiesQueries

        return run_tool("researcher_assertions_as_of", lambda conn: ResearchEntitiesQueries(conn).researcher(
            namespace, orcid, scopes=who()[1], as_of=as_of))

    @mcp.tool()
    def research_datasets_for_paper(namespace: str, doi: str, as_of: str | None = None) -> dict:
        """DataCite datasets whose related identifiers name a paper DOI, with the relation type as published and
        each metadata version cited.
        Exclusions: no researcher rankings or metrics, no affiliation inferred from co-authorship, no author matching
        by name, minimised personal data only."""
        from src.kb.research_entities_queries import ResearchEntitiesQueries

        return run_tool("research_datasets_for_paper", lambda conn: ResearchEntitiesQueries(conn).datasets_for_paper(
            namespace, doi, scopes=who()[1], as_of=as_of))

    @mcp.tool()
    def research_record_history(namespace: str, kind: str, identifier: str, programme: str | None = None) -> dict:
        """Every revision of one organisation, researcher (researchers scope), dataset or project, oldest first,
        including corrections and removals by the source, each cited with its release.
        Exclusions: no researcher rankings or metrics, no affiliation inferred from co-authorship, no author matching
        by name, minimised personal data only."""
        from src.kb.research_entities_queries import ResearchEntitiesQueries

        return run_tool("research_record_history", lambda conn: ResearchEntitiesQueries(conn).record_history(
            namespace, kind, identifier, scopes=who()[1], programme=programme))

    @mcp.tool()
    def list_research_entity_matches(namespace: str, state: str | None = None, ror: str | None = None,
                                     pic: str | None = None) -> dict:
        """ROR-participant and ownership identity matches with method, evidence, confidence and state (proposed,
        accepted, rejected, reverted), and the organisations and participants still unmatched. Researchers are
        never matched."""
        from src.kb.research_entities_identity import ResearchEntitiesIdentity
        from src.kb.research_entities_records import authorize

        def op(conn):
            authorize(namespace, who()[1], READ)
            identity = ResearchEntitiesIdentity(conn, initialize=False)
            return {"matches": identity.matches(namespace, scopes=who()[1], state=state, ror=ror, pic=pic),
                    "unmatched": identity.unmatched(namespace, scopes=who()[1])}

        return run_tool("list_research_entity_matches", op)

    @mcp.tool()
    def list_research_entity_links(namespace: str, target_kind: str | None = None) -> dict:
        """Links to Scholarly-literature works, Funding records and ownership entities with their basis (asserted
        or published identifier, accepted match), both revisions and the target status (resolved, target_missing,
        provider_absent)."""
        from src.kb.research_entities_links import ResearchEntitiesLinks

        return run_tool("list_research_entity_links", lambda conn: {"links": ResearchEntitiesLinks(
            conn, initialize=False).links(namespace, scopes=who()[1], target_kind=target_kind)})

    @mcp.tool()
    def export_research_entities_evidence_bundle(namespace: str, kind: str, identifier: str,
                                                 as_of: str | None = None) -> dict:
        """A noesis-evidence-bundle-v1 for an organisation (ror), researcher (orcid; researchers scope) or paper
        (doi) answer: every record revision cited with source, revision and as-of time; unresolved parts are
        omissions.
        Exclusions: no researcher rankings or metrics, no affiliation inferred from co-authorship, no author matching
        by name, minimised personal data only."""
        from src.kb.research_entities_queries import ResearchEntitiesQueries
        from src.kb.research_entities_records import ResearchEntitiesError

        def op(conn):
            queries = ResearchEntitiesQueries(conn)
            if kind == "ror":
                answer = queries.organisation(namespace, identifier, scopes=who()[1], as_of=as_of)
            elif kind == "orcid":
                answer = queries.researcher(namespace, identifier, scopes=who()[1], as_of=as_of)
            elif kind == "doi":
                answer = queries.datasets_for_paper(namespace, identifier, scopes=who()[1], as_of=as_of)
            else:
                raise ResearchEntitiesError("invalid_request", "kind is ror, orcid or doi")
            return {"bundle": queries.export_bundle(answer), "answer_status": answer["status"]}

        return run_tool("export_research_entities_evidence_bundle", op)

    @mcp.tool()
    def poll_research_entities_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a research-entities monitor's events (new records, registry changes, new asserted works, new
        datasets, new projects)."""
        from src.kb.research_entities_monitoring import ResearchEntitiesMonitor

        return run_tool("poll_research_entities_monitor", lambda conn: ResearchEntitiesMonitor(
            conn, initialize=False).poll(subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))

    # ------------------------------------------------------------------ writes

    @mcp.tool()
    def propose_research_entity_matches(namespace: str, ownership_namespace: str | None = None) -> dict:
        """Propose ROR-participant matches (website domain or name in the same country) and, with an ownership
        namespace, ownership matches (published identifiers first, then name and jurisdiction); nothing is
        accepted or merged, researchers are never matched."""
        from src.kb.research_entities_identity import ResearchEntitiesIdentity

        return run_tool("propose_research_entity_matches", lambda conn: ResearchEntitiesIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1], ownership_namespace=ownership_namespace), write=True)

    @mcp.tool()
    def review_research_entity_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a proposed match with a reason (an entity identity decision, never a merge)."""
        from src.kb.research_entities_identity import ResearchEntitiesIdentity

        return run_tool("review_research_entity_match", lambda conn: ResearchEntitiesIdentity(conn).review(
            namespace, match_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def revert_research_entity_match(namespace: str, match_id: str, reason: str) -> dict:
        """Revert an accepted or rejected match; it is not used until reviewed again."""
        from src.kb.research_entities_identity import ResearchEntitiesIdentity

        return run_tool("revert_research_entity_match", lambda conn: ResearchEntitiesIdentity(conn).revert(
            namespace, match_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def build_research_entity_links(namespace: str, ownership_namespace: str | None = None) -> dict:
        """Link researchers (asserted DOIs) and datasets (related identifiers) to Scholarly works, projects to
        Funding records and organisations to ownership entities (accepted matches only); missing providers and
        targets are reported. No collaboration or influence links."""
        from src.kb.research_entities_links import ResearchEntitiesLinks

        return run_tool("build_research_entity_links", lambda conn: ResearchEntitiesLinks(conn).build(
            namespace, principal_id=who()[0], scopes=who()[1], ownership_namespace=ownership_namespace), write=True)

    @mcp.tool()
    def create_research_entities_monitor(namespace: str, request_key: str, watch: dict,
                                         delivery: dict | None = None) -> dict:
        """Subscribe to an organisation (ror), researcher (orcid; researchers scope), project (project with
        programme) or dataset (doi): notices of registry changes, new asserted works, new datasets and projects
        (record changes, never assessments)."""
        from src.kb.research_entities_monitoring import ResearchEntitiesMonitor

        return run_tool("create_research_entities_monitor", lambda conn: ResearchEntitiesMonitor(conn).create(
            namespace, request_key, watch=watch, principal_id=who()[0], scopes=who()[1], delivery=delivery),
            write=True)

    @mcp.tool()
    def run_research_entities_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a research-entities monitor at a committed watermark; notices cite the new or revised record
        revision and state what changed."""
        from src.kb.research_entities_monitoring import ResearchEntitiesMonitor

        return run_tool("run_research_entities_monitor", lambda conn: ResearchEntitiesMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True)
