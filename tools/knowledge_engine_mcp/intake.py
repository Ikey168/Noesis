"""Ten-mode session tools on the supported Knowledge Engine MCP server."""

from src.kb.intake_modes import IntakeStore, discover_modes, route_mode, verify_export

INTAKE_WRITES = {"start_intake_mode", "command_intake_mode"}
INTAKE_READS = {
    "discover_intake_modes",
    "route_intake_mode",
    "inspect_intake_mode",
    "list_intake_modes",
    "export_intake_mode",
    "export_modulo_intake_handoff",
    "verify_intake_mode_export",
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
