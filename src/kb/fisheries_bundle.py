"""The Fisheries and Maritime Activity bundle: declared contributions, enablement and readiness (#2222, FI12 #2339).

Composed under the pack/workflow composition contracts
(``docs/architecture/pack-workflow-composition.md``): ``packs/fisheries/pack.json``
(``noesis-pack-v1``) with its ``composition.json`` overlay binds the
``fisheries.core`` provider plus the shared ``platform.entity-identity``,
``platform.subscriptions`` and ``platform.source-runtime`` providers. Two
optional features (default off) bind Legal sanctions and Geospatial; OSINT
vessel movements (#2221) and Agriculture & Food Systems are reached through
the citing interface of :mod:`src.kb.fisheries_identity` and report
``provider_unavailable`` when not composed. Readiness reports each provider's
contract status and ``LIVE_VERIFICATION`` separately. The bundle adds no
scheduler, entity store or spatial store; disabling it blocks only the
fisheries entry points.
"""

from __future__ import annotations

import time
from typing import Any

from src.ingestion.fisheries_sources import LIVE_VERIFICATION, PROVIDER_CONTRACTS
from src.kb.fisheries_records import NEVER, READ_SCOPE, RECORD_TYPES, SCHEMA_VERSIONS, SOURCE_PACK

BUNDLE_ID = "fisheries"
MANIFEST = "packs/fisheries/pack.json"
OPTIONAL = {
    "sanctions": {"capability": "legal.sanctions", "store_table": "sanctions_aliases",
                  "used_by": ["link_fisheries_citations (sanctions)"]},
    "geospatial": {"capability": "geospatial.place-resolution", "store_table": "geospatial_places",
                   "used_by": ["project_fisheries_areas", "link_fisheries_citations (areas)"]},
}
BUNDLE = {
    "bundle": BUNDLE_ID, "version": "1.0.0",
    "architecture": {
        "plan": "docs/architecture/pack-workflow-composition.md",
        "manifest": MANIFEST, "overlay": "packs/fisheries/composition.json",
        "provider": "packs/fisheries/providers/fisheries.core.json",
        "status": "composed: fisheries.core plus shared platform providers; optional sanctions and geospatial "
                  "features; OSINT movements and Agriculture & Food Systems by citation",
    },
    "contributions": {
        "sources": [{"provider": p, "access": c["access"], "live_verification": LIVE_VERIFICATION[p]["status"]}
                    for p, c in PROVIDER_CONTRACTS.items()],
        "source_packs": [{"pack_id": SOURCE_PACK, "version": "1.0.0", "manifest": "config/source_packs/fisheries.json"}],
        "ontology": {"contract": "noesis-fisheries-record-v1", "record_types": list(RECORD_TYPES),
                     "schema_versions": dict(SCHEMA_VERSIONS)},
        "optional_features": {k: {"capability": v["capability"], "default": False, "used_by": v["used_by"]}
                              for k, v in OPTIONAL.items()},
        "citing_providers": ["osint.vessel-movements", "agrifood.food-systems"],
        "workflows": {
            "acquisition": {"reuses": ["SourcePackRuntime (fisheries connector)", "DocumentStore"],
                            "tools": ["fisheries_source_contracts", "acquire_fisheries_sources"]},
            "identity": {"reuses": ["canonical_entities", "EntityHistoryStore (identity decisions)"],
                         "tools": ["propose_fisheries_identities", "review_fisheries_identity",
                                   "revert_fisheries_identity", "list_fisheries_identity_matches",
                                   "fisheries_vessel_identity"]},
            "citations": {"reuses": ["SanctionsStore (read-only, stated IMO)", "GeospatialStore (area codes)"],
                          "tools": ["link_fisheries_citations", "project_fisheries_areas", "list_fisheries_links"]},
            "answers": {"tools": ["fisheries_vessel_status", "fisheries_area_aggregates"]},
            "monitoring": {"reuses": ["SubscriptionStore (watermarks, events, outbox)"],
                           "tools": ["create_fisheries_monitor", "run_fisheries_monitor", "poll_fisheries_monitor"]},
        },
    },
    "never": list(NEVER),
}
_DDL = """
CREATE TABLE IF NOT EXISTS fisheries_bundle_state(
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
    if not _table(conn, "fisheries_bundle_state"):
        return True
    row = conn.execute("SELECT enabled FROM fisheries_bundle_state WHERE namespace=?", [namespace]).fetchone()
    return True if row is None else bool(row[0])


def require_enabled(conn: Any, namespace: str) -> None:
    if not is_enabled(conn, namespace):
        raise BundleError("bundle_disabled", "Fisheries and Maritime Activity is disabled; sanctions, geospatial "
                                             "and shared providers remain usable")


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
    conn.execute("INSERT INTO fisheries_bundle_state VALUES (?,?,?,?) ON CONFLICT (namespace) DO UPDATE SET "
                 "enabled=excluded.enabled, changed_by=excluded.changed_by, changed_at_ms=excluded.changed_at_ms",
                 [namespace, bool(enabled), principal_id, stamp])
    return {"namespace": namespace, "bundle": BUNDLE_ID, "enabled": bool(enabled), "shared_capabilities_affected": []}


def optional_status(conn: Any) -> dict[str, Any]:
    """available / provider_unavailable per optional feature and citing provider; absence degrades cleanly."""
    from src.kb.fisheries_identity import CITING_PROVIDERS, KNOWN_CITING_PROVIDERS

    features = {name: {"capability": spec["capability"],
                       "status": "available" if _table(conn, spec["store_table"]) else "provider_unavailable"}
                for name, spec in OPTIONAL.items()}
    citing = {name: {"description": text,
                     "status": "available" if name in CITING_PROVIDERS else "provider_unavailable"}
              for name, text in KNOWN_CITING_PROVIDERS.items()}
    return {"features": features, "citing_providers": citing}


def readiness(conn: Any, namespace: str, *, scopes) -> dict[str, Any]:
    """ready / fixture-only / unavailable per provider, with live verification kept separate."""
    from src.kb.fisheries_store import FisheriesStore

    scopes = set(scopes)
    if READ_SCOPE not in scopes and "operator" not in scopes:
        raise BundleError("unauthorized", "fisheries read scope is required")
    store = FisheriesStore(conn, initialize=False)
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        state = store.provider_state(namespace, provider) if _table(conn, "fisheries_source_runs") else {
            "runs": 0, "last_success_ms": None, "last_evidence_origin": None, "last_status": None}
        status = ("unavailable" if state["last_success_ms"] is None
                  else "ready" if state["last_evidence_origin"] == "live" else "fixture-only")
        providers[provider] = {"status": status, "state": state, "contract_status": contract["access_decision"],
                               "live_verification": LIVE_VERIFICATION[provider]}
    statuses = {p["status"] for p in providers.values()}
    acquisition = "ready" if "ready" in statuses else "fixture-only" if "fixture-only" in statuses else "unavailable"
    return {"bundle": BUNDLE_ID, "namespace": namespace, "enabled": is_enabled(conn, namespace),
            "providers": providers, "optional": optional_status(conn),
            "entry_points": {"acquisition": acquisition, "answers": "ready" if store.ready() else "unavailable"},
            "evidence": "offline fixture results and live results are reported separately; see LIVE_VERIFICATION "
                        "and docs/development/fisheries-evidence/README.md",
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


__all__ = ["BUNDLE", "BUNDLE_ID", "BundleError", "OPTIONAL", "is_enabled", "optional_status", "readiness",
           "require_enabled", "set_enabled"]
