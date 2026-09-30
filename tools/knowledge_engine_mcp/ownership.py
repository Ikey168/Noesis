"""Corporate Ownership and Registries entry points: acquisition, lookup, identity, graph, timeline, dossier.

Every tool except the contract listing checks the bundle's enablement first;
shared providers (market.lei, entity identity, source runtime, authored
reports) are never gated by it. Ownership assertions are what sources state;
no tool infers beneficial ownership or makes a sanctions or AML determination,
and identity matches are reviewable decisions, never automatic merges.
"""

from src.kb.ownership_bundle import BUNDLE, readiness, require_enabled, set_enabled

OWNERSHIP_WRITES = {
    "set_ownership_bundle_enabled", "acquire_ownership_sources", "record_ownership_register_document",
    "propose_ownership_identity_matches", "review_ownership_identity_match", "revert_ownership_identity_match",
    "export_ownership_dossier",
}
OWNERSHIP_TOOLS = OWNERSHIP_WRITES | {
    "ownership_bundle_status", "ownership_provider_contracts", "lookup_ownership_entity",
    "inspect_ownership_record", "ownership_record_history", "list_ownership_identity_candidates",
    "ownership_graph", "ownership_timeline", "ownership_state_as_of", "build_ownership_dossier",
}
OWNERSHIP_SCOPES = {
    "ownership_provider_contracts": [],
    "set_ownership_bundle_enabled": ["operator"],
    "acquire_ownership_sources": ["knowledge:ownership:write", "knowledge:ingestion:execute"],
    "review_ownership_identity_match": ["knowledge:ownership:review"],
    "revert_ownership_identity_match": ["knowledge:ownership:review"],
    "export_ownership_dossier": ["knowledge:ownership:read", "knowledge:reports:write"],
}

# Competition cases and state aid (#2217): the optional competition feature's tools, registered here.
from tools.knowledge_engine_mcp.competition import (  # noqa: E402
    COMPETITION_SCOPES,
    COMPETITION_TOOLS,
    COMPETITION_WRITES,
)
from tools.knowledge_engine_mcp.competition import register as register_competition  # noqa: E402

OWNERSHIP_WRITES = OWNERSHIP_WRITES | COMPETITION_WRITES
OWNERSHIP_TOOLS = OWNERSHIP_TOOLS | COMPETITION_TOOLS
OWNERSHIP_SCOPES = {**OWNERSHIP_SCOPES, **COMPETITION_SCOPES}


def required_scopes(tool_name, mutability):
    return OWNERSHIP_SCOPES.get(
        tool_name, ["knowledge:ownership:write" if mutability == "write" else "knowledge:ownership:read"])


def register(mcp, safe, context):
    register_competition(mcp, safe, context)

    def who():
        return context()[0], context()[1]

    def gated(namespace, operation, *, write=False, scope="knowledge:ownership:read"):
        def run(conn):
            require_enabled(conn, namespace)
            return operation(conn)
        return safe(run, write=write, required_scope=scope)

    def market_context(market_namespace, security_ids, acquired_by_ms):
        if not market_namespace:
            return None
        principal, scopes = who()
        return {"namespace": market_namespace, "security_ids": list(security_ids or []),
                "acquired_by_ms": acquired_by_ms, "principal_id": principal, "scopes": scopes}

    @mcp.tool()
    def ownership_bundle_status(namespace: str) -> dict:
        """Declared contributions plus per-provider record presence, live-verification state and readiness."""
        return safe(lambda conn: {**readiness(conn, namespace, scopes=who()[1]), "declaration": BUNDLE},
                    required_scope="knowledge:ownership:read")

    @mcp.tool()
    def set_ownership_bundle_enabled(namespace: str, enabled: bool) -> dict:
        """Enable/disable Corporate Ownership (a coordinator selection change once composed); shared providers stay."""
        return safe(lambda conn: set_enabled(conn, namespace, enabled, principal_id=who()[0], scopes=who()[1]),
                    write=True, required_scope="operator")

    @mcp.tool()
    def ownership_provider_contracts() -> dict:
        """Per-provider access contracts, identifiers carried, not-implemented registers and live-verification state."""
        from src.ingestion.ownership_providers import IDENTIFIER_OVERLAP, LIVE_VERIFICATION, PROVIDER_CONTRACTS
        return {"contracts": PROVIDER_CONTRACTS, "identifiers": IDENTIFIER_OVERLAP, "live_verification": LIVE_VERIFICATION}

    @mcp.tool()
    def acquire_ownership_sources(namespace: str, run_key: str, source_ids: list[str] | None = None) -> dict:
        """Bounded live acquisition of the installed corporate-ownership source pack through the source runtime."""
        def run(conn):
            import os

            from src.kb.ownership_bundle import acquire
            principal, scopes = who()
            return acquire(conn, namespace, run_key=run_key, principal_id=principal, scopes=scopes,
                           source_ids=source_ids, network="live", secret_resolver=lambda ref: os.environ.get(ref))
        return gated(namespace, run, write=True, scope="knowledge:ownership:write")

    @mcp.tool()
    def record_ownership_register_document(namespace: str, provider: str, register: str, number: str,
                                           jurisdiction: str, document: dict, name: str | None = None,
                                           status: str | None = None, registered_on: str | None = None) -> dict:
        """Record a registration from an official register document the user obtained (no register is scraped)."""
        from src.kb.ownership_store import OwnershipStore
        return gated(namespace, lambda conn: OwnershipStore(conn).record_register_document(
            namespace, provider=provider, register=register, number=number, jurisdiction=jurisdiction,
            document=document, name=name, status=status, registered_on=registered_on,
            principal_id=who()[0], scopes=who()[1]), write=True, scope="knowledge:ownership:write")

    @mcp.tool()
    def lookup_ownership_entity(namespace: str, scheme: str, value: str) -> dict:
        """Entities carrying an identifier (lei, gb-coh, sec-cik, ...) or name|country candidates (never a pick)."""
        from src.kb.ownership_store import OwnershipStore
        return gated(namespace, lambda conn: OwnershipStore(conn, initialize=False).lookup(
            namespace, scheme, value, principal_id=who()[0], scopes=who()[1]))

    @mcp.tool()
    def inspect_ownership_record(namespace: str, record_id: str, revision: int | None = None) -> dict:
        """One ownership record revision with its source, locator and unknowns."""
        from src.kb.ownership_store import OwnershipStore
        return gated(namespace, lambda conn: OwnershipStore(conn, initialize=False).get(
            namespace, record_id, principal_id=who()[0], scopes=who()[1], revision=revision))

    @mcp.tool()
    def ownership_record_history(namespace: str, record_id: str) -> dict:
        """Every revision of an ownership record with run and observation time."""
        from src.kb.ownership_store import OwnershipStore
        return gated(namespace, lambda conn: {"revisions": OwnershipStore(conn, initialize=False).history(
            namespace, record_id, principal_id=who()[0], scopes=who()[1])})

    @mcp.tool()
    def propose_ownership_identity_matches(namespace: str, lei_namespace: str | None = None,
                                           market_namespace: str | None = None, as_of_ms: int | None = None) -> dict:
        """Propose reviewable identity candidates (exact, cross-referenced, name+jurisdiction); nothing is merged."""
        from src.kb.ownership_identity import OwnershipIdentityService

        def run(conn):
            principal, scopes = who()
            market = None if not market_namespace else {
                "namespace": market_namespace, "as_of_ms": as_of_ms, "acquired_by_ms": as_of_ms,
                "principal_id": principal, "scopes": scopes}
            return OwnershipIdentityService(conn).propose(namespace, principal_id=principal, scopes=scopes,
                                                          market=market, lei_namespace=lei_namespace)
        return gated(namespace, run, write=True, scope="knowledge:ownership:write")

    @mcp.tool()
    def list_ownership_identity_candidates(namespace: str, state: str | None = None,
                                           record_key: str | None = None) -> dict:
        """Identity candidates with basis, evidence, confidence, state and the recorded decision."""
        from src.kb.ownership_identity import OwnershipIdentityService
        return gated(namespace, lambda conn: {"candidates": OwnershipIdentityService(conn, initialize=False).candidates(
            namespace, scopes=who()[1], state=state, record_key=record_key)})

    @mcp.tool()
    def review_ownership_identity_match(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a candidate as an entity identity decision (match / non-match)."""
        from src.kb.ownership_identity import OwnershipIdentityService
        return gated(namespace, lambda conn: OwnershipIdentityService(conn).review(
            namespace, candidate_id, decision, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:ownership:review")

    @mcp.tool()
    def revert_ownership_identity_match(namespace: str, candidate_id: str, reason: str) -> dict:
        """Undo an accepted or rejected identity decision; both records stay intact and auditable."""
        from src.kb.ownership_identity import OwnershipIdentityService
        return gated(namespace, lambda conn: OwnershipIdentityService(conn).revert(
            namespace, candidate_id, reason, principal_id=who()[0], scopes=who()[1]),
            write=True, scope="knowledge:ownership:review")

    @mcp.tool()
    def ownership_graph(namespace: str, query: str, entity: str, as_of: str | None = None, max_depth: int = 10,
                        known_at_ms: int | None = None) -> dict:
        """direct_parents / ultimate_parents / subsidiaries / control_chain / successor_chain as of a date."""
        from src.kb.ownership_graph import query as run_query
        return gated(namespace, lambda conn: run_query(conn, namespace, query, entity, principal_id=who()[0],
                                                       scopes=who()[1], as_of=as_of, max_depth=max_depth,
                                                       known_at_ms=known_at_ms))

    @mcp.tool()
    def ownership_timeline(namespace: str, entity: str, known_at_ms: int | None = None,
                           market_namespace: str | None = None, security_ids: list[str] | None = None,
                           market_acquired_by_ms: int | None = None) -> dict:
        """Cited registrations, officers, ownership changes, filings, market corporate actions and temporal assertions."""
        from src.kb.ownership_timeline import timeline
        return gated(namespace, lambda conn: timeline(
            conn, namespace, entity, principal_id=who()[0], scopes=who()[1], known_at_ms=known_at_ms,
            market=market_context(market_namespace, security_ids, market_acquired_by_ms)))

    @mcp.tool()
    def ownership_state_as_of(namespace: str, entity: str, date: str, known_at_ms: int | None = None) -> dict:
        """Registration, officers and ownership in force at a date, from revisions known at a record time."""
        from src.kb.ownership_timeline import state_as_of
        return gated(namespace, lambda conn: state_as_of(conn, namespace, entity, date, principal_id=who()[0],
                                                         scopes=who()[1], known_at_ms=known_at_ms))

    @mcp.tool()
    def build_ownership_dossier(namespace: str, scheme: str, value: str, as_of: str | None = None,
                                known_at_ms: int | None = None, evidence_kind: str = "unspecified") -> dict:
        """An explained, pinned ownership dossier for one identifier, with conflicts and unknowns visible."""
        from src.kb.ownership_dossier import build_dossier
        return gated(namespace, lambda conn: build_dossier(conn, namespace, scheme, value, principal_id=who()[0],
                                                           scopes=who()[1], as_of=as_of, known_at_ms=known_at_ms,
                                                           evidence_kind=evidence_kind))

    @mcp.tool()
    def export_ownership_dossier(namespace: str, scheme: str, value: str, request_key: str,
                                 as_of: str | None = None, evidence_kind: str = "unspecified") -> dict:
        """Save the dossier as an authored report with one citation per source record revision."""
        from src.kb.ownership_dossier import build_dossier, export_dossier

        def run(conn):
            principal, scopes = who()
            dossier = build_dossier(conn, namespace, scheme, value, principal_id=principal, scopes=scopes,
                                    as_of=as_of, evidence_kind=evidence_kind)
            return export_dossier(conn, dossier, request_key, principal_id=principal, scopes=scopes)
        return gated(namespace, run, write=True, scope="knowledge:reports:write")
