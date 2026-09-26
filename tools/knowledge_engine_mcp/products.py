"""Products pack entry points: readiness, model lookup, matching review, documents and comparison.

Acquisition itself runs through the shared source-pack tools
(``run_source_pack_execution`` with pack ``products-displays``). Values are
provider assertions (brand content or supplier registrations), never
independent tests or buying recommendations.
"""

PRODUCT_WRITES = {"propose_product_matches", "review_product_match", "acquire_product_documents"}
PRODUCT_TOOLS = PRODUCT_WRITES | {
    "products_readiness", "product_provider_contracts", "lookup_product_models",
    "inspect_product_identity", "product_selection_outcomes", "compare_product_models",
}
PRODUCT_SCOPES = {
    "product_provider_contracts": [],
    "products_readiness": ["knowledge:products:read"],
    "propose_product_matches": ["knowledge:products:write"],
    "review_product_match": ["knowledge:products:review"],
    "acquire_product_documents": ["knowledge:products:write"],
}


def required_scopes(tool_name, mutability):
    return PRODUCT_SCOPES.get(
        tool_name, ["knowledge:products:write" if mutability == "write" else "knowledge:products:read"])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def store(conn):
        from src.kb.products import ProductStore

        return ProductStore(conn)

    @mcp.tool()
    def product_provider_contracts() -> dict:
        """Pinned Open Icecat and EPREL access contracts, catalogue boundaries and live-verification state."""
        from src.ingestion.product_sources import ASSERTION_KINDS, PROVIDER_CONTRACTS

        return {"contracts": PROVIDER_CONTRACTS, "assertion_kinds": ASSERTION_KINDS}

    @mcp.tool()
    def products_readiness() -> dict:
        """Per-provider fixture/live readiness; one ready provider never implies cross-source validation."""
        from src.kb.products import readiness

        return safe(lambda conn: readiness(conn), required_scope="knowledge:products:read")

    @mcp.tool()
    def lookup_product_models(namespace: str, query: str | None = None, brand: str | None = None,
                              designation: str | None = None, gtin: str | None = None,
                              provider: str | None = None, limit: int = 25) -> dict:
        """Find product variants by brand, exact designation, GTIN or text, with identifier conflicts and matches."""
        return safe(lambda conn: store(conn).lookup(
            namespace, scopes=who()[1], query=query, brand=brand, designation=designation, gtin=gtin,
            provider=provider, limit=limit), required_scope="knowledge:products:read")

    @mcp.tool()
    def inspect_product_identity(namespace: str, identity_id: str) -> dict:
        """A model or variant with revisions, current and superseded assertions, documents, coverage and matches."""
        return safe(lambda conn: store(conn).inspect(namespace, identity_id, scopes=who()[1]),
                    required_scope="knowledge:products:read")

    @mcp.tool()
    def product_selection_outcomes(namespace: str, run_id: str) -> dict:
        """Per-selector outcomes of one run: returned, not_found or outside_open_catalogue."""
        def run(conn):
            from src.kb.products import READ_SCOPE, _authorize

            _authorize(namespace, who()[1], READ_SCOPE, write=False)
            return {"run_id": run_id, "outcomes": store(conn).selection_outcomes(namespace, run_id)}
        return safe(run, required_scope="knowledge:products:read")

    @mcp.tool()
    def propose_product_matches(namespace: str) -> dict:
        """Deterministic Icecat/EPREL match candidates from identifiers and corroborating attributes; none auto-accepted."""
        return safe(lambda conn: store(conn).propose_matches(namespace, scopes=who()[1], principal_id=who()[0]),
                    write=True, required_scope="knowledge:products:write")

    @mcp.tool()
    def review_product_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept, reject or defer a match candidate; history is append-only and reversible."""
        return safe(lambda conn: store(conn).review_match(
            namespace, match_id, decision, reason, scopes=who()[1], principal_id=who()[0]),
            write=True, required_scope="knowledge:products:review")

    @mcp.tool()
    def acquire_product_documents(namespace: str, variant_id: str) -> dict:
        """Fetch provider-linked datasheets/information sheets of one variant within the pack's document policy."""
        def run(conn):
            from src.kb.products import acquire_documents, document_policy

            product_store = store(conn)
            policy = document_policy(conn, namespace, variant_id)
            return acquire_documents(product_store, namespace, variant_id, scopes=who()[1],
                                     principal_id=who()[0], policy=policy)
        return safe(run, write=True, required_scope="knowledge:products:write")

    @mcp.tool()
    def compare_product_models(namespace: str, model_ids: list[str], attributes: list[str] | None = None) -> dict:
        """Evidence-linked comparison of 2-6 resolved models; only accepted matches merge provider columns."""
        return safe(lambda conn: store(conn).compare(namespace, model_ids, scopes=who()[1], attributes=attributes),
                    required_scope="knowledge:products:read")
