"""Reviewable source-pinned retrieval pack drafting."""

from src.kb.intake_practice_drafts import IntakePracticeDrafts


def register(mcp, safe, context):
    @mcp.tool()
    def draft_intake_practice_pack(namespace: str, selections: list[dict]) -> dict:
        """Draft prompts from current intake sources or selected Research Bundle items; author review is required."""
        return safe(
            lambda conn: IntakePracticeDrafts(conn).build(
                namespace, selections, principal_id=context()[0], scopes=context()[1],
            ),
            required_scope="knowledge:intake:read",
        )

    @mcp.tool()
    def create_reviewed_intake_practice_pack(
        namespace: str, request_key: str, title: str, selections: list[dict],
        draft_sha256: str, reviewed_cards: list[dict],
        interval_days: list[int] | None = None,
    ) -> dict:
        """Create a versioned pack from author-approved answers and pinned sources."""
        return safe(
            lambda conn: IntakePracticeDrafts(conn).create_pack(
                namespace, request_key, title, selections, draft_sha256, reviewed_cards,
                principal_id=context()[0], scopes=context()[1], interval_days=interval_days,
            ),
            write=True, required_scope="knowledge:intake:write",
        )
