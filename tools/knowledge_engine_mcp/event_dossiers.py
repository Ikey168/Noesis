"""Public, source-bound event dossier workflows."""

from src.kb.event_dossiers import EventDossierStore

DOSSIER_WRITES = {
    "create_event_dossier",
    "revise_event_dossier",
    "create_event_dossier_report",
}
DOSSIER_READS = {
    "inspect_event_dossier",
    "event_dossier_timeline",
    "compare_event_dossier_revisions",
    "export_event_dossier_comparison",
}


def register(mcp, safe, context):
    def call(method, *args, write=False, **kwargs):
        return safe(
            lambda conn: getattr(EventDossierStore(conn, initialize=write), method)(
                *args, principal_id=context()[0], scopes=context()[1], **kwargs
            ),
            write=write,
            required_scope="knowledge:event-dossier:write"
            if write
            else "knowledge:event-dossier:read",
        )

    @mcp.tool()
    def create_event_dossier(
        namespace: str,
        request_key: str,
        event_id: str,
        overrides: list[dict] | None = None,
    ) -> dict:
        """Capture one existing event, its accounts, source revisions and origin lineage."""
        return call(
            "create",
            namespace,
            request_key,
            event_id,
            overrides=overrides or (),
            write=True,
        )

    @mcp.tool()
    def revise_event_dossier(
        namespace: str,
        dossier_id: str,
        expected_revision: int,
        overrides: list[dict] | None = None,
    ) -> dict:
        """Append an immutable dossier revision with reviewed membership decisions."""
        return call(
            "revise",
            namespace,
            dossier_id,
            expected_revision,
            overrides=overrides or (),
            write=True,
        )

    @mcp.tool()
    def inspect_event_dossier(
        namespace: str,
        dossier_id: str,
        revision: int | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict:
        """Inspect a pinned dossier revision under current event and source access."""
        return call(
            "inspect",
            namespace,
            dossier_id,
            revision=revision,
            offset=offset,
            limit=limit,
        )

    @mcp.tool()
    def event_dossier_timeline(
        namespace: str,
        dossier_id: str,
        revision: int | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict:
        """Read bounded event, source, account and resolution history."""
        return call(
            "timeline",
            namespace,
            dossier_id,
            revision=revision,
            offset=offset,
            limit=limit,
        )

    @mcp.tool()
    def compare_event_dossier_revisions(
        namespace: str, dossier_id: str, from_revision: int, to_revision: int
    ) -> dict:
        """Compare changed accounts and source availability across pinned revisions."""
        return call("compare", namespace, dossier_id, from_revision, to_revision)

    @mcp.tool()
    def export_event_dossier_comparison(
        namespace: str, dossier_id: str, from_revision: int, to_revision: int
    ) -> dict:
        """Export cited before and after accounts, origin evidence and unresolved conflicts."""
        return call(
            "export_comparison", namespace, dossier_id, from_revision, to_revision
        )

    @mcp.tool()
    def create_event_dossier_report(
        namespace: str, request_key: str, dossier_id: str, revision: int | None = None
    ) -> dict:
        """Create an authored report whose source dependencies can be monitored explicitly."""
        return call(
            "create_report",
            namespace,
            request_key,
            dossier_id,
            revision=revision,
            write=True,
        )
