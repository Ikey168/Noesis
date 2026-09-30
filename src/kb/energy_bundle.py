"""The Energy Systems bundle: declared contributions, enablement and readiness (EN12).

Composed under the pack/workflow composition contracts
(``docs/architecture/pack-workflow-composition.md``): the manifest is
``packs/energy/manifest.json`` binding ``energy.core`` plus the shared
``environment.core`` (ENTSO-E records, read by citation), ``market.core``
(price series), ``geospatial.core`` (place resolution),
``platform.entity-identity``, ``platform.subscriptions`` and
``platform.source-runtime`` providers. Enablement is a selection change through
the lifecycle coordinator once the bundle is composition-managed; before
cutover a per-namespace flag applies. It adds no scheduler, permission ledger,
project store, entity store or spatial store.

Disabling the bundle blocks only the energy entry points; Climate and
Environment, Market, Geospatial and the platform providers keep serving every
other root.
"""

from __future__ import annotations

import time

from src.ingestion.energy_sources import BOUNDED_COVERAGE, LIVE_VERIFICATION, NEVER, PROVIDER_CONTRACTS
from src.kb.energy_records import CONTRACT, READ_SCOPE, RECORD_TYPES

BUNDLE_ID = "energy"
MANIFEST = "packs/energy/manifest.json"
SOURCE_PACK = "config/source_packs/energy.json"
BUNDLE = {
    "bundle": BUNDLE_ID, "version": "0.1.0",
    "title": "Energy Systems",
    "architecture": {
        "plan": "docs/architecture/pack-workflow-composition.md",
        "status": "composed: manifest packs/energy/manifest.json binds energy.core plus shared providers",
        "manifest": MANIFEST,
    },
    "contributions": {
        "sources": [{"provider": p, "decision": c["decision"], "live_verification": LIVE_VERIFICATION[p]["status"]}
                    for p, c in PROVIDER_CONTRACTS.items()],
        "bounded_coverage": BOUNDED_COVERAGE,
        "source_packs": [{"pack_id": "energy-systems", "version": "1.0.0", "manifest": SOURCE_PACK}],
        "ontology": {"contract": CONTRACT, "record_types": list(RECORD_TYPES)},
        "workflows": {
            "acquisition": {"reuses": ["environment_providers entsoe adapter", "SDMXConnector (ESTAT)",
                                       "SourcePackRuntime (energy connector)", "DurableHTTP", "MarketPriceStore"],
                            "tools": ["energy_source_contracts", "acquire_energy_source", "publish_energy_prices_to_market",
                                      "register_energy_schemas"]},
            "identity": {"reuses": ["GeospatialStore (resolution, reviews, places)", "canonical_entities",
                                    "EntityHistoryStore (match / non-match / undo)"],
                         "tools": ["propose_energy_identity_matches", "review_energy_identity_match",
                                   "list_energy_identity_matches"]},
            "links": {"reuses": ["EnvironmentStore (read)", "market bar source references (read)"],
                      "tools": ["link_energy_records", "link_energy_facility", "list_energy_links"]},
            "queries": {"reuses": ["environment_vintages.diff_values"],
                        "tools": ["list_energy_series", "energy_generation_mix", "energy_observations",
                                  "energy_revision_history", "energy_capacity_as_of"]},
            "monitoring": {"reuses": ["SubscriptionStore (watermarks, events, outbox)"],
                           "tools": ["create_energy_monitor", "run_energy_monitor", "poll_energy_monitor"]},
        },
    },
    "never": NEVER,
}
_DDL = """
CREATE TABLE IF NOT EXISTS energy_bundle_state(
 namespace TEXT PRIMARY KEY, enabled BOOLEAN NOT NULL, changed_by TEXT NOT NULL, changed_at_ms BIGINT NOT NULL);
"""


class BundleError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _coordinator(conn):
    if not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='composition_authority'").fetchone():
        return None
    from src.composition.lifecycle import CompositionCoordinator

    coordinator = CompositionCoordinator(conn)
    return coordinator if coordinator.is_composition_managed(BUNDLE_ID) else None


def _selected(coordinator):
    plan = (coordinator.active() or {}).get("plan") or {}
    return any(p["id"] == BUNDLE_ID for p in plan.get("packs") or [])


def is_enabled(conn, namespace):
    coordinator = _coordinator(conn)
    if coordinator is not None:
        return _selected(coordinator)
    if not conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='energy_bundle_state'").fetchone():
        return True
    row = conn.execute("SELECT enabled FROM energy_bundle_state WHERE namespace=?", [namespace]).fetchone()
    return True if row is None else bool(row[0])


def require_enabled(conn, namespace):
    if not is_enabled(conn, namespace):
        raise BundleError("bundle_disabled", "Energy Systems is disabled; Climate and Environment, Market and Geospatial "
                                             "remain usable")


def set_enabled(conn, namespace, enabled, *, principal_id, scopes, now=None):
    if "operator" not in scopes:
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
    conn.execute("INSERT INTO energy_bundle_state VALUES (?,?,?,?) ON CONFLICT (namespace) DO UPDATE SET "
                 "enabled=excluded.enabled, changed_by=excluded.changed_by, changed_at_ms=excluded.changed_at_ms",
                 [namespace, bool(enabled), principal_id, stamp])
    return {"namespace": namespace, "bundle": BUNDLE_ID, "enabled": bool(enabled), "shared_capabilities_affected": []}


def readiness(conn, namespace, *, scopes, now=None):
    """ready / fixture-only / stale / unavailable per provider; live verification kept separate."""

    from src.kb.energy_store import EnergyStore, table_exists

    if READ_SCOPE not in scopes and "operator" not in scopes:
        raise BundleError("unauthorized", "energy read scope is required")
    store = EnergyStore(conn, initialize=False, now=now)
    providers = {}
    for provider in PROVIDER_CONTRACTS:
        state = store.provider_state(namespace, provider) if table_exists(conn, "energy_provider_state") else {
            "last_success_ms": None, "stale": True}
        if state.get("last_success_ms") is None:
            status = "unavailable"
        elif state["stale"]:
            status = "stale"
        elif state.get("last_execution") == "network":
            status = "ready"
        else:
            status = "fixture-only"
        providers[provider] = {"status": status, "state": state, "live_verification": LIVE_VERIFICATION[provider]}
    statuses = {p["status"] for p in providers.values()}
    acquisition = "ready" if "ready" in statuses else "fixture-only" if "fixture-only" in statuses else (
        "stale" if "stale" in statuses else "unavailable")
    return {"bundle": BUNDLE_ID, "namespace": namespace, "enabled": is_enabled(conn, namespace),
            "providers": providers, "entry_points": {"acquisition": acquisition,
                                                     "queries": "ready" if acquisition != "unavailable" else "unavailable"},
            "evidence": "offline fixture results and live results are reported separately; see LIVE_VERIFICATION",
            **_composition_readiness(conn, scopes)}


def _composition_readiness(conn, scopes):
    coordinator = _coordinator(conn)
    if coordinator is None or not _selected(coordinator):
        return {}
    assessment = coordinator.readiness(scopes=scopes)
    plan = coordinator.active()["plan"]
    mine = {b["capability"] for b in plan["bindings"] if BUNDLE_ID in b["consumers"]}
    assessment["operations"] = [o for o in assessment["operations"] if o["capability"] in mine]
    return {"composition": assessment}
