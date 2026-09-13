"""Ten-mode session tools on the supported Knowledge Engine MCP server."""

from src.kb.intake_exploration import IntakeExplorationStore
from src.kb.intake_inbox import IntakeInboxStore
from src.kb.intake_modes import IntakeStore, discover_modes, route_mode, verify_export

INTAKE_WRITES = {
    "start_intake_mode",
    "command_intake_mode",
    "subscribe_intake_feed",
    "refresh_intake_feed_inbox",
    "mark_intake_feed_read",
    "decide_intake_feed_item",
    "start_awareness_from_inbox",
    "triage_awareness_item",
    "triage_awareness_batch",
    "annotate_intake_feed_item",
    "promote_awareness_item",
    "save_intake_feed_signal_rule",
    "capture_exploration_page",
    "visit_exploration_feed_item",
}
INTAKE_READS = {
    "discover_intake_modes",
    "route_intake_mode",
    "inspect_intake_mode",
    "list_intake_modes",
    "export_intake_mode",
    "export_modulo_intake_handoff",
    "verify_intake_mode_export",
    "list_intake_feed_subscriptions",
    "list_intake_feed_inbox",
    "inspect_intake_feed_item",
    "preview_intake_feed_signals",
    "list_intake_feed_signal_rules",
    "preview_intake_feed_signal_rule",
    "inspect_exploration_source",
}


def register(mcp, safe, context):
    @mcp.tool()
    def discover_intake_modes() -> dict:
        """List the ten workflow intents and their session budgets and completion inputs."""
        return discover_modes()

    @mcp.tool()
    def route_intake_mode(
        answers: dict[str, bool], override: str | None = None
    ) -> dict:
        """Suggest a mode from ten intent questions, with an explicit user override."""
        try:
            return route_mode(answers, override=override)
        except Exception as exc:  # noqa: BLE001 - return a typed MCP error
            return {
                "ok": False,
                "error": {
                    "code": getattr(exc, "code", "invalid_route"),
                    "message": str(exc),
                },
            }

    @mcp.tool()
    def start_intake_mode(
        namespace: str,
        mode: str,
        request_key: str,
        intent: str,
        inputs: dict | None = None,
        duration_minutes: int | None = None,
        origin: dict | None = None,
        workspace_links: list[dict] | None = None,
        references: list[dict] | None = None,
    ) -> dict:
        """Start or replay an owner-scoped mode session, optionally linked to a prior mode."""
        return safe(
            lambda conn: IntakeStore(conn).create(
                namespace,
                mode,
                request_key,
                intent=intent,
                inputs=inputs,
                duration_minutes=duration_minutes,
                origin=origin,
                workspace_links=workspace_links,
                references=references,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            write=True,
            required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def inspect_intake_mode(
        namespace: str, session_id: str, revision: int | None = None
    ) -> dict:
        """Inspect current progress or an exact prior revision under current access."""
        return safe(
            lambda conn: IntakeStore(conn, initialize=False).inspect(
                namespace,
                session_id,
                revision=revision,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            required_scope="knowledge:intake:read",
        )

    @mcp.tool()
    def list_intake_modes(
        namespace: str, mode: str | None = None, limit: int = 50, offset: int = 0
    ) -> dict:
        """Page through accessible sessions, including interrupted work."""
        return safe(
            lambda conn: IntakeStore(conn, initialize=False).list(
                namespace,
                mode=mode,
                limit=limit,
                offset=offset,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            required_scope="knowledge:intake:read",
        )

    @mcp.tool()
    def export_intake_mode(namespace: str, session_id: str) -> dict:
        """Export the revision chain and digest with current linked access checks."""
        return safe(
            lambda conn: IntakeStore(conn, initialize=False).export(
                namespace,
                session_id,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            required_scope="knowledge:intake:read",
        )

    @mcp.tool()
    def export_modulo_intake_handoff(namespace: str, session_id: str) -> dict:
        """Project current Noesis and Modulo object links for a scoped bridge handoff."""
        return safe(
            lambda conn: IntakeStore(conn, initialize=False).modulo_handoff(
                namespace,
                session_id,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            required_scope="knowledge:intake:read",
        )

    @mcp.tool()
    def verify_intake_mode_export(bundle: dict) -> dict:
        """Check an exported session revision chain and digest offline."""
        return verify_export(bundle)

    @mcp.tool()
    def command_intake_mode(
        namespace: str,
        session_id: str,
        command_key: str,
        expected_revision: int,
        action: str,
        payload: dict | None = None,
    ) -> dict:
        """Record mode evidence, pause, resume, complete, or cancel with safe replay."""
        return safe(
            lambda conn: IntakeStore(conn).command(
                namespace,
                session_id,
                command_key,
                expected_revision=expected_revision,
                action=action,
                payload=payload,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            write=True,
            required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def subscribe_intake_feed(
        namespace: str, url: str, name: str, source_kind: str = "rss_atom"
    ) -> dict:
        """Register a public HTTPS RSS/Atom or newsletter feed for this caller."""
        return safe(
            lambda conn: IntakeInboxStore(conn).subscribe(
                namespace,
                url,
                name,
                source_kind,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            write=True,
            required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def list_intake_feed_subscriptions(namespace: str) -> dict:
        """List this caller's configured intake feed inputs."""
        return safe(
            lambda conn: IntakeInboxStore(conn, initialize=False).subscriptions(
                namespace, principal_id=context()[0], scopes=context()[1]
            ),
            required_scope="knowledge:intake:read",
        )

    @mcp.tool()
    def refresh_intake_feed_inbox(namespace: str, limit_per_feed: int = 50) -> dict:
        """Fetch configured feeds with bounded network scope and retain read/triage state."""
        return safe(
            lambda conn: IntakeInboxStore(conn).refresh(
                namespace,
                principal_id=context()[0],
                scopes=context()[1],
                limit_per_feed=limit_per_feed,
            ),
            write=True,
            required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def list_intake_feed_inbox(
        namespace: str, only_unprocessed: bool = False, limit: int = 50, offset: int = 0
    ) -> dict:
        """Page persistent feed items without marking unseen entries processed."""
        return safe(
            lambda conn: IntakeInboxStore(conn, initialize=False).list(
                namespace,
                principal_id=context()[0],
                scopes=context()[1],
                only_unprocessed=only_unprocessed,
                limit=limit,
                offset=offset,
            ),
            required_scope="knowledge:intake:read",
        )

    @mcp.tool()
    def inspect_intake_feed_item(
        namespace: str, item_id: str, revision: int | None = None
    ) -> dict:
        """Read a source snapshot and its independent read/triage state."""
        return safe(
            lambda conn: IntakeInboxStore(conn, initialize=False).inspect(
                namespace,
                item_id,
                revision=revision,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            required_scope="knowledge:intake:read",
        )

    @mcp.tool()
    def preview_intake_feed_signals(
        namespace: str,
        terms: list[str],
        only_unprocessed: bool = True,
        limit: int = 50,
    ) -> dict:
        """Explain title/content keyword matches without changing read or triage state."""
        return safe(
            lambda conn: IntakeInboxStore(conn, initialize=False).signal_preview(
                namespace,
                terms,
                principal_id=context()[0],
                scopes=context()[1],
                only_unprocessed=only_unprocessed,
                limit=limit,
            ),
            required_scope="knowledge:intake:read",
        )

    @mcp.tool()
    def save_intake_feed_signal_rule(
        namespace: str, name: str, terms: list[str]
    ) -> dict:
        """Save or update a caller-owned, versioned keyword signal rule."""
        return safe(
            lambda conn: IntakeInboxStore(conn).save_signal_rule(
                namespace, name, terms, principal_id=context()[0], scopes=context()[1]
            ),
            write=True,
            required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def list_intake_feed_signal_rules(namespace: str) -> dict:
        """List this caller's saved inbox signal rules."""
        return safe(
            lambda conn: IntakeInboxStore(conn, initialize=False).signal_rules(
                namespace, principal_id=context()[0], scopes=context()[1]
            ),
            required_scope="knowledge:intake:read",
        )

    @mcp.tool()
    def preview_intake_feed_signal_rule(
        namespace: str, rule_id: str, only_unprocessed: bool = True, limit: int = 50
    ) -> dict:
        """Explain matches for a saved rule without changing inbox state."""
        return safe(
            lambda conn: IntakeInboxStore(conn, initialize=False).preview_signal_rule(
                namespace,
                rule_id,
                principal_id=context()[0],
                scopes=context()[1],
                only_unprocessed=only_unprocessed,
                limit=limit,
            ),
            required_scope="knowledge:intake:read",
        )

    @mcp.tool()
    def mark_intake_feed_read(
        namespace: str, item_id: str, command_key: str, read: bool = True
    ) -> dict:
        """Set or clear read state with a durable idempotency key."""
        return safe(
            lambda conn: IntakeInboxStore(conn).mark_read(
                namespace,
                item_id,
                command_key,
                read=read,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            write=True,
            required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def decide_intake_feed_item(
        namespace: str, item_id: str, command_key: str, decision: str
    ) -> dict:
        """Record watch, escalate, schedule, discard, archive, or flag without resetting read state."""
        return safe(
            lambda conn: IntakeInboxStore(conn).decide(
                namespace,
                item_id,
                command_key,
                decision=decision,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            write=True,
            required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def start_awareness_from_inbox(
        namespace: str,
        request_key: str,
        intent: str = "Daily feed triage",
        duration_minutes: int = 15,
        workspace_links: list[dict] | None = None,
    ) -> dict:
        """Start or replay a bounded Awareness queue from unprocessed inbox items."""
        return safe(
            lambda conn: IntakeInboxStore(conn).start_awareness(
                namespace,
                request_key,
                intent=intent,
                duration_minutes=duration_minutes,
                workspace_links=workspace_links,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            write=True,
            required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def triage_awareness_item(
        namespace: str,
        session_id: str,
        item_id: str,
        command_key: str,
        expected_revision: int,
        decision: str,
    ) -> dict:
        """Atomically record one item decision in the inbox and Awareness session."""
        return safe(
            lambda conn: IntakeInboxStore(conn).triage_awareness(
                namespace,
                session_id,
                item_id,
                command_key,
                expected_revision=expected_revision,
                decision=decision,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            write=True,
            required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def triage_awareness_batch(
        namespace: str,
        session_id: str,
        decisions: dict[str, str],
        command_key: str,
        expected_revision: int,
    ) -> dict:
        """Atomically triage 1–100 queued items and record one session revision."""
        return safe(
            lambda conn: IntakeInboxStore(conn).triage_awareness_batch(
                namespace,
                session_id,
                decisions,
                command_key,
                expected_revision=expected_revision,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            write=True,
            required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def annotate_intake_feed_item(
        namespace: str,
        item_id: str,
        request_key: str,
        body: str,
        locator: dict | None = None,
    ) -> dict:
        """Attach an immutable user annotation to the retained feed source."""
        return safe(
            lambda conn: IntakeInboxStore(conn).annotate(
                namespace,
                item_id,
                request_key,
                body,
                locator=locator,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            write=True,
            required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def promote_awareness_item(
        namespace: str,
        awareness_session_id: str,
        item_id: str,
        request_key: str,
        target_mode: str,
        reason: str,
        intent: str,
    ) -> dict:
        """Promote an escalated item with its source and annotation references."""
        return safe(
            lambda conn: IntakeInboxStore(conn).promote_awareness_item(
                namespace,
                awareness_session_id,
                item_id,
                request_key,
                target_mode=target_mode,
                reason=reason,
                intent=intent,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            write=True,
            required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def capture_exploration_page(
        namespace: str,
        session_id: str,
        command_key: str,
        expected_revision: int,
        url: str,
        title: str,
        note: str = "",
        saved: bool = False,
        content: str | None = None,
        fetch_readable: bool = False,
    ) -> dict:
        """Visit or save a public page in Exploration; live extraction needs intake fetch scope."""
        return safe(
            lambda conn: IntakeExplorationStore(conn).capture(
                namespace,
                session_id,
                command_key,
                expected_revision=expected_revision,
                url=url,
                title=title,
                note=note,
                saved=saved,
                content=content,
                fetch_readable=fetch_readable,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            write=True,
            required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def visit_exploration_feed_item(
        namespace: str,
        session_id: str,
        item_id: str,
        command_key: str,
        expected_revision: int,
        note: str = "",
        saved: bool = False,
    ) -> dict:
        """Add a feed item to an Exploration trail without recapturing its source."""
        return safe(
            lambda conn: IntakeExplorationStore(conn).link_feed_item(
                namespace,
                session_id,
                item_id,
                command_key,
                expected_revision=expected_revision,
                note=note,
                saved=saved,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            write=True,
            required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def inspect_exploration_source(
        namespace: str,
        source_id: str,
        version: int | None = None,
    ) -> dict:
        """Inspect a current or historical readable Exploration source snapshot."""
        return safe(
            lambda conn: IntakeExplorationStore(conn, initialize=False).inspect_source(
                namespace,
                source_id,
                version=version,
                principal_id=context()[0],
                scopes=context()[1],
            ),
            required_scope="knowledge:intake:read",
        )
