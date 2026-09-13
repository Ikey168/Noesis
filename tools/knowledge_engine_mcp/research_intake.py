"""Project-bound Deep Research synthesis tools."""

import json

from src.kb.intake_modes import IntakeError
from src.kb.intake_research_bundle import (
    IntakeResearchBundleStore,
    verify_research_bundle_export,
)
from src.kb.intake_research_progress import inspect_research_progress

RESEARCH_INTAKE_WRITES = {"save_intake_research_bundle"}


def register(mcp, safe, context):
    def read(namespace, bundle_id, revision=None):
        return safe(
            lambda conn: IntakeResearchBundleStore(conn, initialize=False).inspect(
                namespace, bundle_id, revision=revision,
                principal_id=context()[0], scopes=context()[1],
            ),
            required_scope="knowledge:intake:read",
        )

    @mcp.tool()
    def inspect_intake_research_progress(namespace: str, session_id: str) -> dict:
        """Read a paired topic's project, loop stage receipts, coverage, blockers, and bundle readiness."""
        return safe(
            lambda conn: inspect_research_progress(
                conn, namespace, session_id,
                principal_id=context()[0], scopes=context()[1],
            ),
            required_scope="knowledge:intake:read",
        )

    @mcp.tool()
    def save_intake_research_bundle(
        namespace: str, project_id: str, command_key: str, document: dict,
        expected_revision: int | None = None,
    ) -> dict:
        """Save a versioned research synthesis with exact citations to pinned project sources."""
        return safe(
            lambda conn: IntakeResearchBundleStore(conn).save(
                namespace, project_id, command_key, document,
                expected_revision=expected_revision,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def inspect_intake_research_bundle(
        namespace: str, bundle_id: str, revision: int | None = None,
    ) -> dict:
        """Inspect an exact synthesis revision and recheck cited sources and project access."""
        return read(namespace, bundle_id, revision)

    @mcp.tool()
    def export_intake_research_bundle(namespace: str, bundle_id: str) -> dict:
        """Export the revision chain while current access to every cited source remains valid."""
        return safe(
            lambda conn: IntakeResearchBundleStore(conn, initialize=False).export(
                namespace, bundle_id, principal_id=context()[0], scopes=context()[1],
            ),
            required_scope="knowledge:intake:read",
        )

    @mcp.tool()
    def verify_intake_research_bundle_export(bundle: dict) -> dict:
        """Verify the exported revision chain digest offline."""
        return verify_research_bundle_export(bundle)

    @mcp.resource("noesis://intake/research-bundles/{namespace}/{bundle_id}", mime_type="application/json")
    def intake_research_bundle_resource(namespace: str, bundle_id: str) -> str:
        value = read(namespace, bundle_id)
        if value.get("ok") is False:
            raise IntakeError(value["error"]["code"], value["error"]["message"])
        return json.dumps(value, sort_keys=True, ensure_ascii=False)

    @mcp.resource("noesis://intake/research-bundles/{namespace}/{bundle_id}/revisions/{revision}", mime_type="application/json")
    def intake_research_bundle_revision_resource(namespace: str, bundle_id: str, revision: int) -> str:
        value = read(namespace, bundle_id, revision)
        if value.get("ok") is False:
            raise IntakeError(value["error"]["code"], value["error"]["message"])
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
