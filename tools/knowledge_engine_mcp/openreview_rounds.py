"""Public OpenReview round and concern tools with current source access checks."""

from src.domains.research.openreview_rounds import OpenReviewRoundStore, READ_SCOPE, WRITE_SCOPE

OPENREVIEW_WRITES = {"save_openreview_round_set"}
OPENREVIEW_READS = {
    "inspect_openreview_round_set", "inspect_openreview_round",
    "compare_openreview_rounds", "export_openreview_round_comparison",
    "openreview_concern_review_target", "assess_openreview_concern",
}


def register(mcp, safe, context):
    def call(method, *args, write=False, **kwargs):
        return safe(
            lambda conn: getattr(OpenReviewRoundStore(conn, initialize=write), method)(
                *args, **kwargs, principal_id=context()[0], scopes=context()[1]
            ),
            write=write,
            required_scope=WRITE_SCOPE if write else READ_SCOPE,
        )

    @mcp.tool()
    def save_openreview_round_set(
        namespace: str, request_key: str, forum_id: str, source_refs: list[dict],
        concerns: list[dict] | None = None, round_set_id: str | None = None,
        expected_revision: int | None = None, paper_family: dict | None = None,
    ) -> dict:
        """Save a bounded public forum snapshot from exact note revisions."""
        return call("save", namespace, request_key, forum_id, source_refs,
                    concerns=concerns or [], round_set_id=round_set_id,
                    expected_revision=expected_revision, paper_family=paper_family,
                    write=True)

    @mcp.tool()
    def inspect_openreview_round_set(
        namespace: str, round_set_id: str, revision: int | None = None,
        limit: int = 50, offset: int = 0,
    ) -> dict:
        """Inspect notes, ambiguous membership and concerns in one snapshot."""
        return call("inspect", namespace, round_set_id, revision=revision,
                    limit=limit, offset=offset)

    @mcp.tool()
    def inspect_openreview_round(
        namespace: str, round_set_id: str, round_id: str,
        revision: int | None = None, limit: int = 50, offset: int = 0,
    ) -> dict:
        """Inspect a pinned public review round with source citations."""
        return call("inspect_round", namespace, round_set_id, round_id,
                    revision=revision, limit=limit, offset=offset)

    @mcp.tool()
    def compare_openreview_rounds(
        namespace: str, round_set_id: str, before: dict, after: dict,
        review_tasks: dict[str, str] | None = None,
    ) -> dict:
        """Compare two pinned public rounds or revisions."""
        return call("compare", namespace, round_set_id, before, after,
                    review_tasks=review_tasks)

    @mcp.tool()
    def export_openreview_round_comparison(
        namespace: str, round_set_id: str, before: dict, after: dict,
        review_tasks: dict[str, str] | None = None,
    ) -> dict:
        """Export cited concern, response and manuscript changes for a report."""
        return call("export", namespace, round_set_id, before, after,
                    review_tasks=review_tasks)

    @mcp.tool()
    def openreview_concern_review_target(
        namespace: str, round_set_id: str, concern_key: str,
        revision: int | None = None,
    ) -> dict:
        """Get the exact target and sources for an independent ReviewInbox task."""
        return call("concern_review_target", namespace, round_set_id, concern_key,
                    revision=revision)

    @mcp.tool()
    def assess_openreview_concern(
        namespace: str, round_set_id: str, concern_key: str,
        revision: int | None = None, review_task_id: str | None = None,
    ) -> dict:
        """Read a concern state; verification requires a resolved human task."""
        return call("assess_concern", namespace, round_set_id, concern_key,
                    revision=revision, review_task_id=review_task_id)
