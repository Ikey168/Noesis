"""Dry-run migration inventory for dedicated Modulo Knowledge plugins."""

from src.kb.intake_modulo_migration import ModuloMigrationStore

MIGRATION_WRITES = {"preview_modulo_intake_migration", "import_modulo_flashcards"}
MIGRATION_READS = {"inspect_modulo_intake_migration"}


def register(mcp, safe, context):
    @mcp.tool()
    def preview_modulo_intake_migration(
        namespace: str, request_key: str, inventory: dict, mappings: list[dict],
    ) -> dict:
        """Persist a bounded, metadata-only plugin inventory and conflict preview."""
        return safe(
            lambda conn: ModuloMigrationStore(conn).preview(
                namespace, request_key, inventory, mappings,
                principal_id=context()[0], scopes=context()[1]),
            write=True, required_scope="knowledge:intake:write",
        )

    @mcp.tool()
    def inspect_modulo_intake_migration(
        namespace: str, preview_id: str, limit: int = 100, offset: int = 0,
    ) -> dict:
        """Read the versioned original-record plan under current owner access."""
        return safe(
            lambda conn: ModuloMigrationStore(conn, initialize=False).inspect(
                namespace, preview_id, limit=limit, offset=offset,
                principal_id=context()[0], scopes=context()[1]),
            required_scope="knowledge:intake:read",
        )

    @mcp.tool()
    def import_modulo_flashcards(
        namespace: str, preview_id: str, request_key: str, title: str,
        source_values: list[dict],
    ) -> dict:
        """Create a native pack while retaining exact plugin IDs, history and unmapped schedule data."""
        return safe(
            lambda conn: ModuloMigrationStore(conn, initialize=False).import_flashcards(
                namespace, preview_id, request_key, title=title,
                source_values=source_values,
                principal_id=context()[0], scopes=context()[1],
            ),
            write=True, required_scope="knowledge:intake:write",
        )
