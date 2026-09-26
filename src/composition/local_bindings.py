"""Built-in registered bindings and readiness probes.

Probes observe; they never issue provider requests, acquire data or change
schedules. Each returns ``{"state": ..., "blockers": [...]}`` where blocker
kinds come from the closed set in :data:`src.composition.contracts.BLOCKER_KINDS`
and never carry record content or credential values.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.composition.bindings import register_probe


def _table_rows(conn: Any, table: str, where: str = "") -> int | None:
    if conn is None:
        return None
    try:
        exists = conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]
        ).fetchone()
        if not exists:
            return None
        return int(conn.execute(f"SELECT count(*) FROM {table} {where}").fetchone()[0])
    except Exception:  # noqa: BLE001 - probes are observations, never failures
        return None


def _ready() -> dict[str, Any]:
    return {"state": "ready", "blockers": []}


def _blocked(kind: str, reason: str, **extra: Any) -> dict[str, Any]:
    return {"state": "blocked", "blockers": [{"kind": kind, "reason": reason, **extra}]}


@register_probe("geospatial.local-store", "Local spatial stores are configured and hold data")
def geospatial_local_store(conn: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    features = _table_rows(conn, "geospatial_feature_current", "WHERE lifecycle='active'")
    geometries = _table_rows(conn, "geospatial_geometries")
    if features is None and geometries is None:
        return _blocked("provider-unavailable", "the local spatial store is not configured")
    if not (features or 0) and not (geometries or 0):
        return _blocked("empty-data", "the local spatial store holds no features or geometries")
    return _ready()


@register_probe("geospatial.live-source", "The Berlin WFS source pack is ready for live acquisition")
def geospatial_live_source(conn: Any, context: Mapping[str, Any]) -> dict[str, Any]:
    if conn is None:
        return _blocked("provider-unavailable", "no warehouse connection is configured")
    try:
        from src.kb.geospatial_features import pack_readiness

        readiness = pack_readiness(conn)
    except Exception:  # noqa: BLE001 - probes are observations
        return _blocked("provider-unavailable", "source-pack readiness could not be observed")
    codes = {blocker.get("code") for blocker in readiness.get("blockers", [])}
    if "pack_not_installed" in codes or "pack_disabled" in codes:
        return _blocked("provider-disabled", "the source pack is not installed or is disabled")
    if readiness.get("modes", {}).get("live") != "ready":
        if "license_not_accepted" in codes:
            return _blocked("unverified-live", "source terms have not been accepted for live use")
        return _blocked("unverified-live", "live acquisition has not been verified")
    if context.get("network") != "live":
        return _blocked("unverified-live", "network access is disabled for this caller")
    return _ready()
