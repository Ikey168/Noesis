"""Hand a deployment's bundles from the legacy path to the coordinator (C09.2, C09.3).

The procedure is the one proven in C08.6, applied to every shipped bundle:

1. install every shipped provider descriptor and manifest into the composition
   store (idempotent; an installed version with different content is refused);
2. select the bundles the legacy registry currently has enabled, so the
   deployment's enabled set does not change;
3. activate one generation through the journaled stages;
4. cut each bundle over, so the coordinator is its only enabled-state
   authority; bundles that were not enabled are cut over disabled;
5. verify the source-pack runtime state (versions, current pins, checkpoints,
   watermarks and runs) is byte-for-byte unchanged.

No step acquires data, changes schedules or touches source-pack pins or
cursors. ``NOESIS_COMPOSITION_LIFECYCLE=legacy`` still keeps the authority
hook uninstalled, and :meth:`Coordinator.rollback` restores one bundle's
legacy registration.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.composition.contracts import CompositionError

SOURCE_TABLES = (
    "source_pack_versions",
    "source_pack_current",
    "source_pack_checkpoints",
    "source_pack_watermarks",
    "source_pack_runs",
)


def source_state(conn: Any) -> dict[str, list[str]]:
    """Every source-pack pin, cursor and run row, for before/after comparison."""

    state: dict[str, list[str]] = {}
    for table in SOURCE_TABLES:
        exists = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]).fetchone()
        if exists:
            state[table] = sorted(map(repr, conn.execute(f"SELECT * FROM {table}").fetchall()))
    return state


def legacy_enabled() -> set[str]:
    """Bundles the legacy registry or installer currently has enabled."""

    from src.domains import pack_install
    from src.domains import registry as domain_registry

    return {p.name for p in domain_registry.get_enabled_packs()} | set(pack_install._INSTALLED)


def migrate(
    conn: Any,
    *,
    principal_id: str,
    bundles: Iterable[str] | None = None,
    enabled: Iterable[str] | None = None,
    key: str = "c09-migration",
    coordinator: Any = None,
) -> dict[str, Any]:
    """Migrate ``bundles`` (default: every shipped bundle) to composition management.

    ``enabled`` overrides which bundles are selected (default: the legacy
    registry's enabled set). The result lists the activation status, the
    selected and cut-over bundles and the resulting authorities.
    """

    from src.composition.deployment import candidates
    from src.composition.lifecycle import Coordinator

    coordinator = coordinator or Coordinator(conn)
    before = source_state(conn)
    shipped = candidates()
    for descriptor in shipped["providers"]:
        coordinator.store.install_provider(descriptor, principal_id=principal_id)
    versions = {}
    for manifest in shipped["manifests"]:
        coordinator.store.install_manifest(manifest, principal_id=principal_id)
        versions[manifest["name"]] = manifest["version"]
    names = sorted(versions if bundles is None else bundles)
    unknown = [name for name in names if name not in versions]
    if unknown:
        raise CompositionError("not_installed", f"no shipped manifest for {unknown}")
    wanted = legacy_enabled() if enabled is None else set(enabled)
    selected = []
    for name in names:
        if name in wanted and name not in coordinator.store.selection():
            coordinator.store.select(name, versions[name], principal_id=principal_id)
            selected.append(name)
    activation = None
    if coordinator.store.selection():
        activation = coordinator.activate(f"{key}:activate", principal_id=principal_id)
        if activation["status"] != "applied":
            # Nothing is cut over: the legacy path stays the only authority.
            return {"activation": activation["status"], "error": activation.get("error"),
                    "selected": selected, "cut_over": [], "authorities": coordinator.store.authority()}
    cut_over = []
    for name in names:
        receipt = coordinator.cutover(name, f"{key}:cutover:{name}", principal_id=principal_id)
        if receipt["status"] == "applied":
            cut_over.append(name)
    after = source_state(conn)
    if after != before:
        changed = sorted(t for t in set(before) | set(after) if before.get(t) != after.get(t))
        raise CompositionError("source_state_changed", f"migration changed source-pack state: {changed}")
    return {
        "activation": None if activation is None else activation["status"],
        "generation": None if activation is None else activation.get("new_generation"),
        "selected": selected,
        "cut_over": cut_over,
        "authorities": {name: item["authority"] for name, item in coordinator.store.authority().items()},
        "source_state_unchanged": True,
    }


__all__ = ["SOURCE_TABLES", "legacy_enabled", "migrate", "source_state"]
