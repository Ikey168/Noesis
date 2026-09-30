"""Chemicals and Substances entry points: acquisition, identity, citations, status, dossiers and monitors (#2212, CH11).

Every tool except the source contracts checks the bundle's enablement first;
Legal, Products, entity identity, subscriptions and the source runtime are
never gated by it. No tool produces a hazard verdict, safety advice, an
exposure assessment or synthesis information, and a substance without
entries is always reported as having none on record.
"""

from src.kb.substances_bundle import BUNDLE, readiness, require_enabled, set_enabled

SUBSTANCE_WRITES = {
    "set_chemicals_bundle_enabled", "acquire_substance_sources", "propose_substance_identities",
    "review_substance_identity", "revert_substance_identity", "propose_substance_identity_manual",
    "link_substance_citations", "create_substance_monitor", "run_substance_monitor",
}
SUBSTANCE_READS = {
    "chemicals_bundle_status", "substance_source_contracts", "resolve_substance", "substance_status_as_of",
    "substance_history", "substance_dossier", "list_substance_identity_candidates", "list_unmatched_substances",
    "list_substance_links", "poll_substance_monitor",
}
SUBSTANCE_TOOLS = SUBSTANCE_WRITES | SUBSTANCE_READS
SUBSTANCE_SCOPES = {
    "set_chemicals_bundle_enabled": ["operator"],
    "acquire_substance_sources": ["knowledge:substances:write", "knowledge:ingestion:execute"],
    "review_substance_identity": ["knowledge:substances:review"],
    "revert_substance_identity": ["knowledge:substances:review"],
    "propose_substance_identity_manual": ["knowledge:substances:review"],
    "link_substance_citations": ["knowledge:substances:write", "knowledge:legal:read", "knowledge:products:read",
                                 "knowledge:read"],
    "create_substance_monitor": ["knowledge:substances:read", "knowledge:subscriptions:write"],
    "run_substance_monitor": ["knowledge:substances:read", "knowledge:subscriptions:write"],
    "poll_substance_monitor": ["knowledge:substances:read", "knowledge:subscriptions:read"],
}


def required_scopes(tool_name, mutability):
    if tool_name == "substance_source_contracts":
        return []
    return SUBSTANCE_SCOPES.get(
        tool_name, ["knowledge:substances:write" if mutability == "write" else "knowledge:substances:read"])


def _also(scopes, required):
    missing = [scope for scope in required if scope not in scopes]
    if missing and "operator" not in scopes:
        from src.kb.substances_records import SubstanceError

        raise SubstanceError("unauthorized", f"{missing[0]} scope is required")


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def gated(namespace, operation, *, write=False, scope="knowledge:substances:read", also=()):
        def run(conn):
            require_enabled(conn, namespace)
            _also(who()[1], also)
            return operation(conn)
        return safe(run, write=write, required_scope=scope)

    @mcp.tool()
    def chemicals_bundle_status(namespace: str) -> dict:
        """Declared contributions plus ready/fixture-only/unavailable per provider; live verification kept separate."""
        return safe(lambda conn: {**readiness(conn, namespace, scopes=who()[1]), "declaration": BUNDLE},
                    required_scope="knowledge:substances:read")

    @mcp.tool()
    def set_chemicals_bundle_enabled(namespace: str, enabled: bool) -> dict:
        """Enable/disable Chemicals and Substances (a coordinator selection change once composed)."""
        return safe(lambda conn: set_enabled(conn, namespace, enabled, principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="operator")

    @mcp.tool()
    def substance_source_contracts() -> dict:
        """Per-provider access contracts (endpoints, auth, licence, attribution, limits, revision behaviour)."""
        from src.ingestion.substance_sources import EXCLUDED_FIELDS, LIVE_VERIFICATION, PROVIDER_CONTRACTS
        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "excluded_fields": list(EXCLUDED_FIELDS),
                "audit": "docs/roadmaps/chemicals-substances-source-audit.md"}

    @mcp.tool()
    def acquire_substance_sources(namespace: str, run_key: str, source_ids: list[str] | None = None) -> dict:
        """Bounded, receipted run of the chemicals-substances source pack's explicit selection (secrets from NOESIS_*)."""
        def run(conn):
            from src.config.env import resolve_env
            from src.ingestion.source_pack_runtime import SourcePackRuntime

            request = {"pack_id": "chemicals-substances", "run_key": run_key, "operation": "substances",
                       **({"source_ids": list(source_ids)} if source_ids else {})}
            return SourcePackRuntime(conn).run(request, principal_id=who()[0], secret_resolver=resolve_env)
        return gated(namespace, run, write=True, scope="knowledge:substances:write",
                     also=("knowledge:ingestion:execute",))

    @mcp.tool()
    def resolve_substance(namespace: str, query: str) -> dict:
        """Name, CAS, EC, index number, InChIKey or DTXSID to reviewable substances with the matches joining them."""
        from src.kb.substances_identity import SubstanceIdentity
        return gated(namespace, lambda conn: SubstanceIdentity(conn, initialize=False).resolve(
            namespace, query, scopes=who()[1]))

    @mcp.tool()
    def substance_status_as_of(namespace: str, query: str | None = None, subject_key: str | None = None,
                               as_of: str | None = None) -> dict:
        """Harmonised classification, SVHC, Annex XIV/XVII and registration status in force on a date, cited."""
        from src.kb.substances_queries import SubstanceQueries
        return gated(namespace, lambda conn: SubstanceQueries(conn).status_as_of(
            namespace, scopes=who()[1], as_of=as_of, query=query, subject_key=subject_key))

    @mcp.tool()
    def substance_history(namespace: str, query: str | None = None, subject_key: str | None = None) -> dict:
        """Every classification, list and registration revision with dates and citations."""
        from src.kb.substances_queries import SubstanceQueries
        return gated(namespace, lambda conn: SubstanceQueries(conn).history(
            namespace, scopes=who()[1], query=query, subject_key=subject_key))

    @mcp.tool()
    def substance_dossier(namespace: str, query: str | None = None, subject_key: str | None = None,
                          as_of: str | None = None) -> dict:
        """Cited dossier: identity, status as of a date, history, regulation text, linked notices, data points."""
        from src.kb.substances_queries import SubstanceQueries
        return gated(namespace, lambda conn: SubstanceQueries(conn).dossier(
            namespace, scopes=who()[1], as_of=as_of, query=query, subject_key=subject_key))

    @mcp.tool()
    def propose_substance_identities(namespace: str) -> dict:
        """Candidate matches across PubChem, ECHA and CompTox by exact identifier, InChIKey or synonym (none accepted)."""
        from src.kb.substances_identity import SubstanceIdentity
        return gated(namespace, lambda conn: SubstanceIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True, scope="knowledge:substances:write")

    @mcp.tool()
    def review_substance_identity(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a candidate as an entity identity decision, with a reason; records are never merged."""
        from src.kb.substances_identity import SubstanceIdentity
        return gated(namespace, lambda conn: SubstanceIdentity(conn).review(
            namespace, candidate_id, decision, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:substances:review")

    @mcp.tool()
    def revert_substance_identity(namespace: str, candidate_id: str, reason: str) -> dict:
        """Undo an accepted or rejected identity decision; both records stay intact."""
        from src.kb.substances_identity import SubstanceIdentity
        return gated(namespace, lambda conn: SubstanceIdentity(conn).revert(
            namespace, candidate_id, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:substances:review")

    @mcp.tool()
    def propose_substance_identity_manual(namespace: str, left_key: str, right_key: str, evidence: str) -> dict:
        """A reviewer's explicit proposal (the only way a group entry, mixture or salt joins another record)."""
        from src.kb.substances_identity import SubstanceIdentity
        return gated(namespace, lambda conn: SubstanceIdentity(conn).propose_manual(
            namespace, left_key, right_key, evidence, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:substances:review")

    @mcp.tool()
    def list_substance_identity_candidates(namespace: str, state: str | None = None,
                                           subject_key: str | None = None) -> dict:
        """Identity candidates with basis, evidence and review state."""
        from src.kb.substances_identity import SubstanceIdentity
        return gated(namespace, lambda conn: {"candidates": SubstanceIdentity(conn, initialize=False).candidates(
            namespace, scopes=who()[1], state=state, subject_key=subject_key)})

    @mcp.tool()
    def list_unmatched_substances(namespace: str) -> dict:
        """Provider records no accepted identity match connects, with their identifiers."""
        from src.kb.substances_identity import SubstanceIdentity
        return gated(namespace, lambda conn: SubstanceIdentity(conn, initialize=False).unmatched(
            namespace, scopes=who()[1]))

    @mcp.tool()
    def link_substance_citations(namespace: str, targets: list[str] | None = None) -> dict:
        """Link cited legal acts (exact CELEX/ELI), product notices and literature by explicit citation only."""
        from src.kb.substances_links import SubstanceLinks

        def run(conn):
            links = SubstanceLinks(conn)
            chosen = set(targets or ["legal", "products", "literature"])
            result = {}
            if "legal" in chosen:
                result["legal"] = links.link_legal(namespace, scopes=who()[1], principal_id=who()[0])
            if "products" in chosen:
                result["products"] = links.link_product_notices(namespace, scopes=who()[1], principal_id=who()[0])
            if "literature" in chosen:
                result["literature"] = links.link_literature(namespace, scopes=who()[1], principal_id=who()[0])
            return result
        return gated(namespace, run, write=True, scope="knowledge:substances:write",
                     also=("knowledge:legal:read", "knowledge:products:read", "knowledge:read"))

    @mcp.tool()
    def list_substance_links(namespace: str, subject_key: str, owner: str | None = None) -> dict:
        """Citation links (legal, products, materials, clinical) of one substance and its reviewed matches."""
        from src.kb.substances_identity import SubstanceIdentity
        from src.kb.substances_links import SubstanceLinks

        def run(conn):
            members = SubstanceIdentity(conn, initialize=False).members(namespace, subject_key)
            return {"members": members, "links": SubstanceLinks(conn, initialize=False).links(
                namespace, members, scopes=who()[1], owner=owner)}
        return gated(namespace, run)

    @mcp.tool()
    def create_substance_monitor(namespace: str, request_key: str, substances: list[str],
                                 delivery: dict | None = None) -> dict:
        """Watch substances for new ATP classifications, SVHC and Annex XIV/XVII events and linked notices."""
        from src.kb.substances_monitoring import SubstanceMonitor
        return gated(namespace, lambda conn: SubstanceMonitor(conn).create(
            namespace, request_key, substances=substances, principal_id=who()[0], scopes=who()[1],
            delivery=delivery), write=True, scope="knowledge:substances:read",
            also=("knowledge:subscriptions:write",))

    @mcp.tool()
    def run_substance_monitor(namespace: str, subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a monitor at a committed complete run (latest when omitted); replay creates no new events."""
        from src.kb.substances_monitoring import SubstanceMonitor
        return gated(namespace, lambda conn: SubstanceMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:substances:read", also=("knowledge:subscriptions:write",))

    @mcp.tool()
    def poll_substance_monitor(namespace: str, subscription_id: str, cursor: str = "") -> dict:
        """Poll delivered monitor events after a cursor."""
        from src.kb.substances_monitoring import SubstanceMonitor
        return gated(namespace, lambda conn: SubstanceMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor),
            also=("knowledge:subscriptions:read",))
