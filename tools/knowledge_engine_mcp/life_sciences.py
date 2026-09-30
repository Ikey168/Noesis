"""Science life-sciences provider's entry points (#2652, LS12 #2711): reference records as of a release with their
cross-reference graph, compounds with published activity against a target, reviewable identity, cross-pack links,
evidence bundles and subscription monitors.

Acquisition runs through the shared source-pack tools (pack ``primary-scientific-evidence``: sources
``uniprot-lifesci-proteins``, ``ncbi-gene-lifesci``, ``ncbi-taxonomy-lifesci``, ``rcsb-pdb-lifesci-structures`` and
``chembl-lifesci-bioactivity``). Every answer item cites its source, record revision and as-of time.

Exclusions (declared by every answering tool): no biological or clinical inference, no activity prediction or
ranking, no conversion or aggregation of activity values, no sequence analysis beyond storage, no redistribution
beyond each source's licence, and no personal names (authors, depositors), which are also stripped from every output.
"""

READ = "knowledge:lifesci:read"
WRITE = "knowledge:lifesci:write"
REVIEW = "knowledge:lifesci:review"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"

LIFESCI_WRITES = {
    "propose_lifesci_matches",
    "review_lifesci_match",
    "revert_lifesci_match",
    "link_lifesci_records",
    "create_lifesci_monitor",
    "run_lifesci_monitor",
}
LIFESCI_READS = {
    "lifesci_source_contracts",
    "lifesci_readiness",
    "lifesci_entry_as_of",
    "lifesci_compounds_for_target",
    "list_lifesci_identity_matches",
    "list_lifesci_unmatched",
    "list_lifesci_links",
    "export_lifesci_evidence_bundle",
    "poll_lifesci_monitor",
}
LIFESCI_TOOLS = LIFESCI_WRITES | LIFESCI_READS
LIFESCI_SCOPES = {
    "lifesci_source_contracts": [],
    "lifesci_readiness": [READ],
    "lifesci_entry_as_of": [READ],
    "lifesci_compounds_for_target": [READ],
    "list_lifesci_identity_matches": [READ],
    "list_lifesci_unmatched": [READ],
    "list_lifesci_links": [READ],
    "export_lifesci_evidence_bundle": [READ],
    "poll_lifesci_monitor": [READ, SUBSCRIPTIONS_READ],
    "propose_lifesci_matches": [WRITE],
    "review_lifesci_match": [REVIEW],
    "revert_lifesci_match": [REVIEW],
    "link_lifesci_records": [WRITE],
    "create_lifesci_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_lifesci_monitor": [READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return LIFESCI_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _require(scopes, *required):
    from src.kb.lifesci_records import LifeSciError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise LifeSciError("unauthorized", f"{', '.join(missing)} required")


def _declared(answer):
    """Declare the exclusions and enforce the minimisation decision on every output."""
    from src.kb.lifesci_records import EXCLUSIONS, minimised

    if isinstance(answer, dict):
        return {**minimised(answer), "exclusions": list(EXCLUSIONS)}
    return minimised(answer)


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def run_tool(tool, operation, *, write=False):
        scopes = LIFESCI_SCOPES[tool]

        def run(conn):
            _require(who()[1], *scopes)
            return _declared(operation(conn))

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def lifesci_source_contracts() -> dict:
        """Per-source access decisions for UniProt, NCBI Gene and Taxonomy, RCSB PDB and ChEMBL: endpoints,
        authentication and key handling, licences, rate limits, revision models, the bounded coverage and the
        personal-data minimisation decision.
        Exclusions: no biological or clinical inference, no activity prediction, no conversion or aggregation of
        values, no sequence analysis, no personal names."""
        from src.ingestion.lifesci_sources import (
            BOUNDED_COVERAGE,
            EXCLUSIONS,
            LIVE_VERIFICATION,
            NEVER_SENTENCE,
            PERSONAL_DATA_DECISION,
            PROVIDER_CONTRACTS,
        )

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "bounded_coverage": BOUNDED_COVERAGE, "personal_data": PERSONAL_DATA_DECISION,
                "never": NEVER_SENTENCE, "exclusions": list(EXCLUSIONS)}

    @mcp.tool()
    def lifesci_readiness(namespace: str = "global") -> dict:
        """Which life-sciences features (UniProt, NCBI, PDB, ChEMBL) are selected, whether the stores are ready,
        records per source, live-verification status and which linked packs (Chemicals, Clinical, Biodiversity,
        literature) are present."""
        from src.kb.lifesci_links import owner_status
        from src.kb.lifesci_records import authorize
        from src.kb.lifesci_store import readiness

        def op(conn):
            authorize(namespace, who()[1], READ)
            return {**readiness(conn, namespace), "linked_packs": owner_status(conn)}

        return run_tool("lifesci_readiness", op)

    @mcp.tool()
    def lifesci_entry_as_of(namespace: str, accession: str, release: str | None = None,
                            as_of: str | None = None) -> dict:
        """A gene, protein, structure, taxon or ChEMBL entry (UniProt accession, PDB ID, ChEMBL ID, or
        ncbi-gene:/ncbi-taxonomy: plus the numeric ID) as of a release label and/or date: the version in force, its
        history, merged or obsolete accessions resolved to successors, and the cross-reference graph with each
        cross-reference labelled by the source that asserts it; every entry version cited.
        Exclusions: no biological or clinical inference, no activity prediction, no conversion or aggregation of
        values, no sequence analysis, no personal names."""
        from src.kb.lifesci_queries import LifeSciQueries

        return run_tool("lifesci_entry_as_of", lambda conn: LifeSciQueries(conn).entry(
            namespace, accession, scopes=who()[1], release=release, as_of=as_of))

    @mcp.tool()
    def lifesci_compounds_for_target(namespace: str, target: str, release: str | None = None) -> dict:
        """Compounds and activities ChEMBL published against a target (ChEMBL target ID, or a UniProt accession
        through the published target components) for a release, grouped side by side by assay type and activity
        type with relation, value, unit and data-validity comment as published; each activity and its document cited.
        Exclusions: no biological or clinical inference, no activity prediction, no conversion or aggregation of
        values, no sequence analysis, no personal names."""
        from src.kb.lifesci_queries import LifeSciQueries

        return run_tool("lifesci_compounds_for_target", lambda conn: LifeSciQueries(conn).compounds_for_target(
            namespace, target, scopes=who()[1], release=release))

    @mcp.tool()
    def export_lifesci_evidence_bundle(namespace: str, accession: str | None = None, target: str | None = None,
                                       release: str | None = None, as_of: str | None = None) -> dict:
        """An evidence bundle for an entry answer (accession) or a target answer (target): every item with its
        source, record revision and as-of time.
        Exclusions: no biological or clinical inference, no activity prediction, no conversion or aggregation of
        values, no sequence analysis, no personal names."""
        from src.kb.lifesci_queries import LifeSciQueries
        from src.kb.lifesci_records import LifeSciError

        def op(conn):
            queries = LifeSciQueries(conn)
            if bool(accession) == bool(target):
                raise LifeSciError("invalid_request", "name an accession or a target")
            answer = (queries.entry(namespace, accession, scopes=who()[1], release=release, as_of=as_of)
                      if accession else queries.compounds_for_target(namespace, target, scopes=who()[1],
                                                                     release=release))
            return queries.export_bundle(answer)

        return run_tool("export_lifesci_evidence_bundle", op)

    @mcp.tool()
    def list_lifesci_identity_matches(namespace: str, record_id: str | None = None, state: str | None = None,
                                      right_kind: str | None = None) -> dict:
        """Cross-source identity matches with method, evidence, confidence and review state (proposed, accepted,
        rejected, reverted). Nothing is merged."""
        from src.kb.lifesci_identity import LifeSciIdentity

        return run_tool("list_lifesci_identity_matches", lambda conn: {"matches": LifeSciIdentity(
            conn, initialize=False).matches(namespace, scopes=who()[1], record_id=record_id, state=state,
                                           right_kind=right_kind)})

    @mcp.tool()
    def list_lifesci_unmatched(namespace: str, record_type: str | None = None) -> dict:
        """Records without an accepted identity match, kept visible and addressable by their own accession."""
        from src.kb.lifesci_identity import LifeSciIdentity

        return run_tool("list_lifesci_unmatched", lambda conn: LifeSciIdentity(conn, initialize=False).unmatched(
            namespace, scopes=who()[1], record_type=record_type))

    @mcp.tool()
    def list_lifesci_links(namespace: str, record_id: str | None = None, owner: str | None = None) -> dict:
        """Links to Chemicals, Clinical, Biodiversity and literature records with their basis (citation, shared
        identifier or accepted match), both revisions and status (resolved or target_missing)."""
        from src.kb.lifesci_links import LifeSciLinks

        return run_tool("list_lifesci_links", lambda conn: {"links": LifeSciLinks(conn, initialize=False).links(
            namespace, scopes=who()[1], record_id=record_id, owner=owner)})

    @mcp.tool()
    def poll_lifesci_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a life-science monitor's events (record changes cited to their revisions)."""
        from src.kb.lifesci_monitoring import LifeSciMonitor

        return run_tool("poll_lifesci_monitor", lambda conn: LifeSciMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))

    @mcp.tool()
    def propose_lifesci_matches(namespace: str) -> dict:
        """Propose identity matches from published cross-references, InChIKeys (Chemicals) and published NCBI Tax
        IDs (Biodiversity); exact scientific names only as low-confidence candidates. Nothing is accepted or
        merged automatically."""
        from src.kb.lifesci_identity import LifeSciIdentity

        return run_tool("propose_lifesci_matches", lambda conn: LifeSciIdentity(conn).propose(
            namespace, scopes=who()[1], principal_id=who()[0]), write=True)

    @mcp.tool()
    def review_lifesci_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a proposed match with a reason (an entity-history decision, reversible)."""
        from src.kb.lifesci_identity import LifeSciIdentity

        return run_tool("review_lifesci_match", lambda conn: LifeSciIdentity(conn).review(
            namespace, match_id, decision, reason, scopes=who()[1], principal_id=who()[0]), write=True)

    @mcp.tool()
    def revert_lifesci_match(namespace: str, match_id: str, reason: str) -> dict:
        """Revert an accepted or rejected match (appends an entity-history undo)."""
        from src.kb.lifesci_identity import LifeSciIdentity

        return run_tool("revert_lifesci_match", lambda conn: LifeSciIdentity(conn).revert(
            namespace, match_id, reason, scopes=who()[1], principal_id=who()[0]), write=True)

    @mcp.tool()
    def link_lifesci_records(namespace: str) -> dict:
        """Link held records to Chemicals, Clinical, Biodiversity and literature by citation, shared identifier or
        accepted match; absent providers are reported and missing targets kept. No drug-target or disease claim."""
        from src.kb.lifesci_links import LifeSciLinks

        return run_tool("link_lifesci_records", lambda conn: LifeSciLinks(conn).link_all(
            namespace, scopes=who()[1], principal_id=who()[0]), write=True)

    @mcp.tool()
    def create_lifesci_monitor(namespace: str, request_key: str, accession: str | None = None,
                               target: str | None = None, taxon: str | None = None) -> dict:
        """Subscribe to one accession, target or NCBI taxon; notices cite new or revised records. No new
        scheduler."""
        from src.kb.lifesci_monitoring import LifeSciMonitor

        watch = {k: v for k, v in {"accession": accession, "target": target, "taxon": taxon}.items() if v}
        return run_tool("create_lifesci_monitor", lambda conn: LifeSciMonitor(conn).create(
            namespace, request_key, watch=watch, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def run_lifesci_monitor(subscription_id: str) -> dict:
        """Evaluate a life-science monitor at the committed watermark; replays emit nothing."""
        from src.kb.lifesci_monitoring import LifeSciMonitor

        return run_tool("run_lifesci_monitor", lambda conn: LifeSciMonitor(conn).run(
            subscription_id, principal_id=who()[0], scopes=who()[1]), write=True)
