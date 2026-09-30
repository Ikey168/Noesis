"""The Agriculture and Food Systems bundle: declared contributions, enablement and readiness (#2213, AF11 #2365).

Composed under the pack/workflow composition contracts
(``docs/architecture/pack-workflow-composition.md``): ``packs/agrifood/pack.json``
(``noesis-pack-v1``) with its ``composition.json`` overlay binds the
``agrifood.core`` provider plus the shared ``economics.core`` (Economics
series storage), ``products.safety`` (RASFF notices), ``geospatial.core``
(place resolution), ``platform.subscriptions`` and ``platform.source-runtime``
providers. Climate & Environment (and weather) records and Economics trade
flows are read by citation only when present - never required, so either can
be disabled - and are reported ``provider_unavailable`` when absent. Enablement is a selection change through the lifecycle coordinator
once the bundle is composition-managed; before cutover a per-namespace flag
applies. The bundle adds no scheduler, permission ledger, project store, entity
store or spatial store.

Disabling the bundle blocks only the agri-food entry points; Economics,
Climate & Environment, Products, Geospatial, subscriptions and the source
runtime keep serving every other root.
"""

from __future__ import annotations

import time
from typing import Any

from src.ingestion.agrifood_sources import LIVE_VERIFICATION, PROVIDER_CONTRACTS
from src.kb.agrifood_records import NEVER, READ_SCOPE, RECORD_TYPES, SCHEMA_VERSIONS, SOURCE_PACK

BUNDLE_ID = "agrifood"
MANIFEST = "packs/agrifood/pack.json"
BUNDLE = {
    "bundle": BUNDLE_ID, "version": "1.0.0",
    "architecture": {
        "plan": "docs/architecture/pack-workflow-composition.md",
        "manifest": MANIFEST, "overlay": "packs/agrifood/composition.json",
        "provider": "packs/agrifood/providers/agrifood.core.json",
        "status": "composed: agrifood.core plus shared economics, products.safety, geospatial and platform "
                  "providers; environment and trade records read by citation when present",
    },
    "contributions": {
        "sources": [{"provider": p, "access": c["access"], "live_verification": LIVE_VERIFICATION[p]["status"]}
                    for p, c in PROVIDER_CONTRACTS.items()],
        "source_packs": [{"pack_id": SOURCE_PACK, "version": "1.0.0", "manifest": "config/source_packs/agrifood.json"}],
        "ontology": {"contract": "noesis-agrifood-record-v1", "record_types": list(RECORD_TYPES),
                     "schema_versions": dict(SCHEMA_VERSIONS)},
        "workflows": {
            "acquisition": {"reuses": ["SourcePackRuntime (agrifood connector)", "SDMXConnector (Eurostat)",
                                       "ObservationStore (Economics series storage)"],
                            "tools": ["agrifood_source_contracts", "acquire_agrifood_sources"]},
            "identity": {"reuses": ["OntologyAlignmentStore (code lists, mapping kinds)",
                                    "GeospatialStore (place resolution)"],
                         "tools": ["resolve_agrifood_commodity", "resolve_agrifood_place", "publish_agrifood_codelists",
                                   "register_agrifood_places", "propose_agrifood_crosswalks",
                                   "propose_agrifood_crosswalk_manual", "review_agrifood_crosswalk",
                                   "revert_agrifood_crosswalk", "list_agrifood_crosswalks",
                                   "list_unmapped_agrifood_codes"]},
            "citations": {"reuses": ["ProductSafetyStore (RASFF notices)", "EnvironmentStore (climate, weather)",
                                     "Economics trade-flow reader (provider_unavailable when absent)"],
                          "tools": ["link_agrifood_citations", "list_agrifood_links"]},
            "series": {"reuses": ["environment_vintages.diff_values"],
                       "tools": ["agrifood_series_as_of", "agrifood_revision_history"]},
            "monitoring": {"reuses": ["SubscriptionStore (watermarks, events, outbox)"],
                           "tools": ["create_agrifood_monitor", "run_agrifood_monitor", "poll_agrifood_monitor"]},
        },
    },
    "never": list(NEVER),
}
_DDL = """
CREATE TABLE IF NOT EXISTS agrifood_bundle_state(
 namespace TEXT PRIMARY KEY, enabled BOOLEAN NOT NULL, changed_by TEXT NOT NULL, changed_at_ms BIGINT NOT NULL);
"""


class BundleError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _table(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def _coordinator(conn: Any):
    if not _table(conn, "composition_authority"):
        return None
    from src.composition.lifecycle import CompositionCoordinator

    coordinator = CompositionCoordinator(conn)
    return coordinator if coordinator.is_composition_managed(BUNDLE_ID) else None


def _selected(coordinator) -> bool:
    plan = (coordinator.active() or {}).get("plan") or {}
    return any(p["id"] == BUNDLE_ID for p in plan.get("packs") or [])


def is_enabled(conn: Any, namespace: str) -> bool:
    coordinator = _coordinator(conn)
    if coordinator is not None:
        return _selected(coordinator)
    if not _table(conn, "agrifood_bundle_state"):
        return True
    row = conn.execute("SELECT enabled FROM agrifood_bundle_state WHERE namespace=?", [namespace]).fetchone()
    return True if row is None else bool(row[0])


def require_enabled(conn: Any, namespace: str) -> None:
    if not is_enabled(conn, namespace):
        raise BundleError("bundle_disabled", "Agriculture and Food Systems is disabled; Economics, Climate & "
                                             "Environment, Products, Geospatial and shared providers remain usable")


def set_enabled(conn: Any, namespace: str, enabled: bool, *, principal_id: str, scopes, now=None) -> dict[str, Any]:
    if "operator" not in set(scopes):
        raise BundleError("unauthorized", "operator scope is required to change bundle enablement")
    stamp = (now or (lambda: int(time.time() * 1000)))()
    coordinator = _coordinator(conn)
    if coordinator is not None:
        key = f"{BUNDLE_ID}:{'enable' if enabled else 'disable'}:{principal_id}:{stamp}"
        if enabled:
            manifest = next(m for m in coordinator.installed("pack") if m["id"] == BUNDLE_ID)
            coordinator.select(BUNDLE_ID, f"^{manifest['version']}")
            receipt = coordinator.activate(key)
        elif _selected(coordinator):
            receipt = coordinator.disable(BUNDLE_ID, key)
        else:
            receipt = None
        return {"namespace": namespace, "bundle": BUNDLE_ID, "enabled": _selected(coordinator), "scope": "deployment",
                "authority": "composition-coordinator", "receipt": receipt, "shared_capabilities_affected": []}
    conn.execute(_DDL)
    conn.execute("INSERT INTO agrifood_bundle_state VALUES (?,?,?,?) ON CONFLICT (namespace) DO UPDATE SET "
                 "enabled=excluded.enabled, changed_by=excluded.changed_by, changed_at_ms=excluded.changed_at_ms",
                 [namespace, bool(enabled), principal_id, stamp])
    return {"namespace": namespace, "bundle": BUNDLE_ID, "enabled": bool(enabled), "shared_capabilities_affected": []}


def readiness(conn: Any, namespace: str, *, scopes) -> dict[str, Any]:
    """ready / fixture-only / unavailable per provider, with live verification kept separate."""
    from src.kb.agrifood_store import AgrifoodStore

    scopes = set(scopes)
    if READ_SCOPE not in scopes and "operator" not in scopes:
        raise BundleError("unauthorized", "agrifood read scope is required")
    store = AgrifoodStore(conn, initialize=False)
    providers = {}
    for provider in PROVIDER_CONTRACTS:
        state = store.provider_state(namespace, provider)
        status = ("unavailable" if state["last_success_ms"] is None
                  else "ready" if state["last_evidence_origin"] == "live" else "fixture-only")
        providers[provider] = {"status": status, "state": state, "live_verification": LIVE_VERIFICATION[provider]}
    statuses = {p["status"] for p in providers.values()}
    acquisition = "ready" if "ready" in statuses else "fixture-only" if "fixture-only" in statuses else "unavailable"
    return {"bundle": BUNDLE_ID, "namespace": namespace, "enabled": is_enabled(conn, namespace),
            "providers": providers,
            "entry_points": {"acquisition": acquisition, "series": "ready" if store.ready() else "unavailable",
                             "trade_links": "provider_unavailable (no Economics trade-flow provider composed)"},
            "evidence": "offline fixture results and live results are reported separately; see LIVE_VERIFICATION",
            **_composition_readiness(conn, scopes)}


def _composition_readiness(conn: Any, scopes: set[str]) -> dict[str, Any]:
    coordinator = _coordinator(conn)
    if coordinator is None or not _selected(coordinator):
        return {}
    assessment = coordinator.readiness(scopes=scopes)
    plan = coordinator.active()["plan"]
    mine = {b["capability"] for b in plan["bindings"] if BUNDLE_ID in b["consumers"]}
    assessment["operations"] = [o for o in assessment["operations"] if o["capability"] in mine]
    return {"composition": assessment}


__all__ = ["BUNDLE", "BUNDLE_ID", "BundleError", "is_enabled", "readiness", "require_enabled", "set_enabled"]
