"""Cultural primary sources in Science/Research and Geospatial (DDB, Europeana).

Acquisition runs through the scientific source pack (``primary-scientific-evidence``)
with the ``ddb`` and ``europeana`` sources. Objects stay their own records;
links to scholarly works need a citation, provider relation or review, and
places come only from provider coordinates or gazetteer-resolved names.
"""

CULTURAL_WRITES = {
    "propose_cultural_matches", "review_cultural_match", "link_cultural_research",
    "suggest_cultural_research_candidates", "review_cultural_research_candidate", "acquire_cultural_assets",
}
CULTURAL_TOOLS = CULTURAL_WRITES | {
    "cultural_readiness", "cultural_source_contracts", "cultural_rights_policy", "search_cultural_objects",
    "inspect_cultural_object", "primary_sources_for_work",
}
CULTURAL_SCOPES = {
    "cultural_source_contracts": [], "cultural_rights_policy": [],
    "review_cultural_match": ["knowledge:cultural:review"],
    "review_cultural_research_candidate": ["knowledge:cultural:review"],
}


def required_scopes(tool_name, mutability):
    return CULTURAL_SCOPES.get(
        tool_name, ["knowledge:cultural:write" if mutability == "write" else "knowledge:cultural:read"])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def store(conn):
        from src.kb.cultural import CulturalStore

        return CulturalStore(conn)

    @mcp.tool()
    def cultural_source_contracts() -> dict:
        """DDB and Europeana access, identifiers, pagination, rights semantics and live-verification state."""
        from src.ingestion.cultural_sources import PROVIDER_CONTRACTS

        return {"contracts": PROVIDER_CONTRACTS}

    @mcp.tool()
    def cultural_rights_policy(statement: str, purpose: str = "research") -> dict:
        """How an item rights statement governs metadata, asset storage and export (unclear means link-only)."""
        from src.kb.cultural import rights_policy

        return rights_policy(statement, purpose=purpose)

    @mcp.tool()
    def cultural_readiness() -> dict:
        """Per-provider readiness of the cultural sources inside the scientific source pack."""
        from src.kb.cultural import readiness

        return safe(lambda conn: readiness(conn), required_scope="knowledge:cultural:read")

    @mcp.tool()
    def search_cultural_objects(namespace: str, place_id: str | None = None, geometry_ids: list[str] | None = None,
                                collection: str | None = None, creator: str | None = None,
                                subject: str | None = None, date_from: str | None = None,
                                date_to: str | None = None, provider: str | None = None, limit: int = 25) -> dict:
        """Objects by place, collection, creator, subject or declared date range, with rights and source links."""
        return safe(lambda conn: store(conn).search(
            namespace, scopes=who()[1], place_id=place_id, geometry_ids=geometry_ids, collection=collection,
            creator=creator, subject=subject, date_from=date_from, date_to=date_to, provider=provider, limit=limit),
            required_scope="knowledge:cultural:read")

    @mcp.tool()
    def inspect_cultural_object(namespace: str, object_id: str) -> dict:
        """One object: provider record, revisions, rights per representation, places with role and precision."""
        return safe(lambda conn: store(conn).object(namespace, object_id, scopes=who()[1]),
                    required_scope="knowledge:cultural:read")

    @mcp.tool()
    def primary_sources_for_work(namespace: str, work_kind: str, work_id: str) -> dict:
        """Start from a scholarly work: linked primary objects, open candidates and their geometries/places."""
        return safe(lambda conn: store(conn).primary_sources_for_work(namespace, work_kind, work_id, scopes=who()[1]),
                    required_scope="knowledge:cultural:read")

    @mcp.tool()
    def propose_cultural_matches(namespace: str) -> dict:
        """Explicit identifier matches plus conservative multi-field candidates across providers."""
        return safe(lambda conn: store(conn).propose_matches(namespace, scopes=who()[1]), write=True,
                    required_scope="knowledge:cultural:write")

    @mcp.tool()
    def review_cultural_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept, reject or defer a cross-provider object match; provider records stay separate."""
        return safe(lambda conn: store(conn).review_match(namespace, match_id, decision, reason, scopes=who()[1],
                                                          principal_id=who()[0]),
                    write=True, required_scope="knowledge:cultural:review")

    @mcp.tool()
    def link_cultural_research(namespace: str, object_id: str, work_kind: str, work_id: str, basis: str,
                               evidence: str) -> dict:
        """Link an object to a scholarly work by explicit citation, provider relation or reviewed assertion."""
        return safe(lambda conn: store(conn).link_research(namespace, object_id, work_kind, work_id, basis, evidence,
                                                           scopes=who()[1], principal_id=who()[0]),
                    write=True, required_scope="knowledge:cultural:write")

    @mcp.tool()
    def suggest_cultural_research_candidates(namespace: str, work_kind: str, work_id: str, text: str,
                                             limit: int = 10) -> dict:
        """Keyword-overlap candidates between a work and objects; suggestions only, never links."""
        return safe(lambda conn: store(conn).suggest_research_candidates(
            namespace, work_kind, work_id, text, scopes=who()[1], principal_id=who()[0], limit=limit),
            write=True, required_scope="knowledge:cultural:write")

    @mcp.tool()
    def review_cultural_research_candidate(namespace: str, link_id: str, decision: str, reason: str) -> dict:
        """Accept (becomes a reviewed link) or reject a candidate research link."""
        return safe(lambda conn: store(conn).review_research_candidate(
            namespace, link_id, decision, reason, scopes=who()[1], principal_id=who()[0]),
            write=True, required_scope="knowledge:cultural:review")

    @mcp.tool()
    def acquire_cultural_assets(namespace: str, object_id: str, action: str = "store", purpose: str = "research",
                                allowed_hosts: list[str] | None = None) -> dict:
        """Retain representations only where item rights permit; otherwise record link-only."""
        return safe(lambda conn: store(conn).acquire_assets(
            namespace, object_id, scopes=who()[1], principal_id=who()[0], action=action, purpose=purpose,
            allowed_hosts=allowed_hosts or []), write=True, required_scope="knowledge:cultural:write")
