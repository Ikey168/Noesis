"""Standards catalogue editions and certification records for Technical (ISO Open Data + explicit imports).

Acquisition runs through the technical source pack (source ``iso-open-data``).
Standards text is never stored; certificates enter by explicit import until a
certification registry is validated.
"""

STANDARDS_WRITES = {"import_certificates", "propose_certificate_product_links", "review_certificate_product_link"}
STANDARDS_TOOLS = STANDARDS_WRITES | {"standards_source_contracts", "inspect_standard_edition", "inspect_certificate"}
STANDARDS_SCOPES = {"standards_source_contracts": [],
                    "review_certificate_product_link": ["knowledge:standards:review"]}


def required_scopes(tool_name, mutability):
    return STANDARDS_SCOPES.get(
        tool_name, ["knowledge:standards:write" if mutability == "write" else "knowledge:standards:read"])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def store(conn):
        from src.kb.standards import StandardsStore

        return StandardsStore(conn)

    @mcp.tool()
    def standards_source_contracts() -> dict:
        """ISO Open Data access and why ETSI and certification registries are not wired yet."""
        from src.ingestion.standards_sources import PROVIDER_CONTRACTS

        return {"contracts": PROVIDER_CONTRACTS}

    @mcp.tool()
    def inspect_standard_edition(namespace: str, reference: str) -> dict:
        """A standard edition: stage/status, supersession and amendment relations, certificates citing it."""
        return safe(lambda conn: store(conn).standard(namespace, reference, scopes=who()[1]),
                    required_scope="knowledge:standards:read")

    @mcp.tool()
    def inspect_certificate(namespace: str, certificate_number: str, as_of: str | None = None) -> dict:
        """All imported records for a certificate, with validity on a date and status conflicts."""
        return safe(lambda conn: store(conn).certificate(namespace, certificate_number, scopes=who()[1], as_of=as_of),
                    required_scope="knowledge:standards:read")

    @mcp.tool()
    def import_certificates(namespace: str, source: str, records: list[dict]) -> dict:
        """Import certificate or conformity-declaration records from a named source with locators."""
        return safe(lambda conn: store(conn).import_certificates(namespace, source, records, scopes=who()[1],
                                                                 principal_id=who()[0]),
                    write=True, required_scope="knowledge:standards:write")

    @mcp.tool()
    def propose_certificate_product_links(namespace: str) -> dict:
        """Candidates where a certificate names a product brand + designation held by a Products model."""
        return safe(lambda conn: store(conn).propose_product_links(namespace, scopes=who()[1],
                                                                   principal_id=who()[0]),
                    write=True, required_scope="knowledge:standards:write")

    @mcp.tool()
    def review_certificate_product_link(namespace: str, link_id: str, decision: str) -> dict:
        """Accept or reject a certificate-to-product candidate link."""
        return safe(lambda conn: store(conn).review_product_link(namespace, link_id, decision, scopes=who()[1]),
                    write=True, required_scope="knowledge:standards:review")
