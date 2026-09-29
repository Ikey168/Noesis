"""Products pack entry points: readiness, model lookup, matching review, documents, comparison and safety notices.

Acquisition itself runs through the shared source-pack tools
(``run_source_pack_execution`` with pack ``products-displays``: operation
``models`` for the display and appliance providers, ``components`` for the
BMEcat component catalogues, ``notices`` for the safety feature's Safety Gate,
CPSC, NHTSA and RASFF sources). Lookup, matching and comparison take a
``category`` from the category registry (displays, household washing machines,
refrigerating appliances, multilayer ceramic capacitors, thick-film chip
resistors); comparison never crosses categories. Values are provider assertions
(brand content, supplier registrations, manufacturer or supplier catalogue
data), never independent tests, buying recommendations or cross-references. Safety notices are what an authority published, quoted and
cited to a notice revision: no tool returns a safety verdict, risk score or
consumer advice, and a product without an accepted notice match has no notice
on record.
"""

SAFETY_WRITES = {
    "propose_product_notice_matches", "review_product_notice_match", "link_product_notice_citations",
    "review_product_notice_party_link", "revert_product_notice_party_link", "create_product_notice_monitor",
    "run_product_notice_monitor",
}
SAFETY_READS = {
    "product_safety_source_contracts", "lookup_product_notices", "inspect_product_notice",
    "propose_product_notice_party_links", "poll_product_notice_monitor",
}
SAFETY_TOOLS = SAFETY_WRITES | SAFETY_READS
# Products expansion (#2061): category registry, component lookup, manufacturer links and document citations.
EXPANSION_WRITES = {"review_component_manufacturer_link", "revert_component_manufacturer_link"}
EXPANSION_READS = {"product_category_registry", "lookup_component", "cite_product_document"}
EXPANSION_TOOLS = EXPANSION_WRITES | EXPANSION_READS
PRODUCT_WRITES = {"propose_product_matches", "review_product_match", "acquire_product_documents"} | SAFETY_WRITES \
    | EXPANSION_WRITES
PRODUCT_TOOLS = PRODUCT_WRITES | SAFETY_READS | EXPANSION_READS | {
    "products_readiness", "product_provider_contracts", "lookup_product_models",
    "inspect_product_identity", "product_selection_outcomes", "compare_product_models",
}
_ENTITY = ["knowledge:entity-history:write", "knowledge:entity-history:review"]
PRODUCT_SCOPES = {
    "product_provider_contracts": [],
    "product_category_registry": [],
    "products_readiness": ["knowledge:products:read"],
    "lookup_product_models": ["knowledge:products:read"],
    "inspect_product_identity": ["knowledge:products:read"],
    "compare_product_models": ["knowledge:products:read"],
    "lookup_component": ["knowledge:products:read"],
    "product_selection_outcomes": ["knowledge:products:read"],
    "cite_product_document": ["knowledge:products:read"],
    "propose_product_matches": ["knowledge:products:write"],
    "review_product_match": ["knowledge:products:review"],
    "acquire_product_documents": ["knowledge:products:write"],
    # Manufacturer links are entity identity decisions; reverting one is an undo decision.
    "review_component_manufacturer_link": ["knowledge:products:review", *_ENTITY],
    "revert_component_manufacturer_link": ["knowledge:products:review", *_ENTITY,
                                           "knowledge:entity-history:execute"],
    "product_safety_source_contracts": [],
    # include_news additionally needs knowledge:read, checked at call time.
    "lookup_product_notices": ["knowledge:products:read"],
    "inspect_product_notice": ["knowledge:products:read"],
    # Proposing reads notices and Products identities and writes candidates.
    "propose_product_notice_matches": ["knowledge:products:write", "knowledge:products:read"],
    "review_product_notice_match": ["knowledge:products:review"],
    # Linking reads the standards catalogue and the Legal works it resolves against.
    "link_product_notice_citations": ["knowledge:products:write", "knowledge:standards:read", "knowledge:legal:read"],
    "propose_product_notice_party_links": ["knowledge:products:read"],
    # Party links are entity identity decisions; reverting one is an undo decision.
    "review_product_notice_party_link": ["knowledge:products:review", *_ENTITY],
    "revert_product_notice_party_link": ["knowledge:products:review", *_ENTITY, "knowledge:entity-history:execute"],
    "create_product_notice_monitor": ["knowledge:products:read", "knowledge:subscriptions:write"],
    # Running reads notices and matches, inspects the subscription and writes its events.
    "run_product_notice_monitor": ["knowledge:products:read", "knowledge:subscriptions:read",
                                   "knowledge:subscriptions:write"],
    # The subscription retains the products scope its evaluations read under; polling needs it too.
    "poll_product_notice_monitor": ["knowledge:subscriptions:read", "knowledge:products:read"],
}


QUERY_EXAMPLES = {
    "lookup_product_models": {
        "arguments": {"namespace": "global", "brand": "Hausmark", "designation": "WM-8E14",
                      "category": "household washing machines"},
        "semantics": "exact designation ignoring case and separators within one registered category; one row per "
                     "provider record, with identifier conflicts and match candidates",
    },
    "propose_product_matches": {
        "arguments": {"namespace": "global", "category": "multilayer ceramic capacitors"},
        "semantics": "deterministic candidates inside one category from designations (appliances, displays) or "
                     "manufacturer + exact MPN (components) and the category's corroborating attributes; never "
                     "across categories and never merged until a reviewer accepts",
    },
    "compare_product_models": {
        "arguments": {"namespace": "global", "model_ids": ["<model_id>", "<model_id>"],
                      "category": "refrigerating appliances"},
        "semantics": "rows from the category registry; a row compares only on a shared attribute, mode, unit and "
                     "label scheme; component nominal values carry their tolerance; mixed categories are refused",
    },
    "lookup_component": {
        "arguments": {"namespace": "global", "mpn": "CX0603X7R104K500", "manufacturer": "Capatronic"},
        "semantics": "one entry per manufacturer + MPN identity with every provider's record, SKU aliases, the "
                     "lifecycle status each source published with its date, match candidates and link-only records; "
                     "no cross-reference, replacement, price or stock",
    },
}


def required_scopes(tool_name, mutability):
    return PRODUCT_SCOPES.get(
        tool_name, ["knowledge:products:write" if mutability == "write" else "knowledge:products:read"])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def store(conn, *, initialize=True):
        from src.kb.products import ProductStore

        # Reads run on read-only connections: they never create tables and answer not_ready before any run.
        return ProductStore(conn, initialize=initialize)

    @mcp.tool()
    def product_provider_contracts() -> dict:
        """Pinned Open Icecat, EPREL and BMEcat access contracts, catalogue boundaries and live-verification state,
        plus the PX01 component-source decisions (implement / link-only / not implemented) with verify notes."""
        from src.ingestion.product_sources import ASSERTION_KINDS, COMPONENT_SOURCE_DECISIONS, PROVIDER_CONTRACTS

        return {"contracts": PROVIDER_CONTRACTS, "assertion_kinds": ASSERTION_KINDS,
                "component_sources": COMPONENT_SOURCE_DECISIONS}

    @mcp.tool()
    def product_category_registry(category: str | None = None) -> dict:
        """Registered product categories: attribute keys, kinds, canonical and accepted units, modes, tolerance
        attributes, label schemes by regulation, default comparison rows and matching rules."""
        def run():
            from src.kb import product_categories

            if category:
                found = product_categories.resolve_label(category)
                if found is None:
                    return {"ok": False, "error": {"code": "unknown_category",
                                                   "message": f"{category!r} is not a registered category",
                                                   "registered": product_categories.categories()}}
                return {"categories": {found: product_categories.category(found)}}
            return {"categories": product_categories.registry()}
        return run()

    @mcp.tool()
    def products_readiness() -> dict:
        """Per-provider fixture/live readiness, display and notice sources; one ready provider never implies another."""
        from src.kb.products import readiness

        return safe(lambda conn: readiness(conn), required_scope="knowledge:products:read")

    @mcp.tool()
    def lookup_product_models(namespace: str, query: str | None = None, brand: str | None = None,
                              designation: str | None = None, gtin: str | None = None,
                              provider: str | None = None, limit: int = 25, category: str | None = None) -> dict:
        """Find product variants by brand, exact designation, GTIN or text, optionally within one registered
        category (id or label), with identifier conflicts and matches."""
        return safe(lambda conn: store(conn, initialize=False).lookup(
            namespace, scopes=who()[1], query=query, brand=brand, designation=designation, gtin=gtin,
            provider=provider, limit=limit, category=category), required_scope="knowledge:products:read")

    @mcp.tool()
    def inspect_product_identity(namespace: str, identity_id: str) -> dict:
        """A model or variant with revisions, current and superseded assertions, documents, coverage and matches
        (components also with the lifecycle status their source published)."""
        return safe(lambda conn: store(conn, initialize=False).inspect(namespace, identity_id, scopes=who()[1]),
                    required_scope="knowledge:products:read")

    @mcp.tool()
    def lookup_component(namespace: str, mpn: str | None = None, manufacturer: str | None = None,
                         sku: str | None = None, category: str | None = None, limit: int = 25) -> dict:
        """Electronic components by manufacturer part number (optionally with the manufacturer) or by a
        provider-scoped distributor SKU: one entry per normalised manufacturer + MPN with each provider's record,
        SKU aliases, lifecycle status only as a named source published it (with its date), match candidates and
        link-only records (Octopart, DigiKey). No cross-reference, replacement, price, stock or availability."""
        return safe(lambda conn: store(conn, initialize=False).lookup_component(
            namespace, scopes=who()[1], mpn=mpn, manufacturer=manufacturer, sku=sku, category=category,
            limit=limit), required_scope="knowledge:products:read")

    @mcp.tool()
    def review_component_manufacturer_link(namespace: str, manufacturer: str, entity_id: str, decision: str,
                                           reason: str) -> dict:
        """Record a match / non-match between a published component manufacturer name and a canonical entity as
        an entity identity decision; never a merge. Linked names count as one manufacturer in component lookups
        and matching."""
        return safe(lambda conn: store(conn).decide_manufacturer_link(
            namespace, manufacturer, entity_id, decision, reason, scopes=who()[1], principal_id=who()[0]),
            write=True, required_scope="knowledge:products:review")

    @mcp.tool()
    def revert_component_manufacturer_link(namespace: str, link_id: str) -> dict:
        """Undo a manufacturer link; the undo is itself an auditable identity decision."""
        return safe(lambda conn: store(conn).revert_manufacturer_link(namespace, link_id, scopes=who()[1],
                                                                      principal_id=who()[0]),
                    write=True, required_scope="knowledge:products:review")

    @mcp.tool()
    def cite_product_document(namespace: str, link_id: str, page: int | None = None,
                              section: str | None = None) -> dict:
        """A citation locator into a provider-linked datasheet or information sheet: link, retained content hash
        and a page checked against the extracted pages (section recorded verbatim). A document never retained
        is cited as its link only."""
        return safe(lambda conn: store(conn, initialize=False).cite_document(
            namespace, link_id, scopes=who()[1], page=page, section=section),
            required_scope="knowledge:products:read")

    @mcp.tool()
    def product_selection_outcomes(namespace: str, run_id: str) -> dict:
        """Per-selector outcomes of one run: returned, not_found or outside_open_catalogue (and notice selectors)."""
        def run(conn):
            from src.kb.product_safety import ProductSafetyStore
            from src.kb.products import READ_SCOPE, _authorize

            _authorize(namespace, who()[1], READ_SCOPE, write=False)
            products = store(conn, initialize=False)
            products._require_ready()
            safety = ProductSafetyStore(conn, initialize=False)
            return {"run_id": run_id, "outcomes": products.selection_outcomes(namespace, run_id),
                    "notice_outcomes": safety.selection_outcomes(namespace, run_id) if safety.ready() else []}
        return safe(run, required_scope="knowledge:products:read")

    @mcp.tool()
    def propose_product_matches(namespace: str, category: str | None = None) -> dict:
        """Deterministic cross-provider match candidates within one category (Icecat/EPREL for displays and
        appliances, manufacturer + exact MPN between component catalogues) from identifiers and the category's
        corroborating attributes; never across categories and none auto-accepted."""
        return safe(lambda conn: store(conn).propose_matches(namespace, scopes=who()[1], principal_id=who()[0],
                                                             category=category),
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
    def compare_product_models(namespace: str, model_ids: list[str], attributes: list[str] | None = None,
                               category: str | None = None) -> dict:
        """Evidence-linked comparison of 2-6 resolved models of one category (rows from the category registry);
        only accepted matches merge provider columns, component values show their tolerance, and models of
        different categories are refused. No ranking or best pick."""
        return safe(lambda conn: store(conn, initialize=False).compare(
            namespace, model_ids, scopes=who()[1], attributes=attributes, category=category),
            required_scope="knowledge:products:read")

    # ------------------------------------------------------------ safety notices (#1916)

    def safety(conn, *, initialize=True):
        from src.kb.product_safety import ProductSafetyStore

        return ProductSafetyStore(conn, initialize=initialize)

    def monitor(conn, *, initialize=True):
        from src.kb.product_safety_monitoring import ProductNoticeMonitor

        return ProductNoticeMonitor(conn, initialize=initialize)

    @mcp.tool()
    def product_safety_source_contracts() -> dict:
        """R01 access decisions for Safety Gate, CPSC, NHTSA, RASFF, BAuA and the GPSR text (verify notes kept)."""
        from src.ingestion.product_sources import SAFETY_AUTHORITIES, SAFETY_PROVIDER_CONTRACTS

        return {"contracts": SAFETY_PROVIDER_CONTRACTS,
                "authorities": {k: {"declared": v[0], "code": v[1], "jurisdiction": v[2]}
                                for k, v in SAFETY_AUTHORITIES.items()}}

    @mcp.tool()
    def lookup_product_notices(namespace: str, model_id: str | None = None, variant_id: str | None = None,
                               brand: str | None = None, designation: str | None = None, gtin: str | None = None,
                               as_of: str | None = None, include_news: bool = False) -> dict:
        """Safety notices naming a Products model/variant (reviewed matches only) or a brand, designation or GTIN
        string, as of an ISO date: each notice with its revision current as of that date, hazard, affected
        identification, the authority's corrective action quoted verbatim, issuing authority, cited standards and
        legal acts and what connected it; authorities side by side, later revisions named. A product without an
        accepted match returns notices [] and status "no notice on record" - never a safety verdict, risk score or
        advice. include_news adds news naming the notice number (needs knowledge:read)."""
        return safe(lambda conn: safety(conn, initialize=False).lookup(
            namespace, scopes=who()[1], model_id=model_id, variant_id=variant_id, brand=brand,
            designation=designation, gtin=gtin, as_of=as_of, include_news=include_news),
            required_scope="knowledge:products:read")

    @mcp.tool()
    def inspect_product_notice(namespace: str, notice: str, as_of: str | None = None) -> dict:
        """One notice (id or provider:notice_number) with the revision current as of a date, every revision and its
        superseded parts, citations with their links and the reviewable product matches. No verdict or advice."""
        return safe(lambda conn: safety(conn, initialize=False).inspect(namespace, notice, scopes=who()[1],
                                                                        as_of=as_of),
                    required_scope="knowledge:products:read")

    @mcp.tool()
    def propose_product_notice_matches(namespace: str) -> dict:
        """Candidates between notice identifications and Products models on GTIN, brand and exact designation;
        suffix siblings are ambiguous and GTIN contradictions contradicted. Nothing is attached without review."""
        return safe(lambda conn: safety(conn).propose_matches(namespace, scopes=who()[1], principal_id=who()[0]),
                    write=True, required_scope="knowledge:products:write")

    @mcp.tool()
    def review_product_notice_match(namespace: str, match_id: str, decision: str, reason: str) -> dict:
        """Accept, reject or defer a notice match with a reason; append-only and reversible. Contradicted and
        sibling candidates cannot be accepted."""
        return safe(lambda conn: safety(conn).review_match(namespace, match_id, decision, reason, scopes=who()[1],
                                                           principal_id=who()[0]),
                    write=True, required_scope="knowledge:products:review")

    @mcp.tool()
    def link_product_notice_citations(namespace: str, standards_namespace: str | None = None,
                                      legal_namespace: str | None = None) -> dict:
        """Link cited standards (exact reference) and legal acts (exact CELEX/ELI) to the standards catalogue and
        Legal works, per citing revision; never by topic, category or hazard similarity."""
        return safe(lambda conn: safety(conn).link_citations(
            namespace, scopes=who()[1], principal_id=who()[0], standards_namespace=standards_namespace,
            legal_namespace=legal_namespace), write=True, required_scope="knowledge:products:write")

    @mcp.tool()
    def propose_product_notice_party_links(namespace: str) -> dict:
        """Manufacturer, importer and retailer names on current notices with canonical-alias candidates; nothing
        is linked without an identity decision and unmatched names stay source strings."""
        return safe(lambda conn: safety(conn, initialize=False).propose_party_links(namespace, scopes=who()[1]),
                    required_scope="knowledge:products:read")

    @mcp.tool()
    def review_product_notice_party_link(namespace: str, party_name: str, entity_id: str, decision: str,
                                         reason: str) -> dict:
        """Record a match / non-match between a notice party name and a canonical entity as an entity identity
        decision; never a merge."""
        return safe(lambda conn: safety(conn).decide_party_link(
            namespace, party_name, entity_id, decision, reason, scopes=who()[1], principal_id=who()[0]),
            write=True, required_scope="knowledge:products:review")

    @mcp.tool()
    def revert_product_notice_party_link(namespace: str, link_id: str) -> dict:
        """Undo a party link; the undo is itself an auditable identity decision."""
        return safe(lambda conn: safety(conn).revert_party_link(namespace, link_id, scopes=who()[1],
                                                                principal_id=who()[0]),
                    write=True, required_scope="knowledge:products:review")

    @mcp.tool()
    def create_product_notice_monitor(namespace: str, request_key: str, models: list[str] | None = None,
                                      variants: list[str] | None = None, brands: list[str] | None = None,
                                      gtins: list[str] | None = None, authorities: list[str] | None = None,
                                      delivery: dict | None = None) -> dict:
        """Watch Products models/variants, brand or GTIN strings or issuing authorities for new notices, notice
        revisions and match reviews, as a knowledge subscription (poll by default)."""
        watch = {k: v for k, v in (("models", models), ("variants", variants), ("brands", brands), ("gtins", gtins),
                                   ("authorities", authorities)) if v}
        return safe(lambda conn: monitor(conn).create(namespace, request_key, watch=watch, principal_id=who()[0],
                                                      scopes=who()[1], delivery=delivery),
                    write=True, required_scope="knowledge:products:read")

    @mcp.tool()
    def run_product_notice_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a monitor at the latest complete notice-source run (partial runs are never evaluated); each
        notification cites the notice revision and the match decision."""
        return safe(lambda conn: monitor(conn).run(subscription_id, watermark, principal_id=who()[0],
                                                   scopes=who()[1]),
                    write=True, required_scope="knowledge:subscriptions:write")

    @mcp.tool()
    def poll_product_notice_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a product-notice monitor's delivered events."""
        return safe(lambda conn: monitor(conn, initialize=False).poll(subscription_id, principal_id=who()[0],
                                                                      scopes=who()[1], cursor=cursor),
                    required_scope="knowledge:subscriptions:read")
