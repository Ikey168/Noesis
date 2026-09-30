"""Climate and Environment biodiversity entry points (optional ``biodiversity`` feature, #2220 BD11 #2530).

Registered through :func:`tools.knowledge_engine_mcp.environment.register`, so
the tools live in the knowledge-engine server beside the other Climate and
Environment tools and share its bundle gate (Geospatial, subscriptions and the
source runtime are never gated by it). Exclusions (#2220): no species
distribution modelling, no abundance or presence/absence estimation, no
location more precise than the publisher released, no Noesis-derived threat
status; IUCN data stay at the reference-only licence tier.
"""

from __future__ import annotations

from src.kb.biodiversity_records import NEVER_SENTENCE

BIODIVERSITY_WRITES = {
    "register_biodiversity_schemas", "propose_taxon_identity_matches", "review_taxon_identity_match",
    "revert_taxon_identity_match", "link_biodiversity_records", "create_biodiversity_monitor",
    "run_biodiversity_monitor",
}
BIODIVERSITY_TOOLS = BIODIVERSITY_WRITES | {
    "biodiversity_source_contracts", "biodiversity_readiness", "lookup_taxa", "occurrences_for_taxon_or_place",
    "conservation_status_history", "list_taxon_identity_matches", "list_biodiversity_links",
    "export_biodiversity_bundle", "poll_biodiversity_monitor",
}
BIODIVERSITY_SCOPES = {
    "biodiversity_source_contracts": [],
    "register_biodiversity_schemas": ["knowledge:environment:write", "knowledge:schema:register"],
    "review_taxon_identity_match": ["knowledge:environment:review", "knowledge:entity-history:review"],
    "revert_taxon_identity_match": ["knowledge:environment:review", "knowledge:entity-history:execute"],
    "link_biodiversity_records": ["knowledge:environment:write", "knowledge:geospatial:write",
                                  "knowledge:geospatial:calculate"],
    "create_biodiversity_monitor": ["knowledge:environment:read", "knowledge:subscriptions:write"],
    "run_biodiversity_monitor": ["knowledge:environment:read", "knowledge:subscriptions:write"],
    "poll_biodiversity_monitor": ["knowledge:environment:read", "knowledge:subscriptions:read"],
}


def required_scopes(tool_name, mutability):
    return BIODIVERSITY_SCOPES.get(
        tool_name, ["knowledge:environment:write" if mutability == "write" else "knowledge:environment:read"])


def feature_enabled(conn) -> bool:
    """Whether the Climate and Environment bundle's optional ``biodiversity`` feature is in the active plan."""
    import json

    try:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN ('composition_authority', "
            "'composition_active', 'composition_generations', 'composition_plans')").fetchall()}
        if len(tables) < 4:
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g ON g.generation_id="
            "a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1").fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return "biodiversity" in ((plan.get("features") or {}).get("climate-environment") or [])


def readiness(conn, namespace):
    from src.ingestion.biodiversity_sources import IUCN_LICENCE_DECISION, LIVE_VERIFICATION, PROVIDER_CONTRACTS
    from src.kb.biodiversity_store import TABLES, BiodiversityStore, table_exists

    store = BiodiversityStore(conn, initialize=False)
    return {"feature": "biodiversity", "bundle": "climate-environment", "selected": feature_enabled(conn),
            "default": False, "stores": {t: table_exists(conn, t) for t in TABLES},
            "providers": {p: {"licence": c["licence"], "live_verification": LIVE_VERIFICATION[p]["status"],
                              "state": store.provider_state(namespace, p) if store.ready() else None}
                          for p, c in PROVIDER_CONTRACTS.items()},
            "iucn_licence_decision": IUCN_LICENCE_DECISION["decision"], "boundary": NEVER_SENTENCE}


def _require(scopes, required):
    from src.kb.biodiversity_records import require

    require(scopes, *required)


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def gated(namespace, tool, operation, *, write=False):
        """Declared scopes first, then the Climate and Environment bundle gate, then the operation."""
        required = required_scopes(tool, "write" if write else "read")

        def run(conn):
            from src.kb.environment_bundle import require_enabled

            _require(who()[1], required)
            require_enabled(conn, namespace)
            return operation(conn)

        return safe(run, write=write, required_scope=required[0] if required else None)

    @mcp.tool()
    def biodiversity_source_contracts() -> dict:
        """CoL, GBIF and IUCN access contracts, the IUCN reference-only licence decision, sensitive-species policy,
        bounded coverage and live state. No distribution modelling, abundance or de-generalised locations."""
        from src.ingestion.biodiversity_sources import (
            BOUNDED_COVERAGE,
            IUCN_LICENCE_DECISION,
            LIVE_VERIFICATION,
            PROVIDER_CONTRACTS,
            SENSITIVE_SPECIES_POLICY,
        )

        return {"contracts": PROVIDER_CONTRACTS, "iucn_licence_decision": IUCN_LICENCE_DECISION,
                "sensitive_species_policy": SENSITIVE_SPECIES_POLICY, "bounded_coverage": BOUNDED_COVERAGE,
                "live_verification": LIVE_VERIFICATION, "boundary": NEVER_SENTENCE}

    @mcp.tool()
    def biodiversity_readiness(namespace: str) -> dict:
        """Whether the optional biodiversity feature is selected, which stores exist and per-provider state."""
        return gated(namespace, "biodiversity_readiness", lambda conn: readiness(conn, namespace))

    @mcp.tool()
    def register_biodiversity_schemas(namespace: str) -> dict:
        """Register noesis-biodiversity-record-v1 in the schema registry."""
        from src.kb.biodiversity_records import register_schemas

        return gated(namespace, "register_biodiversity_schemas", lambda conn: {
            "modules": register_schemas(conn, principal_id=who()[0], scopes=who()[1])}, write=True)

    @mcp.tool()
    def lookup_taxa(namespace: str, query: str) -> dict:
        """Provider taxa for a scientific name, native key or provider:key: names, ranks, statuses and checklist
        versions as published, accepted/open identity matches and dated status changes between releases."""
        from src.kb.biodiversity_queries import lookup_taxa as run

        return gated(namespace, "lookup_taxa", lambda conn: run(conn, namespace, query, scopes=who()[1]))

    @mcp.tool()
    def occurrences_for_taxon_or_place(namespace: str, taxon: str | None = None, place_id: str | None = None,
                                       as_of: str | None = None) -> dict:
        """Occurrence records on record at a date for a taxon and/or place, each citing occurrence, dataset, licence
        and retrieval time; uncertain records apart; counts are record counts, never abundance or presence/absence."""
        from src.kb.biodiversity_queries import occurrences

        return gated(namespace, "occurrences_for_taxon_or_place", lambda conn: occurrences(
            conn, namespace, scopes=who()[1], taxon=taxon, place_id=place_id, as_of=as_of))

    @mcp.tool()
    def conservation_status_history(namespace: str, taxon: str, as_of: str | None = None) -> dict:
        """Red List assessments in date order per scope with the assessor-designated current one, published
        category changes (never a trend) and the reference-only licence; 'not assessed on record' is not NE/DD."""
        from src.kb.biodiversity_queries import status_history

        return gated(namespace, "conservation_status_history", lambda conn: status_history(
            conn, namespace, taxon, scopes=who()[1], as_of=as_of))

    @mcp.tool()
    def propose_taxon_identity_matches(namespace: str) -> dict:
        """Offer taxon identity candidates across CoL, GBIF and IUCN for review (none accepted); split/lump conflicts
        are surfaced, never resolved."""
        from src.kb.biodiversity_identity import BiodiversityIdentity

        return gated(namespace, "propose_taxon_identity_matches", lambda conn: BiodiversityIdentity(conn).propose(
            namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_taxon_identity_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a taxon identity proposal with a reason; records reviewer, time and checklist versions."""
        from src.kb.biodiversity_identity import BiodiversityIdentity

        return gated(namespace, "review_taxon_identity_match", lambda conn: BiodiversityIdentity(conn).review(
            namespace, match_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def revert_taxon_identity_match(namespace: str, match_id: str, reason: str) -> dict:
        """Revert a reviewed taxon identity decision (the entity identity decision is undone)."""
        from src.kb.biodiversity_identity import BiodiversityIdentity

        return gated(namespace, "revert_taxon_identity_match", lambda conn: BiodiversityIdentity(conn).revert(
            namespace, match_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def list_taxon_identity_matches(namespace: str, state: str | None = None, subject_key: str | None = None) -> dict:
        """Taxon identity proposals and decisions with basis, evidence class and conflicts."""
        from src.kb.biodiversity_identity import BiodiversityIdentity

        return gated(namespace, "list_taxon_identity_matches", lambda conn: {"matches": BiodiversityIdentity(
            conn, initialize=False).matches(namespace, scopes=who()[1], state=state, subject_key=subject_key)})

    @mcp.tool()
    def link_biodiversity_records(namespace: str, place_ids: list[str] | None = None) -> dict:
        """Link occurrences to places at their published precision (or by exact country code) and to datasets, and
        datasets/assessments to papers by exact DOI; never geocodes locality text or de-generalises coordinates."""
        from src.kb.biodiversity_links import BiodiversityLinks

        def run(conn):
            links = BiodiversityLinks(conn)
            places = links.link_places(namespace, principal_id=who()[0], scopes=who()[1], place_ids=place_ids)
            citations = links.link_citations(namespace, principal_id=who()[0], scopes=who()[1])
            return {"places": places, "citations": citations}

        return gated(namespace, "link_biodiversity_records", run, write=True)

    @mcp.tool()
    def list_biodiversity_links(namespace: str, record_id: str | None = None, target_id: str | None = None) -> dict:
        """Place, dataset and citation links, each with the source revision it came from."""
        from src.kb.biodiversity_links import BiodiversityLinks

        return gated(namespace, "list_biodiversity_links", lambda conn: {"links": BiodiversityLinks(
            conn, initialize=False).links(namespace, scopes=who()[1], record_id=record_id, target_id=target_id)})

    @mcp.tool()
    def export_biodiversity_bundle(namespace: str, taxon: str | None = None, place_id: str | None = None,
                                   as_of: str | None = None) -> dict:
        """The biodiversity section of a Climate and Environment place or taxon bundle; IUCN rows reduced to
        citation level for export."""
        from src.kb.environment_bundle import biodiversity_section

        return gated(namespace, "export_biodiversity_bundle", lambda conn: biodiversity_section(
            conn, namespace, scopes=who()[1], taxon=taxon, place_id=place_id, as_of=as_of))

    @mcp.tool()
    def create_biodiversity_monitor(namespace: str, request_key: str, taxa: list[str] | None = None,
                                    places: list[str] | None = None, datasets: list[str] | None = None,
                                    delivery: dict | None = None) -> dict:
        """Watch taxa, places or datasets for new/changed/removed occurrences, status changes and new assessments."""
        from src.kb.biodiversity_monitoring import BiodiversityMonitor

        return gated(namespace, "create_biodiversity_monitor", lambda conn: BiodiversityMonitor(conn).create(
            namespace, request_key, principal_id=who()[0], scopes=who()[1], taxa=taxa, places=places,
            datasets=datasets, delivery=delivery), write=True)

    @mcp.tool()
    def run_biodiversity_monitor(namespace: str, subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a biodiversity monitor at a complete source-pack watermark; replays deliver nothing."""
        from src.kb.biodiversity_monitoring import BiodiversityMonitor

        return gated(namespace, "run_biodiversity_monitor", lambda conn: BiodiversityMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def poll_biodiversity_monitor(namespace: str, subscription_id: str, cursor: str = "") -> dict:
        """Poll biodiversity monitor events after a cursor."""
        from src.kb.biodiversity_monitoring import BiodiversityMonitor

        return gated(namespace, "poll_biodiversity_monitor", lambda conn: BiodiversityMonitor(
            conn, initialize=False).poll(subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))
