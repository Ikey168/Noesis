"""Pinned investigation coverage tools."""

from src.kb.coverage_assessments import CoverageAssessmentStore, READ_SCOPE, WRITE_SCOPE

COVERAGE_WRITES = {"save_coverage_assessment"}
COVERAGE_READS = {
    "inspect_coverage_assessment", "compare_coverage_assessments",
    "export_coverage_comparison",
}


def register(mcp, safe, context):
    def call(method, *args, write=False, **kwargs):
        return safe(
            lambda conn: getattr(CoverageAssessmentStore(conn, initialize=write), method)(
                *args, **kwargs, principal_id=context()[0], scopes=context()[1]
            ),
            write=write,
            required_scope=WRITE_SCOPE if write else READ_SCOPE,
        )

    @mcp.tool()
    def save_coverage_assessment(
        namespace: str, request_key: str, scope: dict, bindings: list[dict],
        record_gap_observations: bool = False,
    ) -> dict:
        """Assess explicit scope cells from pinned pack versions and run receipts."""
        return call("save", namespace, request_key, scope, bindings,
                    record_gap_observations=record_gap_observations, write=True)

    @mcp.tool()
    def inspect_coverage_assessment(
        namespace: str, assessment_id: str, limit: int = 50, offset: int = 0,
    ) -> dict:
        """Inspect bounded coverage cells with source reasons and citations."""
        return call("inspect", namespace, assessment_id, limit=limit, offset=offset)

    @mcp.tool()
    def compare_coverage_assessments(
        namespace: str, before_id: str, after_id: str,
    ) -> dict:
        """Compare pinned assessments and separate scope or pack changes."""
        return call("compare", namespace, before_id, after_id)

    @mcp.tool()
    def export_coverage_comparison(
        namespace: str, before_id: str, after_id: str,
    ) -> dict:
        """Export assessed cells, receipts and limitations for authored reports."""
        return call("export", namespace, before_id, after_id)
