"""Reviewed source-pack upgrade impact, application, and retained receipts."""

from src.ingestion.source_pack_upgrades import SourcePackUpgradeStore


def register(mcp, safe, context):
    @mcp.tool()
    def preview_source_pack_upgrade_impact(candidate: dict, limit: int = 100,
                                           offset: int = 0) -> dict:
        """Trace a candidate manifest to accessible saved workflows without writing."""
        principal_id, scopes = context()
        return safe(
            lambda conn: SourcePackUpgradeStore(conn).preview_impact(
                candidate, principal_id=principal_id, scopes=scopes, limit=limit, offset=offset),
            required_scope="knowledge:read",
        )

    @mcp.tool()
    def apply_source_pack_upgrade(candidate: dict, preview_hash: str, impact_hash: str,
                                  apply_key: str, accepted_license_sources: list[str] | None = None,
                                  migrate_schedule: bool = False) -> dict:
        """Apply exactly the reviewed pack and impact snapshot after offline and runtime checks."""
        from src.config.env import resolve_env

        principal_id, scopes = context()
        return safe(
            lambda conn: SourcePackUpgradeStore(conn, initialize=True).apply(
                candidate, preview_hash=preview_hash, impact_hash=impact_hash,
                apply_key=apply_key, principal_id=principal_id, scopes=scopes,
                accepted_license_sources=accepted_license_sources or (),
                migrate_schedule=migrate_schedule,
                secret_available=lambda ref: bool(resolve_env(ref)),
            ),
            write=True,
        )

    @mcp.tool()
    def inspect_source_pack_upgrade_receipt(pack_id: str, apply_key: str) -> dict:
        """Read a retained upgrade receipt and compare it with the current pack pointer."""
        principal_id, scopes = context()
        return safe(
            lambda conn: SourcePackUpgradeStore(conn).inspect_receipt(
                pack_id, apply_key, principal_id=principal_id, scopes=scopes),
            required_scope="operator",
        )
