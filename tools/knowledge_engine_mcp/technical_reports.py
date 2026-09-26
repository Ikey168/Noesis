"""Public technical dependency impact report tools."""

from src.domains.technical.impact_reports import ImpactReportStore, READ_SCOPE, WRITE_SCOPE


TECHNICAL_REPORT_WRITES = {"create_technical_impact_report"}


def register(mcp, safe, context):
    def call(method, *args, write=False, **kwargs):
        principal_id, scopes = context()
        return safe(
            lambda conn: getattr(ImpactReportStore(conn, initialize=write), method)(
                *args, principal_id=principal_id, scopes=scopes, **kwargs),
            write=write, required_scope=WRITE_SCOPE if write else READ_SCOPE,
        )

    @mcp.tool()
    def create_technical_impact_report(namespace: str, request_key: str, inventory_id: str,
                                       project_id: str | None = None, limit: int = 100,
                                       offset: int = 0) -> dict:
        """Pin one bounded dependency assessment and its available source revisions."""
        return call("create", namespace, request_key, inventory_id, project_id=project_id,
                    limit=limit, offset=offset, write=True)

    @mcp.tool()
    def inspect_technical_impact_report(namespace: str, report_id: str) -> dict:
        """Inspect a retained report after rechecking inventory and source access."""
        return call("inspect", namespace, report_id)

    @mcp.tool()
    def compare_technical_impact_reports(namespace: str, left_report_id: str,
                                         right_report_id: str) -> dict:
        """Compare exact inventory and evidence snapshot hashes and finding changes."""
        return call("compare", namespace, left_report_id, right_report_id)

    @mcp.tool()
    def export_technical_impact_report(namespace: str, report_id: str) -> dict:
        """Export cited findings plus compatible project and authored-report references."""
        return call("export", namespace, report_id)
