"""Patent publications for Research and Technical (EPO OPS).

Acquisition runs through the research source pack (source ``epo-ops-patents``).
Results are provider publication and legal-event data; no validity,
enforceability or freedom-to-operate conclusion is drawn.
"""

PATENT_WRITES = {"propose_patent_link", "review_patent_link"}
PATENT_TOOLS = PATENT_WRITES | {"patent_source_contracts", "inspect_patent_publication", "patent_family_members",
                                "patent_links"}
PATENT_SCOPES = {"patent_source_contracts": [], "review_patent_link": ["knowledge:patents:review"]}


def required_scopes(tool_name, mutability):
    return PATENT_SCOPES.get(
        tool_name, ["knowledge:patents:write" if mutability == "write" else "knowledge:patents:read"])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def store(conn):
        from src.kb.patents import PatentStore

        return PatentStore(conn)

    @mcp.tool()
    def patent_source_contracts() -> dict:
        """The implemented EPO OPS path and why Espacenet and WIPO PATENTSCOPE are not implemented."""
        from src.ingestion.patent_sources import PROVIDER_CONTRACTS

        return {"contracts": PROVIDER_CONTRACTS}

    @mcp.tool()
    def inspect_patent_publication(namespace: str, docdb: str) -> dict:
        """One publication: biblio revisions, provider-asserted family, legal events (or not_available), claims, links."""
        return safe(lambda conn: store(conn).publication(namespace, docdb, scopes=who()[1]),
                    required_scope="knowledge:patents:read")

    @mcp.tool()
    def patent_family_members(namespace: str, docdb: str) -> dict:
        """Family members asserted by the provider, split into acquired and not acquired."""
        return safe(lambda conn: store(conn).family_members(namespace, docdb, scopes=who()[1]),
                    required_scope="knowledge:patents:read")

    @mcp.tool()
    def patent_links(namespace: str, target_id: str) -> dict:
        """Publications linked to a scholarly work (doi:...), organization or standard, with basis and state."""
        def run(conn):
            from src.kb.patents import READ_SCOPE, _authorize

            _authorize(namespace, who()[1], READ_SCOPE, write=False)
            return {"target_id": target_id, "links": store(conn).links(namespace, target_id=target_id)}
        return safe(run, required_scope="knowledge:patents:read")

    @mcp.tool()
    def propose_patent_link(namespace: str, docdb: str, target_kind: str, target_id: str, evidence: str) -> dict:
        """A reviewable candidate link from a publication to an organization, standard or scholarly work."""
        return safe(lambda conn: store(conn).propose_link(namespace, docdb, target_kind, target_id, evidence,
                                                          scopes=who()[1], principal_id=who()[0]),
                    write=True, required_scope="knowledge:patents:write")

    @mcp.tool()
    def review_patent_link(namespace: str, link_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a candidate patent link."""
        return safe(lambda conn: store(conn).review_link(namespace, link_id, decision, reason, scopes=who()[1],
                                                         principal_id=who()[0]),
                    write=True, required_scope="knowledge:patents:review")
