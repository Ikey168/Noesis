"""Legal pack entry points: lookup, cited passages, as-of version selection, comparison and dossier links.

Acquisition runs through the shared source-pack tools (pack ``legal-research``).
Results are source facts with exact locators; interpretation and legal-effect
assessment are outside these tools.
"""

LEGAL_WRITES = {"link_legal_dossier"}
LEGAL_TOOLS = LEGAL_WRITES | {
    "legal_readiness", "legal_source_contracts", "lookup_legal_work", "inspect_legal_work",
    "get_legal_passages", "select_legal_version_as_of", "compare_legal_versions", "legal_selection_outcomes",
    "legal_retrieval_modes",
}
LEGAL_SCOPES = {"legal_source_contracts": [], "legal_retrieval_modes": []}


def required_scopes(tool_name, mutability):
    return LEGAL_SCOPES.get(tool_name, ["knowledge:legal:write" if mutability == "write" else "knowledge:legal:read"])


def register(mcp, safe, context):
    def who():
        return context()[0], context()[1]

    def store(conn):
        from src.kb.legal import LegalStore

        return LegalStore(conn)

    @mcp.tool()
    def legal_source_contracts() -> dict:
        """Per-provider access, identifiers, formats and coverage boundaries (CELLAR, RII, Berlin)."""
        from src.ingestion.legal_sources import PROVIDER_CONTRACTS

        return {"contracts": PROVIDER_CONTRACTS}

    @mcp.tool()
    def legal_retrieval_modes() -> dict:
        """Which legal retrieval modes are enabled or deferred, with thresholds and evidence."""
        from src.kb.legal_retrieval import retrieval_modes

        return retrieval_modes()

    @mcp.tool()
    def legal_readiness() -> dict:
        """Per-provider/jurisdiction readiness; planned or uninstalled sources are never listed as ready."""
        from src.kb.legal import readiness

        return safe(lambda conn: readiness(conn), required_scope="knowledge:legal:read")

    @mcp.tool()
    def lookup_legal_work(namespace: str, identifier: str | None = None, title: str | None = None,
                          citation: str | None = None, jurisdiction: str | None = None, limit: int = 20) -> dict:
        """Exact CELEX/ELI/ECLI/doknr/docket lookup, or title/citation lookup within one jurisdiction."""
        return safe(lambda conn: store(conn).lookup(
            namespace, scopes=who()[1], identifier=identifier, title=title, citation=citation,
            jurisdiction=jurisdiction, limit=limit), required_scope="knowledge:legal:read")

    @mcp.tool()
    def inspect_legal_work(namespace: str, work_id: str) -> dict:
        """A work with its language expressions, versions, sourced dates, citations and procedure links."""
        return safe(lambda conn: store(conn).inspect(namespace, work_id, scopes=who()[1]),
                    required_scope="knowledge:legal:read")

    @mcp.tool()
    def get_legal_passages(namespace: str, version_id: str, locator: str | None = None, contains: str | None = None,
                           limit: int = 20) -> dict:
        """Exact source passages of one version by locator fragment or exact substring, with locators."""
        return safe(lambda conn: store(conn).passages(namespace, version_id, scopes=who()[1], locator=locator,
                                                      contains=contains, limit=limit),
                    required_scope="knowledge:legal:read")

    @mcp.tool()
    def select_legal_version_as_of(namespace: str, work_id: str, as_of: str, language: str | None = None) -> dict:
        """Candidate versions on a date with their sourced evidence; unknown or ambiguous stays so."""
        return safe(lambda conn: store(conn).select_as_of(namespace, work_id, as_of, scopes=who()[1],
                                                          language=language), required_scope="knowledge:legal:read")

    @mcp.tool()
    def compare_legal_versions(namespace: str, left_version_id: str, right_version_id: str) -> dict:
        """Passage-level differences between two versions of one work (source change, not legal effect)."""
        return safe(lambda conn: store(conn).compare_versions(namespace, left_version_id, right_version_id,
                                                              scopes=who()[1]), required_scope="knowledge:legal:read")

    @mcp.tool()
    def legal_selection_outcomes(namespace: str, run_id: str) -> dict:
        """Per-selection outcomes of one legal acquisition run (returned, not_found)."""
        def run(conn):
            from src.kb.legal import READ_SCOPE, _authorize

            _authorize(namespace, who()[1], READ_SCOPE, write=False)
            return {"run_id": run_id, "outcomes": store(conn).selection_outcomes(namespace, run_id)}
        return safe(run, required_scope="knowledge:legal:read")

    @mcp.tool()
    def link_legal_dossier(namespace: str, dossier_id: str, work_id: str, relation: str, evidence: str) -> dict:
        """Link a political legislative dossier to an enacted legal work, citing the evidence."""
        return safe(lambda conn: store(conn).link_dossier(namespace, dossier_id, work_id, relation, evidence,
                                                          scopes=who()[1], principal_id=who()[0]),
                    write=True, required_scope="knowledge:legal:write")
