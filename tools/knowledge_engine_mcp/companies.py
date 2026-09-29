"""LEI company identities and ownership assertions (GLEIF) linked to existing company records.

Acquisition runs through the economic source pack (source ``gleif-lei``).
OpenCorporates (regional provider) stays enrichment; links between an LEI and
other records are reviewable candidates, never automatic merges.
"""

COMPANY_WRITES = {"propose_lei_registry_links", "link_company_identity", "review_company_identity_link"}
COMPANY_TOOLS = COMPANY_WRITES | {"company_source_contracts", "inspect_lei_entity", "lei_parents_as_of"}
COMPANY_SCOPES = {"company_source_contracts": [], "review_company_identity_link": ["knowledge:companies:review"]}


def required_scopes(tool_name, mutability):
    return COMPANY_SCOPES.get(
        tool_name, ["knowledge:companies:write" if mutability == "write" else "knowledge:companies:read"])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def store(conn):
        from src.kb.lei import LeiStore

        return LeiStore(conn)

    @mcp.tool()
    def company_source_contracts() -> dict:
        """GLEIF access, the reused OpenCorporates provider and why official registers are not wired."""
        from src.ingestion.lei_sources import PROVIDER_CONTRACTS

        return {"contracts": PROVIDER_CONTRACTS}

    @mcp.tool()
    def inspect_lei_entity(namespace: str, lei: str) -> dict:
        """An LEI's names, legal vs headquarters address, register pointer, status, parents, exceptions and links."""
        return safe(lambda conn: store(conn).entity(namespace, lei, scopes=who()[1]),
                    required_scope="knowledge:companies:read")

    @mcp.tool()
    def lei_parents_as_of(namespace: str, lei: str, as_of_ms: int) -> dict:
        """Parent assertions observed by a time, with conflicting assertions kept."""
        return safe(lambda conn: store(conn).parents_as_of(namespace, lei, as_of_ms, scopes=who()[1]),
                    required_scope="knowledge:companies:read")

    @mcp.tool()
    def propose_lei_registry_links(namespace: str) -> dict:
        """Candidates between LEIs and existing OpenCorporates records on jurisdiction + registry number."""
        return safe(lambda conn: store(conn).propose_registry_links(namespace, scopes=who()[1], principal_id=who()[0]),
                    write=True, required_scope="knowledge:companies:write")

    @mcp.tool()
    def link_company_identity(namespace: str, lei: str, external_kind: str, external_id: str, evidence: str) -> dict:
        """Propose a link from an LEI to an OpenCorporates record, market issuer or register entry."""
        return safe(lambda conn: store(conn).link_identity(namespace, lei, external_kind, external_id, evidence,
                                                           scopes=who()[1], principal_id=who()[0]),
                    write=True, required_scope="knowledge:companies:write")

    @mcp.tool()
    def review_company_identity_link(namespace: str, link_id: str, decision: str, reason: str) -> dict:
        """Accept or reject an LEI identity link."""
        return safe(lambda conn: store(conn).review_link(namespace, link_id, decision, reason, scopes=who()[1],
                                                         principal_id=who()[0]),
                    write=True, required_scope="knowledge:companies:review")
