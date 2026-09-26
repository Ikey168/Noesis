"""Scoped legislative dossier tools for acquired official records."""

from src.domains.political.legislative_dossiers import (
    LegislativeDossierStore,
    READ_SCOPE,
    WRITE_SCOPE,
)

POLITICAL_DOSSIER_WRITES = {"save_legislative_dossier"}
POLITICAL_DOSSIER_READS = {
    "inspect_legislative_dossier", "legislative_dossier_timeline",
    "compare_legislative_dossier", "export_legislative_dossier_changes",
    "legislative_dossier_dependencies",
}


def register(mcp, safe, context):
    def call(method, *args, write=False, **kwargs):
        return safe(
            lambda conn: getattr(LegislativeDossierStore(conn, initialize=write), method)(
                *args, **kwargs, principal_id=context()[0], scopes=context()[1]
            ),
            write=write,
            required_scope=WRITE_SCOPE if write else READ_SCOPE,
        )

    @mcp.tool()
    def save_legislative_dossier(
        namespace: str, request_key: str, jurisdiction: str, procedure_id: str,
        source_refs: list[dict], dossier_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict:
        """Save a versioned dossier from exact acquired DIP or EUR-Lex revisions."""
        return call("save", namespace, request_key, jurisdiction, procedure_id,
                    source_refs, dossier_id=dossier_id,
                    expected_revision=expected_revision, write=True)

    @mcp.tool()
    def inspect_legislative_dossier(
        namespace: str, dossier_id: str, revision: int | None = None,
        limit: int = 50, offset: int = 0,
    ) -> dict:
        """Inspect one dossier revision with current source access checks."""
        return call("inspect", namespace, dossier_id, revision=revision,
                    limit=limit, offset=offset)

    @mcp.tool()
    def legislative_dossier_timeline(
        namespace: str, dossier_id: str, revision: int | None = None,
        observed_as_of_ms: int | None = None, limit: int = 50, offset: int = 0,
    ) -> dict:
        """Order source-backed stages without leaking later observations."""
        return call("timeline", namespace, dossier_id, revision=revision,
                    observed_as_of_ms=observed_as_of_ms, limit=limit, offset=offset)

    @mcp.tool()
    def compare_legislative_dossier(
        namespace: str, dossier_id: str, before_revision: int, after_revision: int,
    ) -> dict:
        """Compare exact dossier revisions and their before/after citations."""
        return call("compare", namespace, dossier_id, before_revision, after_revision)

    @mcp.tool()
    def export_legislative_dossier_changes(
        namespace: str, dossier_id: str, before_revision: int, after_revision: int,
    ) -> dict:
        """Export report-ready recorded changes with separate interpretation."""
        return call("export_change_summary", namespace, dossier_id,
                    before_revision, after_revision)

    @mcp.tool()
    def legislative_dossier_dependencies(
        namespace: str, dossier_id: str, revision: int | None = None,
    ) -> dict:
        """Return existing project/report links and explicit evidence changes."""
        return call("dependencies", namespace, dossier_id, revision=revision)
