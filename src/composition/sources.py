"""Composition glue for the source-pack runtime (slice C06, #1824-#1827).

Source pins, cursors, schedules and receipts stay authoritative in the
source-pack store; composition only references them:

* schedule ownership - a root claims a source schedule as
  ``composition:<root>`` and releases it when the root is disabled; the
  schedule is removed only when no owner remains (C06.2);
* shared acquisitions - consumers acquire through
  ``SourcePackRuntime.run_shared``, keyed by source version, query,
  namespace/access context and mapping (C06.3);
* aggregate limits - provider accounts cap all consumers together; readiness
  reports exhaustion as its own blocker kind (C06.4);
* upgrade impact - ``SourcePackUpgradeStore`` lists composition plans that
  pin a source and blocks upgrades outside their ranges (C06.1).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


def owner_for(root: str) -> str:
    return f"composition:{root}"


def claim_schedule(conn: Any, root: str, pack_id: str, schedule: Mapping[str, Any], *,
                   principal_id: str) -> dict[str, Any]:
    from src.ingestion.source_pack_runtime import SourcePackRuntime

    runtime = SourcePackRuntime(conn, initialize=False)
    return runtime.claim_schedule(pack_id, owner_for(root), schedule, principal_id=principal_id)


def release_root_schedules(conn: Any, root: str, *, principal_id: str) -> list[dict[str, Any]]:
    """Release every schedule ``root`` owns; shared schedules keep other owners."""

    try:
        rows = conn.execute(
            "SELECT pack_id FROM source_pack_schedule_owners WHERE owner=? ORDER BY pack_id",
            [owner_for(root)]).fetchall()
    except Exception:  # noqa: BLE001 - no runtime tables means nothing to release
        return []
    from src.ingestion.source_pack_runtime import SourcePackRuntime

    runtime = SourcePackRuntime(conn, initialize=False)
    return [runtime.release_schedule(pack_id, owner_for(root), principal_id=principal_id)
            for (pack_id,) in rows]


def install_hooks(coordinator: Any) -> None:
    """Release a disabled root's schedule ownership as part of disable (C05.4)."""

    def on_disable(root: str, _before: Any, _after: Any) -> None:
        release_root_schedules(coordinator.conn, root, principal_id="composition-coordinator")

    if on_disable not in coordinator.hooks["disable"]:
        coordinator.hooks["disable"].append(on_disable)


def source_pins(conn: Any) -> dict[str, dict[str, Any]]:
    """Currently installed source-pack versions, for resume checks."""

    try:
        rows = conn.execute(
            "SELECT c.pack_id, c.version, v.manifest_hash FROM source_pack_current c "
            "JOIN source_pack_versions v ON v.pack_id=c.pack_id AND v.version=c.version").fetchall()
    except Exception:  # noqa: BLE001 - no source-pack store
        return {}
    return {row[0]: {"version": row[1], "manifest_hash": row[2]} for row in rows}


def exhausted_providers(conn: Any, providers: Iterable[Mapping[str, Any]], *,
                        now: Any = None) -> set[str]:
    """Providers whose source accounts have exhausted their aggregate limit."""

    from src.ingestion.source_pack_runtime import SourcePackRuntime

    try:
        runtime = (SourcePackRuntime(conn, initialize=False) if now is None
                   else SourcePackRuntime(conn, initialize=False, now=now))
        exhausted = set()
        for descriptor in providers:
            for source in descriptor.get("source_packs", []):
                try:
                    account = runtime.default_account(source["pack_id"])
                except Exception:  # noqa: BLE001 - uninstalled source pack
                    continue
                if runtime.account_state(account)["exhausted"]:
                    exhausted.add(descriptor["provider_id"])
        return exhausted
    except Exception:  # noqa: BLE001 - no runtime tables
        return set()


__all__ = [
    "claim_schedule",
    "exhausted_providers",
    "install_hooks",
    "owner_for",
    "release_root_schedules",
    "source_pins",
]
