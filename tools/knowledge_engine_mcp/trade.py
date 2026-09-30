"""Economics trade features' entry points: flows as of a release, mirror comparisons, sanctions-linked product flows,
concordances, identity review, citation links and subscription monitors.

Acquisition runs through the shared source-pack tools (pack ``economic-statistics-and-filings``:
``un-comtrade-trade-flows`` for the ``trade-comtrade`` feature, ``eurostat-comext-trade-flows`` for
``trade-comext``, ``wits-classification-concordances`` for both); UNSD correlation workbooks are operator imports.
Every figure is cited with its source, classification vintage, release vintage and as-of time; a reporter's figure
and its partner's mirror figure stay side by side.

Exclusions (declared by every tool): no estimation or imputation of missing, confidential or suppressed flows, no
nowcasting, no reconciliation of reporter and mirror figures into one value, no re-allocation of flows between
areas, no conversion with invented weights, and no sanctions-evasion inference or compliance determination.
Sanctions and ownership links degrade to ``provider_absent`` when those stores are not held.
"""

READ = "knowledge:trade:read"
WRITE = "knowledge:trade:write"
REVIEW = "knowledge:trade:review"
LEGAL_READ = "knowledge:legal:read"
OWNERSHIP_READ = "knowledge:ownership:read"
GEO_READ = "knowledge:geospatial:read"
SUBSCRIPTIONS_READ = "knowledge:subscriptions:read"
SUBSCRIPTIONS_WRITE = "knowledge:subscriptions:write"
EXCLUSIONS_NOTE = (
    "Exclusions: no estimation, imputation or nowcast of flows, no reconciliation of reporter and mirror figures, "
    "no sanctions-evasion inference."
)

TRADE_WRITES = {
    "import_trade_concordance",
    "propose_trade_area_matches",
    "propose_trade_product_matches",
    "review_trade_identity_match",
    "revert_trade_identity_match",
    "link_trade_sanctions",
    "link_trade_ownership",
    "record_trade_comparability",
    "review_trade_comparability",
    "create_trade_monitor",
    "run_trade_monitor",
}
TRADE_READS = {
    "trade_source_contracts",
    "trade_readiness",
    "list_trade_series",
    "trade_series_values",
    "query_trade_flows",
    "compare_trade_mirror",
    "query_sanctioned_trade_flows",
    "export_trade_evidence_bundle",
    "map_trade_product_code",
    "list_trade_concordances",
    "list_trade_identity_assertions",
    "list_trade_links",
    "list_trade_comparability_notes",
    "poll_trade_monitor",
}
TRADE_TOOLS = TRADE_WRITES | TRADE_READS
TRADE_SCOPES = {
    "trade_source_contracts": [],
    "trade_readiness": [READ],
    "list_trade_series": [READ],
    "trade_series_values": [READ],
    "query_trade_flows": [READ],
    "compare_trade_mirror": [READ],
    "query_sanctioned_trade_flows": [READ, LEGAL_READ],
    "export_trade_evidence_bundle": [READ],
    "map_trade_product_code": [READ],
    "list_trade_concordances": [READ],
    "list_trade_identity_assertions": [READ],
    "list_trade_links": [READ],
    "list_trade_comparability_notes": [READ],
    "poll_trade_monitor": [READ, SUBSCRIPTIONS_READ],
    "import_trade_concordance": [WRITE],
    "propose_trade_area_matches": [WRITE, GEO_READ],
    "propose_trade_product_matches": [WRITE],
    "review_trade_identity_match": [REVIEW],
    "revert_trade_identity_match": [REVIEW],
    "link_trade_sanctions": [WRITE, LEGAL_READ],
    "link_trade_ownership": [WRITE, OWNERSHIP_READ],
    "record_trade_comparability": [WRITE],
    "review_trade_comparability": [REVIEW],
    "create_trade_monitor": [READ, SUBSCRIPTIONS_WRITE],
    "run_trade_monitor": [READ, SUBSCRIPTIONS_WRITE],
}


def required_scopes(tool_name, mutability):
    return TRADE_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _require(scopes, *required):
    from src.kb.trade_flows import TradeError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise TradeError("unauthorized", f"{', '.join(missing)} required")


def _declared(answer):
    from src.ingestion.trade_sources import EXCLUSIONS

    return {**answer, "exclusions": list(EXCLUSIONS)} if isinstance(answer, dict) else answer


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def run_tool(tool, operation, *, write=False):
        """Checks every declared scope, runs the operation and declares the exclusions on the answer."""
        scopes = TRADE_SCOPES[tool]

        def run(conn):
            _require(who()[1], *scopes)
            return _declared(operation(conn))

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def trade_source_contracts() -> dict:
        """Per-provider access decisions (UN Comtrade, Eurostat Comext, WITS, UNSD), key handling, limits, terms,
        release and classification-vintage models and the bounded coverage.
        Exclusions: no estimation, imputation or nowcast of flows, no reconciliation of reporter and mirror
        figures, no sanctions-evasion inference."""
        from src.ingestion.trade_sources import (
            BOUNDED_COVERAGE,
            EXCLUSIONS,
            LIVE_VERIFICATION,
            NEVER_SENTENCE,
            PROVIDER_CONTRACTS,
        )

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION,
                "bounded_coverage": BOUNDED_COVERAGE, "never": NEVER_SENTENCE, "exclusions": list(EXCLUSIONS)}

    @mcp.tool()
    def trade_readiness() -> dict:
        """Whether the trade-comtrade and trade-comext features are selected, stores and per-provider releases."""
        from src.kb.trade_flows import readiness

        return run_tool("trade_readiness", readiness)

    @mcp.tool()
    def list_trade_series(
        namespace: str,
        provider: str | None = None,
        reporter: str | None = None,
        partner: str | None = None,
        product: str | None = None,
        flow_direction: str | None = None,
        role: str | None = None,
    ) -> dict:
        """Trade series with reporter, partner, flow, product, classification vintage, valuation basis and role
        (the pair reporter's report or the partner's mirror report). No estimated or reconciled series exist."""
        from src.kb.trade_flows import TradeFlowStore, authorize

        def op(conn):
            authorize(namespace, who()[1], READ)
            return {"series": TradeFlowStore(conn, initialize=False).find_series(
                namespace, provider=provider, reporter_codes=[reporter] if reporter else None,
                partner_codes=[partner] if partner else None, product_codes=[product] if product else None,
                flow_direction=flow_direction, role=role)}

        return run_tool("list_trade_series", op)

    @mcp.tool()
    def trade_series_values(
        namespace: str,
        series_id: str,
        vintage_id: str | None = None,
        as_of_ms: int | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
    ) -> dict:
        """One series' values in one vintage (or the vintage released by as_of_ms), flags and quantities verbatim;
        confidential cells carry no value.
        Exclusions: no estimation, imputation or nowcast of flows, no reconciliation of reporter and mirror
        figures, no sanctions-evasion inference."""
        from src.kb.trade_flows import TradeFlowStore, authorize

        def op(conn):
            authorize(namespace, who()[1], READ)
            store = TradeFlowStore(conn, initialize=False)
            return {**store.values(namespace, series_id, vintage_id=vintage_id, as_of_ms=as_of_ms,
                                   period_from=period_from, period_to=period_to),
                    "vintages": store.vintage_rows(namespace, series_id)}

        return run_tool("trade_series_values", op)

    @mcp.tool()
    def query_trade_flows(
        namespace: str,
        reporter: str,
        partner: str,
        product: dict | None = None,
        directions: list[str] | None = None,
        provider: str | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
        as_of_ms: int | None = None,
    ) -> dict:
        """Flows of a country pair as of a release: the reporter's figure and the partner's mirror figure side by
        side per period with the release each used, valuation basis, classification vintage, a displayed asymmetry
        and comparability notes; product {code, scheme, vintage} across vintages only through a cited concordance
        (non-exact mappings flagged); no figure is 'none_reported', never zero.
        Exclusions: no estimation, imputation or nowcast of flows, no reconciliation of reporter and mirror
        figures, no sanctions-evasion inference."""
        from src.kb.trade_queries import TradeQueries

        return run_tool("query_trade_flows", lambda conn: TradeQueries(conn).flows(
            namespace, reporter=reporter, partner=partner, product=product, directions=directions,
            provider=provider, period_from=period_from, period_to=period_to, as_of_ms=as_of_ms, scopes=who()[1]))

    @mcp.tool()
    def compare_trade_mirror(
        namespace: str,
        reporter: str,
        partner: str,
        product: dict,
        direction: str = "export",
        as_of_ms: int | None = None,
    ) -> dict:
        """Reporter-versus-mirror comparison for one product and direction: per period the two published figures,
        their releases and the displayed asymmetry (reporter minus mirror), never reconciled.
        Exclusions: no estimation, imputation or nowcast of flows, no reconciliation of reporter and mirror
        figures, no sanctions-evasion inference."""
        from src.kb.trade_queries import TradeQueries

        def op(conn):
            answer = TradeQueries(conn).flows(namespace, reporter=reporter, partner=partner, product=product,
                                              directions=[direction], as_of_ms=as_of_ms, scopes=who()[1])
            comparisons = []
            for result in answer["results"]:
                for group in result["groups"]:
                    for row in group["rows"]:
                        comparisons.append({
                            "provider": result["provider"],
                            "period": row["period"],
                            "reporter": [{k: f[k] for k in ("value", "status", "valuation", "classification",
                                                            "product_match")} | {"release": f["release"]["vintage_id"]}
                                         for f in row["reporter_figures"]],
                            "mirror": [{k: f[k] for k in ("value", "status", "valuation", "classification",
                                                          "product_match")} | {"release": f["release"]["vintage_id"]}
                                       for f in row["mirror_figures"]],
                            "asymmetry": row["asymmetry"],
                        })
            return {"query": answer["query"], "status": answer["status"], "comparisons": comparisons,
                    "none_reported": [r["provider"] for r in answer["results"] if r["status"] == "none_reported"],
                    "receipt": answer["receipt"]}

        return run_tool("compare_trade_mirror", op)

    @mcp.tool()
    def query_sanctioned_trade_flows(
        namespace: str,
        reporter: str,
        partner: str,
        control_code: str | None = None,
        table_id: str | None = None,
        as_of_ms: int | None = None,
    ) -> dict:
        """The pair's flows in products a cited sanctions measure covers (links and lookup-aid notice attached);
        covered products without a flow are none_reported; provider_absent without the Sanctions store. A link
        says a product code is cited, never that goods were controlled.
        Exclusions: no estimation, imputation or nowcast of flows, no reconciliation of reporter and mirror
        figures, no sanctions-evasion inference."""
        from src.kb.trade_queries import TradeQueries

        return run_tool("query_sanctioned_trade_flows", lambda conn: TradeQueries(conn).sanctioned_flows(
            namespace, reporter=reporter, partner=partner, control_code=control_code, table_id=table_id,
            as_of_ms=as_of_ms, scopes=who()[1]))

    @mcp.tool()
    def export_trade_evidence_bundle(
        namespace: str,
        reporter: str,
        partner: str,
        product: dict | None = None,
        as_of_ms: int | None = None,
    ) -> dict:
        """A noesis-evidence-bundle-v1 for a pair's flows citing every figure with source, classification vintage,
        release vintage and as-of time; none-reported pairs, confidential cells and non-exact mappings are
        omissions.
        Exclusions: no estimation, imputation or nowcast of flows, no reconciliation of reporter and mirror
        figures, no sanctions-evasion inference."""
        from src.kb.trade_queries import TradeQueries

        def op(conn):
            queries = TradeQueries(conn)
            answer = queries.flows(namespace, reporter=reporter, partner=partner, product=product,
                                   as_of_ms=as_of_ms, scopes=who()[1])
            return {"bundle": queries.export_bundle(answer)}

        return run_tool("export_trade_evidence_bundle", op)

    @mcp.tool()
    def map_trade_product_code(namespace: str, code: str, source: dict, target: dict,
                               as_of_ms: int | None = None) -> dict:
        """A product code's targets in another classification vintage through the concordance in force (or the CN
        structure), each citing its basis; non-exact mappings flagged; nothing weighted.
        Exclusions: no estimation, imputation or nowcast of flows, no reconciliation of reporter and mirror
        figures, no sanctions-evasion inference."""
        from src.kb.trade_flows import authorize
        from src.kb.trade_identity import TradeIdentity

        def op(conn):
            authorize(namespace, who()[1], READ)
            return TradeIdentity(conn, initialize=False).resolve_product(namespace, code, source, target,
                                                                         as_of_ms=as_of_ms)

        return run_tool("map_trade_product_code", op)

    @mcp.tool()
    def list_trade_concordances(namespace: str, as_of_ms: int | None = None) -> dict:
        """Concordance tables in force (source and target classification vintages, revision, provider, release)."""
        from src.kb.trade_flows import TradeFlowStore, authorize

        def op(conn):
            authorize(namespace, who()[1], READ)
            return {"concordances": TradeFlowStore(conn, initialize=False).concordances(namespace, as_of_ms=as_of_ms)}

        return run_tool("list_trade_concordances", op)

    @mcp.tool()
    def list_trade_identity_assertions(namespace: str, kind: str | None = None, state: str | None = None) -> dict:
        """Area and product identity assertions in force (proposed, accepted, rejected, reverted, unmatched,
        special-area) with method and evidence."""
        from src.kb.trade_identity import TradeIdentity

        return run_tool("list_trade_identity_assertions", lambda conn: {
            "assertions": TradeIdentity(conn, initialize=False).assertions(namespace, scopes=who()[1], kind=kind,
                                                                           state=state)})

    @mcp.tool()
    def list_trade_links(namespace: str, series_id: str | None = None, kind: str | None = None) -> dict:
        """Sanctions and ownership links of trade series, each on one observation vintage with its basis and
        citation; target_missing and provider_absent are reported."""
        from src.kb.trade_links import TradeLinks

        return run_tool("list_trade_links", lambda conn: {
            "links": TradeLinks(conn, initialize=False).links(namespace, scopes=who()[1], series_id=series_id,
                                                              kind=kind)})

    @mcp.tool()
    def list_trade_comparability_notes(namespace: str, series_id: str | None = None) -> dict:
        """Reviewable comparability notes between trade series (valuation basis, classification vintage, period,
        partner attribution); notes never merge series."""
        from src.kb.trade_flows import TradeComparability

        return run_tool("list_trade_comparability_notes", lambda conn: {
            "notes": TradeComparability(conn, initialize=False).notes(namespace, scopes=who()[1],
                                                                      series_id=series_id)})

    @mcp.tool()
    def poll_trade_monitor(subscription_id: str, cursor: str = "") -> dict:
        """Poll a trade monitor's events (new releases and revisions of the watched pair)."""
        from src.kb.trade_monitoring import TradeMonitor

        return run_tool("poll_trade_monitor", lambda conn: TradeMonitor(conn, initialize=False).poll(
            subscription_id, principal_id=who()[0], scopes=who()[1], cursor=cursor))

    # ------------------------------------------------------------------ writes

    @mcp.tool()
    def import_trade_concordance(namespace: str, table: dict) -> dict:
        """Record an operator-imported correlation table (UNSD workbook) with its URL, publication date and file
        digest; relationships as published, weights only when published; a changed table is a new revision."""
        from src.kb.trade_flows import TradeFlowStore

        return run_tool("import_trade_concordance", lambda conn: TradeFlowStore(conn).import_concordance(
            namespace, table, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def propose_trade_area_matches(namespace: str, geo_namespace: str = "global") -> dict:
        """Offer every reporter and partner code to Geospatial places by published code, stated ISO3 or ISO
        alpha-2 equivalence; special areas stay distinct; nothing is linked until reviewed."""
        from src.kb.trade_identity import TradeIdentity

        return run_tool("propose_trade_area_matches", lambda conn: TradeIdentity(conn).propose_areas(
            namespace, principal_id=who()[0], scopes=who()[1], geo_namespace=geo_namespace), write=True)

    @mcp.tool()
    def propose_trade_product_matches(namespace: str, target: dict) -> dict:
        """Resolve every stated product code to a target classification vintage through the concordances (cited,
        non-exact flagged); unmatched codes stay visible."""
        from src.kb.trade_identity import TradeIdentity

        return run_tool("propose_trade_product_matches", lambda conn: TradeIdentity(conn).propose_products(
            namespace, target, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_trade_identity_match(namespace: str, assertion_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a proposed area or product match with a reason."""
        from src.kb.trade_identity import TradeIdentity

        return run_tool("review_trade_identity_match", lambda conn: TradeIdentity(conn).review(
            namespace, assertion_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def revert_trade_identity_match(namespace: str, assertion_id: str, reason: str) -> dict:
        """Revert a reviewed match; the code is unmatched again."""
        from src.kb.trade_identity import TradeIdentity

        return run_tool("revert_trade_identity_match", lambda conn: TradeIdentity(conn).revert(
            namespace, assertion_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def link_trade_sanctions(namespace: str, sanctions_namespace: str = "global") -> dict:
        """Link observation vintages to sanctions measures that cite their product codes (and cited areas); unmatched
        measures reported; provider_absent without the Sanctions store.
        Exclusions: no estimation, imputation or nowcast of flows, no reconciliation of reporter and mirror
        figures, no sanctions-evasion inference."""
        from src.kb.trade_links import TradeLinks

        return run_tool("link_trade_sanctions", lambda conn: TradeLinks(conn).link_sanctions(
            namespace, principal_id=who()[0], scopes=who()[1], sanctions_namespace=sanctions_namespace), write=True)

    @mcp.tool()
    def link_trade_ownership(namespace: str, series_id: str, vintage_id: str, ownership_namespace: str,
                             record_id: str, citation: dict, statement: str) -> dict:
        """Link an observation vintage to an ownership record a cited source names (source and locator required);
        never inferred from trade values; target_missing and provider_absent are reported."""
        from src.kb.trade_links import TradeLinks

        return run_tool("link_trade_ownership", lambda conn: TradeLinks(conn).link_ownership(
            namespace, series_id=series_id, vintage_id=vintage_id, ownership_namespace=ownership_namespace,
            record_id=record_id, citation=citation, statement=statement, principal_id=who()[0], scopes=who()[1]),
            write=True)

    @mcp.tool()
    def record_trade_comparability(namespace: str, left_series_id: str, right_series_id: str, relation: str,
                                   statement: str) -> dict:
        """Propose a typed comparability note between two series; notes never merge or reconcile series."""
        from src.kb.trade_flows import TradeComparability

        return run_tool("record_trade_comparability", lambda conn: TradeComparability(conn).record(
            namespace, left_series_id, right_series_id, relation, statement, principal_id=who()[0],
            scopes=who()[1]), write=True)

    @mcp.tool()
    def review_trade_comparability(namespace: str, note_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a proposed comparability note with a reason."""
        from src.kb.trade_flows import TradeComparability

        return run_tool("review_trade_comparability", lambda conn: TradeComparability(conn).review(
            namespace, note_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def create_trade_monitor(namespace: str, request_key: str, flow_filter: dict,
                             delivery: dict | None = None) -> dict:
        """Subscribe to a country pair (both report directions), optionally products and providers: notices when a
        release publishes or revises flows there (record changes, not trade alerts)."""
        from src.kb.trade_monitoring import TradeMonitor

        return run_tool("create_trade_monitor", lambda conn: TradeMonitor(conn).create(
            namespace, request_key, flow_filter=flow_filter, principal_id=who()[0], scopes=who()[1],
            delivery=delivery), write=True)

    @mcp.tool()
    def run_trade_monitor(subscription_id: str, watermark: int | None = None) -> dict:
        """Evaluate a trade monitor at a committed watermark; notices cite the new vintage and the changed values."""
        from src.kb.trade_monitoring import TradeMonitor

        return run_tool("run_trade_monitor", lambda conn: TradeMonitor(conn).run(
            subscription_id, watermark, principal_id=who()[0], scopes=who()[1]), write=True)
