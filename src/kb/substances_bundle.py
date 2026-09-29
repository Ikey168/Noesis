"""The Chemicals and Substances bundle: declared contributions, enablement and readiness (#2212, CH11 #2313).

Composed under the pack/workflow composition contracts
(``docs/architecture/pack-workflow-composition.md``): ``packs/chemicals/pack.json``
(``noesis-pack-v1``) with its ``composition.json`` overlay binds the
``chemicals.substances`` provider plus the shared ``legal.core``,
``products.safety``, ``platform.entity-identity``, ``platform.subscriptions``
and ``platform.source-runtime`` providers. Enablement is a selection change
through the lifecycle coordinator once the bundle is composition-managed;
before cutover a per-namespace flag applies. The bundle adds no scheduler,
permission ledger, project store or entity store.

Disabling the bundle blocks only the chemicals entry points; Legal, Products,
entity identity, subscriptions and the source runtime keep serving every
other root.
"""

from __future__ import annotations

import time
from typing import Any

from src.ingestion.substance_sources import LIVE_VERIFICATION, PROVIDER_CONTRACTS
from src.kb.substances_records import NEVER, READ_SCOPE, RECORD_TYPES, SCHEMA_VERSIONS, SOURCE_PACK

BUNDLE_ID = "chemicals"
MANIFEST = "packs/chemicals/pack.json"
BUNDLE = {
    "bundle": BUNDLE_ID, "version": "1.0.0",
    "architecture": {
        "plan": "docs/architecture/pack-workflow-composition.md",
        "manifest": MANIFEST, "overlay": "packs/chemicals/composition.json",
        "provider": "packs/chemicals/providers/chemicals.substances.json",
        "status": "composed: chemicals.substances plus shared legal, products.safety and platform providers",
    },
    "contributions": {
        "sources": [{"provider": p, "access": c["access"], "live_verification": LIVE_VERIFICATION[p]["status"]}
                    for p, c in PROVIDER_CONTRACTS.items()],
        "source_packs": [{"pack_id": SOURCE_PACK, "version": "1.0.0", "manifest": "config/source_packs/chemicals.json"}],
        "ontology": {"contract": "noesis-substance-record-v1", "record_types": list(RECORD_TYPES),
                     "schema_versions": dict(SCHEMA_VERSIONS)},
        "workflows": {
            "acquisition": {"reuses": ["SourcePackRuntime (substances connector)", "DocumentStore"],
                            "tools": ["substance_source_contracts", "acquire_substance_sources"]},
            "identity": {"reuses": ["canonical_entities", "EntityHistoryStore (identity decisions)"],
                         "tools": ["resolve_substance", "propose_substance_identities", "review_substance_identity",
                                   "revert_substance_identity", "propose_substance_identity_manual",
                                   "list_substance_identity_candidates", "list_unmatched_substances"]},
            "citations": {"reuses": ["LegalStore (exact CELEX/ELI)", "ProductSafetyStore (notice revisions)",
                                     "DocumentStore (literature)"],
                          "tools": ["link_substance_citations", "list_substance_links"]},
            "status_and_dossiers": {"tools": ["substance_status_as_of", "substance_history", "substance_dossier"]},
            "monitoring": {"reuses": ["SubscriptionStore (watermarks, events, outbox)"],
                           "tools": ["create_substance_monitor", "run_substance_monitor", "poll_substance_monitor"]},
        },
    },
    "never": list(NEVER),
}
_DDL = """
CREATE TABLE IF NOT EXISTS chemicals_bundle_state(
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
    if not _table(conn, "chemicals_bundle_state"):
        return True
    row = conn.execute("SELECT enabled FROM chemicals_bundle_state WHERE namespace=?", [namespace]).fetchone()
    return True if row is None else bool(row[0])


def require_enabled(conn: Any, namespace: str) -> None:
    if not is_enabled(conn, namespace):
        raise BundleError("bundle_disabled", "Chemicals and Substances is disabled; Legal, Products and shared "
                                             "providers remain usable")


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
    conn.execute("INSERT INTO chemicals_bundle_state VALUES (?,?,?,?) ON CONFLICT (namespace) DO UPDATE SET "
                 "enabled=excluded.enabled, changed_by=excluded.changed_by, changed_at_ms=excluded.changed_at_ms",
                 [namespace, bool(enabled), principal_id, stamp])
    return {"namespace": namespace, "bundle": BUNDLE_ID, "enabled": bool(enabled), "shared_capabilities_affected": []}


def readiness(conn: Any, namespace: str, *, scopes) -> dict[str, Any]:
    """ready / fixture-only / unavailable per provider, with live verification kept separate."""
    from src.kb.substances_store import SubstanceStore

    scopes = set(scopes)
    if READ_SCOPE not in scopes and "operator" not in scopes:
        raise BundleError("unauthorized", "substances read scope is required")
    store = SubstanceStore(conn, initialize=False)
    providers = {}
    for provider in PROVIDER_CONTRACTS:
        state = store.provider_state(namespace, provider) if _table(conn, "substance_source_runs") else {
            "runs": 0, "last_success_ms": None, "last_evidence_origin": None, "last_status": None}
        status = ("unavailable" if state["last_success_ms"] is None
                  else "ready" if state["last_evidence_origin"] == "live" else "fixture-only")
        providers[provider] = {"status": status, "state": state, "live_verification": LIVE_VERIFICATION[provider]}
    statuses = {p["status"] for p in providers.values()}
    acquisition = "ready" if "ready" in statuses else "fixture-only" if "fixture-only" in statuses else "unavailable"
    return {"bundle": BUNDLE_ID, "namespace": namespace, "enabled": is_enabled(conn, namespace),
            "providers": providers, "entry_points": {"acquisition": acquisition,
                                                     "dossiers": "ready" if store.ready() else "unavailable"},
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
