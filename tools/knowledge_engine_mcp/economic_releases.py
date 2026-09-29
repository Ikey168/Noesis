"""Bounded public operations for retained economic releases and comparisons."""

from src.domains.economic.releases import EconomicReleaseStore

ECONOMIC_RELEASE_WRITES = {
    "create_economic_release_snapshot",
    "compare_economic_release_snapshots",
    "create_economic_comparison_report",
}
ECONOMIC_RELEASE_READS = {
    "inspect_economic_release_snapshot",
    "inspect_economic_release_comparison",
    "export_economic_release_comparison",
}


def register(mcp, safe, context):
    def call(method, *args, write=False, **kwargs):
        return safe(
            lambda conn: getattr(EconomicReleaseStore(conn, initialize=write), method)(
                *args, principal_id=context()[0], scopes=context()[1], **kwargs
            ),
            write=write,
            required_scope="knowledge:economic:write"
            if write
            else "knowledge:economic:read",
        )

    @mcp.tool()
    def create_economic_release_snapshot(
        namespace: str,
        request_key: str,
        release_id: str,
        release_cutoff_ms: int,
        acquired_cutoff_ms: int,
        series: list[dict],
        domain: str = "economics",
    ) -> dict:
        """Capture exact retained vintages at distinct publication and acquisition cutoffs."""
        return call(
            "create_snapshot",
            namespace,
            request_key,
            release_id,
            release_cutoff_ms=release_cutoff_ms,
            acquired_cutoff_ms=acquired_cutoff_ms,
            series=series,
            domain=domain,
            write=True,
        )

    @mcp.tool()
    def inspect_economic_release_snapshot(
        namespace: str,
        snapshot_id: str,
        offset: int = 0,
        limit: int = 50,
    ) -> dict:
        """Inspect a stored release snapshot with current source authorization."""
        return call(
            "inspect_snapshot", namespace, snapshot_id, offset=offset, limit=limit
        )

    @mcp.tool()
    def compare_economic_release_snapshots(
        namespace: str,
        request_key: str,
        left_snapshot_id: str,
        right_snapshot_id: str,
        precision: int = 6,
        conversions: list[dict] | None = None,
        assumptions: list[str] | None = None,
    ) -> dict:
        """Separate same-period revisions from new-period changes with calculation receipts."""
        return call(
            "compare",
            namespace,
            request_key,
            left_snapshot_id,
            right_snapshot_id,
            precision=precision,
            conversions=conversions or (),
            assumptions=assumptions or (),
            write=True,
        )

    @mcp.tool()
    def inspect_economic_release_comparison(
        namespace: str,
        comparison_id: str,
        offset: int = 0,
        limit: int = 50,
    ) -> dict:
        """Inspect a pinned economic comparison and its arithmetic receipts."""
        return call(
            "inspect_comparison", namespace, comparison_id, offset=offset, limit=limit
        )

    @mcp.tool()
    def export_economic_release_comparison(namespace: str, comparison_id: str) -> dict:
        """Export the immutable comparison with source and calculation citations."""
        return call("export_comparison", namespace, comparison_id)

    @mcp.tool()
    def create_economic_comparison_report(
        namespace: str, request_key: str, comparison_id: str
    ) -> dict:
        """Create an authored report linked to retained source revisions for change monitoring."""
        return call("create_report", namespace, request_key, comparison_id, write=True)
