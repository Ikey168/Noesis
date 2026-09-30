"""Science life-sciences entry points (provider ``science.life-sciences``, #2652 LS12 #2711).

UniProt, NCBI Gene and Taxonomy, RCSB PDB and ChEMBL records - each an optional
Science feature, default off - with entries as of a release and their
cross-reference graph, published activity against a target, reviewable
identity, cross-pack links, evidence-bundle export and subscription monitors.
Acquisition runs through the shared source-pack tools (pack
``primary-scientific-evidence``: sources ``uniprot-proteins``,
``ncbi-genes-taxonomy``, ``rcsb-pdb-structures`` and ``chembl-bioactivity``).

Exclusions (declared by every answering tool): no biological or clinical
inference, no activity prediction, no sequence analysis beyond storage, no
conversion or aggregation of activity values, no redistribution beyond each
source's licence, and no person names (the LS01 minimisation decision is
enforced on every output).
"""

from __future__ import annotations

READ = "knowledge:lifesci:read"
WRITE = "knowledge:lifesci:write"
REVIEW = "knowledge:lifesci:review"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
EXCLUSIONS_NOTE = ("Exclusions: no biological or clinical inference, no activity prediction, no sequence analysis, "
                   "values never converted or aggregated, no person names.")

LIFESCI_WRITES = {
    "register_lifesci_schemas", "propose_lifesci_identity_matches", "review_lifesci_identity_match",
    "revert_lifesci_identity_match", "link_lifesci_records", "create_lifesci_monitor", "run_lifesci_monitor",
}
LIFESCI_READS = {
    "lifesci_source_contracts", "lifesci_readiness", "lifesci_entry_as_of", "lifesci_target_activities",
    "list_lifesci_identity_matches", "list_lifesci_links", "export_lifesci_evidence_bundle", "poll_lifesci_monitor",
}
LIFESCI_TOOLS = LIFESCI_WRITES | LIFESCI_READS
LIFESCI_SCOPES = {
    "lifesci_source_contracts": [],
    "lifesci_readiness": [READ],
    "lifesci_entry_as_of": [READ],
    "lifesci_target_activities": [READ],
    "list_lifesci_identity_matches": [READ],
    "list_lifesci_links": [READ],
    "export_lifesci_evidence_bundle": [READ],
    "poll_lifesci_monitor": [READ, SUBSCRIPTIONS_READ],
    "register_lifesci_schemas": [WRITE, "knowledge:schema:register"],
    "propose_lifesci_identity_matches": [WRITE],
    "review_lifesci_identity_match": [REVIEW, "knowledge:entity-history:review"],
    "revert_lifesci_identity_match": [REVIEW, "knowledge:entity-history:execute"],
    "link_lifesci_records": [WRITE],
    "create_lifesci_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_lifesci_monitor": [READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return LIFESCI_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def readiness(conn, namespace="global"):
    from src.ingestion.lifesci_sources import LIVE_VERIFICATION, PROVIDER_CONTRACTS
    from src.kb.lifesci_records import (
        FEATURES,
        MINIMISATION,
        NEVER_SENTENCE,
        feature_state,
    )
    from src.kb.lifesci_store import TABLES, LifeSciStore, table_exists

    store = LifeSciStore(conn, initialize=False)
    selected = feature_state(conn)
    return {"provider": "science.life-sciences", "bundle": "science",
            "features": {p: {"feature": f, "selected": selected[p], "default": False} for p, f in FEATURES.items()},
            "stores": {t: table_exists(conn, t) for t in TABLES},
            "providers": {p: {"licence": c["licence"], "live_verification": LIVE_VERIFICATION[p]["status"],
                              "state": store.provider_state(namespace, p) if store.ready() else None}
                          for p, c in PROVIDER_CONTRACTS.items()},
            "linked_packs": {"chemicals.substances": table_exists(conn, "substance_identifiers"),
                             "environment.biodiversity": table_exists(conn, "biodiversity_revisions"),
                             "clinical.medicines": table_exists(conn, "clinical_records")},
            "minimisation": MINIMISATION["decision"], "boundary": NEVER_SENTENCE,
            "note": "offline fixture evidence and live evidence are reported per run (evidence_origin); no provider is "
                    "live until a dated run verifies it"}


def _declared(answer):
    """Declare the exclusions and enforce the minimisation decision on every dict answer."""
    from src.ingestion.lifesci_sources import EXCLUSIONS
    from src.kb.lifesci_records import MINIMISATION, LifeSciError, forbidden_keys

    if not isinstance(answer, dict):
        return answer
    leaks = forbidden_keys(answer)
    if leaks:
        raise LifeSciError("boundary", f"output carries excluded or personal fields: {leaks[:5]}")
    return {**answer, "exclusions": list(EXCLUSIONS), "minimisation": MINIMISATION["decision"]}


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def run_tool(tool, operation, *, write=False):
        """Checks every declared scope, runs the operation, declares exclusions and enforces minimisation."""
        scopes = LIFESCI_SCOPES[tool]

        def run(conn):
            from src.kb.lifesci_records import require

            require(who()[1], *scopes)
            return _declared(operation(conn))

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def lifesci_source_contracts() -> dict:
        """Per-source access decisions for UniProt, NCBI Gene and Taxonomy, RCSB PDB and ChEMBL: endpoints,
        authentication and key handling, licence and redistribution, rate limits, revision and removal model, the
        data-minimisation decision, bounded coverage and LIVE_VERIFICATION per source.
        Exclusions: no biological or clinical inference, no activity prediction, no sequence analysis, values never
        converted or aggregated, no person names."""
        from src.ingestion.lifesci_sources import (
            BOUNDED_COVERAGE,
            EXCLUSIONS,
            LIVE_VERIFICATION,
            NOT_IMPLEMENTED,
            PROVIDER_CONTRACTS,
        )
        from src.kb.lifesci_records import MINIMISATION, NEVER_SENTENCE

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "bounded_coverage": BOUNDED_COVERAGE, "minimisation": MINIMISATION, "not_implemented": NOT_IMPLEMENTED,
                "never": NEVER_SENTENCE, "exclusions": list(EXCLUSIONS)}

    @mcp.tool()
    def lifesci_readiness(namespace: str = "global") -> dict:
        """Which life-sciences features (UniProt, NCBI, PDB, ChEMBL; each optional, default off) are selected, the
        stores, per-provider runs and live state, and whether Chemicals, Biodiversity and Clinical medicines are
        installed for links (absent packs are reported, never required)."""
        return run_tool("lifesci_readiness", lambda conn: readiness(conn, namespace))

    @mcp.tool()
    def lifesci_entry_as_of(namespace: str, identifier: str, release: str | None = None,
                            as_of: str | None = None) -> dict:
        """An accession (or Gene ID, Tax ID, PDB ID, ChEMBL ID) as of a release (UniProt YYYY_MM, ChEMBL_nn) or an
        ISO date: the entry and sequence version in force (UniSave history when the full entry was not acquired),
        obsolete/merged/replaced identifiers resolved to their successors with the history shown, and the
        cross-reference graph labelled by asserting source with identity states and cross-pack links; every entry
        version cited.
        Exclusions: no biological or clinical inference, no activity prediction, no sequence analysis, values never
        converted or aggregated, no person names."""
        from src.kb.lifesci_queries import LifeSciQueries

        return run_tool("lifesci_entry_as_of", lambda conn: LifeSciQueries(conn).entry_as_of(
            namespace, identifier, scopes=who()[1], release=release, as_of=as_of))

    @mcp.tool()
    def lifesci_target_activities(namespace: str, target: str, release: str | None = None,
                                  as_of: str | None = None) -> dict:
        """Compounds and activities ChEMBL published against a target (ChEMBL target ID, or a UniProt accession a
        target component names) for one release: published and ChEMBL-standardised type, relation, value and unit
        as strings, data-validity and activity comments, each row citing its activity and document; removed
        activities listed apart.
        Exclusions: no biological or clinical inference, no activity prediction, no sequence analysis, values never
        converted or aggregated, no person names."""
        from src.kb.lifesci_queries import LifeSciQueries

        return run_tool("lifesci_target_activities", lambda conn: LifeSciQueries(conn).target_activities(
            namespace, target, scopes=who()[1], release=release, as_of=as_of))

    @mcp.tool()
    def export_lifesci_evidence_bundle(namespace: str, identifier: str | None = None, target: str | None = None,
                                       release: str | None = None, as_of: str | None = None) -> dict:
        """An entry-as-of (identifier) or target-activities (target) answer as a noesis-evidence-bundle-v1 citing
        every item with source, record revision, release and as-of time; unknowns are omissions.
        Exclusions: no biological or clinical inference, no activity prediction, no sequence analysis, values never
        converted or aggregated, no person names."""
        from src.kb.lifesci_queries import LifeSciQueries, export_bundle
        from src.kb.lifesci_records import LifeSciError

        def op(conn):
            queries = LifeSciQueries(conn)
            if bool(identifier) == bool(target):
                raise LifeSciError("invalid_request", "give an identifier or a target")
            answer = (queries.entry_as_of(namespace, identifier, scopes=who()[1], release=release, as_of=as_of)
                      if identifier else queries.target_activities(namespace, target, scopes=who()[1],
                                                                   release=release, as_of=as_of))
            return {"bundle": export_bundle(answer), "answer_status": answer["status"]}

        return run_tool("export_lifesci_evidence_bundle", op)

    @mcp.tool()
    def list_lifesci_identity_matches(namespace: str, state: str | None = None, subject_key: str | None = None) -> dict:
        """Identity proposals and decisions across UniProt, NCBI, PDB, ChEMBL, Chemicals (InChIKey) and Biodiversity
        (taxa) with method, evidence, confidence and review history; nothing is merged."""
        from src.kb.lifesci_identity import LifeSciIdentity

        return run_tool("list_lifesci_identity_matches", lambda conn: {"matches": LifeSciIdentity(
            conn, initialize=False).matches(namespace, scopes=who()[1], state=state, subject_key=subject_key)})

    @mcp.tool()
    def list_lifesci_links(namespace: str, record_id: str | None = None, target_kind: str | None = None) -> dict:
        """Links from life-science record revisions to Chemicals substances, Biodiversity occurrences, Clinical
        medicines and literature, each with its basis (citation, shared identifier or accepted match)."""
        from src.kb.lifesci_links import LifeSciLinks

        return run_tool("list_lifesci_links", lambda conn: {"links": LifeSciLinks(conn, initialize=False).links(
            namespace, scopes=who()[1], record_id=record_id, target_kind=target_kind)})

    @mcp.tool()
    def poll_lifesci_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a life-sciences monitor's delivered events."""
        from src.kb.lifesci_monitoring import LifeSciMonitor

        return run_tool("poll_lifesci_monitor", lambda conn: LifeSciMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))

    # ------------------------------------------------------------------ writes

    @mcp.tool()
    def register_lifesci_schemas(namespace: str = "global") -> dict:
        """Register noesis-lifesci-record-v1 in the schema registry."""
        from src.kb.lifesci_records import register_schemas

        del namespace
        return run_tool("register_lifesci_schemas", lambda conn: {
            "modules": register_schemas(conn, principal_id=who()[0], scopes=who()[1])}, write=True)

    @mcp.tool()
    def propose_lifesci_identity_matches(namespace: str) -> dict:
        """Offer identity candidates from published cross-references (UniProt to PDB, Gene and ChEMBL targets),
        ChEMBL compound to Chemicals substance by InChIKey and NCBI taxon to Biodiversity taxon; names only for taxa
        without an identifier. None is accepted; unmatched records are listed."""
        from src.kb.lifesci_identity import LifeSciIdentity

        return run_tool("propose_lifesci_identity_matches", lambda conn: LifeSciIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_lifesci_identity_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a life-sciences identity proposal with a reason (an entity identity decision)."""
        from src.kb.lifesci_identity import LifeSciIdentity

        return run_tool("review_lifesci_identity_match", lambda conn: LifeSciIdentity(conn).review(
            namespace, match_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def revert_lifesci_identity_match(namespace: str, match_id: str, reason: str) -> dict:
        """Revert a reviewed life-sciences identity decision (the entity identity decision is undone)."""
        from src.kb.lifesci_identity import LifeSciIdentity

        return run_tool("revert_lifesci_identity_match", lambda conn: LifeSciIdentity(conn).revert(
            namespace, match_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def link_lifesci_records(namespace: str) -> dict:
        """Link record revisions to Chemicals substances and Biodiversity occurrences (accepted matches only),
        Clinical medicines that name the ChEMBL ID and papers citing the same DOI or PubMed ID; missing packs are
        reported. No inferred drug-target or disease claim."""
        from src.kb.lifesci_links import LifeSciLinks

        return run_tool("link_lifesci_records", lambda conn: LifeSciLinks(conn).link(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def create_lifesci_monitor(namespace: str, request_key: str, accessions: list[str] | None = None,
                               targets: list[str] | None = None, taxa: list[str] | None = None,
                               delivery: dict | None = None) -> dict:
        """Subscribe to accessions, targets or taxa: notices of new releases that change an entry, obsolete it or
        add, revise or remove activities (record changes, never assessments)."""
        from src.kb.lifesci_monitoring import LifeSciMonitor

        return run_tool("create_lifesci_monitor", lambda conn: LifeSciMonitor(conn).create(
            namespace, request_key, principal_id=who()[0], scopes=who()[1], accessions=accessions, targets=targets,
            taxa=taxa, delivery=delivery), write=True)

    @mcp.tool()
    def run_lifesci_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a life-sciences monitor at a complete source-pack watermark; notices cite the prior and new
        revision and state what changed. Replays deliver nothing."""
        from src.kb.lifesci_monitoring import LifeSciMonitor

        return run_tool("run_lifesci_monitor", lambda conn: LifeSciMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True)
