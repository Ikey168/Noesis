"""Science research-entities entry points: ROR organisations, public ORCID researchers, DataCite datasets and CORDIS
projects with revisions and as-of answers, reviewable identity, cross-pack links and monitors.

Acquisition runs through the shared source-pack tools (pack ``research-discovery`` 1.5.0: ``research-entities-ror``,
``research-entities-orcid``, ``research-entities-datacite``, ``research-entities-cordis``). ROR, ORCID, DataCite and
CORDIS coverage are the separate optional features ``research-entities-ror``, ``research-entities-orcid``,
``research-entities-datacite`` and ``research-entities-cordis``; literature, funding and ownership links degrade to
``provider_absent`` / ``target_missing`` when those providers are absent. Every answer cites each item with its source,
record revision, as-of time and observation time.

Exclusions: no researcher rankings or metrics, no inference of affiliation from co-authorship, no author
disambiguation by name, and no personal data beyond the public ORCID fields allowed by the RE01 minimisation decision:
researcher records and researcher-derived answers need ``knowledge:science:research-entities:researchers:read``.
"""

RESEARCH_ENTITIES_WRITES = {
    "propose_research_entity_identity_matches",
    "review_research_entity_identity_match",
    "revert_research_entity_identity_match",
    "link_research_entities",
    "create_research_entities_monitor",
    "run_research_entities_monitor",
}
RESEARCH_ENTITIES_READS = {
    "research_entities_source_contracts",
    "research_entities_readiness",
    "research_entity_record",
    "research_entity_as_of",
    "researcher_asserted_works_as_of",
    "organisation_lineage_projects_datasets",
    "datasets_for_paper",
    "export_research_entities_evidence_bundle",
    "list_research_entity_identity_candidates",
    "list_research_entity_links",
    "poll_research_entities_monitor",
}
RESEARCH_ENTITIES_TOOLS = RESEARCH_ENTITIES_WRITES | RESEARCH_ENTITIES_READS
READ = "knowledge:science:research-entities:read"
WRITE = "knowledge:science:research-entities:write"
RESEARCHERS = "knowledge:science:research-entities:researchers:read"
OWNERSHIP_READ = "knowledge:ownership:read"
# Every scope each tool always reads or writes. The researcher scope is conditional for record, link and organisation
# reads (without it researcher records are withheld and counted); it is always required for researcher answers.
RESEARCH_ENTITIES_SCOPES = {
    "research_entities_source_contracts": [],
    "research_entities_readiness": [READ],
    "research_entity_record": [READ],
    "research_entity_as_of": [READ],
    "researcher_asserted_works_as_of": [READ, RESEARCHERS],
    "organisation_lineage_projects_datasets": [READ],
    "datasets_for_paper": [READ],
    "export_research_entities_evidence_bundle": [READ],
    "list_research_entity_identity_candidates": [READ, OWNERSHIP_READ],
    "list_research_entity_links": [READ],
    "poll_research_entities_monitor": [READ, "knowledge:subscriptions:read"],
    "propose_research_entity_identity_matches": [READ, OWNERSHIP_READ, "knowledge:ownership:write"],
    "review_research_entity_identity_match": [OWNERSHIP_READ, "knowledge:ownership:review"],
    "revert_research_entity_identity_match": [OWNERSHIP_READ, "knowledge:ownership:review"],
    "link_research_entities": [READ, WRITE],
    "create_research_entities_monitor": [READ, "knowledge:subscriptions:write"],
    "run_research_entities_monitor": [READ, "knowledge:subscriptions:write"],
}
EXCLUSIONS_NOTE = ("No researcher ranking or metric, no affiliation inferred from co-authorship, no author "
                   "disambiguation by name; researchers are minimised (RE01).")


def required_scopes(tool_name, mutability):
    return RESEARCH_ENTITIES_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def queries(conn):
        from src.kb.research_entities_queries import ResearchEntityQueries

        return ResearchEntityQueries(conn)

    def checked(answer):
        from src.kb.research_entities_records import ResearchEntityError, forbidden_keys

        if forbidden_keys(answer):
            raise ResearchEntityError("exclusion_violation", "an answer carries a ranking or metric key")
        return answer

    @mcp.tool()
    def research_entities_source_contracts() -> dict:
        """Per-registry endpoints, authentication and key handling (the ORCID public-API token), licences, rate limits,
        revision and removal models, bounded coverage, LIVE_VERIFICATION status and the researcher data-minimisation
        decision for ROR, ORCID, DataCite and CORDIS (and the not-implemented OpenAIRE Graph)."""
        from src.ingestion.research_entities_sources import (
            BOUNDED_COVERAGE,
            EXCLUSIONS,
            LIVE_VERIFICATION,
            MINIMISATION,
            PROVIDER_CONTRACTS,
            REVIEW_BOUNDARY,
        )

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "bounded_coverage": BOUNDED_COVERAGE, "minimisation": MINIMISATION,
                "review_boundary": REVIEW_BOUNDARY, "exclusions": list(EXCLUSIONS)}

    @mcp.tool()
    def research_entities_readiness() -> dict:
        """Which research-entities features (ROR, ORCID, DataCite, CORDIS) are selected, records per registry, and
        whether the ORCID source is degraded without its client token."""
        from src.kb.research_entities_records import readiness

        return safe(lambda conn: readiness(conn), required_scope=READ)

    @mcp.tool()
    def research_entity_record(namespace: str, record_key: str) -> dict:
        """The current revision and every revision of one registry record (ROR organisation, ORCID researcher, DataCite
        dataset or CORDIS project) with source, revision, as-of time and citation. Researcher records only with the
        researcher scope and only with minimised fields. No ranking or metric."""
        from src.kb.research_entities_records import ResearchEntityStore

        def run(conn):
            store = ResearchEntityStore(conn, initialize=False)
            history = store.history(namespace, record_key, scopes=who()[1])
            current = store.records(namespace, scopes=who()[1], record_keys=[record_key])
            if not history:
                withheld = record_key.startswith("research-entities:orcid:") and store.withheld_researchers(
                    namespace, scopes=who()[1])
                return {"status": "withheld" if withheld else "none_on_record", "record_key": record_key}
            return checked({"status": "answered", "record_key": record_key, "current": current[0] if current else None,
                            "revisions": history})

        return safe(run, required_scope=READ)

    @mcp.tool()
    def research_entity_as_of(namespace: str, record_key: str, as_of: str) -> dict:
        """The revision of one registry record in force at a date (the latest whose source time is on or before it),
        with its citation; not_yet_published or none_on_record otherwise."""
        from src.kb.research_entities_records import ResearchEntityStore

        def run(conn):
            revision = ResearchEntityStore(conn, initialize=False).as_of(namespace, record_key, as_of,
                                                                         scopes=who()[1])
            if revision is None:
                return {"status": "none_on_record", "record_key": record_key, "as_of": as_of}
            return checked({"status": "answered", "record_key": record_key, "as_of": as_of, "revision": revision})

        return safe(run, required_scope=READ)

    @mcp.tool()
    def researcher_asserted_works_as_of(namespace: str, orcid: str, as_of: str | None = None) -> dict:
        """A researcher's employments and works as asserted in the public ORCID record version in force at a date,
        each labelled ORCID-asserted (never verified authorship), with the record version cited. Only the public
        fields allowed by the minimisation decision; needs the researcher scope. No ranking or metric."""
        return safe(lambda conn: checked(queries(conn).researcher(namespace, orcid, scopes=who()[1], as_of=as_of)),
                    required_scope=RESEARCHERS)

    @mcp.tool()
    def organisation_lineage_projects_datasets(namespace: str, ror: str, as_of: str | None = None) -> dict:
        """An organisation's ROR relationships and successors as published in the release in force, its CORDIS
        projects through accepted participant matches with contributions as published (per currency, never summed
        across currencies), linked datasets and ownership matches, citing each record version. No ranking."""
        return safe(lambda conn: checked(queries(conn).organisation(namespace, ror, scopes=who()[1], as_of=as_of)),
                    required_scope=READ)

    @mcp.tool()
    def datasets_for_paper(namespace: str, doi: str) -> dict:
        """Datasets whose DataCite metadata relates a paper DOI, with the relation type as published."""
        return safe(lambda conn: queries(conn).datasets_for_paper(namespace, doi, scopes=who()[1]),
                    required_scope=READ)

    @mcp.tool()
    def export_research_entities_evidence_bundle(namespace: str, query: str, key: str, as_of: str | None = None
                                                 ) -> dict:
        """An evidence bundle for a researcher, organisation or paper answer (query: researcher | organisation |
        paper): assertions each citing the record revision, source, as-of time and observation time behind them."""
        def run(conn):
            ask = queries(conn)
            if query == "researcher":
                answer = ask.researcher(namespace, key, scopes=who()[1], as_of=as_of)
            elif query == "organisation":
                answer = ask.organisation(namespace, key, scopes=who()[1], as_of=as_of)
            elif query == "paper":
                answer = ask.datasets_for_paper(namespace, key, scopes=who()[1])
            else:
                return {"ok": False, "error": {"code": "invalid_request",
                                               "message": "query is researcher, organisation or paper"}}
            return checked({"status": answer["status"], "query": query, "key": key, "as_of": as_of,
                            "evidence_bundle": ask.evidence_bundle(answer), "exclusions": answer["exclusions"]})

        return safe(run, required_scope=READ)

    @mcp.tool()
    def list_research_entity_identity_candidates(namespace: str, record_key: str | None = None) -> dict:
        """Identity candidates and decisions for ROR organisations and CORDIS participants with method, evidence and
        confidence, and the unmatched subjects. Researchers are never candidates."""
        from src.kb.research_entities_identity import ResearchEntityIdentity
        from src.kb.research_entities_records import authorize

        def run(conn):
            authorize(namespace, who()[1], READ)
            identity = ResearchEntityIdentity(conn, initialize=False)
            return {"candidates": identity.candidates(namespace, scopes=who()[1], subject_key=record_key),
                    "unmatched": identity.unmatched(namespace, scopes=who()[1]),
                    "conflicts": identity.conflicts(namespace, scopes=who()[1])}

        return safe(run, required_scope=READ)

    @mcp.tool()
    def list_research_entity_links(namespace: str, kind: str | None = None, source_key: str | None = None,
                                   target_key: str | None = None) -> dict:
        """Citation, shared-identifier and accepted-match links with the record revisions they point at and their
        basis; missing providers and targets are listed. A link is not evidence of authorship or collaboration."""
        from src.kb.research_entities_links import ResearchEntityLinks

        return safe(lambda conn: {"links": ResearchEntityLinks(conn, initialize=False).links(
            namespace, scopes=who()[1], kind=kind, source_key=source_key, target_key=target_key)},
            required_scope=READ)

    @mcp.tool()
    def propose_research_entity_identity_matches(namespace: str, ownership_namespace: str | None = None) -> dict:
        """Propose reviewable matches of ROR organisations and CORDIS participants to each other and to Corporate
        Ownership entities by published identifiers first (ROR/ISNI/Wikidata/GRID/FundRef, VAT, website host) and
        names only as low evidence; nothing is merged or accepted automatically and researchers are never proposed."""
        from src.kb.research_entities_identity import ResearchEntityIdentity

        return safe(lambda conn: ResearchEntityIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1], ownership_namespace=ownership_namespace),
            write=True, required_scope="knowledge:ownership:write")

    @mcp.tool()
    def review_research_entity_identity_match(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a research-entities identity candidate as an entity identity decision."""
        from src.kb.research_entities_identity import ResearchEntityIdentity

        return safe(lambda conn: ResearchEntityIdentity(conn).review(
            namespace, candidate_id, decision, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def revert_research_entity_identity_match(namespace: str, candidate_id: str, reason: str) -> dict:
        """Revert an accepted or rejected research-entities identity decision; records stay intact."""
        from src.kb.research_entities_identity import ResearchEntityIdentity

        return safe(lambda conn: ResearchEntityIdentity(conn).revert(
            namespace, candidate_id, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:ownership:review")

    @mcp.tool()
    def link_research_entities(namespace: str, ownership_namespace: str | None = None) -> dict:
        """Link researchers and datasets to literature papers by DOI, projects to Funding records by topic or call id,
        datasets to projects by award number and organisations to ownership entities by accepted match; absent
        providers and missing targets are reported, never dropped. No inferred collaboration or influence links."""
        from src.kb.research_entities_links import ResearchEntityLinks

        return safe(lambda conn: ResearchEntityLinks(conn).link(
            namespace, principal_id=who()[0], scopes=who()[1], ownership_namespace=ownership_namespace),
            write=True, required_scope=WRITE)

    @mcp.tool()
    def create_research_entities_monitor(namespace: str, request_key: str, watch: str, key: str) -> dict:
        """Watch an organisation (ROR id), a researcher (ORCID iD; researcher scope) or a CORDIS project
        (PROGRAMME:id) for registry changes, new asserted works and new datasets."""
        from src.kb.research_entities_monitoring import ResearchEntityMonitor

        return safe(lambda conn: ResearchEntityMonitor(conn).create(
            namespace, request_key, watch=watch, key=key, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def run_research_entities_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a research-entities monitor at a committed watermark; notices cite the new and previous record
        revision, state what changed and carry minimised fields only."""
        from src.kb.research_entities_monitoring import ResearchEntityMonitor

        return safe(lambda conn: ResearchEntityMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]),
            write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def poll_research_entities_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Delivered research-entities monitor events after a cursor."""
        from src.kb.research_entities_monitoring import ResearchEntityMonitor

        return safe(lambda conn: ResearchEntityMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor),
            required_scope="knowledge:subscriptions:read")
