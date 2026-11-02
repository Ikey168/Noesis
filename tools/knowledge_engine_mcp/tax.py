"""Economics tax-transparency features' entry points (``economics.tax``, #2889): a jurisdiction's tax figures as of an
edition, a series' edition history, evidence-bundle export, link-only public CbCR references and their reviewable
identity against ownership entities.

Acquisition runs through the shared source-pack tools (pack ``economic-tax-transparency``: sources ``oecd-cts-cbcr``,
``oecd-cts-rates``, ``eutax-offshore-wealth`` and ``eutax-billionaire-etr``). Every value is cited with its source,
edition, series key, vintage, release and retrieval time. Published statistics and model estimates are separate
sections; a model estimate always shows its method and edition and is never presented as an observation. Tax Justice
Network indices are ``not-implemented`` (licence) and public CbCR reports are link-only references.

Exclusions (declared by every answering tool): no own effective tax rate, ratio, share, rank or score, no blending of
sources, no model estimate presented as an observation, no filled jurisdiction or year, no currency conversion, no
company-level CbCR figure and no forecast.
"""

READ = "knowledge:tax:read"
WRITE = "knowledge:tax:write"
REVIEW = "knowledge:tax:review"
OWNERSHIP_READ = "knowledge:ownership:read"
OWNERSHIP_WRITE = "knowledge:ownership:write"
OWNERSHIP_REVIEW = "knowledge:ownership:review"
EXCLUSIONS_NOTE = (
    "Exclusions: no own effective tax rate, ratio, share, rank or score, no blending of sources, no model estimate "
    "presented as an observation, no filled jurisdiction or year, no currency conversion, no company-level CbCR figure "
    "and no forecast."
)

TAX_WRITES = {
    "record_tax_company_reports",
    "propose_tax_company_matches",
    "review_tax_company_match",
    "revert_tax_company_match",
}
TAX_READS = {
    "tax_source_contracts",
    "tax_readiness",
    "list_tax_series",
    "tax_indicator_for_jurisdiction",
    "tax_series_history",
    "export_tax_evidence_bundle",
    "list_tax_company_reports",
    "list_tax_company_matches",
    "tax_company_reports_for_entity",
}
TAX_TOOLS = TAX_WRITES | TAX_READS
TAX_SCOPES = {
    "tax_source_contracts": [],
    "tax_readiness": [READ],
    "list_tax_series": [READ],
    "tax_indicator_for_jurisdiction": [READ],
    "tax_series_history": [READ],
    "export_tax_evidence_bundle": [READ],
    "list_tax_company_reports": [READ],
    "list_tax_company_matches": [READ, OWNERSHIP_READ],
    "tax_company_reports_for_entity": [READ, OWNERSHIP_READ],
    "record_tax_company_reports": [WRITE],
    "propose_tax_company_matches": [WRITE, OWNERSHIP_READ, OWNERSHIP_WRITE],
    "review_tax_company_match": [REVIEW, OWNERSHIP_REVIEW],
    "revert_tax_company_match": [REVIEW, OWNERSHIP_REVIEW],
}


def required_scopes(tool_name, mutability):
    return TAX_SCOPES.get(tool_name, [WRITE if mutability == "write" else READ])


def _require(scopes, *required):
    from src.kb.tax_records import TaxError

    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise TaxError("unauthorized", f"{', '.join(missing)} required")


def _declared(answer):
    """Every answer leaves with the exclusions declared and is checked against the TX01 minimisation decision."""
    from src.ingestion.tax_sources import EXCLUSIONS
    from src.kb.tax_records import TaxError, forbidden_paths, personal_data_paths

    if not isinstance(answer, dict):
        return answer
    if personal_data_paths(answer) or forbidden_paths(answer):
        raise TaxError("minimisation", "an answer would carry a personal, derived, ranked or converted field")
    return {**answer, "exclusions": list(EXCLUSIONS)}


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def run_tool(tool, operation, *, write=False):
        """Checks every declared scope, runs the operation and declares the exclusions on the answer."""
        scopes = TAX_SCOPES[tool]

        def run(conn):
            _require(who()[1], *scopes)
            return _declared(operation(conn))

        return safe(run, write=write, required_scope=scopes[0] if scopes else None)

    @mcp.tool()
    def tax_source_contracts() -> dict:
        """Per-source access decisions (OECD Corporate Tax Statistics CbCR aggregates and rates, EU Tax Observatory
        offshore wealth and billionaire effective tax rates: unverified-live; Tax Justice Network indices:
        not-implemented, licence; public CbCR: reference-only), licences, revision models, caps and bounded coverage.
        Exclusions: no own effective tax rate, ratio, share, rank or score, no blending of sources, no model estimate
        presented as an observation, no filled jurisdiction or year, no currency conversion, no company-level CbCR
        figure and no forecast."""
        from src.ingestion.tax_sources import (
            BOUNDED_COVERAGE,
            CAPS,
            EXCLUSIONS,
            LIVE_VERIFICATION,
            NEVER_SENTENCE,
            PROVIDER_CONTRACTS,
        )
        from src.kb.tax_records import MINIMISATION

        return {"contracts": PROVIDER_CONTRACTS, "live_verification": LIVE_VERIFICATION, "caps": CAPS,
                "bounded_coverage": BOUNDED_COVERAGE, "minimisation": MINIMISATION, "never": NEVER_SENTENCE,
                "exclusions": list(EXCLUSIONS)}

    @mcp.tool()
    def tax_readiness() -> dict:
        """Which tax features (one per source) are selected, the stores, per-source editions and staleness."""
        from src.kb.tax_records import readiness

        return run_tool("tax_readiness", readiness)

    @mcp.tool()
    def list_tax_series(namespace: str, provider: str | None = None, concept: str | None = None,
                        nature: str | None = None, jurisdiction: str | None = None) -> dict:
        """Tax series with dataset, nature (published statistic or model estimate), indicator, jurisdiction, partner
        grouping, unit and the editions held."""
        from src.kb.tax_records import authorize
        from src.kb.tax_store import TaxStore

        def op(conn):
            authorize(namespace, who()[1], READ)
            return {"series": TaxStore(conn, initialize=False).find_series(
                namespace, provider=provider, concept=concept, nature=nature, jurisdiction=jurisdiction)}

        return run_tool("list_tax_series", op)

    @mcp.tool()
    def tax_indicator_for_jurisdiction(namespace: str, jurisdiction: str, concept: str | None = None,
                                       period: str | None = None, as_of: str | int | None = None,
                                       providers: list[str] | None = None) -> dict:
        """A jurisdiction's (ISO 3166 alpha-3) tax figures from the edition published by a date: published
        statistics and model estimates in separate sections, each row with edition, definition, status letters,
        confidential cells as their status, vintage and citation; estimates with method and 'not an observation';
        Tax Justice Network indices reported not-implemented.
        Exclusions: no own effective tax rate, ratio, share, rank or score, no blending of sources, no model estimate
        presented as an observation, no filled jurisdiction or year, no currency conversion, no company-level CbCR
        figure and no forecast."""
        from src.kb.tax_queries import TaxQueries
        from src.kb.tax_records import enabled_providers

        return run_tool("tax_indicator_for_jurisdiction", lambda conn: TaxQueries(conn).indicator_for_jurisdiction(
            namespace, scopes=who()[1], jurisdiction=jurisdiction, concept=concept, period=period, as_of=as_of,
            providers=providers, enabled_providers=enabled_providers(conn)))

    @mcp.tool()
    def tax_series_history(namespace: str, series_id: str) -> dict:
        """Every retained edition of a tax series with what each edition added, revised or dropped (values and status
        letters before and after), method and definition changes and removals, each edition cited.
        Exclusions: no own effective tax rate, ratio, share, rank or score, no blending of sources, no model estimate
        presented as an observation, no filled jurisdiction or year, no currency conversion, no company-level CbCR
        figure and no forecast."""
        from src.kb.tax_queries import TaxQueries

        return run_tool("tax_series_history", lambda conn: TaxQueries(conn).series_history(
            namespace, series_id, scopes=who()[1]))

    @mcp.tool()
    def export_tax_evidence_bundle(namespace: str, jurisdiction: str, concept: str | None = None,
                                   as_of: str | int | None = None) -> dict:
        """An evidence bundle for a jurisdiction's tax figures: every item cites its source, edition, record revision
        (vintage) and as-of time; model estimates are labelled with method and edition.
        Exclusions: no own effective tax rate, ratio, share, rank or score, no blending of sources, no model estimate
        presented as an observation, no filled jurisdiction or year, no currency conversion, no company-level CbCR
        figure and no forecast."""
        from src.kb.tax_queries import TaxQueries
        from src.kb.tax_records import enabled_providers

        def op(conn):
            queries = TaxQueries(conn)
            answer = queries.indicator_for_jurisdiction(namespace, scopes=who()[1], jurisdiction=jurisdiction,
                                                        concept=concept, as_of=as_of,
                                                        enabled_providers=enabled_providers(conn))
            return {"status": answer["status"], "evidence_bundle": queries.evidence_bundle(answer)}

        return run_tool("export_tax_evidence_bundle", op)

    @mcp.tool()
    def list_tax_company_reports(namespace: str, reference_key: str | None = None, history: bool = False) -> dict:
        """Link-only public CbCR references (Directive (EU) 2021/2101): company name as published, identifiers,
        financial year, report URL, publication date and channel; never a figure."""
        from src.kb.tax_records import authorize
        from src.kb.tax_store import TaxStore

        def op(conn):
            authorize(namespace, who()[1], READ)
            return {"reports": TaxStore(conn, initialize=False).company_reports(namespace, reference_key=reference_key,
                                                                                history=history)}

        return run_tool("list_tax_company_reports", op)

    @mcp.tool()
    def list_tax_company_matches(namespace: str) -> dict:
        """Identity candidates between public CbCR references and ownership entities (proposed, accepted, rejected,
        reverted), each with method, evidence and reviewer, plus the references still unmatched."""
        from src.kb.tax_identity import TaxIdentity

        def op(conn):
            identity = TaxIdentity(conn, initialize=False)
            return {"candidates": identity.candidates(namespace, scopes=who()[1]),
                    "unmatched": identity.unmatched(namespace, scopes=who()[1])}

        return run_tool("list_tax_company_matches", op)

    @mcp.tool()
    def tax_company_reports_for_entity(namespace: str, ownership_key: str) -> dict:
        """Public CbCR references of an ownership record or entity reached through accepted identity decisions only;
        pending candidates are listed, never used.
        Exclusions: no own effective tax rate, ratio, share, rank or score, no blending of sources, no model estimate
        presented as an observation, no filled jurisdiction or year, no currency conversion, no company-level CbCR
        figure and no forecast."""
        from src.kb.tax_queries import TaxQueries

        return run_tool("tax_company_reports_for_entity", lambda conn: TaxQueries(conn).company_reports_for_entity(
            namespace, scopes=who()[1], ownership_key=ownership_key))

    # ------------------------------------------------------------------ writes

    @mcp.tool()
    def record_tax_company_reports(namespace: str, references: list[dict]) -> dict:
        """Record operator-declared public CbCR report references (link-only: company name as published, identifiers,
        financial year, https report URL, publication date, channel). A changed reference is a new revision; a
        reference carrying a figure is refused."""
        from src.kb.tax_store import TaxStore

        return run_tool("record_tax_company_reports", lambda conn: TaxStore(conn).record_company_reports(
            namespace, references, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def propose_tax_company_matches(namespace: str, ownership_namespace: str) -> dict:
        """Propose identity candidates between public CbCR references and ownership legal entities: published
        identifiers first, equal names as low evidence; nothing is accepted automatically."""
        from src.kb.tax_identity import TaxIdentity

        return run_tool("propose_tax_company_matches", lambda conn: TaxIdentity(conn).propose_company_matches(
            namespace, ownership_namespace=ownership_namespace, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def review_tax_company_match(namespace: str, candidate_id: str, decision: str, reason: str) -> dict:
        """Accept or reject a public CbCR company candidate with a reason (an entity identity decision)."""
        from src.kb.tax_identity import TaxIdentity

        return run_tool("review_tax_company_match", lambda conn: TaxIdentity(conn).review(
            namespace, candidate_id, decision, reason, principal_id=who()[0], scopes=who()[1]), write=True)

    @mcp.tool()
    def revert_tax_company_match(namespace: str, candidate_id: str, reason: str) -> dict:
        """Revert an accepted or rejected public CbCR company decision with a reason; records stay intact."""
        from src.kb.tax_identity import TaxIdentity

        return run_tool("revert_tax_company_match", lambda conn: TaxIdentity(conn).revert(
            namespace, candidate_id, reason, principal_id=who()[0], scopes=who()[1]), write=True)
