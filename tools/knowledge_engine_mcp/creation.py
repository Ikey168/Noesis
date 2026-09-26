"""Creation-project orchestration over the existing authored-report store."""

from src.kb.intake_creation import IntakeCreationStore


# Deployment-owned build adapters keyed by artifact type; none are public.
CREATION_BUILD_ADAPTERS: dict = {}


def register(mcp, safe, context):
    @mcp.tool()
    def start_intake_creation(
        namespace: str, request_key: str, title: str, audience: str,
        artifact_type: str, purpose: str, criteria: list[str],
        inputs: list[dict] | None = None, workspace_links: list[dict] | None = None,
        plugin_links: list[dict] | None = None,
    ) -> dict:
        """Start a purpose- and audience-defined project for a supported authored artifact."""
        return safe(
            lambda conn: IntakeCreationStore(conn).create(
                namespace, request_key, title=title, audience=audience,
                artifact_type=artifact_type, purpose=purpose, criteria=criteria,
                inputs=inputs, workspace_links=workspace_links,
                plugin_links=plugin_links,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def inspect_intake_creation(
        namespace: str, project_id: str, revision: int | None = None,
    ) -> dict:
        """Inspect current or historical creation metadata and linked report version."""
        return safe(
            lambda conn: IntakeCreationStore(conn, initialize=False).inspect(
                namespace, project_id, revision=revision,
                principal_id=context()[0], scopes=context()[1],
            ),
            required_scope="knowledge:intake:read",
        )

    @mcp.tool()
    def command_intake_creation(
        namespace: str, project_id: str, command_key: str,
        expected_revision: int, action: str, payload: dict | None = None,
    ) -> dict:
        """Attach a versioned report, review criteria, finish, or reopen with replay."""
        return safe(
            lambda conn: IntakeCreationStore(conn).command(
                namespace, project_id, command_key,
                expected_revision=expected_revision, action=action, payload=payload,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def build_intake_creation_artifact(
        namespace: str, project_id: str, expected_revision: int, idempotency_key: str,
    ) -> dict:
        """Run the configured build adapter once for a finished non-report artifact; never publishes."""
        return safe(
            lambda conn: IntakeCreationStore(conn, build_adapters=CREATION_BUILD_ADAPTERS).build(
                namespace, project_id, expected_revision=expected_revision,
                idempotency_key=idempotency_key,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def export_intake_creation(namespace: str, project_id: str) -> dict:
        """Export a finished project and its authored-report artifact without publishing."""
        return safe(
            lambda conn: IntakeCreationStore(conn, initialize=False).export(
                namespace, project_id, principal_id=context()[0], scopes=context()[1],
            ),
            required_scope="knowledge:intake:read",
        )

    @mcp.tool()
    def handoff_intake_creation(
        namespace: str, project_id: str, request_key: str,
        destination_mode: str, reason: str,
    ) -> dict:
        """Carry a finished current artifact into procedure, practice, or maintenance work."""
        return safe(
            lambda conn: IntakeCreationStore(conn).handoff(
                namespace, project_id, request_key,
                destination_mode=destination_mode, reason=reason,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )
